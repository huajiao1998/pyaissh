# -*- coding: utf-8 -*-
"""pyaissh 统一测试（单文件：全部测试集内联于此，运行时选择要跑的集）。

用法：
  python tests/run_tests.py               交互菜单（数字选集）
  python tests/run_tests.py --all         全量：unit 先、live 后
  python tests/run_tests.py --unit        仅单元（无网络）
  python tests/run_tests.py --artifacts   仅制品结构集（域横幅/代码地图）
  python tests/run_tests.py --sudo --exec --transfer  指定 live 集
  python tests/run_tests.py --list        列出测试集

测试集：
  1) unit_regression   回归（凭据矩阵/parse_target/编码/stdin/--exclude 匹配）
  2) unit_credential   凭据启发式（真命中/误报豁免矩阵）
  3) unit_artifacts    制品结构（域边界横幅/代码地图/VERSION）
  4) unit_host         host add/remove/list 闭环（副本 .env）
  5) live_sudo         --sudo 提权（真机）
  6) live_exec_field   exec+field（真机：行为/超时/field/失败尾巴/progress）
  7) live_transfer     传输往返（真机：默认/并行/断点续传/排除）

本文件代码地图（改测试先看这里；维护记录见 tests/CHANGELOG.md）：
  [框架]      _module()  被测模块加载（PYAISSH_PY / 缺省根）
              _bin()     live 被测二进制（PYAISSH_BIN / 缺省根）
              _Suite     断言运行器（check/计数/result）
              _live_run/_live_sub/_last_json/_live_host/_missing_env
  [unit 集]   suite_unit_regression  回归（verify_r3 迁入，凭据矩阵/parse_target/编码）
              suite_unit_credential  凭据启发式（真命中/误报豁免矩阵）
              suite_unit_artifacts   制品结构（构建产物可读性）
  [live 集]   suite_live_sudo        --sudo 提权（真机）
              suite_live_exec_field  exec+field（真机）
              suite_live_transfer    传输往返（真机）
  [CLI]       SUITES/_run_suite/_interactive/main
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
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
_py_module = None

# 看门狗检查周期：与 CLI 同源（PYAISSH_SESSION_TTL_TICK）。开发用 `--fast` 调到 3s，
# 那些"等一个 tick"的用例随之缩短；发布前全量跑默认 15s（真实生产值）。
_FAST_TICK = 3
_TICK = int(os.environ.get("PYAISSH_SESSION_TTL_TICK") or 15)


def _wait(ticks=1, extra=3):
    """等 n 个看门狗检查周期（随 --fast 自动缩短）。`extra` 是覆盖 SSH 往返的余量。"""
    return max(2, int(ticks) * int(_TICK) + int(extra))

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


def _live_sub_bytes(args, payload, timeout=120):
    """stdin 走**字节**（CRLF 用例必须）。

    坑：text=True + input=str 在 Windows 上会把 \\n 再翻成 \\r\\n（CRLF → CRCRLF），
    计数与断言都会失真——那是测试助手的行为，不是被测程序。
    """
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = os.environ.get("PYAISSH_TEST_PASSWORD", "")
    p = subprocess.run([sys.executable, "-B", _bin()] + args, input=payload,
                       capture_output=True, env=env, timeout=timeout)
    j = None
    for line in reversed(p.stdout.decode("utf-8", "replace").splitlines()):
        try:
            j = json.loads(line)
            break
        except Exception:
            pass
    return p.returncode, j, p.stderr.decode("utf-8", "replace")


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

    # v2.1 --exclude 匹配单元（_excluded_by：模式命中文件名或相对路径即排除）
    s.check("exclude 命中目录名", m._excluded_by("a/b/node_modules", "node_modules",
                                                 ["node_modules", ".git"]))
    s.check("exclude 命中相对路径", m._excluded_by("dist/app.js.map", "app.js.map", ["*.map"]))
    s.check("exclude 目录剪枝靠名字命中", m._excluded_by("static/.git", ".git", [".git"]))
    s.check("exclude 不误伤", not m._excluded_by("src/main.py", "main.py",
                                                 ["node_modules", ".git", "*.map"]))

    # v2.2 后台作业单元：默认保留量 + 作业脚本生成（纯函数，不连远端）
    s.check("默认 max-output 64KB（防宿主裁中段）", m.DEFAULT_MAX_OUTPUT == 65536,
            "got %r" % m.DEFAULT_MAX_OUTPUT)
    paths = m._job_files("/tmp/pyaissh-jobs", "j1")
    s.check("job 路径表（含 job.pid）", paths["log"].endswith("/job.log")
            and paths["rc"].endswith("/job.rc") and paths["pid"].endswith("/job.pid")
            and paths["dir"] == "/tmp/pyaissh-jobs/j1", repr(paths))
    tricky = "echo a'b\nls -l \"$HOME\"\nexit 3"
    job_sh, run_sh = m._detach_scripts(paths, tricky)
    s.check("job.sh 保留命令原文", tricky in job_sh and job_sh.startswith("#!/bin/bash"))
    s.check("run.sh 落日志+写 rc", ("bash " in run_sh and paths["log"] in run_sh
                                    and paths["rc"] in run_sh and "echo $?" in run_sh), run_sh)
    s.check("run.sh 收严 umask（job.log/job.rc 0600）", "umask 077" in run_sh, run_sh)

    # v2.2.4 CRLF 归一（纯函数，不连远端）
    norm = m._normalize_cmd_newlines
    s.check("CRLF → LF 并计数", norm("a\r\nb\r\n") == ("a\nb\n", 2), repr(norm("a\r\nb\r\n")))
    s.check("孤立 CR → LF 并计数", norm("echo X\r") == ("echo X\n", 1), repr(norm("echo X\r")))
    s.check("混合 CRLF/CR 一起归一", norm("a\r\nb\rc\n") == ("a\nb\nc\n", 2),
            repr(norm("a\r\nb\rc\n")))
    s.check("纯 LF 零改动（文本与计数都不变）", norm("a\nb\n") == ("a\nb\n", 0))
    s.check("转义写法 \\r（反斜杠+r 字面量）不受影响",
            norm("printf 'a\\rb'\n") == ("printf 'a\\rb'\n", 0))
    _a = m.build_parser().parse_args(["exec", "h", "--cmd", "x"])
    _b = m.build_parser().parse_args(["exec", "h", "--cmd", "x", "--keep-crlf"])
    s.check("--keep-crlf 默认关、显式开（exec 参数）",
            _a.keep_crlf is False and _b.keep_crlf is True)

    # v2.3 会话单元（纯函数，不连远端）
    sf = m._session_files("/tmp/pyaissh-sessions", "demo")
    s.check("会话路径表完整（dir/fifo/log/pid/meta/bash/token）",
            sf["dir"] == "/tmp/pyaissh-sessions/demo" and sf["fifo"].endswith("/in")
            and sf["log"].endswith("/out.log") and sf["pid"].endswith("/sess.pid")
            and sf["bash"].endswith("/bash.pid") and sf["token"].endswith("/last.token"),
            repr(sf))
    s.check("会话名正则：接受合法、拒绝穿越/空/超长",
            bool(m._SESSION_NAME_RE.match("work-1.x")) and not m._SESSION_NAME_RE.match("../etc")
            and not m._SESSION_NAME_RE.match("") and not m._SESSION_NAME_RE.match("a" * 33)
            and not m._SESSION_NAME_RE.match("-lead"))
    # --data 转义解析
    un = m._session_unescape
    s.check("--data 转义：\\n/\\r/\\t/\\xNN/\\\\ 与原文",
            un("y\\n") == "y\n" and un("a\\tb") == "a\tb" and un("\\x03") == "\x03"
            and un("\\\\") == "\\" and un("plain") == "plain", repr((un("y\\n"), un("\\x03"))))
    # kill 命令（R1 加固）：两个根 + 根自证闸门 + ROOTS/HAD 回传
    _kc = m._session_kill_cmd(m._session_files("/tmp/pyaissh-sessions", "demo"))
    s.check("kill 命令：根候选含 sess.pid / bash.pid / watch.pid / 会话 shell 的父进程",
            "sess.pid" in _kc and "bash.pid" in _kc and "watch.pid" in _kc
            and 'for r in "$P" "$B" "$W"' in _kc and 'ps -o ppid= -p "$B"' in _kc,
            _kc[:200])
    s.check("kill 命令：根自证闸门（argv 必须含本会话目录，防 pid 回收误杀）",
            'case "$A" in *"$D"*)' in _kc and 'D=' in _kc, _kc[:160])
    s.check("kill 命令：回传 ROOTS/HAD 供上层判 verified/是否告警",
            "__PYAISSH_SESS__ROOTS=$ROOTS" in _kc and "__PYAISSH_SESS__HAD=$HAD" in _kc,
            _kc[:160])
    s.check("kill 命令：awk 根按循环变量（不再写死 $P）",
            'awk -v root="$r"' in _kc and 'awk -v root="$P"' not in _kc, _kc[:160])
    s.check("list 会话年龄常量（24h 提醒）", m._SESSION_STALE_HINT == 86400,
            repr(m._SESSION_STALE_HINT))
    # 空闲回收（v2.3.0）：TTL 解析 + 看门狗脚本 + 会话路径/元信息
    _p = m._session_parse_ttl
    s.check("TTL 解析：默认 600s，支持 30s/10m/2h 与 0=关闭",
            _p(None)[0] == 600 and _p("")[0] == 600 and _p("0")[0] == 0
            and _p("30")[0] == 30 and _p("30s")[0] == 30 and _p("10m")[0] == 600
            and _p("2h")[0] == 7200, repr([_p(x) for x in (None, "0", "30", "10m", "2h")]))
    s.check("TTL 解析：非法值给可读错误",
            _p("abc")[0] is None and "无法解析" in (_p("abc")[1] or "")
            and _p("10x")[0] is None, repr(_p("abc")))
    _f = m._session_files("/tmp/pyaissh-sessions", "demo")
    s.check("会话路径表含 beat（活动时间戳）", _f["beat"].endswith("/beat"), _f["beat"])
    _ws = m._session_watchdog_script(_f, 600)
    s.check("看门狗：目录消失时先临终清理再退出（不留常驻循环）",
            'if [ ! -d "$D" ]; then cleanup_tree; exit 0; fi' in _ws, _ws[:160])
    s.check("看门狗：有前台命令就续期 + beat 判闲",
            'pgrep -P "$B"' in _ws and 'LAST=$(<"$BEAT")' in _ws
            and 'printf \'%s\\n\' "$NOW" > "$BEAT"' in _ws, _ws[:200])
    s.check("看门狗：自证闭包（argv 含会话目录）+ 先删目录再杀",
            'case "$A" in *"$D"*)' in _ws and 'case " $SEEN "' in _ws
            and _ws.index('[ "$1" = rm ] && rm -rf "$D"') < _ws.index("kill -TERM $T"),
            _ws[-400:])
    # 临终带走（用户提的补充）：有人 rm -rf 会话目录时，看门狗最后收一次自己的进程树再自退
    s.check("看门狗：共用一套清理实现（cleanup_tree 函数 + 两处调用）",
            "cleanup_tree() {" in _ws and "cleanup_tree rm" in _ws
            and "cleanup_tree; exit 0" in _ws, _ws[:400])
    s.check("看门狗：每轮开头就记 pid/meta（内建读、空读不覆盖旧值）",
            'if [ -r "$SPID" ]; then _pv=$(<"$SPID"); [ -n "$_pv" ] && P=$_pv; fi' in _ws
            and '[ -n "$_pv" ] && B=$_pv' in _ws and '[ -z "$M0" ] && M0=$_pv' in _ws,
            _ws[:600])
    s.check("看门狗：判闲前重读 shell pid，且**pid 未知一律视为忙**（实测踩过的误回收）",
            'if [ -r "$BPID" ]; then _pv=$(<"$BPID"); [ -n "$_pv" ] && B=$_pv; fi' in _ws
            and 'if [ -z "$B" ]; then printf \'%s\\n\' "$NOW" > "$BEAT"; continue; fi' in _ws,
            _ws[-700:])
    s.check("看门狗：归属自检两道（watch.pid≠自己 / meta 变了 ⇒ 退出，防同名新会话被误杀）",
            'W=""; [ -r "$WPID" ] && W=$(<"$WPID")' in _ws
            and '[ "$W" != "$$" ] && exit 0' in _ws
            and '[ "$M" != "$M0" ] && exit 0' in _ws, _ws[:700])
    s.check("看门狗：临终路径不自证根时不乱杀，且不写 wd.log（用户要求不留审计痕）",
            _ws.split('if [ ! -d "$D" ]')[1].split("fi")[0].count("wd.log") == 0
            and "|| return 1" in _ws, _ws[:200])
    s.check("看门狗：读文件前用内建 [ -r ] 保护（缺失文件不往 stderr 报错）",
            '[ -r "$SPID" ]' in _ws and '[ -r "$BPID" ]' in _ws
            and '[ -r "$BEAT" ]' in _ws and '[ -r "$META" ]' in _ws, _ws[:400])
    # 设计 C（v2.3.0）：单进程看门狗 = read -t + 自持 FIFO fd（省掉 sleep 的 1.9 MB/1 进程）
    #   + 防 spin 护栏（内建 $SECONDS 计时，连续 3 次立刻返回就退回外部 sleep）
    #   + 内建读（$(<) 不 fork cat/stat）+ $EPOCHSECONDS（bash≥5 不 fork date）
    s.check("看门狗 C：read -t + 自持 FIFO fd（单进程睡眠，无 sleep 子进程）",
            'read -t "$TICK" -r -u 9 _x' in _ws and 'exec 9<>"$WDFIFO"' in _ws
            and 'mkfifo -m 600 "$WDFIFO"' in _ws, _ws[:260])
    s.check("看门狗 C：防 spin 护栏（连续 3 次立刻返回 ⇒ 退回 sleep + 记 wd.log）",
            "$((SECONDS - T0)) -lt 1" in _ws and '"$FAST" -ge 3' in _ws
            and 'sleep "$TICK"' in _ws and 'wd.log' in _ws, _ws[:400])
    s.check("看门狗 C：内建读 + EPOCHSECONDS（不 fork cat/stat/date）",
            '$(<"$SPID")' in _ws and '$(<"$BPID")' in _ws and '$(<"$META")' in _ws
            and "NOW=${EPOCHSECONDS:-$(date +%s)}" in _ws
            and "cat " not in _ws and "stat -c" not in _ws, _ws[:300])
    s.check("看门狗 C：beat 非法/缺失只续期不回收",
            "case \"$LAST\" in ''|*[!0-9]*)" in _ws, _ws[:300])
    s.check("看门狗 C：无 %% 转义残留（生成脚本里不该出现双百分号）", "%%" not in _ws, _ws[:160])
    # tick 可配（`--fast` 用；生产默认 15）
    _save_tick = os.environ.get("PYAISSH_SESSION_TTL_TICK")
    try:
        os.environ.pop("PYAISSH_SESSION_TTL_TICK", None)
        _t1 = m._session_tick_default()
        os.environ["PYAISSH_SESSION_TTL_TICK"] = "3"
        _t2 = m._session_tick_default()
        for _bad in ("0", "abc", "9999", "2.5"):
            os.environ["PYAISSH_SESSION_TTL_TICK"] = _bad
            globals()["_t_" + _bad.replace(".", "_")] = m._session_tick_default()
        _t3, _t4 = globals()["_t_0"], globals()["_t_abc"]
        _t5, _t6 = globals()["_t_9999"], globals()["_t_2_5"]
    finally:
        if _save_tick is None:
            os.environ.pop("PYAISSH_SESSION_TTL_TICK", None)
        else:
            os.environ["PYAISSH_SESSION_TTL_TICK"] = _save_tick
    s.check("看门狗 tick 可配：默认 15 / 3 生效 / 0・abc・9999・2.5 一律回落 15（只收整数）",
            (_t1, _t2, _t3, _t4, _t5, _t6) == (15, 3, 15, 15, 15, 15),
            repr((_t1, _t2, _t3, _t4, _t5, _t6)))
    s.check("测试等待随 tick 缩短：_wait(2) = 2*tick+3（--fast 时 33s→9s）",
            _wait(2) == 2 * _TICK + 3, "_TICK=%s _wait(2)=%s" % (_TICK, _wait(2)))
    s.check("看门狗：TTL/周期写进脚本且可关闭",
            "TTL=600" in _ws and "TICK=%d" % m._SESSION_TTL_TICK in _ws
            and "watch.sh" not in m._session_start_cmd(_f, 200, False, 0)
            and "watch.sh" in m._session_start_cmd(_f, 200, False, 600), _ws[:80])
    _sc6 = m._session_start_cmd(_f, 200, False, 600)
    s.check("看门狗启动：前台写脚本 + 单条后台启动（`;` 分隔，`&` 只作用于 setsid 那条）",
            "2>/dev/null; chmod 700" in _sc6 and "; setsid nohup bash" in _sc6
            and "& echo $!" in _sc6 and "} >/dev/null 2>&1 </dev/null &" not in _sc6,
            _sc6[-360:])
    s.check("start 命令：beat 初始化写 epoch（看门狗靠内容判闲，不能是空文件）",
            "printf '%s\\n' \"$(date +%s)\" > '/tmp/pyaissh-sessions/demo/beat'" in _sc6,
            _sc6[:400])
    _cap = {}
    _orig_run = m._session_run
    m._session_run = lambda client, cmd, stdin_data=None, timeout=30: (
        _cap.setdefault("cmd", cmd), (0, "", ""))[1]
    try:
        m._session_touch(None, _f)
    finally:
        m._session_run = _orig_run
    s.check("交互续期：_session_touch 写的是 epoch 秒（不再只 touch mtime）",
            _cap.get("cmd") == ("printf '%s\\n' \"$(date +%s)\" > "
                                "'/tmp/pyaissh-sessions/demo/beat' 2>/dev/null || true"),
            repr(_cap.get("cmd")))
    s.check("start 命令：meta 写 4 字段（pty/cols/起始秒/TTL）",
            "printf '%s %s %s %s\\n' \"$PTY\" 200 \"$(date +%s)\" 600" in _sc6, _sc6[-420:-260])
    # 孤儿扫描（kill --all 的 argv 自证）：只认"以会话身份出现"的进程，不误杀"提到路径"的进程
    _ps = "\n".join([
        "  101 root  bash -c umask 077; exec 9<>'/tmp/pyaissh-sessions/gone1/in'; sleep 9",
        "  102 root  script -qfc stty -echo /tmp/pyaissh-sessions/gone1/out.log",
        "  103 root  bash /tmp/pyaissh-sessions/gone2/watch.sh",
        "  104 root  tail -f /tmp/pyaissh-sessions/gone1/out.log",
        "  105 root  bash -c 'sleep 300' /tmp/pyaissh-sessions/gone1/out.log",
        "  106 root  bash -c exec 9<>'/tmp/pyaissh-sessions/alive/in'; sleep 9",
        "  107 root  bash -c exec 9<>'/tmp/pyaissh-sessions/../etc/in'; x",
        "  108 root  ps -eo pid=,args=",
    ])
    _cands = m._session_orphan_candidates(_ps, "/tmp/pyaissh-sessions", {"alive"})
    _kinds = dict((p, k) for p, _n, k in _cands)
    s.check("孤儿扫描：认出 starter/pty 包装/看门狗（目录已不在的那些）",
            sorted(p for p, _n, _k in _cands) == [101, 102, 103], repr(_cands))
    s.check("孤儿扫描：类型标注正确",
            _kinds.get(101) == "starter" and _kinds.get(102) == "pty-wrapper"
            and _kinds.get(103) == "watchdog", repr(_kinds))
    s.check("孤儿扫描：不误杀「只是提到路径」的进程（tail / argv 里带路径的旁观者）",
            104 not in _kinds and 105 not in _kinds, repr(_cands))
    s.check("孤儿扫描：目录还在的会话不在这里处理（走正常 kill 路径）",
            106 not in _kinds, repr(_cands))
    s.check("孤儿扫描：路径穿越/非法名字被拒", 107 not in _kinds, repr(_cands))
    _kc2 = m._session_pid_kill_cmd(101)
    s.check("孤儿清理命令：按 pid 做闭包 + TERM→KILL + 回传 SWEPT/LEFT",
            'awk -v root="101"' in _kc2 and "kill -TERM $T" in _kc2
            and "kill -KILL $K" in _kc2 and "SESS__SWEPT=$SWEPT" in _kc2, _kc2[:160])
    # 哨兵包裹（真 bug 的护栏：哨兵必须与命令同一行被解析，否则被 read 吃掉）
    pl = m._session_payload_text("read -p 'x' V; echo $V", "abcd1234")
    s.check("命令与哨兵同一行（{ ...; }; echo 哨兵）",
            pl.startswith("\n{\n") and "\n}; echo \"%sabcd1234__$?\"\n" % m.SESSION_RC_PREFIX in pl,
            repr(pl))
    s.check("载荷以换行开头（R2：先把上次断线残留的半行终结掉，防串行）",
            pl.startswith("\n") and m._session_payload_text("echo x", "tk", plain=True).startswith("\n"),
            repr(pl[:6]))
    s.check("多行命令也被包裹且状态留在同一 shell（用 {} 非 ()）",
            "(" not in pl.split("\n")[0] and "{\n" in pl)
    # 哨兵切分：只回"这条命令"的输出
    log = ("%saaaa__0\n/var/log\n%sbbbb__0\n"
           "bash: nope: command not found\n%scccc__127\n" % (m.SESSION_RC_PREFIX,
                                                             m.SESSION_RC_PREFIX,
                                                             m.SESSION_RC_PREFIX))
    out_a, rc_a, tok_a = m._session_slice_by_sentinel(log, "aaaa")
    out_b, rc_b, _ = m._session_slice_by_sentinel(log, "bbbb")
    out_c, rc_c, _ = m._session_slice_by_sentinel(log, "cccc")
    _out_last, rc_last, tok_last = m._session_slice_by_sentinel(log)
    s.check("按 token 切出各自输出与退出码",
            out_a == "" and rc_a == 0 and tok_a == "aaaa" and out_b == "/var/log" and rc_b == 0
            and rc_c == 127 and "command not found" in out_c,
            repr((out_a, rc_a, out_b, out_c, rc_c)))
    s.check("不带 token → 取最后一个哨兵（127 那条）",
            rc_last == 127 and tok_last == "cccc", repr((rc_last, tok_last)))
    _o, rc_run, tok_run = m._session_slice_by_sentinel(log + "still running\n", "zzzz")
    s.check("目标 token 未出现 → running（rc=None）且给最后哨兵之后的输出",
            rc_run is None and tok_run is None and "still running" in _o, repr((rc_run, _o)))
    # 输出清洗
    dirty = ("Script started on x [COMMAND=\"bash\"]\r\n\x1b[31mRED\x1b[0m\r\n"
             "%sab12__0\r\n" % m.SESSION_RC_PREFIX)
    s.check("清洗：去 CR/ANSI/哨兵行/script 头",
            m._session_clean_text(dirty) == "RED", repr(m._session_clean_text(dirty)))
    s.check("--keep-ansi 时保留 ANSI 码",
            "\x1b[31m" in m._session_clean_text(dirty, strip_ansi=False))

    # v2.3.0：SFTP 看门狗窗口可配（PYAISSH_SFTP_IO_TIMEOUT）——极慢链路单次大读的逃生阀
    _oe = os.environ.get("PYAISSH_SFTP_IO_TIMEOUT")
    try:
        os.environ.pop("PYAISSH_SFTP_IO_TIMEOUT", None)
        s.check("SFTP 窗口：未设 env 时无覆盖（用默认 30s）", m._sftp_env_timeout() is None)
        os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = "90"
        s.check("SFTP 窗口：env 生效", m._sftp_env_timeout() == 90.0)
        os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = "90.5"
        s.check("SFTP 窗口：接受小数", m._sftp_env_timeout() == 90.5)
        _bad_ok = True
        for bad in ("abc", "0", "-5", " "):
            os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = bad
            if m._sftp_env_timeout() is not None:
                _bad_ok = False
                s.check("SFTP 窗口：非法值 %r 应忽略" % bad, False, "未忽略")
                break
        if _bad_ok:
            s.check("SFTP 窗口：非法值（abc/0/-5/空白）一律忽略并回落默认", True)
    finally:
        if _oe is None:
            os.environ.pop("PYAISSH_SFTP_IO_TIMEOUT", None)
        else:
            os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = _oe
    # 单引号路径注入防护：路径只做 POSIX 单引号转义后进 run.sh
    odd = m._job_files("/tmp/it's dir", "j2")
    _j2, run2 = m._detach_scripts(odd, "true")
    s.check("run.sh 单引号转义", "'\\''" in run2, run2)
    # job-id 校验（防路径穿越）
    s.check("job-id 合法", bool(m._JOB_ID_RE.match("20260913-155021-25332"))
            and bool(m._JOB_ID_RE.match("job_1.x")))
    s.check("job-id 拒绝穿越", not m._JOB_ID_RE.match("../etc")
            and not m._JOB_ID_RE.match("/abs") and not m._JOB_ID_RE.match(""))
    s.check("sh_quote 转义", m._sh_quote("a'b") == "'a'\\''b'")


# ============================================================
# 测试集 2：unit 凭据启发式 47 例（含 $(cat 豁免）
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
    # v2.1：从文件读值 $(cat f) / $(<f) 豁免（值不进命令行文本，无明文泄漏，WARN 只剩噪音）
    nohit += ["PW=$(cat /root/.abbs-webui-password)", "DB_PASS=$(cat /srv/x)",
              "export PASS=$(cat /tmp/p)", "mysql -u root -p $(cat /etc/mysql/pw)",
              "PW=$(< ~/.secret)", "my_pw=$(cat ~/.pw)"]
    # v2.1.4 P0：六误报修复（工具 flag/大小写/空值/打印段/属性空值）
    nohit += ["grep -p foo /etc/passwd", "grep -P 'a+b' file",
              "ffmpeg -pix_fmt yuv420p in.mp4", "useradd -p '' guest",
              "echo 'PASSWORD='", "java -Dspring.datasource.password= -jar app.jar"]
    # v2.1.4：真命中边界保留（&& 后真命令段独立命中 / 非空 -p 值仍命中）
    hit += ["echo x && mysql -u r -psecret", "useradd -p 'hash123' bob"]
    for c in hit:
        s.check("应命中 %r" % c[:30], bool(m.warn_sensitive_cmd(c, enabled=True)))
    for c in nohit:
        s.check("不应命中 %r" % c[:30], not m.warn_sensitive_cmd(c, enabled=True))


# ============================================================
# 测试集 3：unit 制品结构（构建产物可读性——域边界横幅 / 域 docstring
#           代码地图 / VERSION 一致性；防构建器退化）
# ============================================================

def suite_unit_artifacts(s):
    m = _module()
    src_path = getattr(m, "__file__", None)
    if not src_path or not os.path.exists(src_path):
        s.check("可读被测源码文件", False, "module.__file__ 不可用: %r" % src_path)
        return
    text = open(src_path, encoding="utf-8", errors="replace").read()
    lines = text.splitlines()

    # 域边界横幅：12 个（域 01..12，00=文件头无横幅）+ 有序 + 带标题
    banners = re.findall(r"# =+ \[域 (\d+)/13\]", text)
    s.check("域横幅 12 个", len(banners) == 12, "got %d: %r" % (len(banners), banners))
    s.check("域横幅序 01..12", banners == ["%02d" % i for i in range(1, 13)],
            "got %r" % banners)
    titled = re.findall(r"# =+ \[域 \d+/13\]\s+([^=]+?)\s+=+", text)
    s.check("横幅标题非空", len(titled) == 12 and all(t.strip() for t in titled),
            "got %d 标题" % len(titled))

    # 域 docstring 代码地图：每个横幅后紧跟本域 docstring（""" 开头）
    ok_doc = 0
    for i, ln in enumerate(lines):
        if re.match(r"# =+ \[域 \d+/13\]", ln):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.lstrip().startswith('"""'):
                ok_doc += 1
    s.check("横幅后跟域 docstring", ok_doc == 12, "got %d/12" % ok_doc)
    # 代码地图 docstring 分区总数（文件头 docstring + 12 域 docstring ≥ 13）
    heads = sum(1 for ln in lines if ln.lstrip().startswith('"""'))
    s.check("docstring 分区 ≥13", heads >= 13, "got %d" % heads)

    # VERSION：源码文本与模块一致（防单文件漂移）
    vm = re.search(r'VERSION = "([^"]+)"', text)
    s.check("VERSION 一致", bool(vm) and vm.group(1) == m.VERSION,
            "src=%r module=%r" % (vm.group(1) if vm else None, m.VERSION))

    # MANIFEST 有效性（v2.2.4 加：此前锚悄悄过期没人发现；2026-09-21 随 split 移除一并
    # 去掉锚列——锚的唯一消费者是 split，留着只会再烂一次。现在只校验顺序清单本身）
    mf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "pyaissh-dev", "MANIFEST_domains.txt")
    if not os.path.exists(mf):
        s.check("MANIFEST 存在", False, mf)
    else:
        names = []
        for line in open(mf, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line.split("|")[0].strip())
        s.check("MANIFEST 13 域", len(names) == 13, "got %d: %r" % (len(names), names))
        s.check("MANIFEST 域序 00..12",
                [n.split("_")[0] for n in names] == ["%02d" % i for i in range(13)],
                "got %r" % [n.split("_")[0] for n in names])
        missing = [n for n in names
                   if not os.path.exists(os.path.join(os.path.dirname(mf), "domains", n))]
        s.check("域文件齐全（MANIFEST 列出的 13 个都在）", not missing, "缺: %s" % missing)


