"""常驻会话子命令实现（域 11）——真 PTY 会话：逐条喂命令 + 状态保留 + 可中断。

为什么需要它（与 exec / exec --detach 的分工）：
- `exec`：一条命令一次调用，**无状态**（cd/export 不跨调用保留），受宿主单次调用时长限制
- `exec --detach`：一条长命令丢后台，**启动后不能改**，错了只能 --kill 重启
- `session`：远端一个常驻 shell（真 PTY），**逐条喂命令**——每条独立退出码，
  **打错了就把那条命令改对再发一遍**（同一条重试，不是换下一条）：报错 → 改名/改参数 → 重发 → 成功，
  像人在终端里那样；cd/export/函数等状态都在，所以重发时上下文与上次完全一致；执行中的命令**可中断**（ctrl-c）

实测依据（v2.3 开发期真机验证，详见 docs/session.md）：
- util-linux `script` 给出真 PTY：`test -t 0` 为真、`tty` = /dev/pts/N，可应答 `read -p` 提示
- PTY 下 bash 有 job control → **每条命令独立进程组** → `kill -INT -- -<pgid>` 即 Ctrl-C 语义
- 往 FIFO 写 0x03 想靠 pty 行规程转 SIGINT **实测无效** ⇒ 本实现只用进程组信号
- 只杀会话 leader 的进程组会留下 job 自己的进程组（实测踩过孤儿）
  ⇒ kill 按**进程树闭包**清（不是按 sid：见 `_session_kill_cmd` docstring）
"""

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")  # 防路径穿越
_SESSION_RC_RE = re.compile(re.escape(SESSION_RC_PREFIX) + r"([0-9a-f]{4,16})__(\d{1,3})")
# 远端标记统一 `__PYAISSH_SESS__KEY=VALUE`（v2.3：早期写成 `__KEY__VALUE__KEY2__VALUE2`，
# 贪婪匹配会把 PID 吃成 "63864__PTY__1"——实测踩到）
_SESSION_MARK_RE = re.compile(r"__PYAISSH_SESS__([A-Z_]+)=(\S*)")

# list 里对"挂了超过这个时长还活着"的会话给一条提醒（用久了忘 kill 的护栏）
_SESSION_STALE_HINT = 86400

# 空闲回收（v2.3.0 空闲 TTL，用户设计）：会话是远端常驻进程，**不会自己退出**；
# 但"没人用的会话"不该白占远端资源。规则（两个条件同时成立才回收）：
#   ① 提示符空闲——没有前台子进程在跑（`pgrep -P <会话 shell>` 为空），所以构建/安装不会被误杀；
#   ② 距上次 pyaissh 交互（beat 文件 mtime）超过 TTL。
# 交互即续期：send/run/read/ctrl-c/keys/start(attach) 都刷新 beat；`list` 不算（看一眼≠在用）。
# `--ttl 0` 关闭回收；环境变量 PYAISSH_SESSION_TTL 改默认值；看门狗每 _SESSION_TTL_TICK 秒查一次，
# 所以实际回收时间在 TTL..TTL+TICK 之间。
_SESSION_TTL_DEFAULT = 600
_SESSION_TTL_TICK = 15


