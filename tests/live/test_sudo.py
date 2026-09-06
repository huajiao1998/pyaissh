# -*- coding: utf-8 -*-
"""--sudo 提权真机测试（脱敏，凭据走 env）。

需配置（tests/README.md / env.example）：
  PYAISSH_TEST_HOST（root@ip）、PYAISSH_TEST_SUDO_USER[_PASSWORD]（sudoers 需密码 ALL）
  PYAISSH_TEST_SUDO_NP_USER[_PASSWORD]（NOPASSWD: id/whoami 的测试用户）
覆盖：提权/复合命令整链/stderr 干净/密码错/NOPASSWD 免密/失败提示/空密码/互斥/
      无泄漏/cmd-file 整链/无误报凭据/命令失败不误报。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

C.require("PYAISSH_TEST_HOST", "PYAISSH_TEST_SUDO_USER", "PYAISSH_TEST_SUDO_PASSWORD",
          "PYAISSH_TEST_SUDO_NP_USER", "PYAISSH_TEST_SUDO_NP_PASSWORD")
SUDO_PW = os.environ.get("PYAISSH_TEST_SUDO_PASSWORD")
NP_PW = os.environ.get("PYAISSH_TEST_SUDO_NP_PASSWORD")
TGT = C.target(os.environ.get("PYAISSH_TEST_SUDO_USER"))
NP_TGT = C.target(os.environ.get("PYAISSH_TEST_SUDO_NP_USER"))
PW = SUDO_PW


def main():
    # T1 有密码提权
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW, "--cmd", "id"], PW)
    C.check("T1 sudo 提权 uid=0", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    # T2 复合命令整链提权
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW,
                      "--cmd", "id && whoami && echo SEG3_$UID"], PW)
    out = (j or {}).get("stdout", "")
    C.check("T2 && 第二段 uid=0", "uid=0(root)" in out and "root\n" in out and "SEG3_0" in out)
    # T3 成功 stderr 无 password for
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW, "--cmd", "id"], PW)
    C.check("T3 stderr 无 password for", "password for" not in (j or {}).get("stderr", "").lower())
    # T4 密码错误
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", "WrongPass!", "--cmd", "id"], PW)
    stderr = (j or {}).get("stderr", "")
    C.check("T4 密码错失败", (j or {}).get("exit_success") is False
            and "incorrect" in stderr.lower())
    # T5 NOPASSWD 用户简单命令免密
    rc, j, _ = C.run(["exec", NP_TGT, "--sudo", "--cmd", "id"], NP_PW)
    C.check("T5 NOPASSWD 免密成功", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    # T6 NP 用户复合命令需密码失败 + 提示
    rc, j, _ = C.run(["exec", NP_TGT, "--sudo", "--cmd", "id && whoami"], NP_PW)
    w = " ".join((j or {}).get("warnings") or [])
    C.check("T6 sudo -n 失败提示密码配置", (j or {}).get("exit_success") is False
            and "--sudo-password" in w)
    # T7 空密码视为未设置
    rc, j, _ = C.run(["exec", NP_TGT, "--sudo", "--sudo-password", "", "--cmd", "id && whoami"],
                     NP_PW)
    w = " ".join((j or {}).get("warnings") or [])
    C.check("T7 空密码视为未设置", (j or {}).get("exit_success") is False
            and "--sudo-password" in w)
    # T8 --sudo --pty 互斥
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--pty", "--sudo-password", PW, "--cmd", "id"], PW)
    C.check("T8 --pty 互斥 bad_args", (j or {}).get("error") == "bad_args")
    # T9 cmd 字段无密码
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW, "--cmd", "id"], PW)
    C.check("T9 cmd 字段无密码", PW not in (j or {}).get("cmd", ""))
    # T10 --cmd-file + sudo 整链提权
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_sudo.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write("id\nwhoami\necho DONE_$UID\n")
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW, "--cmd-file", script], PW)
    out = (j or {}).get("stdout", "")
    C.check("T10 cmd-file 整链提权", "uid=0(root)" in out and "DONE_0" in out)
    os.remove(script)
    # T11 无误报凭据 WARN
    rc, j, _ = C.run(["exec", TGT, "--sudo", "--sudo-password", PW, "--cmd", "id"], PW)
    C.check("T11 无误报凭据 WARN", "疑似凭据" not in " ".join((j or {}).get("warnings") or []))
    # T12 命令失败不误报需密码（stderr 无 sudo 报错特征）
    rc, j, _ = C.run(["exec", NP_TGT, "--sudo", "--cmd", "id nonexistent_user_xyz"], NP_PW)
    w = " ".join((j or {}).get("warnings") or [])
    C.check("T12 命令失败不误报需密码",
            (j or {}).get("exit_success") is False and "免密探测失败" not in w)

    sys.exit(C.finish("test_sudo"))


if __name__ == "__main__":
    main()
