# -*- coding: utf-8 -*-
"""v1.4.8 极早期信号单元：第一次 main() 极早期中断 + 进程内复用不误判。"""
import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("pssh_mod", os.environ["PSSH_PY"])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def call_main(argv):
    sys.argv = argv
    try:
        return m.main()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else -1


# 1) 模拟 import 阶段收到信号（顶层 handler 置标志）→ 第一次 main() 应 130 + interrupted
m._SIGTERM_RECEIVED = True
m._INTERRUPT_SOURCE = "SIGTERM"
rc1 = call_main(["pssh", "--version"])
print("CASE1 first-main early-signal rc=%d (want 130)" % rc1)
assert rc1 == 130, "极早期信号应 130"

# 2) 进程内复用：标志被上一次中断留下 → 本次应正常执行（不误判 130）
#    此时 _RESPONDER_STARTED 已 True（第一次 main() 启动过）
m._SIGTERM_RECEIVED = True  # 模拟上一次中断残留的标志
rc2 = call_main(["pssh", "--version"])
print("CASE2 second-main with stale flag rc=%d (want 0)" % rc2)
assert rc2 == 0, "复用场景不应误判极早期信号"

# 3) 第三次正常
rc3 = call_main(["pssh", "--version"])
print("CASE3 third-main rc=%d (want 0)" % rc3)
assert rc3 == 0

print("SIG_UNIT_ALL_PASS")