def _session_files(root, name):
    """会话远端路径表（与作业同款：一个会话一个 0700 目录）。"""
    d = "%s/%s" % (root.rstrip("/"), name)
    return {"dir": d, "fifo": d + "/in", "log": d + "/out.log", "err": d + "/err.log",
            "pid": d + "/sess.pid", "meta": d + "/meta", "bash": d + "/bash.pid",
            "token": d + "/last.token", "beat": d + "/beat", "watch": d + "/watch.pid",
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

    会话的 FIFO 写入必须走这条（SFTP 打开 FIFO 会阻塞/失败）；base64 载荷也走 stdin，
    避开 argv 长度与引号问题。辅助命令输出量都很小，直接 read() 不会死锁。
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


def _session_watchdog_script(f, ttl):
    """空闲回收看门狗脚本（v2.3.0 空闲 TTL）：以独立 setsid 进程跑，两个条件同时成立才回收。

      ① 提示符空闲：`pgrep -P <会话 shell>` 为空（没有前台命令在跑）——构建/安装不会被误杀；
      ② 距上次 pyaissh 交互超过 TTL：beat 文件的 mtime 由每次交互刷新（`_session_touch`）。
    为什么独立进程而不是会话树内的一员：
      - 会话是 `setsid nohup` 起的，看门狗也必须脱离发起它的那次 SSH 连接（否则 start 一返回
        就随连接收到 SIGHUP 而死）；
      - 独立进程不在会话树闭包内 ⇒ 它做清理时不会把自己先杀掉（能走完 TERM→KILL→校验）。
    回收动作与 `kill` 同款：**自证**闭包（argv 含本会话目录才认，防 pid 回收误杀）→ 先删目录
    （即使自己被信号打断也不留残留目录）→ TERM → 宽限 → KILL。
    退出条件：会话目录消失（已被 kill/回收）就退，**不留常驻循环**。
    beat 缺失时只续期不回收（宁可多留也不误杀）。
    """
    q = _sh_quote
    return (
        "#!/bin/bash\n"
        "# pyaissh 空闲回收看门狗（自动生成；TTL=%d 秒，每 %d 秒检查一次）\n"
        "D=%s; BEAT=%s; BPID=%s; SPID=%s; TTL=%d; TICK=%d\n"
        "while :; do\n"
        "  sleep \"$TICK\"\n"
        "  [ -d \"$D\" ] || exit 0\n"
        "  B=$(cat \"$BPID\" 2>/dev/null)\n"
        "  if [ -n \"$B\" ] && [ -n \"$(pgrep -P \"$B\" 2>/dev/null)\" ]; then touch \"$BEAT\"; continue; fi\n"
        "  NOW=$(date +%%s); LAST=$(stat -c %%Y \"$BEAT\" 2>/dev/null)\n"
        "  [ -n \"$LAST\" ] || { touch \"$BEAT\"; continue; }\n"
        "  [ $((NOW - LAST)) -gt \"$TTL\" ] || continue\n"
        "  P=$(cat \"$SPID\" 2>/dev/null); T=\"\"; SEEN=\"\"; ROOTS=0; SNAP=$(ps -eo pid=,ppid=)\n"
        "  for r in \"$P\" \"$B\" \"$(ps -o ppid= -p \"$B\" 2>/dev/null | tr -d ' ')\"; do\n"
        "    [ -n \"$r\" ] || continue\n"
        "    [ \"$r\" = 1 ] && continue\n"
        "    case \" $SEEN \" in *\" $r \"*) continue ;; esac\n"
        "    kill -0 \"$r\" 2>/dev/null || continue\n"
        "    A=$(ps -o args= -p \"$r\" 2>/dev/null)\n"
        "    case \"$A\" in *\"$D\"*) ;; *) continue ;; esac\n"
        "    SEEN=\"$SEEN $r\"; ROOTS=$((ROOTS+1))\n"
        "    T=\"$T $(echo \"$SNAP\" | %s)\"\n"
        "  done\n"
        "  [ \"$ROOTS\" -gt 0 ] || exit 0\n"
        "  T=$(echo $T | tr ' ' '\\n' | sort -u -n | tr '\\n' ' ')\n"
        "  rm -rf \"$D\"\n"
        "  kill -TERM $T 2>/dev/null; sleep 0.5\n"
        "  K=\"\"; for p in $T; do kill -0 \"$p\" 2>/dev/null && K=\"$K $p\"; done\n"
        "  [ -n \"$K\" ] && kill -KILL $K 2>/dev/null\n"
        "  exit 0\n"
        "done\n"
        % (int(ttl), _SESSION_TTL_TICK, q(f["dir"]), q(f["beat"]), q(f["bash"]), q(f["pid"]),
           int(ttl), _SESSION_TTL_TICK, _SESSION_TREE_AWK % "$r"))


def _session_watchdog_launch_cmd(f, ttl):
    """把看门狗脚本落成文件并以 setsid 独立进程启动（base64 传输，绕开所有引号问题）。

    TTL<=0 时返回空串（不装看门狗）。脚本落在会话目录内，随目录一起被清掉。

    **整组后台任务必须自己重定向三个流**（实测教训：只给 `setsid` 那条加 `>/dev/null`、
    却让同组的 `printf | base64 -d > watch.sh` / `chmod` 继承 SSH 通道的 stderr ⇒
    通道永不 EOF ⇒ paramiko 的 `recv_exit_status()` 一直等，`session start` 20 秒超时
    报 `session_failed`（消息为空），而会话其实已经起好了）。所以用 `{ ...; } >/dev/null
    2>&1 </dev/null &` 把整组包住。
    """
    if not ttl or ttl <= 0:
        return ""
    q = _sh_quote
    b64 = base64.b64encode(_session_watchdog_script(f, ttl).encode("utf-8")).decode("ascii")
    w = f["dir"] + "/watch.sh"
    return ("{ printf %%s '%s' | base64 -d > %s && chmod 700 %s && "
            "setsid nohup bash %s >/dev/null 2>&1 </dev/null & echo $! > %s; } "
            ">/dev/null 2>&1 </dev/null & "
            % (b64, q(w), q(w), q(w), q(f["watch"])))


def _session_touch(client, f):
    """刷新会话的"最后交互时间"（beat mtime）——空闲回收据此判断"没人用了"。

    失败不致命（旧会话没有 beat 文件、或远端 stat/touch 异常）：只记一条 WARN 到 stderr，
    绝不让它影响正常调用。`list` 不算交互（看一眼不代表在用），故不调用本函数。
    """
    rc, out, err = _session_run(client, "touch %s 2>/dev/null || true" % _sh_quote(f["beat"]),
                                timeout=10)
    if rc != 0 and err.strip():
        log("[WARN] 刷新会话活动时间失败（不影响本次调用）：%s" % err.strip()[:160])
    return rc == 0


def _session_start_cmd(f, cols, no_pty=False, ttl=0):
    """启动常驻会话的远端脚本（成功时输出 __PYAISSH_SESS__PID__<pid>__PTY__<0|1>）。

    - setsid + nohup：脱离本连接，SSH 断开不影响
    - `exec 9<>FIFO`：以**读写**方式持有 FIFO（否则写端每次关闭都会让读循环 EOF 退出）
    - script -qfc：给会话真 PTY（stty -echo 关输入回显、固定列宽；exec bash -i 交互壳）
    - 无 script 时降级为非 PTY 常驻 bash（状态与退出码都在，但没有 tty）
    - ttl > 0：另起一个空闲回收看门狗（见 `_session_watchdog_script`），meta 第四字段记 TTL
    """
    q = _sh_quote
    pty_pref = "0" if no_pty else "1"
    inner = ("exec 9<>%s; script -qfc 'stty -echo; stty cols %d rows 50; exec bash -i' %s <&9"
             % (q(f["fifo"]), cols, q(f["log"])))
    plain = ("exec 9<>%s; while IFS= read -r __l <&9; do eval \"$__l\"; done" % q(f["fifo"]))
    return (
        "umask 077; D=%s; " % q(f["dir"]) +
        "if [ -f %s ] && kill -0 \"$(cat %s)\" 2>/dev/null; then "
        "echo \"__PYAISSH_SESS__EXISTS=$(cat %s)\"; exit 0; fi; " % (q(f["pid"]), q(f["pid"]),
                                                                    q(f["pid"])) +
        "mkdir -p \"$D\" && chmod 700 \"$D\" || { echo __PYAISSH_SESS__MKDIR_FAIL=1; exit 1; }; " +
        "[ -p %s ] || mkfifo -m 600 %s || { echo __PYAISSH_SESS__FIFO_FAIL=1; exit 1; }; " % (
            q(f["fifo"]), q(f["fifo"])) +
        "rm -f %s; : > %s; chmod 600 %s; " % (q(f["log"]), q(f["log"]), q(f["log"])) +
        "if [ %s = 0 ]; then PTY=0; elif command -v script >/dev/null 2>&1; then PTY=1; else PTY=0; fi; " % pty_pref +
        "if [ \"$PTY\" = 1 ]; then setsid nohup bash -c \"%s\" >>%s 2>&1 </dev/null & " % (
            inner, q(f["err"])) +
        # 非 PTY 分支必须把会话输出写进 out.log（早期误写成 err.log ⇒ out.log 永远为空、
        # read/run 永远 running+空输出——实测 --no-pty 完全不可用）
        "else setsid nohup bash -c '%s' >>%s 2>&1 </dev/null & fi; " % (plain, q(f["log"])) +
        "echo $! > %s; sleep 0.4; " % q(f["pid"]) +
        "touch %s; chmod 600 %s 2>/dev/null; " % (q(f["beat"]), q(f["beat"])) +
        "printf '%%s %%s %%s %%s\\n' \"$PTY\" %d \"$(date +%%s)\" %d > %s; " % (cols, int(ttl or 0),
                                                                               q(f["meta"])) +
        "chmod 600 %s 2>/dev/null; " % q(f["meta"]) +
        _session_watchdog_launch_cmd(f, ttl) +
        "if kill -0 \"$(cat %s)\" 2>/dev/null; then "
        "echo \"__PYAISSH_SESS__PID=$(cat %s)\"; echo \"__PYAISSH_SESS__PTY=$PTY\"; "
        "else echo __PYAISSH_SESS__DEAD=1; exit 1; fi"
        % (q(f["pid"]), q(f["pid"])))


def _session_probe_cmd(f):
    """会话状态探测：ALIVE / DEAD / MISSING（+ pid）。"""
    q = _sh_quote
    return ("if [ -f %s ]; then P=$(cat %s 2>/dev/null); "
            "if [ -n \"$P\" ] && kill -0 \"$P\" 2>/dev/null; then echo \"__PYAISSH_SESS__ALIVE=$P\"; "
            "else echo \"__PYAISSH_SESS__DEAD=$P\"; fi; else echo __PYAISSH_SESS__MISSING=1; fi"
            % (q(f["pid"]), q(f["pid"])))


def _session_send_cmd(f):
    """从 stdin 读 base64 载荷写入 FIFO（timeout 5 兜住"无读者时 open 阻塞"）。"""
    return 'timeout 5 sh -c "base64 -d > %s"' % _sh_quote(f["fifo"])


def _session_keys_cmd(f):
    """从 stdin 读**原始字节**写入 FIFO（应答提示、Ctrl-D 等）。"""
    return 'timeout 5 sh -c "cat > %s"' % _sh_quote(f["fifo"])


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


def _session_ctrl_c_cmd(f, sig, escalate=True):
    """中断会话里**正在执行的命令**：先对它的进程组发信号，幸存者升级为 TERM/KILL。

    发现路径（不依赖 sid —— 实测 `script` 的子 shell 自己 setsid 成新会话，
    starter 的 sid 与 pty 会话无关）：
      bash.pid（会话 shell，start 时写入）→ `pgrep -P` 取**直接子进程**（= 前台 job）
      → 若该子进程有独立进程组（PTY 下 job control）则整组发信号，否则按 pid 发

    升级是必需的：实测 `kill -INT <sleep pid>` 返回成功但进程没死
    （会话树由 `setsid nohup` 起，SIGINT 处置被继承为忽略），所以默认升级为 TERM。
    """
    q = _sh_quote
    esc = ("sleep 0.7; K=\"\"; for p in $T; do kill -0 \"$p\" 2>/dev/null && K=\"$K $p\"; done; "
           "if [ -n \"$K\" ]; then kill -TERM $K 2>/dev/null; echo \"__PYAISSH_SESS__ESCALATED=$K\"; "
           "sleep 0.5; fi; "
           if escalate else "")
    return ("B=$(cat %s 2>/dev/null); "
            "if [ -z \"$B\" ] || ! kill -0 \"$B\" 2>/dev/null; then echo __PYAISSH_SESS__DEAD=1; exit 0; fi; "
            "S=$(ps -o sid= -p \"$B\" 2>/dev/null | tr -d ' '); "
            "T=$(pgrep -P \"$B\" 2>/dev/null | tr '\\n' ' '); "
            "echo \"__PYAISSH_SESS__CHILDREN=$T\"; "
            "G=\"\"; N=0; "
            "for c in $T; do g=$(ps -o pgid= -p \"$c\" 2>/dev/null | tr -d ' '); "
            "if [ -n \"$g\" ] && [ \"$g\" != \"$S\" ]; then "
            "case \" $G \" in *\" $g \"*) ;; *) G=\"$G $g\"; kill -%s -- \"-$g\" 2>/dev/null && N=$((N+1));; esac; "
            "else kill -%s \"$c\" 2>/dev/null && N=$((N+1)); fi; done; "
            "echo \"__PYAISSH_SESS__SID=$S\"; echo \"__PYAISSH_SESS__GROUPS=$G\"; "
            "echo \"__PYAISSH_SESS__SIGNALED=$N\"; %s"
            "echo __PYAISSH_SESS__DONE=1"
            % (q(f["bash"]), sig, sig, esc))


def _session_kill_cmd(f, keep_dir=False):
    """结束会话：以**自证的会话进程**为根算进程树闭包 → TERM → 对幸存者 KILL → 校验。

    为什么不用 sid：实测 `script` 的子 shell 自己 setsid 成**新会话**，starter 的 sid
    与 pty 会话无关（早期版本按 sid 清理 ⇒ 会话其实没死、留下 sleep 孤儿）。
    为什么先算集合：父进程被杀后子进程会被 reparent，事后再按树算会漏。

    v2.3.0 加固（R1：不再无声误报"清干净"）：
    - 根候选三个：`sess.pid`(starter) / `bash.pid`(会话 shell) / 会话 shell 的父进程(`script`)。
      为什么加后两个：starter 若被 OOM/外力杀掉，`script` 与 `bash -i` 会被 reparent 到 1 号进程，
      只按 sess.pid 算闭包得空集 ⇒ 旧版会报 swept=0/remaining=0/cleaned=true 而会话仍在跑；
      此时从 `script`（argv 里带 out.log 路径）做根，闭包仍覆盖 script + bash -i + 正在跑的命令。
    - 根必须**自证**：`ps -o args=` 里含本会话目录（bash -i 自己的 argv 没路径，故看它父进程）。
      这同时堵住 pid 回收误杀——陈旧 pid 被无关进程复用时，argv 不含本会话目录 → 不作为根。
    - `ROOTS=0`（三个候选都不可用/都不自证）时**不猜不杀**，输出 ROOTS=0 让上层把
      `verified` 置 false 并给 warning（附自查命令），而不是宣称已清理。
    - `HAD=1`（目录还在）：上层据此区分"会话本来就没起过"（不必告警）与"可能有孤儿"（告警）。
    """
    q = _sh_quote
    rm = "" if keep_dir else "rm -rf %s" % q(f["dir"])
    return ("D=%s; HAD=0; [ -d \"$D\" ] && HAD=1; "
            "P=$(cat %s 2>/dev/null); B=$(cat %s 2>/dev/null); W=$(cat %s 2>/dev/null); "
            "T=\"\"; SEEN=\"\"; ROOTS=0; SWEPT=0; LEFT=0; SNAP=$(ps -eo pid=,ppid=); "
            "for r in \"$P\" \"$B\" \"$W\" \"$(ps -o ppid= -p \"$B\" 2>/dev/null | tr -d ' ')\"; do "
            "[ -n \"$r\" ] || continue; [ \"$r\" = 1 ] && continue; "
            "case \" $SEEN \" in *\" $r \"*) continue ;; esac; "
            "kill -0 \"$r\" 2>/dev/null || continue; "
            "A=$(ps -o args= -p \"$r\" 2>/dev/null); "
            "case \"$A\" in *\"$D\"*) ;; *) continue ;; esac; "
            "SEEN=\"$SEEN $r\"; ROOTS=$((ROOTS+1)); T=\"$T $(echo \"$SNAP\" | %s)\"; "
            "done; "
            "T=$(echo $T | tr ' ' '\\n' | sort -u -n | tr '\\n' ' '); "
            "SWEPT=$(echo $T | wc -w | tr -d ' '); "
            "if [ -n \"$T\" ]; then "
            "kill -TERM $T 2>/dev/null; sleep 0.6; "
            "K=\"\"; for p in $T; do kill -0 \"$p\" 2>/dev/null && K=\"$K $p\"; done; "
            "if [ -n \"$K\" ]; then kill -KILL $K 2>/dev/null; sleep 0.4; fi; "
            "for p in $T; do kill -0 \"$p\" 2>/dev/null && LEFT=$((LEFT+1)); done; "
            "fi; "
            "echo \"__PYAISSH_SESS__SWEPT=$SWEPT\"; echo \"__PYAISSH_SESS__LEFT=$LEFT\"; "
            "echo \"__PYAISSH_SESS__ROOTS=$ROOTS\"; echo \"__PYAISSH_SESS__HAD=$HAD\"; "
            "rm -f %s %s %s; %s; echo __PYAISSH_SESS__CLEANED=1"
            % (q(f["dir"]), q(f["pid"]), q(f["bash"]), q(f["watch"]), _SESSION_TREE_AWK % "$r",
               q(f["pid"]), q(f["bash"]), q(f["watch"]), rm))


def _session_clean_text(s, strip_ansi=True):
    """会话输出清洗：CRLF/CR → LF、去掉哨兵行与 script 头尾、可选剥 ANSI、去首尾空行。"""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if strip_ansi:
        s = _strip_ansi(s)
    lines = []
    for ln in s.split("\n"):
        if _SESSION_RC_RE.search(ln):
            continue
        if ln.startswith("Script started on ") or ln.startswith("Script done on "):
            continue      # util-linux script 的会话头/尾（纯噪音）
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
    """远端路径是否存在（含 FIFO/目录，不问类型）。"""
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


def _session_info(client, f, sftp=None):
    """读会话元信息：pid / pty / cols / started_at / age_seconds / ttl_seconds /
    idle_seconds（距上次交互）/ log_bytes / mtime —— `list` 与 `start --attach` 共用。

    缺什么就少什么字段，**绝不抛**（会话可能正好被回收/删除）。`alive`/`status` 由调用方补
    （list 用一次批量探测，attach 分支已由 EXISTS 证明存活）。
    """
    info = {}
    own = sftp is None
    if own:
        try:
            sftp = open_sftp(client)
        except Exception:
            return info
    try:
        try:
            with sftp.open(f["pid"], "r") as fh:
                t = fh.read().decode("utf-8", "replace").strip()
            info["pid"] = int(t) if t.isdigit() else t
        except Exception:
            pass
        try:
            with sftp.open(f["meta"], "r") as fh:
                parts = fh.read().decode("utf-8", "replace").split()
            if parts:
                info["pty"] = parts[0] == "1"
                if len(parts) > 1 and parts[1].isdigit():
                    info["cols"] = int(parts[1])
                if len(parts) > 2 and parts[2].isdigit():
                    # meta 第三字段 = start 时的 epoch 秒（此前只用于 age，现在也报 TTL）
                    info["started_at"] = int(parts[2])
                    info["age_seconds"] = max(0, int(time.time()) - int(parts[2]))
                if len(parts) > 3 and parts[3].isdigit():
                    v = int(parts[3])
                    info["ttl_seconds"] = v if v > 0 else None
        except Exception:
            pass
        try:
            st = sftp.stat(f["beat"])
            info["idle_seconds"] = max(0, int(time.time()) - int(st.st_mtime or 0))
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


def cmd_session_start(args):
    """起一个常驻会话（真 PTY）：setsid + script + FIFO（v2.3.0 起带空闲回收 TTL）。

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
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        rc, out, err = _session_run(client, _session_start_cmd(f, args.cols, args.no_pty, ttl),
                                    timeout=max(20, args.timeout + 10))
        marks = _SESSION_MARK_RE.findall(out)
        kinds = [k for k, _ in marks]
        if "EXISTS" in kinds:
            ex_pid = dict((k, v) for k, v in marks).get("EXISTS")
            if args.attach:
                # 接上旧会话：刷新活动时间（别让它刚接上就被空闲回收）
                _session_touch(client, f)
                extra = _session_info(client, f)
                result = {
                    "ok": True, "action": "session", "version": VERSION, "session": name,
                    "attached": True, "dir": f["dir"], "fifo": f["fifo"], "log": f["log"],
                    "pid": int(ex_pid) if str(ex_pid).isdigit() else ex_pid,
                    "ready": True, "ttl_seconds": extra.get("ttl_seconds"),
                    "age_seconds": extra.get("age_seconds"),
                    "idle_seconds": extra.get("idle_seconds"),
                    "host": conn["host"], "user": conn["user"], "port": conn["port"],
                    "warnings": [],
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
        for bad, why in (("MKDIR_FAIL", "无法创建会话目录（权限/磁盘）"),
                         ("FIFO_FAIL", "无法创建 FIFO"), ("DEAD", "会话进程启动后立即退出")):
            if bad in kinds:
                emit_error(args.json, "session_failed", "启动会话失败：%s" % why,
                           extra={"session": name, "dir": f["dir"], "stderr": err[-400:]})
                return 255
        pid = dict((k, v) for k, v in marks).get("PID")
        pty = dict((k, v) for k, v in marks).get("PTY", "1") == "1"
        if pid is None:
            emit_error(args.json, "session_failed",
                       "启动会话失败：未取得会话进程号（远端输出见 stderr）",
                       extra={"session": name, "stdout": out[-400:], "stderr": err[-400:]})
            return 255

        # 就绪确认：发一条初始化命令并等它的哨兵（同时验证 FIFO 通路、记录会话 shell pid）
        init = "PS1=; PS2=; stty -echo 2>/dev/null || true; echo $$ > %s" % _sh_quote(f["bash"])
        token = _session_send_payload(client, f, init, plain=not pty)
        ready, wait_s = False, 0.0
        if token:
            sftp = open_sftp(client)
            try:
                off = 0
                _d, _sz, rc_r, _tok, el = _session_poll_sentinel(
                    sftp, f["log"], off, token, max(2, args.wait_ready), interval=0.2)
                ready = rc_r is not None
                wait_s = el
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        result = {
            "ok": True, "action": "session", "version": VERSION, "session": name,
            "attached": False,
            "dir": f["dir"], "fifo": f["fifo"], "log": f["log"],
            "pid": int(pid) if str(pid).isdigit() else pid,
            "pty": pty, "cols": args.cols, "ready": ready, "ready_wait_ms": int(wait_s * 1000),
            "ttl_seconds": ttl if ttl and ttl > 0 else None,
            "idle_seconds": 0,
            "host": conn["host"], "user": conn["user"], "port": conn["port"],
            "permissions": {"dir": "0700", "fifo": "0600", "out.log": "0600", "meta": "0600"},
            "warnings": [],
            "next_action": ("会话已就绪。逐条执行：pyaissh session send <target> --name %s --cmd '...'，"
                            "再用 session read 读结果（见 next_offset）；中断执行中的命令：session ctrl-c；"
                            "收尾：session kill" % name),
            "duration_ms": int((time.time() - start) * 1000),
        }
        if ttl and ttl > 0:
            result["next_action"] += ("。**空闲回收**：提示符空闲且 %s 内没有任何 pyaissh 交互"
                                      "（send/run/read/ctrl-c/keys）就会自动回收（进程 + 目录），"
                                      "需要保活就 --ttl 0" % _fmt_age(ttl))
        else:
            result["warnings"].append("空闲回收已关闭（--ttl 0）：会话会一直留着，用完记得 session kill")
        if not pty:
            result["warnings"].append(
                "远端没有 util-linux `script`（或指定了 --no-pty）：本次为**非 PTY** 会话——"
                "状态与退出码照常，但没有 tty（需要 TTY 的程序不可用；ctrl-c 退化为对子进程发信号）")
        if not ready:
            result["warnings"].append("会话就绪确认超时（哨兵未出现）：请用 session read 查看 out.log 首屏")
        _emit_result(args, result, header="[SESSION %s pid=%s pty=%s] %s"
                     % (name, pid, pty, f["dir"]))
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


def _session_payload_text(cmd, token, plain=False):
    """命令 → 写入 FIFO 的载荷文本（纯函数，便于单测）。

    两种形态（v2.3.0 修正）：
    - **PTY 模式**（默认）：`{ ...; }; echo 哨兵`。要点：哨兵必须与命令**在同一行被 shell 解析**，
      否则命令里从终端读取的语句（`read -p`）会把紧随其后的哨兵行当输入吃掉（实测踩过）。
      用 `{}` 而非 `()`：大括号是同一个 shell，cd/export 状态照常保留。
      多行形态在 PTY 下没问题——但**行的长度必须短**（tty 规范模式单行上限 ~4096B），
      所以长命令不要拼成一行。
    - **非 PTY 降级模式**（`plain=True`）：`eval "$(printf %s '<b64>' | base64 -d)"; echo 哨兵`。
      降级模式的读取循环是**逐行 eval**，多行载荷会被拆成多段（`{` 单独一行直接 syntax error，
      哨兵永不出现 —— 实测 `--no-pty` 完全不可用）。base64 保证是**一行**，且 eval 在同一 shell
      里执行 ⇒ 状态保留 + 多行命令 + 长命令都不受限。

    v2.3.0 加固（R2）：载荷**以换行开头**。本地如果在上一次写入的**半途**断线（关机/断网/被杀），
    远端的 tty 行规程里会留下**没有换行的半行**；下一次写入会与它**串成同一行**——
    实测后果是语法错误且**哨兵永不出现**（`run` 只能回 running/无 exit_code，AI 被卡住）。
    前置一个换行先把那半行终结掉（它作为一条垃圾命令执行、报错留在 out.log），
    之后真正的载荷在干净的输入行里解析 ⇒ 哨兵照常出现，AI 至少能拿到退出码并从输出看出异常。
    """
    body = cmd.rstrip("\n")
    if plain:
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return "\neval \"$(printf %%s '%s' | base64 -d)\"; echo \"%s%s__$?\"\n" % (
            b64, SESSION_RC_PREFIX, token)
    return "\n{\n%s\n}; echo \"%s%s__$?\"\n" % (body, SESSION_RC_PREFIX, token)


def _session_pty_mode(sftp, f):
    """会话是否 PTY 模式（读 start 时写的 meta：`<pty> <cols> <started_at>`）。

    读不到（老会话/异常）时按 PTY 处理（默认路径）。
    """
    try:
        with sftp.open(f["meta"], "r") as fh:
            parts = fh.read().decode("utf-8", "replace").split()
        return parts[0] == "1" if parts else True
    except Exception:
        return True


def _session_send_payload(client, f, cmd, token=None, plain=False):
    """把「命令 + 退出码哨兵」写进会话 FIFO。返回 token（失败返回 None）。"""
    token = token or os.urandom(4).hex()
    payload = _session_payload_text(cmd, token, plain=plain)
    b64 = base64.b64encode(payload.encode("utf-8"))
    rc, _out, _err = _session_run(client, _session_send_cmd(f), stdin_data=b64, timeout=15)
    return token if rc == 0 else None


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
    """公共前置：校验名字 → 连接 → 探测会话。返回 (conn, client, f, pid, alive, ec)。"""
    if not _session_check_name(args.json, name):
        return None, None, None, None, False, 2
    f = _session_files(root, name)
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return None, None, None, None, False, conn_ec
    rc, out, _err = _session_run(client, _session_probe_cmd(f), timeout=15)
    marks = dict(_SESSION_MARK_RE.findall(out))
    if "MISSING" in marks:
        emit_error(args.json, "session_not_found",
                   "找不到会话 %r（%s 不存在）：可能从没起过，或**已被空闲回收**"
                   "（默认提示符空闲 %s 就自动收，`--ttl 0` 可关）——"
                   "用 pyaissh session start --name %s 重建（要接上还活着的旧会话用 --attach），"
                   "或先用 pyaissh session list 看现有会话"
                   % (name, f["pid"], _fmt_age(_SESSION_TTL_DEFAULT), name),
                   extra={"session": name, "dir": f["dir"],
                          "ttl_default_seconds": _SESSION_TTL_DEFAULT})
        close_all(client)
        return None, None, None, None, False, 2
    alive = "ALIVE" in marks
    pid = marks.get("ALIVE") or marks.get("DEAD")
    if need_alive and not alive:
        emit_error(args.json, "session_dead",
                   "会话 %r 的进程已消失（pid %s）：会话状态不可恢复，"
                   "用 pyaissh session kill 清理残留目录后重新 start" % (name, pid),
                   extra={"session": name, "pid": int(pid) if str(pid).isdigit() else pid,
                          "dir": f["dir"]})
        close_all(client)
        return None, None, None, None, False, 2
    _session_touch(client, f)      # 交互即续期：别让正在用的会话被空闲回收
    return conn, client, f, pid, alive, None


def cmd_session_send(args):
    """把一条命令喂进会话（自动追加退出码哨兵）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    cmd = _session_resolve_cmd(args)
    if cmd is None:
        return 2
    conn, client, f, pid, _alive, ec = _session_load(args, root, args.name or "main")
    if ec is not None:
        return ec
    try:
        sftp = open_sftp(client)
        try:
            try:
                log_bytes = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            except Exception:
                log_bytes = 0
            token = _session_send_payload(client, f, cmd,
                                          plain=not _session_pty_mode(sftp, f))
            if token is None:
                emit_error(args.json, "send_failed",
                           "命令未能写入会话 FIFO（会话可能刚退出或 FIFO 无读者）",
                           extra={"session": args.name, "fifo": f["fifo"]})
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
    conn, client, f, pid, _alive, ec = _session_load(args, root, args.name or "main")
    if ec is not None:
        return ec
    try:
        sftp = open_sftp(client)
        try:
            try:
                offset = _session_sftp_read(sftp, f["log"], 0, 0)[1]
            except Exception:
                offset = 0
            token = _session_send_payload(client, f, cmd,
                                          plain=not _session_pty_mode(sftp, f))
            if token is None:
                emit_error(args.json, "send_failed",
                           "命令未能写入会话 FIFO（会话可能刚退出或 FIFO 无读者）",
                           extra={"session": args.name, "fifo": f["fifo"]})
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
    conn, client, f, pid, _alive, ec = _session_load(args, root, args.name or "main")
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
    """中断会话里正在执行的命令（对它的进程组发 SIGINT；--force 用 SIGKILL）。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    name = args.name or "main"
    conn, client, f, pid, _alive, ec = _session_load(args, root, name)
    if ec is not None:
        return ec
    try:
        sig = "KILL" if args.force else "INT"
        rc, out, err = _session_run(client, _session_ctrl_c_cmd(f, sig), timeout=20)
        marks = dict(_SESSION_MARK_RE.findall(out))
        groups = [g for g in (marks.get("GROUPS") or "").split() if g]
        children = [p for p in (marks.get("CHILDREN") or "").split() if p]
        escalated = [p for p in (marks.get("ESCALATED") or "").split() if p]
        n = int(marks.get("SIGNALED") or 0)
        result = {
            "ok": True, "action": "session", "version": VERSION, "session": name,
            "signal": sig, "signaled_groups": groups, "signaled_children": children,
            "signaled_count": n, "escalated_to_term": escalated,
            "sid": int(marks["SID"]) if marks.get("SID", "").isdigit() else marks.get("SID"),
            "pid": int(pid) if str(pid).isdigit() else pid,
            "host": conn["host"], "user": conn["user"], "port": conn["port"],
            "warnings": [], "duration_ms": int((time.time() - start) * 1000),
        }
        if escalated:
            result["warnings"].append(
                "SIGINT 后仍有存活进程（%s），已升级为 SIGTERM——本会话树由 setsid+nohup 起，"
                "SIGINT 处置可能被继承为忽略；要更狠用 --force（SIGKILL）" % " ".join(escalated))
        if n == 0:
            result["hint"] = ("会话内当前没有前台命令在跑（可能空闲）——用 session read 确认输出；"
                              "命令若刚被中断，其退出码（130/143）会在哨兵里")
            result["next_action"] = "先 session read --offset <上次 next_offset> 看当前状态"
        else:
            result["next_action"] = ("已向 %d 个进程组发 %s；用 session read 确认命令已中止、"
                                     "会话仍存活（状态保留，可直接发下一条）" % (n, sig))
        _emit_result(args, result, header="[SESSION %s] %s -> %d groups"
                     % (name, sig, n))
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
    conn, client, f, pid, _alive, ec = _session_load(args, root, name)
    if ec is not None:
        return ec
    try:
        rc, _out, _err = _session_run(client, _session_keys_cmd(f), stdin_data=payload, timeout=15)
        if rc != 0:
            emit_error(args.json, "keys_failed",
                       "按键/文本未能写入会话（会话可能刚退出或 FIFO 无读者）",
                       extra={"session": name, "fifo": f["fifo"]})
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
        sftp = open_sftp(client)
        try:
            try:
                entries = sftp.listdir_attr(root)
            except IOError:
                entries = []
            sessions, pids = [], []
            for e in sorted(entries, key=lambda x: x.filename):
                if not stat.S_ISDIR(e.st_mode or 0):
                    continue
                name = e.filename
                if not _SESSION_NAME_RE.match(name):
                    continue
                f = _session_files(root, name)
                s = _session_info(client, f, sftp=sftp)
                s.update({"session": name, "log": f["log"], "dir": f["dir"]})
                sessions.append(s)
                if isinstance(s.get("pid"), int):
                    pids.append(s["pid"])
            alive = _alive_map(client, pids) if pids else {}
            for s in sessions:
                s["alive"] = alive.get(s["pid"]) if isinstance(s["pid"], int) else None
                s["status"] = ("running" if s["alive"] else "dead") if s["pid"] else "unknown"
                # 空闲回收进度（AI 据此判断"还能放多久"）：ttl - idle 就是剩余保活时间
                if s.get("ttl_seconds") and s.get("idle_seconds") is not None:
                    s["expires_in_seconds"] = max(0, s["ttl_seconds"] - s["idle_seconds"])
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
    """结束会话：按进程树闭包（sess.pid + bash.pid + script，各自证）TERM→校验→KILL，默认连目录一起删。"""
    start = time.time()
    root = getattr(args, "session_dir", None) or DEFAULT_SESSION_DIR
    if not args.all and not args.name:
        emit_error(args.json, "bad_args", "需 --name <会话名> 或 --all（结束该主机全部会话）")
        return 2
    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        if args.all:
            names = []
            sftp = open_sftp(client)
            try:
                try:
                    for e in sftp.listdir_attr(root):
                        if not _SESSION_NAME_RE.match(e.filename):
                            continue
                        # 只认"看起来真是会话"的目录（有 sess.pid 或 FIFO），
                        # 避免把 --session-dir 指向共享目录时误删别人的东西
                        f = _session_files(root, e.filename)
                        if _session_remote_exists(sftp, f["pid"]) or \
                                _session_remote_exists(sftp, f["fifo"]):
                            names.append(e.filename)
                except IOError:
                    names = []
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        else:
            if not _session_check_name(args.json, args.name):
                return 2
            names = [args.name]
        killed, results = 0, []
        for name in names:
            f = _session_files(root, name)
            rc, out, kerr = _session_run(client, _session_kill_cmd(f, args.keep_dir), timeout=40)
            marks = dict(_SESSION_MARK_RE.findall(out))
            left = int(marks.get("LEFT") or 0)
            swept = int(marks.get("SWEPT") or 0)
            roots = int(marks.get("ROOTS") or 0)
            had_dir = marks.get("HAD") == "1"
            killed += 1
            entry = {"session": name, "swept": swept, "remaining": left, "roots": roots,
                     "dir": f["dir"],
                     "cleaned": (marks.get("CLEANED") == "1") and not args.keep_dir,
                     # verified=真的确认过"会话进程已不在"：找到过自证的根、且没有幸存者。
                     # swept=0 不再等于"清干净"（旧版会因此误报，见 _session_kill_cmd docstring）
                     "verified": bool(roots) and left == 0}
            if not roots and had_dir:
                entry["note"] = ("没找到可自证的会话进程（sess.pid/bash.pid 缺失或进程已消失）："
                                 "可能有 reparent 后的孤儿，请自查 "
                                 "ps -eo pid,ppid,tty,args | grep -E 'script -qfc|pyaissh-sessions'")
            results.append(entry)
            if rc != 0 and not marks:
                results[-1]["note"] = (kerr or out or "")[-200:]
        result = {"ok": True, "action": "session", "version": VERSION,
                  "sessions": results, "count": len(results),
                  "remaining_total": sum(r["remaining"] for r in results),
                  "verified_total": sum(1 for r in results if r.get("verified")),
                  "host": conn["host"], "user": conn["user"], "port": conn["port"],
                  "warnings": [], "duration_ms": int((time.time() - start) * 1000)}
        if result["remaining_total"]:
            result["warnings"].append(
                "仍有 %d 个进程属于被结束会话的进程树（可能正在退出或有 SIGKILL 也杀不掉的状态）"
                % result["remaining_total"])
        unverified = [r["session"] for r in results if r.get("note") and not r.get("verified")]
        if unverified:
            result["warnings"].append(
                "会话 %s 的清理**未获确认**（没找到活着的会话进程，可能仍有孤儿）："
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
