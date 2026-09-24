"""常驻会话子命令实现（域 11）——真 PTY 会话：逐条喂命令 + 状态保留 + 可中断。

为什么需要它（与 exec / exec --detach 的分工）：
- `exec`：一条命令一次调用，**无状态**（cd/export 不跨调用保留），受宿主单次调用时长限制
- `exec --detach`：一条长命令丢后台，**启动后不能改**，错了只能 --kill 重启
- `session`：远端一个常驻 shell（真 PTY），**逐条喂命令**——每条独立退出码，
  **打错了就把那条命令改对再发一遍**（同一条重试，不是换下一条）：报错 → 改名/改参数 → 重发 → 成功，
  像人在终端里那样；cd/export/函数等状态都在，所以重发时上下文与上次完全一致；执行中的命令**可中断**（ctrl-c）

实测依据（v2.3 开发期真机验证，详见 docs/session.md）：
- **引擎 = tmux**（v2.4.0 起，见 `pyaissh-dev/SPEC_session_tmux.md`）：PTY、进程生命周期、
  输出镜像（`pipe-pane`）都归 tmux；pyaissh 只保留契约层（哨兵/退出码/字节级 offset/
  清洗/JSON 字段）。旧引擎（setsid + nohup + script + FIFO + 自研看门狗）整体删除。
- 为什么换：旧引擎的复杂度几乎全在"自己实现终端"——FIFO 写入的阻塞/半行、看门狗的
  判闲与自杀式清理、argv 扫孤儿的自证、pid 复用误杀……这些在 tmux 里都是现成的、
  被千万台机器验证过的行为。换掉后每个会话不再需要常驻辅助进程（每主机一个 reaper）。
- 关键实测（S1~S13，SPEC 里逐条有命令与结论）：tmux 会**静默改写会话名里的 `.`/`:`**
  ⇒ 名字映射成 `py-<hex>`；pane 级 target 必须写 `=NAME:`；`load-buffer`+`paste-buffer`
  灌命令（零引号风险、二进制安全）；`send-keys C-c` 即 Ctrl-C；`--force` 用内核给的
  前台进程组 `tpgid` 发 SIGKILL（不用会 core dump 的 SIGQUIT）；`out.log` 被外部删除后
  管道会继续往已 unlink 的 inode 写 ⇒ 探测时**不带 `-o`** 重新 arm。
"""

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")  # 防路径穿越
_SESSION_RC_RE = re.compile(re.escape(SESSION_RC_PREFIX) + r"([0-9a-f]{4,16})__(\d{1,3})")
# 远端标记统一 `__PYAISSH_SESS__KEY=VALUE`（v2.3：早期写成 `__KEY__VALUE__KEY2__VALUE2`，
# 贪婪匹配会把 PID 吃成 "63864__PTY__1"——实测踩到）
_SESSION_MARK_RE = re.compile(r"__PYAISSH_SESS__([A-Z_]+)=(\S*)")

# list 里对"挂了超过这个时长还活着"的会话给一条提醒（用久了忘 kill 的护栏）
_SESSION_STALE_HINT = 86400

# 空闲回收（v2.3.0 用户设计；v2.4.0 起由 tmux 引擎承载）：会话是远端常驻进程，**不会自己退出**；
# 但"没人用的会话"不该白占远端资源。规则（两个条件同时成立才回收）：
#   ① 提示符空闲——没有前台命令在跑（tmux 的 `#{pane_current_command}` 是 shell 本身），
#      所以构建/安装不会被误杀；**判不出就视为忙**（宁可不收也不误杀）；
#   ② 距上次 pyaissh 交互（beat 文件里是 **epoch 秒**）超过 TTL。
# 交互即续期：send/run/read/ctrl-c/keys/start(attach) 都刷新 beat；`list` 不算（看一眼≠在用）。
# `--ttl 0` 关闭回收；环境变量 PYAISSH_SESSION_TTL 改默认值。
# 回收有两个入口（见 SPEC_session_tmux.md REAP）：
#   ① **惰性扫**：`_session_load`（send/run/read/ctrl-c/keys 的前置）与 `list` 会先给目标续期、
#      再扫一遍其它会话；`kill` 不需要扫（它本来就是清理）；
#   ② **每主机 reaper**：`<root>/reap.sh --loop` 每 `_SESSION_REAP_INTERVAL` 秒扫一遍，
#      无会话目录时下一轮自退；由 `start`（仅 --ttl>0）幂等拉起，会话全清后由 `kill` 停掉。
# 所以"没人再回来"的会话最迟在 TTL + 间隔内被收掉。
_SESSION_TTL_DEFAULT = 600


def _session_reap_interval():
    """reaper 常驻循环的检查间隔（秒，**整数**）。默认 300；`PYAISSH_SESSION_REAP_INTERVAL` 可覆盖
    （**测试用**：设 3 可让"等 reaper 收会话"的用例快 100 倍；生产别乱调——越短 fork 越多）。
    非法值（非数字/小于 1/大于 86400）只打 WARN 并回落默认 300。
    """
    raw = (os.environ.get("PYAISSH_SESSION_REAP_INTERVAL") or "").strip()
    if not raw:
        return SESSION_TMUX_LOOP_INTERVAL
    try:
        v = int(raw)
    except ValueError:
        log("[WARN] PYAISSH_SESSION_REAP_INTERVAL 值 %r 非整数，用默认 %d"
            % (raw, SESSION_TMUX_LOOP_INTERVAL))
        return SESSION_TMUX_LOOP_INTERVAL
    if v < 1 or v > 86400:
        log("[WARN] PYAISSH_SESSION_REAP_INTERVAL 值 %r 超范围（1~86400 秒），用默认 %d"
            % (raw, SESSION_TMUX_LOOP_INTERVAL))
        return SESSION_TMUX_LOOP_INTERVAL
    return v


def _session_files(root, name):
    """会话远端路径表（一个会话一个 0700 目录）。

    tmux 引擎只用这五个文件：`out.log`(输出镜像) / `meta`(pty cols 起始时间 TTL) /
    `beat`(最后交互 epoch 秒) / `last.token`(最近一条命令的 token) / `tmux`(tmux 会话名)。
    """
    d = "%s/%s" % (root.rstrip("/"), name)
    return {"dir": d, "log": d + "/out.log", "meta": d + "/meta",
            "token": d + "/last.token", "beat": d + "/beat", "tmux": d + "/tmux",
            "name": name, "root": root.rstrip("/")}


def _session_parse_ttl(raw, default=None):
    """`--ttl` / `PYAISSH_SESSION_TTL` 解析：纯数字=秒，或带后缀 `30s`/`10m`/`2h`；0=关闭。

    返回 (秒数|None, 错误消息|None)。None 表示用默认值（调用方决定）。
    """
    base = _SESSION_TTL_DEFAULT if default is None else default
    if raw is None or str(raw).strip() == "":
        return base, None
    s = str(raw).strip().lower()
    mult = 1
    if s and s[-1] in ("s", "m", "h"):
        mult = {"s": 1, "m": 60, "h": 3600}[s[-1]]
        s = s[:-1]
    if not s.isdigit():
        return None, "无法解析的 TTL %r（用秒数或 30s/10m/2h 形式；0 = 关闭空闲回收）" % (raw,)
    return int(s) * mult, None


def _session_check_name(use_json, name):
    if not name or not _SESSION_NAME_RE.match(name):
        emit_error(use_json, "bad_args",
                   "非法会话名 %r（只允许字母/数字/下划线/点/连字符，首字符须字母或数字，"
                   "最长 32，防路径穿越）" % (name,))
        return False
    return True


def _session_unescape(s):
    """`--data 'y\\n'` 的转义解析：\\n \\r \\t \\0 \\\\ \\xNN（其它原样保留）。"""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "n":
                out.append("\n"); i += 2; continue
            if n == "r":
                out.append("\r"); i += 2; continue
            if n == "t":
                out.append("\t"); i += 2; continue
            if n == "\\":
                out.append("\\"); i += 2; continue
            if n == "0":
                out.append("\0"); i += 2; continue
            if n == "x" and i + 3 < len(s) + 1 and len(s) >= i + 4:
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16))); i += 4; continue
                except ValueError:
                    pass
        out.append(c); i += 1
    return "".join(out)


def _session_run(client, cmd, stdin_data=None, timeout=30):
    """跑一条远端辅助命令，返回 (rc, stdout, stderr)；可选把**字节**写进它的 stdin。

    两处用到 stdin：tmux `load-buffer -`（命令/按键载荷，原样字节）与 base64 传输。
    辅助命令输出量都很小，直接 read() 不会死锁。
    """
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    if stdin_data is not None:
        try:
            stdin.write(stdin_data)
            stdin.flush()
        except Exception:
            pass
    try:
        stdin.channel.shutdown_write()
    except Exception:
        pass
    out = stdout.read()
    err = stderr.read()
    rc = stdout.channel.recv_exit_status()
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# ---------------------------------------------------------------- tmux 引擎
# 设计决策与实测依据见 `pyaissh-dev/SPEC_session_tmux.md`（spike 事实 S1~S13）。
# 一句话：**tmux 只当引擎**（PTY + 进程生命周期 + 输出镜像），pyaissh 的契约层
# （哨兵 / 每条命令独立退出码 / 字节级 offset 读 / CRLF·ANSI 清洗 / JSON 字段）原样保留。
# 依赖：远端 tmux ≥ 3.0（实测 3.5a / Debian 13）；没有则 session 不可用并给出安装命令。
SESSION_TMUX_SOCKET = "pyaissh"        # 专用 socket：与用户自己的 tmux 完全隔离
SESSION_TMUX_PREFIX = "py-"            # 会话名映射前缀（见 _session_tmux_name）
SESSION_TMUX_MIN_MAJOR = 3             # 主版本下限（依赖 window-size manual 与 #{pane_pipe}）
SESSION_TMUX_BUFFER = "pyaissh-buf"    # load-buffer/paste-buffer 的缓冲名
SESSION_TMUX_LOOP_INTERVAL = 300       # reaper --loop 的检查间隔（秒）
SESSION_TMUX_REAP_LOG_MAX = 65536      # .reaper.log 超过此大小就截断（只留尾 200 行）
SESSION_DEFAULT_LANG = "C.UTF-8"       # 通道里没有 LANG/LC_ALL 时给 pane 兜底的 UTF-8 locale
_SESSION_TMUX = "tmux -L %s -f /dev/null" % SESSION_TMUX_SOCKET


def _tpl(s, **kw):
    """`@@KEY@@` 占位替换（bash 脚本模板专用：避开 `%` 与 `{}` 在 shell 文本里的坑）。"""
    for k, v in kw.items():
        s = s.replace("@@%s@@" % k, str(v))
    return s


def _session_tmux_name(name):
    """pyaissh 会话名 → tmux 会话名（纯函数，便于单测）。

    为什么不能直接用原名：实测（S1）tmux 会把名字里的 `.`/`:` **静默改写成 `_`**——
    `a.b` 建出来的会话在 `ls` 里叫 `a_b`，既与真实名字 `a_b` 撞名，又让 `=NAME` 精确匹配失效。
    `py-<utf-8 hex>` 可逆、无碰撞、无 tmux 特殊字符。
    """
    return SESSION_TMUX_PREFIX + name.encode("utf-8").hex()


def _session_tmux_decode(tmux_name):
    """tmux 会话名 → pyaissh 会话名（不是本工具建的返回 None）。"""
    if not tmux_name or not tmux_name.startswith(SESSION_TMUX_PREFIX):
        return None
    h = tmux_name[len(SESSION_TMUX_PREFIX):]
    if not h or len(h) % 2:
        return None
    try:
        nm = bytes.fromhex(h).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return nm if _SESSION_NAME_RE.match(nm) else None


