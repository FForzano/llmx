#!/usr/bin/env python3
"""llmx: optional offload of bounded tasks to local or cheap OpenAI-compatible models.

Stdlib only (Python >= 3.11). Exit codes: 0 ok, 3 no endpoint available (the caller does the task itself),
4 the model answered but the output was unusable, 2 usage error. Never raises a hard failure for the skills:
exit 3/4 simply means "do it yourself".
"""
import argparse, json, os, re, subprocess, sys, time, tomllib, urllib.request, urllib.error
from pathlib import Path

HOME = Path.home()
USER_CFG = Path(os.environ.get("LLMX_CONFIG", HOME / ".config/llmx/config.toml"))
STATE = Path(os.environ.get("LLMX_STATE", HOME / ".local/state/llmx"))
HERE = Path(__file__).resolve().parent

# ---- tasks: system prompt, output kind, flags ------------------------------------------------------
# kind: text | findings | json.  ground: every `backticked` span in the answer must occur in the input (else the line is dropped).
# chunk: input larger than the endpoint context is split and the parts are processed separately (only for tasks that do not need the whole input).
# passes: findings tasks run twice on local endpoints; findings found by both passes are tagged [2/2], the others [1/2].
BT = " Write every file path, identifier, number, error message and copied line between backticks, exactly as in the input, without line-number prefixes."
FIND = ('Return ONLY a JSON array. Each item: {"file": str, "line": int, "severity": "blocker|major|minor", '
        '"quote": "<exact text copied from the input, <=120 chars>", "issue": "<one sentence>"}. '
        'Every item MUST quote the input verbatim; no quote, no item. Empty array if nothing is wrong. No prose.')
SMALL_SKIP = {"log-triage", "summarize", "module-summary", "commit-draft", "review-scan", "lang-scan"}   # below policy.min_chars the offload costs more than it saves
TASKS = {
    "ask":          dict(kind="text", system="Answer precisely and briefly."),
    "summarize":    dict(kind="text", ground=True, chunk=True, system="Summarize the input faithfully in at most 10 short lines. Keep names, numbers, file paths and error messages exactly. Add nothing." + BT),
    "log-triage":   dict(kind="text", ground=True, chunk=True, system="The input is a log or test output. List: (1) the first real error with its exact line, (2) the probable failing component, (3) at most 5 other relevant lines verbatim. No speculation about fixes." + BT),
    "module-summary": dict(kind="text", ground=True, system="For each file given, write ONE line: what it is for, its main public names. Facts from the code only, no guessing." + BT),
    "commit-draft": dict(kind="text", check="commit", system="Draft a git commit message for the diff: subject <=72 chars, imperative, what+scope, no final period; blank line; body only if the WHY is evident from the diff, wrapped at 72. Describe only what the diff does. Output the message only."),
    "test-draft":   dict(kind="text", check="code", system="Write the tests requested for the given code, in the project's test framework and style visible in the examples. Output code only, in one fenced block. Cover edge cases and errors, not only the happy path."),
    "review-scan":  dict(kind="findings", chunk=True, passes=2, system="You review code as a strict colleague. Report only a defect you can demonstrate from the quoted line: a concrete input or state that gives wrong behaviour, a crash, a resource leak, a security hole, or clearly duplicated logic. Do NOT report style, naming, hypothetical improvements, missing features, line endings or whitespace. Prefer an empty array to a guess. At most 6 items. " + FIND),
    "lang-scan":    dict(kind="findings", chunk=True, passes=2, system="You proofread English scientific text (LaTeX source). Report typos, grammar, passive voice hiding the agent, evaluative adjectives (very, novel, significant), sentences over 30 words, dangling 'this/it/which', two names for one object, American/British mixing. Ignore LaTeX commands. " + FIND),
    "claims-scan":  dict(kind="findings", chunk=False, passes=2, system="You check a scientific paper for claims stronger than their evidence: 'optimal', 'first', 'exceeds', numbers in abstract/intro that differ from the results, symbols used before definition. " + FIND),
    "referee":      dict(kind="text", system="You are a severe, fair, independent peer reviewer. Write: summary (3 lines), strengths, major concerns (each: claim, why a problem, where, what would resolve it), minor concerns, questions to authors. Quote the text for every concern. Say 'could not verify' instead of guessing. Never invent references."),
    "split-comments": dict(kind="json", system='The input is a reviewer report. Return ONLY a JSON array of {"reviewer": int, "comment": int, "text": "<verbatim>", "type": "request|question|criticism|typo|praise"}. Keep the original numbering and verbatim text.'),
}

