#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pssh - 基于 paramiko 的命令行 SSH 工具（给 AI 用）

工作原理：
    封装 paramiko，提供 exec / upload / download / test / ls 五个子命令。
    默认输出纯 JSON（stdout 整行一个对象，进度日志一律走 stderr），
    AI 直接解析 stdout 即可；--text 切换为「可读 + ---MARKER---」标记模式。
    大输出自动截断（保留头尾），命令有静默/总时长双重超时，SFTP 有 I/O 超时，
    任何路径都不会无限卡住，也不会撑爆调用方上下文。

环境变量（也可写进脚本目录 .env；工作目录 .env 需显式 PSSH_ALLOW_CWD_ENV=1 才加载，
          默认不加载以防供应链注入）：
    PSSH_USER      默认用户名
    PSSH_PORT      默认端口（22）
    PSSH_KEY       私钥路径（env 形式，优先级等同 --key，高于 PSSH_PASSWORD）
    PSSH_PASSWORD  默认密码
    PSSH_JUMP_KEY / PSSH_JUMP_PASSWORD   跳板机私钥 / 密码（密码未配置时回退 PSSH_PASSWORD，v1.4.9）
    PSSH_HOST_<名称>=user@host:port      主机别名，target 写 @名称 即可引用；
        可配 PSSH_HOST_<名称>_PASSWORD / PSSH_HOST_<名称>_KEY 作为该主机专属凭据
    PSSH_ALLOW_CWD_ENV=1                 显式允许加载工作目录 .env（默认不加载，
        防止恶意仓库注入 PSSH_HOST_* 等把 AI 导向攻击者主机；加载时会打 WARN）
    认证优先级：--key / PSSH_KEY > --password / PSSH_PASSWORD > 默认私钥 ~/.ssh/id_ed25519
        （别名配置了专属 KEY/PASSWORD 时，该主机不再取全局 PSSH_KEY/PSSH_PASSWORD）

退出码：
    exec     = 远程命令退出码（超时 124；连接失败 255；远程退出码恰为 255 时本地返回 254 以免混淆）
    其他     成功 0，传输错误 1，参数错误 2，超时 124，中断 130，连接错误 255

安全提示：
    1. --password / --jump-password 会出现在进程参数里（本地 ps 可见），
       敏感场景请优先用密钥认证，或改用环境变量 PSSH_PASSWORD / .env。
    2. exec 的命令原文会打印到 stderr 日志与结果 JSON，含凭据的命令（如 mysql -p'xxx'、
       curl -u user:pass）会留在记录里；建议敏感凭据用远程环境变量注入。

用法示例：
    pssh exec root@1.2.3.4 --cmd 'uname -a'
    pssh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限
    pssh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120       # 静默超时（连续无输出的窗口）
    pssh exec @prod --cmd 'uname -a'                            # 主机别名（.env 配 PSSH_HOST_PROD）
    pssh exec root@1.2.3.4 --pty --cmd 'tty'                    # 分配 PTY（需要 TTY 的非交互命令）
    pssh exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'top -b -n 1'  # PTY + 剥离 ANSI 供 AI 解析
    pssh exec root@1.2.3.4 --cmd-file - <<'EOF'
    ls -la /var/log
    EOF
    pssh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
    pssh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
    pssh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8  # 高丢包链路提速
    pssh test root@1.2.3.4
    pssh ls root@1.2.3.4 --path /etc --long

    # 通过跳板机连接（跳板用密码，目标用密钥）
    pssh exec root@10.0.0.5 --jump root@156.233.234.206:22024 \
        --jump-password 'xxx' --cmd 'hostname'

路径语义：
    远端路径支持 ~ 与 ~/（SFTP 协议本身不展开，pssh 自动转换为绝对路径，
    实际路径回显在结果的 remote/path 字段）；不支持通配符（SFTP 无 glob，
    请先 ls 拿到明确文件名）。传目录时源目录的【内容】放入目标目录下
    （不额外嵌套一层）；单文件传到已存在的目录 = 放入该目录（scp 语义）。
"""

import argparse
import errno
import json
import os
import posixpath
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

# ===== 极早期信号窗口（v1.4.8）=====
# 这些全局标志与信号 handler 必须定义/注册在 paramiko 慢 import（约 0.3s）
# 之前：进程启动后该窗口内收到 SIGTERM/SIGINT 不再走默认动作（rc=143、
# 无 JSON），而是置标志——main() 入口检查到标志后输出结构化 interrupted
# JSON + 130（窗口从 ~400ms 缩到解释器启动的 ~30ms 物理下限）。
# handler 函数体在信号到达时才解析全局名，此时模块已加载完成，安全。
_SIGTERM_RECEIVED = False
_INTERRUPT_SOURCE = "SIGTERM"
_MAIN_START = None
_CURRENT_ACTION = None
_RESPONDER_STARTED = False
# 进程内第一次 main() 是否已进入：极早期信号检查只对首次调用生效
# （import 阶段），进程内复用时后续调用里的标志是上一次中断留下的，
# 必须走正常重置流程，否则被误判为"本次极早期信号"（实测回归）
_FIRST_MAIN_DONE = False


def _sigterm_handler(signum, frame):
    """SIGTERM/SIGINT 只置标志，【绝不 raise】。

    历史教训（v1.3.x 实测）：在 handler 里 raise KeyboardInterrupt，若主线程
    正阻塞在 paramiko 的 packetizer 读（串行 sftp.get/put），KI 会在持有
    非重入锁的状态下打断 C 级代码，展开途中同线程再次抢锁=自我死锁——
    连强关 socket 都救不了（看门狗/close_all 同样陷进 futex）。
    现在的设计：本函数只置 `_SIGTERM_RECEIVED`；由 _signal_responder 线程
    关闭底层 socket，让阻塞读以普通 socket 错误【在 paramiko 自己的异常
    帧里干净展开】（锁被正常释放），各命令的兜底 except 按标志归位
    interrupted/130；纯 Python 等待点（exec 轮询/目录文件循环）主动检查
    标志抛 KI（在我们的帧里 raise 是安全的）。仅 POSIX 有效——Windows 的
    terminate() 是 TerminateProcess 硬杀，不走信号（注册本身无副作用）。"""
    global _SIGTERM_RECEIVED, _INTERRUPT_SOURCE
    _SIGTERM_RECEIVED = True
    _INTERRUPT_SOURCE = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"


try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
except (ValueError, OSError, ImportError):
    pass  # 非主线程 / 平台不支持（Windows 下注册无副作用）

import paramiko  # 慢 import：handler 已注册，此窗口内的信号走 handler 而非默认动作

VERSION = "1.5.2"

# --text 模式分隔标记的随机 nonce：远程输出无法预测它，伪造不出有效标记
# （标记形如 ---STDOUT.1a2b3c---，每个标记携带本次运行的 nonce 后缀）
_TEXT_NONCE = os.urandom(3).hex()

# 工作目录存在 .env 但未加载（供应链防护）：别名查不到时据此在 stdout
# 错误信息里说明真实原因——只看 stdout 的 AI 不至于陷入"明明写了 .env 却
# 一直被提示去写"的死循环（真实原因只在 stderr WARN 里说过）
_CWD_ENV_SKIPPED = False

# 活动连接注册表：SIGTERM/SIGINT 响应线程用它做"最后救援"——KI 在 paramiko
# C 级 I/O 中展开会破坏其内部锁状态，主线程（连带看门狗）可能互等死锁；
# 响应线程在宽限后【只做裸 socket.close()】（不碰任何会拿 paramiko 锁的
# 方法），强制解除所有阻塞读，让中断走正常异常路径
_ACTIVE_TRANSPORTS = []

# _sftp_put_atomic 中断时远端 .part 清理失败的记录（连接已坏清不掉）：
# 合并进 upload 结果/失败的 warnings，AI 才知道远端有残留待清理
_PUT_RESIDUE_WARNINGS = []


# =========================================================================
# 常量区（v1.5.2 集中）：所有"魔数"与正则模式定义在此，逻辑/消息统一引用，
# 调参只改一处；每个常量带"为什么是这个值"的注释（从原位置搬移，不丢语义）。
# =========================================================================

# --- 超时与时长上限（秒） ---
MAX_TIME_CAP = 1200          # --max-time/--idle-timeout 上限；exec 总时长硬顶（构建/编译类长任务）
DEFAULT_MIN_TOTAL = 120      # exec 默认总时长下限：未指定 --max-time 时 max(2×idle, 120)
MAX_PORT = 65535             # 端口范围上限（1-65535）
SFTP_IO_TIMEOUT = 30         # 单次网络读无数据的超时秒数：防 NAT 断链/网络黑洞导致无限悬挂
PARALLEL_MIN_SIZE = 8 * 1024 * 1024   # 大文件分片阈值：低于此大小单连接（建连开销大于收益）
                             # 背景：单条 TCP 流在高丢包/长 RTT 链路（如跨境）吞吐塌陷
                             #（实测单流 ~20KB/s，8 条独立连接 ~104KB/s），达阈值自动并行
PARALLEL_IO_TIMEOUT = 120    # 分片工作线程的看门狗窗口：高丢包链路单流可能长时间停滞，
                             # 用 30s 会误杀仍然存活的慢传输
DRAIN_WINDOW = 10            # exec 收尾排水窗口上限：min(exec_timeout, 10)
STATUS_GRACE = 2             # test 收到 stdout EOF 后等 exit-status 的最长宽限（高延迟链路实测会晚到）
STDERR_EOF_WINDOW = 1        # test stdout EOF 后收 stderr 尾部的窗口（秒）
SILENCE_GRACE = 1            # 静默超时判定的额外宽限（exit-status 包可能还在路上时避免误判挂死）

# --- 轮询 / 缓冲 / IO 块（秒 / 字节） ---
POLL_TICK = 0.05             # exec 主循环 / test 状态等待的轮询间隔
RESPONDER_TICK = 0.05        # 信号救援线程空闲轮询 tick
RESPONDER_GRACE = 0.2        # 救援线程置标志后的宽限（避免误杀刚建立/即将恢复的连接）
RESPONDER_AFTER = 0.5        # 救援线程关闭连接后的再轮询间隔（兜住分片重建）
WATCHDOG_TICK = 5            # SFTP 看门狗检查间隔
RECV_CHUNK = 65536           # 单次 recv 读块（64KB）
PARALLEL_READ_CHUNK = 262144  # 分片下载单次读块（256KB；与 DEFAULT_MAX_OUTPUT 同值不同义，分开命名）
DEFAULT_MAX_OUTPUT = 262144  # exec 单流默认最大保留字节（256KB：内存缓冲与显示截断同源）
BUF_ALIGN_WINDOW = 4096      # 截断行对齐时回退搜索窗口（字节）
MIN_BUF_FLOOR = 4096         # 内存缓冲下限：max(args.max_output, 4096) 保证小档位也有可用缓冲
JOIN_GRACE = 1.5             # 读线程 join 宽限（秒）
RETRY_SLEEP = 0.5            # Windows 句柄未释放等场景的删除重试等待
PUT_RETRY_SLEEP = 0.3        # 远端 .part 清理重试等待
CYGPATH_TIMEOUT = 5          # cygpath 子进程超时（MSYS 路径转换，本地工具不应挂死）

# --- 正则模式（模块级编译一次；片段化让每个分支可独立注释/测试） ---
_RE_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_RE_IPV6_SEG = re.compile(r"[0-9a-fA-F]{1,4}")
_RE_IPV6_ZONE = re.compile(r"[0-9a-zA-Z._+-]+")
_RE_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

# 疑似凭据模式片段（保守匹配，避免误报）。拼接顺序与历史 alternation 完全一致
#（等价变换，L4 矩阵 40 例验证）。匹配形态：
#   - password=xxx / password: xxx / --password xxx / --password=xxx
#   - -p'xxx' / -p"xxx" / -psecret / -p secret（排除纯数字端口：-p 22 / -p'22' / -p123456）
#   - mysql -u root -p xxx / curl -u user:pass
_P_SENS_PASSWORD = r"passw[o0]?rd\s*[=:]\s*\S+|--password(?:\s+|=)\S+"
_P_SENS_USER = r"--user\s+\S+:\S+"          # curl --user admin:pw 长形式
_P_SENS_URL = r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"   # https://user:pass@host/
_P_SENS_ENV = r"\b\w*(?:PASS(?:WORD|WD|CODE)?|PWD)\s*[=:]\s*\S+"   # DB_PASS=x / DB_PASS: x / MYSQL_PWD=
_P_SENS_P_QUOTED = r"-p['\"](?!\d+['\"])[^'\"]+['\"]"   # -p'secret'（排除 -p'22' 纯数字端口/ID）
# -psecret 紧贴形态（-p 后必须非空白，空格形态交给 _P_SENS_P_SPACE）：
# 前缀 lookbehind 排除常见非密码工具（scp/rsync/curl/make/install/find/perl/echo/unzip/gcc/xargs/awk）
_P_SENS_P_ATTACH = (
    r"(?<!scp )(?<!rsync )(?<!curl )(?<!make )(?<!install )"
    r"(?<!find )(?<!perl )(?<!echo )(?<!unzip )(?<!gcc )(?<!xargs )(?<!awk )"
    r"(?<!-)-p(?!['\"]?\d+(?:['\"]|\b))(?!\s)"
    r"(?!rin|rune|thread|pe\b|roxy|ort|ath|ass|lain)\S+"
)
# -p secret（空格分隔）：lookbehind 排除常见非密码工具（cp/mkdir/ls/tar/scp/rsync/curl/
# make/install/unzip/pytest/awk/xargs/wget，覆盖单/双空格）；(?!--)/(?!-) 排除 -p 后跟选项；
# 词表排除选项名、工具参数与协议名；[^\s/]+ 排除路径类参数（rsync -p /x、mkdir -p a/b 的兜底）
_P_SENS_P_SPACE = (
    r"(?<!cp )(?<!cp  )(?<!ls )(?<!ls  )(?<!tar )(?<!tar  )(?<!scp )(?<!scp  )"
    r"(?<!mkdir )(?<!mkdir  )(?<!rsync )(?<!rsync  )(?<!curl )(?<!curl  )"
    r"(?<!make )(?<!make  )(?<!install )(?<!install  )"
    r"(?<!unzip )(?<!unzip  )(?<!pytest )(?<!pytest  )(?<!awk )(?<!awk  )"
    r"(?<!xargs )(?<!xargs  )(?<!wget )(?<!wget  )"
    r"-p\s+(?!\d+\b)(?!--)(?!-)(?!proxy\b|roxy\b|port\b|path\b|pass\b|plain\b|"
    r"log\b|diff\b|show\b|status\b|add\b|commit\b|clone\b|pull\b|push\b|remote\b|"
    r"branch\b|checkout\b|merge\b|tag\b|stash\b|init\b|config\b|fetch\b|rebase\b|"
    r"reset\b|rm\b|mv\b|help\b|version\b|verbose\b|git\b|docker\b|nmap\b|"
    r"tcp\b|udp\b|icmp\b)"
    r"[^\s/]+"
)
_P_SENS_MYSQL = r"mysql\s+-u\s*\S+\s+-p\s*\S*"
_P_SENS_CURL_U = r"curl\s+.*-u\s*\S+:\S+"

_SENSITIVE_CMD_RE = re.compile(
    r"(?i)(%s|%s|%s|%s|%s|%s|%s|%s|%s)" % (
        _P_SENS_PASSWORD, _P_SENS_USER, _P_SENS_URL, _P_SENS_ENV,
        _P_SENS_P_QUOTED, _P_SENS_P_ATTACH, _P_SENS_P_SPACE,
        _P_SENS_MYSQL, _P_SENS_CURL_U,
    ))

_ANSI_RE = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b.")


def _safe_int(value, default, name="端口"):
    """安全把字符串/数字转成 int，失败返回 default 并不抛异常。"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        log("[WARN] %s 值 %r 非数字，用默认 %s" % (name, value, default))
        return default


# =========================================================================
# 配置加载
# =========================================================================

def _parse_env_file(env_path):
    """解析单个 .env 文件并写入 os.environ（不覆盖已存在的环境变量）。

    返回 True 表示解析成功（即使文件为空）。
    """
    try:
        # utf-8-sig：自动剥离 UTF-8 BOM（\ufeff），否则首个变量 key 带 BOM 失效
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 引号包裹的值：找配对引号，引号内 # 不拆（KEY="a # b"）；
                # 引号后只允许 # 注释（KEY="a # b" # c）。未包裹的值：
                # 行内注释仅当 # 前有空格才拆（KEY=a#b 不误拆）
                if value[:1] in ('"', "'"):
                    q = value[0]
                    end_q = value.find(q, 1)
                    if end_q != -1:
                        rest = value[end_q + 1:].strip()
                        if not rest or rest.startswith("#"):
                            value = value[1:end_q].strip()
                elif " #" in value:
                    value = value.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = value
        return True
    except Exception:
        log("[WARN] 读取 .env 失败（编码可能不是 UTF-8）: %s" % env_path)
        return False


def load_env():
    """加载 .env 文件（不覆盖已存在的环境变量）。

    供应链安全设计：**默认只加载脚本目录的 .env**（用户主动放入 pssh
    工具目录、自己可控的文件）。工作目录（cwd）的 .env 默认【不】加载——
    恶意仓库可自带 .env 注入 PSSH_HOST_* / PSSH_PASSWORD 等变量，把 AI
    的 SSH 连接导向攻击者主机（钓鱼 SSH）。仅当显式设置
    PSSH_ALLOW_CWD_ENV=1（环境变量或脚本目录 .env 中）时才加载 cwd .env，
    并打 WARN 提示供应链风险；未开启但存在 cwd .env 时也打 WARN 提醒。
    """
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(script_env):
        _parse_env_file(script_env)

    cwd_env = os.path.join(os.getcwd(), ".env")
    if cwd_env == script_env or not os.path.isfile(cwd_env):
        return
    if os.environ.get("PSSH_ALLOW_CWD_ENV") == "1":
        log("[WARN] 正在从工作目录加载 .env（供应链风险，因 PSSH_ALLOW_CWD_ENV=1 已显式开启）: %s" % cwd_env)
        _parse_env_file(cwd_env)
    else:
        global _CWD_ENV_SKIPPED
        _CWD_ENV_SKIPPED = True
        log("[WARN] 检测到工作目录 .env 但未加载（供应链风险：防止恶意仓库注入 PSSH_HOST_* 等"
            "导向攻击者主机；如确需使用请设 PSSH_ALLOW_CWD_ENV=1）: %s" % cwd_env)


def _fix_msys_remote_path(path):
    """修复被 Git Bash/MSYS 路径转换破坏的远程路径。

    场景：Git Bash 在没有 MSYS_NO_PATHCONV=1 时，会把 Unix 绝对路径
    （如 /tmp/xxx、/root/xxx、/opt/app）自动转成 Windows 路径，
    传给 pssh.py 后导致 SFTP 拿到错误路径。
    此函数检测并逆转常见转换模式。
    """
    if not path or os.name != "nt":
        return path
    # 检测 MSYS 环境
    in_msys = bool(os.environ.get("MSYSTEM"))
    if not in_msys:
        return path
    # 检测是否已设 MSYS_NO_PATHCONV（已禁用转换）
    if os.environ.get("MSYS_NO_PATHCONV") == "1" or os.environ.get("MSYS2_ARG_CONV_EXCL") == "*":
        return path

    # 模式 1：TEMP 目录转换 /tmp/xxx -> C:/Users/<user>/AppData/Local/Temp/xxx
    temp = os.environ.get("TEMP", "").replace("\\", "/")
    if temp and path.startswith(temp):
        rest = path[len(temp):]
        recovered = "/tmp" + rest
        log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pssh 命令而非 python pssh.py)" % (path, recovered))
        return recovered

    # 模式 2：MSYS 前缀转换 /root/xxx -> C:/Program Files/Git/root/xxx
    # 尝试通过 cygpath 找到 MSYS 根目录
    try:
        msys_root = subprocess.run(["cygpath", "-w", "/"],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
        if msys_root:
            msys_root = msys_root.replace("\\", "/").rstrip("/")
            if path.startswith(msys_root + "/"):
                rest = path[len(msys_root):]
                recovered = rest  # rest is the original /xxx/yyy
                log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pssh 命令而非 python pssh.py)" % (path, recovered))
                return recovered
    except Exception:
        pass

    return path


def _sftp_home(sftp):
    """取远端用户 home（SFTP 会话起始目录），缓存在会话上避免重复往返。"""
    home = getattr(sftp, "_pssh_home", None)
    if home is None:
        home = sftp.normalize(".")
        sftp._pssh_home = home
    return home


