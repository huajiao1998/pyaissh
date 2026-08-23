#!/usr/bin/env bash
PY=/root/pssh_lt/pssh.py
PW=WKkO0147369
export PSSH_PASSWORD=$PW PSSH_USER=root
timeout 120 bash -c 'PSSH_PASSWORD='"$PW"' PSSH_USER=root python3 '"$PY"' upload root@127.0.0.1:22 --local /tmp/pssh_lt_lp2.bin --remote /tmp/pssh_lt_lp2_up.bin & PID=$!; sleep 8; kill -TERM $PID; wait $PID; echo REAL_RC=$?; ls -l /tmp/pssh_lt_lp2_up.bin.part.* 2>/dev/null | wc -l'
