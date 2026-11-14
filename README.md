# llmx

Optional offload for Claude Code skills (used by `ffcode-*`): bounded sub-tasks go to local or cheap open models, Claude verifies and decides.
Not installed / not configured / endpoint down → the skills work exactly as before.

- `skills/llmx/scripts/llmx.py`: stdlib CLI, any OpenAI-compatible endpoint (llama.cpp, Ollama, LM Studio, MLX, DeepSeek, OpenRouter...), per-task fallback order, autostart of local servers, `--local-only`, cloud off by default, usage stats.
- Findings tasks (`review-scan`, `lang-scan`, `claims-scan`) must quote the input: findings whose quote is not in the input are dropped mechanically before Claude sees them.
- Mechanical quality gates, no extra Claude tokens: text tasks drop lines whose `backticked` names are not in the input; `commit-draft` is validated and repaired once; `test-draft --check CMD` runs the draft and repairs once with the real error; findings come from 2 local passes tagged `[2/2]` / `[1/2]`; long logs/diffs are processed in chunks (the size is halved automatically if the server rejects the context).
- `llmx eval`: recall, false positives on clean code and invented findings for each endpoint on known cases (`skills/llmx/evals/cases.json` + `~/.config/llmx/evals/cases.json`): decide per task which model to trust with data.
- `skills/llmx/references/setup.md`: what to install and which models.

```bash
./install.sh && llmx init && llmx status      # then edit ~/.config/llmx/config.toml
tests/run.sh                                  # mock-server smoke tests
```
