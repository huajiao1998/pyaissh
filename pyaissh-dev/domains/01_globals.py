"""全局常量与可变状态容器（域 01）。

- 超时/轮询/缓冲上限常量（MAX_TIME_CAP / PARALLEL_MIN_SIZE / SFTP_IO_TIMEOUT ...）
- _RETRYABLE_ERRORS：错误类型 -> 是否可重试（emit_error 用它给 retryable 字段）
- 模块级可变容器：_ACTIVE_TRANSPORTS（活动连接）、_PUT_RESIDUE_WARNINGS（.part 残留警告）
被 00_head（信号区）、各 cmd_*（超时/常量）引用；拼接后与本包其余域同模块共享命名空间。
"""

VERSION = "2.1.1"

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
DEFAULT_MAX_OUTPUT = 262144  # exec 单流默认最大保留字节（256KB：内存缓冲与显示截断同源）
BUF_ALIGN_WINDOW = 4096      # 截断行对齐时回退搜索窗口（字节）
MIN_BUF_FLOOR = 4096         # 内存缓冲下限：max(args.max_output, 4096) 保证小档位也有可用缓冲
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