def _normalize_remote_path(sftp, path):
    """SFTP 用前规范化远端路径：去尾斜杠 + ~ 展开。

    - '~' 与 '~/' 展开为用户 home：SFTP 协议本身不展开（exec 的 shell 才展开），
      按 SFTP 字面语义处理会静默创建名为 ~ 的目录、文件落到错误位置还报成功；
      展开后的实际路径回显在结果 JSON 的 remote/path 字段，AI 能看到落点。
    - '~user' 形式无法展开：明确报错，绝不按字面路径处理。
    - 去掉尾部 /：POSIX 下 stat("file/") 返回 ENOTDIR，会被误报成"路径不存在"。
    通配符检测不在这里做：下载/列表在"确实不存在"时才提示（文件名合法含 * ? [），
    上传在入口直接拒绝（新建带 glob 字符的路径几乎必是笔误）。
    """
    p = path
    if not p or not p.strip():
        # 空串若兜成 "/" 会整盘递归（download 方向拉全盘）；空/纯空白直接报错
        raise SshError("远端路径为空（--remote/--path 不能是空字符串）", "bad_args")
    if p != "/":
        p = p.rstrip("/") or "/"
    if p == "~":
        p = _sftp_home(sftp)
    elif p.startswith("~/"):
        p = posixpath.join(_sftp_home(sftp), p[2:])
    elif p.startswith("~"):
        raise SshError("远端路径 %s：SFTP 只支持 ~ 与 ~/（~user 形式无法展开），"
                       "请用绝对路径" % p, "bad_args")
    if p != path:
        log("[PATH] 远端路径规范化: %s -> %s" % (path, p))
    return p


def _remote_glob_error(path):
    """构造"路径不存在且含通配符"的专属错误消息（SFTP 无 glob，按字面量找必然不存在）。"""
    return ("远端路径不存在: %s —— 路径含通配符（SFTP 不做 glob 展开），"
            "请先 pssh ls 列出目录拿到明确文件名，再逐个传输" % path)


def _fix_msys_local_path(path):
    """Git Bash 经 ./pssh 包装器（MSYS_NO_PATHCONV=1）运行时，把 /tmp/... 这类
    Unix 风格【本地】路径转换成真实 Windows 路径。

    背景：包装器禁用了 MSYS 路径转换后，--local /tmp/x 会原样到达 Windows Python，
    被解析成当前盘根 D:\\tmp\\x——shell 视角路径明明存在却报"不存在"，下载方向
    更会写错位置。直接 python pssh.py 运行时 MSYS 已提前转换，路径到这儿已是
    Windows 风格，本函数原样返回。
    """
    if not path or os.name != "nt" or not os.environ.get("MSYSTEM"):
        return path
    if path.startswith("~"):
        return os.path.expanduser(path)
    if not path.startswith("/"):
        return path  # 相对路径 / Windows 路径不受影响
    try:
        out = subprocess.run(["cygpath", "-w", path],
                             capture_output=True, text=True, timeout=CYGPATH_TIMEOUT)
        if out.returncode == 0 and out.stdout.strip():
            converted = out.stdout.strip()
            log("[PATH] 本地路径 MSYS 转换: %s -> %s" % (path, converted))
            return converted
    except Exception:
        pass
    log("[WARN] 本地路径 %s 是 Unix 风格但无法转换（cygpath 不可用）：Git Bash 的 /tmp "
        "不是 Windows 的 /tmp，请改用 Windows 路径或相对路径" % path)
    return path


# Git Bash/MSYS 会把 glob 元字符转成私有区字符（实测 * -> U+F000 区）：直接用
# 原始字符判定会漏报"路径含通配符"的专属提示。此集合覆盖转换后形态。
_MSYS_GLOB_CHARS = set("*?[") | set("﹡？［")  # 全角/私有区常见映射
_MSYS_PRIVATE_GLOB = set(map(chr, range(0xF000, 0xF8FF)))  # PUA 区（MSYS 常用落点）


# =========================================================================
# 输出系统：日志 -> stderr，结果 -> stdout
# =========================================================================

