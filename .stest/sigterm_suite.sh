#!/usr/bin/env bash
# SIGTERM suite runner using the in-process harness
cd "$(dirname "$0")/.."
export PYAISSH_PY="D:\工作目录\Leopold\pyssh\pyaissh.py"
PASS=0; FAIL=0; declare -a FAILS
run() {
  local label="$1"; shift
  local want="$1"; shift
  local out
  out=$(PYAISSH_PASSWORD='WKkO0147369' python .stest/sigterm_harness.py "$@" 2>&1)
  local rc_harness=$(echo "$out" | grep -o 'HARNESS_RC=[0-9-]*' | cut -d= -f2)
  local secs=$(echo "$out" | grep -o 'HARNESS_SECS=[0-9.]*' | cut -d= -f2)
  if [ "$rc_harness" = "$want" ]; then
    PASS=$((PASS+1)); echo "PASS [$label] rc=$rc_harness t=${secs}s"
  else
    FAIL=$((FAIL+1)); FAILS+=("$label: want rc=$want got rc=$rc_harness")
    echo "FAIL [$label] want=$want got=$rc_harness t=${secs}s"; echo "$out" | tail -3
  fi
}
run "$@"
echo "---- PASS=$PASS FAIL=$FAIL"
for f in "${FAILS[@]}"; do echo "  FAILED: $f"; done