def _session_tmux_pair(name):
    """返回 (tmux 会话名, 会话级 target, pane 级 target)。

    实测（S2）：pane 级命令（`pipe-pane`/`paste-buffer`/`display-message`/`send-keys`）
    必须用 `=NAME:`，只给 `=NAME` 会报 `can't find pane`；而 `has-session`/`kill-session`
    用 `=NAME`。`=` 前缀 = 精确匹配（防前缀误配）。
    """
    tn = _session_tmux_name(name)
    return tn, "=" + tn, "=" + tn + ":"


# 所有远端操作共用的前置：tmux 存在性 + 版本闸门（不满足就打标记后 exit 0，
# 由 Python 统一翻译成 tmux_missing / tmux_unsupported / tmux_failed）。
_SESSION_TMUX_PROLOGUE = (
    "command -v tmux >/dev/null 2>&1 || { echo __PYAISSH_SESS__TMUX=missing; exit 0; }; "
    "TMV=$(tmux -V 2>/dev/null); TMV=${TMV#tmux }; MAJ=${TMV%%.*}; "
    "case \"$MAJ\" in ''|*[!0-9]*) echo __PYAISSH_SESS__TMUX=badver; "
    "echo \"__PYAISSH_SESS__TMUXV=$TMV\"; exit 0 ;; esac; "
    "[ \"$MAJ\" -ge " + str(SESSION_TMUX_MIN_MAJOR) + " ] || { echo __PYAISSH_SESS__TMUX=oldver; "
    "echo \"__PYAISSH_SESS__TMUXV=$TMV\"; exit 0; }; "
    "TM=\"" + _SESSION_TMUX + "\"; "
)


def _session_tmux_gate(use_json, marks, extra=None):
    """tmux 预检闸门：不满足就发错误并返回退出码；满足返回 None。

    三种失败各自的 message 都**可直接执行**（装/升级 tmux 的确切命令），因为
    session 模式现在显式依赖 tmux（用户 2026-09-24 决定）；装不了的环境用
    `exec` / `exec --detach` 跑长任务（UPG-02）。
    """
    v = marks.get("TMUX")
    if v in (None, "ok"):
        return None
    ver = marks.get("TMUXV") or "?"
    ex = dict(extra or {})
    if v == "missing":
        emit_error(use_json, "tmux_missing",
                   "远端没有 tmux：session 模式依赖 tmux（≥ %d.0）提供 PTY 与进程生命周期。"
                   "装一个即可：Debian/Ubuntu `apt-get install -y tmux`｜"
                   "RHEL/CentOS `dnf install -y tmux`｜Alpine `apk add tmux`；"
                   "装不了（无包管理器/不可变系统）请改用 `exec` 或 `exec --detach` 跑长任务。"
                   % SESSION_TMUX_MIN_MAJOR,
                   extra=dict(ex, install_hint="apt-get install -y tmux",
                              required="tmux >= %d.0" % SESSION_TMUX_MIN_MAJOR))
        return 255
    if v == "oldver":
        emit_error(use_json, "tmux_unsupported",
                   "远端 tmux 版本过低（%s，需要 ≥ %d.0）：本引擎依赖 `window-size manual` "
                   "与 `#{pane_pipe}`。升级后重试（`apt-get install -y --only-upgrade tmux`）。"
                   % (ver, SESSION_TMUX_MIN_MAJOR),
                   extra=dict(ex, tmux_version=ver,
                              required="tmux >= %d.0" % SESSION_TMUX_MIN_MAJOR))
        return 255
    emit_error(use_json, "tmux_failed", "无法解析远端 tmux 版本（`tmux -V` → %r）" % (ver,),
               extra=dict(ex, tmux_version=ver))
    return 255


_SESSION_LS_TPL = """\
@@PROLOGUE@@
$TM ls -F '#{session_name}|#{pane_pid}|#{pane_current_command}|#{pane_pipe}|#{pane_tty}|#{session_created}|#{pane_width}|#{pane_height}' 2>/dev/null || echo __PYAISSH_SESS__NOSERVER=1
"""


def _session_ls_cmd():
    """列本 socket 下所有 tmux 会话（带格式串）。没有 server 时打 NOSERVER 标记。"""
    return _tpl(_SESSION_LS_TPL, PROLOGUE=_SESSION_TMUX_PROLOGUE)


def _session_tmux_ls(client, timeout=20):
    """一次 `tmux ls` → ({pyaissh 名: 会话信息}, 标记字典)。

    名字反解失败（不是 `py-<hex>` 或 hex 非法）的一律无视——同一个 socket 上
    只可能是本工具建的会话，但防御性过滤比误操作强。
    """
    rc, out, _err = _session_run(client, _session_ls_cmd(), timeout=timeout)
    marks = dict(_SESSION_MARK_RE.findall(out))
    sessions = {}

    def _i(x):
        return int(x) if (x or "").isdigit() else None

    for ln in (out or "").splitlines():
        parts = ln.strip().split("|")
        if len(parts) < 5 or not parts[0].startswith(SESSION_TMUX_PREFIX):
            continue
        nm = _session_tmux_decode(parts[0])
        if not nm:
            continue
        sessions[nm] = {"tmux": parts[0], "pane_pid": _i(parts[1]),
                        "cur": (parts[2] or None), "pipe": (parts[3] or ""),
                        "tty": (parts[4] or None),
                        "created": _i(parts[5]) if len(parts) > 5 else None,
                        "cols": _i(parts[6]) if len(parts) > 6 else None}
    return sessions, marks


_SESSION_START_TPL = """\
@@PROLOGUE@@
D='@@DIR@@'
mkdir -p "$D" 2>/dev/null && chmod 700 "$D" 2>/dev/null || { echo __PYAISSH_SESS__MKDIR_FAIL=1; exit 0; }
TN='@@TMUX@@'
if $TM has-session -t "$TN" 2>/dev/null; then
  echo __PYAISSH_SESS__EXISTS=1
  echo "__PYAISSH_SESS__PID=$($TM display-message -p -t "$TN:" '#{pane_pid}' 2>/dev/null)"
  echo "__PYAISSH_SESS__EXTTL=$(@@TTLREAD@@)"
  exit 0
fi
@@SETENV@@
rm -f '@@LOG@@' 2>/dev/null
: > '@@LOG@@' 2>/dev/null; chmod 600 '@@LOG@@' 2>/dev/null
printf '%s\\n' "$(date +%s)" > '@@BEAT@@' 2>/dev/null; chmod 600 '@@BEAT@@' 2>/dev/null
printf '%s' '@@TMUX@@' > '@@TMFILE@@' 2>/dev/null; chmod 600 '@@TMFILE@@' 2>/dev/null
printf '%s %s %s %s\\n' 1 @@COLS@@ "$(date +%s)" @@TTL@@ > '@@META@@' 2>/dev/null; chmod 600 '@@META@@' 2>/dev/null
$TM new-session -d -s "$TN" -x @@COLS@@ -y 50 'bash -i' 2>/dev/null || { echo __PYAISSH_SESS__NEW_FAIL=1; exit 0; }
$TM set-window-option -t "$TN:" window-size manual 2>/dev/null
$TM pipe-pane -o -t "$TN:" 'cat >> @@LOG@@' 2>/dev/null || echo __PYAISSH_SESS__PIPE_FAIL=1
sleep 0.3
echo "__PYAISSH_SESS__PID=$($TM display-message -p -t "$TN:" '#{pane_pid}' 2>/dev/null)"
echo __PYAISSH_SESS__CREATED=1
"""


def _session_start_cmd(f, tmux, cols, ttl, env_items):
    """起会话的远端脚本：目录/记账文件 → 环境注入 → new-session → window-size → pipe-pane。

    环境注入必须在 `new-session` **之前**（tmux server 的环境在 server 启动时冻结，
    新会话继承 server 的全局环境）。实测（S12）：tmux 仍会强制 pane 的 `TERM`
    （`tmux-256color`），这个我们不干预——它比继承来的空 TERM 更准确。
    """
    setenv = "".join("$TM set-environment -g %s %s 2>/dev/null; " % (_sh_quote(k), _sh_quote(v))
                     for k, v in env_items)
    return _tpl(_SESSION_START_TPL,
                PROLOGUE=_SESSION_TMUX_PROLOGUE,
                DIR=f["dir"], LOG=f["log"], BEAT=f["beat"], META=f["meta"],
                TMUX=tmux, TMFILE=f["tmux"], COLS=int(cols), TTL=int(ttl or 0),
                SETENV=setenv,
                TTLREAD="awk '{print $4}' '%s' 2>/dev/null" % f["meta"])


def _session_touch(client, f):
    """刷新会话的"最后交互时间"——beat 里写 **epoch 秒**（空闲回收据此判"没人用了"）。

    绝大多数命令的续期由 `_session_probe_cmd` 顺带完成（同一次 exec，不额外往返）；
    本函数只给 `start --attach` 用。失败不致命（只记一条 stderr WARN）。
    """
    rc, out, err = _session_run(client, _tpl(
        "printf '%s\\n' \"$(date +%s)\" > @@BEAT@@ 2>/dev/null; "
        "chmod 600 @@BEAT@@ 2>/dev/null", BEAT=_sh_quote(f["beat"])), timeout=10)
    if rc != 0 and (err or "").strip():
        log("[WARN] 刷新会话活动时间失败（不影响本次调用）：%s" % err.strip()[:160])
    return rc == 0


_SESSION_PROBE_TPL = """\
@@PROLOGUE@@
TN='@@TMUX@@'
if $TM has-session -t "$TN" 2>/dev/null; then
  echo __PYAISSH_SESS__ALIVE=1
  echo "__PYAISSH_SESS__PID=$($TM display-message -p -t "$TN:" '#{pane_pid}' 2>/dev/null)"
  echo "__PYAISSH_SESS__TTY=$($TM display-message -p -t "$TN:" '#{pane_tty}' 2>/dev/null)"
  echo "__PYAISSH_SESS__CUR=$($TM display-message -p -t "$TN:" '#{pane_current_command}' 2>/dev/null)"
  PP=$($TM display-message -p -t "$TN:" '#{pane_pipe}' 2>/dev/null)
  echo "__PYAISSH_SESS__PIPE=$PP"
  if [ -f '@@LOG@@' ]; then
    [ "$PP" = 1 ] || $TM pipe-pane -o -t "$TN:" 'cat >> @@LOG@@' 2>/dev/null
  else
    $TM pipe-pane -t "$TN:" 'cat >> @@LOG@@' 2>/dev/null && echo __PYAISSH_SESS__LOG_REARM=1
  fi
  @@TOUCH@@
else
  echo __PYAISSH_SESS__GONE=1
fi
"""


def _session_probe_cmd(f, tmux, touch=True):
    """会话探测（ALIVE/GONE + pane pid/tty/前台命令/管道状态），可选顺手续期。

    顺带自愈两件在服务器上可能发生的事（都实测过）：
      - 管道死了（`#{pane_pipe}` != 1）⇒ `-o` 重新 arm（已有管道时是 no-op）；
      - `out.log` 被外部删了（S5：管道还活着但在往已 unlink 的 inode 写，输出会静默丢）
        ⇒ **不带 `-o`** 重新 arm，让 `cat >>` 重建文件；上层据此给 `log_recreated` 提示。
    """
    touch_cmd = ("printf '%s\\n' \"$(date +%s)\" > '@@BEAT@@' 2>/dev/null; "
                 "chmod 600 '@@BEAT@@' 2>/dev/null")
    return _tpl(_SESSION_PROBE_TPL, PROLOGUE=_SESSION_TMUX_PROLOGUE, TMUX=tmux,
                LOG=f["log"], BEAT=f["beat"],
                TOUCH=(_tpl(touch_cmd, BEAT=f["beat"]) if touch else ":"))


