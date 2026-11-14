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
5. **Send less, not more**: pass the diff hunks / error lines / one section (`--file` numbers lines), not the repo. Input too large for the endpoint context → exit 3; do not split to force it.
6. Quality check of a model on this user's material: run the same small task on llmx and yourself a few times (`llmx run TASK ... ` vs your answer); if the model misses or invents often, remove it from that task in `[tasks]`.

## Commands
```bash
llmx status                       # endpoints up/down, resolved endpoint per task
llmx run TASK -f FILE [-f ...] [--in FILE|-] [-p "extra instruction"] [--local-only] [--endpoint NAME]
llmx up|down NAME                 # start/stop a local server (needs `start` in config)
llmx stats                        # tokens and cost offloaded;  llmx init: write the example config
```
Setup and model choice: `references/setup.md`.