def _setup_console_utf8():
    """Windows 下解决中文乱码。

    背景：Python 在 Windows 上默认按本地代码页（如 GBK/936）编码 stdout/stderr，
    而 Git Bash/mintty、Windows Terminal、新版 conhost 等终端按 UTF-8 解码，
    导致中文日志显示为乱码。本函数做两件事：
      1) stdout 是控制台时，用 ctypes 把控制台输出代码页切到 65001 (UTF-8)，
         进程退出时恢复原代码页（不污染用户后续使用的终端）；
      2) 无条件把 stdout/stderr 重配为 UTF-8 输出——重定向/管道场景同样适用
         （--json 输出给 AI 解析时也必须保证是 UTF-8）。
    """
    if os.name != "nt":
        # Linux/macOS：locale 可能非 UTF-8（如 LANG=C/LC_ALL=C），stdout 编码
        # 会是 ASCII，打印中文（错误消息、--json 结果）直接 UnicodeEncodeError 崩溃。
        # errors="replace"：argv/文件名/env 经 surrogateescape 解码可能带 lone
        # surrogate（非法 UTF-8 字节），strict 编码打印必抛 UnicodeEncodeError——
        # 这是唯一能让"stdout 恒单行 JSON"契约破裂的入口（upload 非 UTF-8 文件名、
        # 异常消息含 surrogate 等实测都会炸）。replace 保证永远可打印（surrogate
        # →U+FFFD，JSON 仍单行合法）。UTF-8 locale 下 errors 默认 strict，也要重配
        # （条件同时看 encoding 与 errors）。stdin 同配：--cmd-file - 读 UTF-8 内容
        # 在 LANG=C 下不会 UnicodeDecodeError。
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            try:
                if stream and stream.reconfigure and (
                        (stream.encoding or "").lower().replace("-", "") != "utf8"
                        or stream.errors != "replace"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return
    old_cp = None
    kernel32 = None
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if sys.stdout and sys.stdout.isatty():
            old_cp = kernel32.GetConsoleOutputCP()
            kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass  # 非控制台环境（管道/无 ctypes 权限等）跳过代码页切换
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if stream and stream.reconfigure and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if old_cp and kernel32:
        try:
            import atexit
            atexit.register(lambda: kernel32.SetConsoleOutputCP(old_cp))
        except Exception:
            pass


# 模块级立即生效（不只在 main()）：import 路径（`python -c "import pssh"`、
# AI 嵌入、测试 harness）下 stdout/stderr 也保证 UTF-8，否则管道捕获时
# 中文日志（WARN 等）按本地代码页（GBK）写出会被 UTF-8 解码成乱码。
# main() 里再调一次是幂等兜底（脚本路径双跑无副作用）。
_setup_console_utf8()


def log(msg):
    """进度日志，打到 stderr（两种模式都打），不污染 stdout"""
    print(msg, file=sys.stderr, flush=True)


def emit(result, header=None, sections=None, use_json=False):
    """统一结果输出到 stdout。

    - use_json=True：整行打印一个 JSON 对象
    - use_json=False：打印 header + 各 ---MARKER--- 区块 + ---END---
    """
    if use_json:
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    if header:
        print(header, flush=True)
    if sections:
        for marker, content in sections:
            print("---%s.%s---" % (marker, _TEXT_NONCE), flush=True)
            if content:
                print(content, flush=True)
    print("---END.%s---" % _TEXT_NONCE, flush=True)


def emit_error(use_json, error_type, message, extra=None):
    """错误输出到 stdout（不返回退出码，由调用方自行 return）。

    打印期间屏蔽 KeyboardInterrupt：中断处理路径里再被信号打断会撕裂
    单行 JSON 输出（部分写入 + 上层再打一行），破坏"单行单对象"契约。
    错误对象恒含 version/action/duration_ms/warnings（与成功结果一致，
    help 契约"结果均含"）：action 取当前子命令（解析前/未知为 None），
    duration_ms 自 main() 启动计时，warnings 为空列表（extra 可覆盖）。
    """
    err = {"ok": False, "error": error_type, "message": message,
           "version": VERSION,
           "action": _CURRENT_ACTION,
           "duration_ms": int((time.time() - _MAIN_START) * 1000) if _MAIN_START else None,
           "warnings": []}
    if extra:
        err.update(extra)
    try:
        if use_json:
            print(json.dumps(err, ensure_ascii=False), flush=True)
        else:
            log("[ERROR] %s: %s" % (error_type, message))
            print("---ERROR.%s---" % _TEXT_NONCE, flush=True)
            print(json.dumps(err, ensure_ascii=False), flush=True)
            print("---END.%s---" % _TEXT_NONCE, flush=True)
    except KeyboardInterrupt:
        # 双重中断（第二击恰好落在构造与打印之间）会零输出：补打一次，
        # 再被打断就放弃（退出码 130 仍能表意）
        try:
            print(json.dumps(err, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            pass


def _interrupt_msg():
    """生成中断消息文案：区分 SIGTERM / SIGINT（Ctrl+C）来源。

    历史版本两信号共用 handler、KI 消息恒为 "SIGTERM"，Ctrl+C 用户会看到
    误导性字样。这里统一取 _INTERRUPT_SOURCE；纯 Ctrl+C（无信号标志，如
    argparse 阶段）回落 "Ctrl+C"。"""
    if _SIGTERM_RECEIVED:
        return "用户中断（%s）" % _INTERRUPT_SOURCE
    return "用户中断（Ctrl+C）"


# =========================================================================
# 辅助函数
# =========================================================================

class SshError(Exception):
    """pssh 内部错误（携带 error_type 用于结构化输出）"""
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
    marker_tpl = ("\x01...[pssh: %s 已截断，省略 %d 字节（原文 %d 字节；"
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
    """
    if enabled and cmd and _SENSITIVE_CMD_RE.search(cmd):
        msg = ("命令中疑似包含密码/凭据（日志会原样打印命令），"
               "敏感场景建议改用密钥或环境变量注入")
        log("[WARN] " + msg)
        return msg
    return None


def _spill_writers(args):
    """为 stdout/stderr 各建一个"完整流落盘"writer（tempfile，删除留待调用方）。

    读线程边收边写：内存层为防爆内存只保留头尾（--max-output），落盘文件才是
    完整输出；调用方在命令结束后决定保留（截断时，路径回传结果 JSON 的
    stdout_spill_file / stderr_spill_file）或删除（未截断，不留垃圾）。
    返回 (out_fh, out_path, err_fh, err_path)；创建失败对应位为 None。
    """
    def one(name):
        try:
            base = args.spill_dir or tempfile.gettempdir()
            os.makedirs(base, exist_ok=True)
            tf = tempfile.NamedTemporaryFile(prefix="pssh-%s-" % name, suffix=".spill",
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


def parse_target(target):
    """解析 [user@]host[:port] -> (user, host, port)，未指定部分返回 None。
    支持 IPv6：user@[2001:db8::1]:22、[2001:db8::1]、裸 2001:db8::1（无端口）。"""
    user = None
    port = None
    if "@" in target:
        user, _, target = target.partition("@")
        user = user or None
    if target.startswith("["):
        # 带括号的 IPv6：[addr] 或 [addr]:port
        end = target.find("]")
        if end == -1:
            raise SshError("目标格式错误（缺少 ]）: %s" % target, "bad_args")
        else:
            host = target[1:end]
            rest = target[end + 1:]
            if rest and not rest.startswith(":"):
                raise SshError("目标格式错误（] 后只能跟 :port）: %s" % rest, "bad_args")
            if rest.startswith(":"):
                try:
                    port = int(rest[1:])
                except ValueError:
                    raise SshError("目标端口非数字: %s" % rest[1:], "bad_args")
                if not 1 <= port <= MAX_PORT:
                    raise SshError("目标端口超出范围 (1-%d): %s" % (MAX_PORT, rest[1:]), "bad_args")
    elif target.count(":") == 1:
        # 普通 host:port
        host, _, port_s = target.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise SshError("目标端口非数字: %s" % port_s, "bad_args")
        if not 1 <= port <= MAX_PORT:
            raise SshError("目标端口超出范围 (1-%d): %s" % (MAX_PORT, port_s), "bad_args")
    elif target.count(":") > 1:
        # 裸 IPv6（多个冒号）：校验每段是合法 hex（1-4 位），
        # 防 host:22:33 这类 host:port:port 拼错被静默当主机名连错机器。
        # 末段允许带 zone id（fe80::1%eth0，链路本地地址）——括号形式
        # [fe80::1%eth0] 本来就放行，裸形式不能不一致地拒绝
        segs = target.split(":")
        for i, seg in enumerate(segs):
            if not seg:
                continue
            s = seg
            if i == len(segs) - 1 and "%" in s:
                # zone id：接口名（eth0、enp0s3、%25 编码等）
                addr_part, _, zone = s.partition("%")
                if not zone or not _RE_IPV6_ZONE.fullmatch(zone):
                    raise SshError("目标格式错误（IPv6 zone id 非法）: %s" % target, "bad_args")
                s = addr_part
            if i == len(segs) - 1 and _RE_IPV4.fullmatch(s):
                # IPv4-mapped 尾段（::ffff:1.2.3.4）：dotted-quad 是合法 IPv6
                # 字面量的一部分，此前被当非法 hex 段拒绝；逐段校验 0-255
                if any(int(o) > 255 for o in s.split(".")):
                    raise SshError("目标格式错误（IPv4 尾段越界）: %s" % target, "bad_args")
                continue
            if s and not _RE_IPV6_SEG.fullmatch(s):
                raise SshError("目标格式错误（多冒号但非合法 IPv6 地址）: %s" % target, "bad_args")
        host = target
    else:
        # 无端口主机名
        host = target
    return user, host, port


def _alias_env(alias, suffix=""):
    """查主机别名环境变量 PSSH_HOST_<别名><suffix>（键名整体大小写不敏感）。

    先按原样/全大写两种键直查，再对整个键做大小写归一扫描：Linux 的
    os.environ 严格区分大小写（.env 写全小写 pssh_host_prod 也要能命中），
    Windows 本身不区分。返回 None 表示未配置。
    """
    for a in dict.fromkeys([alias, alias.upper()]):
        v = os.environ.get("PSSH_HOST_%s%s" % (a, suffix))
        if v:
            return v
    want = ("PSSH_HOST_%s%s" % (alias, suffix)).upper()
    for k, v in os.environ.items():
        if k.upper() == want:
            return v
    return None


def resolve_conn(args):
    """合并 target / env / 默认值，返回连接参数 dict"""
    if not args.target:
        raise SshError("未指定目标主机（target）", "bad_args")
    # 主机别名：target 写 @名称，从 PSSH_HOST_<名称> 展开（.env 可配）
    alias = None
    target = args.target
    if target.startswith("@"):
        alias = target[1:].strip()
        if not alias:
            raise SshError("主机别名格式应为 @名称", "bad_args")
        val = _alias_env(alias)
        if not val:
            msg = "未配置主机别名 @%s：请在 .env 写 PSSH_HOST_%s=user@host:port" % (alias, alias.upper())
            if _CWD_ENV_SKIPPED:
                # 只看 stdout 的 AI 需要"为什么我写了 .env 还是找不到"的答案：
                # 工作目录 .env 默认不加载（供应链防护），真实原因此前只在 stderr
                msg += ("（检测到工作目录 .env 但默认不加载——防恶意仓库注入；如需启用设 "
                        "PSSH_ALLOW_CWD_ENV=1，或把 .env 放到 pssh 脚本目录）")
            raise SshError(msg, "bad_args")
        log("[ALIAS] @%s -> %s" % (alias, val))
        target = val
    t_user, t_host, t_port = parse_target(target)

    user = t_user or args.user or os.environ.get("PSSH_USER")
    host = t_host
    # 显式 -p 优先于 target/别名内嵌端口（与 ssh/scp 惯例一致：命令行显式参数最优先，
    # 用户写 -p 通常就是想纠正 target 里的端口）
    port = args.port if args.port is not None else t_port
    if port is None:
        port = _safe_int(os.environ.get("PSSH_PORT"), 22, "PSSH_PORT")
    if not port or not 1 <= port <= MAX_PORT:
        # 命令行 --port 已由 argparse 校验（1-65535）；这里只管 env 与 target
        # 内嵌端口：0/负值/越界/非数字一律回退默认 22 并打 WARN
        log("[WARN] 端口 %r 超出范围 (1-%d)，回退默认 22" % (port, MAX_PORT))
        port = 22
    # 凭据优先级：显式参数 > 别名专属（PSSH_HOST_<名称>_KEY/_PASSWORD）> 全局 env。
    # 别名配置了任一专属凭据时抑制全局 env：否则"别名只配密码 + 全局 PSSH_KEY"
    # 会优先拿全局 key 去认证，key 不匹配时直接 auth_failed，别名密码永远轮不到；
    # 别名主机的凭据应完全由别名决定（显式命令行参数仍最高优先）
    alias_key = _alias_env(alias, "_KEY") if alias else None
    alias_pw = _alias_env(alias, "_PASSWORD") if alias else None
    if alias and (alias_key or alias_pw):
        key = args.key or alias_key
        password = args.password or alias_pw
    else:
        key = args.key or alias_key or os.environ.get("PSSH_KEY")  # None 表示用默认密钥
        password = args.password or alias_pw or os.environ.get("PSSH_PASSWORD")

    if not user:
        raise SshError("未指定用户名：请在 target 写 user@host 或用 -u / PSSH_USER", "bad_args")
    if not host:
        raise SshError("未指定主机", "bad_args")

    return {
        "host": host, "user": user, "port": port,
        "key": key, "password": password,
        "timeout": args.timeout, "strict": args.strict,
    }


def resolve_jump(args, target_user=None):
    """解析跳板机连接参数，返回 conn dict 或 None（无跳板时）。

    target_user：已解析的目标用户（别名 @name 展开后），跳板缺 user 时回退用它。
    """
    if not args.jump:
        if (args.jump_password or args.jump_key
                or os.environ.get("PSSH_JUMP_PASSWORD") or os.environ.get("PSSH_JUMP_KEY")):
            log("[WARN] 指定了跳板凭据（--jump-password/--jump-key/PSSH_JUMP_*）但未提供 --jump，已忽略")
        return None
    j_user = None
    j_alias = None
    jump_target = args.jump
    if jump_target.startswith("@"):
        # 跳板机也支持 @别名（与 target 同一套 PSSH_HOST_* 配置）。
        # 不展开的话 "@bastion" 会被当字面主机名去连，报 DNS 失败极具误导性
        j_alias = jump_target[1:].strip()
        if not j_alias:
            raise SshError("跳板机别名格式应为 @名称", "bad_args")
        val = _alias_env(j_alias)
        if not val:
            raise SshError("未配置跳板机别名 @%s：请在 .env 写 PSSH_HOST_%s=user@host:port"
                           % (j_alias, j_alias.upper()), "bad_args")
        log("[ALIAS] 跳板 @%s -> %s" % (j_alias, val))
        jump_target = val
    j_user, j_host, j_port = parse_target(jump_target)
    # 跳板机用户：优先 --jump 字串里的，其次复用目标用户
    # （target 可能是别名 @name，原始字符串解析不出 user，用已解析的 target_user 回退）
    if not j_user:
        j_user = target_user or args.user or os.environ.get("PSSH_USER")
    if not j_user:
        raise SshError("跳板机未指定用户名：请在 --jump 写 user@host", "bad_args")
    if not j_host:
        raise SshError("跳板机未指定主机", "bad_args")
    j_port = j_port or 22  # parse_target 已保证 1-65535（越界直接 bad_args），这里只补未指定端口
    # 跳板凭据优先级：显式参数 > 别名专属 > PSSH_JUMP_*（别名复用 PSSH_HOST_* 的
    # 专属凭据键：同一台机器当 target 和当跳板通常用同一套凭据）。
    # 与 resolve_conn 同规则：别名配了专属凭据时抑制全局 env
    alias_key = _alias_env(j_alias, "_KEY") if j_alias else None
    alias_pw = _alias_env(j_alias, "_PASSWORD") if j_alias else None
    if j_alias and (alias_key or alias_pw):
        key = args.jump_key or alias_key
        password = args.jump_password or alias_pw
    else:
        key = args.jump_key or alias_key or os.environ.get("PSSH_JUMP_KEY")  # None = 默认密钥
        # 密码回落链（v1.4.9）：--jump-password > 别名专属 > PSSH_JUMP_PASSWORD > PSSH_PASSWORD。
        # 跳板与目标共用一套密码是常见场景（同主多机），且跳板【用户名】本就回落目标用户
        # （上方 target_user 回退链）——唯独凭据不回落是设计不一致，实测会多花一次往返才从
        # 错误提示里拿到"请用 --jump-password/PSSH_JUMP_PASSWORD"。只回落密码、不回落密钥：
        # 错误的 PSSH_KEY 会短路原本可用的默认密钥路径（真回归），而密码错误与缺密码的
        # 失败形态等价、无回归。回退发生时打 stderr WARN 保持行为可见。
        password = args.jump_password or alias_pw or os.environ.get("PSSH_JUMP_PASSWORD")
        if not password and os.environ.get("PSSH_PASSWORD"):
            password = os.environ["PSSH_PASSWORD"]
            log("[JUMP] 跳板未指定专属凭据，密码回退使用 PSSH_PASSWORD"
                "（需要独立跳板密码时请设 PSSH_JUMP_PASSWORD 或 --jump-password）")
    return {
        "host": j_host, "user": j_user,
        "port": j_port,
        "key": key,
        "password": password,
        "timeout": args.timeout, "strict": args.strict,
    }


def connect(conn, jump_conn=None):
    """建立 SSH 连接，返回 paramiko.SSHClient。
    若指定 jump_conn，先连跳板机，再通过 direct-tcpip 隧道连目标。
    跳板客户端挂在 client._jump_client 上，由 close_all() 一并清理。
    任何异常（含认证失败/超时）都会先关闭跳板连接再 re-raise，避免泄漏。
    """
    if not jump_conn:
        return _do_connect(conn, None)

    # 有跳板：先连跳板，再用隧道连目标；任何失败都先关跳板再 re-raise
    try:
        jump_client = _do_connect(jump_conn, None, is_jump=True)
    except SshError as e:
        # 加 [跳板机] 前缀：AI 需要区分是跳板机还是目标机的凭据问题
        raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e), e.error_type)
    try:
        return _do_connect(conn, jump_client)
    except BaseException:
        try:
            jump_client.close()
        except Exception:
            pass
        raise


def _host_key_known(client, host, port):
    """判断已知主机：paramiko 5.0 的 host key 条目按 "[host]:port" 键存储
    （5.0 起按 host:port 区分，AutoAddPolicy 添加时用带端口格式），但
    known_hosts 文件里的旧条目可能是裸 hostname——两种格式都查。"""
    hk = client.get_host_keys()
    try:
        if hk.lookup("[%s]:%d" % (host, port)) is not None:
            return True
    except Exception:
        pass
    try:
        return hk.lookup(host) is not None
    except Exception:
        return False


class _AtomicAutoAddPolicy(paramiko.AutoAddPolicy):
    """AutoAddPolicy + 原子写盘（v1.4.8）。

    paramiko 5.0 的 AutoAddPolicy 在 known_hosts 文件存在时会把新 host key
    写回盘，但 save_host_keys 是直接 open(filename, "w") 覆写：
      1) 多进程并发首次连接同一新主机 → read-merge-write 竞态，互相覆盖
         丢记录；
      2) 写盘中途进程崩溃 → known_hosts 文件本身被截断/半写损坏。
    本策略保持"隐式接受"语义，但写盘改为：
      - Linux：fcntl.flock 对 <known_hosts>.lock 加互斥锁（锁文件固定路径、
        永不被 replace 换 inode），锁内重新加载磁盘最新内容、合并内存键、
        写临时文件、os.replace 原子替换——并发进程串行化合并，既不丢记录
        也不损坏文件；
      - Windows：无 fcntl，仅原子替换（文件不会损坏；极端并发下仍可能
        丢记录，属 paramiko 5.0 语义上限）。
    写盘失败只打 WARN 不阻断连接（与 AutoAddPolicy 一致：内存已接受）。
    """

    def missing_host_key(self, client, hostname, key):
        client._host_keys.add(hostname, key.get_name(), key)
        fn = getattr(client, "_host_keys_filename", None)
        if fn is None:
            return
        try:
            self._atomic_save(client._host_keys, fn)
        except Exception as e:
            log("[WARN] 写 known_hosts 失败（host key 已在内存接受）: %s" % e)

    @staticmethod
    def _atomic_save(host_keys, fn):
        import tempfile
        lock_fd = None
        if os.name == "posix":
            try:
                import fcntl
                lock_fd = open(fn + ".lock", "a+")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except Exception:
                lock_fd = None
        try:
            merged = paramiko.HostKeys()
            try:
                merged.load(fn)  # 锁内重读磁盘最新内容，合并避免覆盖他人更新
            except Exception:
                pass
            for hostname, keys in host_keys.items():
                for keytype, key in keys.items():
                    merged.add(hostname, keytype, key)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(fn) or ".", prefix=".known_hosts.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    for hostname, keys in merged.items():
                        for keytype, key in keys.items():
                            f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))
                os.replace(tmp, fn)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            if lock_fd is not None:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass


def _do_connect(conn, jump_client, is_jump=False):
    """实际建立 SSH 连接。jump_client 非 None 时通过其 direct-tcpip 隧道。

    is_jump=True 表示本次连接的是跳板机本身（认证提示要说对变量名）。
    """
    if _SIGTERM_RECEIVED:
        # 连接尚未开始即拿到信号（transport 未注册、响应线程无从 close）：
        # 在我们自己的帧里抛 KI 是安全的，走正常中断路径 130
        raise KeyboardInterrupt("SIGTERM")
    sock = None
    if jump_client:
        # 通过跳板机开 direct-tcpip 隧道到目标
        try:
            transport = jump_client.get_transport()
            sock = transport.open_channel(
                "direct-tcpip",
                (conn["host"], conn["port"]),
                ("127.0.0.1", 0),
                timeout=conn["timeout"],  # 网络黑洞时避免无限阻塞
            )
        except Exception as e:
            msg = str(e)
            # 给 AI 可执行的排查提示：常见两类失败原因
            if "administratively prohibited" in msg.lower():
                hint = "（跳板机 sshd 禁止转发该目标，常见原因：AllowTcpForwarding=no 或 PermitOpen 限制）"
            elif "refused" in msg.lower() or "connect failed" in msg.lower():
                hint = "（目标端口拒绝连接，可能未开放、NAT 端口不符或目标服务未启动）"
            else:
                hint = ""
            raise SshError("跳板机隧道失败: %s%s" % (msg, hint), "jump_failed")
        if sock is None:
            raise SshError("跳板机无法打开到 %s:%s 的隧道" % (conn["host"], conn["port"]), "jump_failed")
        log("[JUMP] 隧道已建立 -> %s:%s" % (conn["host"], conn["port"]))

    client = paramiko.SSHClient()
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(known_hosts):
        try:
            client.load_host_keys(known_hosts)
        except Exception:
            pass
    # 说明：_AtomicAutoAddPolicy 接受新 host key 并原子写盘（paramiko >= 5.0
    # 在 known_hosts 文件存在时会写回盘；< 5.0 只加内存不写盘）。写盘用
    # flock + 临时文件 + os.replace（v1.4.8）：多进程并发首次连接同一新主机
    # 不再丢记录、写盘中断不损坏文件（原生 save_host_keys 是直接覆写）。
    # 首次连接后 host key 已持久化，后续连接不再提示；敏感环境请用 --strict。

    if conn["strict"]:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        host_key_pre_known = None  # strict 模式不走 TOFU，无需新主机提示
    else:
        # 记录连接前该主机是否已在 known_hosts：连接成功后若为新主机
        # （AutoAddPolicy 隐式接受），打 WARN——AI 据此区分
        # "首次连接的新主机"与"被劫持/重装后的主机"（TOFU 的固有风险，
        # 至少让它可见；敏感环境请用 --strict）
        try:
            host_key_pre_known = _host_key_known(client, conn["host"], conn["port"])
        except Exception:
            host_key_pre_known = None  # 取不到就不提示，不阻断连接
        # 原子写盘版 AutoAddPolicy（v1.4.8）：并发首连不丢记录、写盘不损坏
        # 文件（flock + 临时文件 + os.replace），见 _AtomicAutoAddPolicy 注释
        client.set_missing_host_key_policy(_AtomicAutoAddPolicy())

    key_explicit = conn["key"]  # 显式指定的私钥（None 表示未指定，回退默认）
    password = conn["password"]
    default_key = os.path.expanduser("~/.ssh/id_ed25519")

    kwargs = dict(
        hostname=conn["host"], port=conn["port"],
        username=conn["user"], timeout=conn["timeout"],
        allow_agent=False, look_for_keys=False,  # 默认禁 agent/密钥扫描，保证显式凭据优先级
    )
    if sock:
        kwargs["sock"] = sock
    # 认证优先级：显式 --key > 显式 --password > 默认密钥 > ssh-agent 兜底
    if key_explicit:
        key_path = os.path.expanduser(key_explicit)
        if not os.path.isfile(key_path):
            # 防误把私钥内容当路径传入：超长/含换行/含 BEGIN 时脱敏提示
            if len(key_explicit) > 200 or "\n" in key_explicit or "-----BEGIN" in key_explicit:
                raise SshError("--key 疑似传入了私钥内容而非路径：请传私钥文件路径", "auth_failed")
            raise SshError("私钥文件不存在: %s" % key_explicit, "auth_failed")
        kwargs["key_filename"] = key_path
        auth = "key=%s" % key_explicit
        if password:
            # paramiko 支持 key+password 同传（password 兼作 passphrase），但当前
            # 设计 key 优先且不传 password——静默丢弃显式 --password 会误导排障
            log("[WARN] 同时提供了密码与密钥，本次仅用密钥认证（密码被忽略）")
    elif password:
        kwargs["password"] = password
        auth = "password"
    elif os.path.isfile(default_key):
        kwargs["key_filename"] = default_key
        auth = "key=~/.ssh/id_ed25519 (默认)"
    else:
        # 无显式/默认凭据：回退 ssh-agent（仅 agent 场景，如 ssh-add 过密钥）
        kwargs["allow_agent"] = True
        auth = "ssh-agent"

    log("[SSH] 连接 %s@%s:%s (%s) ..." % (conn["user"], conn["host"], conn["port"], auth))
    t0 = time.time()
    try:
        # allow_agent/look_for_keys=False：禁掉 paramiko 默认的 ssh-agent 和
        # ~/.ssh 全密钥扫描，否则显式 --key/--password 的优先级会被 agent 中
        # 或 ~/.ssh/id_rsa 等默认密钥抢先，连上错误身份（与文档声明矛盾）。
        # 显式/默认的 key_filename 已覆盖密钥认证路径。
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as e:
            if is_jump:
                hint = ("（检查跳板机用户名/密码；密码建议用 PSSH_JUMP_PASSWORD 环境变量"
                        "或 --jump-password，密钥用 --jump-key / PSSH_JUMP_KEY）")
            else:
                hint = ("（检查用户名/密码是否正确；密码建议用 PSSH_PASSWORD "
                        "环境变量，密钥用 --key / PSSH_KEY）")
            raise SshError("认证失败: %s%s" % (e, hint), "auth_failed")
        except paramiko.SSHException as e:
            msg = str(e)
            # 无凭据场景（--key/--password/PSSH_PASSWORD 都没给且无默认密钥/agent）：
            # paramiko 报的原文很含糊，明确告诉 AI 缺什么
            if "no authentication methods available" in msg.lower():
                raise SshError("未提供可用凭据: %s（请用 --password / PSSH_PASSWORD 环境变量，"
                               "或 --key / PSSH_KEY 指定私钥）" % msg, "auth_failed")
            # host key 失败分两类（paramiko 5.0 实测消息）：
            #  1) known_hosts 已有记录但指纹不匹配 -> BadHostKeyException
            #  2) --strict + 新主机不在 known_hosts -> RejectPolicy 抛
            #     SSHException("Server 'x' not found in known_hosts")
            # 之前用 "key" in msg 子串判定会漏判 2（消息不含 key）且误报
            # key-exchange 类错误，改用异常类型 + 消息特征双重判定
            if isinstance(e, paramiko.BadHostKeyException) \
                    or "not found in known_hosts" in msg.lower() \
                    or "host key" in msg.lower():
                raise SshError("host key 校验失败: %s（确认目标无误后清理 "
                               "~/.ssh/known_hosts 再试）" % msg, "host_key_rejected")
            # banner 失败两类形态（paramiko 5.0 实测）：
            #  1) 消息直接含 "banner"（"Error reading SSH protocol banner"）
            #  2) banner 读取失败后认证阶段在非活动 transport 上抛
            #     "No existing session"（原始 banner 异常只打到 stderr）——
            #     之前只匹配 1，导致 TCP 可达但无 SSH 服务的场景（黑洞 IP、
            #     错误端口连到别的服务）报晦涩的 ssh_error "No existing session"
            if "banner" in msg.lower() or "no existing session" in msg.lower():
                # 并发连接过多时 sshd MaxStartups（默认 10:30:100）概率性拒绝
                # 也报 banner 错误：保留原排查提示（仍是 ssh_error）
                if "too many" in msg.lower() or "maxstartups" in msg.lower() \
                        or "administratively" in msg.lower():
                    raise SshError("%s（并发连接过多可能触发服务器 MaxStartups 限制："
                                   "稍后重试、降低并发，或调大目标机 sshd 的 MaxStartups）" % msg,
                                   "ssh_error")
                if "timed out" in msg.lower() or "timeout" in msg.lower() \
                        or time.time() - t0 >= conn["timeout"]:
                    # banner 超时：TCP 已连上但服务器在超时窗口内没发 banner——
                    # 典型是慢速/过载服务器或链路丢包，不是"没有 SSH 服务"。
                    # 归 connection_timeout（排障动作：查网络/调大 --timeout 重试），
                    # 此前归 connection_refused 会误导 AI 去检查端口/sshd。
                    # 注意：paramiko 5.0 的 banner 超时异常消息是 "No existing
                    # session"（不含 timeout 字样），消息匹配判不出，故叠加
                    # 耗时判定（t0 在 connect 前：用满 --timeout 窗口仍未拿到
                    # banner 即超时；快速失败则是"目标不是 SSH 服务"）
                    raise SshError(
                        "SSH banner 超时（TCP 已连接但服务器在 %s 秒内未发送 "
                        "banner，可能过载或网络丢包）: %s（可调大 --timeout 重试）"
                        % (conn["timeout"], msg), "connection_timeout")
                # 其余 banner 失败：TCP 可达但目标不是 SSH 服务/协议不符——
                # 归为 connection_refused 并给出可执行排查方向（而非晦涩消息）
                raise SshError("目标端口没有 SSH 服务（TCP 可达但未收到 SSH banner）: %s"
                               "（检查端口是否写对、目标是否运行 sshd）" % msg,
                               "connection_refused")
            raise SshError(msg, "ssh_error")
        except Exception as e:
            # 网络类错误分类 + 排查提示（AI 依据 error_type 和 hint 决定下一步）
            name = type(e).__name__
            msg = str(e)
            if isinstance(e, socket.gaierror):
                raise SshError("DNS 解析失败: %s（主机名可能拼错）" % msg, "dns_failed")
            if "timeout" in name.lower() or "timed out" in msg.lower():
                raise SshError("连接超时 (%ss): %s（检查主机/端口是否可达、防火墙/NAT 映射）"
                               % (conn["timeout"], msg), "connection_timeout")
            if isinstance(e, ConnectionRefusedError) or "refused" in msg.lower() \
                    or "unable to connect" in msg.lower() \
                    or getattr(e, "errno", None) == errno.ECONNREFUSED:
                raise SshError("连接被拒绝: %s（目标端口没有 SSH 服务在监听，"
                               "检查端口 / NAT 映射是否写对）" % msg, "connection_refused")
            raise SshError("连接失败: %s" % msg, "connection_failed")
    except BaseException:
        # connect 失败时 paramiko 不会自动关掉已建立的 transport/socket，
        # 必须显式 close，否则连接悬挂到进程退出（泄漏）。
        # 用 BaseException：Ctrl+C 中断连接时也要先关掉半开连接
        try:
            client.close()
        except Exception:
            pass
        raise

    dur = int((time.time() - t0) * 1000)
    log("[OK]  已连接 (%dms)" % dur)
    if not conn["strict"] and host_key_pre_known is False:
        # 连接前 known_hosts 无此主机、连接后出现 host key（_host_key_known
        # 兼容 paramiko 5.0 的 "[host]:port" 键与旧版裸 hostname）：
        # 新主机被隐式接受（AutoAddPolicy 可能已写盘，见上方注释）
        try:
            if _host_key_known(client, conn["host"], conn["port"]):
                log("[WARN] 新主机 host key 已隐式接受（AutoAddPolicy）："
                    "%s:%s——首次连接或主机 key 已变更；敏感环境请用 --strict"
                    % (conn["host"], conn["port"]))
        except Exception:
            pass
    client._jump_client = jump_client  # 挂载跳板客户端以便 close_all 清理
    try:
        _ACTIVE_TRANSPORTS.append(client.get_transport())
    except Exception:
        pass
    return client


def close_all(client):
    """关闭 SSH 连接及其跳板机连接（如有）"""
    try:
        _ACTIVE_TRANSPORTS.remove(client.get_transport())
    except Exception:
        pass
    jump = getattr(client, "_jump_client", None)
    try:
        client.close()
    except Exception:
        pass
    if jump:
        try:
            jump.close()
        except Exception:
            pass


# =========================================================================
# SFTP 辅助：远程目录操作
# =========================================================================


def _sftp_touch_activity(sftp):
    """刷新 SFTP 看门狗活动时间：任何 SFTP 操作（含 listdir/stat/mkdir/walk）
    前调用，防止高延迟链路下目录操作被看门狗误杀。"""
    sftp._pssh_last_activity = time.time()


def _make_sftp_touch(sftp):
    """生成 put/get 的进度回调：刷新看门狗的活动时间戳（有数据流动=活着）。

    paramiko 回调签名 func(transferred, total)，用闭包把 sftp 传进去。
    """
    def _cb(transferred, total):
        sftp._pssh_last_activity = time.time()
    return _cb


def _sftp_watchdog(sftp):
    """SFTP 看门狗线程：超过 io_timeout 无数据传输则强制断开。

    背景（实测确认）：paramiko 的 SFTP 读响应走 transport 的 packetizer，
    channel.settimeout / sock.settimeout 对它都无效——服务器静默断链
    （NAT 超时、网络黑洞、服务端 sftp-server 挂起）时 put/get/listdir
    会无限阻塞，违背"任何路径不无限卡住"的承诺。
    看门狗在 open_sftp 时启动（daemon），put/get 通过 callback 刷新
    活动时间；超时后强制 sftp.close()，让阻塞中的操作抛异常返回，
    上层转 upload_failed/download_failed/ls_failed 等错误类型。
    """
    io_timeout = getattr(sftp, "_pssh_io_timeout", SFTP_IO_TIMEOUT)
    try:
        while True:
            time.sleep(WATCHDOG_TICK)
            try:
                # paramiko 5.0 的 SFTPClient：self.sock 是 channel（有 closed），
                # transport 通过 sock.get_transport() 取
                if sftp.sock.closed:
                    return
            except Exception:
                return
            if time.time() - sftp._pssh_last_activity > io_timeout:
                sftp._pssh_watchdog_killed = True
                log("[WARN] SFTP %d 秒无数据传输，强制断开（服务器可能已静默断链）"
                    % io_timeout)
                # 关底层 TCP socket：sftp.close()/channel.close() 都打断不了
                # 阻塞在 packetizer 的读（实测确认），只有 socket.close() 能
                # 让阻塞中的 put/get/listdir 立即抛异常返回
                try:
                    transport = sftp.sock.get_transport()
                    if transport is not None and transport.sock is not None:
                        transport.sock.close()
                except Exception:
                    pass
                try:
                    sftp.close()
                except Exception:
                    pass
                return
    except Exception:
        pass


def open_sftp(client, io_timeout=None):
    """打开带 I/O 超时兜底的 SFTP 会话。

    paramiko 默认不给 SFTP 设超时，连接被 NAT 静默丢弃时 put/get/listdir
    会无限阻塞。这里双重兜底：
      1) channel/sock settimeout（对部分读路径有效）；
      2) 看门狗线程（对 put/get 等阻塞读有效，见 _sftp_watchdog）。
    持续有数据流动的慢传输不受影响（callback 持续刷新活动时间）。
    io_timeout 可对单会话放宽（分片下载工作线程用更长的窗口）。
    """
    io_timeout = io_timeout or SFTP_IO_TIMEOUT
    sftp = client.open_sftp()
    try:
        sftp.get_channel().settimeout(io_timeout)
    except Exception:
        try:
            sftp.sock.settimeout(io_timeout)
        except Exception:
            pass
    sftp._pssh_io_timeout = io_timeout
    sftp._pssh_last_activity = time.time()
    sftp._pssh_watchdog = threading.Thread(target=_sftp_watchdog, args=(sftp,), daemon=True)
    sftp._pssh_watchdog.start()
    return sftp


def _parallel_fetch(conn, args_, remote, local, size, k):
    """多连接分片下载：k 条独立 SSH 连接各下载一段，写入同一本地文件。

    背景（实测）：paramiko 单连接的 SFTP 读是"发一个请求等一个响应"，
    高丢包/长 RTT 链路（如跨境）上单条 TCP 流吞吐塌陷（~20KB/s 且会
    长时间停滞触发看门狗）；独立连接数近似线性提升吞吐（8 连接 ~5.5 倍）。
    全部分片用独立连接（不与主会话共用 transport）：空闲主会话的看门狗
    强断时会连带杀死同 transport 的其他通道（实测踩坑）。
    本地文件生命周期：调用方应传 <目标>.part 路径——全部连接建立成功后
    才创建/清空 .part，任一分片失败抛 SshError（download_failed/download_timeout）
    或建连失败抛连接类 SshError，由调用方负责删除 .part 并原子改名收尾。
    """
    workers = []   # [client, sftp, start, end, got]
    errors = []
    shared_jump = None
    try:
        bounds = [(i * size // k, (i + 1) * size // k if i < k - 1 else size)
                  for i in range(k)]
        # 跳板只建【一条】连接，k 个分片各开一条 direct-tcpip 隧道共享它
        # （否则每分片各建一条跳板 SSH：8 分片 = 16 条连接；共享后 = 9 条，
        #   弱跳板/限连接数环境下差异显著。转发隧道不受 sshd MaxSessions 限制）
        jump_conn = resolve_jump(args_, conn["user"])
        if jump_conn:
            try:
                shared_jump = _do_connect(jump_conn, None, is_jump=True)
            except SshError as e:
                raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e),
                               e.error_type)
            log("[JUMP] 分片共享跳板连接 -> %s@%s:%s"
                % (jump_conn["user"], jump_conn["host"], jump_conn["port"]))
        # 先建全部连接再动本地文件：建连失败（服务器并发会话限制/认证被限流/
        # 跳板资源不足）时，调用方的本地目标文件保持原样不受损
        for i, (start, end) in enumerate(bounds):
            client = None  # 每轮重置：_do_connect 抛出时 except 里不会误关上一轮的 client
            try:
                client = _do_connect(conn, shared_jump)
                sftp = open_sftp(client, io_timeout=PARALLEL_IO_TIMEOUT)
            except BaseException:
                # 半建的 client 不在 workers 里，finally 不会关它：这里先关再抛
                #（_do_connect 内部失败时已自关，重复 close 幂等无害）
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                raise
            workers.append([client, sftp, start, end, 0])
        with open(local, "wb"):
            pass  # 连接全部就绪后才预创建清空 .part，分片线程以 r+b 各自定位写入

        def _run(i):
            client, sftp, start, end, _ = workers[i]
            rf = lf = None
            try:
                rf = sftp.open(remote, "rb")
                lf = open(local, "r+b")  # 各线程独立句柄定位写入
                off = start
                while off < end:
                    if _SIGTERM_RECEIVED:
                        # 信号已到：不再发新读请求，尽快退出让主线程 join 收尾。
                        # 不加这个检查点，慢链路下 worker 会阻塞在 rf.read()
                        # 的 paramiko 内部读里，socket 被 _signal_responder 关闭后
                        # Windows recv 未必立即报错，拖到 120s 看门狗才释放
                        # （实测 --parallel 2/4 中断延迟 90-145s）。
                        raise KeyboardInterrupt("SIGTERM")
                    rf.seek(off)
                    data = rf.read(min(PARALLEL_READ_CHUNK, end - off))
                    if not data:
                        raise IOError("远端在偏移 %d 处提前 EOF（文件传输中被修改？）" % off)
                    lf.seek(off)
                    lf.write(data)
                    off += len(data)
                    workers[i][4] = off - start
                    _sftp_touch_activity(sftp)
            except KeyboardInterrupt:
                # 信号中断归位：不记入 errors（否则主线程会把信号误报成
                # download_failed），由主线程检查 _SIGTERM_RECEIVED 走 130 路径
                pass
            except Exception as e:
                errors.append((i, e))
            finally:
                # 异常路径也要关句柄：Windows 下本进程打开的句柄会让
                # 上层 os.remove(.part) 抛 PermissionError，残留半截文件。
                # close 失败（磁盘满等，缓冲可能未落盘）也计入 errors：
                # 否则按内存计数判成功、rename 上位的可能是坏文件
                for h in (lf, rf):
                    try:
                        if h is not None:
                            h.close()
                    except Exception as ce:
                        errors.append((i, ce))

        log("[PART] %d 连接分片下载 %s（每片约 %s）"
            % (k, format_size(size), format_size(size // k)))
        threads = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(k)]
        for t in threads:
            t.start()
        # join 用短超时轮询 + 信号检查：worker 阻塞在 rf.read() 的 paramiko
        # 内部读时（慢链路 + 信号关闭 socket 后 Windows recv 不立即报错），
        # 无超时 join 会干等到 120s 看门狗。置标志后不等慢 worker，直接进
        # finally 关闭连接收尾（daemon 线程随之消亡），保证信号秒级退出。
        while any(t.is_alive() for t in threads):
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            for t in threads:
                t.join(0.1)
        if errors:
            # 任一分片被看门狗杀/读超时都按超时归类（不能只看 errors[0]：
            # 首个错误可能是普通失败、后面的才是超时）
            timed_out = any(
                getattr(workers[i][1], "_pssh_watchdog_killed", False)
                or isinstance(e, (socket.timeout, TimeoutError))
                for i, e in errors)
            if timed_out:
                raise SshError("并行分片下载超时（%d 连接，单片 %d 秒无数据；"
                               "高丢包链路可调整 --parallel 重试——过高反而可能适得其反"
                               "（8 不行试 4/2），或稍后重试）"
                               % (k, PARALLEL_IO_TIMEOUT), "download_timeout")
            i, e = errors[0]
            raise SshError("并行分片 %d/%d 下载失败: %s" % (i + 1, k, e), "download_failed")
        got = sum(w[4] for w in workers)
        if got != size:
            raise SshError("下载不完整：得到 %d/%d 字节" % (got, size), "download_failed")
        return got
    finally:
        for client, sftp, start, end, _ in workers:
            try:
                sftp.close()
            except Exception:
                pass
            close_all(client)
        # 显式兜底：首个 worker 建连即失败时 workers 为空，shared_jump
        # 无人顺带关闭（close_all 幂等，重复关无害）
        if shared_jump is not None:
            close_all(shared_jump)


def _remote_size_is(sftp, rpath, size):
    """远程文件存在且大小一致（--skip-existing 的判断依据：仅比大小，不比内容/时间）"""
    try:
        _sftp_touch_activity(sftp)
        return sftp.stat(rpath).st_size == size
    except (socket.timeout, TimeoutError):
        raise  # 超时是连接问题：不能当"远端不存在"误判成需要重传（M2）
    except IOError:
        if getattr(sftp, "_pssh_watchdog_killed", False):
            raise  # 看门狗强制断开：同上，交给外层报 timeout
        return False


def sftp_makedirs(sftp, remote_dir):
    """递归创建远程目录（类似 os.makedirs，已存在则跳过）"""
    remote_dir = remote_dir.rstrip("/")
    if remote_dir in ("", ".", "/"):
        return
    try:
        _sftp_touch_activity(sftp)
        st = sftp.stat(remote_dir)
        # 已存在但不是目录：必须报错。静默通过会让目录上传假成功
        # （--no-recursive 变 ok:true 零传输）或后续 put 报出误导性错误
        if not stat.S_ISDIR(st.st_mode):
            raise SshError("远程路径已存在且不是目录: %s（请换目标路径或先处理该文件）"
                           % remote_dir, "bad_args")
        return  # 已存在
    except (socket.timeout, TimeoutError):
        raise
    except SshError:
        raise
    except IOError:
        pass
    parent = posixpath.dirname(remote_dir)
    if parent and parent != remote_dir:
        sftp_makedirs(sftp, parent)
    try:
        _sftp_touch_activity(sftp)
        sftp.mkdir(remote_dir)
    except (socket.timeout, TimeoutError):
        raise
    except SshError:
        raise
    except IOError:
        # 再 stat 一次：并发下已创建可接受；权限不足/父项是文件等真实错误要上报
        try:
            _sftp_touch_activity(sftp)
            sftp.stat(remote_dir)
        except (socket.timeout, TimeoutError):
            raise
        except IOError:
            raise


def sftp_walk(sftp, remote_dir, warnings=None):
    """递归遍历远程目录，yield (relpath, fullpath, attr, is_dir)。

    目录与文件都产出（目录先于其子项）：下载端据此创建本地目录——
    空目录也能重建（只 yield 文件的话空目录会静默消失）。
    warnings 列表非 None 时，把跳过不可读目录的警告收集进去（进结果 JSON）。
    """
    stack = [(remote_dir, "")]
    while stack:
        current, rel = stack.pop()
        try:
            _sftp_touch_activity(sftp)  # 目录操作也刷新看门狗（防慢链路误杀）
            entries = sftp.listdir_attr(current)
        except (socket.timeout, TimeoutError):
            raise  # 连接超时不是"目录不可读"：继续遍历会连环误报（M2）
        except IOError as e:
            if getattr(sftp, "_pssh_watchdog_killed", False):
                raise
            msg = "跳过无法读取的远程目录: %s (%s)" % (current, e)
            log("[WARN] " + _sanitize_log_text(msg))
            if warnings is not None:
                warnings.append(_sanitize_log_text(msg))
            continue
        # 排序保证遍历顺序跨运行稳定：Windows 冲突改名的 .dupN 映射依赖处理
        # 顺序（OpenSSH listdir 顺序不保证），稳定序才能让重复下载幂等
        entries.sort(key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower()))
        for entry in entries:
            # 拒绝危险文件名：SFTP 条目按规范不含 /，含 / 或 .. 说明服务端被入侵
            # 或异常，直接拼接会造成本地路径穿越
            if "/" in entry.filename or entry.filename in (".", ".."):
                log("[WARN] 跳过危险文件名: %r" % _sanitize_log_text(entry.filename))
                continue
            full = posixpath.join(current, entry.filename)
            r = posixpath.join(rel, entry.filename) if rel else entry.filename
            if stat.S_ISDIR(entry.st_mode):
                yield r, full, entry, True
                stack.append((full, r))
            else:
                yield r, full, entry, False


# =========================================================================
# 子命令实现
# =========================================================================

def _part_path(target):
    """进程唯一的临时文件名：<target>.part.<pid>。
    固定名 .part 在两个进程并发写同一目标时被共享：POSIX 下慢方在快方
    os.replace 后仍持 fd 继续写（写的是改名后的 inode），会把快方已报成功
    的文件原地污染成两源的混合垃圾——唯一名让并发退化为"完整的一方胜出"，
    败方干净报错，绝不产出损坏文件。"""
    return "%s.part.%d" % (target, os.getpid())


def _atomic_local_write(write_fn, local):
    """本地原子落盘：write_fn(part) 写进程唯一的 .part，成功后 os.replace。
    失败/中断删除 .part——绝不留下"看似完整实则损坏"的半截文件
    （空洞文件会被 --skip-existing 按大小误判为已传完）。"""
    part = _part_path(local)
    try:
        write_fn(part)
        os.replace(part, local)
    except BaseException:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            # Windows 下句柄未释放等会让删除失败：稍等重试一次，
            # 仍失败至少留 WARN（静默残留违背"不留半截文件"承诺）
            time.sleep(RETRY_SLEEP)
            try:
                os.remove(part)
            except OSError:
                log("[WARN] 清理临时文件失败（可能有进程占用）： %s" % part)
        raise


def _sftp_put_atomic(sftp, local, remote, progress=None):
    """SFTP 上传 + 远端原子改名：先传进程唯一的 remote.part.<pid>，成功后
    posix-rename 覆盖。服务器不支持 posix-rename 扩展时退化为 remove+rename
    （非原子，WARN 一次）；回退前先确认 .part 仍在，防止把别的进程刚改完名的
    成果误删。progress 可选 [0] 列表：put 回调累计已传字节（中断/失败时
    结果 JSON 能报真实进度，而不是恒 0）。"""
    part = _part_path(remote)
    # .part 是否已被创建/写入：put 回调置位。清理失败时据此区分
    # "残留几乎必然存在"（已开始写入）与"可能根本没创建"（put 开头就失败），
    # 配合 stat 确认决定是否告警（详见 except 分支注释）
    part_touched = [False]
    last = [0]  # paramiko put 回调的 transferred 是本次调用内的累计值（增量记账）

    def _cb(transferred, total):
        part_touched[0] = True
        if progress is not None:
            progress[0] += transferred - last[0]
        last[0] = transferred
        sftp._pssh_last_activity = time.time()

    try:
        sftp.put(local, part, callback=_cb)
        try:
            sftp.posix_rename(part, remote)
        except Exception as rename_err:
            # 回退前守卫：part 不在了说明状态已异常（如被外部动过），
            # 此时 remove(remote) 可能删掉别人的成果——抛原始的 rename 错误
            # （不能 bare raise：那会重抛内层 stat 的异常，错误消息指向不准）
            try:
                sftp.stat(part)
            except Exception:
                raise rename_err
            if not getattr(sftp, "_pssh_posix_rename_warned", False):
                sftp._pssh_posix_rename_warned = True
                log("[WARN] 服务器不支持原子改名（posix-rename 扩展），"
                    "本次用删除+改名代替（该服务器上中断可能留下 .part 或旧文件）")
            try:
                sftp.remove(remote)
            except IOError:
                pass  # 目标原本不存在，直接改名即可
            try:
                sftp.rename(part, remote)
            except Exception as fallback_err:
                # 回退也失败：绝不删 .part——它是新数据的唯一副本，删了就是
                # "旧文件已删 + 新数据也丢"的双丢。保留 part 并明确告警，
                # AI 能手动恢复或安全重试（keep_part 标记让外层跳过清理）
                msg = ("远端原子改名失败且回退改名也失败（旧文件已删除，新数据"
                       "保留在临时文件 %s）：%s" % (part, fallback_err))
                log("[WARN] " + msg)
                _PUT_RESIDUE_WARNINGS.append(msg)
                err = SshError("上传失败：远端原子改名失败且回退也失败，"
                               "新数据保留在 %s（旧文件已删除，请手动恢复或重试）: %s"
                               % (part, fallback_err), "upload_failed")
                err.keep_part = True
                raise err
    except BaseException as e:
        if getattr(e, "keep_part", False):
            raise  # 回退双丢防护：.part 是新数据唯一副本，保留不清理（已告警）
        # 连接坏掉时清不掉远端 .part：重试一次，仍失败必须 WARN 并记录进
        # 结果 warnings（静默残留会让远端磁盘按次泄漏且 AI 无从得知）。
        # 判定 .part 是否真的还在：
        #  - put 已开始写入（part_touched）：残留几乎必然存在，无条件告警；
        #  - put 开头就失败（未 touched，如权限不足 open 失败）：stat 确认，
        #    只有明确 SFTP_NO_SUCH_FILE 才断定无残留；
        #  - stat 因连接死/权限也失败：无法确认，按"可能残留"告警（宁多勿漏
        #    ——连接死恰恰是残留概率最高的场景，此前误判成"已不在"静默泄漏）
        removed = False
        for _attempt in range(2):
            try:
                sftp.remove(part)
                removed = True
                break
            except Exception:
                time.sleep(PUT_RETRY_SLEEP)
        if not removed and not part_touched[0]:
            # 未开始写入：用 stat 确认；只有明确"文件不存在"才不告警
            try:
                sftp.stat(part)
            except IOError as stat_err:
                if getattr(stat_err, "errno", None) == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                    removed = True  # 明确不存在，已清理干净
            except Exception:
                pass
        if not removed:
            msg = ("远端临时文件可能残留: %s（连接中断无法清理。清理命令："
                   "rm -f '%s'；批量清理 pssh 中断残留可用 "
                   "find <目标目录> -name '*.part.*' -delete。重试上传前应先清掉，"
                   "否则 .part 会按次累积）" % (part, part))
            log("[WARN] " + msg)
            _PUT_RESIDUE_WARNINGS.append(msg)
        raise


def _transfer_extra(conn, **kw):
    """传输类错误/中断的统一 extra：恒含 host/user/port（与连接期错误一致，
    AI 用 (host,port) 做多主机记账时失败样本不丢键），再叠加传输上下文。"""
    extra = _conn_extra(conn)
    extra.update(kw)
    return extra


def _win_safe_rel_path(rel, used, warnings):
    """Windows 下载的相对路径安全化（目录与文件通用）。

    逐段：拒绝保留设备名/归一化越界（.../.. 尾随点空格）反斜杠；
    清洗非法字符与控制字符（0x00-0x1f、0x7f）；并按大小写不敏感检测
    （NTFS 特性 + 清洗后）的本地名冲突，后者改名 <名>.dupN 保住两份数据。
    返回安全 rel；条目被整体排斥（危险段）时返回 None（调用方跳过，
    警告已记入 warnings）。used 为已占用本地名（小写）集合，含目录条目。
    """
    parts = rel.split("/")
    safe_parts = []
    for p in parts:
        # 防 Windows 路径归一化穿越：... / .. / 尾随点/空格变体
        # 会被 Win32 解析成 ..（如 "...", ".. ", ". "），可写出 local 目录；
        # 'a..' 尾随点会被 Win32 归一化为 'a' 覆盖本地同名文件，同样拒绝；
        # 反斜杠是 Win32 路径分隔符，含 \ 的远端名（Linux 合法）直接拒绝（H1）；
        # 保留设备名（CON/NUL/COM1 等含扩展名变体）无法创建会中止整个下载
        base = p.split(".")[0].upper()
        if (p.rstrip(". ") != p or p.strip() in ("", ".", "..")
                or "\\" in p or base in _WIN_RESERVED_NAMES):
            warnings.append(_sanitize_log_text(
                "跳过危险路径段 %r（Windows 保留设备名或归一化越界）" % p))
            return None
        # 控制字符（0x00-0x1f、0x7f）在 Windows 文件名里非法：
        # open() 会抛 Errno 22 中止整个下载——同样替换为 _
        safe_parts.append(_RE_WIN_ILLEGAL.sub("_", p))
    safe_rel = "/".join(safe_parts)
    if safe_rel != rel:
        warnings.append(_sanitize_log_text(
            "文件名含 Windows 非法字符，已替换: %s -> %s" % (rel, safe_rel)))
    # 大小写不敏感冲突：两个远端名（仅大小写不同，或清洗后同名）落到同一
    # 本地名会静默覆盖（实测 ok=true 丢一份数据）——后者改 <名>.dupN 保两份
    key = safe_rel.lower()
    if key in used:
        base, dot, ext = safe_rel.rpartition(".")
        n = 2
        if dot:
            cand = "%s.dup%d.%s" % (base, n, ext)
            while cand.lower() in used:
                n += 1
                cand = "%s.dup%d.%s" % (base, n, ext)
        else:
            cand = "%s.dup%d" % (safe_rel, n)
            while cand.lower() in used:
                n += 1
                cand = "%s.dup%d" % (safe_rel, n)
        warnings.append(_sanitize_log_text(
            "本地路径冲突（Windows 大小写不敏感或清洗后同名）:%s 改名为 %s 保住两份数据"
            % (safe_rel, cand)))
        safe_rel = cand
    used.add(safe_rel.lower())
    return safe_rel


def cmd_exec(args):
    start = time.time()  # 计时含连接耗时：duration_ms 在跳板/慢网络下偏大

    warnings = []  # 汇总警告（函数体最前初始化：成功/异常路径都能取到）
    # 先校验命令（快速失败，避免无谓连接）
    cmd = args.cmd
    if cmd and args.cmd_file:
        log("[WARN] --cmd 与 --cmd-file 同时指定，--cmd-file 被忽略")
        warnings.append("--cmd 与 --cmd-file 同时指定，--cmd-file 被忽略（本次用 --cmd）")
    if not cmd and args.cmd_file:
        try:
            if args.cmd_file == "-":
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")
                cmd = sys.stdin.read()
                # 读 stdin 期间可能收到信号（handler 只置标志，阻塞的 read 无法
                # 被中断）：读到内容但信号已到 = 用户取消，不应继续执行命令
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")
            else:
                # utf-8-sig：自动剥离 UTF-8 BOM（\ufeff）——记事本/VS Code 等
                # Windows 工具写出的命令文件带 BOM 时，首行命令会被拼进 BOM
                # 字符而报 "command not found"（与 .env 解析同款处理）
                with open(os.path.expanduser(args.cmd_file), encoding="utf-8-sig") as f:
                    cmd = f.read()
        except KeyboardInterrupt:
            raise  # 中断走 main 的 interrupted/130
        except Exception as e:
            emit_error(args.json, "read_cmd_failed", str(e))
            return 2  # 本地参数/文件问题，与 bad_args 同级；1 会与"远程退出码 1"混淆
    if not cmd or not cmd.strip():
        emit_error(args.json, "bad_args", "未指定命令（--cmd 或 --cmd-file）")
        return 2
    if args.max_time is not None and args.max_time < args.exec_timeout:
        emit_error(args.json, "bad_args",
                   "--max-time (%d) 不能小于 --idle-timeout (%d)（总时长上限必须覆盖静默窗口）"
                   % (args.max_time, args.exec_timeout))
        return 2

    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    def _partial_extra():
        """错误时组装已读到的部分输出/警告，供 AI 判断命令卡在哪一步。

        恒带 stdout/stderr/output_incomplete/stdout_bytes/stderr_bytes 键（可能为空串），
        让 AI 能区分"命令没跑起来（零输出）"与"字段缺失"；有警告时带 warnings。
        闭包引用 cmd_exec 的 out_buf/err_buf/warnings（已在函数体最前初始化，
        任何异常路径都能安全取到）。错误路径的部分输出同样做上限截断。
        output_incomplete（而非 output_truncated）：错误中断输出必然不完整，
        与成功路径"超 --max-output 被裁剪"的 output_truncated 语义不同，
        AI 的后继动作（调大 --max-output vs 调大超时重跑）完全相反。
        """
        # 读线程的头尾滚动缓冲只在退出循环时才刷进共享 buf；总超时/中断时
        # 线程可能还在循环里——先置 stop_drain 让其退出并 join，否则组装到
        # 的是空（实测：总超时时 3.8MB 已读输出全部丢失）
        if stop_drain is not None:
            stop_drain.set()
            t_out.join(JOIN_GRACE)
            t_err.join(JOIN_GRACE)
        out_raw = b"".join(out_buf)
        err_raw = b"".join(err_buf)
        out_cut, _, _ = _truncate_output(out_raw, args.max_output, "stdout")
        err_cut, _, _ = _truncate_output(err_raw, args.max_output, "stderr")
        extra = {
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "cmd": cmd,  # 与成功路径对称：错误时也能看到命令原文（含凭据需脱敏）
            "stdout": _clean_pty_text(out_cut.decode("utf-8", errors="replace"), args),
            "stderr": _clean_pty_text(err_cut.decode("utf-8", errors="replace"), args),
            # 与成功路径语义一致：原始字节数（total_counter 在 exec_command
            # 之后定义；exec_command 本身抛错时回退用缓冲长度）
            "stdout_bytes": total_counter[0],
            "stderr_bytes": err_total_counter[0],
            "output_incomplete": True,  # 错误中断，输出必然不完整（区别于超限裁剪）
            "duration_ms": int((time.time() - start) * 1000),
        }
        if warnings:
            extra["warnings"] = list(warnings)
        return extra

    out_buf, err_buf = [], []  # _partial_extra 依赖（try 之前初始化）
    # 读线程哨兵（try 之前预置 None）：_partial_extra 用 is not None 判断
    # 而非 NameError 控制流——except NameError 会掩盖未来真正的拼写错误
    stop_drain = None
    t_out = t_err = None
    # 读线程计数器提前预置（try 之前）：exec_command 本身抛错时
    # _partial_extra 也能安全取到，不依赖 locals() 检查
    total_counter = [0]      # stdout 原始字节数
    err_total_counter = [0]  # stderr 原始字节数
    drop_counter = [0]       # stdout 内存缓冲丢弃字节
    err_drop_counter = [0]   # stderr 内存缓冲丢弃字节
    # spill 完整流落盘句柄（try 之前预置 None）：finally 兜底清理，异常路径不留文件
    spill_out_fh = spill_err_fh = None
    spill_out_path = spill_err_path = None
    _spill_handled = False
    try:
        w = warn_sensitive_cmd(cmd, enabled=not getattr(args, "no_credential_warn", False))
        if w:
            warnings.append(w)
        if args.pty_strip_ansi and not args.pty:
            msg = "--pty-strip-ansi 未生效：需同时指定 --pty（本次未剥离 ANSI）"
            log("[WARN] " + msg)
            warnings.append(msg)
        log("[EXEC] %s" % _sanitize_log_text(cmd if len(cmd) <= 200 else cmd[:200] + "..."))
        stdin, stdout, stderr = client.exec_command(cmd, timeout=args.exec_timeout, get_pty=args.pty)
        # 立即关闭 stdin：paramiko 默认不关，远程命令若读 stdin（如 cat）会一直
        # 等输入直到静默超时误判挂死。关闭后远程立即收到 EOF。
        # （基础版 --pty 仅面向非交互命令，不涉及 sudo 密码等 stdin 交互）
        try:
            stdin.close()
        except Exception:
            pass
        # 并发读 stdout/stderr，避免大输出填满管道窗口导致死锁
        # （若主线程阻塞等远程结束而远程在写 stderr 已满，就会卡死）。
        # 静默超时由读线程掌控：任一流有输出即重置计时，超过 exec_timeout 无输出则退出。
        chan = stdout.channel
        chan.settimeout(1.0)  # 读 tick：无数据时每 1s 醒来检查一次静默计时
        silence_deadline = [time.time() + args.exec_timeout]
        stop_drain = threading.Event()  # 收尾截断信号：设置后读线程尽快退出

        # 有界缓冲：读线程只保留头尾各 max_output//2 字节（与显示截断一致），
        # 中间溢出丢弃并累计 dropped——防止大输出（cat 大文件/恶意流）无限吃内存，
        # --max-output 不只是"最后截显示"，而是真正限制内存占用。
        # （计数器已在 try 之前预置，这里只复用，不重新定义）
        buf_limit = max(args.max_output, MIN_BUF_FLOOR)
        # 头尾配额直接各取 buf_limit//2：不给接缝标记单独预留。之前预留 512B
        # 导致输出量介于 limit-512 与 limit 之间时内存层就提前溢出丢弃（假截断
        # 丢尾，实测 4000 字节/4096 上限丢 416 字节）。head+seam+tail ≤ limit
        # 的约束交给显示层 _truncate_output 保证（它自己会重算 marker 空间并
        # 收缩 half，且内存层数据已 ≤ limit，通常不再二次截断）。
        buf_half = max(buf_limit // 2, 1024)

        def _read(buf, recv_fn, total_cnt, drop_cnt, reason, spill=None):
            head_buf = []
            head_len = 0
            tail_buf = []       # 滚动保留最近 buf_half 字节
            tail_len = 0
            why = "timeout"  # 循环条件退出=静默超时；EOF/异常/stop_drain 会改写
            while time.time() < silence_deadline[0] and not stop_drain.is_set():
                try:
                    data = recv_fn(RECV_CHUNK)
                except (socket.timeout, TimeoutError):
                    continue  # 无数据 tick，回到循环头检查 deadline
                except Exception:
                    why = "error"  # 通道异常（非 EOF）：残留数据可能未完（H2）
                    break  # 通道关闭/其他错误
                if not data:
                    why = "eof"
                    break  # EOF
                silence_deadline[0] = time.time() + args.exec_timeout  # 有输出则重置静默计时
                total_cnt[0] += len(data)
                if spill is not None:
                    spill.write(data)  # 完整流落盘：内存层丢弃中间字节不影响全文
                if head_len < buf_half:
                    room = buf_half - head_len
                    piece = data[:room]
                    head_buf.append(piece)
                    head_len += len(piece)  # 按实际追加量计（块可能小于 room，不能加 room）
                    rest = data[room:]
                    if rest:
                        # 溢出块送入 tail 滚动（不能直接丢：数据块可能恰好跨越
                        # head 边界，丢了会丢失 <max_output 的输出并误报截断）
                        tail_buf.append(rest)
                        tail_len += len(rest)
                else:
                    tail_buf.append(data)
                    tail_len += len(data)
                # 尾部滚动：保留【最近的】buf_half 字节。溢出从头丢，
                # 但首块本身比溢出量大时只丢它的前缀、保留其尾部——
                # 否则单个大块（高延迟链路整块到达）会被整块弹出，
                # 尾部数据全丢，只剩头部
                overflow = tail_len - buf_half
                while overflow > 0 and tail_buf:
                    old = tail_buf[0]
                    if len(old) <= overflow:
                        tail_buf.pop(0)
                        tail_len -= len(old)
                        drop_cnt[0] += len(old)
                        overflow -= len(old)
                    else:
                        tail_buf[0] = old[overflow:]
                        tail_len -= overflow
                        drop_cnt[0] += overflow
                        overflow = 0
            else:
                # 循环条件退出（未 break）：stop_drain（主线程收尾截断）或静默超时
                why = "eof" if stop_drain.is_set() else "timeout"
            # 组装（追加进共享 buf；主线程 b"".join 后还会过 _truncate_output，
            # 此时数据已 ≤ buf_limit，通常不会再截）
            head_part = b"".join(head_buf)
            tail_part = b"".join(tail_buf)
            # 行对齐与接缝标记【只在真实中间丢弃（滚动溢出，pre-snap drop>0）
            # 时做】：数据 ≤ 2×buf_half 时 head+tail 本就连续完整，对齐反而会
            # 误砍「末行无换行」的结尾（printf 'abc\ndef' 的 def 被吞）或对
            # 放得下的输出制造伪截断。head/tail 各自退到最近换行（限窗 4KB，
            # 二进制无换行保持字节边界），snapped 字节计入 drop_cnt（记账精确）。
            real_gap = drop_cnt[0] > 0
            was_line_boundary = False
            if real_gap and head_part:
                was_line_boundary = head_part[-1:] == b"\n"
                nl = head_part.rfind(b"\n", max(0, len(head_part) - BUF_ALIGN_WINDOW))
                if nl != -1:
                    drop_cnt[0] += len(head_part) - (nl + 1)
                    head_part = head_part[:nl + 1]
            if real_gap and tail_part and not was_line_boundary:
                # 边界恰逢行首时 tail 首行本来就完整，不要整行误删。
                # 只有当第一个 \n 之后还有内容（首行之后存在其他行）时才
                # 消费首行；若 \n 恰是 tail 的最后一个字节（单行输出，整个
                # tail 就是一行），该行是完整行——消费它会把尾部整段丢掉
                # （实测 --max-output<=8192 时单行大输出尾部全丢且无 seam
                # 标记，warnings 还谎称"仅保留头尾"），必须保留
                nl2 = tail_part.find(b"\n", 0, BUF_ALIGN_WINDOW)
                if nl2 != -1 and nl2 < len(tail_part) - 1:
                    drop_cnt[0] += nl2 + 1
                    tail_part = tail_part[nl2 + 1:]
            if real_gap:
                # 行对齐会把 head 尾巴 / tail 头部的半个多字节 UTF-8 字符切开
                # （单行无换行 + 超限 + 中文，实测 seam 处出 U+FFFD 半个字）：
                # 组装前各自退到合法字符边界，被吞的字节计入 drop_cnt 保账目一致。
                # 注意顺序：先对 head 做 from_start 回退、再对 tail 做 from_end
                # 回退，否则 head 缩进后 tail 的相对基准会错位。
                if head_part:
                    hp = _utf8_boundary_cut(head_part, len(head_part))
                    drop_cnt[0] += len(head_part) - len(hp)
                    head_part = hp
                if tail_part:
                    tp = _utf8_boundary_cut(tail_part, len(tail_part), from_start=False)
                    drop_cnt[0] += len(tail_part) - len(tp)
                    tail_part = tp
            seam = b""
            if real_gap and tail_part:
                # 中间丢弃过的接缝处插一行带内标记：纯按字节拼接会让相邻两行
                # 拼成"看起来合法"的假数据（如 seq 输出 ...23696\n23 + 78156\n...）
                # seam 自带前导/后随换行：head 若以换行结尾、tail 若以换行开头
                # 会各多一个空行（4096 最小档实测），拼接前先去重避免空行跳号。
                lead = b"" if (head_part and head_part[-1:] == b"\n") else b"\n"
                trail = b"" if (tail_part and tail_part[:1] == b"\n") else b"\n"
                seam_body = ("[pssh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
                             % drop_cnt[0]).encode("utf-8")
                seam = lead + seam_body + trail
                # 防显示层二次截断切真实尾部：head+seam+tail 超 buf_limit 时
                # （head/tail 各占一半=limit，seam 是额外字节），单行无换行场景
                # 显示层会把 tail 锚定到 seam 换行、走纯前缀回退——实测 4096 档
                # 尾部 30 个 Z 全丢且 omitted 少报 77 字节。从 tail 前缀削字节
                # 计入 drop_cnt（保留真实尾部），循环收敛 seam 数字位数变化。
                for _ in range(3):
                    over = len(head_part) + len(seam) + len(tail_part) - buf_limit
                    if over <= 0 or not tail_part:
                        break
                    drop_cnt[0] += over
                    tail_part = tail_part[over:]
                    if not tail_part:
                        break
                    # 从头部削可能切开半个多字节字符：重新对齐 UTF-8 边界
                    tp = _utf8_boundary_cut(tail_part, len(tail_part), from_start=False)
                    drop_cnt[0] += len(tail_part) - len(tp)
                    tail_part = tp
                    trail = b"" if (tail_part and tail_part[:1] == b"\n") else b"\n"
                    seam_body = ("[pssh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
                                 % drop_cnt[0]).encode("utf-8")
                    seam = lead + seam_body + trail
            buf.append(head_part + seam + tail_part)
            reason[0] = why  # 退出原因供主线程判定：eof=数据收完，timeout=静默超时

        out_reason = [None]  # 读线程退出原因（"eof"/"timeout"），drain 阶段主线程接管判定
        err_reason = [None]
        # 完整流落盘：读线程边收边写（内存层只保留头尾，落盘才是全文）。
        # 截断时保留并回传路径（stdout_spill_file/stderr_spill_file），未截断则删除。
        spill_out_fh, spill_out_path, spill_err_fh, spill_err_path = _spill_writers(args)
        t_out = threading.Thread(target=_read,
                                 args=(out_buf, chan.recv, total_counter, drop_counter, out_reason,
                                       spill_out_fh),
                                 daemon=True)
        if args.pty:
            # PTY 模式下 SSH 服务端把 stderr 合并进 stdout，无独立 stderr 流：
            # 给 stderr 读线程传"立即返回 EOF"的哑函数，线程秒退，后续
            # is_alive()/join() 逻辑无需分支。
            t_err = threading.Thread(target=_read, args=(err_buf, lambda n: b"",
                                                         err_total_counter, err_drop_counter,
                                                         err_reason, spill_err_fh), daemon=True)
        else:
            t_err = threading.Thread(target=_read, args=(err_buf, chan.recv_stderr,
                                                         err_total_counter, err_drop_counter,
                                                         err_reason, spill_err_fh), daemon=True)
        t_out.start(); t_err.start()

        # 不用 recv_exit_status 干等：它内部无限等待 status_event，
        # 远程静默挂死时主线程会永久卡住。改为轮询 exit_status_ready + 静默超时判定
        # （读线程持续排水，不会触发大输出死锁）。
        # 总超时兜底：静默超时只覆盖"无输出"场景；持续输出但不结束的命令
        # （如 while true; echo x）会无限重置静默计时，必须有硬上限。
        # 默认 max(2×exec-timeout, DEFAULT_MIN_TOTAL)，长任务（构建/编译）用 --max-time 手动调大（最高 MAX_TIME_CAP）。
        total_limit = args.max_time if args.max_time is not None else max(args.exec_timeout * 2, DEFAULT_MIN_TOTAL)
        if total_limit > MAX_TIME_CAP:
            # 告警同时进 JSON warnings：只看 stdout 的 AI 必须知道实际生效上限被改小
            msg = ("总时长上限 %ds 超过 %d，本次按 %d 执行（与 --max-time 上限一致；"
                   "更久任务请用 nohup 后台化 + 轮询）" % (total_limit, MAX_TIME_CAP, MAX_TIME_CAP))
            log("[WARN] " + msg)
            warnings.append(msg)
            total_limit = MAX_TIME_CAP
        total_deadline = time.time() + total_limit
        while not chan.exit_status_ready():
            if _SIGTERM_RECEIVED:
                # 在我们自己的 Python 帧里抛 KI 是安全的（在 paramiko C 级
                # 代码里抛才是锁损坏根源——handler 已不再 raise）
                raise KeyboardInterrupt("SIGTERM")
            if chan.closed:
                break
            # 先判读线程退出状态再判总时长：--max-time == --exec-timeout 时
            # 静默挂死应报"无输出"而非误标"持续输出"
            if not t_out.is_alive() and not t_err.is_alive():
                # 双读线程都退出但远程未结束
                if out_reason[0] == "error" or err_reason[0] == "error":
                    # 通道异常（非 EOF/非静默超时）：连接问题，不是命令超时
                    raise SshError("连接中断（通道异常），输出可能不完整", "connection_lost")
                if out_reason[0] == "timeout" or err_reason[0] == "timeout":
                    # 读线程因静默超时退出而远程未结束 -> 判定挂死
                    # （留 1s 宽限，避免 exit-status 包还在路上时误判）
                    if time.time() > silence_deadline[0] + SILENCE_GRACE:
                        raise ExecIdleTimeout(
                            "命令执行超时（连续无输出 %ss）。注意：远程进程可能仍在运行"
                            "（断开连接不会杀掉它），副作用类命令重试前请先 pgrep 确认/清理；"
                            "确认命令只是输出少，可用 --idle-timeout 调大静默窗口" % args.exec_timeout)
                # 双读线程都因 EOF 退出（数据收完）：exit-status 包可能还在路上，
                # 属正常收尾，继续等（total_deadline 兜底），不误报"无输出超时"
            if time.time() > total_deadline:
                if not t_out.is_alive() and not t_err.is_alive() \
                        and out_reason[0] == "eof" and err_reason[0] == "eof":
                    # 双流都已 EOF（数据收完）却始终等不到退出状态：是异常关流，
                    # 不是命令超时——报连接中断才能引导正确排查方向
                    raise SshError("连接中断（输出流已结束但未收到退出状态）", "connection_lost")
                if silence_deadline[0] <= time.time():
                    # 静默窗口也已超时（读线程只是还没到 tick 醒来）：按无输出报
                    raise ExecIdleTimeout(
                        "命令执行超时（连续无输出 %ss，总时长 %ds）。注意：远程进程可能仍在运行，"
                        "重试前请先 pgrep 确认/清理；输出少的慢命令可调大 --idle-timeout"
                        % (args.exec_timeout, total_limit))
                raise ExecTotalTimeout(
                    "命令执行超时（持续输出但未结束，总时长超过 %ds）。长任务请用 --max-time "
                    "调大（最高 %d）；注意：远程进程可能仍在运行，重试前请先 pgrep 确认/清理"
                    % (total_limit, MAX_TIME_CAP))
            time.sleep(POLL_TICK)
        exit_code = chan.exit_status if chan.exit_status_ready() else -1
        if exit_code == -1:
            # 通道已关闭但未收到退出状态（网络中断/远程异常断开）：
            # 输出可能不完整，不能当作成功返回
            raise SshError("连接中断，未收到远程退出状态（输出可能不完整）", "connection_lost")
        # 收尾排水：exit-status 已就绪，但远程后台子进程可能仍占用通道（无 EOF）。
        # 1) 收尾阶段把静默窗口缩短到 min(exec_timeout, 10)，避免无输出的后台进程
        #    拖满整个 exec_timeout 才返回；
        # 2) drain_deadline 兜底：后台进程持续输出时强制截断并标记
        #    output_truncated（已读数据不会丢，读线程边读边写共享 buf）。
        # 收尾阶段【不】缩短 silence_deadline：读线程必须因 EOF（数据收完）退出才算完整；
        # 若因静默超时退出说明输出未完，会被 drain_deadline 强制截断并标记 truncated，
        # 避免"输出被丢但无标记"让 AI 误判完整。drain_deadline 单独兜底返回时长。
        drain_limit = min(args.exec_timeout, DRAIN_WINDOW)
        drain_deadline = time.time() + drain_limit
        while (t_out.is_alive() or t_err.is_alive()) and time.time() < drain_deadline:
            if _SIGTERM_RECEIVED:
                # 排水期收到信号：不再等读线程自然退出，立即归位中断。
                # 否则 responder 关 socket 会让 drain 读到"通道异常"误标
                # output_truncated，AI 看到"命令成功但输出被截断"而非"被中断"
                raise KeyboardInterrupt("SIGTERM")
            time.sleep(POLL_TICK)
        t_out.join(0.5)
        t_err.join(0.5)
        drain_truncated = (t_out.is_alive() or t_err.is_alive())

        def _drain_rest(recv_fn, buf, deadline, total_cnt=None, spill=None):
            """主线程接管排空通道缓冲的尾部数据（读线程已因静默超时退出时）。

            exit-status 已就绪说明远程 shell 已退出，剩余数据读完即 EOF，
            不会无限阻塞；deadline 兜底防后台子进程持续占用。
            返回 True 表示收到 EOF（完整）。
            """
            got_eof = True
            got = 0
            limit = args.max_output  # 排空也限内存：超限即视为不完整
            while time.time() < deadline:
                try:
                    data = recv_fn(RECV_CHUNK)
                except (socket.timeout, TimeoutError):
                    continue
                except Exception:
                    got_eof = False  # 通道异常中断排空：数据未完，必须标记截断（H1）
                    break
                if not data:
                    break
                if spill is not None:
                    spill.write(data)  # 排空阶段也写完整流（读线程提前退出时兜底）
                if got + len(data) > limit:
                    buf.append(data[:limit - got])
                    if total_cnt is not None:
                        # 超限丢弃的部分也是"已接收"的字节：全额计入原始
                        # 统计（此前漏计，stdout_bytes 低估真实接收量）
                        total_cnt[0] += len(data)
                    got_eof = False  # 超出上限：输出未完，标记截断
                    break
                buf.append(data)
                got += len(data)
                if total_cnt is not None:
                    total_cnt[0] += len(data)  # 排空阶段也计入原始字节统计（L4）
            else:
                got_eof = False  # 循环条件退出（未 break）= 超时未完
            return got_eof

        if drain_truncated:
            stop_drain.set()
            # 读线程可能正阻塞在 recv（最长 1s tick），set 后等它醒来收完
            # 最后一块数据再退出，避免 join 后组装时漏掉最后一块输出
            t_out.join(JOIN_GRACE)
            t_err.join(JOIN_GRACE)
            warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
            log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
        else:
            # 读线程已退出：若因静默超时/通道异常（非 EOF）退出，通道缓冲里可能还有
            # 命令的尾部输出（如 'sleep 3.5; echo END' 的 END），主线程接管排空，
            # 否则尾部数据丢失且无任何标记（BUG：ok=True 但输出不完整）
            if out_reason[0] in ("timeout", "error"):
                # 用新鲜 deadline：外层 drain_deadline 可能已被上面的等待循环耗尽，
                # 复用会让排空窗口为 0、一行尾部数据都读不到
                if not _drain_rest(chan.recv, out_buf, time.time() + drain_limit, total_counter,
                                   spill_out_fh):
                    drain_truncated = True
                    warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
                    log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
            if err_reason[0] in ("timeout", "error"):
                if not _drain_rest(chan.recv_stderr, err_buf, time.time() + drain_limit,
                                   err_total_counter, spill_err_fh):
                    drain_truncated = True
                    warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
                    log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
        # UTF-8 解码（errors="replace"）：exec 只适合文本输出，二进制内容会损坏
        # 超限截断保留头尾：防止大输出撑爆调用方（AI）的上下文窗口
        if _SIGTERM_RECEIVED:
            # drain/排空阶段信号到达的最终兜底：组装前归位中断，防止
            # 以 ok:true + 远程退出码退出（信号被吞、退出码非 130）
            raise KeyboardInterrupt("SIGTERM")
        out_raw = b"".join(out_buf)
        err_raw = b"".join(err_buf)
        if drop_counter[0]:
            warnings.append("stdout 过大，内存缓冲已丢弃中间 %d 字节（仅保留头尾；调大 --max-output 可取更多）"
                            % drop_counter[0])
        if err_drop_counter[0]:
            warnings.append("stderr 过大，内存缓冲已丢弃中间 %d 字节（仅保留头尾；调大 --max-output 可取更多）"
                            % err_drop_counter[0])
        out_cut, out_trunc, out_omitted = _truncate_output(out_raw, args.max_output, "stdout")
        err_cut, err_trunc, err_omitted = _truncate_output(err_raw, args.max_output, "stderr")
        if out_trunc:
            warnings.append("stdout 已截断：省略 %d/%d 字节（--max-output 调整）"
                            % (out_omitted, len(out_raw)))
        if err_trunc:
            warnings.append("stderr 已截断：省略 %d/%d 字节（--max-output 调整）"
                            % (err_omitted, len(err_raw)))
        out_s = _clean_pty_text(out_cut.decode("utf-8", errors="replace"), args)
        err_s = _clean_pty_text(err_cut.decode("utf-8", errors="replace"), args)
        duration = int((time.time() - start) * 1000)

        if exit_code == 255:
            warnings.append("远程退出码为 255，本地返回 254（255 保留给连接失败语义）")
        stdout_truncated = bool(out_trunc or drop_counter[0])
        stderr_truncated = bool(err_trunc or err_drop_counter[0])
        result = {
            "ok": True,          # 工具操作成功（连接+执行完成）；命令是否成功看 exit_success / exit_code
            "action": "exec",
            "version": VERSION,
            "exit_code": exit_code,
            "local_exit_code": 254 if exit_code == 255 else exit_code,  # 本地实际退出码（255 时本地返 254）
            "exit_success": exit_code == 0,  # 远程命令退出码是否为 0（AI 判断命令成败用这个）
            "stdout": out_s,
            "stderr": err_s,
            "stdout_bytes": total_counter[0],  # 原始接收字节数（截断/丢弃前；与保留量不同见 omitted）
            "stderr_bytes": err_total_counter[0],
            "stdout_truncated": stdout_truncated,   # 该流是否被截断/丢弃过中间
            "stderr_truncated": stderr_truncated,
            "stdout_omitted_bytes": total_counter[0] - len(out_cut),  # 原始字节-最终展示字节（含 marker 附加；kept+omitted 恒等于 stdout_bytes，AI 可精确对账）
            "stderr_omitted_bytes": err_total_counter[0] - len(err_cut),
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "pty": bool(args.pty),
            "pty_strip_ansi": bool(args.pty_strip_ansi),
            "cmd": cmd,  # 完整回显（不截断）：含凭据的命令会原样出现在 JSON，转发结果前需脱敏
            "output_truncated": bool(drain_truncated or stdout_truncated or stderr_truncated),
            "warnings": warnings,
            "duration_ms": duration,
        }
        # spill 收尾：截断（或 drain 不完整）时保留完整输出文件并把路径回传 JSON；
        # 未截断则删除，不留垃圾。置 _spill_handled 让 finally 跳过（成功路径自己管）。
        out_keep = stdout_truncated or drain_truncated
        err_keep = stderr_truncated or drain_truncated
        _close_spill(spill_out_fh, spill_out_path, keep=out_keep)
        _close_spill(spill_err_fh, spill_err_path, keep=err_keep)
        if out_keep and spill_out_path:
            result["stdout_spill_file"] = spill_out_path
        if err_keep and spill_err_path:
            result["stderr_spill_file"] = spill_err_path
        _spill_handled = True
        header = "[%s]  exit_code=%d  duration=%dms" % (
            "OK" if exit_code == 0 else "EXIT %d" % exit_code, exit_code, duration)
        sections = [("STDOUT", out_s), ("STDERR", err_s)]
        emit(result, header=header, sections=sections, use_json=args.json)
        if exit_code == 255:
            # 255 保留给"连接失败"，远程真实退出码 255 时本地改返 254 以免调用方混淆
            log("[WARN] 远程退出码为 255，本地返回 254（255 保留给连接失败语义）")
            return 254
        return exit_code
    except paramiko.SSHException as e:
        if _SIGTERM_RECEIVED:
            # KI 被 paramiko 展开中的 SSHException 替换时按标志归位（同 generic 分支）
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        emit_error(args.json, "exec_failed", str(e), extra=_partial_extra())
        return 255
    except SshError as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_partial_extra())
        return 255
    except KeyboardInterrupt as e:
        # 中断时也带部分输出：AI 判断命令是否已部分执行、能否安全重试
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_partial_extra())
        return 130
    except Exception as e:
        msg = str(e)
        if _SIGTERM_RECEIVED:
            # KI 被 paramiko 展开中的新异常覆盖时按标志归位（同 upload/download）
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        # 超时类给独立退出码 124（对齐 GNU timeout 惯例）：与"连接失败 255"
        # 区分开——调用方只看退出码也能选对重试方向（调超时 vs 查网络）
        if isinstance(e, ExecIdleTimeout):
            error_type = "exec_idle_timeout"
        elif isinstance(e, ExecTotalTimeout):
            error_type = "exec_total_timeout"
        elif isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower() \
                or "timed out" in msg.lower():
            # 非本工具抛出的超时（paramiko 等）：消息各自写清，直接透传
            error_type = "exec_timeout"
        else:
            error_type = "exec_failed"
        emit_error(args.json, error_type, msg, extra=_partial_extra())
        return 124 if error_type != "exec_failed" else 255
    finally:
        # spill 兜底：成功路径已置 _spill_handled；异常/中断路径在此删除，不留垃圾
        if not _spill_handled:
            _close_spill(spill_out_fh, spill_out_path, keep=False)
            _close_spill(spill_err_fh, spill_err_path, keep=False)
        close_all(client)


def cmd_upload(args):
    start = time.time()  # 计时含连接耗时
    local = _fix_msys_local_path(args.local)
    remote = _fix_msys_remote_path(args.remote)
    # 用户意图标记：--remote 以 / 结尾 = 期望目标是目录（scp 语义）。
    # _normalize_remote_path 会剥掉尾斜杠，这里先记录，供单文件分支区分
    # "目标应是目录但不存在"——静默创建同名文件是静默错误（应报错）
    remote_ends_slash = remote.endswith("/")
    if not os.path.exists(local):
        msg = "本地路径不存在: %s" % local
        if any(c in local for c in _MSYS_GLOB_CHARS) \
                or any(c in _MSYS_PRIVATE_GLOB for c in local):
            msg += "（路径含通配符：pssh 不做本地 glob 展开，请先在 shell 展开成明确路径）"
        emit_error(args.json, "bad_args", msg)
        return 2

    # 失败上下文预置（try 之前）：任何异常路径的 extra 字段都齐全且一致
    files_transferred = 0
    files_skipped = 0
    total_bytes = 0        # 清单总大小（含 skipped；实际传了多少看 bytes_transferred）
    bytes_transferred = 0  # 实际传输字节（skip-existing 全跳过时为 0，与 bytes 区分）
    file_list = None       # None=尚未开始；空列表=刚开始就失败（同样有断点价值）
    walk_warnings = []
    bytes_uploaded = [0]  # put 回调累计已传字节：中断/失败时 JSON 报真实进度
    if args.dry_run and args.skip_existing:
        # dry-run 零远端 I/O，无法预演 skip 判定；stderr 与结果 warnings 双通道说明
        msg = "dry-run 不做远端 I/O，--skip-existing 未预演（实跑时才判定）"
        log("[WARN] " + msg)
        walk_warnings.append(msg)

    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    def _fail_extra():
        """失败/中断的统一 extra：host/user/port（与连接期错误一致）+ 传输进度。"""
        return _transfer_extra(
            conn,
            file_list=file_list or [],
            files=files_transferred,
            skipped=files_skipped,
            bytes=total_bytes,
            bytes_transferred=max(bytes_transferred, bytes_uploaded[0]),
            warnings=list(walk_warnings) + list(_PUT_RESIDUE_WARNINGS))

    is_dir = os.path.isdir(local)
    no_recur = (args.recursive is False)  # 显式 --no-recursive
    tag = "递归" if (is_dir and not no_recur) else ("目录(不递归)" if is_dir else "单文件")
    log("[SFTP] 上传 %s -> %s (%s%s)" % (
        local, remote, tag, ", dry-run" if args.dry_run else ""))

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠）；上传是"新建路径"，含 glob 字符
        # 几乎必是笔误——按字面量会创建出名为 * 的文件/目录，直接拒绝
        remote = _normalize_remote_path(sftp, remote)
        if any(c in remote for c in "*?["):
            raise SshError("远端路径 %s 含通配符：SFTP 不做 glob 展开（会按字面量创建），"
                           "请写明确的完整路径" % remote, "bad_args")
        file_list = []  # 每项 {"path","size","transferred","skipped"}：失败时 AI 可精确断点重试

        if is_dir and no_recur:
            # 目录但 --no-recursive: 不递归，只创建远程目录壳
            if not args.dry_run:
                sftp_makedirs(sftp, remote)
            log("[SKIP] 目录 + --no-recursive: 只创建远程目录，不传子项")
        elif is_dir:
            # 目录：递归上传（onerror 收集不可读目录，避免静默部分成功）
            def _walk_onerror(err):
                walk_warnings.append(_sanitize_log_text(
                    "跳过无法读取的本地目录: %s (%s)" % (getattr(err, "filename", "?"), err)))
            for root, dirs, filenames in os.walk(local, onerror=_walk_onerror):
                rel_root = os.path.relpath(root, local)
                remote_root = remote if rel_root == "." else posixpath.join(
                    remote, rel_root.replace(os.sep, "/"))
                if not args.dry_run:
                    sftp_makedirs(sftp, remote_root)
                for fn in filenames:
                    if _SIGTERM_RECEIVED:
                        raise KeyboardInterrupt("SIGTERM")  # 文件间是安全检查点
                    local_file = os.path.join(root, fn)
                    remote_file = posixpath.join(remote_root, fn)
                    size = os.path.getsize(local_file)
                    rel = os.path.relpath(local_file, local).replace(os.sep, "/")
                    skip = bool(args.skip_existing and (not args.dry_run)
                                and _remote_size_is(sftp, remote_file, size))
                    entry = {"path": rel, "size": size, "transferred": False, "skipped": skip}
                    file_list.append(entry)
                    total_bytes += size
                    if skip:
                        files_skipped += 1
                        log("[SKIP] %s (%s, 远端已存在同大小文件)" % (rel, format_size(size)))
                        continue
                    if not args.dry_run:
                        _sftp_put_atomic(sftp, local_file, remote_file, progress=bytes_uploaded)  # 权限由服务器 umask 决定（默认如 644）
                        entry["transferred"] = True
                        bytes_transferred += size
                    log("[FILE] %s (%s)" % (rel, format_size(size)))
                    files_transferred += 1
        else:
            # 单文件
            size = os.path.getsize(local)
            name = os.path.basename(local)
            rstat = None
            if not args.dry_run:  # dry-run 应零远端 I/O
                try:
                    _sftp_touch_activity(sftp)  # 刷新看门狗活动时间
                    rstat = sftp.stat(remote)
                except (socket.timeout, TimeoutError):
                    raise  # 外层统一报 upload_timeout（M2：不能吞掉超时）
                except IOError:
                    rstat = None  # 不存在，正常创建
                if rstat is None and remote_ends_slash:
                    # 尾斜杠 + 目标不存在：用户意图是目录（scp 语义下尾斜杠
                    # 表示"放进此目录"），静默创建同名文件是静默错误——
                    # 此前实测会创建 /root/newdir 同名文件且 remote 回显
                    # 无尾斜杠路径，AI 误判落点是目录。明确报 bad_args
                    raise SshError(
                        "远端路径 %s 以 / 结尾（意图是目录）但目标不存在："
                        "请先创建该目录，或去掉尾斜杠改为文件路径" % remote,
                        "bad_args")
                if rstat is not None and remote_ends_slash and not stat.S_ISDIR(rstat.st_mode):
                    # 尾斜杠 + 目标存在但【不是目录】：同样意图是目录（fix ⑨
                    # 只挡了"不存在"半边，这里补"是文件"半边）——此前实测会
                    # 静默覆盖文件且 ok:true。明确报 bad_args，绝不静默覆盖
                    raise SshError(
                        "远端路径 %s 以 / 结尾（意图是目录）但目标已存在且不是目录："
                        "请改为文件路径，或先删除该文件" % remote,
                        "bad_args")
                if rstat is not None and stat.S_ISDIR(rstat.st_mode):
                    # scp 语义：目标是已存在目录 -> 放入目录内（不报错、不嵌套）
                    remote = posixpath.join(remote, name)
                    log("[PATH] 远端目标是目录，改为放入: %s" % remote)
                    rstat = None
                    try:
                        rstat = sftp.stat(remote)
                    except (socket.timeout, TimeoutError):
                        raise
                    except IOError:
                        rstat = None
            if args.dry_run and remote_ends_slash:
                # dry-run 承诺零远端 I/O，无法确认尾斜杠目标的类型：实跑时
                # 目标不存在/已存在文件都会报 bad_args，仅已存在目录会放入其中
                walk_warnings.append(
                    "dry-run 未验证尾斜杠目标 %s 的类型（实跑时目标不存在或"
                    "非目录会报 bad_args；已存在目录则放入其中）" % remote)
            skip = bool(args.skip_existing and rstat is not None and rstat.st_size == size)
            entry = {"path": name, "size": size, "transferred": False, "skipped": skip}
            file_list.append(entry)  # 先入清单再传输：失败时 AI 能定位到具体文件
            total_bytes = size  # 失败时 bytes 也要反映清单大小（提前赋值，不能只在成功路径设）
            if skip:
                files_skipped = 1
                log("[SKIP] %s (%s, 远端已存在同大小文件)" % (name, format_size(size)))
            elif not args.dry_run:
                parent = posixpath.dirname(remote)
                if parent:
                    sftp_makedirs(sftp, parent)
                _sftp_put_atomic(sftp, local, remote, progress=bytes_uploaded)
                entry["transferred"] = True
                files_transferred = 1
                bytes_transferred = size
            else:
                files_transferred = 1  # dry-run：假装会传（bytes_transferred 保持 0，真实反映零传输）
            log("[FILE] %s (%s)" % (name, format_size(size)))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "upload",
            "version": VERSION,
            "local": local,
            "remote": remote,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "is_dir": is_dir,
            "files": files_transferred,
            "skipped": files_skipped,
            "bytes": total_bytes,           # 清单总大小（含 skipped）
            "bytes_transferred": bytes_transferred,  # 实际传输字节（skip 全跳过时为 0）
            "file_list": file_list,
            "dry_run": bool(args.dry_run),
            "warnings": list(walk_warnings) + list(_PUT_RESIDUE_WARNINGS),
            "duration_ms": duration,
        }
        header = "[OK]  %d 文件, %s, %dms%s%s" % (
            files_transferred, format_size(total_bytes), duration,
            ", 跳过 %d" % files_skipped if files_skipped else "",
            " (dry-run)" if args.dry_run else "")
        sections = [("RESULT", json.dumps(result, ensure_ascii=False))]
        emit(result, header=header, sections=sections, use_json=args.json)
        return 0
    except KeyboardInterrupt as e:
        # 中断也带清单（transferred 标记精确到文件）：AI 判断重试策略
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_fail_extra())
        return 130
    except SshError as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        # sftp_makedirs 的"远程路径已存在且不是目录"、通配符拒绝等参数类错误：
        # 退出码 2（改参数可解决），不是传输失败 1
        emit_error(args.json, e.error_type, str(e), extra=_fail_extra())
        return 2 if e.error_type == "bad_args" else 1
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        emit_error(args.json, "upload_timeout",
                   "SFTP 传输超时：%d 秒无任何数据（连接可能已被 NAT/网络静默断开）" % SFTP_IO_TIMEOUT,
                   extra=_fail_extra())
        return 1
    except Exception as e:
        # 失败带清单：未完成的那条 transferred=false，AI 知道差哪些
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        if "sftp" in locals() and getattr(sftp, "_pssh_watchdog_killed", False):
            emit_error(args.json, "upload_timeout",
                       "SFTP 传输超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_fail_extra())
        else:
            emit_error(args.json, "upload_failed", str(e), extra=_fail_extra())
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


def cmd_download(args):
    start = time.time()  # 计时含连接耗时
    local = _fix_msys_local_path(args.local)
    remote = _fix_msys_remote_path(args.remote)
    if not local:
        # --local '' 之类：os.replace('.part.<pid>', '') 会抛 FileNotFoundError
        # （实测 download_failed + 晦涩消息）。空目标路径是参数错误，明确 bad_args
        emit_error(args.json, "bad_args", "本地目标路径为空（--local 未指定有效路径）")
        return 2

    # 失败上下文预置（try 之前）：任何异常路径的 extra 字段都齐全且一致
    files_transferred = 0
    files_skipped = 0
    total_bytes = 0        # 清单总大小（含 skipped；实际传了多少看 bytes_transferred）
    bytes_transferred = 0
    file_list = None       # None=尚未开始；空列表=刚开始就失败（同样有断点价值）
    parallel_used = 1      # 本次实际并行连接数（结果回显，AI 无需猜测档位）
    dl_warnings = []

    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    def _fail_extra():
        """失败/中断的统一 extra：host/user/port（与连接期错误一致）+ 传输进度。"""
        return _transfer_extra(
            conn,
            file_list=file_list or [],
            files=files_transferred,
            skipped=files_skipped,
            bytes=total_bytes,
            bytes_transferred=bytes_transferred,
            warnings=list(dl_warnings))

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠；~user 形式明确报错）
        remote = _normalize_remote_path(sftp, remote)
        try:
            _sftp_touch_activity(sftp)  # 刷新看门狗活动时间
            rstat = sftp.stat(remote)
        except (socket.timeout, TimeoutError):
            raise  # 外层统一报 download_timeout（M2）
        except IOError as e:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")  # 交给命令层 KI 分支按中断处理
            if getattr(sftp, "_pssh_watchdog_killed", False):
                raise  # 外层按看门狗分支报 download_timeout
            if e.errno == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                # 确实不存在时若路径含通配符，说明真实原因是"SFTP 无 glob"而非文件缺失
                if any(c in remote for c in "*?["):
                    msg = _remote_glob_error(remote)
                else:
                    msg = "远程路径不存在: %s" % remote
                emit_error(args.json, "bad_args", msg,
                           extra=_conn_extra(locals().get("conn")))
            else:
                # 权限不足等真实错误：不能误导为"路径不存在"
                emit_error(args.json, "download_failed",
                           "无法访问远程路径 %s（不存在或权限不足）: %s" % (remote, e),
                           extra=_conn_extra(locals().get("conn")))
                return 1
            return 2

        is_dir = stat.S_ISDIR(rstat.st_mode)
        no_recur = (args.recursive is False)  # 显式 --no-recursive
        tag = "递归" if (is_dir and not no_recur) else ("目录(不递归)" if is_dir else "单文件")
        log("[SFTP] 下载 %s -> %s (%s%s)" % (
            remote, local, tag, ", dry-run" if args.dry_run else ""))

        file_list = []  # 每项 {"path","size","transferred","skipped"}：失败时 AI 可精确断点重试

        if is_dir and os.path.isfile(local):
            # 本地目标已存在且是文件：os.makedirs(..., exist_ok=True) 会抛
            # FileExistsError（实测 download_failed 消息晦涩），且与"目录下载"
            # 语义冲突——明确报 bad_args（远端是文件时覆盖本地文件仍允许）
            emit_error(args.json, "bad_args",
                       "本地目标 %s 已存在且是文件，不能作为目录下载目标"
                       "（请换目录路径或先删除该文件）" % local,
                       extra=_transfer_extra(conn, file_list=file_list or []))
            return 2

        if is_dir and no_recur:
            # 目录但 --no-recursive: 不递归，只创建本地目录壳
            if not args.dry_run:
                os.makedirs(local, exist_ok=True)
            log("[SKIP] 目录 + --no-recursive: 只创建本地目录，不下载子项")
        elif is_dir:
            if not args.dry_run:
                os.makedirs(local, exist_ok=True)
            if args.parallel is not None:
                # 显式 --parallel 对目录下载不生效（逐文件串行），明说防误导
                dl_warnings.append("--parallel 仅对单文件分片下载生效；"
                                   "目录下载为逐文件串行（parallel_used=1）")
            used_local_names = set()  # Windows 大小写不敏感/清洗后同名的冲突检测
            for rel, full, attr, entry_is_dir in sftp_walk(sftp, remote, warnings=dl_warnings):
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")  # 条目间是安全检查点
                if entry_is_dir:
                    # 空目录也要重建：walk 现在产出目录条目（先于其子项），
                    # 只处理文件的话空目录会静默消失
                    if os.name == "nt":
                        # 目录名同样要过 Windows 安全化（保留设备名/控制字符/
                        # 大小写冲突），否则危险目录名可能让整树无法创建
                        safe = _win_safe_rel_path(rel, used_local_names, dl_warnings)
                        if safe is None:
                            continue
                        rel = safe
                    if not args.dry_run:
                        os.makedirs(os.path.join(local, rel.replace("/", os.sep)),
                                    exist_ok=True)
                    continue
                if stat.S_ISLNK(attr.st_mode):
                    # 符号链接一律跳过而不是跟随：悬空链接 sftp.get 报
                    # "No such file"、指向目录的报 "Failure"，都会中止整个
                    # 目录下载且消息无法理解；lstat 的 size 是链接串长度，
                    # 跟随下载还会让 file_list/bytes 记账失真、skip-existing
                    # 永不命中。需要内容的请对指向的具体路径单独 download。
                    dl_warnings.append(_sanitize_log_text(
                        "跳过符号链接 %s（SFTP 目录下载不跟随链接；如需内容请对指向的具体路径单独 download）" % rel))
                    continue
                if stat.S_ISFIFO(attr.st_mode) or stat.S_ISSOCK(attr.st_mode) \
                        or stat.S_ISCHR(attr.st_mode) or stat.S_ISBLK(attr.st_mode):
                    # FIFO/套接字/设备文件：服务端 open(FIFO) 会阻塞写端等读端，
                    # sftp.get 挂到 30s 看门狗后中止整个目录（实测 0 文件到达）
                    dl_warnings.append(_sanitize_log_text(
                        "跳过非常规文件 %s（FIFO/套接字/设备文件无法经 SFTP 下载）" % rel))
                    continue
                if os.name == "nt":
                    # Windows 下 Linux 文件名（控制字符/foo:bar/a*b 等）会导致
                    # 整目录下载失败：统一清洗/跳过危险段/冲突改名（与目录条目
                    # 同一逻辑，见 _win_safe_rel_path）
                    safe = _win_safe_rel_path(rel, used_local_names, dl_warnings)
                    if safe is None:
                        continue  # 跳过整个文件
                    rel = safe
                local_file = os.path.join(local, rel.replace("/", os.sep))
                size = attr.st_size
                skip = bool(args.skip_existing and os.path.isfile(local_file)
                            and os.path.getsize(local_file) == size)
                entry = {"path": rel, "size": size, "transferred": False, "skipped": skip}
                file_list.append(entry)
                total_bytes += size
                if skip:
                    files_skipped += 1
                    log("[SKIP] %s (%s, 本地已存在同大小文件)" % (_sanitize_log_text(rel), format_size(size)))
                    continue
                if not args.dry_run:
                    parent = os.path.dirname(local_file)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    _atomic_local_write(
                        lambda part, _r=full: sftp.get(_r, part, callback=_make_sftp_touch(sftp)),
                        local_file)
                    # 保留远程权限位但掩掉 setuid/setgid/sticky（0o4000/0o2000/0o1000）：
                    # 远端文件属性不受本方控制，root 下载 4755 文件会在本地造出
                    # setuid-root 二进制，属本地提权落点
                    try:
                        os.chmod(local_file, stat.S_IMODE(attr.st_mode) & 0o777)
                    except OSError:
                        # chmod 失败不能算传输失败：文件已原子改名就位，降级为
                        # warning（Windows 只读属性等场景；报 download_failed 会让
                        # AI 误判重下已经就位的文件）
                        dl_warnings.append("设置文件权限失败（已下载，权限未应用）: %s"
                                           % _sanitize_log_text(rel))
                    entry["transferred"] = True
                    bytes_transferred += size
                log("[FILE] %s (%s)" % (_sanitize_log_text(rel), format_size(size)))
                files_transferred += 1
        else:
            size = rstat.st_size
            name = os.path.basename(remote)
            if not name:
                emit_error(args.json, "bad_args",
                           "无法从远程路径 %s 确定文件名（根目录/以 / 结尾），"
                           "请写完整的远程文件路径" % remote,
                           extra=_transfer_extra(conn, file_list=file_list or []))
                return 2
            if os.path.isdir(local):
                # scp 语义：--local 是已存在目录 -> 文件放入该目录（--local . 可用）
                local = os.path.join(local, name)
                log("[PATH] 本地目标是目录，改为放入: %s" % local)
            skip = bool(args.skip_existing and os.path.isfile(local)
                        and os.path.getsize(local) == size)
            entry = {"path": name, "size": size, "transferred": False, "skipped": skip}
            file_list.append(entry)  # 先入清单再传输：失败时 AI 能定位到具体文件
            total_bytes = size  # 失败时 bytes 也要反映清单大小（提前赋值，不能只在成功路径设）
            if skip:
                files_skipped = 1
                log("[SKIP] %s (%s, 本地已存在同大小文件)" % (name, format_size(size)))
            elif not args.dry_run:
                parent = os.path.dirname(local)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                k = args.parallel
                if k is None:
                    k = 4 if size >= PARALLEL_MIN_SIZE else 1
                # 显式 --parallel 时只要求 64KB 就并行（尊重用户意图）；
                # 自动档 1MB 下限（k 已由 8MB 阈值把关，这里是双保险）
                if k > 1 and size >= (64 * 1024 if args.parallel is not None else 1024 * 1024):
                    parallel_used = k
                    # 大文件多连接分片：规避单 TCP 流在高丢包链路的吞吐塌陷。
                    # 先关空闲的主 SFTP 会话：它的看门狗（30s）会强断主 transport
                    # 的 socket，连带杀死复用该 transport 的分片（实测踩坑）。
                    # 写 .part 成功后原子改名：失败/中断删除 .part，绝不留下
                    # "看似完整实则损坏"的半截文件（空洞会被 --skip-existing 误判）
                    try:
                        sftp.close()
                    except Exception:
                        pass
                    sftp = None
                    part = _part_path(local)
                    try:
                        _parallel_fetch(conn, args, remote, part, size, k)
                        os.replace(part, local)
                    except BaseException:
                        try:
                            if os.path.exists(part):
                                os.remove(part)
                        except OSError:
                            # Windows 下句柄未释放等会让删除失败：稍等重试一次，
                            # 仍失败至少留 WARN（静默残留违背"不留半截文件"承诺）
                            time.sleep(RETRY_SLEEP)
                            try:
                                os.remove(part)
                            except OSError:
                                log("[WARN] 清理临时文件失败（可能有进程占用）： %s" % part)
                        raise
                else:
                    # 串行单连接同样走 .part + 原子改名：原子性不应随文件大小/
                    # 并行度静默变化（小文件中断也曾留下最终名的半截文件）
                    _atomic_local_write(
                        lambda part: sftp.get(remote, part, callback=_make_sftp_touch(sftp)),
                        local)
                try:
                    os.chmod(local, stat.S_IMODE(rstat.st_mode) & 0o777)  # 掩掉 setuid 等特殊位
                except OSError:
                    # 文件已就位，chmod 失败降级为 warning（同目录路径的处理）
                    dl_warnings.append("设置文件权限失败（已下载，权限未应用）: %s"
                                       % _sanitize_log_text(name))
                entry["transferred"] = True
                files_transferred = 1
                bytes_transferred = size
            else:
                files_transferred = 1  # dry-run：假装会传（bytes_transferred 保持 0，真实反映零传输）
            log("[FILE] %s (%s)" % (name, format_size(size)))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "download",
            "version": VERSION,
            "remote": remote,
            "local": local,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "is_dir": is_dir,
            "files": files_transferred,
            "skipped": files_skipped,
            "bytes": total_bytes,           # 清单总大小（含 skipped）
            "bytes_transferred": bytes_transferred,  # 实际传输字节（skip 全跳过时为 0）
            "parallel_used": parallel_used,
            "file_list": file_list,
            "dry_run": bool(args.dry_run),
            "warnings": list(dl_warnings),
            "duration_ms": duration,
        }
        header = "[OK]  %d 文件, %s, %dms%s%s" % (
            files_transferred, format_size(total_bytes), duration,
            ", 跳过 %d" % files_skipped if files_skipped else "",
            " (dry-run)" if args.dry_run else "")
        sections = [("RESULT", json.dumps(result, ensure_ascii=False))]
        emit(result, header=header, sections=sections, use_json=args.json)
        return 0
    except KeyboardInterrupt as e:
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_fail_extra())
        return 130
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程强断连接会让 _parallel_fetch 的分片报 "Socket is closed"
            # 并以 SshError 抛出——按标志归位为中断，而非 download_failed
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        # _parallel_fetch / 分片建连抛出的结构化错误：保留 error_type
        #（download_timeout 不能被泛化成 download_failed；worker 建连失败属连接类 255）
        emit_error(args.json, e.error_type, str(e), extra=_fail_extra())
        if e.error_type == "bad_args":
            return 2
        if e.error_type in ("auth_failed", "connection_timeout", "connection_refused",
                            "connection_failed", "dns_failed", "jump_failed",
                            "host_key_rejected", "ssh_error"):
            return 255
        return 1
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            # 信号打断串行 SFTP I/O 时 KI 常被 paramiko 展开中的新异常覆盖，
            # 落到这里——按标志强制归位为 interrupted/130
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        emit_error(args.json, "download_timeout",
                   "SFTP 传输超时：%d 秒无任何数据（连接可能已被 NAT/网络静默断开）" % SFTP_IO_TIMEOUT,
                   extra=_fail_extra())
        return 1
    except Exception as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        if "sftp" in locals() and getattr(sftp, "_pssh_watchdog_killed", False):
            emit_error(args.json, "download_timeout",
                       "SFTP 传输超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_fail_extra())
        else:
            emit_error(args.json, "download_failed", str(e), extra=_fail_extra())
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


def cmd_test(args):
    start = time.time()  # 计时含连接耗时
    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    try:
        # 取服务器信息（带超时兜底：uname 挂死也不能卡住 test）
        info_cmd = "uname -s; uname -r; uname -m; hostname"
        stdin, stdout, stderr = client.exec_command(info_cmd, timeout=args.timeout)
        try:
            stdin.close()
        except Exception:
            pass
        chan = stdout.channel
        chan.settimeout(1.0)
        deadline = time.time() + args.timeout
        chunks = []
        err_chunks = []  # M4: 读 stderr 辅助诊断（uname 报错信息不丢）
        while time.time() < deadline:
            if _SIGTERM_RECEIVED:
                # 信号已到：read 循环是安全检查点，主动抛 KI 走 interrupted/130
                #（否则 responder 关 socket 后 recv 异常 break 会走正常路径，
                #  拼出 ok:true + 退出码 0，信号被完全吞掉——实测 P1）
                raise KeyboardInterrupt("SIGTERM")
            try:
                data = chan.recv(RECV_CHUNK)
            except (socket.timeout, TimeoutError):
                # 无 stdout 数据：趁机收 stderr（BUG-9：EOF 后 stderr 尾部不能丢）
                try:
                    ed = chan.recv_stderr(RECV_CHUNK)
                    if ed:
                        err_chunks.append(ed)
                except Exception:
                    pass
                continue
            except Exception:
                break
            if not data:
                # stdout EOF：命令已结束（uname 类），短窗口（1s）收完 stderr 尾部
                # 即退出，不再空转到固定 deadline（否则每次 test 固定耗时 --timeout 秒）
                eof_at = time.time()
                while time.time() - eof_at < STDERR_EOF_WINDOW:
                    try:
                        ed = chan.recv_stderr(RECV_CHUNK)
                    except Exception:
                        break
                    if not ed:
                        break
                    err_chunks.append(ed)
                break
            chunks.append(data)
            try:
                ed = chan.recv_stderr(RECV_CHUNK)
                if ed:
                    err_chunks.append(ed)
            except Exception:
                pass
        status_ready = chan.exit_status_ready()
        if not status_ready:
            # stdout EOF 后 exit-status 包可能还在路上（高延迟链路实测会晚到）：
            # 短等 2s 再下结论，避免把成功的探测误报成"超时/未完成"
            _status_wait = time.time() + STATUS_GRACE
            while time.time() < _status_wait and not chan.exit_status_ready():
                time.sleep(POLL_TICK)
            status_ready = chan.exit_status_ready()
        out_s = b"".join(chunks).decode("utf-8", errors="replace").strip().splitlines()
        err_s = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
        warnings = []
        if not status_ready:
            # M4: 超时/EOF 未收到退出状态 -> 明确提示探测未完成（不再静默 ok=true）
            warnings.append("服务器信息探测超时/未完成（未收到命令退出状态，结果可能不完整）")
        if err_s:
            warnings.append("服务器信息探测 stderr: %s" % _sanitize_log_text(err_s[:200]))
        if len(out_s) < 4:
            warnings.append("服务器信息探测不完整（uname/hostname 输出缺失，命令可能被限制或系统特殊）")
        os_name = out_s[0] if len(out_s) > 0 else ""
        os_kernel = out_s[1] if len(out_s) > 1 else ""
        os_arch = out_s[2] if len(out_s) > 2 else ""
        hostname = out_s[3] if len(out_s) > 3 else ""
        if _SIGTERM_RECEIVED:
            # 循环退出与 emit 之间的窗口（EOF 快速路径/status 等待期）收到信号：
            # 同样归位 interrupted/130，避免 ok:true + 0 吞掉取消语义
            raise KeyboardInterrupt("SIGTERM")

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "test",
            "version": VERSION,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "hostname": hostname,
            "os": os_name,
            "kernel": os_kernel,
            "arch": os_arch,
            "warnings": warnings,
            "duration_ms": duration,
        }
        header = "[OK]  连接成功 %dms  %s@%s" % (duration, conn["user"], hostname or conn["host"])
        sections = [("INFO", json.dumps(result, ensure_ascii=False, indent=2))]
        emit(result, header=header, sections=sections, use_json=args.json)
        return 0
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除阻塞：按中断而非 test_failed 归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "test_failed", str(e),
                   extra=_conn_extra(locals().get("conn")))
        return 255
    finally:
        close_all(client)


def cmd_ls(args):
    start = time.time()  # 计时含连接耗时
    # 仅 None（未指定）才用默认 "."；空串/纯空白交给 _normalize_remote_path
    # 报 bad_args（与 download/upload 的空路径守卫一致，or "." 会把空串也兜掉）
    path = _fix_msys_remote_path(args.path if args.path is not None else ".")

    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠；~user 形式明确报错）
        path = _normalize_remote_path(sftp, path)
        log("[SFTP] 列目录 %s" % path)
        try:
            _sftp_touch_activity(sftp)  # 防慢链路大目录被看门狗误杀（M4）
            entries = sftp.listdir_attr(path)
        except (socket.timeout, TimeoutError):
            raise  # 重抛给外层统一报 ls_timeout（M2：timeout 是 IOError 子类，先拦会吞掉分类）
        except IOError as e:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")  # 交给外层按中断处理（安全帧内）
            if getattr(sftp, "_pssh_watchdog_killed", False):
                raise  # 外层按看门狗分支报 ls_timeout
            if e.errno == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                # OpenSSH 对"不存在"和"不是目录"都返回 SFTP_NO_SUCH_FILE，无法区分；
                # 确实不存在且含通配符时，真实原因是"SFTP 无 glob"而非路径拼错
                if any(c in path for c in "*?["):
                    msg = _remote_glob_error(path)
                else:
                    msg = "远程路径不存在或不是目录: %s" % path
                emit_error(args.json, "bad_args", msg,
                           extra=_conn_extra(locals().get("conn")))
                return 2
            emit_error(args.json, "ls_failed",
                       "无法读取远程路径 %s（可能不是目录或权限不足）: %s" % (path, e),
                       extra=_conn_extra(locals().get("conn")))
            return 1

        entries.sort(key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower()))
        total = len(entries)
        truncated = total > args.limit
        if truncated:
            entries = entries[:args.limit]

        items = []
        lines = []
        names = []
        for e in entries:
            is_dir = stat.S_ISDIR(e.st_mode)
            is_symlink = stat.S_ISLNK(e.st_mode)
            mode = stat.filemode(e.st_mode)
            mtime = int(e.st_mtime)  # epoch 秒（UTC）：AI 跨机比较时间无时区歧义
            # 目录的 st_size 是目录项/inode 尺寸而非内容大小：置 null 防误读
            fsize = None if is_dir else e.st_size
            name = e.filename + ("/" if is_dir else "")  # 目录名带 / 后缀，AI 拼路径时先去尾
            # entries schema 恒定（不随 --long 变化）：--long 只额外打印文本清单行
            items.append({"name": name, "mode": mode, "size": fsize,
                          "is_dir": is_dir, "is_symlink": is_symlink, "mtime": mtime})
            mtime_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
            lines.append("%s %8s %s %s" % (mode,
                                           "-" if fsize is None else str(fsize),
                                           mtime_s, name))
            names.append(name)
        content = "\n".join(lines if args.long else names)

        if truncated:
            content += "\n[pssh] 共 %d 条，仅显示前 %d 条（--limit 调整）" % (total, len(items))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "ls",
            "version": VERSION,
            "path": path,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "count": len(items),
            "total": total,          # 目录内条目总数（截断前）
            "truncated": truncated,  # 超过 --limit 被截断
            "entries": items,
            "warnings": [],          # 与其他子命令 schema 对齐（恒有该键）
            "duration_ms": duration,
        }
        header = "[OK]  %d 项, %dms" % (len(items), duration)
        sections = [("LS", content)]
        emit(result, header=header, sections=sections, use_json=args.json)
        return 0
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "ls_timeout",
                   "SFTP 超时：%d 秒无任何数据（连接可能已被断开）" % SFTP_IO_TIMEOUT,
                   extra=_conn_extra(locals().get("conn")))
        return 1
    except SshError as e:
        # _normalize_remote_path 的 ~user 拒绝等结构化错误：保持 bad_args/2
        # 语义（落到 generic 会被误报 ls_failed/1）
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 1
    except Exception as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        # 看门狗强制断开（服务器静默断链）报 ls_timeout 而非 ls_failed（与 upload/download 一致）
        if "sftp" in locals() and getattr(sftp, "_pssh_watchdog_killed", False):
            emit_error(args.json, "ls_timeout",
                       "SFTP 超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_conn_extra(locals().get("conn")))
        else:
            emit_error(args.json, "ls_failed", str(e),
                       extra=_conn_extra(locals().get("conn")))
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


# =========================================================================
# 参数解析
# =========================================================================

class PsshArgumentParser(argparse.ArgumentParser):
    """参数错误时也在 stdout 输出一行结构化 JSON（AI 可解析）。

    argparse 默认只把 usage/error 打到 stderr，stdout 无任何输出；
    参数写错的 AI 拿不到可解析的错误信息。error() 覆写后 stdout 始终
    有一行 {"ok": false, "error": "bad_args", "message": ...}，退出码仍为 2。
    （与全局一致：默认 JSON；命令行带 --text 时用可读包裹格式。）
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._in_error = False  # 防 parse_known_args 递归进入 error()

    def _want_text_mode(self):
        """判断错误输出是否用 --text 包裹格式。

        不能简单用 `"--text" in sys.argv`：--cmd '--text' 等参数值恰为
        --text 时会误判（argparse 错误输出被切成分段包裹格式，破坏
        "stdout 恒单行 JSON"契约）。改用 parse_known_args 提取真实解析
        结果；parse_known_args 内部再出错（如 --cmd 缺值）会递归调
        error()，由 _in_error 挡板拦截并回退默认 JSON（契约优先）。
        """
        if self._in_error:
            return False
        self._in_error = True
        try:
            ns, _ = self.parse_known_args()
            return not getattr(ns, "json", True)
        except SystemExit:
            return False
        finally:
            self._in_error = False

    def error(self, message):
        if self._in_error:
            # 递归入口（_want_text_mode 的 parse_known_args 内部再出错）：
            # 不打印不输出，由外层 error() 统一打印一次（否则 JSON 会
            # 重复输出两行，破坏"单行 JSON"契约）
            raise SystemExit(2)
        err = {"ok": False, "error": "bad_args", "message": "参数错误: %s" % message}
        if not self._want_text_mode():
            # 默认 JSON 模式：整行 JSON（与成功路径一致，AI 直接 loads）
            print(json.dumps(err, ensure_ascii=False), flush=True)
        else:
            # 可读模式：与 emit_error 一致的 ---ERROR.<nonce>--- 包裹格式
            print("---ERROR.%s---" % _TEXT_NONCE, flush=True)
            print(json.dumps(err, ensure_ascii=False), flush=True)
            print("---END.%s---" % _TEXT_NONCE, flush=True)
        self.print_usage(sys.stderr)
        sys.exit(2)


