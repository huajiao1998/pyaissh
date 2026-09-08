"""通用工具与字符串/缓冲处理（域 05）。

- 截断：_truncate_output（保留头尾+省略标记）/ _utf8_boundary_cut / _truncate_cmd
- _shell_escape_hint（转义建议，--sudo 场景抑制）、warn_sensitive_cmd（调用 02 正则）
- _clean_pty_text / _strip_ansi（ANSI 清理）、_sanitize_log_text
- spill 落盘 _spill_writers/_close_spill（截断时完整输出留文件）
被 exec 会话（08）与输出层（04）调用。
"""

class SshError(Exception):
    """pyaissh 内部错误（携带 error_type 用于结构化输出）"""
    def __init__(self, message, error_type="error"):
        super().__init__(message)
        self.error_type = error_type


class ExecIdleTimeout(TimeoutError):
    """exec 静默超时（连续无输出超过 --idle-timeout）。
    独立子类让错误路径能区分两种超时：error_type=exec_idle_timeout，退出码 124。"""


class ExecTotalTimeout(TimeoutError):
    """exec 总时长超限（超过 --max-time，命令持续输出但不结束）。
    error_type=exec_total_timeout，退出码 124。"""


def _conn_extra(conn):
    """连接期错误附带主机定位信息（多主机/跳板场景 AI 需要知道是哪台机器）"""
    if conn:
        return {"host": conn["host"], "user": conn["user"], "port": conn["port"]}
    return {}


