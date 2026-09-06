# -*- coding: utf-8 -*-
"""pyaissh 统一测试（单文件：全部测试集内联于此，运行时选择要跑的集）。

用法：
  python tests/run_tests.py               交互菜单（数字选集）
  python tests/run_tests.py --all         全量：unit 先、live 后
  python tests/run_tests.py --unit        仅单元（无网络）
  python tests/run_tests.py --sudo --exec --transfer  指定 live 集
  python tests/run_tests.py --list        列出测试集

测试集：
  1) unit_regression   回归 54 例   （凭据矩阵/parse_target/编码/stdin）
  2) unit_credential   凭据启发式 41 例
  3) live_sudo         --sudo 提权 12 例（真机）
  4) live_exec_field   exec 12 + --field 7 例（真机）
  5) live_transfer     传输往返 3 例（真机）

脱敏：live 凭据一律 env（PYAISSH_TEST_*），本文件零硬编码。
被测目标：env PYAISSH_PY（unit importlib）/ PYAISSH_BIN（live 子进程）；缺省仓库根。
"""
import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
_py_module = None

# ============================================================
# 框架：被测加载 / 断言 / live 工具
# ============================================================

def _module():
    """被测模块（unit 用）：PYAISSH_PY 覆盖；缺省根 pyaissh.py。"""
    global _py_module
    if _py_module is not None:
        return _py_module
    p = os.environ.get("PYAISSH_PY")
    if p:
        spec = importlib.util.spec_from_file_location("pyaissh", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    else:
        sys.path.insert(0, _REPO)
        import pyaissh as m
    _py_module = m
    return m


def _bin():
    return os.environ.get("PYAISSH_BIN", os.path.join(_REPO, "pyaissh.py"))


class _Suite:
    """测试集运行器：PASS/FAIL 计数 + 输出。"""

    def __init__(self, name):
        self.name = name
        self.pass_n = 0
        self.fail_n = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.pass_n += 1
            print("PASS  %s" % name)
        else:
            self.fail_n += 1
            print("FAIL  %s  %s" % (name, detail))

    def result(self):
        print("%s: %d PASS / %d FAIL" % (self.name, self.pass_n, self.fail_n))
        return self.pass_n + self.fail_n, self.fail_n


def _missing_env(keys):
    return [k for k in keys if not os.environ.get(k)]


def _live_run(args, login_pw=None, timeout=180):
    """子进程跑被测（live），返回 (rc, 最后 JSON or None, stderr)。"""
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = login_pw if login_pw is not None \
        else os.environ.get("PYAISSH_TEST_PASSWORD", "")
    p = subprocess.run([sys.executable, "-B", _bin()] + args, capture_output=True,
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


def _live_sub(args, input_text=None, timeout=120):
    """手动子进程（field 输出非 JSON 需 raw）。"""
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = os.environ.get("PYAISSH_TEST_PASSWORD", "")
    return subprocess.run([sys.executable, "-B", _bin()] + args, input=input_text,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)


def _last_json(p):
    for line in reversed(p.stdout.splitlines()):
        try:
            return json.loads(line)
        except Exception:
            pass
    return None


def _live_host():
    h = os.environ.get("PYAISSH_TEST_HOST", "")
    return h if "@" in h else ("root@%s" % h) if h else ""


# ============================================================
# 测试集 1：unit 回归 54 例（原 verify_r3：L4 正则矩阵 / N10 parse_target /
#           M1/L6 reconfigure 编码机制 + stdin 契约 + 版本）
# ============================================================

def suite_unit_regression(s):
    m = _module()

    def hit(cmd):
        return bool(m._SENSITIVE_CMD_RE.search(cmd))

    # L4: 不应误报（工具 flag）
    for cmd in [
        "find / -print0 | xargs -0 rm",
        "find . -prune -o -print",
        "find /tmp -printf '%f\\n'",
        "perl -pe 's/a/b/g' file",
        "pytest -p no:cacheprovider",
        "awk -p '{print}' f",
        "unzip -p x.zip",
        "unzip -pfile.zip",
        "gcc -pthread -O2 main.c",
        "gcc -pthreads main.c",
        "xargs -p rm",
        "wget -p https://example.com/",
        "echo -pabc",
        "make -p x",
        "xmake -p x",
        "pip install -p x",
        "cp -p a b",
        "mkdir -p a/b",
        "tar -p x.tar",
        "ssh -p 22 root@host",
        "ssh -p'22' root@host",
        "ps -p 1234",
        "ps -p 1234,5678",
        "git -p status",
        "scp -p a b",
        "rsync -p /x y",
    ]:
        s.check("no-match: %r" % cmd, not hit(cmd), "但命中了: %r" % cmd)
    # L4: 应命中（疑似凭据）
    for cmd in [
        "mysql -u root -p secret",
        "mysql -p secret",
        "mysql -psecret",
        "-psecret",
        "-p secret",
        "curl -u admin:pw http://x",
        "curl --user admin:pw http://x",
        "curl https://user:pass@example.com/",
        "export DB_PASS=s3cr3t",
        "PASSWORD=abc123",
        "MYSQL_PWD=zzz mysql -e 'select 1'",
        "--password xxx",
        "--password=yyy",
    ]:
        s.check("match: %r" % cmd, hit(cmd), "漏报!")

    # N10: parse_target IPv4-mapped / zone
    cases = [
        ("fe80::1%eth0", (None, "fe80::1%eth0", None)),
        ("[fe80::1%eth0]:22", (None, "fe80::1%eth0", 22)),
        ("::ffff:1.2.3.4", (None, "::ffff:1.2.3.4", None)),
        ("[::ffff:1.2.3.4]:2222", (None, "::ffff:1.2.3.4", 2222)),
        ("2001:db8::1", (None, "2001:db8::1", None)),
    ]
    for target, want in cases:
        try:
            got = m.parse_target(target)
            s.check("parse %r" % target, got == want, "got=%r want=%r" % (got, want))
        except m.SshError as e:
            s.check("parse %r" % target, False, "意外拒绝: %s" % e)
    for target in ["host:22:33", "fe80::1%", "::ffff:1.2.3.999", "gg::1"]:
        try:
            m.parse_target(target)
            s.check("reject %r" % target, False, "竟然接受了")
        except m.SshError:
            s.check("reject %r" % target, True)

    # M1/L6: reconfigure 逻辑单元（FakeStream 模拟 Linux 流）
    class FakeStream:
        def __init__(self, enc, err):
            self.encoding = enc
            self.errors = err

        def reconfigure(self, **kw):
            self.encoding = kw.get("encoding", self.encoding)
            self.errors = kw.get("errors", self.errors)

    def linux_branch(streams):
        # 与 pyaissh._setup_console_utf8 Linux 分支同逻辑（复制验证）
        for stream in streams:
            try:
                if stream and stream.reconfigure and (
                        (stream.encoding or "").lower().replace("-", "") != "utf8"
                        or stream.errors != "replace"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    f1 = FakeStream("ascii", "strict")
    f2 = FakeStream("utf-8", "strict")
    f3 = FakeStream("utf-8", "replace")
    linux_branch([f1, f2, f3])
    s.check("LANG=C 流 -> utf-8/replace", f1.encoding == "utf-8" and f1.errors == "replace")
    s.check("utf-8+strict 流 -> errors=replace", f2.encoding == "utf-8" and f2.errors == "replace")
    s.check("已 replace 流不被重复配置", f3.encoding == "utf-8" and f3.errors == "replace")

    buf_strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    buf_rep = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
    try:
        buf_strict.write("\udcff")
        s.check("strict 流打印 surrogate 抛异常", False, "竟然没炸")
    except UnicodeEncodeError:
        s.check("strict 流打印 surrogate 抛异常", True)
    buf_rep.write(json.dumps({"msg": "bad-\udcff-name"}, ensure_ascii=False))
    buf_rep.flush()
    out = buf_rep.buffer.getvalue().decode("utf-8")
    s.check("replace 流输出仍是合法单行 JSON", json.loads(out)["msg"] == "bad-?-name",
            "got %r" % out)

    s.check("VERSION 合法且 >= 1.5",
            re.match(r"^\d+\.\d+\.\d+$", m.VERSION) is not None and m.VERSION >= "1.5.0")


# ============================================================
# 测试集 2：unit 凭据启发式 41 例
# ============================================================

def suite_unit_credential(s):
    m = _module()
    hit = ["-psecret", "-p secret", "-p'xxx'", '-p"xxx"', "--password=abc", "--password abc",
           "mysql -u root -p secret", "https://user:pass@host/", "DB_PASS=abc", "MYSQL_PWD=abc",
           "curl -u user:pass http://x", "sshpass -p topsecret cmd", "-p's3cr3t!'",
           "mysqldump -u root -pdb_pass123 db", "--password 'abc def'"]
    nohit = ["--profile x", "--parallel 4", "--progress", "--no-pager", "git log --no-pager",
             "--no-color", "--dry-run", "-p 22", "-p'22'", "-p123456", "ssh -p 22 root@h",
             "mkdir -p a/b", "tar -p x", "cp -p a b", "gcc -pthread", "rsync -p /x",
             "wget -p https://x", "pytest -p x", "echo -pabc", "git push", "--port 22",
             "systemctl --no-pager status ssh", "ls --no-pager", "--no-pager=true",
             "apt-get --no-pager list", "--no-plugins"]
    for c in hit:
        s.check("应命中 %r" % c[:30], bool(m.warn_sensitive_cmd(c, enabled=True)))
    for c in nohit:
        s.check("不应命中 %r" % c[:30], not m.warn_sensitive_cmd(c, enabled=True))


# ============================================================
# 测试集 3：live --sudo 提权 12 例
# ============================================================

_REQ_SUDO = ["PYAISSH_TEST_HOST", "PYAISSH_TEST_SUDO_USER", "PYAISSH_TEST_SUDO_PASSWORD",
             "PYAISSH_TEST_SUDO_NP_USER", "PYAISSH_TEST_SUDO_NP_PASSWORD"]


def suite_live_sudo(s):
    if _missing_env(_REQ_SUDO):
        print("SKIP: 需配置 %s（见 tests/README.md，凭据不入库）" % " / ".join(_REQ_SUDO))
        return None
    base = _live_host().split("@")[-1]
    sudo_user = os.environ.get("PYAISSH_TEST_SUDO_USER")
    np_user = os.environ.get("PYAISSH_TEST_SUDO_NP_USER")
    pw = os.environ.get("PYAISSH_TEST_SUDO_PASSWORD")
    np_pw = os.environ.get("PYAISSH_TEST_SUDO_NP_PASSWORD")
    tgt = "%s@%s" % (sudo_user, base)
    np_tgt = "%s@%s" % (np_user, base)

    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw, "--cmd", "id"], pw)
    s.check("T1 sudo 提权 uid=0", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw,
                          "--cmd", "id && whoami && echo SEG3_$UID"], pw)
    out = (j or {}).get("stdout", "")
    s.check("T2 && 第二段 uid=0", "uid=0(root)" in out and "root\n" in out and "SEG3_0" in out)
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw, "--cmd", "id"], pw)
    s.check("T3 stderr 无 password for", "password for" not in (j or {}).get("stderr", "").lower())
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", "WrongPass!", "--cmd", "id"], pw)
    stderr = (j or {}).get("stderr", "")
    s.check("T4 密码错失败", (j or {}).get("exit_success") is False
            and "incorrect" in stderr.lower())
    rc, j, _ = _live_run(["exec", np_tgt, "--sudo", "--cmd", "id"], np_pw)
    s.check("T5 NOPASSWD 免密成功", rc == 0 and "uid=0(root)" in (j or {}).get("stdout", ""))
    rc, j, _ = _live_run(["exec", np_tgt, "--sudo", "--cmd", "id && whoami"], np_pw)
    w = " ".join((j or {}).get("warnings") or [])
    s.check("T6 sudo -n 失败提示密码配置", (j or {}).get("exit_success") is False
            and "--sudo-password" in w)
    rc, j, _ = _live_run(["exec", np_tgt, "--sudo", "--sudo-password", "",
                          "--cmd", "id && whoami"], np_pw)
    w = " ".join((j or {}).get("warnings") or [])
    s.check("T7 空密码视为未设置", (j or {}).get("exit_success") is False
            and "--sudo-password" in w)
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--pty", "--sudo-password", pw, "--cmd", "id"], pw)
    s.check("T8 --pty 互斥 bad_args", (j or {}).get("error") == "bad_args")
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw, "--cmd", "id"], pw)
    s.check("T9 cmd 字段无密码", pw not in (j or {}).get("cmd", ""))
    script = os.path.join(_REPO, "tests", "_tmp_sudo.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write("id\nwhoami\necho DONE_$UID\n")
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw, "--cmd-file", script], pw)
    out = (j or {}).get("stdout", "")
    s.check("T10 cmd-file 整链提权", "uid=0(root)" in out and "DONE_0" in out)
    os.remove(script)
    rc, j, _ = _live_run(["exec", tgt, "--sudo", "--sudo-password", pw, "--cmd", "id"], pw)
    s.check("T11 无误报凭据 WARN", "疑似凭据" not in " ".join((j or {}).get("warnings") or []))
    rc, j, _ = _live_run(["exec", np_tgt, "--sudo", "--cmd", "id nonexistent_user_xyz"], np_pw)
    w = " ".join((j or {}).get("warnings") or [])
    s.check("T12 命令失败不误报需密码",
            (j or {}).get("exit_success") is False and "免密探测失败" not in w)