def _positive_int(value):
    """argparse type：正整数校验（拒绝 0/负值，避免超时参数秒级失效）。
    int() 失败转 ArgumentTypeError：argparse 对 ValueError 只会报
    "invalid _positive_int value"（泄漏内部函数名且无自纠提示）。"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("必须为正整数（收到 %r，如 8）" % (value,))
    if v < 1:
        raise argparse.ArgumentTypeError("必须为正整数（>= 1）")
    return v


def _port(value):
    """argparse type：端口范围校验（1-65535）"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("端口必须为整数（收到 %r，如 22）" % (value,))
    if not 1 <= v <= MAX_PORT:
        raise argparse.ArgumentTypeError("端口必须在 1-%d 之间" % MAX_PORT)
    return v


def _max_time(value):
    """argparse type：--max-time 总时长上限校验（1-1200 秒）"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("总时长上限必须为整数秒（收到 %r，如 600）" % (value,))
    if not 1 <= v <= MAX_TIME_CAP:
        raise argparse.ArgumentTypeError("总时长上限必须在 1-%d 秒之间（构建/编译等长任务最高 %d）"
                                         % (MAX_TIME_CAP, MAX_TIME_CAP))
    return v


def _exec_timeout(value):
    """argparse type：--idle-timeout 静默超时校验（1-1200 秒）。

    上限与 --max-time 一致：静默窗口超过总时长上限时，总上限会先触发并
    把静默挂死误标成"持续输出超时"，两个参数的值域必须对齐。
    """
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("静默超时必须为整数秒（收到 %r，如 60）" % (value,))
    if not 1 <= v <= MAX_TIME_CAP:
        raise argparse.ArgumentTypeError("静默超时必须在 1-%d 秒之间（与 --max-time 上限一致）"
                                         % MAX_TIME_CAP)
    return v


def build_parser():
    parser = PsshArgumentParser(
        prog="pssh",
        description="基于 paramiko 的命令行 SSH 工具（给 AI 用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  pssh exec root@1.2.3.4 --cmd 'uname -a'
  pssh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限（最高 1200）
  pssh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120      # 慢命令调大静默窗口
  pssh exec root@1.2.3.4 --cmd-file - <<'EOF'
  ls -la /var/log
  EOF
  pssh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
  pssh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
  pssh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8   # --local . 可用（scp 语义）
  pssh test root@1.2.3.4
  pssh ls root@1.2.3.4 --path /etc --long --limit 500

跳板机 (--jump):
  pssh exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-password 'xxx' --cmd 'hostname'

主机别名 (.env 配 PSSH_HOST_PROD=root@1.2.3.4:22 后):
  pssh exec @prod --cmd 'uname -a'
  # 别名专属凭据: PSSH_HOST_PROD_PASSWORD / PSSH_HOST_PROD_KEY

路径语义:
  远端路径支持 ~ 与 ~/（自动展开为绝对路径，实际路径回显在结果的 remote 字段）；
  不支持通配符（SFTP 无 glob，请先 ls 拿到明确文件名）。传目录时源目录的
  【内容】放入目标目录下（不额外嵌套）；单文件传到已存在目录 = 放入该目录。

输出: 默认纯 JSON（stdout 单行对象，stderr 为进度日志）；--text 切可读标记模式
      （标记带随机 nonce 防远程输出伪造；AI 程序化解析请一律用默认 JSON）
退出码: 0 成功 / 1 传输错误 / 2 参数错误 / 124 执行超时（远程进程可能仍在运行）/
        130 中断 / 255 连接失败；exec 透传远程退出码（远程恰为 255 时本地返 254）
字段: 结果均含 version/action/duration_ms/warnings；exec 另有 exit_success、
      stdout_bytes(原始接收字节)、stdout_truncated(该流是否截断，省略量见
      stdout_omitted_bytes)；upload/download 的 bytes=清单总大小、
      bytes_transferred=实际传输；ls 的 entries 含 mode/mtime(epoch 秒,UTC)/is_symlink
环境变量 (.env 或系统): PSSH_USER / PSSH_PORT / PSSH_KEY / PSSH_PASSWORD /
                        PSSH_JUMP_KEY / PSSH_JUMP_PASSWORD / PSSH_HOST_<名称>
""",
    )
    parser.add_argument("--version", action="store_true",
                        help="输出版本（stdout 一行 JSON，保持 stdout 恒 JSON 契约）")
    # 默认 JSON 输出；--text 切可读模式。注册顺序有讲究：--text 先注册使默认值
    # (store_false -> True) 生效；--json 仅作兼容入口（显式给出时置回 True）。
    parser.add_argument("--text", dest="json", action="store_false",
                        help="可读文本模式（默认输出纯 JSON 供 AI 精确解析）")
    parser.add_argument("--json", dest="json", action="store_true",
                        help="输出纯 JSON（默认已是 JSON，保留兼容旧用法）")

    sub = parser.add_subparsers(dest="command", required=False, metavar="<子命令>")
    # 子命令不设 required：让 --version 可单独使用；缺子命令时在 main()
    # 给结构化 bad_args（stdout JSON），而非 argparse 的 stderr 纯文本

    def add_conn(p, target_help="目标 [user@]host[:port]"):
        p.add_argument("target", help=target_help)
        p.add_argument("-u", "--user", help="用户名 (env: PSSH_USER)")
        p.add_argument("-p", "--port", type=_port, help="端口 (env: PSSH_PORT, 默认 22)")
        p.add_argument("-k", "--key", help="私钥路径 (env: PSSH_KEY, 默认 ~/.ssh/id_ed25519)")
        p.add_argument("-P", "--password", help="密码 (env: PSSH_PASSWORD)")
        p.add_argument("--timeout", type=_positive_int, default=10, help="连接超时秒数 (默认 10)")
        p.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS,
                       help="输出纯 JSON（默认已是 JSON，保留兼容）")
        p.add_argument("--text", dest="json", action="store_false", default=argparse.SUPPRESS,
                       help="可读文本模式（默认 JSON）")
        p.add_argument("--strict", action="store_true", help="严格校验 host key (默认 auto-add)")
        # 跳板机参数
        p.add_argument("--jump", metavar="[user@]host[:port]",
                       help="跳板机地址，通过它隧道连目标 (如 root@1.2.3.4:2222)")
        p.add_argument("--jump-password", dest="jump_password",
                       help="跳板机密码 (跳板机认证独立于目标机)")
        p.add_argument("--jump-key", dest="jump_key",
                       help="跳板机私钥路径 (默认 ~/.ssh/id_ed25519)")

    # exec
    p = sub.add_parser("exec", help="执行远程命令",
                       description="执行远程命令。本地退出码 = 远程退出码；"
                                   "超时 124、连接失败 255、参数错误 2、中断 130。")
    add_conn(p)
    p.add_argument("--cmd", help="要执行的命令")
    p.add_argument("--cmd-file", dest="cmd_file",
                   help="从文件读命令 (- 表示 stdin，适合长脚本/特殊字符)")
    p.add_argument("--idle-timeout", dest="exec_timeout", type=_exec_timeout, default=60,
                   help="静默超时秒数：连续无输出超过该值即终止，默认 60，最高 1200 "
                        "（区别于 --max-time 总时长；输出少的慢命令调大这个）")
    # 兼容别名：v1.3 前叫 --exec-timeout，名字容易被误当成"总超时"而用错
    p.add_argument("--exec-timeout", dest="exec_timeout", type=_exec_timeout,
                   default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p.add_argument("--max-time", dest="max_time", type=_max_time,
                   help="命令总时长上限秒数（wall clock；默认 2×idle-timeout 且至少 120，"
                        "不得小于 --idle-timeout；构建/编译等长任务请调大，最高 1200）")
    p.add_argument("--max-output", dest="max_output", type=_positive_int, default=DEFAULT_MAX_OUTPUT,
                   help="stdout/stderr 单流最大保留字节，超出保留头尾各一半 (默认 256KB)")
    p.add_argument("--no-credential-warn", dest="no_credential_warn", action="store_true",
                   help="关闭\"命令含疑似凭据\"的 WARN 提示（启发式误报时用；仍建议敏感凭据走环境变量注入）")
    p.add_argument("--spill-dir", dest="spill_dir",
                   help="输出截断时把完整输出落盘的目录（默认系统临时目录；保留的文件路径见结果 "
                        "stdout_spill_file / stderr_spill_file 字段）")
    p.add_argument("--pty", action="store_true",
                   help="分配 PTY 伪终端运行：适用于需要 TTY 的非交互命令"
                        "(watch、top -b、sudo -n、检测 isatty 的脚本)；"
                        "注意 PTY 模式下 stderr 合并进 stdout；vi 等全屏交互程序不可用")
    p.add_argument("--pty-strip-ansi", action="store_true",
                   help="PTY 模式下剥离输出中的 ANSI 转义序列（颜色/光标），供 AI 干净解析")
    p.set_defaults(func=cmd_exec)

    # upload
    p = sub.add_parser("upload", help="上传文件/目录 (本地 -> 远程)",
                       description="上传本地文件或目录到远程。目录自动递归，远程目录自动创建。"
                                   "传目录时源目录的【内容】放入目标目录下（不额外嵌套一层）；"
                                   "单文件传到已存在目录 = 放入该目录（scp 语义）。")
    add_conn(p)
    p.add_argument("--local", required=True, help="本地路径")
    p.add_argument("--remote", required=True,
                   help="远程路径（支持 ~ 展开；不支持通配符）")
    p.add_argument("-r", "--recursive", action="store_true", default=None,
                   help="目录时递归传输 (默认自动递归)")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="对目录不递归：只创建远程目录壳，不传输子项")
    p.add_argument("--dry-run", action="store_true", help="只打印清单不实际传输")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="目标文件已存在且大小一致则跳过（幂等重传，失败重试不重复传）")
    p.set_defaults(func=cmd_upload)

    # download
    p = sub.add_parser("download", help="下载文件/目录 (远程 -> 本地)",
                       description="下载远程文件或目录到本地。目录自动递归，本地目录自动创建。"
                                   "传目录时源目录的【内容】放入目标目录下（不额外嵌套一层）；"
                                   "--local 写已存在目录（如 .）= 文件放入该目录（scp 语义）。")
    add_conn(p)
    p.add_argument("--remote", required=True,
                   help="远程路径（支持 ~ 展开；不支持通配符，请先 ls 拿到明确文件名）")
    p.add_argument("--local", required=True, help="本地路径（已存在目录 = 放入该目录）")
    p.add_argument("-r", "--recursive", action="store_true", default=None,
                   help="目录时递归传输 (默认自动递归)")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="对目录不递归：只创建本地目录壳，不下载子项")
    p.add_argument("--dry-run", action="store_true", help="只打印清单不实际传输")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="本地文件已存在且大小一致则跳过（幂等重下，失败重试不重复传）")
    p.add_argument("--parallel", type=_positive_int, choices=range(1, 9), metavar="1-8",
                   help="大文件分片下载的并行连接数 (默认自动：≥8MB 用 4，实际值见结果 "
                        "parallel_used 字段；跨境高丢包链路可试 8——若 8 失败"
                        "（链路饱和/服务器 MaxStartups 限制）反试 4/2)")
    p.set_defaults(func=cmd_download)

    # test
    p = sub.add_parser("test", help="测试连接",
                       description="测试 SSH 连接，返回连接状态和服务器信息。")
    add_conn(p)
    p.set_defaults(func=cmd_test)

    # ls
    p = sub.add_parser("ls", help="列远程目录",
                       description="列出远程目录内容。上传/下载前可用 ls 确认路径。")
    add_conn(p, target_help="目标 [user@]host[:port]")
    p.add_argument("--path", default=".", help="远程路径 (默认 .；支持 ~ 展开)")
    p.add_argument("-l", "--long", action="store_true",
                   help="额外打印文本清单行（含 mtime；entries 字段两种模式恒同构）")
    p.add_argument("--limit", type=_positive_int, default=2000,
                   help="最多返回条目数 (默认 2000，超出截断并置 truncated=true)")
    p.set_defaults(func=cmd_ls)

    return parser


