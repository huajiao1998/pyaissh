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

# 空闲回收：tmux 引擎下主路径是**惰性扫**（任何会话子命令入口先续期目标、再扫其它），
# reaper（每主机一个，默认 300s 一轮）只是"没人再回来"的兜底。
# 因此回收类用例主要靠"等够 TTL + 发一条会话命令触发懒扫"，reaper 类用例则**显式**给那次
# start 传 PYAISSH_SESSION_REAP_INTERVAL=3（间隔在 start 时写进远端 reap.sh），
# 这样不管有没有 --fast 都是几秒钟的事。`--fast` 只是把默认间隔也调小。
_FAST_REAP = 3
_REAP = int(os.environ.get("PYAISSH_SESSION_REAP_INTERVAL") or 300)

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
    s.check("会话路径表完整（dir/log/meta/token/beat/tmux）",
            sf["dir"] == "/tmp/pyaissh-sessions/demo" and sf["log"].endswith("/out.log")
            and sf["meta"].endswith("/meta") and sf["token"].endswith("/last.token")
            and sf["beat"].endswith("/beat") and sf["tmux"].endswith("/tmux"),
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
    # ---- tmux 引擎（v2.4.0）：名字映射 / target 语法 / 预检闸门（纯函数，无需真机）----
    _tn = m._session_tmux_name
    s.check("会话名映射：py-<utf-8 hex>，可逆且不含 tmux 特殊字符（实测 tmux 会把 `.`/`:` 改成 `_`）",
            _tn("main") == "py-6d61696e" and _tn("a.b") == "py-612e62"
            and m._session_tmux_decode(_tn("a.b")) == "a.b"
            and m._session_tmux_decode(_tn("work-2_x")) == "work-2_x"
            and "." not in _tn("a.b") and ":" not in _tn("a:b"),
            repr((_tn("main"), _tn("a.b"))))
    s.check("tmux 名反解：不是本工具建的会话一律 None（不碰别人的 tmux）",
            m._session_tmux_decode("plain") is None and m._session_tmux_decode("py-zz") is None
            and m._session_tmux_decode("py-612") is None and m._session_tmux_decode("") is None
            and m._session_tmux_decode("py-2f") is None, "非法名/非法 hex 都要拒")
    _tp = m._session_tmux_pair("main")
    s.check("target 语法：会话级 `=NAME`、pane 级 `=NAME:`（实测少了冒号会 can't find pane）",
            _tp == ("py-6d61696e", "=py-6d61696e", "=py-6d61696e:"), repr(_tp))
    _errs = []
    _orig_emit = m.emit_error
    m.emit_error = lambda use_json, et, msg, extra=None: _errs.append((et, msg, extra or {}))
    try:
        _g1 = m._session_tmux_gate(False, {"TMUX": "missing"})
        _g2 = m._session_tmux_gate(False, {"TMUX": "oldver", "TMUXV": "1.8"})
        _g3 = m._session_tmux_gate(False, {"TMUX": "badver", "TMUXV": "weird"})
        _g4 = m._session_tmux_gate(False, {"TMUX": "ok", "TMUXV": "3.5a"})
        _g5 = m._session_tmux_gate(False, {})
    finally:
        m.emit_error = _orig_emit
    s.check("预检闸门：缺 tmux → tmux_missing/255，message 给出可执行安装命令",
            _g1 == 255 and _errs[0][0] == "tmux_missing"
            and "apt-get install -y tmux" in _errs[0][1] and _errs[0][2].get("install_hint"),
            repr(_errs[:1])[:220])
    s.check("预检闸门：版本过低 → tmux_unsupported/255（带版本与下限）",
            _g2 == 255 and _errs[1][0] == "tmux_unsupported"
            and _errs[1][2].get("tmux_version") == "1.8"
            and str(m.SESSION_TMUX_MIN_MAJOR) in str(_errs[1][2].get("required")),
            repr(_errs[1])[:200])
    s.check("预检闸门：版本读不出来 → tmux_failed/255（不猜、继续用）",
            _g3 == 255 and _errs[2][0] == "tmux_failed", repr(_errs[2])[:160])
    s.check("预检闸门：ok 放行；没有标记（老路径/无预检输出）也放行",
            _g4 is None and _g5 is None)
    s.check("retryable 表：tmux_missing/tmux_unsupported 不可重试，tmux_failed 可重试",
            "tmux_missing" not in m._RETRYABLE_ERRORS
            and "tmux_unsupported" not in m._RETRYABLE_ERRORS
            and "tmux_failed" in m._RETRYABLE_ERRORS,
            repr(sorted(m._RETRYABLE_ERRORS))[:200])
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
    # ---- tmux 引擎命令生成（v2.4.0）：start/probe/paste/ctrl-c/kill/reaper ----
    _f = m._session_files("/tmp/pyaissh-sessions", "demo")
    _tm, _tgs, _tgp = m._session_tmux_pair("demo")
    _sc = m._session_start_cmd(_f, _tm, 200, 600,
                              [("PATH", "/usr/bin:/bin"), ("LANG", "C.UTF-8")])
    s.check("start：专用 socket + 不读用户 tmux 配置（-L pyaissh -f /dev/null）",
            "tmux -L pyaissh -f /dev/null" in _sc)
    s.check("start：依赖预检在任何动作之前（缺 tmux 立即退出，不半途建目录）",
            _sc.index("command -v tmux") < _sc.index("mkdir") < _sc.index("new-session"))
    s.check("start：环境注入在 new-session **之前**（tmux server 环境启动即冻结）",
            "set-environment -g" in _sc and _sc.index("set-environment -g") < _sc.index("new-session"))
    s.check("start：window-size manual + pipe-pane 输出镜像落 out.log",
            'set-window-option -t "$TN:" window-size manual' in _sc
            and "pipe-pane -o" in _sc and "cat >> /tmp/pyaissh-sessions/demo/out.log" in _sc)
    s.check("start：meta 4 字段（pty/cols/起始秒/TTL）+ tmux 名文件 + beat 写 epoch",
            "printf '%s %s %s %s\\n' 1 200" in _sc and "/demo/tmux" in _sc
            and "> '/tmp/pyaissh-sessions/demo/beat'" in _sc)
    s.check("start：已有会话时走 EXISTS 分支（不重建、回传旧 pid/TTL）",
            "has-session" in _sc and "SESS__EXISTS=1" in _sc and "SESS__EXTTL" in _sc)
    _sc0 = m._session_start_cmd(_f, _tm, 200, 0, [])
    s.check("start：--ttl 0 只把 TTL 写成 0（回收由 meta 第 4 字段表达，不再有看门狗脚本）",
            " 0 > '/tmp/pyaissh-sessions/demo/meta'" in _sc0
            and "watch.sh" not in _sc0 and "wd.fifo" not in _sc0)
    _pc = m._session_probe_cmd(_f, _tm)
    s.check("probe：一次 exec 拿到 alive/pid/tty/前台命令/管道状态，并顺手续期",
            "SESS__ALIVE=1" in _pc and "SESS__PID=" in _pc and "SESS__CUR=" in _pc
            and "SESS__PIPE=" in _pc and "> '/tmp/pyaissh-sessions/demo/beat'" in _pc)
    s.check("probe：out.log 被删时**不带 -o** 重新 arm（实测：管道活着但文件没了会静默丢输出）",
            "pipe-pane -t" in _pc and "SESS__LOG_REARM=1" in _pc
            and "pipe-pane -o -t" in _pc, _pc[:200])
    _pac = m._session_paste_cmd(_f, _tm)
    s.check("命令注入：load-buffer + paste-buffer（避开 tmux 命令行二次解析）",
            "load-buffer -b pyaissh-buf -" in _pac and "paste-buffer -b pyaissh-buf -d" in _pac
            and "TN='py-64656d6f'" in _pac and '-t "$TN:"' in _pac
            and "send-keys -l" not in _pac, _pac[:260])
    s.check("命令注入：会话不在时先报 GONE（不做无意义的 buffer 操作）",
            "SESS__GONE=1" in _pac)
    _cc = m._session_ctrl_c_cmd(_f, _tm)
    _ccf = m._session_ctrl_c_cmd(_f, _tm, force=True)
    s.check("ctrl-c：注入 C-c 给 pane（tty 交给前台进程组）",
            "send-keys -t \"$TN:\" C-c" in _cc)
    s.check("ctrl-c --force：对内核给的前台进程组 SIGKILL（tpgid），且不用 SIGQUIT（会 core dump）",
            "ps -o tpgid=" in _ccf and 'kill -KILL -- "-$FG"' in _ccf
            and "C-\\\\" not in _ccf and "QUIT" not in _ccf, _ccf[:260])
    s.check("ctrl-c：回传 PID/SID/GROUPS/CHILDREN/SIGNALED（字段集不变）",
            all(k in _cc for k in ("SESS__PID=", "SESS__SID=", "SESS__GROUPS=",
                                   "SESS__CHILDREN=", "SESS__SIGNALED=")))
    _kc = m._session_kill_cmd(_f, _tm, pane_pid=4242)
    s.check("kill：闭包快照根 = tmux 给的 pane_pid + 去掉 last.token（不留旧引擎痕迹）",
            "P='4242'" in _kc and 'kill -0 "$P"' in _kc
            and "sess.pid" not in _kc and "bash.pid" not in _kc and "script -qfc" not in _kc
            and "SESS__LEGACY" not in _kc and "/demo/last.token" in _kc, _kc[:200])
    s.check("kill：先快照再 kill-session（父进程被杀后子进程会 reparent，事后算会漏）",
            _kc.index("SNAP=") < _kc.index("kill-session"))
    s.check("kill：TERM → 校验 → KILL + 回传 SWEPT/LEFT/ROOTS/HAD/CLEANED",
            "kill -TERM $T" in _kc and "kill -KILL $K" in _kc
            and all(k in _kc for k in ("SESS__SWEPT=", "SESS__LEFT=", "SESS__ROOTS=",
                                        "SESS__HAD=", "SESS__CLEANED=1")))
    s.check("kill：--keep-dir 时不删目录", "rm -rf '/tmp/pyaissh-sessions/demo'" in _kc
            and "rm -rf" not in m._session_kill_cmd(_f, _tm, keep_dir=True, pane_pid=1))
    _rs = m._session_reap_script("/tmp/pyaissh-sessions")
    s.check("reaper 脚本：--once/--loop 两用 + pid 文件带指纹（pid + 间隔）",
            "reap_once()" in _rs and "[ \"$1\" = \"--loop\" ]" in _rs
            and 'printf \'%s %s\\n\' "$$" "$INTERVAL" > "$ROOT/.reaper.pid"' in _rs)
    s.check("reaper 脚本：TTL 取 meta 第 4 字段、空闲判据取 beat 内容、非法值一律不回收",
            "awk '{print $4}'" in _rs and 'B=$(cat "$D/beat"' in _rs
            and "*[!0-9]*)" in _rs and "continue" in _rs)
    s.check("reaper 脚本：只有提示符空闲（前台是 shell）才收，判不出就不收",
            "pane_current_command" in _rs and "bash|sh|dash|zsh|ksh|-bash" in _rs)
    s.check("reaper 脚本：没有会话目录时下一轮自退并清掉 pid 文件（不留空转常驻）",
            'if [ "$n" -eq 0 ]; then rm -f "$ROOT/.reaper.pid" "$ROOT/reap.sh" 2>/dev/null; '
            'exit 0; fi' in _rs,
            _rs[-320:])
    s.check("reaper 脚本：不碰陌生目录（没有 tmux 名文件的目录直接跳过）",
            '[ -z "$TN" ]; then continue' in _rs)
    s.check("reaper 脚本：日志超限截断（只留尾 200 行）",
            "tail -n 200" in _rs and str(m.SESSION_TMUX_REAP_LOG_MAX) in _rs)
    _sw = m._session_reap_sweep_cmd("/tmp/pyaissh-sessions")
    s.check("惰性扫 = 同一份 reaper 逻辑内联（不依赖远端 reap.sh 是否落盘）",
            "reap_once()" not in _sw and "SESS__REAPED=1" in _sw
            and "pane_current_command" in _sw and "reclaim $N" in _sw, _sw[:200])
    _save_reap = os.environ.get("PYAISSH_SESSION_REAP_INTERVAL")
    try:
        os.environ.pop("PYAISSH_SESSION_REAP_INTERVAL", None)
        _r1 = m._session_reap_interval()
        os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = "3"
        _r2 = m._session_reap_interval()
        _rbad = []
        for _b in ("0", "abc", "99999", "2.5"):
            os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = _b
            _rbad.append(m._session_reap_interval())
    finally:
        if _save_reap is None:
            os.environ.pop("PYAISSH_SESSION_REAP_INTERVAL", None)
        else:
            os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = _save_reap
    s.check("reaper 间隔可配：默认 %d / 3 生效 / 0・abc・99999・2.5 一律回落默认（只收整数）"
            % m.SESSION_TMUX_LOOP_INTERVAL,
            (_r1, _r2) == (m.SESSION_TMUX_LOOP_INTERVAL, 3)
            and _rbad == [m.SESSION_TMUX_LOOP_INTERVAL] * 4, repr((_r1, _r2, _rbad)))
    _cap = {}
    _orig_run = m._session_run
    m._session_run = lambda client, cmd, stdin_data=None, timeout=30: (
        _cap.setdefault("cmd", cmd), (0, "", ""))[1]
    try:
        m._session_touch(None, _f)
    finally:
        m._session_run = _orig_run
    s.check("交互续期：_session_touch 写的是 epoch 秒（start --attach 用）",
            _cap.get("cmd") == ("printf '%s\\n' \"$(date +%s)\" > "
                                "'/tmp/pyaissh-sessions/demo/beat' 2>/dev/null; "
                                "chmod 600 '/tmp/pyaissh-sessions/demo/beat' 2>/dev/null"),
            repr(_cap.get("cmd")))
    # 哨兵包裹（真 bug 的护栏：哨兵必须与命令同一行被解析，否则被 read 吃掉）
    pl = m._session_payload_text("read -p 'x' V; echo $V", "abcd1234")
    s.check("命令与哨兵同一行（{ ...; }; echo 哨兵）",
            pl.startswith("\n{\n") and "\n}; echo \"%sabcd1234__$?\"\n" % m.SESSION_RC_PREFIX in pl,
            repr(pl))
    s.check("载荷以换行开头（R2：先把上次断线残留的半行终结掉，防串行）",
            pl.startswith("\n"))
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
    # 输出清洗（v2.4.0 起不再特判 util-linux `script` 的头尾行——那是已删引擎的噪音，
    # 现在它只是一行普通输出；这里顺带断言"不特判"）
    dirty = ("Script started on x [COMMAND=\"bash\"]\r\n\x1b[31mRED\x1b[0m\r\n"
             "%sab12__0\r\n" % m.SESSION_RC_PREFIX)
    s.check("清洗：去 CR/ANSI/哨兵行（script 头行不再特判，按普通输出保留）",
            m._session_clean_text(dirty) == 'Script started on x [COMMAND="bash"]\nRED',
            repr(m._session_clean_text(dirty)))
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

    # ---- 编码不变量（2026-09-24 定：所有脚本一律 UTF-8 + 显式声明 I/O 编码）----
    # 为什么上闸：本机代码页是 GBK/936，而工具/脚本产出 UTF-8。历史上踩过两类事：
    #   ① 源码被 GBK 存过 → 中文注释/字符串直接乱码；
    #   ② 本地文本 open() 没写 encoding= → Windows 上按 cp936 读写，写出非 UTF-8 字节；
    #   ③ 入口脚本没把 stdout 设 UTF-8 → 管道/重定向下打印中文乱码，遇到 GBK 编不出的
    #      字符（⬜ 等）直接 UnicodeEncodeError 崩溃。
    import ast as _ast
    _enc_bad, _open_bad = [], []
    _txt_n = 0
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", ".venv",
                  "tavern-ops"}      # tavern-ops 是别的会话的工作目录，不归本测试管
    _TEXT_EXT = (".py", ".sh", ".ps1", ".json", ".md", ".yml", ".yaml", ".cmd")
    for _dp, _dns, _fns in os.walk(_REPO):
        _dns[:] = [d for d in _dns if d not in _SKIP_DIRS]
        for _fn in _fns:
            _ext = os.path.splitext(_fn)[1].lower()
            if _ext not in _TEXT_EXT:
                continue
            _p = os.path.join(_dp, _fn)
            _rel = os.path.relpath(_p, _REPO)
            _txt_n += 1
            _raw = open(_p, "rb").read()
            try:
                _txt = _raw.decode("utf-8")
            except UnicodeDecodeError as _e:
                _enc_bad.append("%s（%s）" % (_rel, str(_e)[:60]))
                continue
            if _raw.startswith(b"\xef\xbb\xbf"):
                _enc_bad.append("%s（带 BOM：统一不要 BOM）" % _rel)
            if _ext != ".py":
                continue
            try:
                _tree = _ast.parse(_txt)
            except SyntaxError as _e:
                _open_bad.append("%s（语法错误，无法静态检查: %s）" % (_rel, _e))
                continue
            for _node in _ast.walk(_tree):
                if not isinstance(_node, _ast.Call):
                    continue
                _f = _node.func
                _is_open = ((isinstance(_f, _ast.Name) and _f.id in ("open", "io.open"))
                            or (isinstance(_f, _ast.Attribute) and _f.attr == "open"
                                and isinstance(_f.value, _ast.Name) and _f.value.id == "io"))
                if not _is_open:
                    continue
                if any(_k.arg == "encoding" for _k in _node.keywords):
                    continue
                _mode = None
                _mode_expr = _node.args[1] if len(_node.args) >= 2 else None
                for _k in _node.keywords:
                    if _k.arg == "mode":
                        _mode_expr = _k.value
                if isinstance(_mode_expr, _ast.Constant) and isinstance(_mode_expr.value, str):
                    _mode = _mode_expr.value
                if isinstance(_mode, str) and "b" in _mode:
                    continue      # 二进制模式不需要 encoding
                if _mode is None and _mode_expr is not None:
                    # 模式不是字面量（如 `"r+b" if existing else "wb"`）：看源码段里的
                    # 字符串字面量是否**都**含 b —— 都是二进制就放过，判不出来才报
                    _seg = _ast.get_source_segment(_txt, _mode_expr) or ""
                    _lits = re.findall(r"['\"]([^'\"]*)['\"]", _seg)
                    if _lits and all("b" in _x for _x in _lits):
                        continue
                _open_bad.append("%s:%d" % (_rel, _node.lineno))
    s.check("编码：仓库文本文件全部是 UTF-8 且无 BOM（%d 个文件）" % _txt_n,
            not _enc_bad, repr(_enc_bad[:6]))
    s.check("编码：本地文本 open() 一律显式写 encoding=（含生成物 pyaissh.py）",
            not _open_bad, repr(_open_bad[:8]))
    _art = open(os.path.join(_REPO, "pyaissh.py"), "rb").read().decode("utf-8")
    s.check("编码：制品把 stdout/stderr/stdin 重配为 UTF-8（errors=replace，永不因编码崩）",
            'reconfigure(encoding="utf-8", errors="replace")' in _art, "")
    s.check("编码：制品在 Windows 控制台切成 UTF-8（65001）并在退出时还原",
            "SetConsoleOutputCP(65001)" in _art and "SetConsoleOutputCP(old_cp)" in _art, "")
    s.check("编码：制品启动时**调用** _setup_console_utf8（不只是定义）",
            _art.count("_setup_console_utf8()") >= 2, "调用次数=%d" % _art.count("_setup_console_utf8()"))
    _entries = ["pyaissh-dev/contract_baseline.py", "pyaissh-mcp/sync_check.py",
                "pyaissh-mcp/pyaissh_mcp.py", "tests/run_tests.py"]
    _tdir = os.path.join(_REPO, "pyaissh-mcp", "test")
    if os.path.isdir(_tdir):
        _entries += [os.path.join("pyaissh-mcp", "test", f)
                     for f in sorted(os.listdir(_tdir)) if f.endswith(".py")]
    _no_guard = []
    for _r in _entries:
        _p = os.path.join(_REPO, _r)
        if not os.path.exists(_p):
            continue
        _t = open(_p, "rb").read().decode("utf-8")
        if 'reconfigure(encoding="utf-8"' not in _t:
            _no_guard.append(_r)
    s.check("编码：会被直接运行的入口脚本都显式设 stdout/stderr=UTF-8（%d 个）" % len(_entries),
            not _no_guard, repr(_no_guard))
    # 远端兜底：通道里没有 LANG/LC_ALL 时要给 pane 补 C.UTF-8（否则远端可能落到 POSIX，
    # 打印中文乱码或直接编码报错）
    _saved_lang = (os.environ.pop("LANG", None), os.environ.pop("LC_ALL", None))
    try:
        _items = dict(m._session_env_items())
        os.environ["LANG"] = "zh_CN.UTF-8"
        _items2 = dict(m._session_env_items())
    finally:
        os.environ.pop("LANG", None)
        if _saved_lang[0] is not None:
            os.environ["LANG"] = _saved_lang[0]
        if _saved_lang[1] is not None:
            os.environ["LC_ALL"] = _saved_lang[1]
    s.check("编码：通道无 LANG/LC_ALL 时兜底注入 %s；有则原样透传" % m.SESSION_DEFAULT_LANG,
            _items.get("LANG") == m.SESSION_DEFAULT_LANG and _items2.get("LANG") == "zh_CN.UTF-8",
            "无=%r 有=%r" % (_items.get("LANG"), _items2.get("LANG")))


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

    # kill：进程树闭包（不只是删目录；tmux 引擎下闭包 = 会话 shell + 它的作业）
    _live_run(["session", "send", tgt, "--name", name, "--cmd", "sleep 200 &"], timeout=60)
    time.sleep(0.8)
    rc, jz, _ = _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    row = ((jz or {}).get("sessions") or [{}])[0]
    s.check("kill 扫到会话进程树（含后台作业，swept>=2）", bool(jz) and (row.get("swept") or 0) >= 2,
            repr(row)[:160])
    s.check("kill 后无残留、目录已清",
            bool(jz) and jz.get("remaining_total") == 0 and row.get("cleaned") is True,
            repr(jz)[:180])
    s.check("kill 报了 pane pid 为根（roots>=1）且 verified=true",
            bool(jz) and (row.get("roots") or 0) >= 1 and row.get("verified") is True,
            repr(row)[:200])
    rc, js, _ = _live_run(["exec", tgt, "--cmd",
                           "echo -n 'sess='; tmux -L pyaissh ls 2>/dev/null | grep -c '^%s:' || true; "
                           "echo -n 'dir='; ls -d /tmp/pyaissh-sessions/%s 2>/dev/null | wc -l"
                           % (_tmux_name(name), name)], timeout=60)
    _js_txt = (js or {}).get("stdout") or ""
    s.check("远端无 tmux 会话、无会话目录残留（真被杀干净）",
            "sess=0" in _js_txt and "dir=0" in _js_txt, repr(_js_txt))
    rc, jsl, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("kill 后该会话不在 list", bool(jsl) and not any(
        x.get("session") == name for x in (jsl.get("sessions") or [])), repr(jsl)[:140])

    # ---- 残留护栏（v2.4.0）：拿不到 pane pid 时**不许无声宣称"清干净"** ----
    # 场景：会话目录被外部删掉 + tmux 会话被外部 kill（连接收尾时既没账也没 pid）
    _ghost = name + "gh"
    _live_run(["session", "start", tgt, "--name", _ghost], timeout=90)
    _live_run(["exec", tgt, "--cmd",
               "tmux -L pyaissh kill-session -t '=%s' 2>/dev/null; "
               "rm -rf /tmp/pyaissh-sessions/%s; echo gone" % (_tmux_name(_ghost), _ghost)],
              timeout=60)
    rc, _jg, _ = _live_run(["session", "kill", tgt, "--name", _ghost], timeout=120)
    _rowg = ((_jg or {}).get("sessions") or [{}])[0]
    s.check("无账无 pid 时不谎报：roots=0 + verified=false（确有残留时空手时也不会说 verified）",
            bool(_jg) and _rowg.get("roots") == 0 and _rowg.get("verified") is False,
            repr(_jg)[:240])

    # 会话不自退：放着不动 20s（远超任何空闲判定窗口）后仍能跑命令
    _idle = name + "id"
    _live_run(["session", "start", tgt, "--name", _idle], timeout=90)
    time.sleep(20)
    rc, _ji, _ = _live_run(["session", "run", tgt, "--name", _idle, "--cmd", "echo IDLE_OK",
                            "--wait-rc", "10"], timeout=60)
    s.check("闲置 20s 后会话仍可用（常驻不自退、无 idle 超时）",
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

def _tmux_name(name):
    """测试侧复算 tmux 会话名（与被测实现同一映射：py-<utf-8 hex>）。"""
    return "py-" + name.encode("utf-8").hex()


def suite_live_session_ttl(s):
    """空闲回收（v2.4.0 tmux 引擎）：**惰性扫**为主 + 每主机 reaper 兜底 + attach 语义。"""
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    # T1：--ttl 5 且完全不碰它 → 8 秒后**一条 list**（惰性扫）就该把它收掉（进程+目录+tmux 会话）
    _ttl = name + "ttl"
    rc, _jt, _ = _live_run(["session", "start", tgt, "--name", _ttl, "--ttl", "5"], timeout=90)
    s.check("T1a start --ttl 5 回传 ttl_seconds=5（字段口径不变）",
            bool(_jt) and _jt.get("ok") is True and _jt.get("ttl_seconds") == 5, repr(_jt)[:200])
    _pid_ttl = (_jt or {}).get("pid")
    time.sleep(8)
    rc, _jl2, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("T1b 空闲 8s 后惰性扫已回收（list 里没有它了）",
            bool(_jl2) and not any(x.get("session") == _ttl
                                   for x in (_jl2.get("sessions") or [])), repr(_jl2)[:220])
    rc, _jd, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'dir='; ls -d /tmp/pyaissh-sessions/%s 2>/dev/null | wc -l; "
                            "echo -n 'proc='; kill -0 %s 2>/dev/null && echo 1 || echo 0; "
                            "echo -n 'sess='; tmux -L pyaissh ls 2>/dev/null | grep -c '%s' || true"
                            % (_ttl, _pid_ttl, _tmux_name(_ttl))], timeout=60)
    _d1 = (_jd or {}).get("stdout") or ""
    s.check("T1c 回收后目录、进程、tmux 会话三样都没了",
            "dir=0" in _d1 and "proc=0" in _d1 and "sess=0" in _d1, repr(_d1))

    # T2：TTL 很小但**有命令在跑** → 不回收（构建/安装不会被误杀）
    _ttlr = name + "run"
    _live_run(["session", "start", tgt, "--name", _ttlr, "--ttl", "5"], timeout=90)
    _live_run(["session", "send", tgt, "--name", _ttlr, "--cmd", "sleep 20; echo TTLRUN_DONE"],
              timeout=60)
    time.sleep(8)      # 远超 TTL=5，但前台有命令 ⇒ 判忙
    rc, _jlr, _ = _live_run(["session", "list", tgt], timeout=60)
    _row = next((x for x in (_jlr or {}).get("sessions", []) if x.get("session") == _ttlr), None)
    s.check("T2a 有前台命令时不回收（跨过 TTL 仍 running，且前台命令是 sleep）",
            bool(_row) and _row.get("status") == "running"
            and _row.get("current_command") == "sleep", repr(_row)[:220])
    rc, _jout, _ = _live_run(["session", "read", tgt, "--name", _ttlr, "--wait-rc", "25"],
                             timeout=90)
    s.check("T2b 长命令跑完能拿到输出（没被空闲回收误杀）",
            bool(_jout) and "TTLRUN_DONE" in (_jout.get("stdout") or ""),
            "status=%s 尾=%r" % ((_jout or {}).get("status"),
                                ((_jout or {}).get("stdout") or "")[-40:]))
    _live_run(["session", "kill", tgt, "--name", _ttlr], timeout=60)

    # T3：list **不续期**（看一眼≠在用）：一直 list 也照样到点被收
    #     注：start 自身耗时也算 idle，所以 TTL 取 10、两次等待 5+6 才稳
    _ttl3 = name + "nolist"
    _live_run(["session", "start", tgt, "--name", _ttl3, "--ttl", "10"], timeout=90)
    time.sleep(5)
    rc, _jl3a, _ = _live_run(["session", "list", tgt], timeout=60)
    _alive3 = any(x.get("session") == _ttl3 for x in (_jl3a or {}).get("sessions", []))
    time.sleep(6)
    rc, _jl3b, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("T3 list 不续期：中途 list 过也照样到点回收",
            _alive3 and not any(x.get("session") == _ttl3
                                for x in (_jl3b or {}).get("sessions", [])),
            "中途在=%s 之后=%r" % (_alive3, [_x.get("session")
                                            for _x in (_jl3b or {}).get("sessions", [])]))

    # T4：--ttl 0 关闭回收 —— 不装 reaper、也不被任何清扫收掉
    _t0 = name + "off"
    rc, _j0, _ = _live_run(["session", "start", tgt, "--name", _t0, "--ttl", "0"], timeout=90)
    s.check("T4a --ttl 0：ttl_seconds=null + 明确警告",
            bool(_j0) and _j0.get("ttl_seconds") is None
            and any("空闲回收已关闭" in w for w in (_j0.get("warnings") or [])), repr(_j0)[:220])
    time.sleep(8)
    rc, _jl0, _ = _live_run(["session", "list", tgt], timeout=60)
    s.check("T4b --ttl 0 的会话不会被惰性扫/reaper 收掉",
            any(x.get("session") == _t0 for x in (_jl0 or {}).get("sessions", [])),
            repr(_jl0)[:200])
    _live_run(["session", "kill", tgt, "--name", _t0], timeout=60)

    # T5：每主机 reaper 兜底（**无人值守**：期间不发任何 pyaissh 命令）
    #     显式把间隔设成 3s（间隔在 start 时写进远端 reap.sh），与有没有 --fast 无关
    _re = name + "reap"
    _old_reap = os.environ.get("PYAISSH_SESSION_REAP_INTERVAL")
    os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = "3"
    try:
        rc, _jr, _ = _live_run(["session", "start", tgt, "--name", _re, "--ttl", "5"], timeout=90)
        s.check("T5a start 带出 reaper（间隔 3s 写进远端脚本）", rc == 0 and bool(_jr),
                repr(_jr)[:160])
        time.sleep(13)
    finally:
        if _old_reap is None:
            os.environ.pop("PYAISSH_SESSION_REAP_INTERVAL", None)
        else:
            os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = _old_reap
    rc, _jre, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'dir='; ls -d /tmp/pyaissh-sessions/%s 2>/dev/null | wc -l; "
                             "echo -n 'log='; grep -c 'reclaim %s' "
                             "/tmp/pyaissh-sessions/.reaper.log 2>/dev/null || true; "
                             "echo -n 'reapers='; ps -eo args | grep -c '[r]eap.sh --loop' || true"
                             % (_re, _re)], timeout=60)
    _d5 = (_jre or {}).get("stdout") or ""
    s.check("T5b reaper 无人值守回收（13s 里没有任何 pyaissh 命令）",
            "dir=0" in _d5 and "log=" in _d5 and "log=0" not in _d5, repr(_d5))
    s.check("T5c 收掉最后一个会话后 reaper 自行退出（不留空转常驻）",
            _field(_d5, "reapers") == "0", repr(_d5))

    # T6：list 的保活进度字段（AI 据此判断"还能放多久"）
    _ttl6 = name + "exp"
    _live_run(["session", "start", tgt, "--name", _ttl6, "--ttl", "60"], timeout=90)
    rc, _jl6, _ = _live_run(["session", "list", tgt], timeout=60)
    _row6 = next((x for x in (_jl6 or {}).get("sessions", []) if x.get("session") == _ttl6), None)
    s.check("T6 list 给出 idle_seconds/ttl_seconds/expires_in_seconds（口径不变）",
            bool(_row6) and isinstance(_row6.get("idle_seconds"), int)
            and _row6.get("ttl_seconds") == 60
            and isinstance(_row6.get("expires_in_seconds"), int)
            and 0 <= _row6["expires_in_seconds"] <= 60, repr(_row6)[:240])
    _live_run(["session", "kill", tgt, "--name", _ttl6], timeout=60)

    # T7：--attach（活着就接上、没有才新建）+ 不带 --attach 时的提示
    _at = name + "at"
    rc, _ja1, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60"], timeout=90)
    rc, _ja2, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60",
                             "--attach"], timeout=90)
    s.check("T7a --attach 接上旧会话（attached=true 且 pid 不变）",
            bool(_ja2) and _ja2.get("ok") is True and _ja2.get("attached") is True
            and _ja2.get("pid") == (_ja1 or {}).get("pid"),
            "attached=%s pid=%s/%s" % ((_ja2 or {}).get("attached"), (_ja2 or {}).get("pid"),
                                       (_ja1 or {}).get("pid")))
    s.check("T7b --attach 续期（idle_seconds 被刷新到很小）",
            bool(_ja2) and isinstance(_ja2.get("idle_seconds"), int)
            and _ja2["idle_seconds"] <= 3, repr(_ja2)[:200])
    rc, _je, _e = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60"], timeout=90)
    s.check("T7c 不带 --attach 撞名仍报 session_exists，但提示改为「直接继续用它」",
            bool(_je) and _je.get("error") == "session_exists"
            and "直接继续用它" in (_je.get("message") or ""),
            repr(_je)[:220])
    _live_run(["session", "kill", tgt, "--name", _at], timeout=60)
    rc, _ja3, _ = _live_run(["session", "start", tgt, "--name", _at, "--ttl", "60",
                             "--attach"], timeout=90)
    s.check("T7d --attach 在会话不存在时新建（attached=false）",
            bool(_ja3) and _ja3.get("ok") is True and _ja3.get("attached") is False
            and isinstance(_ja3.get("pid"), int), repr(_ja3)[:200])
    _live_run(["session", "kill", tgt, "--name", _at], timeout=60)