_SESSION_PASTE_TPL = """\
@@PROLOGUE@@
TN='@@TMUX@@'
if ! $TM has-session -t "$TN" 2>/dev/null; then echo __PYAISSH_SESS__GONE=1; exit 0; fi
$TM load-buffer -b @@BUF@@ - 2>/dev/null || { echo __PYAISSH_SESS__LOAD_FAIL=1; exit 0; }
$TM paste-buffer -b @@BUF@@ -d -t "$TN:" 2>/dev/null || { echo __PYAISSH_SESS__PASTE_FAIL=1; exit 0; }
echo __PYAISSH_SESS__SENT=1
"""


def _session_paste_cmd(f, tmux, buffer_name=None):
    """把 **stdin 的字节**灌进会话（命令载荷与 `keys` 共用这一条路径）。

    为什么用 `load-buffer` + `paste-buffer` 而不是 `send-keys -l`：tmux 命令行会被它
    自己再解析一遍（`;`、`#{`、引号都是它的语法），缓冲区内容则是纯数据、零引号风险，
    而且二进制安全（`keys --cmd-file` 可以喂任意字节）。`-d` 用完即删缓冲，不在 server 里堆积。
    """
    return _tpl(_SESSION_PASTE_TPL, PROLOGUE=_SESSION_TMUX_PROLOGUE, TMUX=tmux,
                BUF=(buffer_name or SESSION_TMUX_BUFFER))


_SESSION_CTRL_C_TPL = """\
@@PROLOGUE@@
TN='@@TMUX@@'
if ! $TM has-session -t "$TN" 2>/dev/null; then echo __PYAISSH_SESS__GONE=1; exit 0; fi
PP=$($TM display-message -p -t "$TN:" '#{pane_pid}' 2>/dev/null)
echo "__PYAISSH_SESS__PID=$PP"
echo "__PYAISSH_SESS__SID=$PP"
case "$PP" in ''|*[!0-9]*) echo __PYAISSH_SESS__NOPID=1; echo __PYAISSH_SESS__SIGNALED=0; echo __PYAISSH_SESS__DONE=1; exit 0;; esac
FG=$(ps -o tpgid= -p "$PP" 2>/dev/null | tr -d ' ')
CH=''; G=''; N=0
if [ -n "$FG" ] && [ "$FG" != "$PP" ]; then
  G="$FG"
  CH=$(ps -eo pid=,pgid= 2>/dev/null | awk -v g="$FG" -v me="$PP" '$2==g && $1!=me {printf "%s ", $1}')
  @@ACTION@@
fi
echo "__PYAISSH_SESS__GROUPS=$G"
echo "__PYAISSH_SESS__CHILDREN=$CH"
echo "__PYAISSH_SESS__SIGNALED=$N"
echo __PYAISSH_SESS__DONE=1
"""


def _session_ctrl_c_cmd(f, tmux, force=False):
    """中断会话里正在执行的命令。

    实测（S8/S9）：`send-keys C-c` 由 pane 的 tty 行规程把 SIGINT 送到**前台进程组**，
    这正是 Ctrl-C 的语义（`pane_current_command` 从 sleep 回到 bash）。
    `--force` 走内核给出的前台进程组：`ps -o tpgid= -p <pane_pid>` + `kill -KILL -- -PGID`，
    只杀那个作业、**会话与状态保留**（比 `C-\\`(SIGQUIT) 干净：不会 core dump 出大文件）。
    """
    action = ('kill -KILL -- "-$FG" 2>/dev/null && N=1 || N=0' if force
              else '$TM send-keys -t "$TN:" C-c 2>/dev/null && N=1 || N=0')
    return _tpl(_SESSION_CTRL_C_TPL, PROLOGUE=_SESSION_TMUX_PROLOGUE, TMUX=tmux, ACTION=action)


_SESSION_KILL_TPL = """\
@@PROLOGUE@@
D='@@DIR@@'; HAD=0; [ -d "$D" ] && HAD=1
TN='@@TMUX@@'; T=''; LEFT=0; SWEPT=0; ROOTS=0; KILLED=0
P='@@PANE_PID@@'
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then
  SNAP=$(ps -eo pid=,ppid= 2>/dev/null)
  T=$(echo "$SNAP" | @@AWK@@)
  T=$(echo $T | tr ' ' '\\n' | sort -u -n | tr '\\n' ' ')
  SWEPT=$(echo $T | wc -w | tr -d ' ')
  [ "$SWEPT" -gt 0 ] && ROOTS=1
fi
$TM kill-session -t "$TN" 2>/dev/null && KILLED=1
i=0
while $TM has-session -t "$TN" 2>/dev/null && [ "$i" -lt 20 ]; do sleep 0.1; i=$((i+1)); done
if $TM has-session -t "$TN" 2>/dev/null; then echo __PYAISSH_SESS__STILL=1; fi
if [ -n "$T" ]; then
  kill -TERM $T 2>/dev/null; sleep 0.5
  K=''; for p in $T; do kill -0 "$p" 2>/dev/null && K="$K $p"; done
  if [ -n "$K" ]; then kill -KILL $K 2>/dev/null; sleep 0.3; fi
  for p in $T; do kill -0 "$p" 2>/dev/null && LEFT=$((LEFT+1)); done
fi
rm -f '@@TMFILE@@' '@@TOKEN@@' 2>/dev/null
@@RM@@
echo "__PYAISSH_SESS__SWEPT=$SWEPT"
echo "__PYAISSH_SESS__LEFT=$LEFT"
echo "__PYAISSH_SESS__ROOTS=$ROOTS"
echo "__PYAISSH_SESS__HAD=$HAD"
echo "__PYAISSH_SESS__KILLED=$KILLED"
echo __PYAISSH_SESS__CLEANED=1
"""


def _session_kill_cmd(f, tmux, keep_dir=False, pane_pid=None):
    """结束会话：进程树闭包快照 → `kill-session` → 对幸存者 TERM→KILL → 校验 → 删目录。

    闭包快照在 `kill-session` **之前**算（父进程被杀后子进程会被 reparent，事后再算会漏），
    根用 tmux 给的 `pane_pid`（权威、无需自证）；快照覆盖 shell 与它的作业。
    实测（S10）：已经 reparent 到 1 的脱离进程（`nohup setsid ...`）不在闭包里——这是
    进程树快照的固有盲区，文档写明（BND-01）。
    """
    rm = "" if keep_dir else "rm -rf '%s' 2>/dev/null" % f["dir"]
    return _tpl(_SESSION_KILL_TPL, PROLOGUE=_SESSION_TMUX_PROLOGUE, TMUX=tmux,
                DIR=f["dir"], TMFILE=f["tmux"], TOKEN=f["token"],
                PANE_PID=(pane_pid if pane_pid else ""),
                AWK=(_SESSION_TREE_AWK % "$P"), RM=rm)


_SESSION_REAP_BODY_TPL = """\
ROOT='@@ROOT@@'
TM="@@TM@@"
NOW=$(date +%s)
for d in "$ROOT"/*/; do
  [ -d "$d" ] || continue
  D=${d%/}; N=${D##*/}
  case "$N" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
  TTL=0
  [ -r "$D/meta" ] && TTL=$(awk '{print $4}' "$D/meta" 2>/dev/null)
  case "$TTL" in ''|*[!0-9]*) continue ;; esac
  [ "$TTL" -gt 0 ] || continue
  B=''
  [ -r "$D/beat" ] && B=$(cat "$D/beat" 2>/dev/null)
  case "$B" in ''|*[!0-9]*) continue ;; esac
  [ $((NOW - B)) -gt "$TTL" ] || continue
  TN=''
  [ -r "$D/tmux" ] && TN=$(cat "$D/tmux" 2>/dev/null)
  if [ -z "$TN" ]; then continue; fi
  if $TM has-session -t "=$TN" 2>/dev/null; then
    CUR=$($TM display-message -p -t "=$TN:" '#{pane_current_command}' 2>/dev/null)
    case "$CUR" in bash|sh|dash|zsh|ksh|-bash) ;; *) continue ;; esac
    $TM kill-session -t "=$TN" 2>/dev/null
  fi
  rm -rf "$D" 2>/dev/null
  printf '%s\\n' "reclaim $N idle=$((NOW - B))s ttl=${TTL}s at $NOW" >> "$ROOT/.reaper.log" 2>/dev/null
done
if [ -f "$ROOT/.reaper.log" ] && [ "$(wc -c < "$ROOT/.reaper.log" 2>/dev/null || echo 0)" -gt @@LOGMAX@@ ]; then
  tail -n 200 "$ROOT/.reaper.log" > "$ROOT/.reaper.log.tmp" 2>/dev/null && mv "$ROOT/.reaper.log.tmp" "$ROOT/.reaper.log" 2>/dev/null
fi
"""


def _session_reap_body(root, body=None):
    """reaper 的单次清扫逻辑（纯文本生成，便于单测与内联复用）。

    判据（与惰性扫一致）：`meta` 第 4 字段 TTL > 0；`beat` 过期 > TTL；
    `tmux` 名文件存在（没有它的目录一律不碰——不是本引擎建的）；
    会话里的前台命令是 shell（空闲）才回收，判不出就不收（宁可多留）。
    """
    return _tpl(body or _SESSION_REAP_BODY_TPL, ROOT=root, TM=_SESSION_TMUX,
                LOGMAX=SESSION_TMUX_REAP_LOG_MAX)


_SESSION_REAP_SCRIPT_TPL = """\
#!/bin/bash
# pyaissh 每主机 reaper（自动生成，勿手改；生成方：pyaissh session start）
# 用法：reap.sh --once（扫一遍就退）｜reap.sh --loop（常驻，每 @@INTERVAL@@ 秒一遍，无会话目录自退）
ROOT='@@ROOT@@'
INTERVAL=@@INTERVAL@@
LOOP=0
[ "$1" = "--loop" ] && LOOP=1
reap_once() {
@@BODY@@
}
if [ "$LOOP" = 0 ]; then reap_once; exit 0; fi
# pid 文件带**指纹**（pid + 间隔）：间隔变了说明这是上一代 reaper，由 ensure 侧收掉重起
printf '%s %s\\n' "$$" "$INTERVAL" > "$ROOT/.reaper.pid" 2>/dev/null
while :; do
  reap_once
  n=0
  for d in "$ROOT"/*/; do [ -d "$d" ] && n=$((n+1)); done
  if [ "$n" -eq 0 ]; then rm -f "$ROOT/.reaper.pid" 2>/dev/null; exit 0; fi
  sleep "$INTERVAL"
done
"""


def _session_reap_script(root):
    """reaper 脚本全文（`--once` / `--loop` 两用）。"""
    body = "\n".join("  " + ln for ln in _session_reap_body(root).splitlines())
    return _tpl(_SESSION_REAP_SCRIPT_TPL, ROOT=root, BODY=body,
                INTERVAL=_session_reap_interval())