# =========================================================================
# 入口
# =========================================================================

# SIGTERM 已到达标志：主线程阻塞在 paramiko C 级 I/O（串行 sftp.get/put）时，
# handler raise 的 KeyboardInterrupt 会在 paramiko 展开途中被其 finally 里的
# 新异常（Garbage packet / No existing session 等）覆盖，落到兜底 except 就
# 被误分类成 download_failed/ssh_error。兜底 except 检查本标志可强制归位到
# 中断/计时/单例全局（_SIGTERM_RECEIVED/_INTERRUPT_SOURCE/_MAIN_START/
# _CURRENT_ACTION/_RESPONDER_STARTED）与 _sigterm_handler 已上移至文件顶部
# ——必须在 paramiko 慢 import 之前注册信号 handler（极早期信号窗口，
# 见文件头部注释）。此处保留信号救援线程与注册入口。


def _signal_responder():
    """信号救援线程：置标志后 0.2s 关闭所有活动连接的底层 socket。

    只做裸 sock.close()（不碰任何会拿 paramiko 锁的方法——看门狗死锁正是
    栽在 sftp.close() 的锁上）。解堵后阻塞读以普通异常干净展开，中断走
    正常 except 路径输出 interrupted JSON。关闭幂等，重复几轮兜住分片重建。"""
    deadline = None
    closed_once = False
    while True:
        time.sleep(RESPONDER_TICK)
        if not _SIGTERM_RECEIVED:
            # 空闲自复位：进程内复用（AI 嵌入/测试 harness 同进程多次 main()）
            # 时，上一轮中断的 deadline/closed_once 不残留到下一轮（否则新
            # 一轮中断会跳过 0.2s 缓冲立即强关连接）
            deadline = None
            closed_once = False
            continue
        if deadline is None:
            deadline = time.time() + RESPONDER_GRACE
        if time.time() < deadline:
            continue
        n = 0
        for t in list(_ACTIVE_TRANSPORTS):
            try:
                if t is not None and t.sock is not None:
                    t.sock.close()
                    n += 1
            except Exception:
                pass
        if n and not closed_once:
            closed_once = True
            log("[WARN] 信号中断：已强制断开 %d 条连接解除阻塞" % n)
        time.sleep(RESPONDER_AFTER)