def suite_live_session_engine(s):
    """tmux 引擎（v2.4.0）：socket 隔离 / 名字映射 / 输出镜像 / 无残留辅助进程 / 开销（PERF）。"""
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    # E1：专用 socket + 名字映射（与用户自己的 tmux 完全隔离）
    _e1 = name + "e1"
    rc, _j1, _ = _live_run(["session", "start", tgt, "--name", _e1, "--ttl", "300"], timeout=90)
    s.check("E1a start 就绪", bool(_j1) and _j1.get("ready") is True, repr(_j1)[:180])
    rc, _js, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'sock='; tmux -L pyaissh ls -F '#{session_name}' 2>/dev/null "
                            "| grep -c '^%s$'; "
                            "echo -n 'user='; tmux ls -F '#{session_name}' 2>/dev/null "
                            "| grep -c '^%s$' || true" % (_tmux_name(_e1), _tmux_name(_e1))],
                           timeout=60)
    _e1s = (_js or {}).get("stdout") or ""
    s.check("E1b 会话在专用 socket 上、且不污染用户默认 socket",
            "sock=1" in _e1s and "user=0" in _e1s, repr(_e1s))

    # E2：pane 事实（pid 一致、空闲时前台是 shell、管道已 arm）
    rc, _jp, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'pane='; tmux -L pyaissh display-message -p "
                            "-t '=%s:' '#{pane_pid}'; "
                            "echo -n 'cmd='; tmux -L pyaissh display-message -p "
                            "-t '=%s:' '#{pane_current_command}'; "
                            "echo -n 'pipe='; tmux -L pyaissh display-message -p "
                            "-t '=%s:' '#{pane_pipe}'; "
                            "echo -n 'size='; tmux -L pyaissh display-message -p "
                            "-t '=%s:' '#{pane_width}x#{pane_height}'"
                            % (_tmux_name(_e1), _tmux_name(_e1), _tmux_name(_e1),
                               _tmux_name(_e1))], timeout=60)
    _e2s = (_jp or {}).get("stdout") or ""
    s.check("E2a pane_pid == 返回的 pid、空闲前台命令是 shell",
            _field(_e2s, "pane") == str((_j1 or {}).get("pid"))
            and _field(_e2s, "cmd") == "bash", repr(_e2s))
    s.check("E2b pipe-pane 已 arm、窗格尺寸 = --cols x 50（window-size manual）",
            _field(_e2s, "pipe") == "1" and _field(_e2s, "size") == "200x50", repr(_e2s))

    # E3：输出按**字节**镜像到 out.log（offset 契约的基础）
    rc, _je3, _ = _live_run(["session", "run", tgt, "--name", _e1, "--cmd",
                             "echo MIRROR_MARK_123", "--wait-rc", "20"], timeout=60)
    rc, _jm, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'has='; grep -c MIRROR_MARK_123 "
                            "/tmp/pyaissh-sessions/%s/out.log" % _e1], timeout=60)
    s.check("E3 命令输出确实落进 out.log（tmux pipe-pane 字节镜像）",
            "has=1" in ((_jm or {}).get("stdout") or ""), repr((_jm or {}).get("stdout")))

    # E4：out.log 被外部删除 ⇒ 下次探测自动重 arm（实测：管道活着但文件没了会静默丢输出）
    rc, _jr4, _ = _live_run(["exec", tgt, "--cmd",
                             "rm -f /tmp/pyaissh-sessions/%s/out.log; echo removed" % _e1],
                            timeout=60)
    rc, _je4, _err4 = _live_run(["session", "run", tgt, "--name", _e1, "--cmd",
                                 "echo AFTER_LOG_RM", "--wait-rc", "20"], timeout=60)
    rc, _jm4, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'exists='; [ -f /tmp/pyaissh-sessions/%s/out.log ] "
                             "&& echo 1 || echo 0; "
                             "echo -n 'has='; grep -c AFTER_LOG_RM "
                             "/tmp/pyaissh-sessions/%s/out.log 2>/dev/null || true" % (_e1, _e1)],
                            timeout=60)
    _e4s = (_jm4 or {}).get("stdout") or ""
    s.check("E4 out.log 被删后自动重建并继续镜像（log_recreated 自愈）",
            "exists=1" in _e4s and "has=1" in _e4s, repr(_e4s))
    s.check("E4 自愈有 stderr 留痕（log_recreated）",
            "log_recreated" in (_err4 or ""), repr((_err4 or "")[-160:]))

    # E5：每会话**没有**常驻辅助进程（旧引擎每会话 1 看门狗 + 1 sleep）
    rc, _ja, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'helpers='; ps -eo args | "
                            "grep -cE '[w]atch[.]sh|[w]d[.]fifo|[s]cript -qfc' || true; "
                            "echo -n 'tmuxsrv='; pgrep -c -f '[t]mux -L pyaissh' || true"],
                           timeout=60)
    _e5s = (_ja or {}).get("stdout") or ""
    s.check("E5 会话没有看门狗/FIFO/script 之类的常驻辅助进程",
            _field(_e5s, "helpers") == "0", repr(_e5s))

    # E8：reaper 收敛——换间隔起新会话会收掉旧代，每主机始终只有一个 reaper
    _old_r2 = os.environ.get("PYAISSH_SESSION_REAP_INTERVAL")
    try:
        os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = "30"
        _live_run(["session", "start", tgt, "--name", name + "rr1", "--ttl", "300"], timeout=90)
        os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = "3"
        _live_run(["session", "start", tgt, "--name", name + "rr2", "--ttl", "300"], timeout=90)
    finally:
        if _old_r2 is None:
            os.environ.pop("PYAISSH_SESSION_REAP_INTERVAL", None)
        else:
            os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = _old_r2
    rc, _jrr, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'reapers='; ps -eo args | grep -c '[r]eap.sh --loop' || true; "
                             "echo -n 'interval='; awk '{print $2}' "
                             "/tmp/pyaissh-sessions/.reaper.pid 2>/dev/null"], timeout=60)
    _rr = (_jrr or {}).get("stdout") or ""
    s.check("E8 每主机只有一个 reaper，且指纹是最新间隔（换间隔收掉旧代，不堆积）",
            _field(_rr, "reapers") == "1" and _field(_rr, "interval") == "3", repr(_rr))
    _live_run(["session", "kill", tgt, "--name", name + "rr1"], timeout=60)
    _live_run(["session", "kill", tgt, "--name", name + "rr2"], timeout=60)

    # E6：PERF（3 个空闲会话：内存 / 进程数 / 空闲 CPU）——SPEC PERF-01..04
    _perf = [name + "p%d" % i for i in (1, 2, 3)]
    for _pn in _perf:
        _live_run(["session", "start", tgt, "--name", _pn, "--ttl", "300"], timeout=90)
    _pscript = ("PIDS=''; for n in %s; do "
                "  TN=$(cat /tmp/pyaissh-sessions/$n/tmux 2>/dev/null); "
                "  P=$(tmux -L pyaissh display-message -p -t \"=$TN:\" '#{pane_pid}' 2>/dev/null); "
                "  PIDS=\"$PIDS $P\"; done; "
                "SRV=$(tmux -L pyaissh display-message -p '#{pid}' 2>/dev/null); "
                "[ -n \"$SRV\" ] || SRV=$(pgrep -f '[t]mux -L pyaissh' | head -1); "
                "RE=$(pgrep -f '[r]eap.sh --loop' | head -1); "
                "ALL=\"$SRV $PIDS $RE\"; "
                "N=0; RSS=0; CPU=0; "
                "for p in $ALL; do [ -n \"$p\" ] || continue; N=$((N+1)); "
                "  RSS=$((RSS + $(awk '/VmRSS/{print $2}' /proc/$p/status 2>/dev/null || echo 0))); "
                "  CPU=$((CPU + $(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null || echo 0))); done; "
                "echo \"procs=$N\"; echo \"rss_kb=$RSS\"; echo \"cpu=$CPU\"") % " ".join(_perf)
    rc, _jp1, _ = _live_run(["exec", tgt, "--cmd", _pscript], timeout=60)
    time.sleep(20)
    rc, _jp2, _ = _live_run(["exec", tgt, "--cmd", _pscript], timeout=60)
    _p1 = (_jp1 or {}).get("stdout") or ""
    _p2 = (_jp2 or {}).get("stdout") or ""
    try:
        _dcpu = int(_field(_p2, "cpu") or 0) - int(_field(_p1, "cpu") or 0)
    except ValueError:
        _dcpu = -1
    _rss_mb = (int(_field(_p1, "rss_kb") or 0) / 1024.0)
    s.check("PERF-01 3 个空闲会话常驻内存 ≤ 25MB（旧引擎实测 45.5MB）",
            _rss_mb > 0 and _rss_mb <= 25.0, "rss=%.1f MB（%s）" % (_rss_mb, _p1))
    s.check("PERF-02/03 常驻进程 ≤ 5（tmux server + 3 shell + reaper）且每会话无辅助进程",
            0 < int(_field(_p1, "procs") or 0) <= 5, "procs=%s" % _field(_p1, "procs"))
    s.check("PERF-04 20 秒空闲 CPU ≤ 2 jiffy（不烧 CPU）",
            0 <= _dcpu <= 2, "cpu_delta=%s jiffy" % _dcpu)
    for _pn in _perf:
        _live_run(["session", "kill", tgt, "--name", _pn], timeout=60)

    # E7：kill 之后不留 reaper（用完即净）
    _live_run(["session", "kill", tgt, "--name", _e1], timeout=60)
    rc, _jz, _ = _live_run(["exec", tgt, "--cmd",
                            "echo -n 'reap='; ps -eo args | grep -c '[r]eap.sh --loop' || true; "
                            "echo -n 'pid='; [ -f /tmp/pyaissh-sessions/.reaper.pid ] && echo 1 || echo 0; "
                            "echo -n 'srv='; tmux -L pyaissh ls 2>/dev/null | wc -l"], timeout=60)
    _e7s = (_jz or {}).get("stdout") or ""
    s.check("E7 会话全清后：reaper 已停、pid 文件已删、tmux server 已退",
            _field(_e7s, "reap") == "0" and _field(_e7s, "pid") == "0"
            and _field(_e7s, "srv") == "0", repr(_e7s))

