# pyaissh 错误类型与退出码（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON）以 SKILL.md 为准，本文是退出码与 error 类型的**完整**参考表。
> **何时读**：任何失败场景需要精确分类与建议动作时；SKILL.md 只留高频摘要。

## retryable 字段（v1.5.6，错误 JSON 恒有）

错误 JSON 统一带 `retryable`（bool）：**机器可读的重试决策**——AI 自动重试策略直接读它，不必解析 message 文本。

- **`true`** = 同类错误重试可能成功且重试本身安全：`connection_timeout` / `connection_refused` / `connection_failed` / `dns_failed` / `connection_lost` / `interrupted` / **所有 SFTP 传输/超时类**（`upload_failed` / `download_failed` / `upload_timeout` / `download_timeout` / `ls_failed` / `ls_timeout`）/ `test_failed` / `exec_idle_timeout` / `exec_total_timeout` / `exec_timeout` / **`tmux_failed`**（session 引擎：tmux server/socket 起不来这类环境-资源问题，值得重试）
- **`false`** = 重试无意义或需先改输入：`auth_failed`（凭据错，重试浪费）/ `host_key_rejected`（安全）/ `bad_args`（参数错）/ `read_cmd_failed`（本地文件）/ `exec_failed`（命令本身失败）/ `jump_failed`（混合原因，保守，message 说明具体）/ `tmux_missing` / `tmux_unsupported`（必须先装/升级 tmux，直接重试同样失败）
- **`exec_*_timeout` 的 `retryable=true` 仅表示"值得一试"**：远程进程可能仍在运行、命令可能有副作用——重试前必须读 message 的"远程进程可能仍在运行"提示并先 pgrep 确认/清理（bool 给机器"值不值得试"，message 给"怎么试才安全"）。`interrupted` 同理，非幂等命令谨慎重试
- 成功结果**无** `retryable` 字段（仅错误 JSON 恒有）

## 退出码语义（完整版）