def _setup_signal_handlers():
    try:
        import signal
        signal.signal(signal.SIGTERM, _sigterm_handler)
        # SIGINT（Ctrl+C）与 SIGTERM 同源同险：串行传输中按 Ctrl+C 同样可能
        # 死锁，一并纳入标志+救援（消息文案由 _INTERRUPT_SOURCE 区分）
        signal.signal(signal.SIGINT, _sigterm_handler)
    except (ValueError, OSError, ImportError):
        pass  # 非主线程 / 平台不支持（Windows 下注册无副作用）


def main():
    global _MAIN_START, _RESPONDER_STARTED, _CURRENT_ACTION
    global _SIGTERM_RECEIVED, _INTERRUPT_SOURCE, _FIRST_MAIN_DONE
    _MAIN_START = time.time()
    _setup_console_utf8()
    # 极早期信号（import 阶段，约前 30-400ms）处理：顶层已注册的 handler
    # 置了标志——该窗口内的信号不再走默认动作（rc=143、无 JSON），而是
    # 输出结构化 interrupted JSON + 130。不重置标志（它就是"用户要中断"
    # 的事实）；此时 args 未解析、--text 不可知，按默认 JSON 输出。
    # 仅对【进程内第一次】main() 生效：进程内复用（AI 嵌入/测试 harness）
    # 时后续调用里的标志是上一次中断留下的，必须走正常重置流程，否则
    # 会被误判为"本次极早期信号"（实测 0.00s 即 130 的回归）。
    if _SIGTERM_RECEIVED and not _FIRST_MAIN_DONE:
        _FIRST_MAIN_DONE = True
        emit_error(True, "interrupted", _interrupt_msg())
        return 130
    _FIRST_MAIN_DONE = True
    # 进程内复用（AI 嵌入/测试 harness 同进程多次调 main()）时，上一次调用的
    # 全局状态会污染本次：SIGTERM 标志不复位会让 responder 线程强关新连接
    # （实测中断后同进程后续调用 0.00s 即 interrupted/130 失败）；活动连接
    # 清单与上传残留警告不清会串到本次结果。CLI 每命令一进程，重置无副作用。
    _SIGTERM_RECEIVED = False
    _INTERRUPT_SOURCE = "SIGTERM"
    _CURRENT_ACTION = None
    _ACTIVE_TRANSPORTS.clear()
    _PUT_RESIDUE_WARNINGS.clear()
    _setup_signal_handlers()
    # 信号救援线程：解救 KI 在 paramiko C 级 I/O 中展开导致的死锁/长尾。
    # 只启动一次（单例）：每次 main() 都启动会在进程内复用场景泄漏线程
    # （实测 40 次调用 +40 线程/+85 句柄）；单例 + 标志复位 = 每轮独立生效。
    if not _RESPONDER_STARTED:
        _RESPONDER_STARTED = True
        threading.Thread(target=_signal_responder, daemon=True).start()
    args = None  # 预置：KeyboardInterrupt 可能发生在 parse_args 期间

    try:
        # 整个流程包进 try：parse_args / handler 任一阶段被 Ctrl+C 中断
        # 都能输出结构化 interrupted JSON（argparse 的 SystemExit 是
        # BaseException 不会被这里捕获，--help/--version/参数错误不受影响）
        load_env()  # 也在 try 内：cwd 被删除等场景 os.getcwd() 会抛 OSError
        parser = build_parser()
        args = parser.parse_args()
        if getattr(args, "version", False):
            # --version 也遵守 stdout 恒一行 JSON 的契约（人类直接读也直观）
            print(json.dumps({"ok": True, "action": "version", "version": VERSION},
                             ensure_ascii=False), flush=True)
            return 0
        handler = getattr(args, "func", None)
        if handler is None:
            emit_error(args.json if args is not None else True, "bad_args",
                       "未指定子命令（可选: exec / upload / download / test / ls）")
            parser.print_help(sys.stderr)
            return 2
        # 错误 JSON 的 action 字段：取当前子命令名（供 emit_error 统一填充）
        _CURRENT_ACTION = handler.__name__[4:] \
            if handler.__name__.startswith("cmd_") else handler.__name__
        return handler(args)
    except KeyboardInterrupt as e:
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, e.error_type, str(e))
        return 255
    except Exception as e:
        # 最后防线：任何未预期异常（各子命令内部兜底之外的漏网）也必须
        # 保持"stdout 恒单行 JSON"契约——traceback 只进 stderr 供人类排查，
        # stdout 输出结构化 internal_error 供 AI 解析，绝不裸崩打 traceback
        import traceback
        traceback.print_exc(file=sys.stderr)
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, "internal_error",
                   "内部错误: %s: %s" % (type(e).__name__, e))
        return 255


if __name__ == "__main__":
    rc = main()
    if _SIGTERM_RECEIVED:
        # 信号中断路径硬退出：解释器正常关闭会 join/收割线程，中断展开后
        # 偶发在 finalization 阶段 abort（实测 1/10 轮退出码 134）。JSON 已
        # flush、本地 .part 已清理，跳过关闭阶段保住确定的退出码 130
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(rc)
    sys.exit(rc)
