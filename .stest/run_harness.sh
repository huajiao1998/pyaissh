#!/usr/bin/env bash
# Usage: run_harness.sh <delay_ms> -- <args...>   ; env SIG=INT for SIGINT
export PYAISSH_PY="D:\工作目录\Leopold\pyssh\pyaissh.py"
cd "$(dirname "$0")/.."
out=$(PYAISSH_PASSWORD='WKkO0147369' python .stest/sigterm_harness.py "$@" 2>&1)
rc=$(echo "$out" | grep -o 'HARNESS_RC=[0-9-]*' | cut -d= -f2)
secs=$(echo "$out" | grep -o 'HARNESS_SECS=[0-9.]*' | cut -d= -f2)
sig=$(echo "$out" | grep -o 'HARNESS_SIG=[A-Z]*' | cut -d= -f2)
echo "RC=$rc T=$secs SIG=$sig"
echo "$out" | grep -o '"error": "[^"]*"' | head -1
echo "$out" | grep -o '"message": "[^"]*"' | head -1
echo "$out" | grep -o '\.part[^"]*' | head -3
echo "$out" | grep -o 'WARN.*' | head -3
# dump full last JSON line
echo "$out" | grep '^{' | tail -1
