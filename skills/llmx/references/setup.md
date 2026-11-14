# Setup and model choice

Model names change every few months: treat the ones below as the current class, check the provider/HF page before downloading.

## What runs where (quality first)
| Tier | Task kinds | Why it is safe |
|---|---|---|
| local-fast (MoE, ~3B active) | log-triage, summarize, module-summary, commit-draft, split-comments | bulk in, short out; errors are visible when Claude reads the result |
| local-big or cloud-cheap (stronger) | review-scan, lang-scan, claims-scan, test-draft, referee | candidates with verbatim quotes; Claude verifies each; a second model family finds different defects |
| Claude | everything else: edits, final reports, judgment, proofs, security | never offloaded |

## Apple Silicon, 36 GB unified memory (M4 Max)
The GPU can use about 24-27 GB: **one** ~18-20 GB model resident at a time, not two.
1. `llama-server` (llama.cpp, `brew install llama.cpp`) is enough; Ollama or LM Studio work too (any OpenAI-compatible URL).
2. Primary local model: a ~30B **MoE with ~3B active** coder/instruct at Q4_K_M (e.g. Qwen3-Coder-30B-A3B, or the newest Qwen A3B ~30-35B): ~18-21 GB, fast (tens of tok/s) so latency is not a reason to avoid it.
3. Optional second local, stronger and slower: dense 27-32B Q4 (~19 GB). Do not run it together with the first: `llmx down local-fast; llmx up local-big`. 
4. `llmx init`, edit `~/.config/llmx/config.toml`, `llmx status`. The first call downloads the model (`-hf`, into `~/.cache/huggingface`) and starts the server (`autostart`).
5. Memory: a server started by llmx stops by itself after `idle_minutes` (default 10) without use and restarts on the next call (a few seconds to load from disk). `llmx down NAME` frees it at once. Servers you start yourself are never stopped.


## Cloud, cheap and open (optional)
Providers with OpenAI-compatible APIs (DeepSeek, Alibaba Qwen, DeepInfra, Together, Groq, OpenRouter, Z.ai): a few cents per million tokens for the flash/small tiers. Export the key in your shell profile, set `policy.allow_cloud = true`. Per project: `.llmx.toml` in the root with `[policy]\nallow_cloud = false`: **use this in every paper/thesis folder (unpublished work, blind review) and in client code.**

## Check that it helps
`llmx eval` first: it runs known cases on each endpoint (recall on seeded defects, false positives on clean code, invented findings caught by the gate). Add your own real cases (a log, a diff with a known bug, a clean file) to `~/.config/llmx/evals/cases.json` (`{"task","name","file","expect":[strings or [alternatives]]}`, `file` relative to that folder).
Then `llmx stats` shows tokens moved. Keep a task on a model only if its candidates mostly survive Claude's verification: if >~30% of review-scan findings are dropped or wrong, move that task to a stronger tier or remove it from `[tasks]` (the skills then do it in Claude, as before).
