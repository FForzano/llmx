#!/usr/bin/env bash
# Smoke tests with a mock OpenAI-compatible server. Run: tests/run.sh
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; L="python3 $D/../skills/llmx/scripts/llmx.py"
T=$(mktemp -d); export LLMX_CONFIG="$T/c.toml" LLMX_STATE="$T/state" LLMX_MIN_CHARS=0; fail=0
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
ok "invented finding dropped, 2 real kept" "$(echo "$out" | wc -l | tr -d ' ')" 2
ok "finding found by both passes is tagged [2/2]" "$(echo "$out" | grep -c '^\[2/2\].*division by zero')" 1
ok "finding found by one pass is tagged [1/2]" "$(echo "$out" | grep -c '^\[1/2\].*unused import')" 1
# commit-draft: subject > 72 chars triggers one repair round
out=$(printf 'diff' | $L run commit-draft 2>/dev/null); ok "commit-draft repaired to a valid subject" "$out" "Fix division in a.py"
# test-draft with --check: first draft fails the check, repair passes
out=$($L run test-draft -f "$T/a.py" --out "$T/t.py" --check "grep -q ok {out}" 2>/dev/null); rc=$?
ok "test-draft repaired after failing check" $rc 0
ok "test-draft returns fence-stripped code" "$out" "print('ok')"
# ground gate on text tasks: backticked span not in the input drops the line
printf 'xx ERROR real thing yy\n' > "$T/l.log"
out=$($L run log-triage --in "$T/l.log" 2>/dev/null)
ok "log-triage: invented backticked line dropped" "$(echo "$out" | grep -c 'not in the input')" 0
ok "log-triage: grounded line kept" "$(echo "$out" | grep -c 'ERROR real thing')" 1
# chunking: input larger than the context is split (context 400 tok -> chunks), irrelevant parts (NONE) are omitted
cat >> "$T/c.toml" <<C
[endpoints.small]
base_url = "http://127.0.0.1:18999/v1"
context = 700
max_tokens = 100
C
python3 -c "
for i in range(300): print('INFO step', i, 'ok')
print('needle line')" > "$T/big.log"
out=$($L run log-triage --raw --endpoint small --in "$T/big.log" 2>"$T/err"); ok "chunked: runs" $? 0
ok "chunked: reports several chunks" "$(grep -c 'chunks' "$T/err")" 1
ok "chunked: only the relevant part is returned" "$(echo "$out" | grep -c 'needle line')" 1
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
# one big model at a time: starting B stops A (started by llmx) when free memory is short, and refuses if it still does not fit
cat > "$T/c3.toml" <<C
[policy]
mem_wait_s = 2
[endpoints.a]
base_url = "http://127.0.0.1:18996/v1"
autostart = true
mem_gb = 20
idle_minutes = 0
start_timeout = 10
start = "python3 $D/mock_server.py 18996"
[endpoints.b]
base_url = "http://127.0.0.1:18995/v1"
autostart = true
mem_gb = 20
idle_minutes = 0
start_timeout = 10
start = "python3 $D/mock_server.py 18995"
[tasks]
default = ["a"]
x = ["b"]
C
export LLMX_CONFIG="$T/c3.toml"
LLMX_FREE_GB=100 $L up a >/dev/null 2>&1
LLMX_FREE_GB=100 $L up b >/dev/null 2>&1; $L status | grep -c " UP" | grep -q 2; ok "plenty of free memory: both may run" $? 0
$L down b >/dev/null
LLMX_FREE_GB=5 $L up b >/dev/null 2>&1; $L status | grep -E "^  a " | grep -q UP; ok "short on memory: a (started by llmx) is stopped before b" $? 1
$L status | grep -E "^  b " | grep -q UP; ok "b refuses to start if memory stays short" $? 1
$L down a >/dev/null; $L down b >/dev/null
# small input: refused unless forced; big log: reduced mechanically before the model
out=$(LLMX_MIN_CHARS=2500 $L run commit-draft --in "$T/a.py" 2>&1); ok "small input refused (exit 3 text)" "$(echo "$out" | grep -c 'reading it yourself')" 1
python3 -c "
for i in range(3000): print(f'2026-01-01 10:00:{i%60:02d} INFO worker {i} heartbeat ok')
print('ERROR real thing broke')
for i in range(500): print('tail line', i)" > "$T/h.log"
err=$(LLMX_MIN_CHARS=0 $L run log-triage --in "$T/h.log" 2>&1 >/dev/null); ok "heartbeat log reduced before the model" "$(echo "$err" | grep -c 'reduced mechanically')" 1
python3 - <<P
import sys; sys.path.insert(0, "$D/../skills/llmx/scripts"); import llmx
t = open("$T/h.log").read(); r = llmx.reduce_log(t)
assert "ERROR real thing broke" in r and len(r) < len(t) // 20, len(r)
assert llmx.span_ok("12| foo ... bar", llmx.norm("foo xx bar")) and not llmx.span_ok("foo qqqq", "foo bar")
print("ok   reduce_log keeps the error, span_ok tolerant")
P
exit $fail
