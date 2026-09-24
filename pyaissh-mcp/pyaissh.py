#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pyaissh - 基于 paramiko 的命令行 SSH 工具（给 AI 用）

工作原理：
    封装 paramiko，提供 exec / upload / download / test / ls 五个子命令。
    默认输出纯 JSON（stdout 整行一个对象，进度日志一律走 stderr），
    AI 直接解析 stdout 即可；--text 切换为「可读 + ---MARKER---」标记模式。
    大输出自动截断（保留头尾），命令有静默/总时长双重超时，SFTP 有 I/O 超时，
    任何路径都不会无限卡住，也不会撑爆调用方上下文。

环境变量（也可写进脚本目录 .env；工作目录 .env 需显式 PYAISSH_ALLOW_CWD_ENV=1 才加载，
          默认不加载以防供应链注入）：
    PYAISSH_USER      默认用户名
    PYAISSH_PORT      默认端口（22）
    PYAISSH_KEY       私钥路径（env 形式，优先级等同 --key，高于 PYAISSH_PASSWORD）
    PYAISSH_PASSWORD  默认密码
    PYAISSH_JUMP_KEY / PYAISSH_JUMP_PASSWORD   跳板机私钥 / 密码（密码未配置时回退 PYAISSH_PASSWORD，v1.4.9）
    PYAISSH_HOST_<名称>=user@host:port      主机别名，target 写 @名称 即可引用；
        可配 PYAISSH_HOST_<名称>_PASSWORD / PYAISSH_HOST_<名称>_KEY 作为该主机专属凭据
    PYAISSH_ALLOW_CWD_ENV=1                 显式允许加载工作目录 .env（默认不加载，
        防止恶意仓库注入 PYAISSH_HOST_* 等把 AI 导向攻击者主机；加载时会打 WARN）
    认证优先级：--key / PYAISSH_KEY > --password / PYAISSH_PASSWORD > 默认私钥 ~/.ssh/id_ed25519
        （别名配置了专属 KEY/PASSWORD 时，该主机不再取全局 PYAISSH_KEY/PYAISSH_PASSWORD）

退出码：
    exec     = 远程命令退出码（超时 124；连接失败 255；远程退出码恰为 255 时本地返回 254 以免混淆）
    其他     成功 0，传输错误 1，参数错误 2，超时 124，中断 130，连接错误 255

安全提示：
    1. --password / --jump-password 会出现在进程参数里（本地 ps 可见），
       敏感场景请优先用密钥认证，或改用环境变量 PYAISSH_PASSWORD / .env。
    2. exec 的命令原文会打印到 stderr 日志与结果 JSON，含凭据的命令（如 mysql -p'xxx'、
       curl -u user:pass）会留在记录里；建议敏感凭据用远程环境变量注入。

用法示例：
    pyaissh exec root@1.2.3.4 --cmd 'uname -a'
    pyaissh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限
    pyaissh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120       # 静默超时（连续无输出的窗口）
    pyaissh exec @prod --cmd 'uname -a'                            # 主机别名（.env 配 PYAISSH_HOST_PROD）
    pyaissh exec root@1.2.3.4 --pty --cmd 'tty'                    # 分配 PTY（需要 TTY 的非交互命令）
    pyaissh exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'top -b -n 1'  # PTY + 剥离 ANSI 供 AI 解析
    pyaissh exec root@1.2.3.4 --cmd-file - <<'EOF'
    ls -la /var/log
    EOF
    pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
    pyaissh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
    pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8  # 高丢包链路提速
    pyaissh test root@1.2.3.4
    pyaissh ls root@1.2.3.4 --path /etc --long

    # 通过跳板机连接（跳板用密码，目标用密钥）
    pyaissh exec root@10.0.0.5 --jump root@156.233.234.206:22024 \
        --jump-password 'xxx' --cmd 'hostname'

路径语义：
    远端路径支持 ~ 与 ~/（SFTP 协议本身不展开，pyaissh 自动转换为绝对路径，
    实际路径回显在结果的 remote/path 字段）；不支持通配符（SFTP 无 glob，
    请先 ls 拿到明确文件名）。传目录时源目录的【内容】放入目标目录下
    （不额外嵌套一层）；单文件传到已存在的目录 = 放入该目录（scp 语义）。
"""

import argparse
import base64
import codecs
import errno
import fnmatch
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

# 注意：paramiko 不在此 import——v1.5.5 起惰性化，只在真正建连（_do_connect）
# 时才 import。错误路径（--version/--help/bad_args/缺用户名/别名未配置）从
# ~300ms 降到 ~30ms；极早期信号窗口也更短（handler 注册后只剩标准库 import）。


# ================= [域 01/13] 全局常量与可变状态容器 ================= 
"""全局常量与可变状态容器（域 01）。

- 超时/轮询/缓冲上限常量（MAX_TIME_CAP / PARALLEL_MIN_SIZE / SFTP_IO_TIMEOUT ...）
- _RETRYABLE_ERRORS：错误类型 -> 是否可重试（emit_error 用它给 retryable 字段）
- 模块级可变容器：_ACTIVE_TRANSPORTS（活动连接）、_PUT_RESIDUE_WARNINGS（.part 残留警告）
被 00_head（信号区）、各 cmd_*（超时/常量）引用；拼接后与本包其余域同模块共享命名空间。
"""

VERSION = "2.3.0"

# =========================================================================
# 代码地图（维护用）：改功能 → 按区域定位函数（grep 函数名即得；不写行号，
# 行号随编辑漂移）→ 行为细节见对应 docs 子文档。
#   区域            关键函数                                对应文档
#   信号/入口        _sigterm_handler / _signal_responder /   docs/errors.md（退出码 130）
#                   _setup_signal_handlers / main             docs/edge-cases.md（极早期信号窗口）
#   配置加载         _parse_env_file / load_env /             docs/setup.md
#                   parse_target / _alias_env /
#                   resolve_conn / resolve_jump
#   路径             _fix_msys_remote_path /                 docs/setup.md
#                   _fix_msys_local_path /
#                   _sftp_home / _normalize_remote_path
#   输出层           log / emit / emit_error /                docs/contract.md
#                   _truncate_output / _utf8_boundary_cut /
#                   _sanitize_log_text / _strip_ansi /
#                   _clean_pty_text / warn_sensitive_cmd
#   异常类型         SshError / ExecIdleTimeout /             docs/errors.md
#                   ExecTotalTimeout
#   连接层           connect / _do_connect /                  docs/setup.md（凭据/host key）
#                   _host_key_known / _AtomicAutoAddPolicy    docs/edge-cases.md（known_hosts 原子写）
#   SFTP 传输层      open_sftp / _sftp_watchdog /             docs/transfer.md
#                   _parallel_fetch / _sftp_put_atomic /
#                   _atomic_local_write / sftp_makedirs /
#                   sftp_walk / _remote_size_is / _part_path
#   exec            cmd_exec（读线程 _read / _drain_rest）    docs/exec.md
#   upload/download cmd_upload / cmd_download /               docs/transfer.md
#                   _win_safe_rel_path
#   test / ls       cmd_test / cmd_ls                         SKILL.md「子命令速览」
#   参数解析         build_parser / PsshArgumentParser /      SKILL.md「参数默认值」
#                   _port / _max_time / _exec_timeout
#   常量/正则        _CONSTANTS（本文件顶部）                 调参只改此处
# =========================================================================

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
DEFAULT_MAX_OUTPUT = 65536   # exec 单流默认最大保留字节（v2.2 由 256KB 降为 64KB）
#   理由：结果 JSON 过大时宿主会裁剪工具结果中段（[... tool result middle pruned ...]），
#   连 spill 路径都可能一起被裁掉；64KB 已覆盖多数命令全文，超出的完整输出本来就
#   落在 spill 文件里（路径回传 stdout_spill_file/stderr_spill_file，读文件比重跑便宜）
# --- 后台作业（v2.2：exec --detach 启动 + log 子命令读取）-------------------
DEFAULT_JOB_DIR = "/tmp/pyaissh-jobs"  # 远端作业根目录（每作业一个子目录）
JOB_TAIL_WINDOW = 1048576    # log 取尾部时的最大回看字节窗口（1MB，防大日志全量入内存）
JOB_WAIT_MAX = 600           # --wait-rc 上限秒数（宿主单次调用上限约 600s）
JOB_RC_GRACE = 0.6           # 判 dead 前给 job.rc 落盘的宽限（防"刚结束被误判 dead"，v2.3.0）
BUF_ALIGN_WINDOW = 4096      # 截断行对齐时回退搜索窗口（字节）
MIN_BUF_FLOOR = 4096         # 内存缓冲下限：max(args.max_output, 4096) 保证小档位也有可用缓冲

# --- 常驻会话（v2.3：session 子命令 —— 真 PTY + 逐条喂命令 + 状态保留）------
DEFAULT_SESSION_DIR = "/tmp/pyaissh-sessions"  # 远端会话根目录（每会话一个子目录）
SESSION_TAIL_WINDOW = 1048576    # read 取尾部时的最大回看字节窗口（1MB，与 log 同款）
SESSION_WAIT_MAX = 600           # --wait-rc 上限秒数
SESSION_READY_WAIT = 8           # start 后等会话就绪（哨兵）的默认秒数
SESSION_DEFAULT_LINES = 100      # read 默认尾部行数
SESSION_RC_PREFIX = "__PYAISSH_RC__"   # 每条命令的退出码哨兵前缀（<prefix><token>__<rc>）
SESSION_RUN_WAIT = 60            # session run 默认等待秒数（send + 等结束合成一次调用）
JOIN_GRACE = 1.5             # 读线程 join 宽限（秒）
RETRY_SLEEP = 0.5            # Windows 句柄未释放等场景的删除重试等待
PUT_RETRY_SLEEP = 0.3        # 远端 .part 清理重试等待
CYGPATH_TIMEOUT = 5          # cygpath 子进程超时（MSYS 路径转换，本地工具不应挂死）
RESUME_MIN_SIZE = 50 * 1024 * 1024   # 单文件 ≥ 此大小且未用 --resume 时提示推荐启用
                             # （断点续传收益与文件大小/链路速度相关，做成常量可调）
CMD_ECHO_LIMIT = 8192        # 结果 JSON 的 cmd 字段回显上限：--cmd-file 读入的大脚本
                             # 原样回显会让单行 JSON 到 MB 级撑爆调用方上下文；超限保留头尾
# 错误类型的重试建议（emit_error 统一带出 retryable 字段，机器可读的重试决策）：
#   True  = 同类错误重试可能成功且重试本身安全（网络/传输类）。exec 超时类
#           retryable=True 仅表示"值得一试"——远程进程可能仍在运行/命令可能有
#           副作用，重试前必须读 message 的"远程进程可能仍在运行"提示并先
#           pgrep 确认/清理（bool 给机器"值不值得试"，message 给人"怎么试才安全"）。
#   False = 重试无意义或需先改输入（凭据/参数/本地文件/命令本身失败）。
# 未列出的类型（jump_failed 等混合原因类）默认 False（保守，message 说明具体原因）。
_RETRYABLE_ERRORS = {
    "connection_timeout", "connection_refused", "connection_failed", "dns_failed",
    "connection_lost", "interrupted",
    # 所有 SFTP 传输/超时类统一 true（upload/download/ls 的 _failed/_timeout 同性质：
    # 操作目标资源时的网络/通道问题，重试可能成功，message 说明具体原因）
    "upload_failed", "download_failed", "upload_timeout", "download_timeout",
    "ls_failed", "ls_timeout",
    # test 连接成功后的通道/传输异常兜底（test 只跑系统查询，命令本身几乎不会
    # 失败；信号中断已单独归 interrupted）——与 connection_lost 同类，可重试
    "test_failed",
    "exec_idle_timeout", "exec_total_timeout", "exec_timeout",
}


# ================= [域 02/13] 凭据启发式正则 ================= 
"""凭据启发式正则（域 02）——"疑似凭据"WARN 的判定源。

- 模式全家 _P_SENS_*（password=/--password/-p'xxx'/-psecret/-p secret/URL 凭据/env 凭据/mysql/curl）
- 统一防线 _P_LOOKBEHIND_P（(?<![A-Za-z0-9-])-p：词中 -p 全排除，1.5.17 修 --no-pager）
- _SENSITIVE_CMD_RE 组合入口 + 验收注释矩阵（改模式必过矩阵）
- _ANSI_RE / _RE_WIN_ILLEGAL
被 05_util.warn_sensitive_cmd 调用（凭据 WARN 落在 log/结果）。
"""

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
_P_SENS_PASSWORD = (r"passw[o0]?rd\s*[=:]\s*(?!-)(?![\"']{2}(?:\s|$))\S+"
                     r"|--password(?:\s+|=)(?!-)(?![\"']{2}(?:\s|$))\S+")
_P_SENS_USER = r"--user\s+\S+:\S+"          # curl --user admin:pw 长形式
_P_SENS_URL = r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"   # https://user:pass@host/
# DB_PASS=x / DB_PASS: x / MYSQL_PWD=；v2.1.4：(?!-) 排除赋值后跟 -参数
# （java -Dspring.datasource.password= -jar 实测误报——password= 空、-jar 是下个参数）；
# 空引号对排除（DB_PASS='' 空值无秘密）
_P_SENS_ENV = (r"\b\w*(?:PASS(?:WORD|WD|CODE)?|PWD)\s*[=:]\s*(?!-)(?![\"']{2}(?:\s|$))\S+")
# 统一防线 (?<![A-Za-z0-9-])：-p 作为密码选项时前字符必为空白/行首/引号，
# 绝不可能是字母/数字/连字符——连字符复合词中间的 -p（--no-pager、a-px）与
# 词内 -p 全部排除（1.5.16 修：原 (?<!-) 只挡双横线开头，挡不住 no-pager 的 -p）。
# v2.1.4：(?-i:-p) 局部区分大小写——-p 密码选项恒小写；-P（grep -P 等）是
# Perl/端口 flag 不命中（实测误报 "grep -P 'a+b' file" 因 (?i) 吞了大写）。
_P_LOOKBEHIND_P = r"(?<![A-Za-z0-9-])(?-i:-p)"
_P_SENS_P_QUOTED = _P_LOOKBEHIND_P + r"['\"](?!\d+['\"])[^'\"]+['\"]"   # -p'secret'（排除 -p'22' 纯数字端口/ID）
# -psecret 紧贴形态（-p 后必须非空白，空格形态交给 _P_SENS_P_SPACE）：
# 前缀 lookbehind 排除常见非密码工具（scp/rsync/curl/make/install/find/perl/echo/unzip/gcc/xargs/awk；
# v2.1.4 +ffmpeg/ffprobe——-pix_fmt 实测误报）
_P_SENS_P_ATTACH = (
    r"(?<!scp )(?<!rsync )(?<!curl )(?<!make )(?<!install )"
    r"(?<!find )(?<!perl )(?<!echo )(?<!unzip )(?<!gcc )(?<!xargs )(?<!awk )"
    r"(?<!ffmpeg )(?<!ffprobe )"
    + _P_LOOKBEHIND_P
    + r"(?!['\"]?\d+(?:['\"]|\b))(?!\s)"
    r"(?!rin|rune|thread|pe\b|roxy|ort|ath|ass|lain)\S+"
)
# -p secret（空格分隔）：lookbehind 排除常见非密码工具（cp/mkdir/ls/tar/scp/rsync/curl/
# make/install/unzip/pytest/awk/xargs/wget，覆盖单/双空格；v2.1.4 +grep/egrep/fgrep——
# 实测误报 "grep -p foo /etc/passwd"）；(?!--)/(?!-) 排除 -p 后跟选项；
# 词表排除选项名、工具参数与协议名；[^\s/]+ 排除路径类参数（rsync -p /x、mkdir -p a/b 的兜底）；
# (?!["']{2}...) 排除空引号值（useradd -p '' 实测误报——空值无秘密）
_P_SENS_P_SPACE = (
    r"(?<!cp )(?<!cp  )(?<!ls )(?<!ls  )(?<!tar )(?<!tar  )(?<!scp )(?<!scp  )"
    r"(?<!mkdir )(?<!mkdir  )(?<!rsync )(?<!rsync  )(?<!curl )(?<!curl  )"
    r"(?<!make )(?<!make  )(?<!install )(?<!install  )"
    r"(?<!unzip )(?<!unzip  )(?<!pytest )(?<!pytest  )(?<!awk )(?<!awk  )"
    r"(?<!xargs )(?<!xargs  )(?<!wget )(?<!wget  )"
    r"(?<!grep )(?<!grep  )(?<!egrep )(?<!egrep  )(?<!fgrep )(?<!fgrep  )"
    + _P_LOOKBEHIND_P
    + r"\s+(?!\d+\b)(?!--)(?!-)(?![\"']{2}(?:\s|$))"
    r"(?!proxy\b|roxy\b|port\b|path\b|pass\b|plain\b|"
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

# "从文件读值"形态（v2.1 豁免：$(cat f) / $(<f)）——凭据不进命令行文本，
# 日志无明文可泄，warn_sensitive_cmd 对含此形态的命令整条放行（实测误报：
# DB_PASS=$(cat /srv/x)、export PASS=$(cat /tmp/p)、mysql -p $(cat f)）。
_READ_FROM_FILE_RE = re.compile(r"\$\(\s*(?:cat\b|<)")

# ---- 验收案例（改 _P_SENS_* 片段必对照自查；完整矩阵见开发机 verify_r3 L4）----
# 应命中（疑似凭据）：
#   -psecret / -p secret / -p'xxx' / -p"xxx"       紧贴与空格形态
#   --password=abc / --password abc / password=abc 长选项与赋值
#   mysql -u root -p secret / curl -u user:pass    工具专有形态
#   https://user:pass@host/ / DB_PASS=abc / MYSQL_PWD=abc  URL/环境变量
# 不应命中（工具 flag/端口/路径，历史误报点）：
#   --profile x / --parallel 4 / --progress        双横线长选项（1.5.0 修）
#   --no-pager / --no-color / --dry-run           复合长选项词中 -p（1.5.16 修：
#     (?<![A-Za-z0-9-]) 统一防线——-p 密码选项前必空白/行首，词中/复合词 -p 全挡）
#   -p 22 / -p'22' / -p123456 / ssh -p 22 root@h   纯数字端口/ID
#   mkdir -p a/b / tar -p x / cp -p a b / gcc -pthread  工具 -p
#   rsync -p /x / wget -p https://... / pytest -p x     路径/参数
#   echo -pabc / git push / --port 22              其他
# 注意：_P_SENS_P_ATTACH 与 _P_SENS_P_SPACE 的排除表（cp/mkdir/tar/...）
# 靠 lookbehind 前缀精确匹配——改排除表时上面每条都要重新过一遍。

_ANSI_RE = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b.")



# ================= [域 03/13] 配置与路径层 ================= 
"""配置与路径层（域 03）。

- .env 解析/加载（load_env：技能目录 .env 自动加载、工作目录需 PYAISSH_ALLOW_CWD_ENV=1）
- MSYS/Git Bash 路径修复：_fix_msys_local_path / _fix_msys_remote_path（含 ~ 展开逆转）
- 远端路径规范化：_normalize_remote_path（~ 展开 / 去尾斜杠 / glob 拒绝）
被 06_conn（解析）、各 cmd_*（--local/--remote/--cmd-file 路径）调用。
"""

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

    供应链安全设计：**默认只加载脚本目录的 .env**（用户主动放入 pyaissh
    工具目录、自己可控的文件）。工作目录（cwd）的 .env 默认【不】加载——
    恶意仓库可自带 .env 注入 PYAISSH_HOST_* / PYAISSH_PASSWORD 等变量，把 AI
    的 SSH 连接导向攻击者主机（钓鱼 SSH）。仅当显式设置
    PYAISSH_ALLOW_CWD_ENV=1（环境变量或脚本目录 .env 中）时才加载 cwd .env，
    并打 WARN 提示供应链风险；未开启但存在 cwd .env 时也打 WARN 提醒。
    """
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(script_env):
        _parse_env_file(script_env)

    cwd_env = os.path.join(os.getcwd(), ".env")
    if cwd_env == script_env or not os.path.isfile(cwd_env):
        return
    if os.environ.get("PYAISSH_ALLOW_CWD_ENV") == "1":
        log("[WARN] 正在从工作目录加载 .env（供应链风险，因 PYAISSH_ALLOW_CWD_ENV=1 已显式开启）: %s" % cwd_env)
        _parse_env_file(cwd_env)
    else:
        global _CWD_ENV_SKIPPED
        _CWD_ENV_SKIPPED = True
        log("[WARN] 检测到工作目录 .env 但未加载（供应链风险：防止恶意仓库注入 PYAISSH_HOST_* 等"
            "导向攻击者主机；如确需使用请设 PYAISSH_ALLOW_CWD_ENV=1）: %s" % cwd_env)


def _fix_msys_remote_path(path):
    """修复被 Git Bash/MSYS 路径转换破坏的远程路径。

    场景：Git Bash 在没有 MSYS_NO_PATHCONV=1 时，会把 Unix 绝对路径
    （如 /tmp/xxx、/root/xxx、/opt/app）自动转成 Windows 路径，
    传给 pyaissh.py 后导致 SFTP 拿到错误路径。
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
        log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pyaissh 命令而非 python pyaissh.py)" % (path, recovered))
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
                log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pyaissh 命令而非 python pyaissh.py)" % (path, recovered))
                return recovered
    except Exception:
        pass

    # 模式 3：MSYS ~ 展开转换 C:/Users/<user>/xxx -> ~/xxx
    # Git Bash 把 ~ 展开为 /c/Users/<user> 再经 pathconv 变 C:/Users/<user>：
    # --remote 的 ~ 语义是"远端用户 home"，应由 _normalize_remote_path 在远端
    # 展开；此处还原成 ~/xxx，避免在远端创建 /C:/Users/<user>/ 垃圾目录树。
    # 仅当 <user> 匹配本地 Windows 用户名（MSYS home 的典型形态），
    # 远端 Windows 服务器上的 C:/Users/<其他名>/... 不受影响。
    local_user = os.environ.get("USERNAME") or os.environ.get("USER")
    if local_user:
        home_prefix = "C:/Users/%s" % local_user
        if path.startswith(home_prefix + "/"):
            rest = path[len(home_prefix):]
            recovered = "~" + rest
            log("[WARN] MSYS ~ 转换检测: %s → %s (建议给 ~ 加引号或设 MSYS_NO_PATHCONV=1)" % (path, recovered))
            return recovered

    return path


def _sftp_home(sftp):
    """取远端用户 home（SFTP 会话起始目录），缓存在会话上避免重复往返。"""
    home = getattr(sftp, "_pyaissh_home", None)
    if home is None:
        home = sftp.normalize(".")
        sftp._pyaissh_home = home
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
            "请先 pyaissh ls 列出目录拿到明确文件名，再逐个传输" % path)


def _fix_msys_local_path(path):
    """Git Bash 经 ./pyaissh 包装器（MSYS_NO_PATHCONV=1）运行时，把 /tmp/... 这类
    Unix 风格【本地】路径转换成真实 Windows 路径。

    背景：包装器禁用了 MSYS 路径转换后，--local /tmp/x 会原样到达 Windows Python，
    被解析成当前盘根 D:\\tmp\\x——shell 视角路径明明存在却报"不存在"，下载方向
    更会写错位置。直接 python pyaissh.py 运行时 MSYS 已提前转换，路径到这儿已是
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