# ============================================================
# 测试集 4：unit host add/remove/list 闭环（v2.1.4 自动化）
# 被测模块复制到临时副本 importlib——host 命令写"模块同目录 .env"=
# 副本 .env，不碰仓库根（此前 CHANGELOG 自述"手动验证"的缺口）
# ============================================================

def suite_unit_host(s):
    import argparse
    import contextlib
    import importlib.util
    import shutil

    m = _module()
    # 不用 tempfile.mkdtemp：它在受限令牌下建 0o700 目录（空 DACL），
    # 创建进程自己都写不进（os.chmod WinError 5）——用脚本同目录 +
    # os.makedirs 默认 ACL 继承，可写可删（实测教训 2026-09-08）
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".tmp_host_%d" % os.getpid())
    os.makedirs(tmp, exist_ok=True)
    try:
        dst = os.path.join(tmp, "pyaissh.py")
        shutil.copyfile(os.path.abspath(m.__file__), dst)
        spec = importlib.util.spec_from_file_location("pyaissh_copy", dst)
        hm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hm)
        env_p = os.path.join(tmp, ".env")

        def ns(**kw):
            d = dict(json=True, field=None)
            d.update(kw)
            return argparse.Namespace(**d)

        def quiet(fn):
            # host 命令正常输出 JSON 到 stdout——测试重定向防污染
            with contextlib.redirect_stdout(io.StringIO()):
                return fn()

        # add：写 .env（含 # 密码引号包裹）
        quiet(lambda: hm.cmd_host_add(ns(name="prod", host_target="root@1.2.3.4", password="Secret#9!", key=None)))
        txt = open(env_p, encoding="utf-8").read()
        s.check("host add 写 .env", "PYAISSH_HOST_PROD=root@1.2.3.4" in txt
                and 'PYAISSH_HOST_PROD_PASSWORD="Secret#9!"' in txt, txt)
        # add 幂等（重复 add 更新不重复行）
        quiet(lambda: hm.cmd_host_add(ns(name="prod", host_target="root@1.2.3.4", password="NewPw2", key=None)))
        txt = open(env_p, encoding="utf-8").read()
        s.check("host add 幂等更新", txt.count("PYAISSH_HOST_PROD=") == 1
                and "NewPw2" in txt and 'Secret#9!' not in txt, txt)
        # add 第二个别名
        hm.cmd_host_add(ns(name="stage", host_target="deploy@1.2.3.5:2222", password=None,
                           key="/k/kk"))
        # list：entries 正确、不回显密码/密钥
        entries = hm._env_host_entries(env_p)
        names = sorted(e[0] for e in entries)  # _env_host_entries 返回 (name, target) 元组
        s.check("host list entries", names == ["prod", "stage"]
                and any(e[1] == "deploy@1.2.3.5:2222" for e in entries), repr(entries))
        s.check("host list 不回显凭据", all("password" not in e[1].lower()
                                           for e in entries))
        # remove：删别名 + 专属凭据行
        rc = quiet(lambda: hm.cmd_host_remove(ns(name="prod")))
        txt = open(env_p, encoding="utf-8").read()
        s.check("host remove 删别名及凭据", rc == 0
                and "PYAISSH_HOST_PROD" not in txt and "PYAISSH_HOST_STAGE=" in txt, txt)
        # remove 不存在 -> bad_args 明确（返回 2）
        rc = quiet(lambda: hm.cmd_host_remove(ns(name="nope")))
        s.check("host remove 不存在报错", rc == 2)
        # add 非法名 -> 2
        rc = quiet(lambda: hm.cmd_host_add(ns(name="bad name!", host_target="root@1.2.3.4", password=None, key=None)))
        s.check("host add 非法名报错", rc == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 测试集 4：live --sudo 提权 12 例
# ============================================================
# live 凭据预检（2026-09-08 事故教训：凭据 env 配错时整集用例会对真实
# 服务器连刷认证失败——sudo 集曾一次连发 5 次 tester 失败登录触发 fail2ban
# 封出口 IP 2h，靠跳板才救回。预检失败立即 SKIP，绝不用失败用例刷服务器）
# ============================================================

def _preflight(user, login_pw, tag):
    """开跑前单次连通/凭据验证：跑 test 一次；失败打印 SKIP 原因并返回 False。
    预检本身只产生 1 次失败（若凭据错），远低于 fail2ban 5 次阈值。"""
    tgt = "%s@%s" % (user, _live_host().split("@")[-1])
    rc, j, _ = _live_run(["test", tgt], login_pw=login_pw, timeout=60)
    if j and j.get("ok") is True:
        return True
    reason = (j or {}).get("error") or ("rc=%s" % rc)
    print("SKIP %s 预检失败（%s: %s）——先修 env 凭据/网络再跑；"
          "不要在凭据错时硬跑整集（会对服务器连刷认证失败触发 fail2ban 封 IP）"
          % (tag, reason, (j or {}).get("message", "")))
    return False


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
    # 预检：tester 与 tester_np 密码都验证通过才开跑（任一错 → SKIP 不刷失败）
    if not _preflight(sudo_user, pw, "sudo 集 tester"):
        return None
    if not _preflight(np_user, os.environ.get("PYAISSH_TEST_SUDO_NP_PASSWORD"),
                      "sudo 集 tester_np"):
        return None
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
    if not _preflight("root", os.environ.get("PYAISSH_TEST_PASSWORD"), "exec 集 root"):
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

    # v2.1 #1：命令失败 + stderr 未提取 -> stderr 通道直接给截断尾巴（不多跑一轮）
    p = _live_sub(["exec", tgt, "--cmd", "sh -c 'echo v; echo boom_direct >&2; exit 1'",
                   "--field", "stdout"])
    s.check("field 失败给 stderr 尾巴", "boom_direct" in p.stderr
            and p.stdout.strip() == "v")
    # v2.1 #1：命令成功但 stderr 非空 -> 保持提示（不塞内容，内容非报错）
    p = _live_sub(["exec", tgt, "--cmd", "sh -c 'echo v; echo warn_line >&2'",
                   "--field", "stdout"])
    s.check("field 成功 stderr 仅提示", "结果含非空 stderr" in p.stderr
            and "warn_line" not in p.stderr)

    # v2.1 #5：--progress 心跳（长任务静默时 stderr 打[PROGRESS]，AI 知进程活着）
    p = _live_sub(["exec", tgt, "--cmd", "sleep 3", "--progress", "1"], timeout=60)
    s.check("--progress 心跳", "[PROGRESS] 仍在运行" in p.stderr
            and p.returncode == 0)
    # v2.1.1：心跳是显式请求的信号——--field stdout 下不被噪音静音吞掉
    #   （长任务 + --field stdout 恰是最需要心跳的场景，实测教训）
    p = _live_sub(["exec", tgt, "--cmd", "sleep 2", "--progress", "1", "--field", "stdout"],
                  timeout=60)
    s.check("--field 下 --progress 心跳可见", "[PROGRESS] 仍在运行" in p.stderr
            and "[SSH]" not in p.stderr and p.stdout.strip() == "")

    # v2.2.4 CRLF 归一：默认归一命令文本（内联/文件/stdin），--keep-crlf 保留，结果回传计数
    import tempfile as _tf
    _cdir = _tf.mkdtemp(prefix="pyaissh_crlf_")
    _probe = 'v=1\necho "A[$v]B"\n'          # CR 若到达 bash，v 里会混进 \r
    _crlf = _probe.replace("\n", "\r\n")
    _f_crlf = os.path.join(_cdir, "probe_crlf.sh")
    _f_lf = os.path.join(_cdir, "probe_lf.sh")
    io.open(_f_crlf, "w", encoding="utf-8", newline="").write(_crlf)
    io.open(_f_lf, "w", encoding="utf-8", newline="").write(_probe)
    try:
        # ① 内联 --cmd（v2.2.4 修的真坑：此前 CR 直达 bash）
        rc, j, _ = _live_run(["exec", tgt, "--cmd", _crlf], timeout=60)
        s.check("内联 --cmd 的 CRLF 被归一 + 回传 crlf_normalized=2",
                bool(j) and j.get("stdout") == "A[1]B\n" and j.get("crlf_normalized") == 2,
                repr(j)[:200])
        s.check("归一有 warnings 说明且给出 --keep-crlf 逃生阀",
                bool(j) and any("归一为 LF" in w and "--keep-crlf" in w
                                for w in (j.get("warnings") or [])), repr(j.get("warnings"))[:160])
        # ② --keep-crlf：保留原样（CR 真的到达 bash）
        rc, j, _ = _live_run(["exec", tgt, "--cmd", _crlf, "--keep-crlf"], timeout=60)
        s.check("--keep-crlf 保留 CR（v 值含 \\r）且不回传计数",
                bool(j) and "A[1\r]B" in (j.get("stdout") or "")
                and not j.get("crlf_normalized"), repr(j.get("stdout"))[:120])
        # ③ cmd-file / stdin：行为不回退（原本隐式归一）且现在带计数
        rc, j, _ = _live_run(["exec", tgt, "--cmd-file", _f_crlf], timeout=60)
        s.check("--cmd-file 的 CRLF 仍归一（行为不回退）+ 计数",
                bool(j) and j.get("stdout") == "A[1]B\n" and j.get("crlf_normalized") == 2,
                repr(j)[:180])
        rc, j, _ = _live_sub_bytes(["exec", tgt, "--cmd-file", "-"], _crlf.encode("utf-8"),
                                   timeout=60)
        s.check("stdin(-) 的 CRLF 仍归一 + 计数（stdin 走字节）",
                bool(j) and j.get("stdout") == "A[1]B\n" and j.get("crlf_normalized") == 2,
                repr(j)[:180])
        rc, j, _ = _live_run(["exec", tgt, "--cmd-file", _f_crlf, "--keep-crlf"], timeout=60)
        s.check("--cmd-file + --keep-crlf 保留 CR",
                bool(j) and "A[1\r]B" in (j.get("stdout") or ""), repr(j.get("stdout"))[:120])
        # ④ 纯 LF 文件零改动（不能凭空多出字段/警告）
        rc, j, _ = _live_run(["exec", tgt, "--cmd-file", _f_lf], timeout=60)
        s.check("纯 LF 文件零改动（无 crlf_normalized、无 CRLF 警告）",
                bool(j) and j.get("stdout") == "A[1]B\n" and "crlf_normalized" not in j
                and not any("CRLF" in w for w in (j.get("warnings") or [])), repr(j)[:180])
        # ⑤ heredoc 数据面：默认落盘 LF；--keep-crlf 落盘保留 CRLF（逃生阀有效）
        _hd = ("cat > /tmp/pyaissh_hd_probe.txt <<'EOF'\nline1\nline2\nEOF\n"
               "grep -c $'\\r' /tmp/pyaissh_hd_probe.txt || true\n")
        _f_hd = os.path.join(_cdir, "hd_crlf.sh")
        io.open(_f_hd, "w", encoding="utf-8", newline="").write(_hd.replace("\n", "\r\n"))
        rc, j, _ = _live_run(["exec", tgt, "--cmd-file", _f_hd], timeout=60)
        s.check("默认：heredoc 落盘的文件不带 CR（数据面顺带修好）",
                bool(j) and (j.get("stdout") or "").strip() == "0", repr(j.get("stdout"))[:80])
        rc, j, _ = _live_run(["exec", tgt, "--cmd-file", _f_hd, "--keep-crlf"], timeout=60)
        s.check("--keep-crlf：heredoc 落盘保留 CRLF（要数据面 CRLF 时可用）",
                bool(j) and (j.get("stdout") or "").strip() == "2", repr(j.get("stdout"))[:80])
        # ⑥ 单行孤立 CR
        rc, j, _ = _live_run(["exec", tgt, "--cmd", "echo X\r"], timeout=60)
        s.check("行尾孤立 CR 被去掉", bool(j) and (j.get("stdout") or "").strip() == "X",
                repr(j.get("stdout"))[:80])
        # ⑦ detach：job.sh 落盘不带 CR
        rc, jd, _ = _live_run(["exec", tgt, "--detach", "--cmd", _crlf], timeout=90)
        if jd and jd.get("job_id"):
            rc, jg, _ = _live_run(["exec", tgt, "--cmd",
                                   "grep -c $'\\r' %s || true" % jd.get("cmd_written_to")],
                                  timeout=60)
            s.check("detach：计数回传 + job.sh 落盘无 CR",
                    jd.get("crlf_normalized") == 2
                    and (jg.get("stdout") or "").strip() == "0", repr(jd)[:120])
            _live_run(["log", tgt, "--job-id", jd["job_id"], "--cleanup"], timeout=60)
        else:
            s.check("detach：计数回传 + job.sh 落盘无 CR", False, "detach 未返回 job_id")
    finally:
        import shutil as _sh
        _sh.rmtree(_cdir, ignore_errors=True)
        _live_run(["exec", tgt, "--cmd", "rm -f /tmp/pyaissh_hd_probe.txt"], timeout=60)

    # v2.2 后台作业（--detach + log）：长任务不受宿主调用上限，增量读不重复
    rc, j, _ = _live_run(["exec", tgt, "--detach", "--cmd", "sleep 2; echo detach_hi; exit 5"],
                         timeout=90)
    jid = (j or {}).get("job_id")
    s.check("detach 启动返回 job_id/log/rc/pid_file",
            bool(j) and j.get("detached") is True and bool(jid)
            and j.get("log", "").endswith("/job.log") and j.get("rc", "").endswith("/job.rc")
            and j.get("pid_file", "").endswith("/job.pid"), repr(j)[:200])
    if jid:
        rc, j2, _ = _live_run(["log", tgt, "--job-id", jid, "--wait-rc", "30"], timeout=90)
        s.check("log --wait-rc 拿退出码（载荷字段 stdout，无 content）",
                bool(j2) and j2.get("status") == "finished" and j2.get("exit_code") == 5
                and "detach_hi" in j2.get("stdout", "") and j2.get("stream") == "stdout+stderr"
                and "content" not in j2, repr(j2)[:200])
        # v2.2.1 权限从严：目录 0700、job.sh 0600、job.log/job.rc 0600、run.sh 0700
        # （作业已结束，job.rc 一定存在；用 _live_sub 取 --field 的裸 stdout）
        p = _live_sub(["exec", tgt, "--cmd",
                       "stat -c '%%a %%n' %s %s %s %s %s %s"
                       % (j.get("job_dir"), j.get("cmd_written_to"),
                          j.get("job_dir", "") + "/run.sh", j.get("log"), j.get("rc"),
                          j.get("job_dir", "") + "/job.pid"),
                       "--field", "stdout"], timeout=60)
        modes = {}
        for ln in (p.stdout or "").splitlines():
            parts = ln.split()
            if len(parts) == 2:
                modes[parts[1].rsplit("/", 1)[-1]] = parts[0]
        dname = j.get("job_dir", "").rsplit("/", 1)[-1]
        s.check("远端作业权限 0700/0600 从严",
                modes.get(dname) == "700" and modes.get("job.sh") == "600"
                and modes.get("run.sh") == "700" and modes.get("job.log") == "600"
                and modes.get("job.rc") == "600" and modes.get("job.pid") == "600",
                repr(modes))
        # 增量读：--offset 0 → next_offset 递增，两段拼起来是全文
        rc, j3, _ = _live_run(["log", tgt, "--job-id", jid, "--offset", "0"], timeout=60)
        s.check("log --offset 增量读", bool(j3) and j3.get("next_offset", 0) > 0
                and isinstance(j3.get("has_more"), bool), repr(j3)[:160])
        rc, j4, _ = _live_run(["log", tgt, "--job-id", jid, "--offset",
                               str(j3.get("next_offset", 0))], timeout=60)
        s.check("log 续读不重复", bool(j4)
                and (j3.get("stdout", "") + j4.get("stdout", "")).count("detach_hi") == 1,
                "first=%r second=%r" % (j3.get("stdout"), j4.get("stdout")))
        rc, j5, _ = _live_run(["log", tgt, "--job-id", jid, "--cleanup"], timeout=60)
        s.check("log --cleanup 清理", bool(j5) and j5.get("cleaned") is True)
        rc, j6, _ = _live_run(["log", tgt, "--job-id", jid], timeout=60)
        s.check("清理后读报 job_not_found", bool(j6) and j6.get("error") == "job_not_found")
    rc, jl, _ = _live_run(["log", tgt, "--list"], timeout=60)
    s.check("log --list 可用", bool(jl) and jl.get("ok") is True
            and isinstance(jl.get("jobs"), list) and "job_dir" in jl, repr(jl)[:160])

    # B1（真机复现，v2.3.0 修）：log --wait-rc >30s 曾被 SFTP 看门狗误杀（30s 处断链，
    # 随后报"读不到日志文件"）——与 session 同病：轮询循环不刷新看门狗活动时间
    rc, jb1d, _ = _live_run(["exec", tgt, "--detach", "--cmd", "sleep 32; echo JOB32"], timeout=90)
    if jb1d and jb1d.get("job_id"):
        _t0 = time.time()
        rc, jb1, _ = _live_run(["log", tgt, "--job-id", jb1d["job_id"], "--wait-rc", "50"],
                               timeout=180)
        _dt = time.time() - _t0
        s.check("B1 log --wait-rc 50 等满 32s 不被看门狗误杀（finished + exit 0）",
                bool(jb1) and jb1.get("status") == "finished" and jb1.get("exit_code") == 0,
                "%.1fs status=%s exit=%s msg=%s" % (_dt, (jb1 or {}).get("status"),
                                                    (jb1 or {}).get("exit_code"),
                                                    ((jb1 or {}).get("message") or "")[:40]))
        s.check("B1 刚结束的作业不误判 dead（rc 落盘宽限）",
                bool(jb1) and jb1.get("status") != "dead", repr(jb1)[:120])
        _live_run(["log", tgt, "--job-id", jb1d["job_id"], "--cleanup"], timeout=60)
    else:
        s.check("B1 log --wait-rc 50 等满 32s 不被看门狗误杀（finished + exit 0）", False,
                "detach 未返回 job_id: %r" % (jb1d,))

    # v2.2.1 kill：长作业整组停掉 → 状态收敛为 dead（不再永久 running）
    rc, jk, _ = _live_run(["exec", tgt, "--detach", "--cmd", "sleep 120"], timeout=90)
    kjob = (jk or {}).get("job_id")
    if kjob:
        # v2.2.2 守卫：运行中 --cleanup 必须被拒（否则自断追踪 + 进程仍在跑）
        rc, jc, _ = _live_run(["log", tgt, "--job-id", kjob, "--cleanup"], timeout=60)
        s.check("运行中 --cleanup 被拒（job_running, exit 2）",
                bool(jc) and jc.get("error") == "job_running" and rc == 2
                and jc.get("pid"), repr(jc)[:200])
        rc, still, _ = _live_run(["log", tgt, "--job-id", kjob], timeout=60)
        s.check("拒绝后目录仍在（可继续追踪）", bool(still) and still.get("status") == "running"
                and bool(still.get("pid")), repr(still)[:160])
        # v2.2.2：--force 放弃追踪（允许删，但要留痕）
        rc, jf, _ = _live_run(["log", tgt, "--job-id", kjob, "--cleanup", "--force"], timeout=60)
        s.check("--cleanup --force 强制清理且留痕",
                bool(jf) and jf.get("cleaned") is True and jf.get("forced_cleanup") is True
                and any("仍在运行" in w for w in (jf.get("warnings") or [])), repr(jf)[:200])
        # v2.2.3：force 之后 job.pid 已删、--kill 不可用 → 唯一的恢复路径必须**直接给命令**
        # （pid 就是 setsid 组长，负号=整组；省掉一次 pgrep）
        gk = jf.get("group_kill") if jf else None
        s.check("force 后给出可执行整组杀命令（group_kill/warning/next_action 三处一致）",
                bool(jf) and gk == "kill -9 -%s" % jf.get("pid")
                and gk in " ".join(jf.get("warnings") or [])
                and gk in (jf.get("next_action") or "")
                and "整组" in (jf.get("next_action") or ""), repr(jf)[:260])
        s.check("force 后 next_action 不再误导为「继续增量读」",
                bool(jf) and "--offset" not in (jf.get("next_action") or ""),
                repr(jf.get("next_action"))[:160])
        # 该作业的进程此刻仍在跑（正是 force 的代价）
        p = _live_sub(["exec", tgt, "--cmd",
                       "pgrep -af '[s]leep 120' >/dev/null && echo ORPHAN_ALIVE "
                       "|| echo NO_ORPHAN", "--field", "stdout"], timeout=60)
        s.check("force 清理后进程确实仍在跑（代价可见）", "ORPHAN_ALIVE" in (p.stdout or ""),
                repr(p.stdout)[:120])
        # 直接执行结果里给的那条命令（不 pgrep）——文档路径必须真能一次清整组
        p = _live_sub(["exec", tgt, "--cmd",
                       "%s; sleep 0.5; pgrep -af '[s]leep 120' >/dev/null && echo STILL_ALIVE "
                       "|| echo GROUP_KILLED" % (gk or "echo NO_CMD"), "--field", "stdout"],
                      timeout=60)
        s.check("照 group_kill 执行即一次清整组（无需 pgrep）",
                "GROUP_KILLED" in (p.stdout or ""), repr(p.stdout)[:120])
        # 对照：正 pid（非整组）只杀组长，子进程被 reparent 成孤儿——证明负号必要
        rc, jc2, _ = _live_run(["exec", tgt, "--detach", "--cmd",
                                "sleep 120 # pyaissh_orphan_control"], timeout=90)
        cjob = (jc2 or {}).get("job_id")
        if cjob:
            p = _live_sub(["exec", tgt, "--cmd",
                           "kill -9 %s; sleep 0.5; pgrep -af '[s]leep 120' >/dev/null "
                           "&& echo ORPHAN_LEFT || echo NO_ORPHAN" % jc2.get("pid"),
                           "--field", "stdout"], timeout=60)
            s.check("对照：正 pid 杀只留孤儿（负号不可省）", "ORPHAN_LEFT" in (p.stdout or ""),
                    repr(p.stdout)[:120])
            p = _live_sub(["exec", tgt, "--cmd",
                           "pkill -f '[s]leep 120'; sleep 0.3; "
                           "pgrep -af '[s]leep 120' >/dev/null && echo STILL_ALIVE "
                           "|| echo KILLED", "--field", "stdout"], timeout=60)
            s.check("外部 pkill 收尾（测试环境不留孤儿）", "KILLED" in (p.stdout or ""),
                    repr(p.stdout)[:120])
            _live_run(["log", tgt, "--job-id", cjob, "--cleanup"], timeout=60)
    else:
        s.check("运行中 --cleanup 被拒（job_running, exit 2）", False, "detach 未返回 job_id")

    # v2.2.2 kill 整组：--kill 必须连子进程一起杀（否则 job.sh/sleep 被 reparent 成孤儿）
    rc, jg, _ = _live_run(["exec", tgt, "--detach", "--cmd",
                           "sleep 300 # pyaissh_orphan_probe"], timeout=90)
    gjob = (jg or {}).get("job_id")
    if gjob:
        rc, gk, _ = _live_run(["log", tgt, "--job-id", gjob, "--kill", "--wait-rc", "30"],
                              timeout=120)
        s.check("--kill 收敛 dead（整组停掉）",
                bool(gk) and gk.get("status") == "dead" and gk.get("exit_code") is None
                and gk.get("kill", {}).get("ok") is True and bool(gk.get("pid")),
                repr(gk)[:220])
        s.check("kill 后 wait-rc 快速收敛（未等满 30s）",
                bool(gk) and gk.get("waited_ms", 99999) < 20000, repr(gk)[:160])
        s.check("dead 状态带 hint 指引", bool(gk) and "job.rc" in (gk.get("hint") or ""),
                repr(gk.get("hint"))[:120])
        p = _live_sub(["exec", tgt, "--cmd",
                       "pgrep -af '[s]leep 300' >/dev/null && echo ORPHAN_ALIVE "
                       "|| echo NO_ORPHAN", "--field", "stdout"], timeout=60)
        s.check("--kill 杀掉整组：无孤儿进程", "NO_ORPHAN" in (p.stdout or ""),
                repr(p.stdout)[:120])
        _live_run(["log", tgt, "--job-id", gjob, "--cleanup"], timeout=60)
    else:
        s.check("--kill 杀掉整组：无孤儿进程", False, "detach 未返回 job_id")

    # v2.2.2 参数校验：--force 只配合 --cleanup
    rc, jb5, _ = _live_run(["log", tgt, "--job-id", "x", "--force"], timeout=60)
    s.check("--force 缺 --cleanup → bad_args", bool(jb5) and jb5.get("error") == "bad_args"
            and rc == 2, repr(jb5)[:160])

    # v2.2.1 尾部截断：truncated 时给 --offset 0 顺序读的 next_action
    rc, jt, _ = _live_run(["exec", tgt, "--detach", "--cmd",
                           "seq 1 3000"], timeout=90)
    tjob = (jt or {}).get("job_id")
    if tjob:
        _live_run(["log", tgt, "--job-id", tjob, "--wait-rc", "20"], timeout=60)
        # 触发尾读截断：回传内容必须大于 --max-output（--lines 5000 会把全文纳入，
        # 再被 --max-output 2000 截中段）——小 --lines 时内容本身就小，不会截断
        rc, tt, _ = _live_run(["log", tgt, "--job-id", tjob, "--lines", "5000",
                               "--max-output", "2000"], timeout=60)
        s.check("尾读截断给 --offset 0 补齐提示",
                bool(tt) and tt.get("truncated") is True and tt.get("omitted_bytes", 0) > 0
                and "--offset 0" in (tt.get("next_action") or ""), repr(tt)[:220])
        rc, t2, _ = _live_run(["log", tgt, "--job-id", tjob, "--offset", "0",
                               "--max-output", "65536"], timeout=60)
        s.check("--offset 0 顺序读拿到全文尾部标记", bool(t2) and "3000" in t2.get("stdout", ""),
                repr(t2)[:160])
        _live_run(["log", tgt, "--job-id", tjob, "--cleanup"], timeout=60)
    else:
        s.check("尾读截断给 --offset 0 补齐提示", False, "detach 未返回 job_id")

    # 互斥与校验
    rc, jb, _ = _live_run(["exec", tgt, "--detach", "--sudo", "--cmd", "id"], timeout=60)
    s.check("detach+sudo 互斥", bool(jb) and jb.get("error") == "bad_args" and rc == 2)
    rc, jb2, _ = _live_run(["log", tgt], timeout=60)
    s.check("log 缺 --job-id", bool(jb2) and jb2.get("error") == "bad_args" and rc == 2)
    rc, jb3, _ = _live_run(["log", tgt, "--job-id", "../etc"], timeout=60)
    s.check("log job-id 穿越拦截", bool(jb3) and jb3.get("error") == "bad_args" and rc == 2)
    rc, jb4, _ = _live_run(["log", tgt, "--kill"], timeout=60)
    s.check("--kill 缺 job-id 拦截", bool(jb4) and jb4.get("error") == "bad_args" and rc == 2)


# ============================================================
# 测试集 5：live 传输往返
# ============================================================

def suite_live_transfer(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    if not _preflight("root", os.environ.get("PYAISSH_TEST_PASSWORD"), "transfer 集 root"):
        return None
    tgt = _live_host()
    local = os.path.join(_REPO, "tests", "_tmp_xfer.bin")
    dl = os.path.join(_REPO, "tests", "_tmp_xfer_dl.bin")
    part = dl + ".part"
    with open(local, "wb") as f:
        f.write(os.urandom(300000))
    # T1 默认上传往返
    rc, j, _ = _live_run(["upload", tgt, "--local", local, "--remote", "/tmp/_t_xfer.bin"],
                         timeout=300)
    s.check("upload 300KB", j and j.get("ok") is True
            and j.get("bytes_transferred") == 300000)
    # T2 默认下载字节一致
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = _live_run(["download", tgt, "--remote", "/tmp/_t_xfer.bin", "--local", dl],
                         timeout=300)
    ok_dl = j and j.get("ok") is True and os.path.exists(dl) \
        and open(dl, "rb").read() == open(local, "rb").read()
    s.check("download 往返字节一致", ok_dl)
    # T3 download --parallel 4（并行分片往返 + parallel_used 回显）
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = _live_run(["download", tgt, "--remote", "/tmp/_t_xfer.bin", "--local", dl,
                          "--parallel", "4"], timeout=300)
    ok_par = j and j.get("ok") is True and j.get("parallel_used") == 4 \
        and os.path.exists(dl) and open(dl, "rb").read() == open(local, "rb").read()
    s.check("download --parallel 4", ok_par)
    # T4 upload --parallel 4（并行上传往返：bytes + parallel_used + 远端大小一致）
    rc, j, _ = _live_run(["upload", tgt, "--local", local, "--remote", "/tmp/_t_xfer_par.bin",
                          "--parallel", "4"], timeout=300)
    ok_up = j and j.get("ok") is True and j.get("bytes_transferred") == 300000 \
        and j.get("parallel_used") == 4
    if ok_up:
        rc, j2, _ = _live_run(["exec", tgt, "--cmd",
                               "stat -c %s /tmp/_t_xfer_par.bin 2>/dev/null || wc -c < /tmp/_t_xfer_par.bin"])
        ok_up = j2 and j2.get("exit_success") and "300000" in j2.get("stdout", "")
    s.check("upload --parallel 4", ok_up)
    # T5 download --resume 断点续传往返：伪造 local.part(前 40%) → 应从断点续传完成
    #    （resume 模式续传点固定名 local+".part"，见 _sftp_get_resume 语义）
    src_bytes = open(local, "rb").read()
    head = src_bytes[:len(src_bytes) * 2 // 5]
    if os.path.exists(part):
        os.remove(part)
    with open(part, "wb") as f:
        f.write(head)
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, pstderr = _live_run(["download", tgt, "--remote", "/tmp/_t_xfer.bin",
                                "--local", dl, "--resume"], timeout=300)
    ok_res = j and j.get("ok") is True and os.path.exists(dl) \
        and open(dl, "rb").read() == src_bytes
    s.check("download --resume 往返字节一致", ok_res)
    if ok_res:
        # 续传证据：stderr 出现 [RESUME] 续传日志（JSON bytes_transferred = 文件全量
        # 非增量，语义见 cmd_download；续传事实以日志佐证）
        s.check("download --resume 续传日志", "RESUME" in pstderr
                or "从断点继续" in pstderr or "续传" in pstderr,
                "stderr 无续传标记: %r" % pstderr[:160])
    # T6 upload --exclude 目录排除（v2.1）：本地树含 node_modules/.git → 不上传
    deploy_dir = os.path.join(_REPO, "tests", "_tmp_deploy")
    os.makedirs(os.path.join(deploy_dir, "node_modules", "esbuild"), exist_ok=True)
    os.makedirs(os.path.join(deploy_dir, ".git"), exist_ok=True)
    os.makedirs(os.path.join(deploy_dir, "src"), exist_ok=True)
    with open(os.path.join(deploy_dir, "app.js"), "wb") as f:
        f.write(os.urandom(60000))
    with open(os.path.join(deploy_dir, "node_modules", "esbuild", "bin.exe"), "wb") as f:
        f.write(os.urandom(110000))
    with open(os.path.join(deploy_dir, ".git", "config"), "wb") as f:
        f.write(b"[core] test = 1\n")
    with open(os.path.join(deploy_dir, "src", "main.py"), "wb") as f:
        f.write(b"print('hi')\n")
    expect_bytes = (os.path.getsize(os.path.join(deploy_dir, "app.js"))
                    + os.path.getsize(os.path.join(deploy_dir, "src", "main.py")))
    rc, j, pstderr = _live_run(["upload", tgt, "--local", deploy_dir,
                                "--remote", "/tmp/_t_deploy",
                                "--exclude", "node_modules,.git"], timeout=300)
    # 上传只含 app.js + src/main.py；node_modules(110000B)/.git 被排除
    ok_ex = j and j.get("ok") is True and j.get("bytes_transferred") == expect_bytes
    if ok_ex:
        rc, j2, _ = _live_run(["exec", tgt, "--cmd",
                               "test ! -d /tmp/_t_deploy/node_modules && "
                               "test ! -d /tmp/_t_deploy/.git && "
                               "test -f /tmp/_t_deploy/app.js && echo EXCL_OK"])
        ok_ex = j2 and "EXCL_OK" in j2.get("stdout", "")
    s.check("upload --exclude 排除生效", ok_ex)
    import shutil
    shutil.rmtree(deploy_dir, ignore_errors=True)
    # 远端 + 本地清理
    _live_run(["exec", tgt, "--cmd",
               "rm -rf /tmp/_t_xfer.bin /tmp/_t_xfer_par.bin /tmp/_t_deploy"])
    os.remove(local)
    for p in (dl, part):
        if os.path.exists(p):
            os.remove(p)

    # v2.3.0：PYAISSH_SFTP_IO_TIMEOUT（看门狗窗口逃生阀）不破坏正常路径
    os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = "90"
    try:
        rc, jw, _ = _live_run(["ls", tgt, "--path", "/tmp"], timeout=60)
        s.check("设 PYAISSH_SFTP_IO_TIMEOUT=90 后 ls 正常（不走坏路径）",
                bool(jw) and jw.get("ok") is True, repr(jw)[:140])
    finally:
        os.environ.pop("PYAISSH_SFTP_IO_TIMEOUT", None)
    os.environ["PYAISSH_SFTP_IO_TIMEOUT"] = "abc"      # 非法值只 WARN，不应影响功能
    try:
        rc, jb, _ = _live_run(["ls", tgt, "--path", "/tmp"], timeout=60)
        s.check("非法 PYAISSH_SFTP_IO_TIMEOUT 只回落默认、功能不受影响",
                bool(jb) and jb.get("ok") is True, repr(jb)[:140])
    finally:
        os.environ.pop("PYAISSH_SFTP_IO_TIMEOUT", None)


# ============================================================
# 测试集 8：live_session —— 常驻会话（v2.3，真机）
#   真 PTY + 逐条喂命令 + 状态保留 + 每条退出码 + ctrl-c 中断执行中的命令 + keys 应答提示
#   开发期实测出的三个坑都在这里有护栏：
#     ① 哨兵若单列一行会被命令里的 read 吃掉 → 必须 { ...; }; echo 哨兵（同一行解析）
#     ② kill 若只删目录/按 sid 杀 → 会话进程仍活（script 的子 shell 自己 setsid）→ 用进程树闭包
#     ③ ctrl-c 只发 SIGINT 杀不死（setsid+nohup 起，SIGINT 处置被继承）→ 自动升级 TERM
# ============================================================

def _field(txt, key):
    """从 `key=value` 多行文本里取值（live 断言常用）。"""
    for _ln in (txt or "").splitlines():
        if _ln.startswith(key + "="):
            return _ln.split("=", 1)[1].strip()
    return ""

def suite_live_session(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    s.check("start：返回 pid/pty/ready + 0700 权限",
            rc == 0 and bool(j) and j.get("ok") and isinstance(j.get("pid"), int)
            and j.get("pty") is True and j.get("ready") is True
            and j.get("permissions", {}).get("dir") == "0700", repr(j)[:200])

    rc, _js1, _ = _live_run(["session", "send", tgt, "--name", name, "--cmd",
                            "cd /var/log; pwd"], timeout=60)
    rc, j, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "20",
                          "--token", (_js1 or {}).get("token")], timeout=90)
    s.check("逐条喂命令：退出码 0 + 输出 /var/log",
            bool(j) and j.get("exit_code") == 0 and "/var/log" in (j.get("stdout") or ""),
            repr(j)[:160])
    rc, _js2, _ = _live_run(["session", "send", tgt, "--name", name, "--cmd",
                            "this_cmd_is_missing"], timeout=60)
    rc, j, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "20",
                          "--token", (_js2 or {}).get("token")], timeout=90)
    s.check("错误命令独立退出码 127，会话不死", bool(j) and j.get("exit_code") == 127,
            repr(j.get("exit_code")))
    rc, _js3, _ = _live_run(["session", "send", tgt, "--name", name, "--cmd",
                            "echo PWD=$(pwd)"], timeout=60)
    rc, j, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "20",
                          "--token", (_js3 or {}).get("token")], timeout=90)
    s.check("状态保留：错误命令后 cwd 仍是 /var/log",
            bool(j) and "PWD=/var/log" in (j.get("stdout") or ""), repr(j.get("stdout"))[:120])

    # ctrl-c：中断执行中的命令，会话与状态都保住
    rc, _js4, _ = _live_run(["session", "send", tgt, "--name", name, "--cmd", "sleep 300"],
                           timeout=60)
    rc, jr, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "2",
                          "--token", (_js4 or {}).get("token")], timeout=60)
    s.check("长命令 status=running", bool(jr) and jr.get("status") == "running",
            repr(jr.get("status")))
    rc, jc, _ = _live_run(["session", "ctrl-c", tgt, "--name", name], timeout=90)
    s.check("ctrl-c 发出信号（signaled_count>=1）",
            bool(jc) and jc.get("ok") and (jc.get("signaled_count") or 0) >= 1, repr(jc)[:180])
    rc, j2, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "15"], timeout=60)
    s.check("被中断命令收敛（有退出码，非 None）",
            bool(j2) and j2.get("exit_code") is not None, repr(j2.get("exit_code")))
    _live_run(["session", "send", tgt, "--name", name, "--cmd", "echo ALIVE; pwd"], timeout=60)
    rc, j3, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "20"], timeout=90)
    s.check("ctrl-c 后会话存活且状态保留",
            bool(j3) and "ALIVE" in (j3.get("stdout") or "")
            and "/var/log" in (j3.get("stdout") or ""), repr(j3.get("stdout"))[:140])

    # run：send + 等结果合成一次调用（v2.3.0 的"一步一调用"）
    rc, jr0, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "cd /tmp && pwd"],
                           timeout=90)
    s.check("session run 一次调用拿退出码+输出",
            bool(jr0) and jr0.get("status") == "done" and jr0.get("exit_code") == 0
            and (jr0.get("stdout") or "").strip() == "/tmp", repr(jr0)[:180])
    rc, jr1, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "echo RUN2"],
                           timeout=90)
    s.check("run 之间状态保留（每条仍一次调用）",
            bool(jr1) and jr1.get("exit_code") == 0 and "RUN2" in (jr1.get("stdout") or ""),
            repr(jr1.get("stdout"))[:80])
    rc, jr2, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "sleep 300",
                            "--wait-rc", "2"], timeout=90)
    s.check("run --wait-rc 超时 → status=running 且保留 token（可续等）",
            bool(jr2) and jr2.get("status") == "running" and jr2.get("exit_code") is None
            and re.match(r"^[0-9a-f]{4,16}$", jr2.get("token") or ""), repr(jr2)[:160])
    _live_run(["session", "ctrl-c", tgt, "--name", name], timeout=90)
    rc, jr3, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "15"], timeout=60)
    s.check("run 起的命令可被 ctrl-c 中断并收敛",
            bool(jr3) and jr3.get("exit_code") is not None, repr(jr3.get("exit_code")))
    rc, jr4, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "echo NOWAIT",
                            "--no-wait"], timeout=60)
    s.check("run --no-wait 只发送（running + token）",
            bool(jr4) and jr4.get("status") == "running" and bool(jr4.get("token")),
            repr(jr4)[:140])
    rc, jr5, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "15"], timeout=60)
    s.check("--no-wait 之后 read 能取到结果",
            bool(jr5) and "NOWAIT" in (jr5.get("stdout") or ""), repr(jr5.get("stdout"))[:80])

    # 人类式重试（用户描述的用法）：打错 → 报错 → **改对再发一遍同一条命令** → 成功，状态保留
    rc, he1, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd",
                            "cd /etc && export RF=hostname && ls -l /etc/hostnam"], timeout=60)
    s.check("打错的命令报错（exit_code!=0 且输出里有 No such file）",
            bool(he1) and he1.get("exit_code") not in (0, None)
            and "No such file" in (he1.get("stdout") or ""),
            "exit=%s out=%r" % ((he1 or {}).get("exit_code"), ((he1 or {}).get("stdout") or "")[:70]))
    rc, he2, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "cat $RF"], timeout=60)
    s.check("改对后重发同一条命令即成功（用的正是会话里 export 的变量）",
            bool(he2) and he2.get("exit_code") == 0 and (he2.get("stdout") or "").strip() != "",
            "exit=%s out=%r" % ((he2 or {}).get("exit_code"), ((he2 or {}).get("stdout") or "")[:60]))
    rc, he3, _ = _live_run(["session", "run", tgt, "--name", name, "--cmd", "pwd"], timeout=60)
    s.check("重试期间 cwd 保持不变（重发时上下文与上次一致）",
            bool(he3) and (he3.get("stdout") or "").strip() == "/etc",
            repr((he3 or {}).get("stdout"))[:40])

    # keys：应答交互提示（哨兵必须不被 read 吃掉——本套件的核心回归点）
    _live_run(["session", "send", tgt, "--name", name, "--cmd",
               'read -p "N? " X; echo GOT:$X'], timeout=60)
    time.sleep(1.0)
    rc, jk, _ = _live_run(["session", "keys", tgt, "--name", name, "--data", "hello_pty\\n"],
                          timeout=60)
    s.check("keys 写入成功", rc == 0 and bool(jk) and jk.get("bytes_sent") == 10, repr(jk)[:140])
    rc, j4, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "15"], timeout=60)
    s.check("交互提示被应答（GOT:hello_pty）且哨兵未被吃掉",
            bool(j4) and "GOT:hello_pty" in (j4.get("stdout") or "")
            and j4.get("exit_code") == 0, repr(j4.get("stdout"))[:140])

    # ANSI/CR 清洗
    rc, _js5, _ = _live_run(["session", "send", tgt, "--name", name, "--cmd",
                            "printf '\\033[31mRED\\033[0m\\n'"], timeout=60)
    rc, j5, _ = _live_run(["session", "read", tgt, "--name", name, "--wait-rc", "20",
                          "--token", (_js5 or {}).get("token")], timeout=60)
    s.check("默认剥离 ANSI（保留文本）",
            bool(j5) and "RED" in (j5.get("stdout") or "") and "\x1b" not in (j5.get("stdout") or ""),
            repr(j5.get("stdout"))[:100])

    # list
    rc, jl, _ = _live_run(["session", "list", tgt], timeout=60)
    rows = [(x.get("session"), x.get("status"), x.get("pty"))
            for x in (jl or {}).get("sessions", [])]
    s.check("list 含本会话且 running/pty", bool(jl) and any(
        r[0] == name and r[1] == "running" and r[2] for r in rows), repr(rows))
    _aged = [x for x in (jl or {}).get("sessions", []) if x.get("session") == name]
    s.check("list 给出 started_at/age_seconds（看得出会挂了多久）",
            bool(_aged) and isinstance(_aged[0].get("started_at"), int)
            and isinstance(_aged[0].get("age_seconds"), int)
            and 0 <= _aged[0]["age_seconds"] < 3600, repr(_aged[:1])[:200])

    # kill：进程树闭包（不只是删目录）
    _live_run(["session", "send", tgt, "--name", name, "--cmd", "sleep 200 &"], timeout=60)
    time.sleep(0.8)
    rc, jz, _ = _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    row = ((jz or {}).get("sessions") or [{}])[0]
    s.check("kill 扫到会话进程树（swept>=3）", bool(jz) and (row.get("swept") or 0) >= 3,
            repr(row)[:160])
    s.check("kill 后无残留、目录已清",
            bool(jz) and jz.get("remaining_total") == 0 and row.get("cleaned") is True,
            repr(jz)[:180])
    s.check("kill 报了自证的根（roots>=1）且 verified=true",
            bool(jz) and (row.get("roots") or 0) >= 1 and row.get("verified") is True,
            repr(row)[:200])
    rc, js, _ = _live_run(["exec", tgt, "--cmd", "pgrep -x script | wc -l"], timeout=60)
    s.check("远端无 script 残留（会话进程真被杀）",
            bool(js) and (js.get("stdout") or "").strip() == "0", repr(js.get("stdout")))
    rc, jsl, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("kill 后该会话不在 list", bool(jsl) and not any(
        x.get("session") == name for x in (jsl.get("sessions") or [])), repr(jsl)[:140])

    # ---- 残留护栏（v2.3.0 加固 R1：会话是 setsid+nohup 的常驻进程，不会自己退出）----
    # ① starter 被外力杀掉（模拟 OOM）：script/bash -i 会被 reparent →
    #    kill 必须靠 bash.pid / script 兜底清干净（旧版只按 sess.pid 做闭包 ⇒ 空集 ⇒ 误报清干净）
    _orph = name + "or"
    _live_run(["session", "start", tgt, "--name", _orph], timeout=90)
    _live_run(["session", "run", tgt, "--name", _orph, "--cmd", "sleep 300 &",
               "--wait-rc", "3"], timeout=60)
    rc, _jo, _ = _live_run(["exec", tgt, "--cmd",
                            "kill -9 $(cat /tmp/pyaissh-sessions/%s/sess.pid); sleep 0.5; "
                            "kill -0 $(cat /tmp/pyaissh-sessions/%s/bash.pid) 2>/dev/null "
                            "&& echo ORPHAN_ALIVE" % (_orph, _orph)], timeout=60)
    s.check("孤儿场景就绪（starter 已死、script/bash -i 仍在）",
            bool(_jo) and "ORPHAN_ALIVE" in (_jo.get("stdout") or ""), repr(_jo)[:160])
    rc, _jok, _ = _live_run(["session", "kill", tgt, "--name", _orph], timeout=120)
    _rowk = ((_jok or {}).get("sessions") or [{}])[0]
    s.check("R1 孤儿兜底：starter 已死也能清干净（swept>=2、remaining=0、verified）",
            bool(_jok) and (_rowk.get("swept") or 0) >= 2 and _jok.get("remaining_total") == 0
            and _rowk.get("verified") is True, repr(_rowk)[:200])
    rc, _jr1, _ = _live_run(["exec", tgt, "--cmd",
                             "ps -eo pid,args | grep -c '[p]yaissh-sessions/%s'" % _orph],
                            timeout=60)
    s.check("R1 孤儿场景后无残留进程", bool(_jr1) and (_jr1.get("stdout") or "").strip() == "0",
            repr(_jr1.get("stdout")))

    # ② 两个根都不可用（进程被杀 + pid 文件删掉）→ 不许无声宣称"清干净"，
    #    必须 roots=0 + verified=false + warning（附 ps 自查）
    _ghost = name + "gh"
    _live_run(["session", "start", tgt, "--name", _ghost], timeout=90)
    # 把三个自证根全部打掉（sess.pid / bash.pid / watch.pid 的进程）再删 pid 文件，
    # 才真正构成"无根"场景——否则空闲回收看门狗本身就是一个活的自证根（v2.3.0 起）
    _live_run(["exec", tgt, "--cmd",
               "for k in sess bash watch; do kill -9 $(cat /tmp/pyaissh-sessions/{g}/$k.pid) "
               "2>/dev/null; done; sleep 0.5; "
               "rm -f /tmp/pyaissh-sessions/{g}/sess.pid /tmp/pyaissh-sessions/{g}/bash.pid "
               "/tmp/pyaissh-sessions/{g}/watch.pid".format(g=_ghost)],
              timeout=60)
    rc, _jg, _ = _live_run(["session", "kill", tgt, "--name", _ghost], timeout=120)
    _rowg = ((_jg or {}).get("sessions") or [{}])[0]
    s.check("R1 无根时不误报：roots=0 + verified=false + warning(未获确认)",
            bool(_jg) and _rowg.get("roots") == 0 and _rowg.get("verified") is False
            and any("未获确认" in w for w in (_jg.get("warnings") or [])), repr(_jg)[:240])

    # ③ 会话不自退：放着不动 35s（超过看门狗窗口）后仍能跑命令
    _idle = name + "id"
    _live_run(["session", "start", tgt, "--name", _idle], timeout=90)
    time.sleep(_wait(2))
    rc, _ji, _ = _live_run(["session", "run", tgt, "--name", _idle, "--cmd", "echo IDLE_OK",
                            "--wait-rc", "10"], timeout=60)
    s.check("闲置 %ds 后会话仍可用（常驻不自退、无 idle 超时）" % _wait(2),
            bool(_ji) and _ji.get("exit_code") == 0 and "IDLE_OK" in (_ji.get("stdout") or ""),
            repr(_ji)[:160])

    # ④ R2 半行残留（本地在写命令半途断线）：tty 输入缓冲里留下没有换行的半行，
    #    下一次写入会与它串成一行 → 旧版哨兵永不出现（run 卡在 running、拿不到退出码）。
    #    加固后载荷以换行开头：先终结那半行，再执行新命令，退出码照常拿到。
    rc, _jk2, _ = _live_run(["session", "keys", tgt, "--name", _idle, "--data",
                             "echo PARTIAL_HALF"], timeout=60)
    time.sleep(0.5)
    rc, _jr2, _ = _live_run(["session", "run", tgt, "--name", _idle, "--cmd", "echo SECOND_OK",
                             "--wait-rc", "15"], timeout=90)
    _o2 = (_jr2 or {}).get("stdout") or ""
    s.check("R2 半行残留后下一条命令仍拿得到退出码（不再卡 running）",
            bool(_jr2) and _jr2.get("exit_code") == 0 and "SECOND_OK" in _o2,
            "exit=%s 输出=%r" % ((_jr2 or {}).get("exit_code"), _o2[:120]))
    _live_run(["session", "kill", tgt, "--name", _idle], timeout=60)

