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

# ---- tasks: system prompt, output kind, hard input limit hint -------------------------------------
FIND = ('Return ONLY a JSON array. Each item: {"file": str, "line": int, "severity": "blocker|major|minor", '
        '"quote": "<exact text copied from the input, <=120 chars>", "issue": "<one sentence>"}. '
        'Every item MUST quote the input verbatim; no quote, no item. Empty array if nothing is wrong. No prose.')
TASKS = {
    "ask":          dict(kind="text", system="Answer precisely and briefly."),
    "summarize":    dict(kind="text", system="Summarize the input faithfully in at most 10 short lines. Keep names, numbers, file paths and error messages exactly. Add nothing."),
    "log-triage":   dict(kind="text", system="The input is a log or test output. List: (1) the first real error with its exact line, (2) the probable failing component, (3) at most 5 other relevant lines verbatim. No speculation about fixes."),
    "module-summary": dict(kind="text", system="For each file given, write ONE line: what it is for, its main public names. Facts from the code only, no guessing."),
    "commit-draft": dict(kind="text", system="Draft a git commit message for the diff: subject <=72 chars, imperative, what+scope; blank line; body only if the WHY is evident from the diff, wrapped at 72. Describe only what the diff does."),
    "test-draft":   dict(kind="text", system="Write the tests requested for the given code, in the project's test framework and style visible in the examples. Output code only. Cover edge cases and errors, not only the happy path."),
    "review-scan":  dict(kind="findings", system="You review a code diff as a strict colleague: wrong behaviour, edge cases, missing error handling, security, duplicated logic, missing tests. " + FIND),
    "lang-scan":    dict(kind="findings", system="You proofread English scientific text (LaTeX source). Report typos, grammar, passive voice hiding the agent, evaluative adjectives (very, novel, significant), sentences over 30 words, dangling 'this/it/which', two names for one object, American/British mixing. Ignore LaTeX commands. " + FIND),
    "claims-scan":  dict(kind="findings", system="You check a scientific paper for claims stronger than their evidence: 'optimal', 'first', 'exceeds', numbers in abstract/intro that differ from the results, symbols used before definition. " + FIND),
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

def start(name, ep):
    cmd = ep.get("start")
    if not cmd: return False
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
def strip_think(s): return re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()

def numbered(path):
    try: lines = Path(path).read_text(errors="replace").splitlines()
    except Exception as e: return f"[cannot read {path}: {e}]"
    return f"=== {path} ===\n" + "\n".join(f"{i+1}| {l}" for i, l in enumerate(lines))

def norm(s): return re.sub(r"\s+", " ", s).strip()

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
    hay = norm(raw_input); filelines = {}
    for f in files:
        try: filelines[f] = Path(f).read_text(errors="replace").splitlines()
        except Exception: pass
    keep, dropped = [], 0
    for it in items if isinstance(items, list) else []:
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

def log_usage(rec):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "usage.jsonl", "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception: pass

def chat(ep, system, user, max_tokens, temp):
    body = {"model": ep.get("model", "default"), "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temp, "stream": False}
    if ep.get("no_think"): body["chat_template_kwargs"] = {"enable_thinking": False}
    body.update(ep.get("extra", {}))
    r = http(ep["base_url"].rstrip("/") + "/chat/completions", body, ep_key(ep), timeout=ep.get("timeout", 600))
    return r["choices"][0]["message"].get("content") or "", r.get("usage", {})

def cmd_run(a):
    cfg = load_cfg()
    if not enabled(cfg): print("llmx: disabled", file=sys.stderr); return 3
    t = TASKS.get(a.task)
    if not t: print(f"llmx: unknown task {a.task} (llmx tasks)", file=sys.stderr); return 2
    parts = []
    if a.file: parts += [numbered(f) for f in a.file]
    if a.input:
        parts.append(sys.stdin.read() if a.input == "-" else Path(a.input).read_text(errors="replace"))
    elif not a.file and not sys.stdin.isatty(): parts.append(sys.stdin.read())
    if not "".join(parts).strip(): print("llmx: empty input", file=sys.stderr); return 2
    prompt = (a.prompt + "\n\n" if a.prompt else "") + "\n\n".join(parts)
    system = a.system or t["system"]
    order = cfg["tasks"].get(a.task) or cfg["tasks"].get("default") or []
    if a.endpoint: order = [a.endpoint]
    est = len(prompt) / 3.2
    why = []
    for name in order:
        ep = cfg["endpoints"].get(name)
        if not ep: why.append(f"{name}: not defined"); continue
        ok, r = ep_usable(name, ep, cfg, not a.local_only)
        if not ok: why.append(f"{name}: {r}"); continue
        if a.local_only and is_cloud(ep): why.append(f"{name}: --local-only"); continue
        if est > ep.get("context", 32768) * 0.75: why.append(f"{name}: input ~{int(est)} tok too large for context"); continue
        if not ep_up(ep):
            if loading(name, ep): why.append(f"{name}: still loading/downloading (llmx status)"); continue
            if not (ep.get("autostart") and start(name, ep)):
                if loading(name, ep): touch_and_watch(name, ep)
                why.append(f"{name}: not reachable" + (" (started, still loading)" if loading(name, ep) else "")); continue
        try:
            t0 = time.time(); touch_and_watch(name, ep)
            out, u = chat(ep, system, prompt, a.max_tokens or ep.get("max_tokens", 2048), ep.get("temperature", 0.2))
        except Exception as e: why.append(f"{name}: {type(e).__name__} {str(e)[:80]}"); continue
        out = strip_think(out)
        note = ""
        if t["kind"] in ("findings", "json"):
            try: items = parse_json(out)
            except Exception: why.append(f"{name}: output not JSON"); continue
            if t["kind"] == "findings":
                items, dropped = verify_findings(items, a.file or [], "\n".join(parts))
                note = f", {len(items)} kept, {dropped} dropped (quote not in input)"
                out = "\n".join(f"{i.get('file','?')}:{i.get('line','?')} [{i.get('severity','minor')}] {i.get('issue','')} — \"{norm(str(i.get('quote','')))[:100]}\"" for i in items) or "(no findings)"
            else: out = json.dumps(items, ensure_ascii=False, indent=1)
        log_usage(dict(ts=int(time.time()), task=a.task, endpoint=name, cloud=is_cloud(ep), pin=u.get("prompt_tokens", int(est)),
                       pout=u.get("completion_tokens", len(out) // 4), cost_in=ep.get("cost_in", 0), cost_out=ep.get("cost_out", 0), s=round(time.time() - t0, 1)))
        touch_and_watch(name, ep)
        print(out)
        print(f"llmx: {a.task} via {name} ({u.get('prompt_tokens','?')}→{u.get('completion_tokens','?')} tok, {time.time()-t0:.0f}s{note}); candidate output: verify before relying on it", file=sys.stderr)
        return 0
    print("llmx: no endpoint available (" + "; ".join(why or ["none configured for this task"]) + "): do the task yourself", file=sys.stderr)
    return 3 if not why or not any("output not JSON" in w for w in why) else 4

def cmd_status(a):
    cfg = load_cfg(); res = {}
    print(f"config: {USER_CFG} ({'found' if USER_CFG.exists() else 'MISSING: llmx init'}); enabled={enabled(cfg)}; allow_cloud={cfg['policy'].get('allow_cloud', False)}")
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
        k = (r["task"], r["endpoint"]); g = agg.setdefault(k, [0, 0, 0, 0.0])
        g[0] += 1; g[1] += r["pin"]; g[2] += r["pout"]; g[3] += (r["pin"] * r["cost_in"] + r["pout"] * r["cost_out"]) / 1e6
    tin = tout = 0
    for (t, e), (n, i, o, c) in sorted(agg.items()):
        print(f"{t:15} {e:14} calls={n:4} in={i:8} out={o:7} cost=${c:.4f}"); tin += i; tout += o
    print(f"offloaded: {tin} input + {tout} output tokens that Claude did not have to read/write as bulk (its own cost: only the verification of the result)")
    return 0

def cmd_init(a):
    src = HERE.parent / "templates" / "config.example.toml"
    if USER_CFG.exists() and not a.force: print(f"{USER_CFG} exists (use --force)"); return 1
    USER_CFG.parent.mkdir(parents=True, exist_ok=True); USER_CFG.write_text(src.read_text()); print(f"written {USER_CFG}: edit endpoints, then `llmx status`"); return 0

def cmd_up(a):
    ep = load_cfg()["endpoints"].get(a.name)
    if not ep: print("unknown endpoint"); return 2
    if ep_up(ep): print("up"); return 0
    if not loading(a.name, ep) and not start(a.name, ep) and not loading(a.name, ep):
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
    r.add_argument("--local-only", action="store_true", help="never use a cloud endpoint, whatever the policy"); r.set_defaults(fn=cmd_run)
    w = s.add_parser("_watch"); w.add_argument("name"); w.set_defaults(fn=cmd_watch)
    s.add_parser("status").set_defaults(fn=cmd_status); s.add_parser("stats").set_defaults(fn=cmd_stats); s.add_parser("tasks").set_defaults(fn=cmd_tasks)
    i = s.add_parser("init"); i.add_argument("--force", action="store_true"); i.set_defaults(fn=cmd_init)
    for n, f in (("up", cmd_up), ("down", cmd_down)): x = s.add_parser(n); x.add_argument("name"); x.set_defaults(fn=f)
    a = p.parse_args()
    try: sys.exit(a.fn(a))
    except KeyboardInterrupt: sys.exit(130)

if __name__ == "__main__": main()