# ================= [域 04/13] 控制台与输出层 ================= 
"""控制台与输出层（域 04）。

- _setup_console_utf8()：Windows 控制台 UTF-8（模块级调用 + main 幂等）
- _QUIET / log()：进度日志（stderr；--field 静音模式只放 [WARN] 信号，1.5.19）
- 结果输出：emit（JSON/--text）、_emit_result（--field 分流）、_emit_fields（字段提取+
  stderr 盲区提示）、emit_error（错误完整 JSON）
- 异常基类 SshError / ExecIdleTimeout / ExecTotalTimeout
被所有 cmd_*（输出/报错）调用；log/emit 的语义契约见 SKILL.md 输出约定。
"""

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


# 模块级立即生效（不只在 main()）：import 路径（`python -c "import pyaissh"`、
# AI 嵌入、测试 harness）下 stdout/stderr 也保证 UTF-8，否则管道捕获时
# 中文日志（WARN 等）按本地代码页（GBK）写出会被 UTF-8 解码成乱码。
# main() 里再调一次是幂等兜底（脚本路径双跑无副作用）。
_setup_console_utf8()


# --field 消费端模式的进度日志静音开关：--field 消费者只要字段裸值 + 信号，
# [SSH]/[OK]/[EXEC] 进度行是噪音（v1.5.19：消费者被噪音烦到 2>/dev/null，
# 把 stderr 盲区提示一起静音——死结；静音噪音后 stderr 只剩信号，屏蔽动机消失）。
# 置位点：main() 解析出 args.field 后（见 main）。
_QUIET = False


def log(msg, force=False):
    """进度日志，打到 stderr（两种模式都打），不污染 stdout。
    --field 静音模式下只保留 [WARN] 级信号（凭据警告等），丢进度行。
    force=True 时绕过 _QUIET（v2.1：--progress 心跳是显式请求的信号——
    长任务 + --field stdout 恰是最需要心跳的场景，不该被噪音静音吞掉）。"""
    if _QUIET and not force and not msg.startswith("[WARN]"):
        return
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


def _emit_fields(result, field_spec):
    """--field 消费端字段提取（成功路径）：打印顶层字段值，不打印 JSON。

    - 无 - 前缀字段 -> 打进程 stdout（单字段裸值；多字段每行一个值）
    - - 前缀字段（如 -stderr）-> 打进程 stderr（调用方 2>&1 或单独流可见，
      不被 stdout 展示脚本吞掉——实测教训：AI 只读 stdout 字段丢了 stderr 报错）
    - dict/list 值 JSON 序列化（如 ls 的 entries、upload 的 file_list）
    - 值本身多行（如 stdout 内容）原样保留
    - 字段不存在（拼错）-> stderr 提示字段名（不静默空行误导）
    - 结果 stderr 字段非空且本次未提取 stderr（未给 stderr/-stderr）-> 自动在
      进程 stderr 打提示（A 升级：--field stdout 吞 stderr 教训第三次应验——
      传输失败真实原因在 stderr 里被吞，多烧一轮排查；提示走进程 stderr 不
      污染 stdout 裸值，只读 stdout 的消费者也能察觉有 stderr 值得看）
    错误路径不走这里（emit_error 保持完整 JSON，AI 需要 retryable/message）。"""
    names = [s.strip().lstrip("-") for s in field_spec.split(",") if s.strip()]
    for spec in field_spec.split(","):
        spec = spec.strip()
        if not spec:
            continue
        to_stderr = spec.startswith("-")
        name = spec[1:] if to_stderr else spec
        if name not in result:
            print("[pyaissh: 字段不存在: %s（可用字段见默认 JSON 输出的键名）]" % name,
                  file=sys.stderr, flush=True)
            continue
        value = result[name]
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        elif value is None:
            text = ""
        else:
            text = str(value)
        if to_stderr:
            print(text, file=sys.stderr, flush=True)
        else:
            print(text, flush=True)
    # stderr 盲区处理（v2.1 升级：命令失败直接给内容，不再让 AI 多跑一轮取 stderr）
    err_val = result.get("stderr")
    if err_val is not None and str(err_val).strip() and "stderr" not in names:
        err_s = str(err_val)
        rc = result.get("exit_code")
        failed = result.get("ok") is False or (rc not in (0, None))
        if failed:
            # 失败路径：直接打 stderr 尾巴（1KB 封顶，报错通常在尾部）——AI 一次
            # 往返拿到真实报错（实测教训：pip 装依赖失败只给提示要多烧一轮真金白银）
            tail = err_s if len(err_s) <= 1024 else "…" + err_s[-1024:]
            print("[pyaissh: 命令失败(exit_code=%s) 且本次未提取 stderr——"
                  "stderr 尾巴%s（完整内容用 -stderr 字段: --field stdout,-stderr）:\n%s]"
                  % (rc, "" if len(err_s) <= 1024 else "（仅尾部 1KB）", tail),
                  file=sys.stderr, flush=True)
        else:
            print("[pyaissh: 结果含非空 stderr（%d 字节）——本次 --field 未提取 stderr，"
                  "真实报错可能在其中；用 -stderr 字段（--field stdout,-stderr）查看]"
                  % len(err_s), file=sys.stderr, flush=True)


def _emit_result(args, result, header=None, sections=None):
    """统一结果输出：--field 消费端模式打印字段；否则标准 emit（JSON / --text）。"""
    field = getattr(args, "field", None)
    if field:
        _emit_fields(result, field)
    else:
        emit(result, header=header, sections=sections,
             use_json=getattr(args, "json", True))


def emit_error(use_json, error_type, message, extra=None):
    """错误输出到 stdout（不返回退出码，由调用方自行 return）。

    打印期间屏蔽 KeyboardInterrupt：中断处理路径里再被信号打断会撕裂
    单行 JSON 输出（部分写入 + 上层再打一行），破坏"单行单对象"契约。
    错误对象恒含 version/action/duration_ms/warnings（与成功结果一致，
    help 契约"结果均含"）：action 取当前子命令（解析前/未知为 None），
    duration_ms 自 main() 启动计时，warnings 为空列表（extra 可覆盖）。
    """
    err = {"ok": False, "error": error_type, "message": message,
           "retryable": error_type in _RETRYABLE_ERRORS,
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


# ================= [域 05/13] 通用工具与字符串/缓冲处理 ================= 
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


def _normalize_cmd_newlines(text):
    """命令文本行尾归一：CRLF / 孤立 CR → LF，返回 (归一后文本, 归一处的行尾数)。

    v2.2.4：Windows 工具（记事本 / VS Code / PowerShell 重定向 / here-string）写出的
    命令文件或内联命令，行尾是 \\r\\n；远端 bash 把 \\r 当词的一部分，典型症状
    "$'\\r': command not found"、判断/关键字行报语法错、heredoc 落盘的文件每行带 CR。

    注：`--cmd-file` 此前**靠 Python 文本模式的 universal newlines 隐式归一**（副作用，
    代码里没写、也无法关闭）；本函数把它变成显式、可计数、可用 --keep-crlf 关闭的行为。
    """
    n_crlf = text.count("\r\n")
    out = text.replace("\r\n", "\n")
    n_cr = out.count("\r")
    if n_cr:
        out = out.replace("\r", "\n")
    return out, n_crlf + n_cr


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



# ================= [域 06/13] 连接层：目标解析 -> 认证 -> 建连 ================= 
"""连接层（域 06）：目标解析 -> 认证 -> 建连。

- parse_target（[user@]host[:port]/IPv6/别名 @名称）、_alias_env
- resolve_conn / resolve_jump（凭据优先级：参数 > env > 默认私钥；跳板回落）
- _do_connect（paramiko 惰性 import + host key AutoAddPolicy 缓存）、connect / close_all
被 cmd_* 的连接段（编排 cmd_* 调 resolve_conn/connect）调用。
"""

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
    """查主机别名环境变量 PYAISSH_HOST_<别名><suffix>（键名整体大小写不敏感）。

    先按原样/全大写两种键直查，再对整个键做大小写归一扫描：Linux 的
    os.environ 严格区分大小写（.env 写全小写 pssh_host_prod 也要能命中），
    Windows 本身不区分。返回 None 表示未配置。
    """
    for a in dict.fromkeys([alias, alias.upper()]):
        v = os.environ.get("PYAISSH_HOST_%s%s" % (a, suffix))
        if v:
            return v
    want = ("PYAISSH_HOST_%s%s" % (alias, suffix)).upper()
    for k, v in os.environ.items():
        if k.upper() == want:
            return v
    return None


def resolve_conn(args):
    """合并 target / env / 默认值，返回连接参数 dict"""
    if not args.target:
        raise SshError("未指定目标主机（target）", "bad_args")
    # 主机别名：target 写 @名称，从 PYAISSH_HOST_<名称> 展开（.env 可配）
    alias = None
    target = args.target
    if target.startswith("@"):
        alias = target[1:].strip()
        if not alias:
            raise SshError("主机别名格式应为 @名称", "bad_args")
        val = _alias_env(alias)
        if not val:
            msg = "未配置主机别名 @%s：请在 .env 写 PYAISSH_HOST_%s=user@host:port" % (alias, alias.upper())
            if _CWD_ENV_SKIPPED:
                # 只看 stdout 的 AI 需要"为什么我写了 .env 还是找不到"的答案：
                # 工作目录 .env 默认不加载（供应链防护），真实原因此前只在 stderr
                msg += ("（检测到工作目录 .env 但默认不加载——防恶意仓库注入；如需启用设 "
                        "PYAISSH_ALLOW_CWD_ENV=1，或把 .env 放到 pyaissh 脚本目录）")
            raise SshError(msg, "bad_args")
        log("[ALIAS] @%s -> %s" % (alias, val))
        target = val
    t_user, t_host, t_port = parse_target(target)

    user = t_user or args.user or os.environ.get("PYAISSH_USER")
    host = t_host
    # 显式 -p 优先于 target/别名内嵌端口（与 ssh/scp 惯例一致：命令行显式参数最优先，
    # 用户写 -p 通常就是想纠正 target 里的端口）
    port = args.port if args.port is not None else t_port
    if port is None:
        port = _safe_int(os.environ.get("PYAISSH_PORT"), 22, "PYAISSH_PORT")
    if not port or not 1 <= port <= MAX_PORT:
        # 命令行 --port 已由 argparse 校验（1-65535）；这里只管 env 与 target
        # 内嵌端口：0/负值/越界/非数字一律回退默认 22 并打 WARN
        log("[WARN] 端口 %r 超出范围 (1-%d)，回退默认 22" % (port, MAX_PORT))
        port = 22
    # 凭据优先级：显式参数 > 别名专属（PYAISSH_HOST_<名称>_KEY/_PASSWORD）> 全局 env。
    # 别名配置了任一专属凭据时抑制全局 env：否则"别名只配密码 + 全局 PYAISSH_KEY"
    # 会优先拿全局 key 去认证，key 不匹配时直接 auth_failed，别名密码永远轮不到；
    # 别名主机的凭据应完全由别名决定（显式命令行参数仍最高优先）
    alias_key = _alias_env(alias, "_KEY") if alias else None
    alias_pw = _alias_env(alias, "_PASSWORD") if alias else None
    if alias and (alias_key or alias_pw):
        key = args.key or alias_key
        password = args.password or alias_pw
    else:
        key = args.key or alias_key or os.environ.get("PYAISSH_KEY")  # None 表示用默认密钥
        password = args.password or alias_pw or os.environ.get("PYAISSH_PASSWORD")

    if not user:
        raise SshError("未指定用户名：请在 target 写 user@host 或用 -u / PYAISSH_USER", "bad_args")
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
                or os.environ.get("PYAISSH_JUMP_PASSWORD") or os.environ.get("PYAISSH_JUMP_KEY")):
            log("[WARN] 指定了跳板凭据（--jump-password/--jump-key/PYAISSH_JUMP_*）但未提供 --jump，已忽略")
        return None
    j_user = None
    j_alias = None
    jump_target = args.jump
    if jump_target.startswith("@"):
        # 跳板机也支持 @别名（与 target 同一套 PYAISSH_HOST_* 配置）。
        # 不展开的话 "@bastion" 会被当字面主机名去连，报 DNS 失败极具误导性
        j_alias = jump_target[1:].strip()
        if not j_alias:
            raise SshError("跳板机别名格式应为 @名称", "bad_args")
        val = _alias_env(j_alias)
        if not val:
            raise SshError("未配置跳板机别名 @%s：请在 .env 写 PYAISSH_HOST_%s=user@host:port"
                           % (j_alias, j_alias.upper()), "bad_args")
        log("[ALIAS] 跳板 @%s -> %s" % (j_alias, val))
        jump_target = val
    j_user, j_host, j_port = parse_target(jump_target)
    # 跳板机用户：优先 --jump 字串里的，其次复用目标用户
    # （target 可能是别名 @name，原始字符串解析不出 user，用已解析的 target_user 回退）
    if not j_user:
        j_user = target_user or args.user or os.environ.get("PYAISSH_USER")
    if not j_user:
        raise SshError("跳板机未指定用户名：请在 --jump 写 user@host", "bad_args")
    if not j_host:
        raise SshError("跳板机未指定主机", "bad_args")
    j_port = j_port or 22  # parse_target 已保证 1-65535（越界直接 bad_args），这里只补未指定端口
    # 跳板凭据优先级：显式参数 > 别名专属 > PYAISSH_JUMP_*（别名复用 PYAISSH_HOST_* 的
    # 专属凭据键：同一台机器当 target 和当跳板通常用同一套凭据）。
    # 与 resolve_conn 同规则：别名配了专属凭据时抑制全局 env
    alias_key = _alias_env(j_alias, "_KEY") if j_alias else None
    alias_pw = _alias_env(j_alias, "_PASSWORD") if j_alias else None
    if j_alias and (alias_key or alias_pw):
        key = args.jump_key or alias_key
        password = args.jump_password or alias_pw
    else:
        key = args.jump_key or alias_key or os.environ.get("PYAISSH_JUMP_KEY")  # None = 默认密钥
        # 密码回落链（v1.4.9）：--jump-password > 别名专属 > PYAISSH_JUMP_PASSWORD > PYAISSH_PASSWORD。
        # 跳板与目标共用一套密码是常见场景（同主多机），且跳板【用户名】本就回落目标用户
        # （上方 target_user 回退链）——唯独凭据不回落是设计不一致，实测会多花一次往返才从
        # 错误提示里拿到"请用 --jump-password/PYAISSH_JUMP_PASSWORD"。只回落密码、不回落密钥：
        # 错误的 PYAISSH_KEY 会短路原本可用的默认密钥路径（真回归），而密码错误与缺密码的
        # 失败形态等价、无回归。回退发生时打 stderr WARN 保持行为可见。
        password = args.jump_password or alias_pw or os.environ.get("PYAISSH_JUMP_PASSWORD")
        if not password and os.environ.get("PYAISSH_PASSWORD"):
            password = os.environ["PYAISSH_PASSWORD"]
            log("[JUMP] 跳板未指定专属凭据，密码回退使用 PYAISSH_PASSWORD"
                "（需要独立跳板密码时请设 PYAISSH_JUMP_PASSWORD 或 --jump-password）")
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


_ATOMIC_POLICY = None  # _AtomicAutoAddPolicy 单例缓存（惰性创建，见 _atomic_auto_add_policy）


def _atomic_auto_add_policy():
    """惰性创建 AutoAddPolicy+原子写盘策略实例（v1.4.8 设计，v1.5.5 惰性化）。

    类继承 paramiko.AutoAddPolicy，模块级定义会强制 import paramiko——改为
    首次调用时 import + 定义 + 缓存实例。paramiko 只在真正建连时才加载，
    错误路径（--version/bad_args 等）不付 ~190ms import 开销。"""
    global _ATOMIC_POLICY
    if _ATOMIC_POLICY is None:
        global paramiko  # 统一绑定全局：类方法引用 paramiko.HostKeys 等
        import paramiko

        class _AtomicAutoAddPolicy(paramiko.AutoAddPolicy):
            """AutoAddPolicy + 原子写盘。

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

        _ATOMIC_POLICY = _AtomicAutoAddPolicy()
    return _ATOMIC_POLICY


def _do_connect(conn, jump_client, is_jump=False):
    """实际建立 SSH 连接。jump_client 非 None 时通过其 direct-tcpip 隧道。

    is_jump=True 表示本次连接的是跳板机本身（认证提示要说对变量名）。
    """
    global paramiko  # 惰性 import 绑定到模块全局（cmd_download/cmd_ls/_sftp_put_atomic
    try:             # 等模块级函数引用 paramiko.X；函数内裸 import 是局部名会 NameError）
        import paramiko
    except ImportError as e:
        # 依赖缺失独立分类：误导 AI 查网络（此前实测归 connection_failed 且
        # retryable:true，AI 会白白重试）。dependency_missing 不在
        # _RETRYABLE_ERRORS → retryable=false（装依赖前重试无意义）
        raise SshError(
            "依赖缺失: %s（pip install paramiko 可安装；pyaissh 的 SSH 底层库未就绪，"
            "与目标主机/网络无关，重试前请先安装依赖）" % e, "dependency_missing")
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
        client.set_missing_host_key_policy(_atomic_auto_add_policy())

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
                hint = ("（检查跳板机用户名/密码；密码建议用 PYAISSH_JUMP_PASSWORD 环境变量"
                        "或 --jump-password，密钥用 --jump-key / PYAISSH_JUMP_KEY）")
            else:
                hint = ("（检查用户名/密码是否正确；密码建议用 PYAISSH_PASSWORD "
                        "环境变量，密钥用 --key / PYAISSH_KEY）")
            raise SshError("认证失败: %s%s" % (e, hint), "auth_failed")
        except paramiko.SSHException as e:
            msg = str(e)
            # 无凭据场景（--key/--password/PYAISSH_PASSWORD 都没给且无默认密钥/agent）：
            # paramiko 报的原文很含糊，明确告诉 AI 缺什么
            if "no authentication methods available" in msg.lower():
                raise SshError("未提供可用凭据: %s（请用 --password / PYAISSH_PASSWORD 环境变量，"
                               "或 --key / PYAISSH_KEY 指定私钥）" % msg, "auth_failed")
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
    sftp._pyaissh_last_activity = time.time()


def _make_sftp_touch(sftp):
    """生成 put/get 的进度回调：刷新看门狗的活动时间戳（有数据流动=活着）。

    paramiko 回调签名 func(transferred, total)，用闭包把 sftp 传进去。
    """
    def _cb(transferred, total):
        sftp._pyaissh_last_activity = time.time()
    return _cb




# =========================================================================
# host 子命令（v2.1）：host add NAME user@host[:port] —— 把主机别名写进 .env
# =========================================================================

def _env_write_value(env_path, key, value):
    """把 key=value 写入 .env（已有同名行则整行替换，否则追加）；返回是否新增。

    行内值含空格/#/引号时用双引号包裹（解析器支持引号内 # 不拆）；
    值内含双引号时拒写（密码等凭据建议 --key 认证替代）。
    """
    lines = None
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
    newline_eol = "\r\n" if lines and any(l.endswith("\r\n") for l in lines) else "\n"
    if value and any(ch in value for ch in ' "#\''):
        if '"' in value:
            return None  # 拒写信号
        value = '"%s"' % value
    line = "%s=%s%s" % (key, value, newline_eol)
    if lines is None:
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w", encoding="utf-8", newline="") as f:
            f.write("# pyaissh 主机别名配置（host add 写入；.env 明文请勿提交 git）" + newline_eol + line)
        return True
    out, replaced, done = [], False, False
    for ln in lines:
        stripped = ln.split("=", 1)
        if len(stripped) == 2 and stripped[0].strip() == key:
            if not done:
                out.append(line)
                done = True
                replaced = True
            continue  # 丢弃旧的重复行
        out.append(ln)
    if not done:
        out.append(line)
    with open(env_path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))
    return not replaced


def cmd_host_add(args):
    """pyaissh host add <name> <user@host[:port]> [--password P] [--key PATH]

    把主机别名写进脚本同目录 .env（幂等：同名别名整行更新），随后可用
    `pyaissh exec @name ...` 直接调用（凭据由别名专属环境变量提供）。
    密码存 .env 是明文（与 PYAISSH_PASSWORD 同风险），脚本目录 .env 不会被
    供应链意外加载（仅同目录自动读），但切勿提交 git/分享。
    """
    name = getattr(args, "name", "") or ""
    target = getattr(args, "host_target", "") or ""
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        emit_error(args.json, "bad_args",
                   "别名只允许字母/数字/下划线: %r" % name)
        return 2
    try:
        user, host, port = parse_target(target)
    except SshError as e:
        emit_error(args.json, "bad_args", "目标格式错误: %s" % e)
        return 2
    if not user:
        emit_error(args.json, "bad_args",
                   "别名 target 必须写 user@host[:port]（别名凭据完全由 target 决定）")
        return 2
    key = "PYAISSH_HOST_%s" % name.upper()
    canonical = "%s@%s" % (user, host)
    if port and port != 22:
        canonical += ":%d" % port
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    added = _env_write_value(env_path, key, canonical)
    pw = getattr(args, "password", None)
    key_path = getattr(args, "key", None)
    if pw is not None:
        if _env_write_value(env_path, key + "_PASSWORD", pw) is None:
            emit_error(args.json, "bad_args",
                       "密码含双引号无法安全写入 .env——建议用密钥认证（--key）替代")
            return 2
    if key_path:
        _env_write_value(env_path, key + "_KEY", key_path)
    tips = ["别名 %s -> %s（调用: pyaissh exec @%s ...）" % (name, canonical, name.lower())]
    if pw is None and not key_path:
        tips.append("未存密码/密钥：将复用全局 PYAISSH_PASSWORD 或默认私钥；"
                    "要专属凭据可重跑加 --password/--key")
    tips.append(".env 是明文（路径 %s），请勿提交 git/分享" % env_path)
    result = {"ok": True, "action": "host", "alias": "@%s" % name.lower(),
              "target": canonical, "env_path": env_path, "tips": tips}
    # v2.1.2：统一走 _emit_result——host 也支持 --field（如 --field alias 只取别名）
    _emit_result(args, result)
    return 0


def _env_remove_keys(env_path, prefix):
    """从 .env 删除所有以 prefix 开头的键行（返回删除行数）。"""
    if not os.path.isfile(env_path):
        return 0
    with open(env_path, "r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    newline_eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
    kept, removed = [], 0
    for ln in lines:
        key = ln.split("=", 1)[0].strip()
        if key.startswith(prefix):
            removed += 1
            continue
        kept.append(ln)
    if removed:
        with open(env_path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(kept))
    return removed


def _env_host_entries(env_path):
    """读 .env 的 PYAISSH_HOST_<NAME>=user@host 行 -> [(name, target), ...]
    （只读 host 行，不含密码/密钥——list 绝不回显凭据）。"""
    if not os.path.isfile(env_path):
        return []
    out = []
    with open(env_path, "r", encoding="utf-8") as f:
        for ln in f:
            line = ln.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("PYAISSH_HOST_"):
                continue
            if "_PASSWORD" in line or "_KEY" in line:
                continue
            key, _, val = line.partition("=")
            name = key[len("PYAISSH_HOST_"):].strip()
            target = val.strip().strip('"').strip("'")
            if name and target:
                out.append((name.lower(), target))
    return out


def cmd_host_remove(args):
    """pyaissh host remove <name> —— 从 .env 删除别名（含专属密码/密钥行）。"""
    name = getattr(args, "name", "") or ""
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        emit_error(args.json, "bad_args", "别名只允许字母/数字/下划线: %r" % name)
        return 2
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    prefix = "PYAISSH_HOST_%s" % name.upper()
    removed = _env_remove_keys(env_path, prefix)
    if removed == 0:
        emit_error(args.json, "bad_args",
                   "别名 %s 不存在（host list 可查看已配置别名）" % name)
        return 2
    result = {"ok": True, "action": "host", "removed": "@%s" % name.lower(),
              "env_path": env_path,
              "note": "已删除 %d 行（host 目标及其专属密码/密钥，如有）" % removed}
    _emit_result(args, result)
    return 0


def cmd_host_list(args):
    """pyaissh host list —— 列出 .env 已配置的别名（只列 host 行，不回显密码/密钥）。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    entries = [{"name": n, "target": t} for n, t in _env_host_entries(env_path)]
    result = {"ok": True, "action": "host_list", "count": len(entries),
              "entries": entries,
              "tip": "调用: pyaissh exec @<name> ...（别名专属密码/密钥不在此列出）"}
    _emit_result(args, result)
    return 0