# ============================================================
# 测试集 4：live exec 行为 + --field（19 例）
# ============================================================

_REQ_EXEC = ["PYAISSH_TEST_HOST", "PYAISSH_TEST_PASSWORD"]


def suite_live_exec_field(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = _live_host()

    rc, j, _ = _live_run(["exec", tgt, "--cmd", "echo hello_exec"])
    s.check("exec echo", j and j.get("exit_success") and "hello_exec" in j.get("stdout", ""))
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "echo 你好世界"])
    s.check("exec 中文", j and "你好世界" in j.get("stdout", ""))
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "exit 3"])
    s.check("exec exit 3", j and j.get("exit_code") == 3 and j.get("exit_success") is False)
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "uname -s && echo SEG2"])
    s.check("exec 复合", j and "Linux" in j.get("stdout", "") and "SEG2" in j.get("stdout", ""))
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "ls /nonexistent_exec_xyz"])
    s.check("exec stderr 带回", j and j.get("exit_success") is False
            and "no such file" in j.get("stderr", "").lower())
    rc, j, _ = _live_run(["exec", tgt, "--pty", "--cmd", "tty"])
    s.check("exec pty", j and "/dev/" in j.get("stdout", ""))
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "sleep 4", "--idle-timeout", "1"], timeout=60)
    s.check("exec idle 超时", j and j.get("error") == "exec_idle_timeout"
            and j.get("remote_may_be_running") is True)
    rc, j, _ = _live_run(["exec", tgt, "--cmd", "seq 1 2000", "--max-output", "4096"])
    s.check("exec 截断标记", j and j.get("output_truncated") is True
            and "pyaissh" in j.get("stdout", ""))
    p = _live_sub(["exec", tgt, "--cmd-file", "-"], input_text="echo from_stdin && hostname\n")
    j = _last_json(p)
    s.check("exec cmd-file stdin", j and "from_stdin" in j.get("stdout", ""))
    p = _live_sub(["exec", tgt])
    j = _last_json(p)
    s.check("bad_args 无 cmd", j and j.get("error") == "bad_args")
    rc, j, _ = _live_run(["test", tgt])
    s.check("test 连接", j and j.get("ok") is True and "os" in j)
    rc, j, _ = _live_run(["ls", tgt, "--path", "/etc", "--limit", "3"])
    s.check("ls /etc", j and isinstance(j.get("entries"), list) and j["entries"])

    p = _live_sub(["exec", tgt, "--cmd", "echo field_v", "--field", "stdout"])
    s.check("field stdout 裸值", p.stdout.strip() == "field_v" and "[SSH]" not in p.stderr)
    p = _live_sub(["exec", tgt, "--cmd", "sh -c 'echo v; echo e >&2'", "--field", "stdout"])
    s.check("field stderr 盲区提示", "结果含非空 stderr" in p.stderr
            and "[SSH]" not in p.stderr and p.stdout.strip() == "v")
    p = _live_sub(["exec", tgt, "--cmd", "sh -c 'echo o; echo e >&2'", "--field", "stdout,-stderr"])
    s.check("field stdout,-stderr 分流", p.stdout.strip() == "o" and "e" in p.stderr)
    p = _live_sub(["exec", "root@203.0.113.250", "--cmd", "x", "--timeout", "3",
                   "--field", "stdout"], timeout=60)
    j = _last_json(p)
    s.check("field 工具错误完整 JSON", j and j.get("ok") is False and j.get("error"))
    p = _live_sub(["exec", tgt, "--cmd", "echo x", "--field", "exit_code,exit_success"])
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    s.check("field 多字段", len(lines) == 2 and lines[0] == "0" and lines[1] == "True")
    p = _live_sub(["exec", tgt, "--cmd", "echo x", "--field", "stdout", "--text"])
    j = _last_json(p)
    s.check("field --text 互斥", j and j.get("error") == "bad_args")
    p = _live_sub(["exec", tgt, "--cmd", "echo x", "--field", "no_such_field"])
    s.check("field 字段不存在提示", "字段不存在" in p.stderr)


