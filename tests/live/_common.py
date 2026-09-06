# -*- coding: utf-8 -*-
"""live 测试公共层（脱敏：凭据一律 env 读取，禁止硬编码）。

env 键（见 env.example）：
  PYAISSH_TEST_HOST（root@ip）/ PYAISSH_TEST_PASSWORD
被测二进制：PYAISSH_BIN 覆盖；缺省仓库根 pyaissh.py。
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.environ.get("PYAISSH_BIN", os.path.join(_REPO, "pyaissh.py"))
HOST = os.environ.get("PYAISSH_TEST_HOST", "")          # root@ip 或 ip
_PW = os.environ.get("PYAISSH_TEST_PASSWORD", "")

PASS = FAIL = 0
SKIPPED = False


def require(*keys):
    """缺任一 env 键则打印 SKIP 并退出 0（live 测试统一入口）。"""
    global SKIPPED
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        SKIPPED = True
        print("SKIP: 需配置 %s（见 tests/README.md，凭据不入库）" % " / ".join(missing))
        sys.exit(0)


def target(user=None):
    """目标串：缺省返回 HOST 原样（root@ip）；给 user 时替换 user 部分。"""
    if user is None:
        return HOST if "@" in HOST else "root@%s" % HOST
    host = HOST.split("@")[-1] if "@" in HOST else HOST
    return "%s@%s" % (user, host)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name, flush=True)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail), flush=True)


def run(args, login_pw=None, timeout=180):
    """跑被测 BIN（login_pw 缺省 root 密码），返回最后一行 JSON（无则 None）。"""
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = login_pw if login_pw is not None else _PW
    p = subprocess.run([sys.executable, BIN] + args, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    j = None
    for line in reversed(p.stdout.splitlines()):
        try:
            j = json.loads(line)
            break
        except Exception:
            pass
    return p.returncode, j, p.stderr


def finish(name):
    """统一收尾输出（PASS/FAIL 计数），返回退出码。"""
    print("%s: %d PASS / %d FAIL" % (name, PASS, FAIL), flush=True)
    return 0 if FAIL == 0 else 1
