#!/usr/bin/env bash
PY=/root/pssh_lt/pyaissh.py
PW=WKkO0147369
export PYAISSH_PASSWORD=$PW PYAISSH_USER=root
timeout 120 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' upload root@127.0.0.1:22 --local /tmp/pssh_lt_lp2.bin --remote /tmp/pssh_lt_lp2_up.bin & PID=$!; sleep 8; kill -TERM $PID; wait $PID; echo REAL_RC=$?; ls -l /tmp/pssh_lt_lp2_up.bin.part.* 2>/dev/null | wc -l'