def suite_live_session_ttl(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    # ---- T：空闲回收（v2.3.0，用户设计：提示符空闲 + TTL 内无交互 ⇒ 自动回收）----
    # T1：--ttl 5 且完全不碰它（list 不算交互）→ 看门狗应在 ~15-30s 内回收进程与目录
    _ttl = name + "ttl"
    rc, _jt, _ = _live_run(["session", "start", tgt, "--name", _ttl, "--ttl", "5"], timeout=90)
    s.check("T1a start --ttl 5 回传 ttl_seconds=5",
            bool(_jt) and _jt.get("ok") is True and _jt.get("ttl_seconds") == 5,
            repr(_jt)[:200])
    time.sleep(_wait(2, 6))
    rc, _jl2, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("T1b 空闲 %ds 后会话已自动回收（list 里没了）" % _wait(2, 6),
            bool(_jl2) and not any(x.get("session") == _ttl
                                   for x in (_jl2.get("sessions") or [])), repr(_jl2)[:200])
    rc, _jd, _ = _live_run(["exec", tgt, "--cmd",
                            "ls -d /tmp/pyaissh-sessions/%s 2>/dev/null | wc -l; "
                            "pgrep -fc '[p]yaissh-sessions/%s' || true" % (_ttl, _ttl)],
                           timeout=60)
    s.check("T1c 回收后目录与进程都没了",
            bool(_jd) and ((_jd.get("stdout") or "").split() or [""])[0] == "0",
            repr((_jd or {}).get("stdout")))

    # T2：TTL 很小但**有命令在跑** → 不回收（构建/安装不会被误杀）
    _ttlr = name + "run"
    _live_run(["session", "start", tgt, "--name", _ttlr, "--ttl", "5"], timeout=90)
    _live_run(["session", "send", tgt, "--name", _ttlr, "--cmd",
               "sleep 25; echo TTLRUN_DONE"], timeout=60)
    time.sleep(_wait(1, 6))   # 跨过看门狗第一次检查：那会儿命令还在跑 ⇒ 必须续期
    rc, _jlr, _ = _live_run(["session", "list", tgt], timeout=60)
    _row = next((x for x in (_jlr or {}).get("sessions", []) if x.get("session") == _ttlr), None)
    s.check("T2a 命令在跑时不回收（跨过看门狗检查仍 running）",
            bool(_row) and _row.get("status") == "running", repr(_row)[:200])
    rc, _jout, _ = _live_run(["session", "read", tgt, "--name", _ttlr, "--wait-rc", "20"],
                             timeout=90)
    s.check("T2b 长命令跑完能拿到输出（没被空闲回收误杀）",
            bool(_jout) and "TTLRUN_DONE" in (_jout.get("stdout") or ""),
            "status=%s 尾=%r" % ((_jout or {}).get("status"), ((_jout or {}).get("stdout") or "")[-40:]))
    _live_run(["session", "kill", tgt, "--name", _ttlr], timeout=60)

    # T3：--attach（活着就接上、没有才新建）+ 不带 --attach 时的提示
    _at = name + "at"
    rc, _ja1, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60"], timeout=90)
    rc, _ja2, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60",
                             "--attach"], timeout=90)
    s.check("T3a --attach 接上旧会话（attached=true 且 pid 不变）",
            bool(_ja2) and _ja2.get("ok") is True and _ja2.get("attached") is True
            and _ja2.get("pid") == (_ja1 or {}).get("pid"),
            "attached=%s pid=%s/%s" % ((_ja2 or {}).get("attached"), (_ja2 or {}).get("pid"),
                                       (_ja1 or {}).get("pid")))
    rc, _je, _e = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60"], timeout=90)
    s.check("T3b 不带 --attach 撞名仍报 session_exists，但提示改为「直接继续用它」",
            bool(_je) and _je.get("error") == "session_exists"
            and "直接继续用它" in (_je.get("message") or ""),
            repr(_je)[:220])
    _live_run(["session", "kill", tgt, "--name", _at], timeout=60)
    rc, _ja3, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60",
                             "--attach"], timeout=90)
    s.check("T3c --attach 在会话不存在时新建（attached=false）",
            bool(_ja3) and _ja3.get("ok") is True and _ja3.get("attached") is False
            and isinstance(_ja3.get("pid"), int), repr(_ja3)[:200])
    _live_run(["session", "kill", tgt, "--name", _at], timeout=60)