# ================= [域 07/13] SFTP 传输层：上传/下载的底层原语 ================= 
"""SFTP 传输层（域 07）：上传/下载的底层原语。

- open_sftp（惰性 + 看门狗）、_sftp_watchdog（30s 静默保活）
- 并行分片：_parallel_fetch / _parallel_put（多连接，高丢包链路提速）
- 原子性：_sftp_atomic_rename（posix-rename 或退化）、_sftp_put_atomic、.part 机制
- 断点：_sftp_get_resume / _remote_size_is、sftp_makedirs / sftp_walk
- _win_safe_rel_path（Windows 文件名安全化）、_transfer_extra（失败进度上下文）
被 cmd_upload/cmd_download（09）调用。
"""

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
    io_timeout = getattr(sftp, "_pyaissh_io_timeout", SFTP_IO_TIMEOUT)
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
            if time.time() - sftp._pyaissh_last_activity > io_timeout:
                sftp._pyaissh_watchdog_killed = True
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


def _sftp_env_timeout():
    """`PYAISSH_SFTP_IO_TIMEOUT`：全局放宽看门狗"静默即断"的判据（默认 30 秒）。

    适用场景：极慢链路（<~33KB/s）上**单次**大读可能超过 30 秒——例如会话首轮读 1MB 尾窗。
    看门狗语义不变（仍是"多久没有成功往返判死"），只是把数字放大；
    非法值忽略并 WARN，不影响默认行为。也可以只对某个会话放宽：`open_sftp(client, io_timeout=N)`。
    """
    raw = (os.environ.get("PYAISSH_SFTP_IO_TIMEOUT") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        log("[WARN] PYAISSH_SFTP_IO_TIMEOUT=%r 不是数字，忽略（用默认 %d 秒）"
            % (raw, SFTP_IO_TIMEOUT))
        return None
    if v <= 0:
        log("[WARN] PYAISSH_SFTP_IO_TIMEOUT 必须为正数（收到 %r），忽略（用默认 %d 秒）"
            % (raw, SFTP_IO_TIMEOUT))
        return None
    return v


def open_sftp(client, io_timeout=None):
    """打开带 I/O 超时兜底的 SFTP 会话。

    paramiko 默认不给 SFTP 设超时，连接被 NAT 静默丢弃时 put/get/listdir
    会无限阻塞。这里双重兜底：
      1) channel/sock settimeout（对部分读路径有效）；
      2) 看门狗线程（对 put/get 等阻塞读有效，见 _sftp_watchdog）。
    持续有数据流动的慢传输不受影响（callback / `_sftp_touch_activity` 持续刷新活动时间）。

    超时取值优先级：**显式参数 > `PYAISSH_SFTP_IO_TIMEOUT` > 默认 30 秒**
    （分片下载工作线程用显式更长的窗口）。
    """
    io_timeout = io_timeout or _sftp_env_timeout() or SFTP_IO_TIMEOUT
    sftp = client.open_sftp()
    try:
        sftp.get_channel().settimeout(io_timeout)
    except Exception:
        try:
            sftp.sock.settimeout(io_timeout)
        except Exception:
            pass
    sftp._pyaissh_io_timeout = io_timeout
    sftp._pyaissh_last_activity = time.time()
    sftp._pyaissh_watchdog = threading.Thread(target=_sftp_watchdog, args=(sftp,), daemon=True)
    sftp._pyaissh_watchdog.start()
    return sftp


def _parallel_fetch(conn, args_, remote, local, size, k, resume=False):
    """多连接分片下载：k 条独立 SSH 连接各下载一段，写入同一本地文件。

    背景（实测）：paramiko 单连接的 SFTP 读是"发一个请求等一个响应"，
    高丢包/长 RTT 链路（如跨境）上单条 TCP 流吞吐塌陷（~20KB/s 且会
    长时间停滞触发看门狗）；独立连接数近似线性提升吞吐（8 连接 ~5.5 倍）。
    全部分片用独立连接（不与主会话共用 transport）：空闲主会话的看门狗
    强断时会连带杀死同 transport 的其他通道（实测踩坑）。
    本地文件生命周期：调用方应传 <目标>.part 路径——全部连接建立成功后
    才创建/清空 .part，任一分片失败抛 SshError（download_failed/download_timeout）
    或建连失败抛连接类 SshError，由调用方负责删除 .part 并原子改名收尾。
    resume=True（--resume 分片续传）：不清空已有 .part；每个分片完成后写
    <目标>.part.done.<i> 标记，重跑时存在标记的分片直接跳过（省已完成分片
    的传输量）。调用方在全部完成后负责清理 .done.* 并 os.replace。"""
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
        if resume:
            open(local, "ab").close()  # 续传：保留已有数据（ab 模式不清空；不存在则创建）
        else:
            with open(local, "wb"):
                pass  # 连接全部就绪后才预创建清空 .part，分片线程以 r+b 各自定位写入

        def _run(i):
            client, sftp, start, end, _ = workers[i]
            done_path = "%s.done.%d" % (local, i)
            if resume and os.path.exists(done_path):
                # 该分片上次已完整写完（done 标记存在）：跳过，节省传输量
                workers[i][4] = end - start
                log("[SKIP] 分片 %d/%d 已完成（续传点），跳过" % (i + 1, k))
                return
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
                # 分片完整写完：写 done 标记（--resume 重跑时跳过已完成分片）
                if resume:
                    try:
                        open(done_path, "wb").close()
                    except Exception:
                        pass
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
                getattr(workers[i][1], "_pyaissh_watchdog_killed", False)
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


def _parallel_put(conn, args_, local, remote, size, k):
    """多连接分片上传：k 条独立 SSH 连接各上传本地文件的一段，写入远端同一 .part。

    对称于 _parallel_fetch（下载分片）：高丢包/长 RTT 链路单连接 SFTP 写吞吐
    同样塌陷，多连接近似线性提升（dogfood 实测：慢链路上传大文件是真实痛点，
    --parallel 仅下载生效的缺口在此补齐）。共享跳板隧道（只建一条跳板连接）。
    远端 .part 由主连接预创建（worker 以 r+b seek 写；并发 w+b 会互截空），
    全部写满后**用活跃的 worker 连接完成原子改名**（_sftp_atomic_rename）——
    不能用调用方的主 sftp：分片上传期间主连接空闲 30s 会被看门狗强断（实测
    dogfood 踩坑：50MB 分片 257s 后 rename 用主连接 -> 连接已死 upload_timeout）。
    失败/中断时用活跃 worker 连接清理 .part（keep_part 双丢防护保留）。"""
    part = _part_path(remote)  # 进程唯一名（分片上传不做续传）
    workers = []   # [client, sftp, start, end, got]
    errors = []
    shared_jump = None
    try:
        bounds = [(i * size // k, (i + 1) * size // k if i < k - 1 else size)
                  for i in range(k)]
        jump_conn = resolve_jump(args_, conn["user"])
        if jump_conn:
            try:
                shared_jump = _do_connect(jump_conn, None, is_jump=True)
            except SshError as e:
                raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e),
                               e.error_type)
            log("[JUMP] 分片共享跳板连接 -> %s@%s:%s"
                % (jump_conn["user"], jump_conn["host"], jump_conn["port"]))
        for i, (start, end) in enumerate(bounds):
            client = None
            try:
                client = _do_connect(conn, shared_jump)
                sftp = open_sftp(client, io_timeout=PARALLEL_IO_TIMEOUT)
            except BaseException:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                raise
            workers.append([client, sftp, start, end, 0])
        # 主连接预创建远端空 .part（worker 的 r+b 需要文件已存在）
        _sftp_touch_activity(workers[0][1])
        f0 = workers[0][1].open(part, "wb")
        f0.close()

        def _run(i):
            client, sftp, start, end, _ = workers[i]
            lf = rf = None
            try:
                lf = open(local, "rb")
                rf = sftp.open(part, "r+b")  # 各线程独立句柄定位写入
                off = start
                while off < end:
                    if _SIGTERM_RECEIVED:
                        # 信号已到：不再发新写请求，尽快退出让主线程 join 收尾
                        #（同 _parallel_fetch：不加检查点会阻塞在慢链路的
                        #  paramiko 内部读写里，拖到看门狗才释放）
                        raise KeyboardInterrupt("SIGTERM")
                    lf.seek(off)
                    data = lf.read(min(PARALLEL_READ_CHUNK, end - off))
                    if not data:
                        raise IOError("本地文件在偏移 %d 处提前 EOF" % off)
                    rf.seek(off)
                    rf.write(data)
                    off += len(data)
                    workers[i][4] = off - start
                    _sftp_touch_activity(sftp)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                errors.append((i, e))
            finally:
                for h in (lf, rf):
                    try:
                        if h is not None:
                            h.close()
                    except Exception as ce:
                        errors.append((i, ce))

        log("[PART] %d 连接分片上传 %s（每片约 %s）"
            % (k, format_size(size), format_size(size // k)))
        threads = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(k)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            for t in threads:
                t.join(POLL_TICK)
        if errors:
            timed_out = any(
                getattr(workers[i][1], "_pyaissh_watchdog_killed", False)
                or isinstance(e, (socket.timeout, TimeoutError))
                for i, e in errors)
            if timed_out:
                raise SshError("并行分片上传超时（%d 连接，单片 %d 秒无数据；"
                               "高丢包链路可调整 --parallel 重试——过高反而可能适得其反"
                               "（8 不行试 4/2），或稍后重试）"
                               % (k, PARALLEL_IO_TIMEOUT), "upload_timeout")
            i, e = errors[0]
            raise SshError("并行分片 %d/%d 上传失败: %s" % (i + 1, k, e), "upload_failed")
        got = sum(w[4] for w in workers)
        if got != size:
            raise SshError("上传不完整：得到 %d/%d 字节" % (got, size), "upload_failed")
        # 大小校验：所有分片写满区间后 .part 应等于本地大小
        _sftp_touch_activity(workers[0][1])
        rsize = workers[0][1].stat(part).st_size
        if rsize != size:
            raise SshError("分片上传大小校验失败（远端 %d != 本地 %d）"
                           % (rsize, size), "upload_failed")
        # 原子改名用活跃的 worker 连接（主 sftp 空闲 30s 会被看门狗杀）
        _sftp_atomic_rename(workers[0][1], part, remote)
    except BaseException as e:
        # 失败/中断：用活跃 worker 连接清理 .part（主连接可能已被看门狗杀；
        # keep_part 双丢防护：改名回退失败时 part 是新数据唯一副本，保留不删）
        if not getattr(e, "keep_part", False):
            cleaned = False
            for _, sftp, *_ in workers:
                try:
                    sftp.remove(part)
                    cleaned = True
                    break
                except Exception:
                    continue
            if not cleaned:
                # 极窄的静默残留窗口：所有 worker 连接已死（如传输中网络整体
                # 断开）→ 远端 .part 清不掉。part 名带 pid 下次不撞名，纯卫生
                # 问题；但 SKILL.md 第 7 条承诺"warnings 会提示清理命令"——
                # 串行路径由 _PUT_RESIDUE_WARNINGS 兜底，并行路径这里补上
                msg = ("并行分片上传中断，远端临时文件可能残留: %s"
                       "（清理：rm -f '%s'）" % (part, part))
                log("[WARN] " + msg)
                _PUT_RESIDUE_WARNINGS.append(msg)
        raise
    finally:
        for client, sftp, start, end, _ in workers:
            try:
                sftp.close()
            except Exception:
                pass
            close_all(client)
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
        if getattr(sftp, "_pyaissh_watchdog_killed", False):
            raise  # 看门狗强制断开：同上，交给外层报 timeout
        return False


def sftp_makedirs(sftp, remote_dir):
    """递归创建远程目录（类似 os.makedirs，已存在则跳过）。

    返回本次【新建】的目录列表（外层→内层顺序；已存在的不算）。
    v1.5.6 起：单文件上传用它自动建父目录后，调用方据返回值提示
    AI 新建了哪些目录（拼写错误的垃圾目录树可被发现）；目录传输的
    自动创建是文档明示行为，调用方不检查返回值即保持静默。"""
    remote_dir = remote_dir.rstrip("/")
    if remote_dir in ("", ".", "/"):
        return []
    try:
        _sftp_touch_activity(sftp)
        st = sftp.stat(remote_dir)
        # 已存在但不是目录：必须报错。静默通过会让目录上传假成功
        # （--no-recursive 变 ok:true 零传输）或后续 put 报出误导性错误
        if not stat.S_ISDIR(st.st_mode):
            raise SshError("远程路径已存在且不是目录: %s（请换目标路径或先处理该文件）"
                           % remote_dir, "bad_args")
        return []  # 已存在
    except (socket.timeout, TimeoutError):
        raise
    except SshError:
        raise
    except IOError:
        pass
    parent = posixpath.dirname(remote_dir)
    created = []
    if parent and parent != remote_dir:
        created = sftp_makedirs(sftp, parent)
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
        return created  # 并发下已存在：不算本次新建
    created.append(remote_dir)  # 递归先建父、自身后建：列表保持外层→内层
    return created


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
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
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


def _atomic_local_write(write_fn, local, resume=False):
    """本地原子落盘：write_fn(part) 写 .part，成功后 os.replace。
    失败/中断默认删除 .part——绝不留下"看似完整实则损坏"的半截文件
    （空洞文件会被 --skip-existing 按大小误判为已传完）。
    resume=True（--resume 续传模式）：.part 用固定名 <目标>.part（跨进程
    可续传），失败/中断【保留】它供下次 --resume 续传（log TIP 提示）。"""
    part = local + ".part" if resume else _part_path(local)
    try:
        write_fn(part)
        os.replace(part, local)
    except BaseException:
        if not resume:
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
        else:
            # 续传模式：保留 .part 作为续传点，提示下次可续传
            log("[TIP] 已保留续传点 %s（下次重试加 --resume 从断点继续）" % part)
        raise


def _sftp_atomic_rename(sftp, part, remote):
    """远端原子改名：posix-rename 覆盖；服务器不支持时退化为 remove+rename
    （非原子，WARN 一次）；回退前守卫防误删他人成果；回退也失败保留 part
    （keep_part 双丢防护标记，外层跳过清理）。从 _sftp_put_atomic 抽出，
    供串行上传与分片上传共用同一原子收尾。"""
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
        if not getattr(sftp, "_pyaissh_posix_rename_warned", False):
            sftp._pyaissh_posix_rename_warned = True
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


def _sftp_put_atomic(sftp, local, remote, progress=None, resume=False):
    """SFTP 上传 + 远端原子改名：先传 .part（默认进程唯一名 <pid>；--resume 时
    固定名 <remote>.part 且支持断点续传），成功后 posix-rename 覆盖。服务器不
    支持 posix-rename 扩展时退化为 remove+rename（非原子，WARN 一次）；回退前
    先确认 .part 仍在，防止把别的进程刚改完名的成果误删。progress 可选 [0]
    列表：累计已传字节（中断/失败时结果 JSON 能报真实进度，而不是恒 0）。
    resume=True：远端 .part 已存在且小于本地大小 → 从断点续传；大于等于本地
    大小 → 视为已完成/损坏，覆盖重传；中断保留续传点（不清理、不告警残留）。"""
    part = remote + ".part" if resume else _part_path(remote)
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
        sftp._pyaissh_last_activity = time.time()

    # 续传决策（--resume）：远端 .part 大小决定走全量 / 续传 / 覆盖重传
    resume_from = 0
    if resume:
        local_size = os.path.getsize(local)
        try:
            _sftp_touch_activity(sftp)
            part_size = sftp.stat(part).st_size
        except (socket.timeout, TimeoutError):
            raise
        except IOError:
            part_size = 0  # 不存在：全量传
        if part_size > 0 and part_size < local_size:
            resume_from = part_size
            log("[RESUME] 远端续传点 %s 已有 %s，从断点继续"
                % (part, format_size(part_size)))
        elif part_size >= local_size:
            # 续传点大小异常（>= 源大小）：绝不续一个坏尾巴，覆盖重传
            try:
                sftp.remove(part)
            except (socket.timeout, TimeoutError):
                raise
            except Exception:
                pass
            log("[WARN] 续传点大小异常（>= 源大小），覆盖重传: %s" % part)

    try:
        if resume_from:
            # 手动续传：本地从 offset 读，远端 part 以 r+ 定位写（paramiko put 不支持 offset）
            lf = open(local, "rb")
            lf.seek(resume_from)
            rf = sftp.open(part, "r+b")
            rf.seek(resume_from)
            got = resume_from
            try:
                while got < local_size:
                    if _SIGTERM_RECEIVED:
                        raise KeyboardInterrupt("SIGTERM")
                    data = lf.read(RECV_CHUNK)
                    if not data:
                        raise IOError("本地文件在偏移 %d 处提前 EOF" % got)
                    rf.write(data)
                    got += len(data)
                    part_touched[0] = True
                    if progress is not None:
                        progress[0] += len(data)
                    _sftp_touch_activity(sftp)
            finally:
                lf.close()
                try:
                    rf.close()
                except Exception:
                    pass
            if got != local_size:
                raise IOError("上传续传不完整：得到 %d/%d 字节" % (got, local_size))
            log("[FILE] 续传完成 %s (%s)" % (remote, format_size(local_size)))
        else:
            sftp.put(local, part, callback=_cb)
        _sftp_atomic_rename(sftp, part, remote)
    except BaseException as e:
        if getattr(e, "keep_part", False):
            raise  # 回退双丢防护：.part 是新数据唯一副本，保留不清理（已告警）
        if resume:
            # 续传模式：保留远端 .part 作为续传点（不清理、不告警残留），
            # 提示下次重试加 --resume 从断点继续
            log("[TIP] 已保留远端续传点 %s（下次重试加 --resume 从断点继续）" % part)
            _PUT_RESIDUE_WARNINGS.append(
                "上传中断，远端续传点已保留: %s（下次重试加 --resume 从断点继续）" % part)
            raise
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
                   "rm -f '%s'；批量清理 pyaissh 中断残留可用 "
                   "find <目标目录> -name '*.part.*' -delete。重试上传前应先清掉，"
                   "否则 .part 会按次累积）" % (part, part))
            log("[WARN] " + msg)
            _PUT_RESIDUE_WARNINGS.append(msg)
        raise


def _sftp_get_resume(sftp, remote, part, total_size):
    """下载断点续传（--resume 串行路径）：本地 .part 已有 N 字节 → 从 N 续传剩余。

    规则：part 不存在 → 全量写；已有 < 远端大小 → seek 续传；已有 >= 远端大小
    → 视为损坏/过时，清空覆盖重传（绝不续一个坏尾巴）。完成时 .part 大小必须
    等于 total_size（大小校验）。返回本次实际传输字节数（增量）。"""
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    if existing > total_size:
        log("[WARN] 本地续传点 %s 大小异常（%d > 远端 %d），覆盖重传" % (part, existing, total_size))
        existing = 0
    if existing:
        log("[RESUME] 本地续传点 %s 已有 %s，从断点继续" % (part, format_size(existing)))
    rf = sftp.open(remote, "rb")
    rf.seek(existing)
    lf = open(part, "r+b" if existing else "wb")
    lf.seek(existing)
    got = existing
    try:
        while got < total_size:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            data = rf.read(min(RECV_CHUNK, total_size - got))
            if not data:
                raise IOError("远端在偏移 %d 处提前 EOF（文件传输中被修改？）" % got)
            lf.write(data)
            got += len(data)
            _sftp_touch_activity(sftp)
    finally:
        try:
            rf.close()
        except Exception:
            pass
        try:
            lf.close()
        except Exception:
            pass
    if got != total_size:
        raise IOError("下载续传不完整：得到 %d/%d 字节" % (got, total_size))
    return got - existing


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



# ================= [域 08/13] exec 子命令实现 ================= 
"""exec 子命令实现（域 08）——cmd_exec 编排 + 三段函数。

- cmd_exec：编排（前置 -> 连接 -> 会话）
- _prepare_exec_command：组装段（命令加载/校验/sudo 组装，哨兵返回）
- _connect_exec：连接段（连接+跳板，错误映射+信号归位）
- _exec_session：执行会话（凭据检测 -> exec_command+stdin 注入 -> 并发读线程 ->
  drain/排水 -> 结果组装/异常映射；内嵌闭包 _partial_extra/_read/_drain_rest）
超时/截断/spill/--field 语义见 SKILL.md 与 docs/exec.md。
"""

def _prepare_exec_command(args):
    """exec 前置（无连接副作用）：加载/校验命令 + --sudo 组装。

    返回 (cmd, warnings, sudo_pw) 成功；
    失败返回 (None, (error_type, message, warnings))——错误输出由 cmd_exec 处理。
    等价搬移自 cmd_exec 原头部（校验快速失败 + sudo 组装），行为零变化。
    """
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
                # v2.2.4：读**字节**再显式解码（不再依赖 sys.stdin 的隐式 universal newlines），
                # 这样行尾归一由下面 _normalize_cmd_newlines 统一负责、可计数、可 --keep-crlf 关闭
                cmd = sys.stdin.buffer.read().decode("utf-8-sig")
                # 读 stdin 期间可能收到信号（handler 只置标志，阻塞的 read 无法
                # 被中断）：读到内容但信号已到 = 用户取消，不应继续执行命令
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")
            else:
                # utf-8-sig：自动剥离 UTF-8 BOM（\ufeff）——记事本/VS Code 等
                # Windows 工具写出的命令文件带 BOM 时，首行命令会被拼进 BOM
                # 字符而报 "command not found"（与 .env 解析同款处理）
                # newline=""：不做隐式 universal newlines 转换，行尾交给
                # _normalize_cmd_newlines 显式归一（v2.2.4；此前是文本模式副作用）
                # _fix_msys_local_path：Git Bash 下 /tmp/x.sh 等 Unix 风格本地路径
                # 转 Windows 路径（与 --local 同款；内部含 ~ 展开），避免 Windows
                # Python 把 /tmp 解析成盘根而报 Errno 2
                with open(_fix_msys_local_path(args.cmd_file), encoding="utf-8-sig",
                          newline="") as f:
                    cmd = f.read()
        except KeyboardInterrupt:
            raise  # 中断走 main 的 interrupted/130
        except Exception as e:
            return None, ("read_cmd_failed", str(e), warnings)  # 本地参数/文件问题，由 cmd_exec 输出
    # v2.2.4 CRLF 归一（默认；--keep-crlf 保留原样）：
    #  - 内联 --cmd 此前**完全没有**归一（argv 里的真 CR 直达 bash）——这是本版修的真坑，
    #    也覆盖"内联 heredoc 落盘的文件带 CR"（用户此前靠 sed -i 's/\r$//' 收尾）
    #  - --cmd-file / stdin 此前靠 Python 文本模式隐式归一（行为不变，只是变显式 + 可关闭）
    if cmd and not getattr(args, "keep_crlf", False):
        cmd, crlf_n = _normalize_cmd_newlines(cmd)
        if crlf_n:
            args._crlf_normalized = crlf_n
            warnings.append(
                "命令文本有 %d 处 CRLF/CR 行尾，已归一为 LF（远端 bash 会把 \\r 当词的一部分："
                "$'\\r': command not found、关键字行语法错、heredoc 落盘文件带 CR）；"
                "要原样发送加 --keep-crlf" % crlf_n)
    if not cmd or not cmd.strip():
        return None, ("bad_args", "未指定命令（--cmd 或 --cmd-file）", warnings)
    if args.max_time is not None and args.max_time < args.exec_timeout:
        return None, ("bad_args",
                      "--max-time (%d) 不能小于 --idle-timeout (%d)（总时长上限必须覆盖静默窗口）"
                      % (args.max_time, args.exec_timeout), warnings)

    # --sudo 提权组装：
    #  - 复合命令（含 &&/||/;/管道/重定向/$()/反引号/换行）→ bash -c 包裹整链提权
    #    （sudo 只提权首命令，第二段会回到原用户——实测回归项"&& 第二段 uid=0"）
    #  - 简单命令（无 shell 元字符）→ 直连 sudo：保留 sudoers NOPASSWD 按命令
    #    路径匹配（如 NOPASSWD: /usr/bin/apt 对 `sudo apt update` 生效；bash -c
    #    包裹会让 sudo 看到 /usr/bin/bash 而匹配不上免密规则）
    #  - -p ''：压掉 "[sudo] password for ..." 提示符（成功路径 stderr 干净）
    #  - 密码只经 SSH stdin 注入：命令文本/cmd 字段/日志/远端磁盘均无密码
    #  - orig_cmd 保留组装前原文：凭据启发式检测用原命令（组装后的 sudo -S -p ''
    #    前缀命中 _SENSITIVE_CMD_RE 的 -p 模式 -> 100% 误报"疑似凭据"，A 修复）
    orig_cmd = cmd
    sudo_pw = (args.sudo_password if args.sudo_password
               else os.environ.get("PYAISSH_SUDO_PASSWORD")) if args.sudo else None
    if args.sudo:
        if args.pty:
            return None, ("bad_args",
                          "--sudo 与 --pty 互斥（sudo -S 走 stdin 管道而非 pty）", warnings)
        if re.search(r"[\$`\n;|&><]|\(|\)", cmd):
            qcmd = cmd.replace("'", "'\\''")
            cmd = ("sudo -S -p '' bash -c '%s'" % qcmd) if sudo_pw \
                else ("sudo -n bash -c '%s'" % qcmd)
        else:
            cmd = ("sudo -S -p '' %s" % cmd) if sudo_pw else ("sudo -n %s" % cmd)

    return cmd, warnings, sudo_pw, orig_cmd



def _connect_exec(args):
    """连接目标（含跳板解析）；成功返回 (conn, client, None)；失败已 emit，返回 (None, None, 退出码)。

    与原 cmd_exec 连接 try 逐语句等价（SshError 带信号归位 / bad_args 特判 / 兜底 connection_failed）。
    """
    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
        return conn, client, None
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return None, None, 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return None, None, 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return None, None, 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return None, None, 255



def _exec_session(args, start, cmd, orig_cmd, sudo_pw, warnings, conn, client):
    """执行会话：凭据检测 -> exec_command(+stdin 注入) -> 并发读线程 -> drain/排水 -> 结果组装。

    原 cmd_exec 连接后主体整体搬入（语句顺序零改动）；闭包子例程
    _partial_extra / _read / _drain_rest 随迁（引用本函数局部 = 原闭包语义）。
    返回本地退出码；异常已 emit（130/255/124）。finally 兜底 spill + close_all。
    """
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
        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        extra = {
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "cmd": cmd_echo,  # 与成功路径对称：错误时也能看到命令原文（含凭据需脱敏；超限截断）
            "cmd_truncated": cmd_cut,  # cmd 回显是否被截断（--cmd-file 大脚本）
            "stdout": _clean_pty_text(out_cut.decode(args.encoding, errors="replace"), args),
            "stderr": _clean_pty_text(err_cut.decode(args.encoding, errors="replace"), args),
            # 与成功路径语义一致：原始字节数（total_counter 在 exec_command
            # 之后定义；exec_command 本身抛错时回退用缓冲长度）
            "stdout_bytes": total_counter[0],
            "stderr_bytes": err_total_counter[0],
            "output_incomplete": True,  # 错误中断，输出必然不完整（区别于超限裁剪）
            "duration_ms": int((time.time() - start) * 1000),
        }
        if warnings:
            extra["warnings"] = list(warnings)
        if getattr(args, "_crlf_normalized", 0):
            extra["crlf_normalized"] = args._crlf_normalized
        if cmd_cut:
            extra.setdefault("warnings", []).append(
                "cmd 字段已截断（完整命令 %d 字节，见原始调用；--cmd-file 时为本地文件可重读）" % cmd_n)
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
        w = warn_sensitive_cmd(orig_cmd, enabled=not getattr(args, "no_credential_warn", False))
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
        # --sudo 有密码时：先经 stdin 注入密码（sudo -S 从 stdin 读），再关闭
        #（密码只进 SSH 管道，不进 cmd 字段/日志/远端磁盘；sudo -p '' 压提示符）
        try:
            if sudo_pw:
                stdin.write(sudo_pw + "\n")
                stdin.flush()
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
                seam_body = ("[pyaissh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
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
                    seam_body = ("[pyaissh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
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
        last_hb = [time.time()]  # --progress 心跳（v2.1）：仅告知仍在运行，不重置静默计时
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
            if args.progress:
                now = time.time()
                if now - last_hb[0] >= args.progress:
                    # 心跳（--progress）：进程活着但静默——打 stderr 让 AI 安心，
                    # 不重置 silence_deadline（否则永远不 idle 超时）；force=True
                    # 绕过 --field 静音（长任务 + --field stdout 恰最需要心跳）
                    log("[PROGRESS] 仍在运行，已持续 %ds（连续 %ds 无输出/未结束；"
                        "更久任务调大 --idle-timeout/--max-time）"
                        % (int(now - start), args.progress), force=True)
                    last_hb[0] = now
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
        out_s = _clean_pty_text(out_cut.decode(args.encoding, errors="replace"), args)
        err_s = _clean_pty_text(err_cut.decode(args.encoding, errors="replace"), args)
        duration = int((time.time() - start) * 1000)

        if exit_code == 255:
            warnings.append("远程退出码为 255，本地返回 254（255 保留给连接失败语义）")
        if exit_code != 0:
            # 命令退出码非零 = 最常见的"命令失败"（工具 ok:true 但命令失败）。
            # 含 shell 特殊字符时提示转义（PowerShell 吃 \$ 的坑：远端收到
            # 被改写的命令，行为诡异且退出码非零——用户最需要 hint 的场景）。
            # --sudo 场景抑制转义 hint（E 修复）：命令由工具组装（sudo -S -p ''
            # / bash -c 包裹），用户输入的转义问题已由组装解决，此时 hint 只会
            # 把 AI 从"sudo 密码错/权限错"的真实原因引向"改用 --cmd-file"
            if not getattr(args, "sudo", False):
                hint = _shell_escape_hint(args.cmd)
                if hint:
                    warnings.append("命令退出码非零且含特殊字符。%s" % hint)
            # sudo 专用提示（--sudo 场景）：
            #  - 无密码（-n 探测）且 stderr 是 sudo 报错特征 -> 提示配密码
            #  - 有密码（-S 路径）且 stderr 是 sudo 密码错误特征 -> 提示改密码
            #    （最常发生的"密码错"此前无提示，只有误导性转义 hint——E 修复）
            if args.sudo:
                if not sudo_pw and re.search(
                        r"sudo:[^\n]*(password (is )?required|no password was provided|password required)",
                        err_s, re.IGNORECASE):
                    # -n 路径（无密码探测）提示：与 -S 路径同构——只认带
                    # "sudo:" 前缀的报错行（实测 sudo -n 报错恒带前缀：
                    # "sudo: a password is required"）。命令自身 stderr 恰好
                    # 打出 "a password is required"（无前缀）不触发（对称统一）
                    warnings.append("sudo -n 免密探测失败（sudo 需要密码）：用 --sudo-password "
                                    "或 PYAISSH_SUDO_PASSWORD 环境变量提供密码后重试，"
                                    "或由管理员配置 sudoers NOPASSWD")
                elif sudo_pw and re.search(
                        r"sudo:[^\n]*(incorrect password|no password was provided|password required|authentication failure)",
                        err_s, re.IGNORECASE):
                    # -S 路径密码错提示。门控必须收窄到 sudo 专属报错（带
                    # "sudo:" 前缀）：sudo 与命令的 stderr 同一 channel 无法
                    # 区分来源，宽泛子串（incorrect password / Sorry, try
                    # again / password mismatch）会被"命令自己往 stderr 打
                    # 这些词后失败"的场景误报（R7/R8，与 C 门控同类的坑）。
                    # sudo 密码错的典型 stderr："sudo: no password was
                    # provided" / "sudo: 1 incorrect password attempt"
                    warnings.append("sudo 密码错误（stderr 提示 incorrect password）："
                                    "检查 --sudo-password / PYAISSH_SUDO_PASSWORD 是否与"
                                    "登录密码一致后重试")
        stdout_truncated = bool(out_trunc or drop_counter[0])
        stderr_truncated = bool(err_trunc or err_drop_counter[0])
        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        if cmd_cut:
            warnings.append("cmd 字段已截断（完整命令 %d 字节，见原始调用；--cmd-file 时为本地文件可重读）" % cmd_n)
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
            "cmd": cmd_echo,  # 回显命令（含凭据需脱敏；超 CMD_ECHO_LIMIT 截断，见 cmd_truncated）
            "cmd_truncated": cmd_cut,  # cmd 回显是否被截断（--cmd-file 读入的大脚本）
            "output_truncated": bool(drain_truncated or stdout_truncated or stderr_truncated),
            "warnings": warnings,
            "duration_ms": duration,
        }
        if getattr(args, "_crlf_normalized", 0):
            # 明确回传"我改了你的命令文本"（改了就说清楚；--keep-crlf 时为 0/缺省）
            result["crlf_normalized"] = args._crlf_normalized
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
        if out_keep or err_keep:
            # v2.2：截断时给显式下一步——完整输出在本地 spill 文件，读文件比重跑便宜。
            # （默认保留量已降到 64KB：结果 JSON 太大会被宿主裁中段，连这个路径都可能丢）
            kept = [p for p in (spill_out_path if out_keep else None,
                                spill_err_path if err_keep else None) if p]
            result["next_action"] = (
                "输出超 --max-output(%d 字节) 被截断，完整内容已落盘：%s"
                "（本地文件，直接读，不要重跑；需要内联更多才调大 --max-output）"
                % (args.max_output, "、".join(kept)))
        _spill_handled = True
        header = "[%s]  exit_code=%d  duration=%dms" % (
            "OK" if exit_code == 0 else "EXIT %d" % exit_code, exit_code, duration)
        sections = [("STDOUT", out_s), ("STDERR", err_s)]
        _emit_result(args, result, header=header, sections=sections)
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
        # 超时类机器可读标记：远程进程可能仍在运行（124 幽灵进程）——AI 重试
        # 循环直接读该字段决定是否先 pgrep 确认，不必依赖记住文字提示
        if error_type in ("exec_idle_timeout", "exec_total_timeout", "exec_timeout"):
            timeout_extra = dict(_partial_extra())
            timeout_extra["remote_may_be_running"] = True
        else:
            timeout_extra = _partial_extra()
        # 转义 hint 同样抑制于 --sudo 场景（命令由工具组装，hint 会误导真实
        # 原因定位；与成功路径的抑制保持一致——E 修复）
        if not getattr(args, "sudo", False):
            hint = _shell_escape_hint(args.cmd)
            if hint:
                msg += hint
        emit_error(args.json, error_type, msg, extra=timeout_extra)
        return 124 if error_type != "exec_failed" else 255
    finally:
        # spill 兜底：成功路径已置 _spill_handled；异常/中断路径在此删除，不留垃圾
        if not _spill_handled:
            _close_spill(spill_out_fh, spill_out_path, keep=False)
            _close_spill(spill_err_fh, spill_err_path, keep=False)
        close_all(client)


def cmd_exec(args):
    """exec 编排：--detach 走后台作业；否则前置校验组装 -> 连接 -> 执行会话（三段独立函数）。"""
    if getattr(args, "detach", False):
        return _cmd_exec_detach(args)
    start = time.time()  # 计时含连接耗时：duration_ms 在跳板/慢网络下偏大

    prepared = _prepare_exec_command(args)
    if prepared[0] is None:
        _, (etype, emsg, pwarnings) = prepared
        extra = {"warnings": pwarnings} if pwarnings else None
        emit_error(args.json, etype, emsg, extra=extra)
        return 2  # 本地参数/校验问题，与 bad_args 同级
    cmd, warnings, sudo_pw, orig_cmd = prepared

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec

    return _exec_session(args, start, cmd, orig_cmd, sudo_pw, warnings, conn, client)


# =========================================================================
# 后台作业（v2.2）：exec --detach 启动 + log 子命令读取
#
# 设计要点：
# - 作业脚本落盘（job.sh = 用户命令原文）+ 运行器（run.sh = 落日志 + 捕获退出码）
#   → 不把命令拼进一行 shell（本地 PowerShell 展 $?/$! 的坑实测踩过三次）
# - setsid + nohup + 三路重定向 → SSH 断开/宿主单次调用超时都不影响作业
# - job.rc 是"作业已结束"的唯一可靠标记（被 kill 则永不出现，status 恒 running）
# - log 子命令增量读（--offset/next_offset）→ 轮询 payload 小、绕开宿主调用上限
# =========================================================================

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
JOB_SCRIPT = "job.sh"      # 作业本体（用户命令原文）
JOB_RUNNER = "run.sh"      # 运行器（跑作业 + 落日志 + 写 rc）
JOB_LOG_NAME = "job.log"   # 输出日志（stdout+stderr 合并）
JOB_RC_NAME = "job.rc"     # 退出码文件（存在 = 作业已结束）
JOB_PID_NAME = "job.pid"   # 进程组 leader pid（setsid 后 pid == pgid == sid，供 --kill / 存活探测）
JOB_POLL_TICK = 1.0        # --wait-rc 轮询间隔（秒）
JOB_TAIL_LINES = 100       # 不给 --lines/--offset 时默认回传的尾部行数
JOB_ERR_TAIL = 2048        # 启动失败时回传的日志尾巴字节数
JOB_KILL_GRACE = 5         # --kill：TERM 后宽限秒数，超时补 KILL


def _sh_quote(s):
    """POSIX 单引号转义（远端 shell 命令里安全嵌入路径/文本）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _job_dir_path(job_dir, job_id):
    """作业目录（远端 POSIX 路径；job_dir 允许尾部 /）。"""
    return "%s/%s" % (str(job_dir).rstrip("/") or "/", job_id)


def _job_files(job_dir, job_id):
    """作业文件路径表：dir / job / run / log / rc / pid。"""
    d = _job_dir_path(job_dir, job_id)
    return {"dir": d,
            "job": "%s/%s" % (d, JOB_SCRIPT),
            "run": "%s/%s" % (d, JOB_RUNNER),
            "log": "%s/%s" % (d, JOB_LOG_NAME),
            "rc": "%s/%s" % (d, JOB_RC_NAME),
            "pid": "%s/%s" % (d, JOB_PID_NAME)}


def _detach_scripts(paths, cmd):
    """生成 (job.sh 内容, run.sh 内容)——纯函数（单测覆盖）。

    job.sh = 用户命令**原文**（不转义/不拼接：多行脚本天然支持，无二次解析坑）
    run.sh = 跑 job.sh，stdout+stderr 合并进 job.log，随后退出码写入 job.rc
             （rc 是"作业已结束"的可靠标记；被 kill 则永不出现——此时靠 job.pid
              的存活探测判定 dead）
    v2.2.1 起 run.sh 首行 umask 077：job.log/job.rc 由 shell 创建，若按默认 umask
    会是 0644（同机其他用户可读命令输出/日志），收严到 0600。
    """
    body = cmd if cmd.endswith("\n") else cmd + "\n"
    job_sh = ("#!/bin/bash\n"
              "# pyaissh 后台作业本体（用户命令原文；自动生成，勿手改）\n" + body)
    run_sh = ("#!/bin/bash\n"
              "# pyaissh 后台作业运行器（自动生成，勿手改）\n"
              "umask 077\n"
              "bash %s > %s 2>&1\n"
              "echo $? > %s\n"
              % (_sh_quote(paths["job"]), _sh_quote(paths["log"]), _sh_quote(paths["rc"])))
    return job_sh, run_sh


def _sftp_mkdirs(sftp, path):
    """远端递归建目录（已存在不报错）。"""
    parts = [p for p in str(path).split("/") if p]
    cur = "/" if str(path).startswith("/") else ""
    for p in parts:
        cur = (cur.rstrip("/") + "/" + p) if cur else p
        try:
            sftp.mkdir(cur)
        except Exception:
            pass


def _sftp_write_bytes(sftp, path, data):
    """SFTP 写文件（覆盖；父目录需已存在）。"""
    with sftp.open(path, "wb") as f:
        f.write(data)


def _sftp_read_rc(sftp, rc_path):
    """读 rc 文件取退出码；文件不存在/内容非法 → None（None = 作业未正常结束）。"""
    if not rc_path:
        return None
    try:
        with sftp.open(rc_path, "rb") as f:
            raw = f.read(64)
    except Exception:
        return None
    try:
        return int(raw.decode("utf-8", errors="replace").strip())
    except (TypeError, ValueError):
        return None


def _sftp_read_pid(sftp, pid_path):
    """读作业进程组 leader pid；缺失/非法 → None。

    v2.3.0：读前先 `_sftp_touch_activity` —— 作业 `--wait-rc` 轮询会几十秒只有这一处
    SFTP 操作，不刷新活动时间就会被 SFTP 看门狗（默认 30s）误杀（实测 `--wait-rc 50`
    在 ~30s 处连接被杀、随后报"读不到日志文件"）。"""
    if not pid_path:
        return None
    try:
        _sftp_touch_activity(sftp)
    except Exception:
        pass
    try:
        with sftp.open(pid_path, "rb") as f:
            raw = f.read(64)
    except Exception:
        return None
    try:
        return int(raw.decode("utf-8", errors="replace").strip())
    except (TypeError, ValueError):
        return None


def _sftp_chmod(sftp, path, mode):
    """远端 chmod（尽力而为：不支持/权限不足不报错，不影响主流程）。"""
    if not path:
        return
    try:
        sftp.chmod(path, mode)
    except Exception:
        pass


def _pid_alive(client, pid):
    """远端存活探测（kill -0）。返回 True/False；探测不可用时 None。

    v2.2.1：作业被 kill/OOM/崩溃时 job.rc 永不出现——只认 rc 会让状态机
    永远停在 running（--wait-rc 轮询不收敛）。存活探测是唯一通用的收敛依据
    （比"我们自己 kill 时写合成 rc"通用：外部 kill 同样能判死）。
    """
    if not pid:
        return None
    try:
        cmd = "kill -0 %d 2>/dev/null && echo pyaissh_alive=1 || echo pyaissh_alive=0" % int(pid)
        _in, out, _err = client.exec_command(cmd, timeout=10)
        try:
            _in.close()
        except Exception:
            pass
        txt = out.read().decode("utf-8", errors="replace")
        try:
            out.channel.recv_exit_status()
        except Exception:
            pass
        if "pyaissh_alive=1" in txt:
            return True
        if "pyaissh_alive=0" in txt:
            return False
        return None
    except Exception:
        return None


def _alive_map(client, pids):
    """一次远端调用批量探测多个 pid（--list 场景避免 N 次 exec）。返回 {pid: bool}。"""
    pids = sorted({int(p) for p in pids if p})
    if not pids:
        return {}
    loop = ("for p in %s; do if kill -0 $p 2>/dev/null; then echo \"$p=1\"; "
            "else echo \"$p=0\"; fi; done" % " ".join(str(p) for p in pids))
    out = {}
    try:
        _in, so, _err = client.exec_command(loop, timeout=10)
        try:
            _in.close()
        except Exception:
            pass
        txt = so.read().decode("utf-8", errors="replace")
        try:
            so.channel.recv_exit_status()
        except Exception:
            pass
        for line in txt.splitlines():
            if "=" in line:
                k, _, v = line.strip().partition("=")
                try:
                    out[int(k)] = (v.strip() == "1")
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def _job_status(sftp, client, files):
    """作业状态三级判定 → (status, rc_val, pid)。

    finished：job.rc 存在（内容即退出码）
    dead    ：无 rc 且 pid 已消失（被 kill / OOM / 崩溃——不会再产出 rc）
    running ：无 rc 且进程仍在（或探测不可用，保守判 running）

    "dead" 的存在意义：让 --wait-rc 与消费端轮询**收敛**——否则被 kill 的作业
    永远是 running，AI 只能靠超时放弃（v2.2.1 修复）。

    v2.3.0 加固（真机偶发）：**刚结束的作业不能误判 dead**——`sleep 45` 跑完的那一瞬间，
    进程已退出但 `run.sh` 还没把 `job.rc` 落盘，实测被读成 dead（exit 不可知）。
    现在判 dead 前给 rc 一个短暂宽限（`JOB_RC_GRACE` 内重试两次读 rc）。
    """
    files = files or {}
    rc_val = _sftp_read_rc(sftp, files.get("rc"))
    pid = _sftp_read_pid(sftp, files.get("pid"))
    if rc_val is not None:
        return "finished", rc_val, pid
    if _pid_alive(client, pid) is False:
        # 进程没了：可能是"刚结束、rc 还在落盘"，也可能是"被 kill/OOM 永不落 rc"
        for _ in range(2):
            time.sleep(JOB_RC_GRACE / 2.0)
            rc_val = _sftp_read_rc(sftp, files.get("rc"))
            if rc_val is not None:
                return "finished", rc_val, pid
        return "dead", None, pid
    return "running", None, pid


def _detach_cleanup(sftp, paths):
    """删除作业目录及其中文件；返回成功删除的路径列表（尽力而为，不抛错）。"""
    removed = []
    for key in ("job", "run", "log", "rc", "pid"):
        p = paths.get(key)
        if not p:
            continue
        try:
            sftp.remove(p)
            removed.append(p)
        except Exception:
            pass
    try:
        sftp.rmdir(paths["dir"])
    except Exception:
        pass
    return removed


def _job_kill(client, sftp, files, grace=JOB_KILL_GRACE):
    """杀掉整个作业进程组：TERM → 宽限 grace 秒 → KILL。

    setsid 起的作业是独立会话/进程组，leader pid == pgid，故用负 pid 整组杀
    （否则只杀 run.sh，它派生的子进程会变孤儿继续跑）。

    返回 (ok, info_dict)：ok=False 时 info 里有 reason（如无 pid / 进程不在）。
    pid 校验：pid 文件只在作业目录内、且作业结束后会被读到"不在"，但 pid 复用
    理论上可能撞上无关进程——Linux 上额外校验 /proc/<pid>/cmdline 是否提到本作业
    的 run.sh 路径；非 Linux（无 /proc）跳过校验（此时按 pid 直杀，风险由用户知悉）。
    """
    pid = _sftp_read_pid(sftp, files.get("pid"))
    if not pid:
        return False, {"reason": "no_pid", "message": "没有 job.pid（作业可能已结束或被清理）"}
    if _pid_alive(client, pid) is False:
        return False, {"reason": "already_gone", "pid": pid,
                       "message": "进程 %d 已不存在（作业已结束/被 kill）" % pid}
    run_path = files.get("run") or ""
    guard = ("P=%d; "
             "if [ -r /proc/$P/cmdline ] && ! tr '\\0' ' ' < /proc/$P/cmdline | grep -qF %s; "
             "then echo pyaissh_kill=mismatch; exit 3; fi; "
             "kill -TERM -$P 2>/dev/null || kill -TERM $P 2>/dev/null; echo pyaissh_kill=term"
             % (pid, _sh_quote(run_path)))
    try:
        _in, so, se = client.exec_command(guard, timeout=max(10, grace + 5))
        try:
            _in.close()
        except Exception:
            pass
        txt = so.read().decode("utf-8", errors="replace")
        err = se.read().decode("utf-8", errors="replace")
        try:
            rc = so.channel.recv_exit_status()
        except Exception:
            rc = None
        if "pyaissh_kill=mismatch" in txt:
            return False, {"reason": "pid_mismatch", "pid": pid,
                           "message": "pid %d 的进程与作业脚本不匹配（pid 复用？）——拒绝杀，"
                                      "请人工确认" % pid}
        if "pyaissh_kill=term" not in txt:
            return False, {"reason": "kill_failed", "pid": pid,
                           "message": "TERM 未发出（rc=%s）：%s" % (rc, (err or txt).strip()[:200])}
    except Exception as e:
        return False, {"reason": "kill_failed", "pid": pid, "message": "TERM 失败：%s" % e}

    # 宽限等待：进程消失即算成功；仍在则 KILL
    deadline = time.time() + max(1, int(grace))
    while time.time() < deadline:
        time.sleep(JOB_POLL_TICK)
        if _pid_alive(client, pid) is False:
            return True, {"pid": pid, "signal": "TERM", "escalated": False}
    try:
        _in, so, _se = client.exec_command(
            "kill -KILL -%d 2>/dev/null || kill -KILL %d 2>/dev/null; echo done" % (pid, pid),
            timeout=15)
        try:
            _in.close()
        except Exception:
            pass
        so.read()
        try:
            so.channel.recv_exit_status()
        except Exception:
            pass
    except Exception:
        pass
    return True, {"pid": pid, "signal": "KILL", "escalated": True}


def _cmd_exec_detach(args):
    """exec --detach：远端后台运行命令，立即返回作业句柄（job_id/log/rc）。"""
    start = time.time()
    if args.sudo:
        emit_error(args.json, "bad_args",
                   "--detach 与 --sudo 互斥（sudo 密码需经 SSH stdin 注入，后台作业无 stdin；"
                   "提权请在命令内自行处理，或改用前台 exec）")
        return 2
    if args.pty or args.pty_strip_ansi:
        emit_error(args.json, "bad_args",
                   "--detach 与 --pty/--pty-strip-ansi 互斥（后台作业无终端）")
        return 2
    prepared = _prepare_exec_command(args)
    if prepared[0] is None:
        _, (etype, emsg, pwarnings) = prepared
        emit_error(args.json, etype, emsg,
                   extra={"warnings": pwarnings} if pwarnings else None)
        return 2
    cmd, warnings, _sudo_pw, orig_cmd = prepared

    job_dir = getattr(args, "job_dir", None) or DEFAULT_JOB_DIR
    job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-%d" % os.getpid()
    paths = _job_files(job_dir, job_id)
    job_sh, run_sh = _detach_scripts(paths, cmd)

    # 凭据启发式：后台作业把命令原文落盘到远端 job.sh（比前台更需注意别写明文凭据）
    w = warn_sensitive_cmd(orig_cmd, enabled=not getattr(args, "no_credential_warn", False))
    if w:
        warnings.append(w + "；且 --detach 会把命令原文写入远端 %s（勿留明文凭据，用完清理）"
                        % paths["job"])

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec

    dir_made = False
    try:
        sftp = open_sftp(client)
        _sftp_mkdirs(sftp, paths["dir"])
        dir_made = True
        # 权限从严（v2.2.1）：目录 0700、job.sh 0600——命令正文与完整输出都落盘，
        # 同机其他用户不得读取；job.log/job.rc 由 run.sh 首行 umask 077 保证 0600
        _sftp_chmod(sftp, job_dir, 0o700)
        _sftp_chmod(sftp, paths["dir"], 0o700)
        _sftp_write_bytes(sftp, paths["job"], job_sh.encode("utf-8"))
        _sftp_write_bytes(sftp, paths["run"], run_sh.encode("utf-8"))
        _sftp_chmod(sftp, paths["job"], 0o600)
        _sftp_chmod(sftp, paths["run"], 0o700)
        # 启动：setsid+nohup 完全脱离会话；同一条命令里落 pid、sleep 0.3 探一次状态——
        # 短作业直接给 finished+exit_code，长作业给 running，立即死掉给 dead
        # （umask 077：这里创建的 job.pid 也是 0600）
        launch = (
            "umask 077\n"
            "setsid nohup bash %s >/dev/null 2>&1 </dev/null &\n"
            "P=$!\n"
            "echo \"$P\" > %s\n"
            "sleep 0.3\n"
            "if [ -f %s ]; then echo \"pyaissh_rc=$(cat %s)\";\n"
            "elif kill -0 $P 2>/dev/null; then echo pyaissh_state=running;\n"
            "else echo pyaissh_state=dead; fi\n"
            "echo \"pyaissh_pid=$P\"\n"
            % (_sh_quote(paths["run"]), _sh_quote(paths["pid"]),
               _sh_quote(paths["rc"]), _sh_quote(paths["rc"]))
        )
        stdin, stdout, stderr = client.exec_command(launch, timeout=args.exec_timeout)
        try:
            stdin.close()
        except Exception:
            pass
        launch_out = stdout.read().decode(args.encoding, errors="replace")
        launch_rc = stdout.channel.recv_exit_status()
        launch_err = stderr.read().decode(args.encoding, errors="replace")
        m_pid = re.search(r"pyaissh_pid=(\d+)", launch_out)
        m_rc = re.search(r"pyaissh_rc=(-?\d+)", launch_out)
        pid = int(m_pid.group(1)) if m_pid else None
        rc_val = int(m_rc.group(1)) if m_rc else None
        if launch_rc != 0:
            _detach_cleanup(sftp, paths)
            emit_error(args.json, "detach_failed",
                       "后台作业启动失败（启动命令退出码 %d）：%s"
                       % (launch_rc, (launch_err or launch_out).strip()[:JOB_ERR_TAIL]))
            return 255
        if rc_val is None and "pyaissh_state=running" not in launch_out:
            # 启动后 0.3s 进程已不在且没有 rc：读日志尾巴帮定位（路径保留供人工查看）
            tail = ""
            try:
                with sftp.open(paths["log"], "rb") as f:
                    data = f.read(JOB_ERR_TAIL)
                tail = data.decode(args.encoding, errors="replace")
            except Exception:
                pass
            emit_error(args.json, "detach_failed",
                       "后台作业启动后立即退出且未写退出码（可能命令不可执行/脚本语法错）。"
                       "日志 %s%s" % (paths["log"], ("：\n" + tail) if tail else "（空）"),
                       extra={"job_id": job_id, "log": paths["log"], "rc": paths["rc"],
                              "pid": pid})
            return 255

        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        if cmd_cut:
            warnings.append("cmd 字段已截断（完整命令 %d 字节；远端作业脚本 %s 保留全文）"
                            % (cmd_n, paths["job"]))
        status = "finished" if rc_val is not None else "running"
        target_hint = "%s@%s" % (conn["user"], conn["host"])
        result = {
            "ok": True,
            "action": "exec",
            "version": VERSION,
            "detached": True,
            "job_id": job_id,
            "pid": pid,
            "status": status,
            "job_dir": paths["dir"],
            "log": paths["log"],
            "rc": paths["rc"],
            "pid_file": paths["pid"],
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "cmd": cmd_echo,
            "cmd_truncated": cmd_cut,
            "cmd_written_to": paths["job"],
            "permissions": {"job_dir": "0700", "job.sh": "0600", "run.sh": "0700",
                            "job.log": "0600", "job.rc": "0600", "job.pid": "0600"},
            "warnings": warnings,
            "duration_ms": int((time.time() - start) * 1000),
        }
        if getattr(args, "_crlf_normalized", 0):
            result["crlf_normalized"] = args._crlf_normalized
        if rc_val is not None:
            result["exit_code"] = rc_val
            result["exit_success"] = rc_val == 0
            result["next_action"] = (
                "作业已结束（exit_code=%d）。用 pyaissh log %s --job-id %s 读日志（载荷字段 stdout）；"
                "--cleanup 清理远端作业目录" % (rc_val, target_hint, job_id))
        else:
            result["next_action"] = (
                "作业在远端后台运行（SSH 断开不影响）。读日志：pyaissh log %s --job-id %s"
                "（载荷字段 stdout；增量：--offset <next_offset>）；等结束拿退出码：--wait-rc 30；"
                "停掉：--kill；清理：--cleanup" % (target_hint, job_id))
        header = "[DETACHED %s] job_id=%s pid=%s log=%s" % (
            status, job_id, pid if pid is not None else "?", paths["log"])
        _emit_result(args, result, header=header)
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        if dir_made:
            log("[WARN] --detach 失败，远端可能残留作业目录 %s（可手工删除）" % paths["dir"])
        emit_error(args.json, "detach_failed", str(e))
        return 255
    finally:
        close_all(client)


def _log_list(sftp, client, job_dir, args, start):
    """列出作业目录下的所有作业（状态/日志大小/退出码/时间）。

    状态三级（v2.2.1）：finished（有 rc）/ dead（无 rc 且进程已消失）/ running；
    无 rc 的条目会做一次批量存活探测（单条 exec，避免 N 次往返）。
    """
    jobs = []
    try:
        attrs = sftp.listdir_attr(job_dir)
    except Exception:
        attrs = []
    pending = []           # 无 rc 的作业：待批量存活探测
    for a in attrs:
        try:
            if not stat.S_ISDIR(a.st_mode):
                continue
        except Exception:
            continue
        d = "%s/%s" % (str(job_dir).rstrip("/"), a.filename)
        log_p, rc_p = "%s/%s" % (d, JOB_LOG_NAME), "%s/%s" % (d, JOB_RC_NAME)
        pid_p = "%s/%s" % (d, JOB_PID_NAME)
        try:
            log_bytes = sftp.stat(log_p).st_size
        except Exception:
            log_bytes = None
        rc_val = _sftp_read_rc(sftp, rc_p)
        entry = {"job_id": a.filename, "log": log_p, "log_bytes": log_bytes,
                 "status": "finished" if rc_val is not None else "running",
                 "exit_code": rc_val, "mtime": int(getattr(a, "st_mtime", 0))}
        if rc_val is None:
            entry["_pid"] = _sftp_read_pid(sftp, pid_p)
            pending.append(entry)
        jobs.append(entry)
    if pending:
        # 一次 exec 批量探测（list 可能几十条，逐个 exec 太贵）
        amap = _alive_map(client, [e.get("_pid") for e in pending])
        for e in pending:
            pid = e.pop("_pid", None)
            if pid and amap.get(pid) is False:
                e["status"] = "dead"     # 无 rc 且进程已消失：被 kill / OOM / 崩溃
    jobs.sort(key=lambda e: e["mtime"], reverse=True)
    result = {"ok": True, "action": "log", "version": VERSION,
              "job_dir": job_dir, "count": len(jobs),
              "jobs": jobs[:args.limit], "warnings": [],
              "duration_ms": int((time.time() - start) * 1000)}
    if len(jobs) > len(result["jobs"]):
        result["truncated"] = True
    return result


def _log_read(sftp, client, args, job_dir, conn, start):
    """读单个作业日志：--offset 增量 / 默认尾部 N 行；带结束状态、--kill 与清理。

    返回结果 dict；读不到日志时 emit_error 并返回 None（调用方返回 2）。

    v2.2.1 两处语义修订：
    - 载荷字段 `content` → **`stdout`**（与 exec 的 stdout/stderr 命名一致，
      消费端不用猜；合并流用 `stream: "stdout+stderr"` 声明）
    - 状态三级：finished（有 rc）/ **dead**（无 rc 且进程已消失）/ running
      ——被 kill 的作业不再永远 running，--wait-rc 与轮询都能收敛
    """
    paths = _job_files(job_dir, args.job_id) if args.job_id else None
    if args.path:
        log_path = _normalize_remote_path(sftp, args.path)
        d = log_path.rsplit("/", 1)[0] if "/" in log_path else "."
        if log_path.endswith("/" + JOB_LOG_NAME):
            rc_path = "%s/%s" % (d, JOB_RC_NAME)
            pid_path = "%s/%s" % (d, JOB_PID_NAME)
        else:
            rc_path = pid_path = None
        job_id = args.job_id or (d.rsplit("/", 1)[-1] if rc_path else None)
        job_dir_out = d
        files = {"dir": d, "log": log_path, "rc": rc_path, "pid": pid_path}
    else:
        log_path, job_id = paths["log"], args.job_id
        job_dir_out, files = paths["dir"], paths
        rc_path, pid_path = paths["rc"], paths["pid"]

    # --kill：先整组停掉（TERM → 宽限 → KILL），随后照常读状态/等收敛
    kill_info = None
    if getattr(args, "kill", False):
        if not paths:
            emit_error(args.json, "bad_args", "--kill 需配合 --job-id（要靠作业目录里的 job.pid）")
            return None
        ok, info = _job_kill(client, sftp, paths)
        kill_info = dict(info)
        kill_info["ok"] = ok
        if not ok and info.get("reason") not in ("already_gone",):
            emit_error(args.json, "kill_failed", info.get("message") or "杀作业失败",
                       extra={"job_id": job_id, "pid": info.get("pid"),
                              "reason": info.get("reason")})
            return None

    # --wait-rc：轮询到状态不再是 running（rc 出现 或 进程消失）或超时
    status, rc_val, pid = _job_status(sftp, client, files)
    waited_ms = 0
    if args.wait_rc and status == "running":
        t0 = time.time()
        deadline = t0 + args.wait_rc
        while time.time() < deadline:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            time.sleep(JOB_POLL_TICK)
            try:
                _sftp_touch_activity(sftp)   # 防 SFTP 看门狗误杀长等待（v2.3.0）
            except Exception:
                pass
            status, rc_val, pid = _job_status(sftp, client, files)
            if status != "running":
                break
        waited_ms = int((time.time() - t0) * 1000)

    try:
        size = sftp.stat(log_path).st_size
    except Exception as e:
        emit_error(args.json, "job_not_found",
                   "读不到日志文件 %s：%s（用 --list 看现有作业；作业目录 %s）"
                   % (log_path, e, job_dir_out))
        return None

    content_raw = b""
    next_offset = None
    has_more = False
    tail_window_cut = False
    if args.offset is not None:
        off = max(0, int(args.offset))
        if off > size:
            off = size
        with sftp.open(log_path, "rb") as f:
            f.seek(off)
            content_raw = f.read(args.max_output)
        next_offset = off + len(content_raw)
        has_more = next_offset < size
    else:
        n = args.lines if args.lines else JOB_TAIL_LINES
        back = min(size, JOB_TAIL_WINDOW)
        start_off = size - back
        tail_window_cut = start_off > 0
        with sftp.open(log_path, "rb") as f:
            f.seek(start_off)
            data = f.read(back)
        text = data.decode(args.encoding, errors="replace")
        picked = text.splitlines()[-n:]
        content_raw = "\n".join(picked).encode(args.encoding, errors="replace")
        if picked:
            content_raw += b"\n"
        next_offset = size

    cut, truncated, omitted = _truncate_output(content_raw, args.max_output, "log")
    result = {
        "ok": True, "action": "log", "version": VERSION,
        "job_id": job_id, "log": log_path, "rc": rc_path, "pid_file": pid_path,
        "pid": pid, "status": status,
        "exit_code": rc_val,
        "exit_success": (rc_val == 0) if rc_val is not None else None,
        "log_bytes": size,
        "stdout": cut.decode(args.encoding, errors="replace"),
        "stream": "stdout+stderr",     # job.log 是 2>&1 合并流，别误以为只有 stdout
        "bytes_returned": len(cut),
        "next_offset": next_offset, "has_more": has_more,
        "tail_window_truncated": tail_window_cut,
        "truncated": truncated, "omitted_bytes": omitted,
        "wait_rc_secs": args.wait_rc or None, "waited_ms": waited_ms,
        "host": conn["host"], "user": conn["user"], "port": conn["port"],
        "warnings": [], "duration_ms": int((time.time() - start) * 1000),
    }
    if kill_info is not None:
        result["kill"] = kill_info
    if args.cleanup and paths:
        # v2.2.2 守卫：作业还在跑就删目录 = 自断追踪（job.pid/job.log 一并没了，--list 归零，
        # 而 run.sh/job.sh 的子进程仍在远端跑）。默认拒绝，给出两条正路：先停或先等。
        if status == "running" and not getattr(args, "force", False):
            emit_error(args.json, "job_running",
                       "作业仍在运行（pid %s），拒绝 --cleanup：删掉作业目录会让本工具彻底失去"
                       "追踪（job.pid/job.log/job.rc 都没了），而远端进程仍在跑。"
                       "先停掉（--kill）或等结束（--wait-rc），再 --cleanup；"
                       "确要放弃追踪：--cleanup --force" % (pid if pid is not None else "?"),
                       extra={"job_id": job_id, "pid": pid, "status": status,
                              "log": log_path,
                              "hint": "一步到位：pyaissh log <target> --job-id %s --kill --cleanup"
                                      % (job_id or "<id>")})
            return None
        result["cleaned_paths"] = _detach_cleanup(sftp, paths)
        result["cleaned"] = True
        if status == "running":
            result["forced_cleanup"] = True
            if pid is not None:
                # pid 就是 setsid 的进程组组长（pid==pgid），负号即"整组"——一次清干净，
                # 不必 pgrep；此时 job.pid 已删，--kill 用不了，这是唯一出路（v2.2.3）
                result["group_kill"] = "kill -9 -%s" % pid
                result["warnings"].append(
                    "作业仍在运行（pid %s）时被强制清理：本工具已失去该作业的追踪，远端进程仍在跑"
                    "——要停掉直接整组杀：kill -9 -%s（负号 = 进程组；pid 即 setsid 组长，"
                    "子进程一并清掉，无需 pgrep）。下次可直接用 --kill --cleanup"
                    % (pid, pid))
            else:
                result["warnings"].append(
                    "作业仍在运行（pid 未知）时被强制清理：本工具已失去该作业的追踪，"
                    "远端进程可能仍在跑（job.pid 缺失，只能人工 pgrep 定位）")
    if status == "finished":
        result["next_action"] = ("作业已结束（exit_code=%s）。%s"
                                 % (rc_val, "已清理远端作业目录"
                                    if result.get("cleaned") else
                                    "如需清理：加 --cleanup"))
    elif status == "dead":
        result["hint"] = ("无 job.rc 且进程已消失（被 kill / OOM / 崩溃）：退出码不可知，"
                          "状态机按 dead 收敛，不要再用 --wait-rc 等")
        result["next_action"] = ("作业已死（无退出码）。看日志尾部确认原因；%s"
                                 % ("已清理远端作业目录" if result.get("cleaned")
                                    else "清理：加 --cleanup"))
    elif result.get("forced_cleanup"):
        # 目录（含 job.pid）已按 --force 删除 → job.log 也没了，不能再"继续增量读"，
        # --kill 同样不可用；唯一出路是按组长 pid 手工整组杀（v2.2.3 修掉误导文案）
        result["next_action"] = (
            "作业目录已按 --force 清理，无法再追踪也无法用 --kill（job.pid 已删）。"
            "要停掉远端进程：%s（负号 = 进程组，一次清整组）；"
            "下次改用：pyaissh log <target> --job-id <id> --kill --cleanup"
            % (result.get("group_kill") or "kill -9 -<pid>"))
    else:
        result["next_action"] = ("作业仍在运行。继续增量读：--offset %s（或稍后重读）；"
                                 "等结束：--wait-rc 30；停掉：--kill"
                                 % (next_offset if next_offset is not None else 0))
    if truncated and args.offset is None:
        # 尾部模式被 _truncate_output 截中段：has_more=false + next_offset=EOF 会让人
        # 以为"读完了"，其实中段缺失（省略 omitted 字节）——明确给出补齐路径
        result["next_action"] = ("内容被截断（省略 %d 字节）；完整读取请用 --offset 0 顺序读"
                                 "（按 next_offset 续读）。" % omitted) + result["next_action"]
    return result


def cmd_log(args):
    """log（别名 tail）：读后台作业日志 + 结束状态，支持增量读与清理。

    与其他子命令同契约：stdout 单行 JSON；--field 可提取 content/exit_code/jobs 等。
    """
    start = time.time()
    job_dir = getattr(args, "job_dir", None) or DEFAULT_JOB_DIR
    if args.list_jobs and (args.job_id or args.path):
        emit_error(args.json, "bad_args", "--list 不与 --job-id/--path 同用（列清单即可）")
        return 2
    if not args.list_jobs and not (args.job_id or args.path):
        emit_error(args.json, "bad_args", "需指定 --job-id 或 --path（列作业清单用 --list）")
        return 2
    if args.job_id and not _JOB_ID_RE.match(args.job_id):
        emit_error(args.json, "bad_args",
                   "非法 --job-id %r（只允许字母/数字/下划线/点/连字符，防路径穿越）"
                   % (args.job_id,))
        return 2
    if args.lines is not None and args.offset is not None:
        emit_error(args.json, "bad_args", "--lines 与 --offset 互斥（尾部 N 行 / 增量读，二选一）")
        return 2
    if args.wait_rc and args.wait_rc > JOB_WAIT_MAX:
        emit_error(args.json, "bad_args",
                   "--wait-rc 上限 %d 秒（宿主单次调用约 600s 上限；更久作业请稍后轮询）"
                   % JOB_WAIT_MAX)
        return 2
    if args.cleanup and not args.job_id:
        emit_error(args.json, "bad_args", "--cleanup 需配合 --job-id（清理整个作业目录）")
        return 2
    if args.cleanup and args.wait_rc:
        emit_error(args.json, "bad_args", "--cleanup 与 --wait-rc 互斥（先等结束再清理，分两次调用）")
        return 2
    if args.kill and not args.job_id:
        emit_error(args.json, "bad_args", "--kill 需配合 --job-id（要靠作业目录里的 job.pid 整组停掉）")
        return 2
    if args.kill and args.list_jobs:
        emit_error(args.json, "bad_args", "--kill 不与 --list 同用")
        return 2
    if args.force and not args.cleanup:
        emit_error(args.json, "bad_args", "--force 只配合 --cleanup（强制清理运行中的作业目录）")
        return 2

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        sftp = open_sftp(client)
        if args.list_jobs:
            result = _log_list(sftp, client, job_dir, args, start)
            _emit_result(args, result, header="[JOBS] %d in %s" % (result["count"], job_dir))
            return 0
        result = _log_read(sftp, client, args, job_dir, conn, start)
        if result is None:
            return 2
        head = ("[FINISHED exit_code=%s]" % result["exit_code"]
                if result["status"] == "finished" else
                "[DEAD 无退出码]" if result["status"] == "dead" else "[RUNNING]")
        _emit_result(args, result, header="%s job_id=%s log=%s"
                     % (head, result.get("job_id"), result["log"]),
                     sections=[("LOG", result["stdout"])])
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "log_failed", str(e))
        return 255
    finally:
        close_all(client)



# ================= [域 09/13] upload/download 子命令实现 ================= 
"""upload/download 子命令实现（域 09）。

- cmd_upload：校验 -> 预置失败上下文 -> 连接 -> sftp 主流程（单文件/递归/并行/
  --resume/--dry-run/--skip-existing）-> 结果（file_list/bytes/parallel_used）
- cmd_download：对称；断点重试/符号链接/路径安全化
底层原语在 07_sftp_transfer.py；失败上下文 _transfer_extra 见 07。
"""

def _excluded_by(rel, name, pats):
    """--exclude 匹配（v2.1）：任一模式（fnmatch glob）命中文件名或相对路径即排除。
    rel 传入正斜杠相对路径（Windows 也先归一）。"""
    for p in pats:
        if not p:
            continue
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p):
            return True
    return False


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
            msg += "（路径含通配符：pyaissh 不做本地 glob 展开，请先在 shell 展开成明确路径）"
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
    parallel_used = 1     # 本次实际并行连接数（结果回显，AI 无需猜测档位；对称下载）
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
    # --exclude（v2.1）：逗号分隔 glob，目录整树剪枝 / 文件跳过（不上传不计数）
    exclude_pats = [p.strip() for p in (args.exclude or "").split(",") if p.strip()]
    if exclude_pats:
        log("[EXCL] 排除模式: %s" % ", ".join(exclude_pats))

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

        if args.resume and is_dir:
            msg = "--resume 仅对单文件上传生效；目录上传忽略 --resume"
            log("[WARN] " + msg)
            walk_warnings.append(msg)

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
                if exclude_pats:
                    dirs[:] = [d for d in dirs
                               if not _excluded_by(os.path.join(rel_root, d).replace(os.sep, "/"),
                                                   d, exclude_pats)]
                    filenames = [f for f in filenames
                                 if not _excluded_by(os.path.join(rel_root, f).replace(os.sep, "/"),
                                                     f, exclude_pats)]
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
            if size >= RESUME_MIN_SIZE and not args.resume:
                tip = ("文件 %s ≥ %s，建议加 --resume 断点续传"
                       "（中断后重试不重传已传部分）" % (name, format_size(RESUME_MIN_SIZE)))
                log("[TIP] " + tip)
                walk_warnings.append(tip)
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
                created = []
                if parent:
                    created = sftp_makedirs(sftp, parent)
                if created:
                    # 单文件上传自动 mkdir -p 父目录：warnings 提示新建了哪些目录
                    #（AI 可见；拼写错误的垃圾目录树（如 /usr/loca/bin/x）可被发现）
                    tip = ("已自动创建远端父目录: %s（单文件上传 mkdir -p 语义；"
                           "若为路径拼写错误请检查 remote）" % ", ".join(created))
                    log("[MKDIR] " + tip)
                    walk_warnings.append(tip)
                if args.parallel and size >= 64 * 1024:
                    # 显式 --parallel：分片上传（对称下载分片，慢链路提速）。
                    # 默认不加自动档（upload 行为零变化，用户主动提速才用）；
                    # 与 --resume 互斥：分片上传不做续传
                    if args.resume:
                        walk_warnings.append("--parallel 上传与 --resume 互斥，忽略 --resume"
                                             "（分片上传不做续传）")
                        log("[WARN] --parallel 上传与 --resume 互斥，忽略 --resume")
                    parallel_used = args.parallel
                    try:
                        # _parallel_put 内部用活跃 worker 连接完成原子改名与
                        # 失败清理（主 sftp 空闲 30s 会被看门狗杀，不能参与）
                        _parallel_put(conn, args, local, remote, size, args.parallel)
                    except BaseException:
                        # 兜底清理：worker 连接清理失败时主连接再试一次
                        try:
                            sftp.remove(_part_path(remote))
                        except Exception:
                            pass
                        raise
                else:
                    _sftp_put_atomic(sftp, local, remote, progress=bytes_uploaded,
                                     resume=bool(args.resume))
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
            "parallel_used": parallel_used,  # 实际分片连接数：单连接=1，分片=--parallel 值（AI 确认档位，对称下载）
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
        _emit_result(args, result, header=header, sections=sections)
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
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
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
        # --resume 中断：本地固定续传点保留时，warnings 提示下次可续传
        #（串行路径的 _atomic_local_write 只 log stderr TIP，这里补 JSON 通道；
        #  分片路径的 except 已单独加过，靠内容去重）
        if getattr(args, "resume", False) and os.path.exists(local + ".part"):
            tip = "下载中断，本地续传点已保留: %s（下次重试加 --resume 从断点继续）" % (local + ".part")
            if tip not in dl_warnings:
                dl_warnings.append(tip)
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
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
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

        if args.resume and is_dir:
            msg = "--resume 仅对单文件下载生效；目录下载忽略 --resume"
            log("[WARN] " + msg)
            dl_warnings.append(msg)

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
            if size >= RESUME_MIN_SIZE and not args.resume:
                tip = ("文件 %s ≥ %s，建议加 --resume 断点续传"
                       "（中断后重试不重传已传部分）" % (name, format_size(RESUME_MIN_SIZE)))
                log("[TIP] " + tip)
                dl_warnings.append(tip)
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
                    part = local + ".part" if args.resume else _part_path(local)
                    try:
                        _parallel_fetch(conn, args, remote, part, size, k,
                                        resume=bool(args.resume))
                        # 清理分片 done 标记（--resume 续传时产生），再原子改名
                        if args.resume:
                            for i in range(k):
                                dp = "%s.done.%d" % (part, i)
                                if os.path.exists(dp):
                                    try:
                                        os.remove(dp)
                                    except OSError:
                                        pass
                        os.replace(part, local)
                    except BaseException:
                        if not args.resume:
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
                        else:
                            # 续传模式：保留 .part + done 标记作为续传资产；
                            # 中断若发生在分片建连阶段（.part 未创建）则如实说明
                            if os.path.exists(part):
                                log("[TIP] 已保留本地续传点 %s（下次重试加 --resume 从断点继续）" % part)
                                dl_warnings.append(
                                    "下载中断，本地续传点已保留: %s（下次重试加 --resume 从断点继续）" % part)
                            else:
                                log("[TIP] 下载中断于分片建连阶段，未产生续传点；"
                                    "下次重试加 --resume 全量重传")
                        raise
                else:
                    # 串行单连接同样走 .part + 原子改名：原子性不应随文件大小/
                    # 并行度静默变化（小文件中断也曾留下最终名的半截文件）
                    if args.resume:
                        # --resume：从本地 .part 已有字节续传（_sftp_get_resume 内部
                        # 处理"不存在全量/已有续传/大小异常覆盖"三种情形）
                        _atomic_local_write(
                            lambda part: _sftp_get_resume(sftp, remote, part, size),
                            local, resume=True)
                    else:
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
        _emit_result(args, result, header=header, sections=sections)
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
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
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



# ================= [域 10/13] test/ls 子命令实现 ================= 
"""test/ls 子命令实现（域 10）。

- cmd_test：连通/认证探测（hostname/os/kernel/arch；stdout EOF 后等 exit-status 宽限）
- cmd_ls：SFTP 列表（entries[] name/mode/size/is_dir/is_symlink/mtime；~ 展开；无 glob）
"""

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
        out_s = b"".join(chunks).decode(args.encoding, errors="replace").strip().splitlines()
        err_s = b"".join(err_chunks).decode(args.encoding, errors="replace").strip()
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
        _emit_result(args, result, header=header, sections=sections)
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
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
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
            # 文件名编码说明：paramiko 按 UTF-8+replace 解码 SFTP 文件名（原始字节
            # 不可还原）——GBK 等非 UTF-8 文件名会以 U+FFFD 显示（实测确认，
            # v1.5.13 曾试 --encoding 还原，paramiko 层信息已丢，回滚）；文件名
            # 建议保持 UTF-8，或经 exec `ls -b`/base64 自行取原始字节
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
            content += "\n[pyaissh] 共 %d 条，仅显示前 %d 条（--limit 调整）" % (total, len(items))

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
        _emit_result(args, result, header=header, sections=sections)
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
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
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


# ================= [域 11/13] 常驻会话子命令实现 ================= 
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


def _session_tick_default():
    """看门狗检查周期（秒，**整数**）。默认 15；`PYAISSH_SESSION_TTL_TICK` 可覆盖（**测试用**：
    设 3 可让"等一个 tick"的用例快 5 倍；生产别乱调——周期越短 fork 越多、回收越及时）。
    非法值（非数字/小于 1/大于 600）只打 WARN 并回落默认 15。

    只接受整数：脚本里用 `TICK=%d` 与 `sleep "$TICK"` 落值，小数会被 `%d` 截成 0 ⇒
    `read -t 0` 立刻返回，看门狗会退化成忙循环（护栏能兜住，但没必要冒这个险）。
    """
    raw = (os.environ.get("PYAISSH_SESSION_TTL_TICK") or "").strip()
    if not raw:
        return 15
    try:
        v = int(raw)
    except ValueError:
        log("[WARN] PYAISSH_SESSION_TTL_TICK 值 %r 非整数，用默认 15" % raw)
        return 15
    if v < 1 or v > 600:
        log("[WARN] PYAISSH_SESSION_TTL_TICK 值 %r 超范围（1~600 秒），用默认 15" % raw)
        return 15
    return v


_SESSION_TTL_TICK = _session_tick_default()


def _session_files(root, name):
    """会话远端路径表（与作业同款：一个会话一个 0700 目录）。"""
    d = "%s/%s" % (root.rstrip("/"), name)
    return {"dir": d, "fifo": d + "/in", "log": d + "/out.log", "err": d + "/err.log",
            "pid": d + "/sess.pid", "meta": d + "/meta", "bash": d + "/bash.pid",
            "token": d + "/last.token", "beat": d + "/beat", "watch": d + "/watch.pid",
            "wdlog": d + "/wd.log", "wdfifo": d + "/wd.fifo",
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
      ② 距上次 pyaissh 交互超过 TTL：beat 文件里存的是 **epoch 秒**（每次交互由 `_session_touch` 写入）。
    为什么独立进程而不是会话树内的一员：
      - 会话是 `setsid nohup` 起的，看门狗也必须脱离发起它的那次 SSH 连接（否则 start 一返回
        就随连接收到 SIGHUP 而死）；
      - 独立进程不在会话树闭包内 ⇒ 它做清理时不会把自己先杀掉（能走完 TERM→KILL→校验）。
    回收动作与 `kill` 同款：**自证**闭包（argv 含本会话目录才认，防 pid 回收误杀）→ 先删目录
    （即使自己被信号打断也不留残留目录）→ TERM → 宽限 → KILL。
    退出条件：会话目录消失（已被 kill/回收）就退，**不留常驻循环**。
    beat 缺失/内容非法时只续期不回收（宁可多留也不误杀）。

    **单进程实现（v2.3.0，设计 C）**：不再用外部 `sleep`，改用 bash 内建的
    `read -t "$TICK" -u 9`（fd 9 = 自持读写的 `wd.fifo`，写端握在自己手里所以永不 EOF）——
    每个会话因此只多 **1 个**进程（省掉 `sleep` 那 1.9 MB）。同时尽量用内建少 fork：
    `B=$(<"$BPID")`（不 fork `cat`）、时间用 `$EPOCHSECONDS`（bash≥5，不 fork `date`；
    老 bash 自动回落 `date +%s`）。回收路径每轮只剩 1 次 `pgrep`（`ps`/`awk` 只在真正回收时跑）。

    **防 spin 护栏**：`read -t` 若因 fd 异常而**立刻返回**，循环会变成忙循环（实测无护栏时
    5 秒烧掉 ≈6 秒 CPU = 跑满一个核）。所以每轮用内建 `$SECONDS` 量耗时，连续 3 次"立刻返回"
    就写一行 `wd.log` 并**退回外部 `sleep`**（此后再出问题也只是回到"2 个进程"的老形态，不会烧 CPU）。

    **判闲的两个前提（都是实测踩出来的）**：
      - shell pid 要在**判闲前重新读**一次：第一轮 tick 在 `read` 之前（约 0.4 s）`bash.pid` 还没写出来，
        用空值会判成"没有命令在跑"——实测把正在 `sleep 40` 的会话误回收了（`--ttl 5`）。
      - **pid 未知一律视为忙**（不回收）：没有 shell pid 就无法证明空闲，宁可多留也不误杀；
        这种降级会话（init payload 没写成功）不会被空闲回收，`list` 会提示 shell pid 缺失。
    """
    q = _sh_quote
    return (
        "#!/bin/bash\n"
        "# pyaissh 空闲回收看门狗（自动生成；TTL=%d 秒，每 %d 秒检查一次；单进程实现）\n"
        "D=%s; BEAT=%s; BPID=%s; SPID=%s; WPID=%s; META=%s; TTL=%d; TICK=%d; WDFIFO=%s\n"
        "mkfifo -m 600 \"$WDFIFO\" 2>/dev/null || true\n"
        "exec 9<>\"$WDFIFO\"          # 自持读写端：read -t 才有阻塞语义（写端在自己手里，不会 EOF）\n"
        "FAST=0; P=\"\"; B=\"\"; M0=\"\"\n"
        "\n"
        "# 共用清理（TTL 到期与\"临终带走\"都走这里，只有一套实现）：\n"
        "#   自证闭包（argv 必须仍含本会话目录**加斜杠** ⇒ 防 pid 被内核复用后误杀无关进程）→ 可选删目录 → TERM→KILL\n"
        "#   用 \"$D/\" 而不是 \"$D\"：会话名互为前缀时（work 与 work2）后者会误匹配到另一个会话的进程\n"
        "#   参数 rm ⇒ 连目录一起删（TTL 到期路径）；不带参数 ⇒ 目录已不在，只收进程（临终路径）\n"
        "cleanup_tree() {\n"
        "  T=\"\"; SEEN=\"\"; ROOTS=0; SNAP=$(ps -eo pid=,ppid=)\n"
        "  for r in \"$P\" \"$B\" \"$(ps -o ppid= -p \"$B\" 2>/dev/null | tr -d ' ')\"; do\n"
        "    [ -n \"$r\" ] || continue\n"
        "    [ \"$r\" = \"$$\" ] && continue\n"
        "    [ \"$r\" = 1 ] && continue\n"
        "    case \" $SEEN \" in *\" $r \"*) continue ;; esac\n"
        "    kill -0 \"$r\" 2>/dev/null || continue\n"
        "    A=$(ps -o args= -p \"$r\" 2>/dev/null)\n"
        "    case \"$A\" in *\"$D/\"*) ;; *) continue ;; esac\n"
        "    SEEN=\"$SEEN $r\"; ROOTS=$((ROOTS+1))\n"
        "    T=\"$T $(echo \"$SNAP\" | %s)\"\n"
        "  done\n"
        "  [ \"$ROOTS\" -gt 0 ] || return 1\n"
        "  T=$(echo $T | tr ' ' '\\n' | sort -u -n | tr '\\n' ' ')\n"
        "  [ \"$1\" = rm ] && rm -rf \"$D\"\n"
        "  kill -TERM $T 2>/dev/null; sleep 0.5\n"
        "  K=\"\"; for p in $T; do kill -0 \"$p\" 2>/dev/null && K=\"$K $p\"; done\n"
        "  [ -n \"$K\" ] && kill -KILL $K 2>/dev/null\n"
        "  return 0\n"
        "}\n"
        "\n"
        "while :; do\n"
        "  # ① 记 pid/meta（含第一轮，所以\"启动后 ~0 秒\"就记住了；$(<) 内建不 fork；\n"
        "  #    空读不覆盖旧值——一次抖动不能丢掉临终带走的能力）\n"
        "  if [ -r \"$SPID\" ]; then _pv=$(<\"$SPID\"); [ -n \"$_pv\" ] && P=$_pv; fi\n"
        "  if [ -r \"$BPID\" ]; then _pv=$(<\"$BPID\"); [ -n \"$_pv\" ] && B=$_pv; fi\n"
        "  if [ -r \"$META\" ]; then _pv=$(<\"$META\"); [ -n \"$_pv\" ] && [ -z \"$M0\" ] && M0=$_pv; fi\n"
        "  # ② 归属自检（两道）：名字被新会话接管（watch.pid 在但不是自己）/ 会话被重建（meta 变了）\n"
        "  #    ⇒ 立刻退出，绝不碰别人的会话（否则同名重建的老看门狗会把新会话收回）\n"
        "  W=\"\"; [ -r \"$WPID\" ] && W=$(<\"$WPID\")\n"
        "  [ -n \"$W\" ] && [ \"$W\" != \"$$\" ] && exit 0\n"
        "  M=\"\"; [ -r \"$META\" ] && M=$(<\"$META\")\n"
        "  [ -n \"$M0\" ] && [ -n \"$M\" ] && [ \"$M\" != \"$M0\" ] && exit 0\n"
        "  # ③ 目录消失（且没被上述自检判定为\"别人的会话\"）⇒ 临终带走：\n"
        "  #    有人 rm -rf 了会话目录时，把 starter/script/bash -i 一起收掉再自退，不留孤儿\n"
        "  if [ ! -d \"$D\" ]; then cleanup_tree; exit 0; fi\n"
        "  # ④ 正常一轮：内建睡眠（read -t + 自持 fd）+ 防 spin 护栏\n"
        "  T0=$SECONDS\n"
        "  read -t \"$TICK\" -r -u 9 _x\n"
        "  if [ $((SECONDS - T0)) -lt 1 ]; then\n"
        "    FAST=$((FAST+1))\n"
        "    if [ \"$FAST\" -ge 3 ]; then\n"
        "      echo \"guard: read -t 立刻返回，本会话退回 sleep\" >> \"$D/wd.log\" 2>/dev/null\n"
        "      sleep \"$TICK\"; FAST=0\n"
        "    fi\n"
        "    continue\n"
        "  fi\n"
        "  NOW=${EPOCHSECONDS:-$(date +%%s)}\n"
        "  # 判闲前**重新读一次** shell pid：第一轮在 read 之前（约 0.4s）bash.pid 还没被 init\n"
        "  # payload 写出来，用那时的空值会判成\"没有命令在跑\"——实测把正在 sleep 40 的会话误回收了。\n"
        "  # 另外：**pid 未知时一律视为忙**（没有 shell pid 就无法证明空闲，宁可多留也不误杀）\n"
        "  if [ -r \"$BPID\" ]; then _pv=$(<\"$BPID\"); [ -n \"$_pv\" ] && B=$_pv; fi\n"
        "  if [ -z \"$B\" ]; then printf '%%s\\n' \"$NOW\" > \"$BEAT\"; continue; fi\n"
        "  if [ -n \"$(pgrep -P \"$B\" 2>/dev/null)\" ]; then printf '%%s\\n' \"$NOW\" > \"$BEAT\"; continue; fi\n"
        "  LAST=\"\"; [ -r \"$BEAT\" ] && LAST=$(<\"$BEAT\")\n"
        "  case \"$LAST\" in ''|*[!0-9]*) printf '%%s\\n' \"$NOW\" > \"$BEAT\"; continue ;; esac\n"
        "  [ $((NOW - LAST)) -gt \"$TTL\" ] || continue\n"
        "  cleanup_tree rm\n"
        "  exit 0\n"
        "done\n"
        % (int(ttl), _SESSION_TTL_TICK, q(f["dir"]), q(f["beat"]), q(f["bash"]), q(f["pid"]),
           q(f["watch"]), q(f["meta"]), int(ttl), _SESSION_TTL_TICK, q(f["wdfifo"]),
           _SESSION_TREE_AWK % "$r"))


def _session_watchdog_launch_cmd(f, ttl):
    """把看门狗脚本落成文件并以 setsid 独立进程启动（base64 传输，绕开所有引号问题）。

    TTL<=0 时返回空串（不装看门狗）。脚本落在会话目录内，随目录一起被清掉。

    **两段式**（实测教训，两版都踩过）：
      - 早先写成"整组后台任务"`{ printf ...; chmod ...; setsid ...; } &`：组里的 `printf | base64`
        /`chmod` 继承了 SSH 通道的 stderr ⇒ 通道永不 EOF ⇒ paramiko 的 `recv_exit_status()` 一直等，
        `session start` 20 秒超时误报 `session_failed`（会话其实起好了）。
      - 改用 `{ ...; } >/dev/null 2>&1 </dev/null &` 修好了超时，但 `&&` 链整体被 `&` 后台化会多留一个
        **wrapper 进程**（argv 继承 start 命令原文、以 init 为父、还要等看门狗退出才结束），
        `watch.pid` 记的也是这个 wrapper 而不是看门狗本身 —— 实测 `ps --ppid` 才看见真正的看门狗。
      ⇒ 现在：**先在前台把脚本写好**（管道 stderr 直接丢 /dev/null，写完全部进程即退出，不占通道），
        再用**一条**自带三个重定向的后台命令启动看门狗。两段之间必须用 `;` 而不是 `&&` ——
        写成 `printf … && chmod … && setsid … &` 时 `&` 会把整条 `&&` 链一起后台化，
        又会变回"多一个 wrapper + 挂住通道"（实测踩过）。这样每个会话只多 **1 个**进程，
        `watch.pid` 就是看门狗本人。
    """
    if not ttl or ttl <= 0:
        return ""
    q = _sh_quote
    b64 = base64.b64encode(_session_watchdog_script(f, ttl).encode("utf-8")).decode("ascii")
    w = f["dir"] + "/watch.sh"
    return ("printf %%s '%s' | base64 -d > %s 2>/dev/null; chmod 700 %s; "
            "setsid nohup bash %s >/dev/null 2>&1 </dev/null & echo $! > %s; "
            % (b64, q(w), q(w), q(w), q(f["watch"])))


def _session_touch(client, f):
    """刷新会话的"最后交互时间"——把 **epoch 秒写进 beat 文件**（空闲回收据此判断"没人用了"）。

    v2.3.0 起写的是时间戳内容而不是单纯 `touch` mtime：看门狗可以用 bash 内建 `$(<beat)` 读它，
    省掉每轮一次 `stat`（少一个 fork、也少一处 GNU `stat -c` 依赖）；`list` 仍按 mtime 计算
    `idle_seconds`（写文件同样会更新 mtime，两边都成立）。

    失败不致命（旧会话没有 beat 文件、或远端写入异常）：只记一条 WARN 到 stderr，
    绝不让它影响正常调用。`list` 不算交互（看一眼不代表在用），故不调用本函数。
    """
    rc, out, err = _session_run(
        client,
        "printf '%%s\\n' \"$(date +%%s)\" > %s 2>/dev/null || true" % _sh_quote(f["beat"]),
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
        # beat 里写 epoch 秒（不是空文件）：看门狗用 $(<beat) 内建读它判闲，省掉每轮 stat
        "printf '%%s\\n' \"$(date +%%s)\" > %s; chmod 600 %s 2>/dev/null; " % (
            q(f["beat"]), q(f["beat"])) +
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
            "case \"$A\" in *\"$D/\"*) ;; *) continue ;; esac; "
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


def _session_orphan_candidates(ps_text, root, live_dirs):
    """从 `ps -eo pid=,args=` 输出里挑出"**目录已不在**、但 argv 里还带着会话路径"的会话进程。

    纯函数（便于单测）。三条同时成立才算孤儿：
      ① argv 里出现 `<root>/<名字>/`，且名字过 `_SESSION_NAME_RE`（防路径穿越/误判）；
      ② **看起来真是会话进程**——含 `script -qfc`（PTY 包装）／`<root>/<名字>/in`（starter 的 FIFO
         路径）／`<root>/<名字>/watch.sh`（空闲回收看门狗）之一。
         ③ 该名字的目录**不在** `live_dirs` 里（目录还在 ⇒ 走正常 kill 路径，不在这里重复处理）。

    为什么②不能省：argv 里"提到"会话路径的进程很多（人肉 `tail -f .../out.log`、编辑器、
    备份脚本），只按路径匹配就会误杀无关进程——这正是自证原则的延伸（argv 必须**以会话身份**出现）。

    返回 `[(pid, 名字, 类型)]`（pid 去重）。
    """
    pref = root.rstrip("/") + "/"
    out, seen = [], set()
    for line in (ps_text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, args = int(parts[0]), parts[1]
        if pid in seen or pref not in args:
            continue
        name = args.split(pref, 1)[1].split("/", 1)[0]
        if not _SESSION_NAME_RE.match(name) or name in live_dirs:
            continue
        d = pref + name
        if "script -qfc" in args:
            kind = "pty-wrapper"
        elif d + "/in" in args:
            kind = "starter"
        elif d + "/watch.sh" in args:
            kind = "watchdog"
        else:
            continue            # 只是"提到"路径的无关进程：不动
        seen.add(pid)
        out.append((pid, name, kind))
    return out


def _session_pid_kill_cmd(pid):
    """按给定 pid 的**进程树闭包** TERM→KILL（孤儿清理用）。

    pid 已经由 argv 扫描自证过身份，所以这里不需要再判一遍；闭包是为了连带清掉
    `script` 的 pty 子 shell 与它正在跑的命令（孤儿场景里这些正是残留主体）。
    """
    return ("SNAP=$(ps -eo pid=,ppid=); T=$(echo \"$SNAP\" | %s); "
            "T=$(echo $T | tr ' ' '\\n' | sort -u -n | tr '\\n' ' '); "
            "SWEPT=$(echo $T | wc -w | tr -d ' '); LEFT=0; "
            "if [ -n \"$T\" ]; then "
            "kill -TERM $T 2>/dev/null; sleep 0.5; "
            "K=\"\"; for p in $T; do kill -0 \"$p\" 2>/dev/null && K=\"$K $p\"; done; "
            "[ -n \"$K\" ] && kill -KILL $K 2>/dev/null; sleep 0.3; "
            "for p in $T; do kill -0 \"$p\" 2>/dev/null && LEFT=$((LEFT+1)); done; "
            "fi; "
            "echo \"__PYAISSH_SESS__SWEPT=$SWEPT\"; echo \"__PYAISSH_SESS__LEFT=$LEFT\"; "
            "echo __PYAISSH_SESS__DONE=1"
            % (_SESSION_TREE_AWK % str(int(pid))))


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
            with sftp.open(f["bash"], "r") as fh:
                info["shell_pid"] = fh.read().decode("utf-8", "replace").strip() or None
        except Exception:
            info["shell_pid"] = None      # 降级会话：看门狗判不了闲（它把"pid 未知"视为忙）
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
                degraded = [s for s in sessions if s.get("status") == "running"
                            and not s.get("shell_pid")]
                if degraded:
                    result["warnings"].append(
                        "会话 %s 缺 shell pid（bash.pid）——看门狗无法判断\"命令是否在跑\"，"
                        "因此**空闲回收对它不生效**（宁可不收也不误杀）：用完请 session kill"
                        % ",".join(s["session"] for s in degraded))
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

        # ---- 按 argv 扫孤儿（v2.3.0）：目录已不在、但命令行里还带着会话路径的会话进程 ----
        # 为什么需要：手工 `rm -rf` 掉会话目录（或半清理）后，进程失去 pid 记录，而上面的
        # 逐目录枚举看不见它们 ⇒ 永久残留。这里按 argv **自证身份**（starter 的 FIFO 路径 /
        # `script -qfc` / `watch.sh`）挑出来，再用"该 pid 的进程树闭包" TERM→KILL。
        # 只为"目录已不在"的会话做（目录还在 = 正常路径已处理，避免重复杀与误杀）。
        orphans = []
        scan_rc, scan_out, _scan_err = _session_run(client, "ps -eo pid=,args=", timeout=20)
        if scan_rc == 0:
            _cands = _session_orphan_candidates(scan_out, root, set(names))
            if args.name:
                _cands = [c for c in _cands if c[1] == args.name]
            for _pid, _nm, _kind in _cands:
                rc2, out2, _ = _session_run(client, _session_pid_kill_cmd(_pid), timeout=30)
                marks2 = dict(_SESSION_MARK_RE.findall(out2))
                orphans.append({"session": _nm, "via": "argv-scan", "kind": _kind, "pid": _pid,
                                "swept": int(marks2.get("SWEPT") or 0),
                                "remaining": int(marks2.get("LEFT") or 0),
                                "verified": marks2.get("DONE") == "1"
                                            and int(marks2.get("LEFT") or 0) == 0})
                if rc2 != 0 and not marks2:
                    orphans[-1]["note"] = (out2 or "")[-160:]
        result = {"ok": True, "action": "session", "version": VERSION,
                  "sessions": results, "count": len(results),
                  "remaining_total": sum(r["remaining"] for r in results),
                  "verified_total": sum(1 for r in results if r.get("verified")),
                  "host": conn["host"], "user": conn["user"], "port": conn["port"],
                  "warnings": [], "duration_ms": int((time.time() - start) * 1000)}
        if orphans:
            result["orphans"] = orphans
            result["orphans_total"] = len(orphans)
            result["orphan_remaining_total"] = sum(o["remaining"] for o in orphans)
        if result["remaining_total"]:
            result["warnings"].append(
                "仍有 %d 个进程属于被结束会话的进程树（可能正在退出或有 SIGKILL 也杀不掉的状态）"
                % result["remaining_total"])
        if orphans and result.get("orphan_remaining_total"):
            result["warnings"].append(
                "按 argv 扫到的 %d 个孤儿里仍有 %d 个进程没死（SIGKILL 也杀不掉的状态？）："
                "ps -eo pid,args | grep pyaissh-sessions 自查"
                % (len(orphans), result["orphan_remaining_total"]))
        unverified = [r["session"] for r in results if r.get("note") and not r.get("verified")]
        if unverified:
            result["warnings"].append(
                "会话 %s 的清理**未获确认**（没找到活着的会话进程，可能仍有孤儿）："
                "按 note 里的 ps 自查，必要时手工 kill 或 pyaissh session kill --all"
                % ",".join(unverified))
        result["next_action"] = ("会话已清理。重开：session start <target> --name <name>"
                                 if names else
                                 ("已按 argv 清掉 %d 个孤儿会话进程" % len(orphans) if orphans
                                  else "没有需要清理的会话"))
        _emit_result(args, result, header="[SESSION KILL] %d 个会话%s"
                     % (len(results), "，孤儿 %d" % len(orphans) if orphans else ""))
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

# ================= [域 12/13] CLI 装配与入口 ================= 
"""CLI 装配与入口（域 11）。

- PsshArgumentParser（--help 纯文本、子命令缺省结构化 bad_args）
- build_parser + add_conn（target/凭据/跳板参数，子命令共用注册）
- 参数类型校验（_port/_max_time/_exec_timeout/_positive_int/_encoding_type）
- 信号：_setup_signal_handlers（极早期注册语义在 00）、_signal_responder（救援线程）
- main()：解析 -> --field/--text 互斥 -> 子命令分发；极早期窗口标志在 00_head
"""

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
        err = {"ok": False, "error": "bad_args", "message": "参数错误: %s" % message,
               "retryable": False}  # 参数错不可重试（重试同样失败）
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


def _nonneg_int(value):
    """argparse type：非负整数校验（--offset 允许 0 = 从头读）。"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("必须为非负整数（收到 %r，如 0）" % (value,))
    if v < 0:
        raise argparse.ArgumentTypeError("必须为非负整数（>= 0）")
    return v


def _encoding_type(value):
    """argparse type：编码名校验（codecs.lookup，拼错立刻 bad_args/2，不连远端）。
    裸 str 会把错误拖到解码期 LookupError，被通用 except 误归 exec_failed 误导
    AI 查网络——解析期拦截才是"参数写错"的语义。"""
    try:
        codecs.lookup(value)
    except LookupError:
        raise argparse.ArgumentTypeError(
            "未知编码 %r（如 utf-8 / gbk / shift_jis / latin-1）" % (value,))
    return value


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
        prog="pyaissh",
        description="基于 paramiko 的命令行 SSH 工具（给 AI 用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  pyaissh exec root@1.2.3.4 --cmd 'uname -a'
  pyaissh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限（最高 1200）
  pyaissh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120      # 慢命令调大静默窗口
  pyaissh exec root@1.2.3.4 --detach --cmd 'apt install -y nginx' # 后台跑（v2.2）：立即返回 job_id
  pyaissh log root@1.2.3.4 --job-id <id> --wait-rc 300            # 读日志/等结束拿退出码/--cleanup 清理
  pyaissh exec root@1.2.3.4 --cmd-file - <<'EOF'
  ls -la /var/log
  EOF
  pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
  pyaissh upload root@1.2.3.4 --local big.bin --remote /tmp/big.bin --parallel 8  # 高丢包/长 RTT 链路分片上传（收益随链路而定；v1.5.8）
  pyaissh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
  pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8   # --local . 可用（scp 语义）
  pyaissh test root@1.2.3.4
  pyaissh ls root@1.2.3.4 --path /etc --long --limit 500

跳板机 (--jump):
  pyaissh exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-password 'xxx' --cmd 'hostname'

主机别名 (.env 配 PYAISSH_HOST_PROD=root@1.2.3.4:22 后):
  pyaissh exec @prod --cmd 'uname -a'
  # 别名专属凭据: PYAISSH_HOST_PROD_PASSWORD / PYAISSH_HOST_PROD_KEY

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
      stdout_omitted_bytes)；截断时完整输出落盘，路径见 stdout_spill_file
      （并有 next_action 直接告诉你下一步）；exec --detach 返回 job_id/log/rc，
      log 返回 stdout（合并流，与 exec 的 stdout 命名一致）/next_offset/status/exit_code
      （status 三级：finished/running/dead——被 kill 的作业也能收敛，不再永久 running）；
      upload/download 的 bytes=清单总大小、
      bytes_transferred=实际传输；ls 的 entries 含 mode/mtime(epoch 秒,UTC)/is_symlink
特性: 传输零 token 消耗——upload/download 的文件内容从不回传 JSON，AI 只消费
      元数据（files/bytes/file_list），大文件/二进制不会烧爆 LLM 上下文
环境变量 (.env 或系统): PYAISSH_USER / PYAISSH_PORT / PYAISSH_KEY / PYAISSH_PASSWORD /
                        PYAISSH_JUMP_KEY / PYAISSH_JUMP_PASSWORD / PYAISSH_HOST_<名称>
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
        p.add_argument("-u", "--user", help="用户名 (env: PYAISSH_USER)")
        p.add_argument("-p", "--port", type=_port, help="端口 (env: PYAISSH_PORT, 默认 22)")
        p.add_argument("-k", "--key", help="私钥路径 (env: PYAISSH_KEY, 默认 ~/.ssh/id_ed25519)")
        p.add_argument("-P", "--password", help="密码 (env: PYAISSH_PASSWORD)")
        p.add_argument("--timeout", type=_positive_int, default=10, help="连接超时秒数 (默认 10)")
        p.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS,
                       help="输出纯 JSON（默认已是 JSON，保留兼容）")
        p.add_argument("--text", dest="json", action="store_false", default=argparse.SUPPRESS,
                       help="可读文本模式（默认 JSON）")
        p.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                       help="消费端字段提取（打印值，不打印 JSON）：--field stdout 打印裸值；"
                            "逗号分隔多字段每行一个；- 前缀字段打到 stderr（如 --field stdout,-stderr，"
                            "stderr 内容不被 stdout 展示吞掉）；与 --text 互斥；错误路径仍输出完整 JSON")
        p.add_argument("--strict", action="store_true", help="严格校验 host key (默认 auto-add)")
        # 跳板机参数
        p.add_argument("--jump", metavar="[user@]host[:port]",
                       help="跳板机地址，通过它隧道连目标 (如 root@1.2.3.4:2222)")
        p.add_argument("--jump-password", dest="jump_password",
                       help="跳板机密码 (跳板机认证独立于目标机)")
        p.add_argument("--jump-key", dest="jump_key",
                       help="跳板机私钥路径 (默认 ~/.ssh/id_ed25519)")

    # exec
    p = sub.add_parser("exec", help="执行远程命令（长任务可用 --detach 后台化）",
                       description="执行远程命令。本地退出码 = 远程退出码；"
                                   "超时 124、连接失败 255、参数错误 2、中断 130。",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="""\
场景 → 参数（凭感觉调超时是坑，照表来；v2.2）:
  systemctl restart / nginx reload（静默 1-2 分钟）   --idle-timeout 120
  apt/yum install / docker pull / npm i（数分钟）     --idle-timeout 120 --max-time 900
  编译构建 / 大数据处理（10-20 分钟）                 --idle-timeout 300 --max-time 1200
  要"边跑边看"或超过宿主调用上限                     --detach，再用 pyaissh log 增量读
  静默但想确认还活着（不解决卡死判定）               --progress 30
  输出很大（>64KB）                                  完整输出自动落 spill，读 stdout_spill_file
  命令里有 $ 等特殊字符（PowerShell 会吃）           写脚本文件后 --cmd-file -（勿内联）
  Windows 工具写出的脚本/命令（CRLF 行尾）          默认已归一为 LF，无需 sed -i 's/\r$//'
                                                     （结果回传 crlf_normalized；要原样发加 --keep-crlf）
""")
    add_conn(p)
    p.add_argument("--cmd", help="要执行的命令")
    p.add_argument("--cmd-file", dest="cmd_file",
                   help="从文件读命令 (- 表示 stdin，适合长脚本/特殊字符)")
    p.add_argument("--keep-crlf", dest="keep_crlf", action="store_true",
                   help="保留命令文本里的 CRLF/CR 行尾（默认归一为 LF，避免远端 bash 把 \\r 当"
                        "词的一部分：$'\\r': command not found、heredoc 落盘文件带 CR）；"
                        "仅在确实要输出 CRLF 数据时用")
    p.add_argument("--detach", action="store_true",
                   help="后台运行（v2.2）：远端 setsid+nohup 起作业，立即返回 job_id/log/rc；"
                        "之后用 pyaissh log 增量读日志、--wait-rc 等结束拿退出码——"
                        "长任务不受宿主单次调用时长限制（SSH 断开作业照跑）；与 --sudo/--pty 互斥")
    p.add_argument("--job-dir", dest="job_dir",
                   help="后台作业根目录（默认 %s；每作业一个子目录）" % DEFAULT_JOB_DIR)
    p.add_argument("--idle-timeout", dest="exec_timeout", type=_exec_timeout, default=60,
                   help="静默超时秒数：连续无输出超过该值即终止，默认 60，最高 1200 "
                        "（区别于 --max-time 总时长；输出少的慢命令调大这个，"
                        "参见 --help 末尾的场景表）")
    p.add_argument("--progress", type=_positive_int, nargs="?", const=30, metavar="SECS",
                   help="长任务心跳（v2.1）：命令每静默/持续运行超过 N 秒（默认 30）往 stderr "
                        "打一行[PROGRESS]仍在运行——AI 知道进程活着不是挂死；"
                        "不重置静默计时（idle-timeout 仍按真实输出判定）")
    # 兼容别名：v1.3 前叫 --exec-timeout，名字容易被误当成"总超时"而用错
    p.add_argument("--exec-timeout", dest="exec_timeout", type=_exec_timeout,
                   default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p.add_argument("--max-time", dest="max_time", type=_max_time,
                   help="命令总时长上限秒数（wall clock；默认 2×idle-timeout 且至少 120，"
                        "不得小于 --idle-timeout；构建/编译等长任务请调大，最高 1200）")
    p.add_argument("--max-output", dest="max_output", type=_positive_int, default=DEFAULT_MAX_OUTPUT,
                   help="stdout/stderr 单流最大保留字节，超出保留头尾各一半"
                        "（默认 64KB；完整输出自动落盘并把路径回传 stdout_spill_file/"
                        "stderr_spill_file，读文件比重跑便宜——结果过大时宿主会裁掉工具结果中段）")
    p.add_argument("--no-credential-warn", dest="no_credential_warn", action="store_true",
                   help="关闭\"命令含疑似凭据\"的 WARN 提示（启发式误报时用；仍建议敏感凭据走环境变量注入）")
    p.add_argument("--sudo", action="store_true",
                   help="以 sudo 提权执行（普通用户登录时提权用）。有密码（--sudo-password 或 "
                        "PYAISSH_SUDO_PASSWORD）时组装 sudo -S -p '' bash -c '<cmd>' 并经 SSH "
                        "stdin 注入密码（命令文本不含密码）；无密码时 sudo -n bash -c '<cmd>'"
                        "（免密探测，需密码立即失败不挂）；复合命令（&&/||/;）整链提权；与 --pty 互斥")
    p.add_argument("--sudo-password", dest="sudo_password", default=None,
                   help="sudo 密码（优先于 PYAISSH_SUDO_PASSWORD 环境变量；空串视为未设置→sudo -n "
                        "免密探测；密码只经 SSH stdin 注入，不进命令文本/cmd 字段/日志/远端磁盘）")
    p.add_argument("--spill-dir", dest="spill_dir",
                   help="输出截断时把完整输出落盘的目录（默认系统临时目录；保留的文件路径见结果 "
                        "stdout_spill_file / stderr_spill_file 字段）")
    p.add_argument("--encoding", dest="encoding", type=_encoding_type, default="utf-8",
                   help="远端 stdout/stderr 的解码编码（默认 utf-8；GBK/Shift-JIS 等系统日志用 "
                        "--encoding gbk / shift_jis；非法字节以 U+FFFD 替换，不中断；拼错立即报错不连远端）")
    p.add_argument("--pty", action="store_true",
                   help="分配 PTY 伪终端运行：适用于需要 TTY 的非交互命令"
                        "(watch、top -b、sudo -n、检测 isatty 的脚本)；"
                        "注意 PTY 模式下 stderr 合并进 stdout；vi 等全屏交互程序不可用")
    p.add_argument("--pty-strip-ansi", action="store_true",
                   help="PTY 模式下剥离输出中的 ANSI 转义序列（颜色/光标），供 AI 干净解析")
    p.set_defaults(func=cmd_exec)

    # log（v2.2，别名 tail）：读 exec --detach 起的后台作业日志/状态
    p = sub.add_parser("log", aliases=["tail"], help="读后台作业日志（配合 exec --detach）",
                       description="读取 exec --detach 启动的后台作业的日志与结束状态。"
                                   "默认回传尾部 100 行；--offset 增量读（用返回的 next_offset 续读）；"
                                   "--wait-rc 等结束直接拿退出码；--cleanup 清理远端作业目录。",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="""\
典型用法（配合 exec --detach）:
  pyaissh exec h --detach --cmd 'apt install -y nginx'   # 返回 job_id/log/rc
  pyaissh log h --list                                   # 列出作业与状态
  pyaissh log h --job-id 20260911-120000-1234            # 尾部 100 行
  pyaissh log h --job-id <id> --offset 0                 # 从头增量读（返回 next_offset）
  pyaissh log h --job-id <id> --offset <next_offset>     # 接着上次读（轮询不重复）
  pyaissh log h --job-id <id> --wait-rc 30               # 等结束（≤600s）并拿退出码
  pyaissh log h --job-id <id> --kill                     # 整组停掉（TERM→宽限 5s→KILL）
  pyaissh log h --job-id <id> --kill --cleanup           # 停掉并清理（推荐收尾方式）
  pyaissh log h --job-id <id> --cleanup                  # 清理远端作业目录（运行中会被拒绝）
  pyaissh log h --job-id <id> --cleanup --force          # 明知在跑也要删（放弃追踪，进程可能仍在跑）

载荷字段（别猜错，v2.2.1 起与 exec 对齐）:
  stdout       日志内容（**合并流**：job.log 是 2>&1，stdout 与 stderr 都在这；stream 字段声明）
  next_offset  下次增量读的字节偏移；has_more 之后是否还有未读字节
  status       finished（有 job.rc，退出码见 exit_code）/ running / **dead**（无 rc 且进程已消失）
  --list 的载荷是 jobs[]（job_id/status/log_bytes/exit_code/mtime）

状态与收敛:
  finished = job.rc 存在（内容即退出码）；被 kill/OOM/崩溃的作业永不产出 rc，
  此时由 job.pid 的存活探测判定 **dead** —— 所以 --wait-rc 不会永久卡在 running。
  truncated=true 且非 --offset 模式时，中段被省略（omitted_bytes）→ 用 --offset 0 顺序读补齐
  --cleanup 只在作业已结束（finished/dead）时执行：运行中会拒绝并报 job_running——
  删掉 job.pid/job.log 会让本工具彻底失去追踪，而进程仍在远端跑（要停用 --kill 整组停）
  --force 强制清理后 job.pid 已删、--kill 不可用：要停掉远端进程直接整组杀
  kill -9 -<pid>（负号 = 进程组；pid 即 setsid 组长，子进程一并清掉，无需 pgrep）
""")
    add_conn(p)
    p.add_argument("--job-id", dest="job_id",
                   help="作业 id（exec --detach 返回；只允许字母/数字/下划线/点/连字符）")
    p.add_argument("--path", help="直接指定日志文件路径（名为 job.log 时自动配对同目录 job.rc）")
    p.add_argument("--job-dir", dest="job_dir", help="作业根目录（默认 %s）" % DEFAULT_JOB_DIR)
    p.add_argument("--list", dest="list_jobs", action="store_true",
                   help="列出作业目录下所有作业（状态/日志大小/退出码/时间）")
    p.add_argument("--lines", type=_positive_int,
                   help="回传尾部 N 行（默认 %d；与 --offset 互斥）" % JOB_TAIL_LINES)
    p.add_argument("--offset", type=_nonneg_int,
                   help="从该字节偏移增量读（配返回的 next_offset 轮询；与 --lines 互斥）")
    p.add_argument("--wait-rc", dest="wait_rc", type=_positive_int,
                   help="阻塞等待作业结束（rc 出现或进程消失）最多 N 秒（上限 %d），收敛即返回"
                        % JOB_WAIT_MAX)
    p.add_argument("--kill", action="store_true",
                   help="整组停掉作业（读 job.pid → 对进程组 TERM → 宽限 %ds → KILL）："
                        "被 kill 的作业随后判定为 dead（无退出码），状态机可收敛；需 --job-id"
                        % JOB_KILL_GRACE)
    p.add_argument("--cleanup", action="store_true",
                   help="读完后删除远端作业目录（job.sh/run.sh/job.log/job.rc/job.pid）；需 --job-id；"
                        "作业仍在运行时拒绝（会自断追踪），先 --kill 或 --wait-rc，或用 --force 放弃追踪")
    p.add_argument("--force", action="store_true",
                   help="配合 --cleanup：作业仍在运行时也强制清理（本工具不再追踪该作业，"
                        "远端进程可能仍在跑——正常应先 --kill）；清理后要停掉进程用 "
                        "kill -9 -<pid>（负号=进程组，pid 即 setsid 组长，一次清整组，无需 pgrep）")
    p.add_argument("--limit", type=_positive_int, default=50, help="--list 最多返回条数（默认 50）")
    p.add_argument("--max-output", dest="max_output", type=_positive_int, default=DEFAULT_MAX_OUTPUT,
                   help="单次回传内容上限字节（默认 64KB；截断时看 omitted_bytes，"
                        "增量读用 --offset 继续）")
    p.add_argument("--encoding", dest="encoding", type=_encoding_type, default="utf-8",
                   help="日志解码编码（默认 utf-8；GBK 日志用 --encoding gbk）")
    p.set_defaults(func=cmd_log)

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
    p.add_argument("--exclude", metavar="GLOB[,GLOB...]",
                   help="目录递归时排除匹配项（v2.1）：逗号分隔 glob，命中文件名或相对路径"
                        "即整项跳过——目录整树剪枝、文件不上传不计数；"
                        "例：--exclude node_modules,.git,'*.log'")
    p.add_argument("--resume", action="store_true",
                   help="断点续传：中断后保留远端 .part，重试从断点继续（仅单文件；"
                        "续传点基于大小，极端损坏场景可下载后 md5sum 复核；"
                        "--resume 模式禁用并发写同一目标；与 --parallel 互斥）")
    p.add_argument("--parallel", type=_positive_int, choices=range(1, 9), metavar="1-8",
                   help="单文件分片上传连接数（1-8）：高丢包/慢链路大文件提速，"
                        "对称下载分片（默认单连接；显式给出时 ≥64KB 即分片；"
                        "与 --resume 互斥）")
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
    p.add_argument("--resume", action="store_true",
                   help="断点续传：中断后保留本地 .part，重试从断点继续（仅单文件；"
                        "续传点基于大小，极端损坏场景可下载后 md5sum 复核；"
                        "--resume 模式禁用并发写同一目标）")
    p.add_argument("--parallel", type=_positive_int, choices=range(1, 9), metavar="1-8",
                   help="大文件分片下载的并行连接数 (默认自动：≥8MB 用 4，实际值见结果 "
                        "parallel_used 字段；跨境高丢包链路可试 8——若 8 失败"
                        "（链路饱和/服务器 MaxStartups 限制）反试 4/2)")
    p.set_defaults(func=cmd_download)

    # test
    p = sub.add_parser("test", help="测试连接",
                       description="测试 SSH 连接，返回连接状态和服务器信息。")
    add_conn(p)
    p.add_argument("--encoding", dest="encoding", type=_encoding_type, default="utf-8",
                   help="服务器信息输出的解码编码（默认 utf-8；GBK 系统 os-release 等用 --encoding gbk）")
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

    # host（v2.1）：主机别名管理——host add 把别名写进 .env；remove/list（v2.1.3）
    p = sub.add_parser("host", help="主机别名管理 (host add/remove/list)",
                       description="host add：把主机别名与专属凭据写进脚本同目录 .env，"
                                   "之后 pyaissh exec @NAME 直接使用（多主机不同密码不再"
                                   "逐条 --password）；remove/list 管理已配别名。")
    hsub = p.add_subparsers(dest="host_cmd", metavar="{add,remove,list}")
    ha = hsub.add_parser("add", help="添加/更新主机别名",
                         description="例: pyaissh host add prod root@203.0.113.10 --password xxx"
                                     "  → 之后 pyaissh exec @prod 使用别名凭据")
    ha.add_argument("name", help="别名（字母/数字/下划线，不区分大小写）")
    ha.add_argument("host_target", metavar="USER@HOST[:PORT]",
                    help="目标（必须带用户名，如 root@1.2.3.4:22）")
    ha.add_argument("--password", dest="password", default=None,
                    help="该主机专属密码（写 .env；不给则复用全局 PYAISSH_PASSWORD/私钥）")
    ha.add_argument("--key", dest="key", default=None,
                    help="该主机专属私钥路径（写 .env；与密码同时给时 KEY 优先）")
    ha.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field alias 得 @prod；dict/list JSON 序列化）"
                         "——host 与各子命令统一（v2.1.2）")
    ha.set_defaults(func=cmd_host_add)

    hr = hsub.add_parser("remove", help="删除主机别名（含专属密码/密钥）",
                         description="例: pyaissh host remove prod  → 从 .env 删该别名（含其专属密码/密钥行）")
    hr.add_argument("name", help="别名")
    hr.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field removed）")
    hr.set_defaults(func=cmd_host_remove)

    hl = hsub.add_parser("list", help="列出已配置别名（只列 host 行，不回显密码/密钥）",
                         description="例: pyaissh host list  → entries[{name,target}]；--field entries 只取清单")
    hl.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field entries 得清单 JSON）")
    hl.set_defaults(func=cmd_host_list)

    # session（v2.3）：真 PTY 常驻会话——逐条喂命令 + 状态保留 + 可中断
    sp = sub.add_parser("session", help="常驻会话（真 PTY）：逐条喂命令、状态保留、可中断",
                        description="远端一个常驻 shell（util-linux script 给真 PTY）："
                                    "每条命令独立退出码，cd/export 等状态跨命令保留，"
                                    "执行中的命令可 ctrl-c 中断。子命令：start/send/read/"
                                    "ctrl-c/keys/list/kill。",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog="""\
典型流程（打错了就改对**再发一遍**，状态还在——像人在终端里那样）:
  pyaissh session start h --name work                    # 起会话（返回 pid/pty/log）
  pyaissh session run   h --name work --cmd 'cd /opt/app && git pull'   # 跑一条并等结果（一次调用）
  pyaissh session run   h --name work --cmd 'make -j8' --wait-rc 5      # 状态还在（cwd 仍是 /opt/app）
  pyaissh session ctrl-c h --name work                   # 中断正在跑的 make（会话不死）
  pyaissh session keys  h --name work --data 'y\\n'       # 应答程序提示（y/n、密码等）
  pyaissh session kill  h --name work                    # 结束会话（进程树全清 + 删目录）
  pyaissh session kill  h --all                          # 清掉该主机上全部会话（含忘了关的）

与 exec / exec --detach 的分工:
  exec              一次一条、无状态（cd/export 不跨调用）、stdout/stderr 分离、零残留
  exec --detach     一条长命令丢后台，启动后不能改，错了只能 --kill 重启（可中断但无状态）
  session           多步·需状态·可能要中断·要应答提示：逐条喂 + 状态保留 + ctrl-c + keys
                    （run = send + 等结果，一步一次调用；send/read 分离时用于增量读）

会话的生命周期（重要）:
  会话是 setsid+nohup 起的**远端常驻进程，不会自己退出**（SSH 断开也照跑）——
  用完必须 session kill（默认连目录一起删，--keep-dir 保留日志）；
  会话里 exit 掉、或进程被 OOM/外力杀掉时，进程会消失但 /tmp 下的目录与日志仍在。
  list 会给你 age_seconds/log_bytes，挂了超过 24 小时的会额外提示。

载荷字段: stdout（合并流，已清洗 CR/ANSI/哨兵行）/ next_offset / status(done|running)
          / exit_code / token / session / pid / pty
""")
    ss = sp.add_subparsers(dest="session_cmd", metavar="start|send|read|ctrl-c|keys|list|kill")

    ssp = ss.add_parser("start", help="起会话（真 PTY；缺 script 时降级为非 PTY）",
                        description="setsid+nohup 起常驻 shell：SSH 断开不影响；"
                                    "有 util-linux script 则分配真 PTY（可跑需要 tty 的程序）。"
                                    "**空闲回收**：提示符空闲（没有命令在跑）且 TTL 内没有任何 pyaissh "
                                    "交互时，会话自动回收（进程 + 目录）——默认 600 秒，--ttl 0 关闭")
    add_conn(ssp)
    ssp.add_argument("--name", default="main", help="会话名（默认 main；字母/数字/._-）")
    ssp.add_argument("--session-dir", dest="session_dir", help="会话根目录（默认 %s）"
                     % DEFAULT_SESSION_DIR)
    ssp.add_argument("--cols", type=_positive_int, default=200, help="PTY 列宽（默认 200，防折行）")
    ssp.add_argument("--no-pty", dest="no_pty", action="store_true",
                     help="强制非 PTY（无 tty，但状态与退出码照常）")
    ssp.add_argument("--ttl", help="空闲回收秒数（默认 600=10 分钟；可写 30s/10m/2h；0 = 关闭回收）；"
                                   "也可用环境变量 PYAISSH_SESSION_TTL")
    ssp.add_argument("--attach", action="store_true",
                     help="同名会话还活着就接上（返回 attached=true + pid/age），不存在才新建")
    ssp.add_argument("--wait-ready", dest="wait_ready", type=_positive_int,
                     default=SESSION_READY_WAIT, help="等会话就绪秒数（默认 %d）" % SESSION_READY_WAIT)
    ssp.set_defaults(func=cmd_session_start)

    sse = ss.add_parser("send", help="把一条命令喂进会话（自动追加退出码哨兵）",
                        description="命令 + 哨兵写入会话 FIFO；用返回的 token/offset 去 read")
    scur = ss.add_parser("run", help="会话内跑一条命令并等它结束（send+等待，一次调用）",
                         description="喂命令 + 等哨兵 + 回传这条命令的输出与 exit_code——"
                                     "会话式的「一步一次调用」。--no-wait 则只发送（等价 send）")
    add_conn(scur)
    scur.add_argument("--name", default="main", help="会话名（默认 main）")
    scur.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    scur.add_argument("--cmd", help="要执行的命令")
    scur.add_argument("--cmd-file", dest="cmd_file", help="从文件读命令 (- 表示 stdin)")
    scur.add_argument("--keep-crlf", dest="keep_crlf", action="store_true",
                      help="保留命令文本里的 CRLF（默认归一为 LF，与 exec 同规则）")
    scur.add_argument("--wait-rc", dest="wait_rc", type=_positive_int,
                      default=None, help="最多等 N 秒（默认 %d，上限 %d）；超时返回 status=running"
                      % (SESSION_RUN_WAIT, SESSION_WAIT_MAX))
    scur.add_argument("--no-wait", dest="no_wait", action="store_true",
                      help="只发送不等待（等价 send，之后自己 read）")
    scur.add_argument("--max-output", dest="max_output", type=_positive_int,
                      default=DEFAULT_MAX_OUTPUT, help="单次回传上限字节（默认 64KB）")
    scur.add_argument("--keep-ansi", dest="keep_ansi", action="store_true",
                      help="保留 ANSI 颜色码（默认剥离，便于 AI 解析）")
    scur.set_defaults(func=cmd_session_run)
    add_conn(sse)
    sse.add_argument("--name", default="main", help="会话名（默认 main）")
    sse.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    sse.add_argument("--cmd", help="要执行的命令")
    sse.add_argument("--cmd-file", dest="cmd_file", help="从文件读命令 (- 表示 stdin)")
    sse.add_argument("--keep-crlf", dest="keep_crlf", action="store_true",
                     help="保留命令文本里的 CRLF（默认归一为 LF，与 exec 同规则）")
    sse.set_defaults(func=cmd_session_send)

    ssr = ss.add_parser("read", help="读会话输出（尾部/增量/等某条命令结束）",
                        description="默认回传尾部 %d 行；--offset 增量读；"
                                    "--wait-rc 等到哨兵出现并回 exit_code" % SESSION_DEFAULT_LINES)
    add_conn(ssr)
    ssr.add_argument("--name", default="main", help="会话名（默认 main）")
    ssr.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    ssr.add_argument("--offset", type=_nonneg_int, help="从该字节偏移增量读（与 --lines 互斥）")
    ssr.add_argument("--lines", type=_positive_int, default=None,
                     help="回传尾部 N 行（默认 %d；与 --offset 互斥）" % SESSION_DEFAULT_LINES)
    ssr.add_argument("--wait-rc", dest="wait_rc", type=_positive_int,
                     help="等待命令结束最多 N 秒（上限 %d）" % SESSION_WAIT_MAX)
    ssr.add_argument("--token", help="只等这个 token 的哨兵（send 返回的 token）")
    ssr.add_argument("--max-output", dest="max_output", type=_positive_int,
                     default=DEFAULT_MAX_OUTPUT, help="单次回传上限字节（默认 64KB）")
    ssr.add_argument("--keep-ansi", dest="keep_ansi", action="store_true",
                     help="保留 ANSI 颜色码（默认剥离，便于 AI 解析）")
    ssr.set_defaults(func=cmd_session_read)

    ssc = ss.add_parser("ctrl-c", help="中断会话里正在执行的命令（会话不死，状态保留）",
                        description="对命令自己的进程组发 SIGINT（--force 用 SIGKILL）。"
                                    "PTY 下 bash 有 job control，每条命令独立进程组")
    add_conn(ssc)
    ssc.add_argument("--name", default="main", help="会话名（默认 main）")
    ssc.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    ssc.add_argument("--force", action="store_true", help="用 SIGKILL（对忽略 SIGINT 的命令）")
    ssc.set_defaults(func=cmd_session_ctrl_c)

    ssk = ss.add_parser("keys", help="向会话注入按键/文本（应答提示、Ctrl-D）",
                        description="原始字节写入会话；中断命令请用 ctrl-c（信号≠按键）")
    add_conn(ssk)
    ssk.add_argument("--name", default="main", help="会话名（默认 main）")
    ssk.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    ssk.add_argument("--data", help="要注入的文本（支持 \\n \\r \\t \\xNN 转义）")
    ssk.add_argument("--cmd-file", dest="cmd_file", help="从文件读原始字节 (- 表示 stdin)")
    ssk.add_argument("--raw", action="store_true", help="--data 不做转义解析（原样发送）")
    ssk.set_defaults(func=cmd_session_keys)

    ssl = ss.add_parser("list", help="列该主机的会话（存活/pty/日志大小/最后活动）")
    add_conn(ssl)
    ssl.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    ssl.set_defaults(func=cmd_session_list)

    ssz = ss.add_parser("kill", help="结束会话（按 sid 全量清理，默认连目录一起删）",
                        description="以自证的会话进程（sess.pid / bash.pid / script）为根算进程树闭包，"
                                    "TERM → 校验 → KILL 残留：只杀 leader 进程组会留 job 孤儿，"
                                    "只按 sess.pid 会在 starter 已被 OOM/外力杀掉时漏掉 reparent 的孤儿")
    add_conn(ssz)
    ssz.add_argument("--name", help="会话名")
    ssz.add_argument("--all", action="store_true",
                     help="结束该主机全部会话；并**按 argv 扫描孤儿**（目录已被删、只剩进程的会话）")
    ssz.add_argument("--session-dir", dest="session_dir", help="会话根目录")
    ssz.add_argument("--keep-dir", dest="keep_dir", action="store_true",
                     help="保留会话目录（只杀进程，便于事后看 out.log）")
    ssz.set_defaults(func=cmd_session_kill)

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
                       "未指定子命令（可选: exec / log / upload / download / test / ls / host）")
            parser.print_help(sys.stderr)
            return 2
        # --field 与 --text 互斥（--text 要可读包裹、--field 要裸字段值；
        # --json 是兼容 no-op 不冲突）
        if getattr(args, "field", None) and not getattr(args, "json", True):
            emit_error(True, "bad_args",
                       "--field 与 --text 互斥（--field 是消费端字段提取，"
                       "--text 是可读模式；二选一）")
            return 2
        # --field 消费端模式：静音进度日志（stderr 只留 WARN 与盲区提示等信号）
        if getattr(args, "field", None):
            global _QUIET
            _QUIET = True
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
