---
name: llmx
description: Optional offload of bounded sub-tasks (log triage, summaries, commit drafts, first-pass review/language scans, second-opinion referee) to local or cheap open models, with Claude verifying. Used by other skills that opt in; also "usa il modello locale", "llmx".
allowed-tools: Bash(llmx:*)
---

# llmx: cheap candidates, Claude decides

`llmx` (`~/.local/bin/llmx`, stdlib Python) sends a task to the first usable endpoint of `~/.config/llmx/config.toml` (local servers, cheap open cloud APIs; the user may have none). **It is always optional**: exit 3 = no endpoint, exit 4 = unusable output → do the task yourself exactly as the skill says without llmx. Never mention llmx errors beyond one line.

## When
Only tasks that are bulk-in, small-out, or that benefit from a second independent opinion: `ask summarize log-triage module-summary commit-draft test-draft review-scan lang-scan claims-scan referee split-comments` (`llmx tasks`). Check once per session with `llmx status`; if no config or nothing is up, skip llmx for the whole session (do not retry per step).

## Rules that keep quality
1. **Output is a candidate, never a result.** Findings come as `file:line [sev] issue — "quote"`; `llmx` already drops those whose quote is not in the input. You still open each cited line before reporting it; report only what you confirmed. Drafts (commit, tests) are read and corrected like a colleague's.
2. **Second opinion, not replacement.** For judgment tasks (review, referee, claims) do your own pass too, then merge: findings found by both are the strongest; found only by the model → verify; found only by you → keep. Never lower your own bar because the model found nothing.
3. **Never offload**: edits, security decisions, final reports, anything needing the whole repo or paper in context, proofs/derivations.
4. **Privacy**: cloud endpoints run only if `policy.allow_cloud = true`. For unpublished manuscripts, blind reviews, secrets, `.env`: add `--local-only`.
5. **Small inputs are not offloaded**: below `policy.min_chars` (default 2500 chars, ~800 tokens) `log-triage summarize module-summary commit-draft review-scan lang-scan` exit 3 with "read it yourself": the offload plus your verification costs more than it saves (`--force` overrides). `log-triage` reduces the log mechanically first (collapses repeated lines, keeps errors ±2 lines, head and tail; `--raw` disables): seconds instead of minutes, one coherent chunk, no model tokens.
6. **Send less, not more**: pass the diff hunks / error lines / one section (`--file` numbers lines), not the repo. `log-triage`, `summarize`, `review-scan`, `lang-scan` split an input larger than the context into chunks by themselves (slow on very long inputs: run `ff debug log` first); the other tasks need the whole input and exit 3 if it does not fit.
7. **What llmx already checks for you** (mechanical, so you do not spend tokens on it): findings with a quote not in the input are dropped; text tasks (`log-triage`, `summarize`, `module-summary`) drop lines whose `backticked` name/line is not in the input; `commit-draft` is validated (subject ≤ 72, no period, blank line) with one repair round; `test-draft --out F --check CMD` runs the draft and repairs once with the real error (exit 4 = still failing, draft left in F). Findings come from 2 passes on local models: `[2/2]` = both passes agree (strongest), `[1/2]` = one pass only (verify with extra care). This lowers noise; it does not make a candidate true.
8. **Measure, don't guess**: `llmx eval [TASK]` runs known cases (shipped in `evals/cases.json`, plus yours in `~/.config/llmx/evals/cases.json`) and prints recall, false positives on clean inputs and invented findings caught. Run it after changing a model or a prompt; remove a task from a model in `[tasks]` when it misses real defects or floods clean code. `llmx stats` shows the gate-dropped share per task: a high one means that model invents; its savings are an estimate that counts gate-emptied runs, so judge usefulness by what you actually reused. Dropped spans are sampled in `usage.jsonl` (`samples`).

## Commands
```bash
llmx status                       # endpoints up/down, resolved endpoint per task
llmx run TASK -f FILE [-f ...] [--in FILE|-] [-p "extra instruction"] [--local-only] [--endpoint NAME] [--passes N]
llmx run test-draft -f SRC -p "..." --out tests/test_x.py --check "<narrow test cmd on {out}>"
llmx eval [TASK] [--endpoint NAME]   # recall / false positives / invented findings on known cases
llmx up|down NAME                 # start/stop a local server (needs `start` in config)
llmx stats                        # tokens and cost offloaded;  llmx init: write the example config
```
Setup and model choice: `references/setup.md`.