def _session_reap_sweep_cmd(root):
    """惰性扫：把同一份 reaper 逻辑内联进 `bash -s` 跑一次（不依赖 reap.sh 是否存在）。"""
    return _SESSION_TMUX_PROLOGUE + _session_reap_body(root) + "\necho __PYAISSH_SESS__REAPED=1\n"


def _session_reaper_ensure_cmd(root, script_b64, interval):
    """幂等拉起每主机 reaper，并**收敛到恰好一个**。

    为什么需要收敛（实测踩到）：`start` 会重写 `reap.sh`（间隔/内容可能变），但**旧代的
    reaper 进程还在 `sleep`**——它们既用旧间隔，又会同时扫同一批会话；反复 start 会攒出
    好几个 reaper（实测一次冒烟里攒了 4 个），互相看不出对方。所以这里做三件事：
      1. 落盘最新脚本；
      2. 把**其它**正在跑的 `<root>/reap.sh --loop` 停掉（按 argv 自证是本 root 的，不是就跳过）；
      3. 只有当"记名的 pid 活着 **且** argv 确实是本 root 的 reap.sh **且** 指纹（间隔）一致"
         才认它活着；否则起新一代并写 pid 文件。

    注意 `%` 只作用于后半段：prologue 里有 `${TMV%%.*}`，整段去格式化会把 `%%` 吃掉。
    """
    q = _sh_quote
    body = ("D=%s; mkdir -p \"$D\" 2>/dev/null; chmod 700 \"$D\" 2>/dev/null; "
            "printf %%s '%s' | base64 -d > \"$D/reap.sh\" 2>/dev/null; chmod 700 \"$D/reap.sh\"; "
            "P=''; OLD=''; "
            "if [ -r \"$D/.reaper.pid\" ]; then read -r P OLD < \"$D/.reaper.pid\" 2>/dev/null; fi; "
            "ALIVE=0; "
            "if [ -n \"$P\" ] && kill -0 \"$P\" 2>/dev/null && "
            "ps -o args= -p \"$P\" 2>/dev/null | grep -qF \"$D/reap.sh\"; then ALIVE=1; fi; "
            "KILLED=0; "
            "for q in $(ps -eo pid=,args= 2>/dev/null | awk -v pat=\"$D/reap.sh --loop\" "
            "'index($0, pat) {print $1}'); do "
            "[ \"$q\" = \"$P\" ] && [ \"$ALIVE\" = 1 ] && continue; "
            "kill \"$q\" 2>/dev/null && KILLED=$((KILLED+1)); done; "
            "if [ \"$ALIVE\" = 1 ] && [ \"$OLD\" = \"%d\" ]; then echo __PYAISSH_SESS__REAPER=alive; "
            "else [ -n \"$P\" ] && kill \"$P\" 2>/dev/null; "
            "setsid nohup bash \"$D/reap.sh\" --loop </dev/null >/dev/null 2>&1 & "
            "echo __PYAISSH_SESS__REAPER=started; fi; "
            "echo \"__PYAISSH_SESS__REAPER_KILLED=$KILLED\"; "
            "echo __PYAISSH_SESS__DONE=1"
            % (q(root), script_b64, int(interval)))
    return _SESSION_TMUX_PROLOGUE + body


def _session_start_followup_cmd(root, script_b64):
    """start 收尾（一次 exec 干两件事）：惰性扫一遍过期空闲会话 + 幂等拉起每主机 reaper。"""
    return (_session_reap_sweep_cmd(root) + "\n"
            + _session_reaper_ensure_cmd(root, script_b64, _session_reap_interval()) + "\n")


def _session_sweep(client, root, timeout=25):
    """惰性扫一遍：回收"过期且提示符空闲"的会话（REAP-02）。

    为什么内联同一份 reaper 逻辑而不是调 `reap.sh`：脚本可能还没落盘（老会话/手工删过），
    内联保证"任何一条会话子命令都能顺带回收"。失败只记 stderr，绝不影响本次调用。
    """
    try:
        rc, out, err = _session_run(client, _session_reap_sweep_cmd(root), timeout=timeout)
        if rc != 0 and (err or "").strip():
            log("[WARN] 空闲回收扫描未完成（不影响本次调用）：%s" % err.strip()[:160])
        return out
    except Exception as e:
        log("[WARN] 空闲回收扫描异常（不影响本次调用）：%s" % str(e)[:160])
        return ""


def _session_reaper_stop_cmd(root):
    """没有任何会话了 ⇒ 把本主机的 reaper 收掉（"用完即净"）。

    不做这一步的话，reaper 要等**下一轮**（最长一个间隔，默认 5 分钟）才发现"没会话了"而自退；
    虽然只有 1 个进程，但收尾留一个后台进程不符合本工具的习惯。
    只按 pid（且 argv 自证是本 root 的 `reap.sh`）与 argv 精确匹配停——不用 pkill 模式杀。
    """
    q = _sh_quote
    body = ("D=%s; P=''; "
            "if [ -r \"$D/.reaper.pid\" ]; then read -r P _ < \"$D/.reaper.pid\" 2>/dev/null; fi; "
            "if [ -n \"$P\" ] && kill -0 \"$P\" 2>/dev/null && "
            "ps -o args= -p \"$P\" 2>/dev/null | grep -qF \"$D/reap.sh\"; then "
            "kill \"$P\" 2>/dev/null && echo __PYAISSH_SESS__REAPER=stopped; fi; "
            "rm -f \"$D/.reaper.pid\" 2>/dev/null; "
            "for q in $(ps -eo pid=,args= 2>/dev/null | awk -v pat=\"$D/reap.sh --loop\" "
            "'index($0, pat) {print $1}'); do kill \"$q\" 2>/dev/null; done; "
            "echo __PYAISSH_SESS__DONE=1" % q(root))
    return _SESSION_TMUX_PROLOGUE + body


# awk：求"以 root 为根的进程树闭包"（一次性快照；父进程被杀后孤儿会被 reparent，
# 所以必须在杀之前把集合算完，之后只对这批 pid 反复校验）
# 两处实测坑：①引号必须写成 shell 认的 root="$P"（写成 root=\"$P\" ⇒ awk 收到的值带引号、
#   闭包恒空 ⇒ kill 变空操作）；②打印时要用 pid[k] 而不是下标 k（s[] 以 pid 为键）。
_SESSION_TREE_AWK = (
    "awk -v root=\"%s\" '"
    "{pid[NR]=$1; pp[$1]=$2} "
    "END{ s[root]=1; "
    "for(i=0;i<12;i++){ for(k in pid){ p=pid[k]; if (p==root || s[pp[p]]==1) s[p]=1 } } "
    "for(k in pid){ p=pid[k]; if (s[p]==1) printf \"%%s \", p } }'"
)


def _session_clean_text(s, strip_ansi=True):
    """会话输出清洗：CRLF/CR → LF、去掉哨兵行、可选剥 ANSI、去首尾空行。"""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if strip_ansi:
        s = _strip_ansi(s)
    lines = []
    for ln in s.split("\n"):
        if _SESSION_RC_RE.search(ln):
            continue
        lines.append(ln)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _session_slice_by_sentinel(text, token=None):
    """按哨兵切出"这条命令的输出"：起点=上一个哨兵之后，终点=目标哨兵（或 EOF）。

    返回 (清洗后的输出, exit_code|None, 命中的 token|None)。
    目标 token 尚未出现（命令还在跑）时，给"最后一个哨兵之后到 EOF"的输出。
    """
    ms = list(_SESSION_RC_RE.finditer(text))
    if not ms:
        return _session_clean_text(text), None, None
    target = None
    if token:
        for m in ms:
            if m.group(1) == token:
                target = m
        if target is None:
            return _session_clean_text(text[ms[-1].end():]), None, None
    else:
        target = ms[-1]
    prev_end = 0
    for m in ms:
        if m.start() < target.start():
            prev_end = m.end()
    return _session_clean_text(text[prev_end:target.start()]), int(target.group(2)), target.group(1)


def _session_last_token(sftp, f):
    """读会话最近一次 send 的 token（read --wait-rc 不带 --token 时用它）。"""
    try:
        with sftp.open(f["token"], "r") as fh:
            t = fh.read().decode("utf-8", "replace").strip()
        return t if re.match(r"^[0-9a-f]{4,16}$", t) else None
    except Exception:
        return None


def _session_find_sentinel(text, token=None):
    """在文本里找哨兵；返回 (最后匹配对象, token, rc) 或 (None, None, None)。"""
    last = None
    for m in _SESSION_RC_RE.finditer(text):
        if token is None or m.group(1) == token:
            last = m
    if last is None:
        return None, None, None
    return last, last.group(1), int(last.group(2))


def _session_remote_exists(sftp, path):
    """远端路径是否存在（文件/目录/FIFO 都算存在，不问类型）。"""
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _session_sftp_read(sftp, path, offset=0, limit=None):
    """从 offset 读日志（limit 为 None 则读到尾）。返回 (bytes, size)。

    v2.3.0：读前刷新 SFTP 看门狗活动时间——会话轮询可持续几十秒只做这类小读，
    不刷新就会被看门狗（默认 30s）误杀（实测 `session run sleep 35` 在 30.7s 处被杀、结果丢失）。
    """
    try:
        _sftp_touch_activity(sftp)
    except Exception:
        pass
    f = sftp.open(path, "rb")
    try:
        f.seek(offset)
        size = f.stat().st_size
        want = None if limit is None else max(0, limit)
        data = f.read() if want is None else f.read(want)
        return data, size
    finally:
        try:
            f.close()
        except Exception:
            pass


def _session_sftp_size(sftp, path):
    """只取日志大小（stat，便宜）；失败返回 0。读前刷新看门狗活动时间。"""
    try:
        _sftp_touch_activity(sftp)
    except Exception:
        pass
    try:
        return sftp.stat(path).st_size
    except Exception:
        return 0


def _session_poll_sentinel(sftp, path, offset, token, timeout, interval=0.25):
    """轮询日志直到出现目标哨兵或超时。返回 (data, size, rc|None, token|None, elapsed)。

    v2.3.0 修两个语义问题（B4，真机复现）：
      ① **回看窗口**：从 `max(offset, size - SESSION_TAIL_WINDOW)` 起读——哨兵总在文件**末尾**；
         早期实现"从 offset 起读、上限 1MB"，命令输出 >1MB 时哨兵落在窗外 ⇒ 明明跑完了也永远
         回 running（实测 `seq 1 300000` 卡在 ~1MB 处、只看到 144960 行）。
      ② **只追增量**：每轮只 `stat`（便宜），文件长长了才读新增那一段、维护末尾 1MB 输出窗口，
         不再每 0.25s 重下整个 1MB 窗口（窄带宽链路自残）。
    另：每轮刷新 SFTP 看门狗活动时间（B1：`session run sleep 35` 曾因 30s 无活动被杀）。
    """
    t0 = time.time()
    tail = b""          # 输出窗口（末尾 ≤1MB，哨兵在其中）
    scanned = -1        # 已扫描到的文件大小
    size = 0
    while True:
        try:
            _sftp_touch_activity(sftp)
        except Exception:
            pass
        size = _session_sftp_size(sftp, path)
        if size != scanned:
            if scanned >= 0 and size > scanned:
                inc, _ = _session_sftp_read(sftp, path, scanned, None)
                tail = (tail + inc)[-SESSION_TAIL_WINDOW:]
            else:
                start = max(0 if offset is None else offset, size - SESSION_TAIL_WINDOW)
                tail, _ = _session_sftp_read(sftp, path, start, None)
            scanned = size
        text = tail.decode("utf-8", "replace")
        m, tok, rc = _session_find_sentinel(text, token)
        if m is not None:
            return tail, size, rc, tok, time.time() - t0
        if time.time() - t0 >= timeout:
            return tail, size, None, None, time.time() - t0
        time.sleep(interval)


