# -*- coding: utf-8 -*-
"""--sudo 提权真机测试（脱敏版，凭据从 env 读）。

需配置（tests/live/env.example 见 README）：
  PYAISSH_TEST_SUDO_USER / PYAISSH_TEST_SUDO_PASSWORD   （需 sudoers ALL 需密码）
  PYAISSH_TEST_SUDO_NP_USER / PYAISSH_TEST_SUDO_NP_PASSWORD （NOPASSWD: id/whoami）
  PYAISSH_TEST_HOST 主机目标（root@ip，仅作可达性对照；用例连 SUDO_USER@host）
被测二进制：env PYAISSH_BIN 覆盖；缺省根 pyaissh.py。
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.environ.get("PYAISSH_TEST_HOST", "")
SUDO_USER = os.environ.get("PYAISSH_TEST_SUDO_USER", "")
SUDO_PW = os.environ.get("PYAISSH_TEST_SUDO_PASSWORD", "")
NP_USER = os.environ.get("PYAISSH_TEST_SUDO_NP_USER", "")
NP_PW = os.environ.get("PYAISSH_TEST_SUDO_NP_PASSWORD", "")

if not (HOST and SUDO_USER and SUDO_PW and NP_USER and NP_PW):
    print("SKIP: 需配置 PYAISSH_TEST_HOST / PYAISSH_TEST_SUDO_USER[_PASSWORD] / "
          "PYAISSH_TEST_SUDO_NP_USER[_PASSWORD]（见 tests/README.md）")
    sys.exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.environ.get("PYAISSH_BIN", os.path.join(ROOT, "pyaissh.py"))
PY = [sys.executable, BIN]
TGT = "%s@%s" % (SUDO_USER, HOST.split("@")[-1])
NP_TGT = "%s@%s" % (NP_USER, HOST.split("@")[-1])
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name, flush=True)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail), flush=True)


def run(args, env_extra=None, timeout=180):
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = env_extra.pop("_login_pw") if env_extra and "_login_pw" in env_extra else SUDO_PW
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(PY + args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", env=env)
    j = None
    for line in reversed(p.stdout.splitlines()):
        try:
            j = json.loads(line)
            break
        except Exception:
            pass
    return p.returncode, j, p.stderr


def main():
    # T1: 有密码提权
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW, "--cmd", "id"])
    check("T1 sudo 提权 uid=0", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    # T2: 复合命令整链提权
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW,
                    "--cmd", "id && whoami && echo SEG3_$UID"])
    out = (j or {}).get("stdout", "")
    check("T2 && 第二段 uid=0", "uid=0(root)" in out and "root\n" in out and "SEG3_0" in out)
    # T3: stderr 无 password for
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW, "--cmd", "id"])
    check("T3 stderr 无 password for", "password for" not in (j or {}).get("stderr", "").lower())
    # T4: 密码错误
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", "WrongPass!", "--cmd", "id"])
    stderr = (j or {}).get("stderr", "")
    check("T4 密码错失败", (j or {}).get("exit_success") is False and "incorrect" in stderr.lower())
    # T5: NOPASSWD 用户简单命令免密
    rc, j, _ = run(["exec", NP_TGT, "--sudo", "--cmd", "id"], {"_login_pw": NP_PW})
    check("T5 NOPASSWD 免密成功", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    # T6: NP 用户复合命令需密码失败 + 提示
    rc, j, _ = run(["exec", NP_TGT, "--sudo", "--cmd", "id && whoami"], {"_login_pw": NP_PW})
    w = " ".join((j or {}).get("warnings") or [])
    check("T6 sudo -n 失败提示密码配置", (j or {}).get("exit_success") is False and "--sudo-password" in w)
    # T7: 空密码视为未设置
    rc, j, _ = run(["exec", NP_TGT, "--sudo", "--sudo-password", "", "--cmd", "id && whoami"],
                   {"_login_pw": NP_PW})
    w = " ".join((j or {}).get("warnings") or [])
    check("T7 空密码视为未设置", (j or {}).get("exit_success") is False and "--sudo-password" in w)
    # T8: --sudo --pty 互斥
    rc, j, _ = run(["exec", TGT, "--sudo", "--pty", "--sudo-password", SUDO_PW, "--cmd", "id"])
    check("T8 --pty 互斥 bad_args", (j or {}).get("error") == "bad_args")
    # T9: cmd 字段无密码
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW, "--cmd", "id"])
    check("T9 cmd 字段无密码", SUDO_PW not in (j or {}).get("cmd", ""))
    # T10: --cmd-file + sudo
    script = os.path.join(ROOT, "tests", "live", "_tmp_sudo.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write("id\nwhoami\necho DONE_$UID\n")
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW, "--cmd-file", script])
    out = (j or {}).get("stdout", "")
    check("T10 cmd-file 整链提权", "uid=0(root)" in out and "DONE_0" in out)
    os.remove(script)
    # T11: 无误报凭据 WARN
    rc, j, _ = run(["exec", TGT, "--sudo", "--sudo-password", SUDO_PW, "--cmd", "id"])
    check("T11 无误报凭据 WARN", "疑似凭据" not in " ".join((j or {}).get("warnings") or []))
    # T12: 命令失败不误报需密码
    rc, j, _ = run(["exec", NP_TGT, "--sudo", "--cmd", "id nonexistent_user_xyz"], {"_login_pw": NP_PW})
    w = " ".join((j or {}).get("warnings") or [])
    check("T12 命令失败不误报需密码",
          (j or {}).get("exit_success") is False and "免密探测失败" not in w)

    print("test_sudo: %d PASS / %d FAIL" % (PASS, FAIL), flush=True)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