def suite_live_session_watchdog(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    # ---- W：单进程看门狗（设计 C）的实机开销 + 防 spin 护栏 ----


    _wdc = name + "wd"
    rc, _jw, _ = _live_run(["session", "start", tgt, "--name", _wdc, "--ttl", "300"], timeout=90)
    _wp = (_jw or {}).get("pid")
    _cpu0 = None
    if _wp:
        rc, _jc0, _ = _live_run(["exec", tgt, "--cmd",
                                 "W=$(cat /tmp/pyaissh-sessions/%s/watch.pid); "
                                 "echo \"procs=$(pgrep -fc '/tmp/pyaissh-sessions/%s/[w]atch[.]sh')\"; "
                                 "echo \"kids=$(pgrep -P $W | wc -l)\"; "
                                 "echo \"rss=$(awk '/VmRSS/{print $2}' /proc/$W/status)\"; "
                                 "echo \"cpu=$(awk '{print $14+$15}' /proc/$W/stat)\"" % (_wdc, _wdc)],
                                timeout=60)
        _w0 = (_jc0 or {}).get("stdout") or ""
        time.sleep(_wait(2, 2))      # 2 个检查点
        rc, _jc1, _ = _live_run(["exec", tgt, "--cmd",
                                 "W=$(cat /tmp/pyaissh-sessions/%s/watch.pid); "
                                 "echo \"cpu=$(awk '{print $14+$15}' /proc/$W/stat)\"; "
                                 "echo \"beat=$(<//tmp/pyaissh-sessions/%s/beat)\"; "
                                 "echo \"now=$(date +%%s)\"" % (_wdc, _wdc)], timeout=60)
        _w1 = (_jc1 or {}).get("stdout") or ""

        _rss = _field(_w0, "rss")
        try:
            _dcpu = int(_field(_w1, "cpu") or 0) - int(_field(_w0, "cpu") or 0)
        except ValueError:
            _dcpu = -1
        s.check("W1 单进程看门狗：只有 1 个 watch 进程、没有 sleep 子进程",
                _field(_w0, "procs") == "1" and _field(_w0, "kids") == "0", repr(_w0)[:160])
        s.check("W1 RSS < 4 MB（比 sleep 版省 ~1.9 MB）",
                _rss.isdigit() and int(_rss) < 4000, "rss=%s KB" % _rss)
        s.check("W1 %d 秒（2 个检查点）CPU ≤ 2 jiffy（内建睡眠不烧 CPU）" % _wait(2, 2),
                0 <= _dcpu <= 2, "cpu_delta=%s jiffy" % _dcpu)
        s.check("W1 beat 里是 epoch 秒，且与远端 now 相差 < TTL（判闲靠内容）",
                _field(_w1, "beat").isdigit() and _field(_w1, "now").isdigit()
                and 0 <= int(_field(_w1, "now")) - int(_field(_w1, "beat")) < 300,
                repr(_w1)[:160])
        _live_run(["session", "kill", tgt, "--name", _wdc], timeout=60)

    # W2 防 spin 护栏：把真实生成的看门狗脚本的 fd 改成 /dev/null（read 立刻返回）后跑 35 秒，
    #    必须看到 wd.log 里出现护栏记录，且进程 CPU 仍然≈0（没有忙循环）。
    import base64 as _b64
    _wsrc = _module()._session_watchdog_script(
        _module()._session_files("/tmp/pyaissh-sessions", "guard1"), 60)
    _bad = _wsrc.replace('mkfifo -m 600 "$WDFIFO" 2>/dev/null || true', ":") \
                .replace('exec 9<>"$WDFIFO"', "exec 9</dev/null")
    assert "exec 9</dev/null" in _bad and "mkfifo" not in _bad, _bad[:200]
    _live_run(["exec", tgt, "--cmd",
               "rm -rf /tmp/pyaissh-sessions/guard1; mkdir -m 700 -p /tmp/pyaissh-sessions/guard1; "
               "printf '%s' \"$(date +%s)\" > /tmp/pyaissh-sessions/guard1/beat; "
               "echo $$ > /tmp/pyaissh-sessions/guard1/bash.pid; echo 1 > /tmp/pyaissh-sessions/guard1/sess.pid; "
               "printf %s '" + _b64.b64encode(_bad.encode()).decode() + "' | base64 -d > "
               "/tmp/pyaissh-sessions/guard1/watch.sh; chmod 700 /tmp/pyaissh-sessions/guard1/watch.sh; "
               "setsid nohup bash /tmp/pyaissh-sessions/guard1/watch.sh >/dev/null 2>&1 </dev/null & "
               "echo started"], timeout=60)
    time.sleep(_wait(1, 6))
    rc, _jg2, _ = _live_run(["exec", tgt, "--cmd",
                             "P=$(pgrep -f '/tmp/pyaissh-sessions/guard1/[w]atch[.]sh' | head -1); "
                             "echo \"guardlog=$(grep -c 'guard: read -t' /tmp/pyaissh-sessions/guard1/wd.log 2>/dev/null)\"; "
                             "echo \"cpu=$(awk '{print $14+$15}' /proc/$P/stat 2>/dev/null)\"; "
                             "echo \"kids=$(pgrep -P $P | wc -l)\" ; kill -9 $P 2>/dev/null; "
                             "rm -rf /tmp/pyaissh-sessions/guard1; echo cleaned"], timeout=60)
    _g2 = (_jg2 or {}).get("stdout") or ""
    s.check("W2 防 spin 护栏生效：read 立刻返回时写 wd.log 并退回 sleep",
            _field(_g2, "guardlog") not in ("", "0"), repr(_g2)[:200])
    s.check("W2 护栏兜底期间不烧 CPU（≤3 jiffy / %d 秒）" % _wait(1, 6),
            _field(_g2, "cpu").isdigit() and int(_field(_g2, "cpu")) <= 3,
            "cpu=%s jiffy" % _field(_g2, "cpu"))

def suite_live_session_lifecycle(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    # ---- L：看门狗"临终带走"（v2.3.0 用户提案）：有人 rm -rf 会话目录时，看门狗最后收一次再自退 ----
    import base64 as _b64b
    # L1 主用例：start(ttl>0) → 等 >15s（看门狗已记下 pid）→ rm -rf 目录 → 等 ≤20s
    #    → starter/script/bash -i 与看门狗全消失，且随后 kill --all 没有孤儿可扫（证明是看门狗干的）
    _lz = name + "lz"
    rc, _jl, _ = _live_run(["session", "start", tgt, "--name", _lz, "--ttl", "300"], timeout=90)
    time.sleep(_wait(1, 3))
    rc, _jpre, _ = _live_run(["exec", tgt, "--cmd",
                              "rm -rf /tmp/pyaissh-sessions/%s; sleep 0.5; "
                              "echo -n 'before='; pgrep -fc '[p]yaissh-sessions/%s' || true"
                              % (_lz, _lz)], timeout=60)
    _before = _field(((_jpre or {}).get("stdout") or ""), "before")
    time.sleep(_wait(2, 4))
    # 路径用变量拼（cmdline 里不出现完整路径）⇒ pgrep -f 不会匹配到执行本命令的 shell 自己
    _l1cmd = ("R=$(printf '/tmp/pyaissh-%s' sessions); D=\"$R/" + _lz + "\"; "
              "echo -n 'after='; pgrep -fc \"$D\" || true; "
              "echo -n 'dir='; [ -d \"$D\" ] && echo 1 || echo 0; "
              "echo -n 'script='; ps -eo args | grep -c \"[s]cript -qfc.*$D\" || true")
    rc, _jz, _ = _live_run(["exec", tgt, "--cmd", _l1cmd], timeout=60)
    _lzs = (_jz or {}).get("stdout") or ""
    s.check("L1 孤儿确实存在过（rm -rf 目录后还有 >=3 个进程）",
            _before.isdigit() and int(_before) >= 3, "before=%s" % _before)
    s.check("L1 看门狗临终带走：rm -rf 目录后该会话的进程全消失、看门狗自退",
            _field(_lzs, "after") == "0" and _field(_lzs, "dir") == "0"
            and _field(_lzs, "script") == "0", repr(_lzs)[:200])
    rc, _jall2, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    s.check("L1 随后 kill --all 已无可扫孤儿（说明是看门狗清的，不是兜底扫描）",
            bool(_jall2) and not (_jall2.get("orphans_total") or 0), repr(_jall2)[:200])

    # L2 自证反向：记住的 pid 被内核复用成"无关进程"时**不得误杀**
    #    做法：伪造 sess.pid/bash.pid 指向一个 argv 不含会话路径的 sleep，再 rm -rf 目录等一个 tick
    _lz2 = name + "lz2"
    _D2 = "/tmp/pyaissh-sessions/" + _lz2
    _wsrc2 = _module()._session_watchdog_script(
        _module()._session_files("/tmp/pyaissh-sessions", _lz2), 300)
    _l2cmd = ("rm -rf {D}; mkdir -m 700 -p {D}; "
              "setsid nohup sleep 120 >/dev/null 2>&1 </dev/null & DP=$!; "
              "echo $DP > {D}/sess.pid; echo $DP > {D}/bash.pid; "
              "date +%s > {D}/beat; echo \"1 200 $(date +%s) 300\" > {D}/meta; "
              "printf %s '{B64}' | base64 -d > {D}/watch.sh; chmod 700 {D}/watch.sh; "
              "setsid nohup bash {D}/watch.sh >/dev/null 2>&1 </dev/null & echo \"decoy=$DP\""
              ).format(D=_D2, B64=_b64b.b64encode(_wsrc2.encode()).decode())
    rc, _jdec2, _ = _live_run(["exec", tgt, "--cmd", _l2cmd], timeout=60)
    _decoy2 = _field(((_jdec2 or {}).get("stdout") or ""), "decoy")
    time.sleep(3)
    _live_run(["exec", tgt, "--cmd", "rm -rf " + _D2], timeout=60)
    time.sleep(_wait(2, 4))
    rc, _jl2b, _ = _live_run(["exec", tgt, "--cmd",
                              "echo -n 'decoy='; kill -0 %s 2>/dev/null && echo alive || echo gone; "
                              "echo -n 'wd='; pgrep -fc '%s/[w]atch[.]sh' || true; "
                              "kill -9 %s 2>/dev/null; rm -rf %s; echo cleaned"
                              % (_decoy2, _D2, _decoy2, _D2)], timeout=60)
    _l2s = (_jl2b or {}).get("stdout") or ""
    s.check("L2 自证闸门：记住的 pid 已属无关进程（argv 不含会话路径）⇒ 不误杀、看门狗自退",
            _decoy2.isdigit() and _field(_l2s, "decoy") == "alive" and _field(_l2s, "wd") == "0",
            repr(_l2s)[:200])

    # L3 名字被接管：rm -rf 目录后立刻同名重建 → 老看门狗必须退出、新会话必须活着
    _lz3 = name + "lz3"
    _live_run(["session", "start", tgt, "--name", _lz3, "--ttl", "300"], timeout=90)
    time.sleep(_wait(1, 3))
    _live_run(["exec", tgt, "--cmd", "rm -rf /tmp/pyaissh-sessions/%s" % _lz3], timeout=60)
    rc, _jnew, _ = _live_run(["session", "start", tgt, "--name", _lz3, "--ttl", "300"], timeout=90)
    time.sleep(_wait(2, 4))   # 给老看门狗足够时间 tick（它会看到 watch.pid/meta 变了，应自行退出）
    rc, _jl3, _ = _live_run(["session", "run", tgt, "--name", _lz3, "--cmd", "echo NEWALIVE",
                             "--wait-rc", "10"], timeout=60)
    rc, _jc3, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'watchprocs='; pgrep -fc '/tmp/pyaissh-sessions/%s/[w]atch[.]sh' || true"
                             % _lz3], timeout=60)
    s.check("L3 同名重建不被老看门狗误杀：新会话仍可执行命令",
            bool(_jl3) and _jl3.get("exit_code") == 0 and "NEWALIVE" in (_jl3.get("stdout") or ""),
            repr(_jl3)[:160])
    s.check("L3 老看门狗已自退（只剩新会话的那 1 个看门狗进程）",
            _field(((_jc3 or {}).get("stdout") or ""), "watchprocs") == "1",
            repr((_jc3 or {}).get("stdout"))[:160])
    _live_run(["session", "kill", tgt, "--name", _lz3], timeout=60)