# ---------------------------------------------------------------- 子命令

def _fmt_age(sec):
    """秒 → 人类可读（消息里用）。"""
    if sec is None:
        return "未知时长"
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    return "%.1f 小时" % (sec / 3600.0)


def _session_info(client, f, sftp=None, tmux_info=None):
    """读会话元信息：pid / pty / cols / started_at / age_seconds / ttl_seconds /
    idle_seconds（距上次交互）/ log_bytes / mtime —— `list` 与 `start --attach` 共用。

    缺什么就少什么字段，**绝不抛**（会话可能正好被回收/删除）。`alive`/`status` 由调用方补。
    `tmux_info` 是 `_session_tmux_ls` 给的那条会话记录：pid/shell_pid 由它来（tmux 权威），
    `meta` 只负责 pty/cols/started_at/ttl（四个字段）。
    """
    info = {}
    own = sftp is None
    if own:
        try:
            sftp = open_sftp(client)
        except Exception:
            return info
    try:
        if tmux_info:
            info["pid"] = tmux_info.get("pane_pid")
            info["shell_pid"] = tmux_info.get("pane_pid")
            if tmux_info.get("cur"):
                info["current_command"] = tmux_info["cur"]
        try:
            with sftp.open(f["meta"], "r") as fh:
                parts = fh.read().decode("utf-8", "replace").split()
            if parts:
                info["pty"] = parts[0] == "1"
                if len(parts) > 1 and parts[1].isdigit():
                    info["cols"] = int(parts[1])
                if len(parts) > 2 and parts[2].isdigit():
                    # meta 第三字段 = start 时的 epoch 秒（用于 age）
                    info["started_at"] = int(parts[2])
                    info["age_seconds"] = max(0, int(time.time()) - int(parts[2]))
                if len(parts) > 3 and parts[3].isdigit():
                    v = int(parts[3])
                    info["ttl_seconds"] = v if v > 0 else None
        except Exception:
            pass
        try:
            with sftp.open(f["beat"], "r") as fh:
                b = fh.read().decode("utf-8", "replace").strip()
            # beat 里是 epoch 秒；读不到内容就给 None（list/回收侧都按"未知"处理，不乱猜）
            info["idle_seconds"] = max(0, int(time.time()) - int(b)) if b.isdigit() else None
        except Exception:
            pass
        try:
            with sftp.open(f["tmux"], "r") as fh:
                info["tmux_session"] = fh.read().decode("utf-8", "replace").strip() or None
        except Exception:
            pass
        try:
            st = sftp.stat(f["log"])
            info["log_bytes"] = st.st_size
            info["mtime"] = int(st.st_mtime or 0)
        except Exception:
            pass
    finally:
        if own:
            try:
                sftp.close()
            except Exception:
                pass
    return info


def _session_env_items():
    """当前 SSH 通道环境里值得注入 tmux 全局环境的变量（tmux server 环境在启动时冻结）。

    **中文/编码兜底**：本机（Windows）环境通常没有 `LANG`/`LC_ALL`，而 tmux server 的环境
    又冻结在它启动那一刻——如果那一刻也没有 locale，远端的 pane 会落到 `POSIX`/`C`，
    里面打印中文要么乱码要么直接报编码错（Python `UnicodeEncodeError`、`ls` 把非 ASCII
    文件名显示成 `?`）。所以两者都缺时**补一个 `C.UTF-8`**（glibc ≥ 2.35 起内置；
    老系统上没有这个 locale 时等价于原来的 C，不会更糟）。用户自己的 `LANG`/`LC_ALL`
    若存在则原样透传，不覆盖。
    """
    out = []
    for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ", "TERM"):
        v = os.environ.get(k)
        if v:
            out.append((k, v))
    if not os.environ.get("LANG") and not os.environ.get("LC_ALL"):
        out.append(("LANG", SESSION_DEFAULT_LANG))
    return out


def _session_ready_wait(client, f, tmux, init, timeout):
    """发初始化载荷并等它的哨兵；失败时**自愈一次**（探测重 arm 管道 → 重发 → 再等）。

    返回 (ready, wait_seconds, warnings)。第一次等不到就探测一下：`out.log` 被删/管道
    死掉时探测会重新 arm（实测 S5：管道活着但文件被删会静默丢输出），然后再给一次机会。
    """
    warns = []
    for attempt in (1, 2):
        token = _session_send_payload(client, f, tmux, init)
        if not token:
            warns.append("初始化载荷未能写入会话（tmux load-buffer/paste-buffer 失败）")
            return False, 0.0, warns
        sftp = open_sftp(client)
        try:
            _d, _sz, rc_r, _tok, el = _session_poll_sentinel(
                sftp, f["log"], 0, token, timeout, interval=0.2)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        if rc_r is not None:
            return True, el, warns
        if attempt == 1:
            _prc, pout, _perr = _session_run(client, _session_probe_cmd(f, tmux, touch=False),
                                             timeout=15)
            if "LOG_REARM" in dict(_SESSION_MARK_RE.findall(pout)):
                warns.append("out.log 曾被删除，已重建输出镜像（log_recreated）")
    return False, 0.0, warns


def cmd_session_start(args):
    """起一个常驻会话（真 PTY；引擎 = tmux，见 SPEC_session_tmux.md）。

    `--attach`：同名会话还活着就**接上**（不新建），返回 attached=true + pid/age；
    会话不存在（或被空闲回收）则正常新建并返回 attached=false。
    """
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    name = args.name or "main"
    if not _session_check_name(args.json, name):
        return 2
    ttl, terr = _session_parse_ttl(getattr(args, "ttl", None),
                                   _session_parse_ttl(os.environ.get("PYAISSH_SESSION_TTL"))[0])
    if terr is not None:
        emit_error(args.json, "bad_args", terr)
        return 2
    f = _session_files(root, name)
    tmux, _tgt_s, _tgt_p = _session_tmux_pair(name)
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        rc, out, err = _session_run(
            client, _session_start_cmd(f, tmux, args.cols, ttl, _session_env_items()),
            timeout=max(20, args.timeout + 10))
        marks = dict(_SESSION_MARK_RE.findall(out))
        ec = _session_tmux_gate(args.json, marks, {"session": name, "dir": f["dir"]})
        if ec is not None:
            return ec
        warnings = []
        if "EXISTS" in marks:
            ex_pid = marks.get("PID")
            if args.attach:
                # 接上旧会话：刷新活动时间（别让它刚接上就被空闲回收）
                _session_touch(client, f)
                extra = _session_info(client, f,
                                      tmux_info={"pane_pid": int(ex_pid) if str(ex_pid).isdigit()
                                                 else None})
                result = {
                    "ok": True, "action": "session", "version": VERSION, "session": name,
                    "attached": True, "dir": f["dir"], "log": f["log"],
                    "pid": int(ex_pid) if str(ex_pid).isdigit() else ex_pid,
                    "ready": True, "ttl_seconds": extra.get("ttl_seconds"),
                    "age_seconds": extra.get("age_seconds"),
                    "idle_seconds": extra.get("idle_seconds"),
                    "host": conn["host"], "user": conn["user"], "port": conn["port"],
                    "warnings": warnings,
                    "next_action": ("已接上仍在运行的会话 %r（状态——cwd/变量——都保留）："
                                    "直接 session run/send/read 继续；要重开先 session kill"
                                    % name),
                    "duration_ms": int((time.time() - start) * 1000),
                }
                _emit_result(args, result, header="[SESSION %s 接上 pid=%s] %s"
                             % (name, ex_pid, f["dir"]))
                return 0
            emit_error(args.json, "session_exists",
                       "会话 %r 仍在运行（pid %s，已挂 %s）：**直接继续用它**——"
                       "pyaissh session run/send/read --name %s（状态/cwd/变量都还在）；"
                       "确实要重开：先 pyaissh session kill --name %s，"
                       "或改用 pyaissh session start --attach 自动接上"
                       % (name, ex_pid, _fmt_age(_session_info(client, f).get("age_seconds")),
                          name, name),
                       extra={"session": name, "dir": f["dir"], "pid": ex_pid,
                              "attach_hint": "session start --attach 可自动接上"})
            return 2
        for bad, why, etype in (("MKDIR_FAIL", "无法创建会话目录（权限/磁盘）", "session_failed"),
                                ("NEW_FAIL", "tmux 无法创建会话（server 起不来？）", "tmux_failed")):
            if bad in marks:
                emit_error(args.json, etype, "启动会话失败：%s" % why,
                           extra={"session": name, "dir": f["dir"], "stderr": err[-400:]})
                return 255
        pid = marks.get("PID")
        if not pid:
            emit_error(args.json, "session_failed",
                       "启动会话失败：未取得会话进程号（远端输出见 stderr）",
                       extra={"session": name, "stdout": out[-400:], "stderr": err[-400:]})
            return 255
        if "PIPE_FAIL" in marks:
            warnings.append("输出镜像（tmux pipe-pane）未能建立：session read/run 可能读不到输出；"
                            "可 session kill 后重开，或检查 tmux 的临时目录权限")

        # 就绪确认：初始化载荷（关回显/去 bracketed paste 噪音）+ 等它的哨兵
        init = ('PS1=; PS2=; stty -echo 2>/dev/null || true; '
                'bind "set enable-bracketed-paste off" 2>/dev/null || true')
        ready, wait_s, w2 = _session_ready_wait(client, f, tmux, init, max(2, args.wait_ready))
        warnings.extend(w2)

        result = {
            "ok": True, "action": "session", "version": VERSION, "session": name,
            "attached": False,
            "dir": f["dir"], "log": f["log"],
            "pid": int(pid) if str(pid).isdigit() else pid,
            "pty": True, "cols": args.cols, "ready": ready, "ready_wait_ms": int(wait_s * 1000),
            "ttl_seconds": ttl if ttl and ttl > 0 else None,
            "idle_seconds": 0,
            "host": conn["host"], "user": conn["user"], "port": conn["port"],
            "permissions": {"dir": "0700", "out.log": "0600", "meta": "0600",
                            "beat": "0600", "tmux": "0600"},
            "warnings": warnings,
            "next_action": ("会话已就绪。逐条执行：pyaissh session send <target> --name %s --cmd '...'，"
                            "再用 session read 读结果（见 next_offset）；中断执行中的命令：session ctrl-c；"
                            "收尾：session kill" % name),
            "duration_ms": int((time.time() - start) * 1000),
        }
        if ttl and ttl > 0:
            result["next_action"] += ("。**空闲回收**：提示符空闲且 %s 内没有任何 pyaissh 交互"
                                      "（send/run/read/ctrl-c/keys）就会自动回收（进程 + 目录），"
                                      "需要保活就 --ttl 0" % _fmt_age(ttl))
            # 拉起每主机 reaper（幂等）+ 顺手惰性扫一遍过期空闲会话（REAP-02/03）
            try:
                b64 = base64.b64encode(_session_reap_script(root).encode("utf-8")).decode("ascii")
                _session_run(client, _session_start_followup_cmd(root, b64), timeout=40)
            except Exception as e:
                result["warnings"].append("空闲回收 reaper 未能拉起（不影响本次会话）：%s"
                                          % str(e)[:120])
        else:
            result["warnings"].append("空闲回收已关闭（--ttl 0）：会话会一直留着，用完记得 session kill")
        if not ready:
            result["warnings"].append("会话就绪确认超时（哨兵未出现）：请用 session read 查看 out.log 首屏")
        _emit_result(args, result, header="[SESSION %s pid=%s tmux=%s] %s"
                     % (name, pid, tmux, f["dir"]))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_failed", str(e))
        return 255
    finally:
        close_all(client)


