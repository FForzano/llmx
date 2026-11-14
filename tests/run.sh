#!/usr/bin/env bash
# Smoke tests with a mock OpenAI-compatible server. Run: tests/run.sh
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; L="python3 $D/../skills/llmx/scripts/llmx.py"
T=$(mktemp -d); export LLMX_CONFIG="$T/c.toml" LLMX_STATE="$T/state"; fail=0
ok() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got $2, want $3)"; fail=1; fi; }
python3 "$D/mock_server.py" 18999 & P=$!; trap 'kill $P 2>/dev/null; rm -rf "$T"' EXIT; sleep 1
cat > "$T/c.toml" <<C
[policy]
allow_cloud = false
[endpoints.m]
base_url = "http://127.0.0.1:18999/v1"
[endpoints.dead]
base_url = "http://127.0.0.1:18998/v1"
[endpoints.cl]
kind = "cloud"
base_url = "http://127.0.0.1:18999/v1"
[tasks]
default = ["dead", "cl", "m"]
C
printf 'import os\nx = 1 / y\n' > "$T/a.py"
out=$($L run review-scan -f "$T/a.py" 2>/dev/null); rc=$?
ok "exit 0 with fallback past dead and cloud-not-allowed" $rc 0
ok "hallucinated finding dropped, real kept" "$(echo "$out" | wc -l | tr -d ' ')" 1
ok "finding text" "$(echo "$out" | grep -c 'division by zero')" 1
$L run summarize --local-only --endpoint cl --in "$T/a.py" >/dev/null 2>&1; ok "--local-only refuses cloud (exit 3)" $? 3
echo "[policy]
enabled=false" >> "$T/c.toml"; $L run summarize --in "$T/a.py" >/dev/null 2>&1; ok "disabled -> exit 3" $? 3
LLMX_DISABLE=0 $L stats | grep -q "review-scan"; ok "stats logged" $? 0
# idle shutdown: a server started by llmx stops after idle_minutes
cat > "$T/c2.toml" <<C
[endpoints.i]
base_url = "http://127.0.0.1:18997/v1"
autostart = true
idle_minutes = 0.02
start_timeout = 10
start = "python3 $D/mock_server.py 18997"
[tasks]
default = ["i"]
C
LLMX_CONFIG="$T/c2.toml" $L run summarize --in "$T/a.py" >/dev/null 2>&1; ok "autostart works" $? 0
sleep 1; LLMX_CONFIG="$T/c2.toml" $L status | grep -q "  i .*UP"; ok "server up after use" $? 0
sleep 75; LLMX_CONFIG="$T/c2.toml" $L status | grep -q "  i .*UP"; ok "server stopped when idle" $? 1
exit $fail
