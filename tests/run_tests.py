# -*- coding: utf-8 -*-
"""pyaissh 统一测试入口（单入口 + 运行时选集）。

用法：
  python tests/run_tests.py               交互菜单（数字选集）
  python tests/run_tests.py --all         全量：unit 先、live 后
  python tests/run_tests.py --unit        仅单元（无网络）
  python tests/run_tests.py --sudo --exec --transfer  指定 live 集
  python tests/run_tests.py --list        列出测试集

测试集（逻辑在各自集文件，本脚本统一调度/汇总）：
  1 unit_regression   回归 54 例        tests/unit/test_regression.py
  2 unit_credential   凭据 41 例        tests/unit/test_credential.py
  3 live_sudo         --sudo 12 例      tests/live/test_sudo.py
  4 live_exec_field   exec+field 19 例  tests/live/test_exec.py
  5 live_transfer     传输 3 例         tests/live/test_transfer.py

脱敏：live 凭据 env（PYAISSH_TEST_*）读取；缺凭据集文件自行 SKIP(exit 0)。
被测目标：集文件支持 PYAISSH_PY / PYAISSH_BIN 覆盖。
"""
import argparse
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))

# (name, 描述, 集文件路径[正斜杠], 需 live 凭据?)
SUITES = [
    ("unit_regression", "回归 54 例（凭据矩阵/parse_target/编码/stdin）",
     "unit/test_regression.py", False),
    ("unit_credential", "凭据启发式 41 例",
     "unit/test_credential.py", False),
    ("live_sudo", "--sudo 提权 12 例（真机）",
     "live/test_sudo.py", True),
    ("live_exec_field", "exec 12 + --field 7 例（真机）",
     "live/test_exec.py", True),
    ("live_transfer", "传输往返 3 例（真机）",
     "live/test_transfer.py", True),
]

LIVE_ENV_KEYS = {
    "live/test_sudo.py": ["PYAISSH_TEST_HOST", "PYAISSH_TEST_SUDO_USER",
                          "PYAISSH_TEST_SUDO_PASSWORD", "PYAISSH_TEST_SUDO_NP_USER",
                          "PYAISSH_TEST_SUDO_NP_PASSWORD"],
    "live/test_exec.py": ["PYAISSH_TEST_HOST", "PYAISSH_TEST_PASSWORD"],
    "live/test_transfer.py": ["PYAISSH_TEST_HOST", "PYAISSH_TEST_PASSWORD"],
}


def _check_skip(name, rel, is_live):
    if not is_live:
        return False
    missing = [k for k in LIVE_ENV_KEYS.get(rel, []) if not os.environ.get(k)]
    if missing:
        print("%s SKIP: 缺凭据 env %s（见 tests/README.md，凭据不入库）"
              % (name, " / ".join(missing)))
        return True
    return False


def _run_suite(idx):
    name, desc, rel, is_live = SUITES[idx]
    if _check_skip(name, rel, is_live):
        return None  # 跳过
    path = os.path.join(_HERE, rel)
    print("\n>>> %s（%s）" % (name, desc))
    p = subprocess.run([sys.executable, "-B", path], env=dict(os.environ))
    return p.returncode == 0


def _interactive():
    print("\npyaissh 测试集选择：")
    for i, (name, desc, _, _) in enumerate(SUITES, 1):
        print("  %d) %s — %s" % (i, name, desc))
    print("  0) 全部")
    try:
        choice = input("选择 (0-%d): " % len(SUITES)).strip()
    except EOFError:
        return None
    if choice == "0":
        return list(range(len(SUITES)))
    if not choice:
        return None
    try:
        n = int(choice) - 1
        if 0 <= n < len(SUITES):
            return [n]
    except ValueError:
        pass
    print("无效选择")
    return None


def main():
    ap = argparse.ArgumentParser(description="pyaissh 统一测试入口")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--unit", action="store_true")
    ap.add_argument("--sudo", action="store_true")
    ap.add_argument("--exec", action="store_true")
    ap.add_argument("--transfer", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        for i, (name, desc, _, _) in enumerate(SUITES, 1):
            print("  %d) %s — %s" % (i, name, desc))
        return 0
    if a.all:
        order = list(range(len(SUITES)))
    elif a.unit:
        order = [0, 1]
    elif a.sudo or a.exec or a.transfer:
        order = []
        if a.sudo:
            order.append(2)
        if a.exec:
            order.append(3)
        if a.transfer:
            order.append(4)
    else:
        order = _interactive()
        if order is None:
            return 0

    results = []
    for idx in order:
        name = SUITES[idx][0]
        ok = _run_suite(idx)
        results.append((name, ok))

    ran = [(n, o) for n, o in results if o is not None]
    skipped = [n for n, o in results if o is None]
    failed = [n for n, o in ran if not o]
    print("\n=== 汇总: %s ==="
          % ("ALL PASS" if ran and not failed else "FAIL: " + ", ".join(failed)
             if failed else "（全部跳过）"))
    if skipped:
        print("跳过: %s" % ", ".join(skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