def format_size(n):
    """字节数 -> 人类可读"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def _utf8_boundary_cut(data, n, from_start=True):
    """在字节 n 处截断但回退到合法 UTF-8 字符边界（避免截出半个字符产生 �）。

    from_start=True：取 data[:n] 并向前回退；False：取 data[len-n:] 并向后回退。
    """
    if n <= 0:
        return b""
    # 只回退有限次（UTF-8 单字符最多 4 字节）：内容含非法字节（如二进制
    # 0xff）时任何前缀都 decode 失败，若递减到空会把整段输出清掉。
    # 回退失败就原样返回，由调用方 decode(errors="replace") 兜底。
    if from_start:
        cut = data[:n]
        for _ in range(4):
            try:
                cut.decode("utf-8")
                return cut
            except UnicodeDecodeError:
                cut = cut[:-1]
        return data[:n]
    cut = data[-n:]
    for _ in range(4):
        try:
            cut.decode("utf-8")
            return cut
        except UnicodeDecodeError:
            cut = cut[1:]
    return data[-n:]


def _truncate_output(data, limit, stream_name):
    """超出 limit 字节时保留头尾各一半（日志类输出的关键信息常在尾部）。

    返回 (截断后的 bytes, 是否截断, 省略的字节数)；省略处插入可识别的标记行，
    让 AI 知道输出不完整、被省略了多少、如何取全文。截断点会回退到 UTF-8
    字符边界，避免切出半个多字节字符。
    """
    if not limit or limit <= 0 or len(data) <= limit:
        return data, False, 0
    # marker 前导/后随换行做成可选的：head 已以换行结尾时不再补前导换行、
    # tail 已以换行开头时不再补后随换行，避免 4096 最小档出现空行（AI 按行
    # 号解析会跳号）。构造时用 \x01/\x02 占位，拼装时按 head/tail 实际形态替换。
    marker_tpl = ("\x01...[pyaissh: %s 已截断，省略 %d 字节（原文 %d 字节；"
                  "调大 --max-output 可取全文，尾部信息重要时用 tail]...\x02")
    # 先用真实数字的位数上限（各按 10 位估）预留 marker 空间再算 half：
    # 若按单字符占位估，真实数字（如 15831 比 N 多 4 字节）会让拼装结果
    # 必然超限、走纯前缀回退，头尾保留在任何实际场景都不生效
    est_marker = (marker_tpl % (stream_name, 10 ** 9, 10 ** 9)).encode("utf-8")
    half = (limit - len(est_marker)) // 2
    if half <= 0:
        # marker 放不下：保留尾部（关键信息在尾，且 marker 承诺"尾部可用 tail"）
        cut = _utf8_boundary_cut(data, limit, from_start=False)
        return cut, True, len(data) - len(cut)
    for _ in range(3):
        head = _utf8_boundary_cut(data, half)
        tail = _utf8_boundary_cut(data, half, from_start=False)
        # 行对齐：头尾边界各自退到最近的换行（限窗 4KB；二进制流无换行则保持
        # 字节边界）——逐行解析的消费者不会拿到首尾各一行"缺半"的残行。
        # 只向后退不会超出 limit；省略量在行对齐后重算，记账与实际字节严格一致
        nl = head.rfind(b"\n", max(0, len(head) - BUF_ALIGN_WINDOW))
        if nl != -1:
            head = head[:nl + 1]
        t_start = len(data) - len(tail)
        nl2 = data.rfind(b"\n", max(0, t_start - BUF_ALIGN_WINDOW), t_start)
        if nl2 != -1:
            tail = data[nl2 + 1:]
        omitted = len(data) - len(head) - len(tail)
        # marker 前导换行：head 以 \n 结尾则省略（避免空行）；否则补 \n 独立成行。
        # 后随同理：tail 以 \n 开头则省略。占位 \x01/\x02 换成实际换行或空。
        lead_nl = b"" if head.endswith(b"\n") else b"\n"
        trail_nl = b"" if tail.startswith(b"\n") else b"\n"
        marker = (marker_tpl % (stream_name, omitted, len(data))).encode("utf-8")
        marker = marker.replace(b"\x01", lead_nl).replace(b"\x02", trail_nl)
        overflow = len(head) + len(marker) + len(tail) - limit
        if overflow <= 0 or half <= 1:
            break
        # 真实数字比预估长导致超限：收缩 half 重新拼（一两轮内收敛）
        half = max(half - (overflow + 1) // 2, 1)
    result = head + marker + tail
    if len(result) > limit:
        # 头尾+marker 拼不下（极限小 limit 或数据含 seam 锚定失效）：
        # 回退为【保留尾部】而不是纯前缀——日志/命令输出的关键信息在尾部，
        # marker 语义也承诺"尾部可用 tail"（实测 --max-output<4096 时纯前缀
        # 会把真实尾部整个切掉，AI 拿到 aaaa... 而丢 ZZZZ 尾部）
        cut = _utf8_boundary_cut(data, limit, from_start=False)
        return cut, True, len(data) - len(cut)
    return result, True, omitted


# Windows 保留设备名（下载路径防护）：任何盘符下这些名字都无法作为文件创建
# （含扩展名变体 CON.txt 也算），递归下载遇到会整体中止
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _truncate_cmd(cmd):
    """cmd 回显截断：按【字节】超过 CMD_ECHO_LIMIT 保留头尾 + 中间省略标记。

    返回 (显示文本, truncated 标志, 原始字节数)。用字节而非字符计数/截断：
    多字节内容（中文等）下字符数会低估真实大小 2-3 倍（工具记账风格是
    字节级精确，同 stdout_omitted_bytes）。截断边界用 _utf8_boundary_cut
    对齐合法字符，不切半个字符。--cmd 命令行参数通常远小于上限，只有
    --cmd-file 读入的大脚本会触发。凭据检测（warn_sensitive_cmd）用完整
    cmd，不受本函数影响；截断只作用于结果 JSON 的回显字段。"""
    enc = cmd.encode("utf-8", errors="replace")
    n = len(enc)
    if n <= CMD_ECHO_LIMIT:
        return cmd, False, n
    half = CMD_ECHO_LIMIT // 2
    head = _utf8_boundary_cut(enc, half).decode("utf-8", errors="replace")
    tail = _utf8_boundary_cut(enc, half, from_start=False).decode("utf-8", errors="replace")
    body = ("\n...[pyaissh: cmd 回显已截断，共 %d 字节"
            "（完整命令见原始调用，--cmd-file 时为本地文件可重读）]...\n" % n)
    return head + body + tail, True, n


def _shell_escape_hint(cmd):
    """--cmd 来源且含 shell 特殊字符的命令失败时，提示改用 --cmd-file -。

    --cmd-file 来源（args.cmd 为 None）不加；纯文本/简单命令不加。
    启发式字符集：$() 反引号、换行、分号/管道/重定向/尖括号（SKILL.md 第 8 条
    速查的"含特殊字符走 --cmd-file -"场景，dogfood 实测的转义坑）。"""
    if not cmd:
        return ""
    if re.search(r"[\$`\n;|&><]", cmd) or "(" in cmd or ")" in cmd:
        return ("（若为 shell 转义问题：命令含特殊字符/换行，建议改用 "
                "--cmd-file - 从 stdin 读脚本，绕过所有转义）")
    return ""


def _sanitize_log_text(s):
    """去掉终端转义序列，防止污染日志/欺骗 AI 解析（\x1b 开头序列统一替换）。"""
    if not s:
        return s
    return _ANSI_RE.sub("<ESC>", s)


def _strip_ansi(s):
    """PTY 模式下剥离输出中的 ANSI 转义序列（颜色/光标控制），供 AI 干净解析。"""
    if not s:
        return s
    return _ANSI_RE.sub("", s)


def _clean_pty_text(s, args):
    """PTY 输出清洗：\r\n/\r -> \n（终端行转换），可选剥离 ANSI。"""
    if not args.pty:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if args.pty_strip_ansi:
        s = _strip_ansi(s)
    return s


def warn_sensitive_cmd(cmd, enabled=True):
    """命令里出现疑似凭据时打 WARN 并返回警告文本（供结果 warnings 字段收集）。

    enabled=False 关闭启发式（--no-credential-warn）：误报时使用；注意关闭后
    命令里的真实凭据不再被提示，日志脱敏责任回到调用方（结果 JSON 的 cmd 字段
    仍会原样回显命令）。

    v2.1 豁免：命令含 `$(cat ...)` / `$(<file)` 这类"从文件读值"时整条不报——
    值来自文件、不落在命令字符串里，日志无明文可泄，WARN 只剩噪音（实测误报：
    DB_PASS=$(cat /srv/x)、export PASS=$(cat /tmp/p)、mysql -p $(cat f)）。
    v2.1.4 段级豁免：按 shell 分隔符拆段，echo/printf 打印段（字符串无执行语义）
    不贡献命中——"echo 'PASSWORD='" 实测误报；&&/| 后的真命令段独立保留
    （"echo x && mysql -u r -psecret" 的 mysql 段照报）。
    """
    if not (enabled and cmd):
        return None
    if _READ_FROM_FILE_RE.search(cmd):
        return None
    for seg in _CMD_SEG_RE.split(cmd):
        seg = seg.strip()
        if not seg or not _SENSITIVE_CMD_RE.search(seg):
            continue
        first = _first_cmd_word(seg)
        if first in _PRINT_ONLY_TOOLS:
            continue
        msg = ("命令中疑似包含密码/凭据（日志会原样打印命令），"
               "敏感场景建议改用密钥或环境变量注入")
        log("[WARN] " + msg)
        return msg
    return None


# v2.1.4：shell 段分隔（粗分：括号内嵌套 $() 罕见凭据形态，不细拆）
_CMD_SEG_RE = re.compile(r";|&&|\|\||\||\n")


def _first_cmd_word(seg):
    """段首命令词：剥常见前缀(sudo/env/nohup/command/time)与开头引号后取首词。"""
    s = seg.lstrip()
    s = re.sub(r"^(?:sudo|env|nohup|command|time|exec)\s+", "", s)
    if not s:
        return ""
    s = s.lstrip("'\"!~")
    parts = s.split(None, 1)
    return parts[0].strip("'\"") if parts else ""


# 纯打印工具段（无执行语义——echo/printf 只把文本打到 stdout，字符串里的
# PASSWORD= 不是赋值、-p 不是选项；实测误报 "echo 'PASSWORD='"）
_PRINT_ONLY_TOOLS = frozenset(("echo", "printf", "print", "logger"))


def _spill_writers(args):
    """为 stdout/stderr 各建一个"完整流落盘"writer（tempfile，删除留待调用方）。

    读线程边收边写：内存层为防爆内存只保留头尾（--max-output），落盘文件才是
    完整输出；调用方在命令结束后决定保留（截断时，路径回传结果 JSON 的
    stdout_spill_file / stderr_spill_file）或删除（未截断，不留垃圾）。
    返回 (out_fh, out_path, err_fh, err_path)；创建失败对应位为 None。
    """
    def one(name):
        try:
            # --spill-dir 同 --local：Git Bash 下 Unix 风格路径（/tmp/x）需 MSYS 转换
            base = _fix_msys_local_path(args.spill_dir) if args.spill_dir else tempfile.gettempdir()
            os.makedirs(base, exist_ok=True)
            tf = tempfile.NamedTemporaryFile(prefix="pyaissh-%s-" % name, suffix=".spill",
                                             dir=base, delete=False)
            return tf, tf.name
        except Exception:
            return None, None
    of, op = one("stdout")
    ef, ep = one("stderr")
    return of, op, ef, ep


def _close_spill(fh, path, keep):
    """关闭 spill 文件；keep=False 时顺带删除（未截断/异常路径不留垃圾）。"""
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    if path and not keep:
        try:
            os.remove(path)
        except OSError:
            pass


