# -*- coding: utf-8 -*-
"""exec 行为矩阵 + --field 消费端 合并真机测试（脱敏，凭据走 env）。

覆盖（合并自 v516_field_verify + v2_compare exec 用例提炼）：
  exec：echo/中文/exit码/复合/stderr/pty/idle超时/截断/cmd-file stdin/ls/test
  field：stdout 裸值 / stdout,-stderr 分流 / stderr 非空自动提示 / 工具错误完整 JSON /
         多字段 / --text 互斥 / bad_args
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

C.require("PYAISSH_TEST_HOST", "PYAISSH_TEST_PASSWORD")
TGT = C.target()
ENV_PW = os.environ.get("PYAISSH_TEST_PASSWORD")


def _sub(args, input_text=None, timeout=120):
    """手动子进程跑被测（field 模式输出非 JSON，需 raw stdout/stderr）。"""
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = ENV_PW
    return subprocess.run([sys.executable, C.BIN] + args, input=input_text,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)


def _last_json(p):
    for line in reversed(p.stdout.splitlines()):
        try:
            return json.loads(line)
        except Exception:
            pass
    return None


def main():
    # ===== exec 行为矩阵 =====
    rc, j, _ = C.run(["exec", TGT, "--cmd", "echo hello_exec"])
    C.check("exec echo", j and j.get("exit_success") and "hello_exec" in j.get("stdout", ""))
    rc, j, _ = C.run(["exec", TGT, "--cmd", "echo 你好世界"])
    C.check("exec 中文", j and "你好世界" in j.get("stdout", ""))
    rc, j, _ = C.run(["exec", TGT, "--cmd", "exit 3"])
    C.check("exec exit 3", j and j.get("exit_code") == 3 and j.get("exit_success") is False)
    rc, j, _ = C.run(["exec", TGT, "--cmd", "uname -s && echo SEG2"])
    C.check("exec 复合", j and "Linux" in j.get("stdout", "") and "SEG2" in j.get("stdout", ""))
    rc, j, _ = C.run(["exec", TGT, "--cmd", "ls /nonexistent_exec_xyz"])
    C.check("exec stderr 带回", j and j.get("exit_success") is False
            and "no such file" in j.get("stderr", "").lower())
    rc, j, _ = C.run(["exec", TGT, "--pty", "--cmd", "tty"])
    C.check("exec pty", j and "/dev/" in j.get("stdout", ""))
    rc, j, _ = C.run(["exec", TGT, "--cmd", "sleep 4", "--idle-timeout", "1"], timeout=60)
    C.check("exec idle 超时", j and j.get("error") == "exec_idle_timeout"
            and j.get("remote_may_be_running") is True)
    rc, j, _ = C.run(["exec", TGT, "--cmd", "seq 1 2000", "--max-output", "4096"])
    C.check("exec 截断标记", j and j.get("output_truncated") is True
            and "pyaissh" in j.get("stdout", ""))
    p = _sub(["exec", TGT, "--cmd-file", "-"], input_text="echo from_stdin && hostname\n")
    j = _last_json(p)
    C.check("exec cmd-file stdin", j and "from_stdin" in j.get("stdout", ""))
    p = _sub(["exec", TGT])
    j = _last_json(p)
    C.check("bad_args 无 cmd", j and j.get("error") == "bad_args")
    rc, j, _ = C.run(["test", TGT])
    C.check("test 连接", j and j.get("ok") is True and "os" in j)
    rc, j, _ = C.run(["ls", TGT, "--path", "/etc", "--limit", "3"])
    C.check("ls /etc", j and isinstance(j.get("entries"), list) and j["entries"])

    # ===== --field 消费端 =====
    p = _sub(["exec", TGT, "--cmd", "echo field_v", "--field", "stdout"])
    C.check("field stdout 裸值", p.stdout.strip() == "field_v" and "[SSH]" not in p.stderr)
    p = _sub(["exec", TGT, "--cmd", "sh -c 'echo v; echo e >&2'", "--field", "stdout"])
    C.check("field stderr 盲区提示", "结果含非空 stderr" in p.stderr
            and "[SSH]" not in p.stderr and p.stdout.strip() == "v")
    p = _sub(["exec", TGT, "--cmd", "sh -c 'echo o; echo e >&2'", "--field", "stdout,-stderr"])
    C.check("field stdout,-stderr 分流", p.stdout.strip() == "o" and "e" in p.stderr)
    p = _sub(["exec", "root@203.0.113.250", "--cmd", "x", "--timeout", "3", "--field", "stdout"],
             timeout=60)
    j = _last_json(p)
    C.check("field 工具错误完整 JSON", j and j.get("ok") is False and j.get("error"))
    p = _sub(["exec", TGT, "--cmd", "echo x", "--field", "exit_code,exit_success"])
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    C.check("field 多字段", len(lines) == 2 and lines[0] == "0" and lines[1] == "True")
    p = _sub(["exec", TGT, "--cmd", "echo x", "--field", "stdout", "--text"])
    j = _last_json(p)
    C.check("field --text 互斥", j and j.get("error") == "bad_args")
    p = _sub(["exec", TGT, "--cmd", "echo x", "--field", "no_such_field"])
    C.check("field 字段不存在提示", "字段不存在" in p.stderr)

    sys.exit(C.finish("test_exec"))


if __name__ == "__main__":
    main()
