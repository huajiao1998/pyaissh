#!/usr/bin/env bash
# Real kill -TERM loopback tests using deployment copy on Linux.
# $1 = scenario: exec | up | dl_serial | dl_par2 | dl_par8 | int
SC="$1"
PY=/root/pssh_lt/pyaissh.py
PW=WKkO0147369
export PYAISSH_PASSWORD=$PW
export PYAISSH_USER=root
case "$SC" in
  exec)
    timeout 30 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' exec root@127.0.0.1:22 --cmd "sleep 25" & PID=$!; sleep 6; kill -TERM $PID; wait $PID; echo REAL_RC=$?' 2>&1 | tail -2
    ;;
  up)
    timeout 90 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' upload root@127.0.0.1:22 --local /tmp/pssh_lt_lp.bin --remote /tmp/pssh_lt_lp_up.bin & PID=$!; sleep 7; kill -TERM $PID; wait $PID; echo REAL_RC=$?; ls /tmp/pssh_lt_lp_up.bin.part.* 2>/dev/null | wc -l' 2>&1 | tail -3
    ;;
  dl_serial)
    timeout 90 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' download root@127.0.0.1:22 --remote /tmp/pssh_lt_lp.bin --local /tmp/pssh_lt_dl_s.bin & PID=$!; sleep 7; kill -TERM $PID; wait $PID; echo REAL_RC=$?; ls /tmp/pssh_lt_dl_s.bin.part.* 2>/dev/null | wc -l' 2>&1 | tail -3
    ;;
  dl_par2|dl_par8)
    K=${SC#dl_par}
    timeout 90 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' download root@127.0.0.1:22 --remote /tmp/pssh_lt_lp.bin --local /tmp/pssh_lt_dl_p'"$K"'.bin --parallel '"$K"' & PID=$!; sleep 7; kill -TERM $PID; wait $PID; echo REAL_RC=$?; ls /tmp/pssh_lt_dl_p'"$K"'.bin.part.* 2>/dev/null | wc -l' 2>&1 | tail -3
    ;;
  int)
    timeout 30 bash -c 'PYAISSH_PASSWORD='"$PW"' PYAISSH_USER=root python3 '"$PY"' exec root@127.0.0.1:22 --cmd "sleep 25" & PID=$!; sleep 6; kill -INT $PID; wait $PID; echo REAL_RC=$?' 2>&1 | tail -2
    ;;
esac
