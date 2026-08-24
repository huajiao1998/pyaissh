# -*- coding: utf-8 -*-
"""SIGTERM/SIGINT harness: import pyaissh.py as module, call main() in-process as
the real main thread, raise the signal after DELAY ms via signal.raise_signal.
Usage: PYAISSH_PY=/path/pyaissh.py SIG=TERM python sigterm_harness.py <delay_ms> -- <pyaissh args...>
Prints HARNESS_RC=<rc> HARNESS_SECS=<wall> HARNESS_SIG=<which> to stderr."""
import sys, os, time, signal, threading, importlib.util

def load_pssh(path):
    spec = importlib.util.spec_from_file_location("pssh_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def main():
    delay_ms = int(sys.argv[1])
    idx = sys.argv.index("--")
    sys.argv = [sys.argv[0]] + sys.argv[idx+1:]
    which = os.environ.get("SIG", "TERM")
    signum = signal.SIGINT if which == "INT" else signal.SIGTERM
    mod = load_pssh(os.environ["PYAISSH_PY"])
    def fire():
        time.sleep(delay_ms/1000.0)
        try:
            signal.raise_signal(signum)
        except Exception as e:
            sys.stderr.write("HARNESS_RAISE_ERR %s %s\n" % (type(e).__name__, e))
    threading.Thread(target=fire, daemon=True).start()
    t0 = time.time()
    try:
        rc = mod.main()
    except BaseException as e:
        sys.stderr.write("HARNESS_UNCAUGHT %s %s\n" % (type(e).__name__, e))
        rc = -1
    secs = time.time() - t0
    sys.stderr.write("HARNESS_RC=%d HARNESS_SECS=%.3f HARNESS_SIG=%s\n" % (rc, secs, which))
    return 0 if rc == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