def _session_payload_text(cmd, token):
    """命令 → 灌进会话的载荷文本（纯函数，便于单测）。

    形态：`\\n{\\n<命令>\\n}; echo "哨兵__$?"\\n`。要点（两条都是实测踩出来的）：
    - 哨兵必须与命令**在同一行被 shell 解析**（`}; echo 哨兵` 那一行）：命令里从终端读取的
      语句（`read -p`）会把紧随其后的**独立行**当输入吃掉，哨兵就永不出现（`run` 卡在 running）。
    - 用 `{}` 而非 `()`：大括号是同一个 shell，`cd`/`export`/函数等状态照常保留。

    R2 加固：载荷**以换行开头**。本地若在上一次写入的半途断线（关机/断网/被杀），远端 tty 的
    行规程里会留下**没有换行的半行**，下一次写入会与它串成同一行 ⇒ 语法错误且哨兵永不出现。
    前置一个换行先把那半行终结掉（它作为一条垃圾命令执行、报错留在 out.log），
    之后真正的载荷在干净的输入行里解析 ⇒ 哨兵照常出现，AI 至少能拿到退出码。

    （tmux 引擎下这一层完全不变：载荷经 `paste-buffer` 原样灌入 pane 的输入流，
    输入流是原样字节，因此状态保留、多行与二进制都安全。）
    """
    body = cmd.rstrip("\n")
    return "\n{\n%s\n}; echo \"%s%s__$?\"\n" % (body, SESSION_RC_PREFIX, token)


def _session_send_payload(client, f, tmux, cmd, token=None):
    """把「命令 + 退出码哨兵」灌进会话（tmux `load-buffer` + `paste-buffer`）。返回 token。

    传输走 tmux 缓冲区：`load-buffer` 是数据通道，不经过 tmux 自己的命令行解析
    （零引号风险、二进制安全）。
    """
    token = token or os.urandom(4).hex()
    payload = _session_payload_text(cmd, token)
    rc, out, _err = _session_run(client, _session_paste_cmd(f, tmux),
                                 stdin_data=payload.encode("utf-8"), timeout=20)
    marks = dict(_SESSION_MARK_RE.findall(out))
    return token if (rc == 0 and "SENT" in marks) else None


def _session_tail_lines(text, n):
    """取末尾 n 行（n<=0 或为 None 时原样返回）。

    v2.3.0：`read --lines N` 此前**完全没被消费**（argparse 注册了、互斥校验也有，但读取分支
    没用它）——20000 行日志 `--lines 5` 会回传上万行，既违约又烧 token（真机复现）。
    """
    if not n or n <= 0:
        return text
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])


def _session_load(args, root, name, need_alive=True):
    """公共前置：校验名字 → 连接 → 探测会话（顺手续期）→ 惰性扫。

    返回 `(conn, client, f, tmux, pid, alive, ec)`（tmux = tmux 会话名）。

    「会话不在」分两种：
      - 目录里连 `meta` 都没有 ⇒ `session_not_found`（从没起过，或已被空闲回收）；
      - `meta` 在但 tmux 会话没了 ⇒ `session_dead`（在会话里敲了 `exit`、被人 `kill-session`）
        ——状态不可恢复，提示用 `kill` 清目录后重开。
    """
    if not _session_check_name(args.json, name):
        return None, None, None, None, None, False, 2
    f = _session_files(root, name)
    tmux, _tgt_s, _tgt_p = _session_tmux_pair(name)
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return None, None, None, None, None, False, conn_ec
    rc, out, _err = _session_run(client, _session_probe_cmd(f, tmux), timeout=15)
    marks = dict(_SESSION_MARK_RE.findall(out))
    ec = _session_tmux_gate(args.json, marks, {"session": name, "dir": f["dir"]})
    if ec is not None:
        close_all(client)
        return None, None, None, None, None, False, ec
    if "LOG_REARM" in marks:
        log("[WARN] 会话 %r 的 out.log 曾被删除，已重建输出镜像（log_recreated）" % name)
    if "PIPE" in marks and marks["PIPE"] not in ("1", ""):
        log("[WARN] 会话 %r 的输出镜像未生效（pane_pipe=%s），已尝试重新 arm"
            % (name, marks["PIPE"]))
    alive = "ALIVE" in marks
    pid = marks.get("PID")
    if not alive:
        sftp = open_sftp(client)
        try:
            has_meta = _session_remote_exists(sftp, f["meta"])
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        if not has_meta:
            emit_error(args.json, "session_not_found",
                       "找不到会话 %r（%s 不存在）：可能从没起过，或**已被空闲回收**"
                       "（默认提示符空闲 %s 就自动收，`--ttl 0` 可关）——"
                       "用 pyaissh session start --name %s 重建（要接上还活着的旧会话用 --attach），"
                       "或先用 pyaissh session list 看现有会话"
                       % (name, f["dir"], _fmt_age(_SESSION_TTL_DEFAULT), name),
                       extra={"session": name, "dir": f["dir"],
                              "ttl_default_seconds": _SESSION_TTL_DEFAULT})
            close_all(client)
            return None, None, None, None, None, False, 2
        emit_error(args.json, "session_dead",
                   "会话 %r 的 tmux 会话已消失（在会话里 `exit`、被人 kill-session，或宿主重启）："
                   "会话状态不可恢复——用 pyaissh session kill --name %s 清理残留目录后重新 start"
                   % (name, name),
                   extra={"session": name, "dir": f["dir"], "pid": None})
        close_all(client)
        return None, None, None, None, None, False, 2
    if need_alive is False:
        return conn, client, f, tmux, pid, alive, None
    # 目标已续期（探测脚本里写 beat）⇒ 现在扫其它会话是安全的（REAP-02：先续期再扫）
    _session_sweep(client, root)
    return conn, client, f, tmux, pid, alive, None