def suite_live_session_lifecycle(s):
    """会话消亡与账实分离（v2.4.0）：exit / 外部 kill-session / 目录被删 / 只剩 meta 的残留 / 同名重建。"""
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    # L1：会话里 `exit` → tmux 会话消失但目录还在 ⇒ session_dead（不猜、不接管），kill 清目录
    _l1 = name + "l1"
    _live_run(["session", "start", tgt, "--name", _l1, "--ttl", "300"], timeout=90)
    _live_run(["session", "run", tgt, "--name", _l1, "--cmd", "exit", "--no-wait"], timeout=60)
    time.sleep(1.5)
    rc, _je1, _ = _live_run(["session", "read", tgt, "--name", _l1], timeout=60)
    s.check("L1a 会话里 exit 后：read 报 session_dead（rc=2）+ 目录仍在",
            rc == 2 and bool(_je1) and _je1.get("error") == "session_dead"
            and "tmux 会话已消失" in (_je1.get("message") or ""), repr(_je1)[:240])
    rc, _jl1, _ = _live_run(["session", "list", tgt], timeout=60)
    _row1 = next((x for x in (_jl1 or {}).get("sessions", []) if x.get("session") == _l1), None)
    s.check("L1b list 显示 status=dead（账实不一致如实呈现）",
            bool(_row1) and _row1.get("status") == "dead", repr(_row1)[:200])
    rc, _jk1, _ = _live_run(["session", "kill", tgt, "--name", _l1], timeout=120)
    _krow1 = ((_jk1 or {}).get("sessions") or [{}])[0]
    s.check("L1c kill 清掉残留目录，但 verified=false + note（没拿到 pane pid，不谎报清干净）",
            bool(_jk1) and _krow1.get("cleaned") is True and _krow1.get("verified") is False
            and bool(_krow1.get("note")), repr(_krow1)[:240])

    # L2：外部 `tmux kill-session`（模拟人手工关掉）→ 同样 session_dead，不留进程
    _l2 = name + "l2"
    rc, _j2, _ = _live_run(["session", "start", tgt, "--name", _l2, "--ttl", "300"], timeout=90)
    _live_run(["exec", tgt, "--cmd",
               "tmux -L pyaissh kill-session -t '=%s'; echo killed" % _tmux_name(_l2)],
              timeout=60)
    time.sleep(0.8)
    rc, _je2, _ = _live_run(["session", "run", tgt, "--name", _l2, "--cmd", "echo X"],
                           timeout=60)
    s.check("L2a 外部 kill-session 后 run 报 session_dead",
            rc == 2 and bool(_je2) and _je2.get("error") == "session_dead", repr(_je2)[:200])
    rc, _jk2, _ = _live_run(["session", "kill", tgt, "--name", _l2], timeout=120)
    _krow2 = ((_jk2 or {}).get("sessions") or [{}])[0]
    s.check("L2b kill 清理后无残留（目录删了）", bool(_jk2) and _krow2.get("cleaned") is True,
            repr(_krow2)[:200])

    # L3：目录被外部删除、tmux 会话还活着 ⇒ list 仍看得见（running + 目录缺失警告），kill 能收掉
    _l3 = name + "l3"
    rc, _j3, _ = _live_run(["session", "start", tgt, "--name", _l3, "--ttl", "300"], timeout=90)
    _pid3 = (_j3 or {}).get("pid")
    _live_run(["exec", tgt, "--cmd", "rm -rf /tmp/pyaissh-sessions/%s; echo rm" % _l3],
              timeout=60)
    rc, _jl3, _ = _live_run(["session", "list", tgt], timeout=60)
    _row3 = next((x for x in (_jl3 or {}).get("sessions", []) if x.get("session") == _l3), None)
    s.check("L3a 目录被删但会话活着：list 仍列出 running + 目录缺失警告",
            bool(_row3) and _row3.get("status") == "running"
            and any("目录缺失" in w for w in (_jl3.get("warnings") or [])), repr(_jl3)[:260])
    rc, _jk3, _ = _live_run(["session", "kill", tgt, "--name", _l3], timeout=120)
    _krow3 = ((_jk3 or {}).get("sessions") or [{}])[0]
    rc, _jv3, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'proc='; kill -0 %s 2>/dev/null && echo 1 || echo 0; "
                             "echo -n 'sess='; tmux -L pyaissh ls 2>/dev/null "
                             "| grep -c '%s' || true" % (_pid3, _tmux_name(_l3))], timeout=60)
    _v3 = (_jv3 or {}).get("stdout") or ""
    s.check("L3b kill 收掉这种" + "无目录活会话" + "（进程与 tmux 会话都没了）",
            bool(_jk3) and "proc=0" in _v3 and "sess=0" in _v3, repr(_v3))

    # L4：同名重建（kill 后同名字再起必须是新会话）
    _l4 = name + "l4"
    rc, _j4a, _ = _live_run(["session", "start", tgt, "--name", _l4, "--ttl", "300"], timeout=90)
    rc, _j4b, _ = _live_run(["session", "start", tgt, "--name", _l4, "--ttl", "300"], timeout=90)
    s.check("L4a 同名再起 → session_exists（不悄悄接管）",
            bool(_j4b) and _j4b.get("error") == "session_exists", repr(_j4b)[:180])
    _live_run(["session", "kill", tgt, "--name", _l4], timeout=120)
    rc, _j4c, _ = _live_run(["session", "start", tgt, "--name", _l4, "--ttl", "300"], timeout=90)
    rc, _j4d, _ = _live_run(["session", "run", tgt, "--name", _l4, "--cmd", "echo NEWALIVE",
                             "--wait-rc", "15"], timeout=60)
    s.check("L4b kill 后同名重建可用（新 pid，能跑命令）",
            bool(_j4c) and _j4c.get("pid") != (_j4a or {}).get("pid")
            and bool(_j4d) and "NEWALIVE" in (_j4d.get("stdout") or ""),
            "pid=%s→%s" % ((_j4a or {}).get("pid"), (_j4c or {}).get("pid")))
    _live_run(["session", "kill", tgt, "--name", _l4], timeout=120)

    # L5：目录里只有 meta、没有 tmux 会话（会话 `exit` 或被人 kill-session 后的正常残留）——
    #     仍按 session_dead 处理，且不被惰性扫/reaper 收走，用 kill 能清掉
    _lg = name + "lg"
    _live_run(["exec", tgt, "--cmd",
               "D=/tmp/pyaissh-sessions/%s; rm -rf $D; mkdir -m 700 -p $D; "
               "printf '1 200 %%d 5\\n' $(( $(date +%%s) - 600 )) > $D/meta; "
               "printf '%%s\\n' $(( $(date +%%s) - 600 )) > $D/beat; echo made" % _lg],
              timeout=60)
    rc, _je5, _ = _live_run(["session", "read", tgt, "--name", _lg], timeout=60)
    s.check("L5a 只有 meta 没有 tmux 会话 → session_dead（rc=2）+ 提示用 kill 清",
            rc == 2 and bool(_je5) and _je5.get("error") == "session_dead"
            and "session kill" in (_je5.get("message") or ""), repr(_je5)[:260])
    rc, _jl5, _ = _live_run(["session", "list", tgt], timeout=60)
    _row5 = next((x for x in (_jl5 or {}).get("sessions", []) if x.get("session") == _lg), None)
    s.check("L5b list 显示 dead 并提示清理（不再有 legacy_engine 字段）",
            bool(_row5) and _row5.get("status") == "dead"
            and "legacy_engine" not in _row5
            and any("tmux 会话已不存在" in w for w in (_jl5.get("warnings") or [])),
            repr(_jl5)[:260])
    rc, _jex5, _ = _live_run(["exec", tgt, "--cmd",
                              "[ -d /tmp/pyaissh-sessions/%s ] && echo still || echo gone" % _lg],
                             timeout=60)
    s.check("L5c 过期也不被惰性扫/reaper 误删（没有 tmux 名文件 ⇒ 不碰）",
            "still" in ((_jex5 or {}).get("stdout") or ""), repr((_jex5 or {}).get("stdout")))
    rc, _jk5, _ = _live_run(["session", "kill", tgt, "--name", _lg], timeout=120)
    _krow5 = ((_jk5 or {}).get("sessions") or [{}])[0]
    s.check("L5d kill 能清掉它（cleaned=true，且不再标 legacy_engine）",
            bool(_jk5) and _krow5.get("cleaned") is True
            and "legacy_engine" not in _krow5, repr(_krow5)[:240])