# ============================================================
# 测试集 5：live 传输往返
# ============================================================

def suite_live_transfer(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = _live_host()
    local = os.path.join(_REPO, "tests", "_tmp_xfer.bin")
    dl = os.path.join(_REPO, "tests", "_tmp_xfer_dl.bin")
    with open(local, "wb") as f:
        f.write(os.urandom(300000))
    rc, j, _ = _live_run(["upload", tgt, "--local", local, "--remote", "/tmp/_t_xfer.bin"],
                         timeout=300)
    s.check("upload 300KB", j and j.get("ok") is True and j.get("bytes_transferred") == 300000)
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = _live_run(["download", tgt, "--remote", "/tmp/_t_xfer.bin", "--local", dl],
                         timeout=300)
    ok_dl = j and j.get("ok") is True and os.path.exists(dl) \
        and open(dl, "rb").read() == open(local, "rb").read()
    s.check("download 往返字节一致", ok_dl)
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = _live_run(["download", tgt, "--remote", "/tmp/_t_xfer.bin", "--local", dl,
                          "--parallel", "4"], timeout=300)
    ok_par = j and j.get("ok") is True and j.get("parallel_used") == 4 \
        and os.path.exists(dl) and open(dl, "rb").read() == open(local, "rb").read()
    s.check("download --parallel 4", ok_par)
    _live_run(["exec", tgt, "--cmd", "rm -f /tmp/_t_xfer.bin"])
    os.remove(local)
    if os.path.exists(dl):
        os.remove(dl)


# ============================================================
# CLI：选择 / 编排
# ============================================================

SUITES = [
    ("unit_regression", "回归 54 例（凭据矩阵/parse_target/编码/stdin）", suite_unit_regression),
    ("unit_credential", "凭据启发式 41 例", suite_unit_credential),
    ("live_sudo", "--sudo 提权 12 例（真机）", suite_live_sudo),
    ("live_exec_field", "exec 12 + --field 7 例（真机）", suite_live_exec_field),
    ("live_transfer", "传输往返 3 例（真机）", suite_live_transfer),
]


def _run_suite(idx):
    name, desc, fn = SUITES[idx]
    print("\n>>> %s（%s）" % (name, desc))
    s = _Suite(name)
    fn(s)
    if s.pass_n == 0 and s.fail_n == 0:
        return None  # SKIP（live 缺凭据返回 None 已 print）
    n, f = s.result()
    return f == 0


def _interactive():
    print("\npyaissh 测试集选择：")
    for i, (name, desc, _) in enumerate(SUITES, 1):
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
    ap = argparse.ArgumentParser(description="pyaissh 统一测试（单文件）")
    ap.add_argument("--all", action="store_true", help="全量")
    ap.add_argument("--unit", action="store_true", help="仅单元集")
    ap.add_argument("--sudo", action="store_true")
    ap.add_argument("--exec", action="store_true")
    ap.add_argument("--transfer", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出测试集")
    a = ap.parse_args()

    if a.list:
        for i, (name, desc, _) in enumerate(SUITES, 1):
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
        results.append((name, _run_suite(idx)))
    ran = [(n, o) for n, o in results if o is not None]
    skipped = [n for n, o in results if o is None]
    failed = [n for n, o in ran if not o]
    print("\n=== 汇总: %s ===" % ("ALL PASS" if ran and not failed else "FAIL: " + ", ".join(failed)
                                  if failed else "（全部跳过）"))
    if skipped:
        print("跳过: %s" % ", ".join(skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