def cmd_session_send(args):
    """把一条命令喂进会话（自动追加退出码哨兵）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    cmd = _session_resolve_cmd(args)
    if cmd is None:
        return 2
    conn, client, f, tmux, pid, _alive, ec = _session_load(args, root, args.name or "main")
    if ec is not None:
        return ec
    try:
        sftp = open_sftp(client)
        try:
            try:
                log_bytes = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            except Exception:
                log_bytes = 0
            token = _session_send_payload(client, f, tmux, cmd)
            if token is None:
                emit_error(args.json, "send_failed",
                           "命令未能灌进会话（tmux load-buffer/paste-buffer 失败：会话可能刚退出）",
                           extra={"session": args.name, "tmux": tmux})
                return 255
            # 记下"最近这条命令"的 token：read --wait-rc 不带 --token 时用它定位，
            # 否则会拿历史哨兵立刻返回"已完成"（实测踩过）；写失败要在结果里留痕——
            # 否则消费者会拿**上一条**命令的 token 去 read，读到上一条的输出
            # （A 机实测偶发：ANSI 用例读回了 keys 用例的 `N? GOT:hello_pty`）
            _tok_ok = _session_write_token(sftp, f, token)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        result = {
            "ok": True, "action": "session", "version": VERSION, "session": args.name,
            "token": token, "sent_bytes": len(cmd.encode("utf-8")),
            "log": f["log"], "offset": log_bytes, "next_offset": log_bytes,
            "pid": int(pid) if str(pid).isdigit() else pid,
            "host": conn["host"], "user": conn["user"], "port": conn["port"], "warnings": [],
            "next_action": ("读结果：pyaissh session read <target> --name %s --offset %d"
                            "（或 --wait-rc 30 --token %s 等这条命令跑完拿退出码）"
                            % (args.name, log_bytes, token)),
            "duration_ms": int((time.time() - start) * 1000),
        }
        if getattr(args, "_crlf_normalized", 0):
            result["crlf_normalized"] = args._crlf_normalized
        if not _tok_ok:
            result["warnings"].append(
                "last.token 写入失败：后续 `read --wait-rc` 不带 --token 可能定位到上一条命令——"
                "请显式传 --token %s" % token)
        _emit_result(args, result, header="[SESSION %s] sent token=%s offset=%d"
                     % (args.name, token, log_bytes))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "send_failed", str(e))
        return 255
    finally:
        close_all(client)


def _session_resolve_cmd(args):
    """从 --cmd / --cmd-file 取命令文本（CRLF 归一，与 exec 同规则）。"""
    cmd = args.cmd
    if cmd and args.cmd_file:
        log("[WARN] --cmd 与 --cmd-file 同时指定，--cmd-file 被忽略")
    if not cmd and args.cmd_file:
        try:
            if args.cmd_file == "-":
                cmd = sys.stdin.buffer.read().decode("utf-8-sig")
            else:
                with open(_fix_msys_local_path(args.cmd_file), encoding="utf-8-sig",
                          newline="") as fh:
                    cmd = fh.read()
        except Exception as e:
            emit_error(args.json, "read_cmd_failed", str(e))
            return None
    if not cmd or not cmd.strip():
        emit_error(args.json, "bad_args", "未指定命令（--cmd 或 --cmd-file）")
        return None
    if not getattr(args, "keep_crlf", False):
        cmd, n = _normalize_cmd_newlines(cmd)
        if n:
            args._crlf_normalized = n
    return cmd


def _session_write_token(sftp, f, token):
    """记下"最近这条命令"的 token（read/run 不带 --token 时用它定位）。

    重试两次并返回是否成功——写失败会让后续 `read --wait-rc`（不带 --token）误用**上一条**命令的
    token，从而读到上一条的输出（A 机实测偶发：ANSI 用例读回了 keys 用例的 `N? GOT:hello_pty`）。
    """
    for _ in range(2):
        try:
            with sftp.open(f["token"], "w") as fh:
                fh.write(token)
            _sftp_chmod(sftp, f["token"], 0o600)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def cmd_session_run(args):
    """会话内跑一条命令并等它结束——**send + 等待合成一次调用**。

    这是"会话式一步一调用"的关键：没有它，每步要 send + read 两次调用，
    会话就比 exec 贵一倍（实测 exec 一次调用即可）。返回与 read 同构：
    `stdout`（本条命令的输出）/`exit_code`/`status`(`done`|`running`)/`next_offset`/`token`。
    超时未结束 → `status:"running"` + `next_action` 指路（read --wait-rc 继续等 / ctrl-c 中断）。
    `--no-wait` 只发送不等（等价 send），让调用方自己决定怎么读。
    """
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    cmd = _session_resolve_cmd(args)
    if cmd is None:
        return 2
    if args.wait_rc and args.wait_rc > SESSION_WAIT_MAX:
        emit_error(args.json, "bad_args",
                   "--wait-rc 上限 %d 秒（宿主单次调用约 600s；更久请稍后 read 轮询）"
                   % SESSION_WAIT_MAX)
        return 2
    conn, client, f, tmux, pid, _alive, ec = _session_load(args, root, args.name or "main")
    if ec is not None:
        return ec
    try:
        sftp = open_sftp(client)
        try:
            try:
                offset = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            except Exception:
                offset = 0
            token = _session_send_payload(client, f, tmux, cmd)
            if token is None:
                emit_error(args.json, "send_failed",
                           "命令未能灌进会话（tmux load-buffer/paste-buffer 失败：会话可能刚退出）",
                           extra={"session": args.name, "tmux": tmux})
                return 255
            _session_write_token(sftp, f, token)
            wait = 0 if args.no_wait else (args.wait_rc or SESSION_RUN_WAIT)
            exit_code, waited, out_text, size = None, 0.0, "", offset
            if wait:
                data, size, _rc, _tok, waited = _session_poll_sentinel(
                    sftp, f["log"], offset, token, wait)
                text_all = data.decode("utf-8", "replace")
                out_text, exit_code, tok_hit = _session_slice_by_sentinel(text_all, token)
                # 超时分支 slice 会返回 None（目标哨兵还没出现）——不能拿它覆盖我们已知的 token，
                # 否则消费者没法用 --token 继续等（实测踩到：result.token 变 None）
                if tok_hit:
                    token = tok_hit
            else:
                size = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            truncated, omitted = False, 0
            if len(out_text.encode("utf-8")) > args.max_output:
                cut, truncated, omitted = _truncate_output(
                    out_text.encode("utf-8"), args.max_output, "stdout")
                out_text = cut.decode("utf-8", "replace")
            done = exit_code is not None
            result = {
                "ok": True, "action": "session", "version": VERSION, "session": args.name,
                "token": token, "sent_bytes": len(cmd.encode("utf-8")),
                "stdout": out_text, "stream": "stdout+stderr",
                "bytes_returned": len(out_text.encode("utf-8")),
                "log": f["log"], "log_bytes": size, "next_offset": size,
                "status": "done" if done else "running",
                "exit_code": exit_code,
                "exit_success": (exit_code == 0) if done else None,
                "waited_ms": int(waited * 1000),
                "pid": int(pid) if str(pid).isdigit() else pid,
                "host": conn["host"], "user": conn["user"], "port": conn["port"],
                "warnings": [], "duration_ms": int((time.time() - start) * 1000),
            }
            if truncated:
                result["output_truncated"] = True
                result["omitted_bytes"] = omitted
            if done:
                result["next_action"] = ("命令已结束（exit_code=%s）。继续下一步：再来一条 session run；"
                                         "收尾：session kill" % exit_code)
            else:
                result["next_action"] = ("命令仍在跑（等了 %d ms 未结束）。继续等：session read "
                                         "--wait-rc 30 --token %s；中断它：session ctrl-c；"
                                         "读增量输出：session read --offset %d"
                                         % (result["waited_ms"], token, size))
            if getattr(args, "_crlf_normalized", 0):
                result["crlf_normalized"] = args._crlf_normalized
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        _emit_result(args, result, header="[SESSION %s run %s] exit=%s token=%s"
                     % (args.name, "done" if result["status"] == "done" else "running",
                        result["exit_code"], result["token"]),
                     sections=[("OUT", result["stdout"])])
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_run_failed", str(e))
        return 255
    finally:
        close_all(client)


def cmd_session_read(args):
    """读会话输出：尾部 N 行 / --offset 增量读 / --wait-rc 等某条命令结束。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    if args.lines is not None and args.offset is not None:
        emit_error(args.json, "bad_args", "--lines 与 --offset 互斥（尾部 N 行 / 增量读，二选一）")
        return 2
    if args.wait_rc and args.wait_rc > SESSION_WAIT_MAX:
        emit_error(args.json, "bad_args",
                   "--wait-rc 上限 %d 秒（宿主单次调用约 600s；更久请稍后轮询）" % SESSION_WAIT_MAX)
        return 2
    conn, client, f, tmux, pid, _alive, ec = _session_load(args, root, args.name or "main")
    if ec is not None:
        return ec
    try:
        sftp = open_sftp(client)
        try:
            size = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            offset = args.offset
            exit_code, token, status, waited = None, None, "running", 0.0
            raw = b""
            if args.wait_rc:
                # 等哪条命令：--token > last.token（不带 --offset 时）> 任意新哨兵
                want = args.token
                if want is None and offset is None:
                    want = _session_last_token(sftp, f)
                data, size, _rc_any, _tok_any, waited = _session_poll_sentinel(
                    sftp, f["log"], offset if offset is not None else 0, want, args.wait_rc)
                raw = data
                next_offset = size
            elif offset is not None:
                data, size = _session_sftp_read(sftp, f["log"], offset, args.max_output)
                raw, next_offset = data, offset + len(data)
            else:
                back = min(size, SESSION_TAIL_WINDOW)
                data, _ = _session_sftp_read(sftp, f["log"], size - back, None)
                raw, next_offset = data, size
            text_all = raw.decode("utf-8", "replace")
            lines_applied = None
            if args.wait_rc:
                out_text, exit_code, token = _session_slice_by_sentinel(
                    text_all, args.token or (None if offset is not None
                                             else _session_last_token(sftp, f)))
                if exit_code is not None:
                    status = "done"
                if args.lines:      # --wait-rc 时 --lines 同样生效（取该命令输出的末尾 N 行）
                    out_text = _session_tail_lines(out_text, args.lines)
                    lines_applied = args.lines
            else:
                out_text = _session_clean_text(text_all, not args.keep_ansi)
                if offset is None:
                    # 尾部读按 --lines 截尾（默认 SESSION_DEFAULT_LINES 行）
                    # —— B3：此前该参数被完全忽略（20000 行日志 --lines 5 回传上万行）
                    lines_applied = args.lines or SESSION_DEFAULT_LINES
                    out_text = _session_tail_lines(out_text, lines_applied)
            truncated, omitted = False, 0
            if len(out_text.encode("utf-8")) > args.max_output:
                cut, truncated, omitted = _truncate_output(
                    out_text.encode("utf-8"), args.max_output, "stdout")
                out_text = cut.decode("utf-8", "replace")
            result = {
                "ok": True, "action": "session", "version": VERSION, "session": args.name,
                "stdout": out_text, "stream": "stdout+stderr", "bytes_returned": len(out_text.encode("utf-8")),
                "log_bytes": size, "next_offset": next_offset,
                "has_more": bool(next_offset < size), "status": status,
                "exit_code": exit_code, "exit_success": (exit_code == 0) if exit_code is not None else None,
                "token": token or args.token,
                "pid": int(pid) if str(pid).isdigit() else pid,
                "host": conn["host"], "user": conn["user"], "port": conn["port"],
                "warnings": [], "duration_ms": int((time.time() - start) * 1000),
            }
            if args.wait_rc:
                result["waited_ms"] = int(waited * 1000)
                result["wait_rc_secs"] = args.wait_rc
            if lines_applied:
                result["lines_returned"] = len(out_text.split("\n")) if out_text else 0
                result["lines_requested"] = lines_applied
            if truncated:
                result["output_truncated"] = True
                result["omitted_bytes"] = omitted
            if status == "done":
                result["next_action"] = ("该命令已结束（exit_code=%s）。继续下一条：session send；"
                                         "结束会话：session kill" % exit_code)
            else:
                result["next_action"] = ("命令仍在运行。继续读：session read --offset %d；"
                                         "等它结束：--wait-rc 30；中断它：session ctrl-c"
                                         % next_offset)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        _emit_result(args, result, header="[SESSION %s %s] log=%s"
                     % (args.name, "done exit=%s" % result["exit_code"]
                        if result.get("exit_code") is not None else "running", f["log"]),
                     sections=[("OUT", result["stdout"])])
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_read_failed", str(e))
        return 255
    finally:
        close_all(client)


def cmd_session_ctrl_c(args):
    """中断会话里正在执行的命令（tmux 注入 C-c；`--force` = SIGKILL 前台进程组）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    name = args.name or "main"
    conn, client, f, tmux, pid, _alive, ec = _session_load(args, root, name)
    if ec is not None:
        return ec
    try:
        sig = "KILL" if args.force else "INT"
        rc, out, err = _session_run(client, _session_ctrl_c_cmd(f, tmux, force=args.force),
                                    timeout=20)
        marks = dict(_SESSION_MARK_RE.findall(out))
        groups = [g for g in (marks.get("GROUPS") or "").split() if g]
        children = [p for p in (marks.get("CHILDREN") or "").split() if p]
        # tmux 引擎不再需要"INT 无效就升级 TERM"那条路（tty 行规程会正确投递 SIGINT），
        # 但字段保留且恒空——AI 侧零感知（见 SPEC C3）。
        escalated = [p for p in (marks.get("ESCALATED") or "").split() if p]
        n = int(marks.get("SIGNALED") or 0)
        injected = None
        if n:
            # 被中断的命令**不会自己产出哨兵**（bash 收到 SIGINT 后丢弃当前命令行，实测两次），
            # 于是正等着 `read --wait-rc --token X` 的调用方会一直 running。这里**代它补一条**
            # 哨兵（退出码与信号一致：INT→130、KILL→137），让正在等它的 read --wait-rc 能收敛。
            # 只对 `last.token`（最近一条命令）补：中断的几乎总是它。
            code = 137 if args.force else 130
            try:
                sftp = open_sftp(client)
                try:
                    lt = _session_last_token(sftp, f)
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                if lt:
                    raw = '\necho "%s%s__%d"\n' % (SESSION_RC_PREFIX, lt, code)
                    _irc, iout, _ierr = _session_run(client, _session_paste_cmd(f, tmux),
                                                     stdin_data=raw.encode("utf-8"), timeout=20)
                    if "SENT" in dict(_SESSION_MARK_RE.findall(iout)):
                        injected = {"token": lt, "exit_code": code}
            except Exception as e:
                log("[WARN] 补发被中断命令的哨兵失败（不影响中断本身）：%s" % str(e)[:120])
        result = {
            "ok": True, "action": "session", "version": VERSION, "session": name,
            "signal": sig, "signaled_groups": groups, "signaled_children": children,
            "signaled_count": n, "escalated_to_term": escalated,
            "sid": int(marks["SID"]) if marks.get("SID", "").isdigit() else marks.get("SID"),
            "pid": int(pid) if str(pid).isdigit() else pid,
            "host": conn["host"], "user": conn["user"], "port": conn["port"],
            "warnings": [], "duration_ms": int((time.time() - start) * 1000),
        }
        if n == 0:
            result["hint"] = ("会话内当前没有前台命令在跑（可能空闲）——用 session read 确认输出")
            result["next_action"] = "先 session read --offset <上次 next_offset> 看当前状态"
        else:
            tail = ""
            if injected:
                tail = ("。已代被中断的命令补发退出码哨兵（token=%s，exit=%d）——"
                        "正在等它的 read --wait-rc 会收敛" % (injected["token"], injected["exit_code"]))
            result["next_action"] = ("已中断 %s 个前台进程组（%s）：用 session read 看输出后直接发下一条%s"
                                     % (len(groups) or n, sig, tail))
        _emit_result(args, result, header="[SESSION %s] %s -> %s groups"
                     % (name, sig, len(groups) or n))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_ctrl_c_failed", str(e))
        return 255
    finally:
        close_all(client)


def cmd_session_keys(args):
    """向会话注入按键/文本（应答提示、Ctrl-D 等；不是信号，中断用 ctrl-c）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    name = args.name or "main"
    if args.cmd_file and args.data is not None:
        emit_error(args.json, "bad_args", "--data 与 --cmd-file 互斥")
        return 2
    if args.cmd_file:
        try:
            if args.cmd_file == "-":
                payload = sys.stdin.buffer.read()
            else:
                with open(_fix_msys_local_path(args.cmd_file), "rb") as fh:
                    payload = fh.read()
        except Exception as e:
            emit_error(args.json, "read_cmd_failed", str(e))
            return 2
    elif args.data is not None:
        payload = (_session_unescape(args.data) if not args.raw
                   else args.data).encode("utf-8")
    else:
        emit_error(args.json, "bad_args", "需 --data '文本'（支持 \\n \\r \\t \\xNN 转义）或 --cmd-file")
        return 2
    conn, client, f, tmux, pid, _alive, ec = _session_load(args, root, name)
    if ec is not None:
        return ec
    try:
        rc, out, _err = _session_run(client, _session_paste_cmd(f, tmux),
                                     stdin_data=payload, timeout=20)
        if rc != 0 or "SENT" not in dict(_SESSION_MARK_RE.findall(out)):
            emit_error(args.json, "keys_failed",
                       "按键/文本未能灌进会话（tmux load-buffer/paste-buffer 失败：会话可能刚退出）",
                       extra={"session": name, "tmux": tmux})
            return 255
        result = {
            "ok": True, "action": "session", "version": VERSION, "session": name,
            "bytes_sent": len(payload), "log": f["log"],
            "pid": int(pid) if str(pid).isdigit() else pid,
            "host": conn["host"], "user": conn["user"], "port": conn["port"],
            "warnings": [], "duration_ms": int((time.time() - start) * 1000),
            "next_action": "用 session read 读程序对这次输入的响应（会话状态保留）",
        }
        _emit_result(args, result, header="[SESSION %s] keys %d bytes" % (name, len(payload)))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "keys_failed", str(e))
        return 255
    finally:
        close_all(client)