def suite_live_session_orphan(s):
    """孤儿字段（v2.4.0）：tmux 引擎下结构性孤儿不复存在，`orphans*` **恒返回且恒空**；
    `kill --all` 幂等、且能收掉"目录被外部删掉、只剩 tmux 会话"的活会话。"""
    if _missing_env(_REQ_EXEC):
        print("SKIP: 需配置 %s" % " / ".join(_REQ_EXEC))
        return None
    tgt = os.environ["PYAISSH_TEST_HOST"]
    name = "ts1"
    _live_run(["session", "kill", tgt, "--name", name], timeout=120)
    _live_run(["session", "kill", tgt, "--all"], timeout=120)

    # O1：空主机上的 kill --all：字段齐、计数 0
    rc, _j0, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    s.check("O1a 空主机 kill --all：ok + count=0 + 恒空孤儿三字段",
            bool(_j0) and _j0.get("ok") is True and _j0.get("count") == 0
            and _j0.get("orphans") == [] and _j0.get("orphans_total") == 0
            and _j0.get("orphan_remaining_total") == 0, repr(_j0)[:220])

    # O2：正常会话的 kill：孤儿字段依然在（且空）——AI 侧零感知
    _o2 = name + "o2"
    _live_run(["session", "start", tgt, "--name", _o2, "--ttl", "300"], timeout=90)
    _live_run(["session", "send", tgt, "--name", _o2, "--cmd", "sleep 200 &"], timeout=60)
    time.sleep(0.8)
    rc, _j2, _ = _live_run(["session", "kill", tgt, "--name", _o2], timeout=120)
    _r2 = ((_j2 or {}).get("sessions") or [{}])[0]
    s.check("O2a kill 扫到会话进程树（含后台作业，swept>=2）+ 无残留 + verified",
            bool(_j2) and (_r2.get("swept") or 0) >= 2 and _j2.get("remaining_total") == 0
            and _r2.get("verified") is True, repr(_r2)[:220])
    s.check("O2b 孤儿字段恒空（不再是 argv 扫描的产物）",
            _j2.get("orphans") == [] and _j2.get("orphans_total") == 0
            and _j2.get("orphan_remaining_total") == 0, repr(_j2)[:200])

    # O3：目录被外部删掉、只剩 tmux 会话 ⇒ kill --all 仍要收掉它（不能"看不见就留着"）
    _o3 = name + "o3"
    rc, _j3, _ = _live_run(["session", "start", tgt, "--name", _o3, "--ttl", "300"], timeout=90)
    _pid3 = (_j3 or {}).get("pid")
    _live_run(["exec", tgt, "--cmd", "rm -rf /tmp/pyaissh-sessions/%s; echo rm" % _o3],
              timeout=60)
    rc, _ja3, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    rc, _jv3, _ = _live_run(["exec", tgt, "--cmd",
                             "echo -n 'proc='; kill -0 %s 2>/dev/null && echo 1 || echo 0; "
                             "echo -n 'sess='; tmux -L pyaissh ls 2>/dev/null "
                             "| grep -c '%s' || true" % (_pid3, _tmux_name(_o3))], timeout=60)
    _v3 = (_jv3 or {}).get("stdout") or ""
    s.check("O3 kill --all 收掉" + "无目录活会话" + "（进程与 tmux 会话都没了）",
            bool(_ja3) and "proc=0" in _v3 and "sess=0" in _v3, repr(_v3))
    s.check("O3 孤儿字段仍恒空", _ja3.get("orphans") == [] and _ja3.get("orphans_total") == 0,
            repr(_ja3)[:200])

    # O4：幂等 —— 反复 kill --all 不报错、不留东西
    rc, _j4a, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    rc, _j4b, _ = _live_run(["session", "kill", tgt, "--all"], timeout=120)
    rc, _jj4, _ = _live_run(["session", "kill", tgt, "--name", name + "never"], timeout=120)
    s.check("O4 kill --all 幂等（连做两次都 ok、remaining_total=0）",
            bool(_j4a) and bool(_j4b) and _j4b.get("ok") is True
            and _j4b.get("remaining_total") == 0, repr(_j4b)[:200])
    s.check("O4 对不存在的会话 kill 也不报错（清理是幂等的）",
            bool(_jj4) and _jj4.get("ok") is True, repr(_jj4)[:200])

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

    # B1：会话长等待不被 SFTP/回收误杀（>30s）
    rc, jb1s, _ = _live_run(["session", "start", tgt, "--name", name + "b1"], timeout=90)
    _t1 = time.time()
    rc, jb1, _ = _live_run(["session", "run", tgt, "--name", name + "b1", "--cmd",
                            "sleep 32; echo SLEPT32", "--wait-rc", "60"], timeout=180)
    _dt1 = time.time() - _t1
    s.check("B1 会话长等待 >30s 不被误杀（sleep 32 跑完）",
            bool(jb1) and jb1.get("status") == "done" and jb1.get("exit_code") == 0
            and "SLEPT32" in (jb1.get("stdout") or ""),
            "%.1fs status=%s" % (_dt1, (jb1 or {}).get("status")))
    _live_run(["session", "kill", tgt, "--name", name + "b1"], timeout=60)

    # B2（v2.4.0 收口）：`--no-pty` 参数已彻底删除（旧引擎的非 PTY 降级路径随之消失）——
    #     传它必须被 argparse 当场拒绝（rc=2），不能静默忽略
    rc, jbnp, errnp = _live_run(["session", "start", tgt, "--name", name + "np", "--no-pty"],
                                timeout=90)
    s.check("B2 --no-pty 已被删除：argparse 直接拒绝（rc=2，不静默接受）",
            rc == 2 and "no-pty" in (errnp or ""), "rc=%s stderr=%r" % (rc, (errnp or "")[-120:]))
    rc, jbnp2, _ = _live_run(["session", "start", tgt, "--name", name + "np"], timeout=90)
    s.check("B2 不带 --no-pty 的普通会话仍正常（pty=True + ready）",
            bool(jbnp2) and jbnp2.get("pty") is True and jbnp2.get("ready") is True,
            repr(jbnp2)[:180])
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
    # v2.4.0：live_session 按"改动路径"拆块——开发时只跑动过的那块（`--suite live_session_engine`），
    # 全量（--session / --all）仍是这些块全跑（发布前用）
    ("live_session_ttl", "会话空闲回收（惰性扫 + reaper）/attach（真机）", suite_live_session_ttl),
    ("live_session_engine", "tmux 引擎（socket 隔离/输出镜像/无残留/开销，真机）", suite_live_session_engine),
    ("live_session_lifecycle", "会话消亡/外部删目录/残留目录/同名重建（真机）", suite_live_session_lifecycle),
    ("live_session_orphan", "孤儿字段恒空 + kill --all 幂等（真机）", suite_live_session_orphan),
    ("live_session_bugs", "B1~B5 回归护栏（真机）", suite_live_session_bugs),
]