def suite_live_session_orphan(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    # ---- O：按 argv 扫孤儿（v2.3.0）：目录被手工删掉、只剩进程的会话，`kill --all` 也要收掉 ----
    _orph2 = name + "o2"
    # 用 --ttl 0：新语义下"目录被删"的孤儿会由看门狗临终带走，argv 扫描只能在**没有看门狗**时被验证
    _live_run(["session", "start", tgt, "--name", _orph2, "--ttl", "0"], timeout=90)
    # 旁观者诱饵：argv 里"提到"会话路径，但不是会话进程（不该被杀）
    rc, _jdec, _ = _live_run(["exec", tgt, "--cmd",
                              "setsid nohup bash -c 'sleep 120; :' "
                              "/tmp/pyaissh-sessions/%s/out.log >/dev/null 2>&1 </dev/null & echo $!"
                              % _orph2], timeout=60)
    _decoy = ((_jdec or {}).get("stdout") or "").strip().splitlines()[-1:] or [""]
    _decoy = _decoy[0].strip()
    # 手工删掉会话目录：进程失去 pid 记录 ⇒ 逐目录枚举看不见它们（本次加固要解决的场景）
    _live_run(["exec", tgt, "--cmd", "rm -rf /tmp/pyaissh-sessions/%s; sleep 0.5; "
                                     "ps -eo pid,args | grep -c '[p]yaissh-sessions/%s'"
                                     % (_orph2, _orph2)], timeout=60)
    rc, _jall, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    _orph_rows = [o for o in (_jall or {}).get("orphans", []) if o.get("session") == _orph2]
    s.check("O1a kill --all 按 argv 扫到孤儿（无看门狗会话：目录已删只剩进程）",
            bool(_orph_rows), repr((_jall or {}).get("orphans"))[:220])
    s.check("O1b 孤儿清理无残留（orphan_remaining_total=0 且 verified）",
            bool(_jall) and (_jall.get("orphan_remaining_total") or 0) == 0
            and all(o.get("verified") for o in _orph_rows), repr(_orph_rows)[:220])
    # 注意：这条命令里既做 pgrep 又看同一个路径 ⇒ 路径用变量拼，避免 pgrep -f 的自匹配假阳性
    _o1cmd = ("R=$(printf '/tmp/pyaissh-%s' sessions); D=\"$R/" + _orph2 + "\"; "
              "echo -n 'script='; ps -eo args | grep -c '[s]cript -qfc' || true; "
              "echo -n 'starter='; ps -eo args | grep -c \"[e]xec 9<>'$D/in'\" || true; "
              "echo -n 'dir='; [ -d \"$D\" ] && echo 1 || echo 0; "
              "echo -n 'decoy='; kill -0 " + str(_decoy) + " 2>/dev/null && echo alive || echo gone")
    rc, _jo2, _ = _live_run(["exec", tgt, "--cmd", _o1cmd], timeout=60)
    _o2s = (_jo2 or {}).get("stdout") or ""
    s.check("O1c 孤儿进程真被杀掉（无 script 包装、无 starter、目录已删）",
            "script=0" in _o2s and "starter=0" in _o2s and "dir=0" in _o2s, repr(_o2s))
    s.check("O1d 旁观者（argv 提到路径但不是会话进程）**没被误杀**",
            "decoy=alive" in _o2s, repr(_o2s))
    if _decoy.isdigit():
        _live_run(["exec", tgt, "--cmd", "kill -9 %s 2>/dev/null; echo decoy_cleaned" % _decoy],
                  timeout=60)

def suite_live_session_bugs(s):
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    rc, j, _ = _live_run(["session", "start", tgt, "--name", name], timeout=120)
    # ---- 真机复现的 5 个 bug 的回归护栏（v2.3.0；外部评审报的，逐个先复现再修）----
    # B3：read --lines N 曾被完全忽略（20000 行日志 --lines 5 回传上万行）
    rc, jb3s, _ = _live_run(["session", "start", tgt, "--name", name + "b3"], timeout=90)
    _live_run(["session", "run", tgt, "--name", name + "b3", "--cmd", "seq 1 3000",
               "--wait-rc", "20"], timeout=90)
    rc, jb3, _ = _live_run(["session", "read", tgt, "--name", name + "b3", "--lines", "5"],
                           timeout=60)
    _n3 = len([x for x in ((jb3 or {}).get("stdout") or "").splitlines() if x.strip()])
    s.check("B3 read --lines 5 真的只回 5 行", bool(jb3) and 0 < _n3 <= 5,
            "行数=%d lines_returned=%s" % (_n3, (jb3 or {}).get("lines_returned")))
    _live_run(["session", "kill", tgt, "--name", name + "b3"], timeout=60)

    # B4：命令输出 >1MB 时哨兵在old窗口外 → 曾永远回 running（seq 1 300000 ≈ 2MB）
    rc, jb4s, _ = _live_run(["session", "start", tgt, "--name", name + "b4"], timeout=90)
    rc, jb4, _ = _live_run(["session", "run", tgt, "--name", name + "b4", "--cmd",
                            "seq 1 300000; echo BIG_DONE", "--wait-rc", "30"], timeout=120)
    s.check("B4 输出 >1MB 也能看到哨兵（status=done + 尾部有 BIG_DONE）",
            bool(jb4) and jb4.get("status") == "done" and jb4.get("exit_code") == 0
            and "BIG_DONE" in (jb4.get("stdout") or ""),
            "status=%s exit=%s 尾=%r" % ((jb4 or {}).get("status"), (jb4 or {}).get("exit_code"),
                                        ((jb4 or {}).get("stdout") or "")[-30:]))
    _live_run(["session", "kill", tgt, "--name", name + "b4"], timeout=60)

    # B5：CRLF 命令文本要像 exec 一样回传 crlf_normalized
    import shutil as _sh2
    import tempfile as _tf2
    _cdir2 = _tf2.mkdtemp(prefix="pyaissh_sess_crlf_")
    try:
        _f5 = os.path.join(_cdir2, "crlf.sh")
        io.open(_f5, "w", encoding="utf-8", newline="").write("echo SESS_CRLF_OK\r\n")
        rc, jb5s, _ = _live_run(["session", "start", tgt, "--name", name + "b5"], timeout=90)
        rc, jb5, _ = _live_run(["session", "run", tgt, "--name", name + "b5", "--cmd-file", _f5,
                                "--wait-rc", "15"], timeout=60)
        s.check("B5 session run 回传 crlf_normalized",
                bool(jb5) and jb5.get("crlf_normalized") == 1
                and jb5.get("exit_code") == 0, repr(jb5)[:180])
        rc, jb5b, _ = _live_run(["session", "send", tgt, "--name", name + "b5",
                                 "--cmd-file", _f5], timeout=60)
        s.check("B5 session send 回传 crlf_normalized",
                bool(jb5b) and jb5b.get("crlf_normalized") == 1, repr(jb5b)[:160])
        _live_run(["session", "kill", tgt, "--name", name + "b5"], timeout=60)
    finally:
        _sh2.rmtree(_cdir2, ignore_errors=True)

    # B1+B2：长等待不被 SFTP 看门狗误杀（>30s）+ --no-pty 降级路径可用
    rc, jb1s, _ = _live_run(["session", "start", tgt, "--name", name + "b1"], timeout=90)
    _t1 = time.time()
    rc, jb1, _ = _live_run(["session", "run", tgt, "--name", name + "b1", "--cmd",
                            "sleep 32; echo SLEPT32", "--wait-rc", "60"], timeout=180)
    _dt1 = time.time() - _t1
    s.check("B1 会话长等待 >30s 不被看门狗误杀（sleep 32 跑完）",
            bool(jb1) and jb1.get("status") == "done" and jb1.get("exit_code") == 0
            and "SLEPT32" in (jb1.get("stdout") or ""),
            "%.1fs status=%s" % (_dt1, (jb1 or {}).get("status")))
    _live_run(["session", "kill", tgt, "--name", name + "b1"], timeout=60)

    rc, jbnp, _ = _live_run(["session", "start", tgt, "--name", name + "np", "--no-pty"],
                            timeout=90)
    s.check("B2 --no-pty 会话就绪（ready=True，pty=False）",
            bool(jbnp) and jbnp.get("pty") is False and jbnp.get("ready") is True,
            repr(jbnp)[:160])
    rc, jbnp2, _ = _live_run(["session", "run", tgt, "--name", name + "np", "--cmd",
                              "echo NOPTY_OK", "--wait-rc", "15"], timeout=60)
    s.check("B2 --no-pty 下 run 能拿到输出与退出码（曾静默挂死）",
            bool(jbnp2) and jbnp2.get("status") == "done" and jbnp2.get("exit_code") == 0
            and "NOPTY_OK" in (jbnp2.get("stdout") or ""), repr(jbnp2)[:180])
    rc, jbnp3, _ = _live_run(["session", "run", tgt, "--name", name + "np", "--cmd",
                              "cd /tmp && pwd", "--wait-rc", "15"], timeout=60)
    s.check("B2 --no-pty 下多行/复合命令与状态保留正常",
            bool(jbnp3) and jbnp3.get("exit_code") == 0
            and (jbnp3.get("stdout") or "").strip() == "/tmp", repr(jbnp3)[:160])
    _live_run(["session", "kill", tgt, "--name", name + "np"], timeout=60)

    # 错误路径
    rc, je, _ = _live_run(["session", "read", tgt, "--name", "nope_xyz"], timeout=60)
    s.check("读不存在的会话 → session_not_found(2)",
            rc == 2 and bool(je) and je.get("error") == "session_not_found", repr(je)[:140])
    rc, je2, _ = _live_run(["session", "ctrl-c", tgt, "--name", "../etc"], timeout=60)
    s.check("非法会话名拦截（bad_args）",
            rc == 2 and bool(je2) and je2.get("error") == "bad_args", repr(je2)[:140])
    rc, je3, _ = _live_run(["session", "send", tgt, "--name", "x"], timeout=60)
    s.check("send 缺命令 → bad_args", rc == 2 and bool(je3) and je3.get("error") == "bad_args",
            repr(je3)[:140])
    _live_run(["session", "kill", tgt, "--all"], timeout=120)


# ============================================================
# CLI：选择 / 编排
# ============================================================

SUITES = [
    ("unit_regression", "回归（凭据矩阵/parse_target/编码/stdin/--exclude 匹配）", suite_unit_regression),    ("unit_credential", "凭据启发式（真命中/误报豁免矩阵）", suite_unit_credential),
    ("unit_artifacts", "制品结构（域横幅/代码地图/VERSION）", suite_unit_artifacts),
    ("unit_host", "host add/remove/list 闭环（副本 .env，v2.1.4 自动化）", suite_unit_host),
    ("live_sudo", "--sudo 提权（真机）", suite_live_sudo),
    ("live_exec_field", "exec+field（真机）", suite_live_exec_field),
    ("live_transfer", "传输往返（真机：默认/并行/续传/排除）", suite_live_transfer),
    ("live_session", "会话 core（真机：PTY/状态保留/退出码/ctrl-c/keys/kill）", suite_live_session),
    # v2.3.0：live_session 按"改动路径"拆块——开发时只跑动过的那块（`--suite live_session_watchdog`），
    # 全量（--session / --all）仍是这些块全跑（发布前用）
    ("live_session_ttl", "会话空闲回收 TTL/attach（真机）", suite_live_session_ttl),
    ("live_session_watchdog", "会话看门狗（单进程/护栏，真机）", suite_live_session_watchdog),
    ("live_session_lifecycle", "看门狗临终带走/自证闸门/同名接管（真机）", suite_live_session_lifecycle),
    ("live_session_orphan", "argv 扫孤儿（真机）", suite_live_session_orphan),
    ("live_session_bugs", "B1~B5 回归护栏（真机）", suite_live_session_bugs),
]

#: `--session` 展开成哪些套件（保持"全部会话用例"的语义）
_SESSION_SUITES = ["live_session", "live_session_ttl", "live_session_watchdog",
                   "live_session_lifecycle", "live_session_orphan", "live_session_bugs"]


def _run_suite(idx):
    """跑一个套件。**崩了也记 FAIL 并继续跑后面的套件**。

    为什么要接住异常（2026-09-24 教训）：套件中途抛异常（例如新加的断言引用了还没赋值的变量）
    会让整个 runner 带 traceback 退出——后面的套件根本没跑，而 PowerShell 管道里"最后一个命令
    成功"还会让外层看起来是 exit 0。那次我先把它误读成"单位集通过"。现在崩溃=FAIL 且不中断。
    """
    name, desc, fn = SUITES[idx]
    print("\n>>> %s（%s）" % (name, desc))
    s = _Suite(name)
    try:
        fn(s)
    except Exception:
        print("SUITE CRASH（记 FAIL，继续跑其余套件）:\n%s" % traceback.format_exc())
        return False
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
    ap.add_argument("--artifacts", action="store_true", help="仅制品结构集")
    ap.add_argument("--sudo", action="store_true")
    ap.add_argument("--exec", action="store_true")
    ap.add_argument("--transfer", action="store_true")
    ap.add_argument("--session", action="store_true", help="会话真机集（= 下面 6 块全跑）")
    ap.add_argument("--suite", help="按名字选套件（逗号分隔，见 --list）——开发时只跑动过的路径，"
                                   "例如 --suite live_session_watchdog,live_session_lifecycle")
    ap.add_argument("--fast", action="store_true",
                    help="快跑：把看门狗检查周期调到 %ds（PYAISSH_SESSION_TTL_TICK），"
                         "并把用例里「等一个 tick」的等待按比例缩短（默认 15s，真实生产值）"
                         % _FAST_TICK)
    ap.add_argument("--release", action="store_true",
                    help="发布前全量（**唯一**允许 --all / --session 的场合；也可用 "
                         "PYAISSH_TEST_RELEASE=1）。开发期禁止全量与整个模式测试——见 AGENTS.md")
    ap.add_argument("--list", action="store_true", help="列出测试集")
    a = ap.parse_args()

    _release = a.release or os.environ.get("PYAISSH_TEST_RELEASE") == "1"
    if (a.all or a.session) and not _release:
        print("拒绝：开发期禁止全量测试与「整个模式」测试（AGENTS.md『测试只测代码动过的路径』）。\n"
              "  只跑改动落点： python -u tests/run_tests.py --suite <块名> [--fast]   （--list 看块名）\n"
              "  发布前全量：   python -u tests/run_tests.py --all --release")
        return 2

    if a.list:
        print("  套件（--suite 用名字选）：")
        for i, (name, desc, _) in enumerate(SUITES, 1):
            print("  %2d) %-26s %s" % (i, name, desc))
        print("\n  常用组合：")
        print("   开发（只跑动过的路径，示例）：--suite live_session_watchdog,live_session_lifecycle --fast")
        print("   发布前全量：                    --all")
        return 0
    if a.fast:
        os.environ["PYAISSH_SESSION_TTL_TICK"] = str(_FAST_TICK)
        globals()["_TICK"] = _FAST_TICK
        print("[fast] 看门狗 tick=%ds（默认 15s），等待按比例缩短" % _FAST_TICK)
    if a.all:
        order = list(range(len(SUITES)))
    elif a.unit:
        order = [0, 1, 2, 3]
    elif a.artifacts:
        order = [2]
    elif a.suite:
        want = [x.strip() for x in a.suite.split(",") if x.strip()]
        names = [s[0] for s in SUITES]
        order = []
        for w in want:
            if w not in names:
                print("未知套件 %r（用 --list 看可选）" % w)
                return 2
            order.append(names.index(w))
    elif a.sudo or a.exec or a.transfer or a.session:
        names = [s[0] for s in SUITES]
        order = []
        if a.sudo:
            order.append(names.index("live_sudo"))
        if a.exec:
            order.append(names.index("live_exec_field"))
        if a.transfer:
            order.append(names.index("live_transfer"))
        if a.session:
            order += [names.index(n) for n in _SESSION_SUITES]
    else:
        order = _interactive()
        if order is None:
            return 0
        if len(order) == len(SUITES) and not _release:
            print("拒绝：开发期禁止全量测试（AGENTS.md）。请用 --suite <块名> [--fast]。")
            return 2

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