def cmd_session_list(args):
    """列会话（名称/pid/存活/pty/日志大小/最后活动）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        # 先扫再取数据：否则"刚被扫掉的会话"还会出现在后面合并进来的 tmux 列表里
        # （表现为"目录缺失的活会话"假警报——实测踩过）
        _session_sweep(client, root)
        ls_map, marks = _session_tmux_ls(client)
        ec = _session_tmux_gate(args.json, marks, {})
        if ec is not None:
            return ec
        sftp = open_sftp(client)
        try:
            try:
                entries = sftp.listdir_attr(root)
            except IOError:
                entries = []
            names = []
            for e in sorted(entries, key=lambda x: x.filename):
                if stat.S_ISDIR(e.st_mode or 0) and _SESSION_NAME_RE.match(e.filename):
                    names.append(e.filename)
            sessions = []
            for name in names:
                f = _session_files(root, name)
                ent = ls_map.get(name)
                s = _session_info(client, f, sftp=sftp, tmux_info=ent)
                s.update({"session": name, "log": f["log"], "dir": f["dir"]})
                s["alive"] = bool(ent)
                s["status"] = "running" if ent else "dead"
                if ent:
                    s.setdefault("pid", ent.get("pane_pid"))
                    s.setdefault("shell_pid", ent.get("pane_pid"))
                else:
                    s.setdefault("pid", None)
                    s.setdefault("shell_pid", None)
                # 空闲回收进度（AI 据此判断"还能放多久"）：ttl - idle 就是剩余保活时间
                if s.get("ttl_seconds") and s.get("idle_seconds") is not None:
                    s["expires_in_seconds"] = max(0, s["ttl_seconds"] - s["idle_seconds"])
                sessions.append(s)
            # tmux 里有、目录却没有（会话目录被外部删过）：也要能看见，否则"看不见的活会话"
            for name in sorted(set(ls_map) - set(names)):
                ent = ls_map[name]
                fx = _session_files(root, name)
                sessions.append({"session": name, "alive": True, "status": "running",
                                 "pid": ent.get("pane_pid"), "shell_pid": ent.get("pane_pid"),
                                 "cols": ent.get("cols"), "pty": True,
                                 "tmux_session": ent.get("tmux"),
                                 "current_command": ent.get("cur"),
                                 "dir": fx["dir"], "log": fx["log"],
                                 "session_dir_missing": True})
            result = {"ok": True, "action": "session", "version": VERSION,
                      "session_dir": root, "sessions": sessions, "count": len(sessions),
                      "host": conn["host"], "user": conn["user"], "port": conn["port"],
                      "warnings": [], "duration_ms": int((time.time() - start) * 1000)}
            if sessions:
                result["next_action"] = ("读某个会话：session read --name <name>；"
                                         "结束它：session kill --name <name>")
                stale = [s for s in sessions
                         if s.get("alive") and (s.get("age_seconds") or 0) >= _SESSION_STALE_HINT]
                if stale:
                    # "用了忘了关"的护栏：out.log 只增不减，挂久了白占远端资源
                    result["warnings"].append(
                        "会话 %s 已常驻超过 %d 小时（out.log 只增不减）——"
                        "不再需要时请 session kill"
                        % (",".join("%s(%.1fh)" % (s["session"], s["age_seconds"] / 3600.0)
                                    for s in stale), _SESSION_STALE_HINT // 3600))
                near = [s for s in sessions if s.get("status") == "running"
                        and s.get("expires_in_seconds") is not None
                        and s["expires_in_seconds"] <= 120]
                if near:
                    result["warnings"].append(
                        "会话 %s 即将因空闲被回收（剩余 %s；想留住就发一条命令或 --ttl 0 重开）"
                        % (",".join("%s(%s)" % (s["session"], _fmt_age(s["expires_in_seconds"]))
                                    for s in near), _fmt_age(near[0]["expires_in_seconds"])))
                dead = [s["session"] for s in sessions if s.get("status") == "dead"]
                if dead:
                    result["warnings"].append(
                        "会话 %s 的 tmux 会话已不存在（在会话里 `exit` 过？）："
                        "用 session kill 清理残留目录" % ",".join(dead))
                nodir = [s["session"] for s in sessions if s.get("session_dir_missing")]
                if nodir:
                    result["warnings"].append(
                        "会话 %s 还活着但目录缺失（会话目录被外部删了）：read/run 可能读不到输出，"
                        "建议 kill 后重开" % ",".join(nodir))
            else:
                result["next_action"] = "还没有会话：session start <target> [--name main]"
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        _emit_result(args, result, header="[SESSIONS] %d in %s" % (result["count"], root))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_list_failed", str(e))
        return 255
    finally:
        close_all(client)


def cmd_session_kill(args):
    """结束会话：tmux `kill-session` + 进程树闭包 TERM→KILL→校验，默认连目录一起删。

    `orphans` / `orphans_total` / `orphan_remaining_total` **恒返回且恒空**：tmux 引擎下
    "目录没了但进程还在"的结构性孤儿不复存在（进程归 tmux 管），但字段保留——AI 侧零感知。
    """
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    if not args.all and not args.name:
        emit_error(args.json, "bad_args", "需 --name <会话名> 或 --all（结束该主机全部会话）")
        return 2
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        ls_map, marks0 = _session_tmux_ls(client)
        ec = _session_tmux_gate(args.json, marks0, {})
        if ec is not None:
            return ec
        if args.all:
            names = []
            sftp = open_sftp(client)
            try:
                try:
                    for e in sftp.listdir_attr(root):
                        if not _SESSION_NAME_RE.match(e.filename):
                            continue
                        # 只认"看起来真是会话"的目录（有 tmux 名文件或 meta），
                        # 避免把 --session-dir 指向共享目录时误删别人的东西
                        f = _session_files(root, e.filename)
                        if (_session_remote_exists(sftp, f["tmux"])
                                or _session_remote_exists(sftp, f["meta"])):
                            names.append(e.filename)
                except IOError:
                    names = []
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            # tmux 里活着但目录缺失的会话也要一并收掉（否则 --all 之后还留着活会话）
            names.extend(n for n in ls_map if n not in names)
        else:
            if not _session_check_name(args.json, args.name):
                return 2
            names = [args.name]
        results = []
        for name in names:
            f = _session_files(root, name)
            tmux, _s, _p = _session_tmux_pair(name)
            pane_pid = (ls_map.get(name) or {}).get("pane_pid")
            rc, out, kerr = _session_run(
                client, _session_kill_cmd(f, tmux, args.keep_dir, pane_pid=pane_pid), timeout=40)
            marks = dict(_SESSION_MARK_RE.findall(out))
            ec2 = _session_tmux_gate(args.json, marks, {"session": name, "dir": f["dir"]})
            if ec2 is not None:
                return ec2
            left = int(marks.get("LEFT") or 0)
            swept = int(marks.get("SWEPT") or 0)
            roots = int(marks.get("ROOTS") or 0)
            had_dir = marks.get("HAD") == "1"
            entry = {"session": name, "swept": swept, "remaining": left, "roots": roots,
                     "dir": f["dir"],
                     "cleaned": (marks.get("CLEANED") == "1") and not args.keep_dir,
                     # verified=真的确认过"会话进程已不在"：拿到过 pane_pid 且没有幸存者。
                     # swept=0 不等于"清干净"（旧版会因此误报）
                     "verified": bool(roots) and left == 0}
            if marks.get("KILLED") == "1":
                entry["tmux_killed"] = True
            if not roots and had_dir:
                entry["note"] = ("没拿到 pane pid（会话已先一步消失/被外部 kill）：无法核对进程树，"
                                 "按 tmux/会话语义应已无残留；如需自查："
                                 "`ps -eo pid,ppid,tty,args | grep pyaissh-sessions`")
            results.append(entry)
            if rc != 0 and not marks:
                results[-1]["note"] = (kerr or out or "")[-200:]

        # 会话全清了就把 reaper 也收掉（否则它要挂到下一轮才发现"没会话了"）
        try:
            _sftp2 = open_sftp(client)
            try:
                left_dirs = [e.filename for e in _sftp2.listdir_attr(root)
                             if stat.S_ISDIR(e.st_mode or 0)
                             and _SESSION_NAME_RE.match(e.filename)]
            finally:
                try:
                    _sftp2.close()
                except Exception:
                    pass
        except Exception:
            left_dirs = ["?"]        # 读不到就保守处理：不动 reaper
        if not left_dirs:
            try:
                _rrc, rout, _rerr = _session_run(client, _session_reaper_stop_cmd(root), timeout=15)
                if "REAPER=stopped" in (rout or ""):
                    log("[SESSION KILL] 已停掉本主机的空闲回收 reaper（没有会话了）")
            except Exception:
                pass

        # 结构性孤儿在 tmux 引擎下不复存在（进程生命周期归 tmux）；字段保留且恒空
        orphans = []
        result = {"ok": True, "action": "session", "version": VERSION,
                  "sessions": results, "count": len(results),
                  "remaining_total": sum(r["remaining"] for r in results),
                  "verified_total": sum(1 for r in results if r.get("verified")),
                  "orphans": orphans, "orphans_total": 0, "orphan_remaining_total": 0,
                  "host": conn["host"], "user": conn["user"], "port": conn["port"],
                  "warnings": [], "duration_ms": int((time.time() - start) * 1000)}
        if result["remaining_total"]:
            result["warnings"].append(
                "仍有 %d 个进程属于被结束会话的进程树（可能正在退出或有 SIGKILL 也杀不掉的状态）"
                % result["remaining_total"])
        unverified = [r["session"] for r in results if r.get("note") and not r.get("verified")]
        if unverified:
            result["warnings"].append(
                "会话 %s 的清理**未获确认**（没拿到 pane pid，或没找到活着的会话进程）："
                "按 note 里的 ps 自查，必要时手工 kill 或 pyaissh session kill --all"
                % ",".join(unverified))
        result["next_action"] = ("会话已清理。重开：session start <target> --name <name>"
                                 if names else "没有需要清理的会话")
        _emit_result(args, result, header="[SESSION KILL] %d 个会话" % len(results))
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "session_kill_failed", str(e))
        return 255
    finally:
        close_all(client)