# ---- config -----------------------------------------------------------------------------------------
def load_cfg(root=None):
    cfg = {"endpoints": {}, "tasks": {}, "policy": {}}
    paths = [USER_CFG]
    d = Path(root or os.getcwd()).resolve()
    for p in [d, *d.parents]:
        if (p / ".llmx.toml").exists():
            paths.append(p / ".llmx.toml"); break
    for p in paths:
        if p.exists():
            try: c = tomllib.loads(p.read_text())
            except Exception as e: print(f"llmx: bad config {p}: {e}", file=sys.stderr); continue
            for k in ("endpoints", "tasks", "policy"):
                if k == "endpoints":
                    for n, v in c.get(k, {}).items(): cfg[k].setdefault(n, {}).update(v)
                else: cfg[k].update(c.get(k, {}))
    return cfg

def enabled(cfg):
    return os.environ.get("LLMX_DISABLE") != "1" and cfg["policy"].get("enabled", True)

def http(url, body=None, key=None, timeout=10):
    h = {"Content-Type": "application/json"}
    if key: h["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())

def ep_key(ep):
    e = ep.get("api_key_env")
    return os.environ.get(e, "") if e else ""

def is_cloud(ep): return ep.get("kind", "local") == "cloud"

def ep_up(ep, quick=True):
    try:
        http(ep["base_url"].rstrip("/") + "/models", key=ep_key(ep), timeout=2 if quick else 5); return True
    except Exception: return False

def ep_usable(name, ep, cfg, task_cloud_ok=True):
    """(ok, reason)"""
    if is_cloud(ep):
        if not cfg["policy"].get("allow_cloud", False) or not task_cloud_ok: return False, "cloud not allowed (policy.allow_cloud)"
        if ep.get("api_key_env") and not ep_key(ep): return False, f"${ep['api_key_env']} not set"
    return True, ""

def state_file(name): STATE.mkdir(parents=True, exist_ok=True); return STATE / f"{name}.pid"

def free_gb():
    """Memory a new model can take without swapping: free + inactive + speculative + purgeable (macOS) / MemAvailable (Linux). None if unknown."""
    if os.environ.get("LLMX_FREE_GB"): return float(os.environ["LLMX_FREE_GB"])      # tests
    try:
        if sys.platform == "darwin":
            # macOS's own estimate of what is available (free + purgeable + reclaimable file cache); vm_stat alone undercounts the cache
            mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"free percentage:\s*(\d+)%", mp)
            if m: return int(m.group(1)) / 100 * int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
            o = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            ps = int(re.search(r"page size of (\d+)", o).group(1))
            n = sum(int(m.group(1)) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable") for m in [re.search(k + r":\s+(\d+)", o)] if m)
            return n * ps / 2**30
        for l in open("/proc/meminfo"):
            if l.startswith("MemAvailable:"): return int(l.split()[1]) / 2**20
    except Exception: return None

def make_room(name, ep, cfg):
    """One big local model in RAM at a time. If `mem_gb` is set and free memory is short, stop the other local servers llmx started
    (never ones started by hand) and wait for the memory to come back. -> (ok, reason)."""
    need = ep.get("mem_gb")
    if not need: return True, ""
    need += cfg["policy"].get("mem_margin_gb", 3)
    f = free_gb()
    if f is None or f >= need: return True, ""
    others = [(n, e) for n, e in cfg["endpoints"].items() if n != name and not is_cloud(e) and e.get("mem_gb") and ep_up(e)]
    mine = [n for n, e in others if state_file(n).exists()]
    for n in mine: stop(n)
    if cfg["policy"].get("stop_foreign", False):          # opt-in: also stop a server of ours that was started by hand (found by its port)
        for n, e in others:
            if n in mine: continue
            port = re.search(r":(\d+)", e["base_url"].split("//", 1)[-1])
            pids = subprocess.run(["lsof", "-ti", f"tcp:{port.group(1)}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split() if port else []
            for pid in pids:
                try: os.kill(int(pid), 15); mine.append(n)
                except Exception: pass
    t0 = time.time()
    while mine and time.time() - t0 < cfg["policy"].get("mem_wait_s", 30):
        f = free_gb()
        if f is None or f >= need: return True, ""
        time.sleep(1.5)
    byhand = [n for n, e in others if n not in mine]
    return False, f"not enough free memory ({f:.0f} GB free, {need:.0f} needed)" + (f"; {', '.join(byhand)} runs outside llmx: stop it yourself" if byhand else "; close other apps")

def start(name, ep, cfg=None):
    cmd = ep.get("start")
    if not cmd: return False
    if cfg is not None:
        ok, why = make_room(name, ep, cfg)
        if not ok: print(f"llmx: {name}: {why}", file=sys.stderr); return False
    STATE.mkdir(parents=True, exist_ok=True)
    log = open(STATE / f"{name}.log", "ab")
    p = subprocess.Popen(cmd, shell=True, stdout=log, stderr=log, start_new_session=True)
    state_file(name).write_text(str(p.pid))
    t0 = time.time()
    while time.time() - t0 < ep.get("start_timeout", 120):
        if ep_up(ep): return True
        if p.poll() is not None: state_file(name).unlink(missing_ok=True); return False
        time.sleep(1.5)
    return False   # still alive: loading or downloading the model (see loading())

def alive(pid):
    try: os.kill(pid, 0); return True
    except Exception: return False

def touch_and_watch(name, ep):
    """Mark the endpoint as used; for servers started by llmx, make sure an idle watcher runs (idle_minutes, default 10, 0 = never stop)."""
    if not state_file(name).exists() or not ep.get("idle_minutes", 10): return
    (STATE / f"{name}.last").write_text(str(time.time()))
    w = STATE / f"{name}.watch"
    try:
        if w.exists() and alive(int(w.read_text())): return
    except Exception: pass
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_watch", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    w.write_text(str(p.pid))

def cmd_watch(a):
    while True:
        time.sleep(30)
        ep = load_cfg()["endpoints"].get(a.name, {}); limit = ep.get("idle_minutes", 10) * 60
        try: last = float((STATE / f"{a.name}.last").read_text())
        except Exception: last = 0
        if not state_file(a.name).exists(): break
        if loading(a.name, ep): (STATE / f"{a.name}.last").write_text(str(time.time())); continue
        if limit and time.time() - last > limit: stop(a.name); break
    (STATE / f"{a.name}.watch").unlink(missing_ok=True); return 0

def loading(name, ep):
    """True if llmx started this server, it is alive but not answering yet (first run: model download)."""
    f = state_file(name)
    try: return f.exists() and alive(int(f.read_text())) and not ep_up(ep)
    except Exception: return False

def stop(name):
    f = state_file(name)
    if f.exists():
        try: os.killpg(int(f.read_text()), 15)
        except Exception: pass
        f.unlink(missing_ok=True); return True
    return False

# ---- running ----------------------------------------------------------------------------------------
class Fail(Exception): pass
class CtxError(Fail): pass      # the server says the prompt exceeds its context: the caller retries with smaller chunks

def strip_think(s): return re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()

def numbered(path):
    try: lines = Path(path).read_text(errors="replace").splitlines()
    except Exception as e: return f"[cannot read {path}: {e}]"
    return f"=== {path} ===\n" + "\n".join(f"{i+1}| {l}" for i, l in enumerate(lines))

def norm(s): return re.sub(r"\s+", " ", s).strip()
def raw_of(text): return re.sub(r"(?m)^\d+\| ", "", text)      # the input as the model should quote it: no line-number prefixes

def parse_json(s):
    s = strip_think(s)
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m: s = m.group(1)
    a = min([i for i in (s.find("["), s.find("{")) if i >= 0], default=-1)
    if a < 0: raise ValueError("no JSON")
    end = max(s.rfind("]"), s.rfind("}"))
    return json.loads(s[a:end + 1])

def verify_findings(items, files, raw_input):
    """Keep only findings whose quote really occurs in the input (and near the stated line when the file is known)."""
    hay = norm(raw_of(raw_input)); filelines = {}
    for f in files:
        try: filelines[f] = Path(f).read_text(errors="replace").splitlines()
        except Exception: pass
    keep, dropped = [], 0
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict): dropped += 1; continue
        q = norm(str(it.get("quote", "")))
        if len(q) < 6 or q not in hay: dropped += 1; continue
        f, ln = it.get("file"), it.get("line")
        fl = next((v for k, v in filelines.items() if f and (k == f or k.endswith(str(f)) or str(f).endswith(k))), None)
        if fl is not None and isinstance(ln, int):
            win = norm(" ".join(fl[max(0, ln - 4): ln + 3]))
            if q not in win:
                # relocate: exact line of the quote if it occurs once, else drop
                hits = [i + 1 for i, l in enumerate(fl) if q in norm(l)]
                if len(hits) == 1: it["line"] = hits[0]
                else: dropped += 1; continue
        keep.append(it)
    return keep, dropped

def span_ok(sp, hay):
    """A backticked span is grounded if it occurs in the input, ignoring line-number prefixes and with "..." / "…" standing for any text."""
    sp = norm(re.sub(r"(?m)^\d+\| ", "", sp))
    pieces = [x.strip() for x in re.split(r"\.{3}|…", sp)]
    return all(len(x) < 4 or x in hay for x in pieces)

def ground_text(out, raw_input, samples=None):
    """Text tasks: a line whose `backticked` span is not in the input is invented: drop it. Returns (text, dropped)."""
    hay = norm(raw_of(raw_input)); keep, dropped = [], 0
    for l in out.splitlines():
        bad = [sp for sp in re.findall(r"`([^`\n]+)`", l) if not span_ok(sp, hay)]
        if bad:
            dropped += 1
            if samples is not None and len(samples) < 3: samples.append(bad[0][:80])
            continue
        keep.append(l)
    return "\n".join(keep).strip(), dropped

# ---- mechanical log reduction: no model, no tokens. Collapses repeats, then keeps errors with context, head and tail ----------
ERRX = re.compile(r"(error|exception|traceback|fatal|failed|fail\b|assert|panic|undefined|cannot|denied|timeout|warn)", re.I)
def reduce_log(text, limit=20000):
    lines = text.splitlines(); out = []; prev = None; run = 0
    def key(l): return re.sub(r"\d+", "#", re.sub(r"^\d+\| ", "", l)).strip()
    for l in lines:
        k = key(l)
        if k and k == prev: run += 1; continue
        if run: out.append(f"[... {run} more similar lines]")
        out.append(l); prev = k; run = 0
    if run: out.append(f"[... {run} more similar lines]")
    if sum(len(l) + 1 for l in out) <= limit: return "\n".join(out)
    n = len(out); keep = set(range(min(15, n))) | set(range(max(0, n - 30), n))
    for i, l in enumerate(out):
        if ERRX.search(l): keep |= set(range(max(0, i - 2), min(n, i + 3)))
    res = []; last = -1
    for i in sorted(keep):
        if i != last + 1: res.append(f"[... {i - last - 1} lines omitted]")
        res.append(out[i][:400]); last = i
    if last < n - 1: res.append(f"[... {n - 1 - last} lines omitted]")
    txt = "\n".join(res)
    return txt if len(txt) <= limit else txt[:limit // 2] + "\n[... middle cut ...]\n" + txt[-limit // 2:]

def check_commit(out):
    lines = out.strip().splitlines()
    if not lines: return "empty message"
    if len(lines[0]) > 72: return f"subject is {len(lines[0])} chars, must be <= 72"
    if lines[0].rstrip().endswith("."): return "subject must not end with a period"
    if re.match(r"(?i)^(here is|here's|commit message)", lines[0]): return "output the message only, no preamble"
    if len(lines) > 1 and lines[1].strip(): return "second line must be blank"
    return None

def strip_fence(s):
    s = strip_think(s); m = re.search(r"```[\w+-]*\n(.*?)```", s, re.S)
    return (m.group(1) if m else s).strip("\n")

def log_usage(rec):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "usage.jsonl", "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception: pass

def chat(ep, messages, max_tokens, temp):
    body = {"model": ep.get("model", "default"), "messages": messages, "max_tokens": max_tokens, "temperature": temp, "stream": False}
    if ep.get("no_think"): body["chat_template_kwargs"] = {"enable_thinking": False}
    body.update(ep.get("extra", {}))
    try: r = http(ep["base_url"].rstrip("/") + "/chat/completions", body, ep_key(ep), timeout=ep.get("timeout", 600))
    except urllib.error.HTTPError as e:
        if e.code == 400 and b"context" in e.read().lower(): raise CtxError("prompt exceeds the server context")
        raise
    return r["choices"][0]["message"].get("content") or "", r.get("usage", {})

# ---- chunking: input larger than the endpoint context is processed in parts (log, diff, one paper section...) ----
def pieces(part, budget, overlap):
    """Split one input part into pieces <= budget chars, on line boundaries; a '=== file ===' header is repeated in every piece."""
    if len(part) <= budget: return [part]
    lines = part.splitlines(); head = lines[0] if lines and lines[0].startswith("=== ") else ""
    body = lines[1:] if head else lines; out, i = [], 0
    while i < len(body):
        cur, n, j = [], len(head), i
        while j < len(body) and n + len(body[j]) + 1 <= budget: cur.append(body[j]); n += len(body[j]) + 1; j += 1
        if j == i: cur, j = [body[i][:budget]], i + 1          # one single huge line
        out.append("\n".join(([head] if head else []) + cur))
        i = j if j >= len(body) else max(i + 1, j - overlap)
    return out

def make_units(parts, budget, overlap):
    units, cur, n = [], [], 0
    for p in parts:
        for pc in pieces(p, budget, overlap):
            if cur and n + len(pc) + 2 > budget: units.append(cur); cur, n = [], 0
            cur.append(pc); n += len(pc) + 2
    if cur: units.append(cur)
    return ["\n\n".join(u) for u in units]

# ---- one task on one endpoint ----------------------------------------------------------------------------------
def run_on(ep, t, task, prefix, parts, files, a):
    """-> dict(out, ...). Raises Fail(reason). Token estimates are rough (logs tokenize badly): on a context error, retry with chunks half the size."""
    for scale in (1.0, 0.5, 0.25):
        try: return run_on1(ep, t, task, prefix, parts, files, a, scale)
        except CtxError: continue
    raise Fail("input still exceeds the context after splitting")

def run_on1(ep, t, task, prefix, parts, files, a, scale):
    system = a.system or t["system"]; maxtok = a.max_tokens or ep.get("max_tokens", 2048)
    ctx = ep.get("context", 32768); budget = int(ctx * 0.75 * 3.2 * scale) - len(system) - len(prefix) - maxtok
    chunkable = t.get("chunk", False)
    units = make_units(parts, budget, 5 if t["kind"] == "findings" else 0)
    total = sum(len(p) for p in parts)
    if len(units) > 1 and not chunkable: raise Fail(f"input ~{int(total/3.2)} tok too large for context")
    if len(units) > ep.get("max_chunks", 8): raise Fail(f"input needs {len(units)} chunks (max {ep.get('max_chunks', 8)})")
    npass = (a.passes or ep.get("passes") or t.get("passes", 1)) if t["kind"] == "findings" and not is_cloud(ep) else 1
    pin = pout = kept_n = dropped_n = 0; outs = []; samples = []
    for k, unit in enumerate(units):
        more = f"(Part {k+1} of {len(units)} of a longer input. If this part holds nothing relevant to the task, answer exactly: NONE)\n\n" if len(units) > 1 and t["kind"] == "text" else ""
        prompt = prefix + more + unit; msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if t["kind"] == "findings":
            runs = []
            for p in range(npass):
                try: raw, u = chat(ep, msgs, maxtok, ep.get("temperature", 0.2) + 0.4 * p)
                except CtxError: raise
                except Exception as e:
                    if p == 0: raise Fail(f"{type(e).__name__} {str(e)[:80]}")
                    break
                pin += u.get("prompt_tokens", len(prompt) // 3); pout += u.get("completion_tokens", len(raw) // 4)
                try: items = parse_json(raw)
                except Exception:
                    if p == 0: raise Fail("output not JSON")
                    continue
                items, d = verify_findings(items, files, unit); dropped_n += d; runs.append(items)
            merged = {}
            for items in runs:
                for it in items:
                    key = (norm(str(it.get("quote", ""))).lower()[:60], str(it.get("file", "")))
                    if key in merged: merged[key][1] += 1
                    else: merged[key] = [it, 1]
            n_ok = len(runs)
            for it, c in merged.values(): it["_agree"] = f"[{c}/{n_ok}] " if n_ok > 1 else ""; it["_c"] = c
            outs.append(list(merged.values()))
            continue
        try: raw, u = chat(ep, msgs, maxtok, ep.get("temperature", 0.2))
        except CtxError: raise
        except Exception as e: raise Fail(f"{type(e).__name__} {str(e)[:80]}")
        pin += u.get("prompt_tokens", len(prompt) // 3); pout += u.get("completion_tokens", len(raw) // 4)
        out = strip_think(raw)
        if t["kind"] == "json":
            try: outs.append(parse_json(out))
            except Exception: raise Fail("output not JSON")
            continue
        # text: mechanical validation, one repair round (the model sees the exact problem)
        problem = check_commit(out) if t.get("check") == "commit" else None
        if t.get("check") == "code": out = strip_fence(out)
        if problem:
            msgs += [{"role": "assistant", "content": out}, {"role": "user", "content": f"Problem: {problem}. Fix it. Output the corrected message only."}]
            try:
                raw, u = chat(ep, msgs, maxtok, ep.get("temperature", 0.2)); out = strip_think(raw)
                pin += u.get("prompt_tokens", 0); pout += u.get("completion_tokens", len(raw) // 4)
            except Exception as e: raise Fail(f"{type(e).__name__} {str(e)[:80]}")
            problem = check_commit(out)
            if problem: raise Fail(f"commit message invalid after repair: {problem}")
        if t.get("ground"):
            out, d = ground_text(out, unit, samples); dropped_n += d
        if len(units) > 1 and re.fullmatch(r"\W*NONE\W*", out or "NONE"): continue
        outs.append(out)
    # ---- merge units ----
    if t["kind"] == "findings":
        seen, flat = set(), []
        for ch in outs:
            for it, c in ch:
                key = (norm(str(it.get("quote", ""))).lower()[:60], str(it.get("file", "")))
                if key not in seen: seen.add(key); flat.append(it)
        sev = {"blocker": 0, "major": 1, "minor": 2}
        flat.sort(key=lambda i: (-i.get("_c", 1), sev.get(str(i.get("severity")), 2)))
        kept_n = len(flat)
        out = "\n".join(f"{i.get('_agree','')}{i.get('file','?')}:{i.get('line','?')} [{i.get('severity','minor')}] {i.get('issue','')} — \"{norm(str(i.get('quote','')))[:100]}\"" for i in flat) or "(no findings)"
    elif t["kind"] == "json":
        out = json.dumps([x for ch in outs for x in (ch if isinstance(ch, list) else [ch])], ensure_ascii=False, indent=1)
    else:
        out = "(nothing relevant found)" if not outs else outs[0] if len(outs) == 1 else "\n".join(f"--- part {i+1}/{len(outs)} ---\n{o}" for i, o in enumerate(outs))
    return dict(out=out, pin=pin, pout=pout, kept=kept_n, dropped=dropped_n, samples=samples, chunks=len(units), passes=npass)

def read_inputs(a):
    parts = []
    if a.file: parts += [numbered(f) for f in a.file]
    if a.input: parts.append(sys.stdin.read() if a.input == "-" else Path(a.input).read_text(errors="replace"))
    elif not a.file and not sys.stdin.isatty(): parts.append(sys.stdin.read())
    return parts

def pick_endpoint(cfg, name, a, why, local_only):
    ep = cfg["endpoints"].get(name)
    if not ep: why.append(f"{name}: not defined"); return None
    ok, r = ep_usable(name, ep, cfg, not local_only)
    if not ok: why.append(f"{name}: {r}"); return None
    if local_only and is_cloud(ep): why.append(f"{name}: --local-only"); return None
    if not ep_up(ep):
        if loading(name, ep): why.append(f"{name}: still loading/downloading (llmx status)"); return None
        if not (ep.get("autostart") and start(name, ep, cfg)):
            if loading(name, ep): touch_and_watch(name, ep)
            why.append(f"{name}: not reachable" + (" (started, still loading)" if loading(name, ep) else "")); return None
    return ep

def run_check(cmd, path, tail=40):
    """Run the user's check command ({out} = the draft file). -> (ok, output tail)."""
    try: p = subprocess.run(cmd.replace("{out}", str(path)), shell=True, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired: return False, "check timed out"
    o = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode == 0, "\n".join(o[-tail:])

def cmd_run(a):
    cfg = load_cfg()
    if not enabled(cfg): print("llmx: disabled", file=sys.stderr); return 3
    t = TASKS.get(a.task)
    if not t: print(f"llmx: unknown task {a.task} (llmx tasks)", file=sys.stderr); return 2
    if a.check and not a.out: print("llmx: --check needs --out FILE", file=sys.stderr); return 2
    parts = read_inputs(a)
    if not "".join(parts).strip(): print("llmx: empty input", file=sys.stderr); return 2
    orig_tok = len("".join(parts)) // 3
    min_chars = int(os.environ.get("LLMX_MIN_CHARS", cfg.get("policy", {}).get("min_chars", 2500)))
    if a.task in SMALL_SKIP and len("".join(parts)) < min_chars and not a.force:
        print(f"llmx: input only ~{orig_tok} tokens: reading it yourself costs less than the offload plus the verification (--force to override)", file=sys.stderr); return 3
    if a.task == "log-triage" and not a.raw:
        parts = [reduce_log(p) for p in parts]
        if len("".join(parts)) // 3 < orig_tok: print(f"llmx: log reduced mechanically ~{orig_tok}→~{len(''.join(parts)) // 3} tokens before the model", file=sys.stderr)
    prefix = (a.prompt + "\n\n" if a.prompt else "")
    order = [a.endpoint] if a.endpoint else (cfg["tasks"].get(a.task) or cfg["tasks"].get("default") or [])
    why = []
    for name in order:
        ep = pick_endpoint(cfg, name, a, why, a.local_only)
        if not ep: continue
        t0 = time.time()
        try:
            touch_and_watch(name, ep); r = run_on(ep, t, a.task, prefix, parts, a.file or [], a)
        except Fail as e: why.append(f"{name}: {e}"); continue
        note = ""
        if r["kept"] or r["dropped"]: note += f", {r['kept']} kept, {r['dropped']} dropped (not in input)"
        if r["chunks"] > 1: note += f", {r['chunks']} chunks"
        if r["passes"] > 1: note += f", {r['passes']} passes"
        out = r["out"]
        if a.check:   # run the draft against the project's own check; one repair round with the real error
            Path(a.out).write_text(out + "\n"); ok, log = run_check(a.check, a.out)
            if not ok:
                msgs = [{"role": "system", "content": a.system or t["system"]}, {"role": "user", "content": prefix + "\n\n".join(parts)},
                        {"role": "assistant", "content": out}, {"role": "user", "content": f"Running the draft failed:\n{log}\nFix the draft. Output code only, in one fenced block."}]
                try:
                    raw, u = chat(ep, msgs, a.max_tokens or ep.get("max_tokens", 2048), ep.get("temperature", 0.2))
                    out = strip_fence(raw); Path(a.out).write_text(out + "\n"); ok, log = run_check(a.check, a.out)
                    r["pin"] += u.get("prompt_tokens", 0); r["pout"] += u.get("completion_tokens", 0)
                except Exception as e: log += f"\n(repair failed: {type(e).__name__})"
            if not ok:
                print(f"llmx: {a.task} draft kept in {a.out} but the check still fails:\n{log}\n(do the task yourself; the draft may help)", file=sys.stderr); return 4
            note += ", check passed"
        log_usage(dict(ts=int(time.time()), task=a.task, endpoint=name, cloud=is_cloud(ep), pin=r["pin"], pout=r["pout"], kept=r["kept"], dropped=r["dropped"],
                       chunks=r["chunks"], passes=r["passes"], orig=orig_tok, samples=r.get("samples", []), cost_in=ep.get("cost_in", 0), cost_out=ep.get("cost_out", 0), s=round(time.time() - t0, 1)))
        touch_and_watch(name, ep)
        print(out)
        print(f"llmx: {a.task} via {name} ({r['pin']}→{r['pout']} tok, {time.time()-t0:.0f}s{note}); candidate output: verify before relying on it", file=sys.stderr)
        return 0
    print("llmx: no endpoint available (" + "; ".join(why or ["none configured for this task"]) + "): do the task yourself", file=sys.stderr)
    return 3 if not why or not any(k in w for w in why for k in ("not JSON", "invalid after repair")) else 4

# ---- eval: measure a model on known cases (shipped + your own in ~/.config/llmx/evals/cases.json) ------------------
def cmd_eval(a):
    cfg = load_cfg(); cases = []
    for base in (HERE.parent / "evals", HOME / ".config/llmx/evals"):
        f = base / "cases.json"
        if f.exists():
            for c in json.loads(f.read_text()): c["_dir"] = base; cases.append(c)
    cases = [c for c in cases if not a.task or c["task"] == a.task]
    if not cases: print("no eval cases"); return 2
    stat = {}
    for c in cases:
        t = TASKS[c["task"]]; order = [a.endpoint] if a.endpoint else (cfg["tasks"].get(c["task"]) or cfg["tasks"].get("default") or [])
        why = []; ep = next((e for e in (pick_endpoint(cfg, n, a, why, True) for n in order) if e), None)
        name = next((n for n in order if cfg["endpoints"].get(n) is ep), "?") if ep else None
        if not ep: print(f"  skip {c['name']}: {'; '.join(why)}"); continue
        path = c["_dir"] / c["file"]; parts = [numbered(str(path))] if t["kind"] == "findings" or c.get("numbered") else [path.read_text()]
        class A: system = None; max_tokens = None; passes = a.passes
        t0 = time.time()
        try: r = run_on(ep, t, c["task"], c.get("prompt", ""), parts, [str(path)] if t["kind"] == "findings" else [], A)
        except Fail as e: print(f"  FAIL {c['name']}: {e}"); stat.setdefault((c["task"], name), []).append(dict(fail=1)); continue
        out = r["out"]; exp = c.get("expect", [])
        hit = [e for e in exp if any(norm(x).lower() in norm(out).lower() for x in ([e] if isinstance(e, str) else e))]
        fp = 0
        if t["kind"] == "findings" and not exp: fp = 0 if out == "(no findings)" else len(out.splitlines())
        st = dict(hit=len(hit), exp=len(exp), fp=fp, dropped=r["dropped"], kept=r["kept"], s=time.time() - t0, tok=r["pin"] + r["pout"])
        stat.setdefault((c["task"], name), []).append(st)
        print(f"  {'ok  ' if len(hit) == len(exp) and not fp else 'MISS'} {c['name']:24} found {len(hit)}/{len(exp)}  false-positives {fp}  gate-dropped {r['dropped']}  {st['s']:.0f}s")
    print("summary (task, endpoint: recall, false positives on clean cases, hallucinations caught by the gate):")
    for (task, name), L in sorted(stat.items()):
        e = sum(x.get("exp", 0) for x in L); h = sum(x.get("hit", 0) for x in L)
        print(f"  {task:14} {name:12} recall {h}/{e}  false-pos {sum(x.get('fp', 0) for x in L)}  gate-dropped {sum(x.get('dropped', 0) for x in L)}  failed-runs {sum(x.get('fail', 0) for x in L)}  cases {len(L)}")
    return 0

def cmd_status(a):
    cfg = load_cfg(); res = {}
    print(f"config: {USER_CFG} ({'found' if USER_CFG.exists() else 'MISSING: llmx init'}); enabled={enabled(cfg)}; allow_cloud={cfg['policy'].get('allow_cloud', False)}")
    fg = free_gb()
    if fg is not None: print(f"free memory: {fg:.0f} GB (a local endpoint with mem_gb starts only if it fits; llmx stops the other local server it started)")
    for n, ep in cfg["endpoints"].items():
        ok, r = ep_usable(n, ep, cfg)
        up = ep_up(ep) if ok else False
        res[n] = up
        s = "UP" if up else ("LOADING (downloading/loading the model)" if loading(n, ep) else ("startable" if ep.get("start") and ep.get("autostart") and ok else "down"))
        print(f"  {n:14} {ep.get('kind','local'):5} {ep.get('model','?'):28} {s}{'  ('+r+')' if r else ''}")
    print("tasks:")
    for t in sorted(TASKS):
        order = cfg["tasks"].get(t) or cfg["tasks"].get("default") or []
        first = next((n for n in order if n in res and (res[n] or cfg['endpoints'][n].get('autostart')) and ep_usable(n, cfg['endpoints'][n], cfg)[0]), None)
        print(f"  {t:15} -> {first or 'claude (no endpoint)'}   [{', '.join(order)}]")
    return 0

def cmd_stats(a):
    f = STATE / "usage.jsonl"
    if not f.exists(): print("no usage yet"); return 0
    agg = {}
    for l in f.read_text().splitlines():
        try: r = json.loads(l)
        except Exception: continue
        k = (r["task"], r["endpoint"]); g = agg.setdefault(k, [0, 0, 0, 0.0, 0, 0, 0, 0.0])
        g[0] += 1; g[1] += r.get("orig", r["pin"]); g[2] += r["pout"]; g[3] += (r["pin"] * r["cost_in"] + r["pout"] * r["cost_out"]) / 1e6
        g[4] += r.get("kept", 0); g[5] += r.get("dropped", 0); g[6] += r["pin"]; g[7] += r.get("s", 0)
    tin = tout = 0
    for (t, e), (n, i, o, c, kp, dr, pin, sec) in sorted(agg.items()):
        gate = f" gate-dropped={dr}/{kp + dr}" if kp + dr else ""
        print(f"{t:15} {e:14} calls={n:4} orig_in={i:8} sent={pin:8} out={o:7} {sec:6.0f}s cost=${c:.4f}{gate}"); tin += i; tout += o
    print(f"offloaded: ~{tin} input tokens Claude did not read; Claude reads {tout} output tokens instead (net about {tin - tout} fewer, before its verification reads)")
    print("estimate only: it counts runs whose output the gate emptied as savings. A high gate-dropped share = that model invents: `llmx eval`, or use a stronger endpoint")
    return 0

def cmd_init(a):
    src = HERE.parent / "templates" / "config.example.toml"
    if USER_CFG.exists() and not a.force: print(f"{USER_CFG} exists (use --force)"); return 1
    USER_CFG.parent.mkdir(parents=True, exist_ok=True); USER_CFG.write_text(src.read_text()); print(f"written {USER_CFG}: edit endpoints, then `llmx status`"); return 0

def cmd_up(a):
    ep = load_cfg()["endpoints"].get(a.name)
    if not ep: print("unknown endpoint"); return 2
    if ep_up(ep): print("up"); return 0
    if not loading(a.name, ep) and not start(a.name, ep, load_cfg()) and not loading(a.name, ep):
        print(f"failed (see {STATE}/{a.name}.log)"); return 1
    touch_and_watch(a.name, ep)
    if ep_up(ep): print("up"); return 0
    print(f"started, still loading (first run downloads the model: several GB). Follow it: tail -f {STATE}/{a.name}.log ; `llmx status` shows UP when ready"); return 0

def cmd_down(a): print("stopped" if stop(a.name) else "nothing started by llmx"); return 0
def cmd_tasks(a):
    for k, v in TASKS.items(): print(f"{k:15} {v['kind']:9} {v['system'][:90]}")
    return 0

def main():
    p = argparse.ArgumentParser(prog="llmx", description=__doc__.split("\n")[0]); s = p.add_subparsers(dest="c", required=True)
    r = s.add_parser("run", help="run a task on the first usable endpoint"); r.add_argument("task")
    r.add_argument("--file", "-f", action="append", help="file given to the model with line numbers (repeatable)")
    r.add_argument("--in", dest="input", help="input file or - for stdin"); r.add_argument("--prompt", "-p", help="extra instruction")
    r.add_argument("--system"); r.add_argument("--endpoint"); r.add_argument("--max-tokens", type=int)
    r.add_argument("--local-only", action="store_true", help="never use a cloud endpoint, whatever the policy")
    r.add_argument("--passes", type=int, help="findings tasks: independent passes (default 2 on local endpoints); findings found by every pass are tagged [n/n]")
    r.add_argument("--check", help="test-draft: command run on the draft ({out} = the file); on failure the model gets one repair round with the real error")
    r.add_argument("--force", action="store_true", help="run even if the input is small"); r.add_argument("--raw", action="store_true", help="log-triage: do not pre-reduce the log")
    r.add_argument("--out", help="file where --check finds the draft"); r.set_defaults(fn=cmd_run)
    e = s.add_parser("eval", help="measure endpoints on known cases (recall, false positives, invented findings)"); e.add_argument("task", nargs="?")
    e.add_argument("--endpoint"); e.add_argument("--passes", type=int); e.set_defaults(fn=cmd_eval)
    w = s.add_parser("_watch"); w.add_argument("name"); w.set_defaults(fn=cmd_watch)
    s.add_parser("status").set_defaults(fn=cmd_status); s.add_parser("stats").set_defaults(fn=cmd_stats); s.add_parser("tasks").set_defaults(fn=cmd_tasks)
    i = s.add_parser("init"); i.add_argument("--force", action="store_true"); i.set_defaults(fn=cmd_init)
    for n, f in (("up", cmd_up), ("down", cmd_down)): x = s.add_parser(n); x.add_argument("name"); x.set_defaults(fn=f)
    a = p.parse_args()
    try: sys.exit(a.fn(a))
    except KeyboardInterrupt: sys.exit(130)

if __name__ == "__main__": main()