| 码 | 含义 | 说明 |
|---|---|---|
| 0 | 成功 | |
| 1 | 传输错误 / SFTP 超时 | upload/download/ls（`upload_failed`/`download_failed`/`ls_failed`/`*_timeout`） |
| 2 | **所有参数错误**：argparse 层（未知参数/缺参数/`--port` 非数字/0/负值/越界/超时参数非整数）、exec 缺 `--cmd`、`--cmd-file` 读取失败（`read_cmd_failed`）、缺用户名/主机、target 内嵌端口非法、别名未配置、路径不存在或含通配符（`~user` 形式也归此类）、**session 类状态错**（`session_not_found`/`session_dead`/`session_exists`/非法会话名） | 均报 `bad_args` 或 `read_cmd_failed`；`--port 0`/负值在**命令行**直接报错退出 2，**静默回退 22 仅发生在 `PYAISSH_PORT` 环境变量**（越界也回退并打 WARN） |
| 124 | **exec 超时**（对齐 GNU timeout 惯例） | `exec_idle_timeout`（连续无输出超 `--idle-timeout`）或 `exec_total_timeout`（总时长超 `--max-time`）；**远程进程可能仍在运行**（断开不会杀掉它），副作用命令重试前先 pgrep 确认/清理 |
| 130 | 用户中断（Ctrl+C / SIGTERM，仅 POSIX） | 超时机制杀子进程（`subprocess.terminate()`/`timeout` 命令发 SIGTERM）同样走此路径。v1.4.0 起信号处理器**只置标志不再抛异常**（在 paramiko C 级 I/O 中抛 KI 会导致锁损坏死锁），由救援线程强断连接 + Python 轮询点检查标志，串行/并行/上传/下载/exec/test 全部可靠 130 + `interrupted` JSON + 零本地残留（v1.4.5 起 `test`/`--cmd-file -`/exec 排水阶段也覆盖，不再有信号被吞返回假成功）；`interrupted` 消息区分来源（`用户中断（SIGTERM）`/`用户中断（SIGINT）`）；**慢链路分片下载中断也秒级退出**（v1.4.3 起分片 worker 与主线程 join 均带信号检查，不再拖到 120s 看门狗）；中断路径硬退出（跳过解释器关闭阶段，退出码确定）；**Windows 的 terminate() 是硬杀不走信号**，无 JSON 无清理（调用方应靠 `--max-time` 兜底而非外部强杀） |
| 254 | exec 成功但远程退出码恰为 255 | 255 保留给连接失败语义；JSON 的 `local_exit_code` 字段即本地实际退出码（254），`exit_code` 仍是远程真实值 255。**歧义提示**：本地退出码 254 可能是"远程真实 254"或"远程 255 的映射"——区分只看 JSON 的 `exit_code`/`local_exit_code` 双字段（纯 `$?` 消费者无法区分，契约要求决策以 JSON 为准） |
| 255 | 连接失败，以及 exec/test/log/session 的执行期错误（`exec_failed`/`connection_lost`/`test_failed`/`detach_failed`/`log_failed`/`kill_failed`/`session_failed`/`send_failed`/`keys_failed`/`session_read_failed`/**`tmux_missing`/`tmux_unsupported`/`tmux_failed`**） | **退出码仅作粗筛，决策一律以 JSON `error` 字段为准** |

## 错误类型（JSON `error` 字段）与建议动作（完整表）

| error | 含义 | 建议动作 |
|---|---|---|
| `bad_args` | 参数错误（退出码 2） | 检查 target/命令/路径是否齐全正确（缺用户名/主机、端口非法、目标格式错误如缺 `]`/多冒号非 IPv6、路径不存在、路径含通配符（SFTP 无 glob，先 ls 拿明确文件名）、`~user` 形式、`--max-time < --idle-timeout` 都归此类，退出码统一 2） |
| `auth_failed` | 认证失败 | 换密码或私钥；检查用户名；确认密钥文件存在 |
| `connection_timeout` | 连接超时（退出码 255）：TCP connect 超时，**或 SSH banner 超时**——TCP 已连接但服务器在 `--timeout` 窗口内未发送 banner（多为慢速/过载服务器或链路丢包，消息会注明"SSH banner 超时"） | 检查地址/端口/网络，可加大 `--timeout` 重试 |
| `dns_failed` | DNS 解析失败（退出码 255） | 主机名可能拼错，检查拼写/DNS |
| `connection_refused` | 连接被拒绝（退出码 255） | 目标端口没有 SSH 服务监听，检查端口/NAT 映射是否写对；**TCP 可达但快速收到错误 banner（端口连到别的服务）也归此类**，消息会注明"未收到 SSH banner"（慢速/无响应导致 banner 超时的归 `connection_timeout`） |
| `connection_failed` | 无法连接（兜底） | 检查主机可达性、端口是否开放 |
| `host_key_rejected` | host key 不匹配 | 确认目标无误后清理 `~/.ssh/known_hosts` 再试 |
| `jump_failed` | 跳板机隧道失败（sshd 拒绝转发/目标端口不通） | 检查跳板机 sshd 是否允许转发（AllowTcpForwarding/PermitOpen）与目标端口；**跳板机自身凭据/连接问题**报 `auth_failed`/`connection_timeout` 且 message 带 `[跳板机 user@host]` 前缀，注意与目标机错误区分 |
| `exec_idle_timeout` | 命令**连续无输出**超过 `--idle-timeout`（退出码 124） | 命令可能挂死或输出极少；**远程进程可能仍在运行**——副作用命令（apt/写文件/发请求）重试前先 pgrep 确认/清理，避免双份执行；确认命令只是输出少就调大 `--idle-timeout` |
| `exec_total_timeout` | 命令**持续输出但总时长**超过 `--max-time`（退出码 124） | 长任务用 `--max-time` 调大（最高 1200）；更久的用 `nohup` 后台化 + 轮询；**远程进程可能仍在运行**，重试前先 pgrep 确认/清理 |
| `exec_timeout` | 非本工具判定的超时（paramiko 内部超时等，退出码 124） | 看 `message` 判断是网络还是命令问题 |
| `exec_failed` | 执行异常（退出码 255） | 查看 `message` 字段 |
| `connection_lost` | 执行中连接中断，未收到退出状态，**输出可能不完整** | 不要信任部分结果，重跑命令核对 |
| `interrupted` | 用户中断（Ctrl+C 或 SIGTERM），退出码 130 | 任务被手动/超时机制终止；错误 JSON 带已读到的部分进展——exec 为 `stdout`/`stderr`，upload/download 为已传 `file_list`（upload 的 `bytes_transferred` 为真实已传字节）——据此判断命令是否已部分执行，`rm`/`apt install`/`git push` 等非幂等命令**谨慎重试**；上传中断可能残留 `.part`（warnings 会明示路径与清理命令），重试前先清理；下载中断不留 `.part` 残留 |
| `ssh_error` | SSH 协商/协议错误（`--strict` 下新主机不在 known_hosts 时常见） | 查看 `message`；`--strict` 场景先确认主机或清理 known_hosts |
| `detach_failed` | `exec --detach` 启动后台作业失败（退出码 255）：启动命令非零退出，或启动后 0.3s 内进程即死且未写 rc | 消息带日志尾巴定位（命令不可执行/语法错/作业目录不可写）；远端可能残留作业目录，`log --list` 可见或手工删除 |
| `job_not_found` | `log` 读不到作业日志（退出码 2） | 作业 id 拼错、已被 `--cleanup` 清理，或 `--job-dir` 不一致；用 `log --list` 看现有作业 |
| `job_running` | `log --cleanup` 时作业**仍在运行**（退出码 2） | 防自断追踪：删掉 `job.pid`/`job.log` 后工具再也看不到该作业，而进程仍在远端跑。正路：`--kill --cleanup`（整组停掉再清理）或 `--wait-rc` 等结束后清理；确要放弃追踪：`--cleanup --force`（留痕 `forced_cleanup`+warnings）|
| `log_failed` | `log` 读取期错误（退出码 255） | 看 `message`（多为 SFTP 权限/路径问题）；修正后重读 |
| `kill_failed` | `log --kill` 拒绝或失败（退出码 255） | 看 `message` 与 `reason`：`no_pid`（无 job.pid：作业可能已结束/被清理）、`already_gone`（进程已不在）、`pid_mismatch`（pid 已复用给别的进程——为防误杀而拒绝，需人工确认）、`kill_failed`（TERM 未发出，多为权限）|
| `tmux_missing` | **远端没有 tmux**（`command -v tmux` 失败；session 的每个子命令入口都会先做这道预检）：PTY / 进程生命周期 / 输出镜像都依赖 tmux ≥ 3.0；**退出码 255、`retryable=false`** | `message`/`next_action` 直接给可执行安装命令：Debian/Ubuntu `apt-get install -y tmux`、RHEL/CentOS `dnf install -y tmux`、Alpine `apk add tmux`；**pyaissh 不自动安装**；装不了的环境（不可变系统/无包管理器/air-gapped）改用 `exec` / `exec --detach` 跑长任务 |
| `tmux_unsupported` | 远端 tmux **主版本 < 3.0**（退出码 255、`retryable=false`）：引擎依赖 `window-size manual` 与 `#{pane_pipe}`；结果里带 `tmux_version`/`required` | 升级后重试（`apt-get install -y --only-upgrade tmux`）；`message` 里给的是可执行命令 |
| `tmux_failed` | tmux **server/socket 起不来**（`TMUX_TMPDIR`/socket 目录不可写、`new-session` 非零退出），或 `tmux -V` 输出无法解析（退出码 255、**`retryable=true`**） | 看 `message`/`tmux_version`：确认远端 `/tmp/tmux-<uid>`（或 `TMUX_TMPDIR` 指向的目录）可写、磁盘未满，然后重试；持续失败先用 `exec` 手工跑一次 `tmux -L pyaissh -f /dev/null ls` 看真实报错 |
| `session_exists` | `session start` 时同名会话已在运行（退出码 2） | 换 `--name`，或先 `session kill --name <名>`；`message` 里带现有 pid |
| `session_not_found` | `session read/send/run/ctrl-c/keys` 找不到会话（退出码 2）：会话目录里连 `meta` 都没有（从没起过，或**已被空闲回收**） | 会话名拼错、已被 `kill` 清理或空闲回收，或 `--session-dir` 不一致；用 `session list` 看现有会话；先 `session start`（要接上还活着的旧会话加 `--attach`） |
| `session_dead` | 会话目录还在但 **tmux 会话已消失**（退出码 2）：会话里 `exit`/`Ctrl-D`、被人 `kill-session`、宿主重启 | 会话状态**不可恢复**：`session kill` 清掉残留目录后重新 `start`（`list` 里这类会话是 `dead`）；没有 `tmux` 名文件的陌生目录一律不碰（reaper/惰性扫都跳过） |
| `session_failed` | `session start` 失败（退出码 255）：会话目录建不出来（权限/磁盘）、`new-session` 之后拿不到 pane pid、初始化载荷迟迟不就绪 | 看 `message`/`stderr`：确认会话根目录（默认 `/tmp/pyaissh-sessions`，或 `--session-dir`）可写、远端有 `bash`；tmux 自身的缺失/版本过低/server 起不来会分别报 `tmux_missing`/`tmux_unsupported`/`tmux_failed`，不归本类 |
| `send_failed` | `session send`/`run` 的命令没能灌进会话（退出码 255）：tmux `load-buffer`/`paste-buffer` 失败（会话可能刚好退出） | 用 `session list` 确认会话仍是 `running`，必要时 `session start` 重开；持续失败先 `exec` 手工跑一次 `tmux -L pyaissh -f /dev/null ls` 看 server 是否还在 |
| `keys_failed` | `session keys` 的按键/文本没能灌进会话（退出码 255）：同 `send_failed`（tmux 缓冲区失败、会话可能刚退出） | 同上；另确认 `--data`/`--cmd-file` 至少给了一个 |
| `session_read_failed` / `session_ctrl_c_failed` / `session_list_failed` / `session_kill_failed` | 会话读取/中断/列举/清理期错误（退出码 255） | 多为 SFTP/权限/远端命令异常；看 `message`，必要时先用 `session list` 看状态 |
| `internal_error` | 工具内部未预期异常（理论不可达，兜底分支） | 属于 pyaissh 自身缺陷：把 stderr 的 traceback 与复现命令反馈给维护者；可安全重试 |
| `read_cmd_failed` | `--cmd-file` 读取失败（退出码 2） | 检查文件路径与编码 |
| `dependency_missing` | 本地依赖缺失（如未安装 paramiko），退出码 255，`retryable=false` | **本地环境问题，与目标主机/网络无关**——`pip install paramiko` 安装后重试；重试前无需排查网络/目标机（v1.5.6 起独立分类；此前误归 `connection_failed` 且 retryable=true 导致 AI 白白重试） |
| `upload_failed` / `download_failed` | 传输失败 | 查看 `message`（多为权限/磁盘/网络问题）；错误 JSON **恒带** `host`/`user`/`port` 和**已完成的 `file_list`**（含 transferred/skipped 状态，中断后直接全量重试或按清单断点重试都安全——下载/上传均 `.part` 原子收尾，不留半截最终文件）；`upload_timeout`/`download_timeout`/`ls_timeout`/`ls_failed`/`test_failed` 同样带 `host`/`user`（传输类还带 `port`/`file_list`/`warnings`） |
| `upload_timeout` / `download_timeout` | SFTP 传输超时（30s 无数据，NAT/网络静默断开；分片下载为 120s/片） | 检查网络/防火墙；**download 超时优先加 `--parallel 8` 重试**（高丢包/跨境链路单连接吞吐塌陷，多连接近似线性提速） |
| `ls_failed` / `ls_timeout` / `test_failed` | ls 兜底错误 / ls 的 SFTP 30s 无数据超时（退出码 1）/ test 兜底错误 | 查看 `message`（多为权限/磁盘/网络问题）；`ls_timeout` 查网络后重试 |

> **凭据 WARN**：命令含疑似凭据时打的 WARN（`warnings` 字段，不阻断执行）其判定形态与已测正反例，见 `pyaissh.py` 中 `_SENSITIVE_CMD_RE` 定义处注释（29 条验收案例，改判定规则时对照自查）。