#: `--session` 展开成哪些套件（保持"全部会话用例"的语义）
_SESSION_SUITES = ["live_session", "live_session_ttl", "live_session_engine",
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
                                   "例如 --suite live_session_engine,live_session_lifecycle")
    ap.add_argument("--fast", action="store_true",
                    help="快跑：把每主机 reaper 的检查间隔调到 %ds（PYAISSH_SESSION_REAP_INTERVAL），"
                         "把「等 reaper 收会话」的等待按比例缩短（默认 300s，真实生产值）。"
                         "注：回收主路径是惰性扫（任何会话命令都触发），多数回收用例本来就只需几秒"
                         % _FAST_REAP)
    ap.add_argument("--release", action="store_true",
                    help="全量/整个模式（--all / --session）需显式确认：加本参数，"
                         "或设 PYAISSH_TEST_RELEASE=1")
    ap.add_argument("--list", action="store_true", help="列出测试集")
    a = ap.parse_args()

    _release = a.release or os.environ.get("PYAISSH_TEST_RELEASE") == "1"
    if (a.all or a.session) and not _release:
        print("需要 --release 才会跑全量/整个模式（--all / --session）。\n"
              "  全量：   python -u tests/run_tests.py --all --release\n"
              "  单块：   python -u tests/run_tests.py --suite <块名>   （--list 看块名）")
        return 2

    if a.list:
        print("  套件（--suite 用名字选）：")
        for i, (name, desc, _) in enumerate(SUITES, 1):
            print("  %2d) %-26s %s" % (i, name, desc))
        print("\n  常用组合：")
        print("   开发（只跑动过的路径，示例）：--suite live_session_engine,live_session_lifecycle --fast")
        print("   发布前全量：                    --all")
        return 0
    if a.fast:
        os.environ["PYAISSH_SESSION_REAP_INTERVAL"] = str(_FAST_REAP)
        globals()["_REAP"] = _FAST_REAP
        print("[fast] reaper 间隔=%ds（默认 300s），等待按比例缩短" % _FAST_REAP)
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
            print("需要 --release 才会跑全量（--all）。单块：--suite <块名>（--list 看块名）。")
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
