#!/usr/bin/env bash
PY=/root/pssh_lt/pssh.py
PW=WKkO0147369
export PSSH_PASSWORD=$PW PSSH_USER=root
timeout 120 bash -c 'PSSH_PASSWORD='"$PW"' PSSH_USER=root python3 '"$PY"' upload root@127.0.0.1:22 --local /tmp/pssh_lt_lp2.bin --remote /tmp/pssh_lt_lp2_up.bin 2>&1 & PID=$!; sleep 8; kill -TERM $PID; wait $PID; echo REAL_RC=$?' 2>&1 | grep -oE '远端临时文件可能残留[^"]*|REAL_RC=[0-9]+'
