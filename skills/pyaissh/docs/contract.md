# pyaissh 输出约定完整版（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：SKILL.md 只留输出契约的核心几条，本文是字段/截断/warnings/`--text` 标记的**完整**参考。
> **何时读**：需要精确解析 JSON 字段、判断截断、使用 `--text` 可读模式、理解 warnings 语义时。

## 解析规则（完整版）

- **stdout 才是可解析结果**；进度日志全部在 stderr，不要拿 stderr 当结果
- **默认即 JSON**：stdout 输出**整行 JSON**，直接 `json.loads` 解析（`--json` 为兼容保留、`--text` 切可读模式）。成功时字段含 `ok` / `action` / `version` / `host` / `user` / `port` / `duration_ms`，其余按子命令不同（exec 另含 `exit_code`/`exit_success`/`stdout`/`stderr`/`cmd`/`pty` 等，test 含 `hostname`/`os` 等，ls 含 `entries`/`count`/`total`，upload/download 含 `files`/`file_list`/`bytes`/`bytes_transferred`/**`parallel_used`**（v1.5.8 起：实际分片连接数——单连接=1，分片=--parallel 值；下载与上传结果都有，AI 确认档位用，见 docs/transfer.md）等）；**错误 JSON 分两类**：连接期错误（`auth_failed`/`connection_timeout`/`connection_refused`/`dns_failed`/`connection_failed`/`host_key_rejected`/`jump_failed`/`bad_args`）带 `ok`/`error`/`message`/**`retryable`**（v1.5.6 起**错误 JSON 恒有**，bool：机器可读的重试建议，true=同类错误重试可能成功且安全，false=重试无意义或需先改输入；exec 超时类 true 仅表示值得一试，重试前必须读 message 确认远程进程/副作用，详见 docs/errors.md），连接解析成功后另带 `host`/`user`（resolve 阶段失败如缺用户名/主机无这两个字段）；exec 执行期错误（`exec_idle_timeout`/`exec_total_timeout`/`exec_timeout`/`exec_failed`/`connection_lost`/`interrupted`）额外**恒带** `stdout`/`stderr`/`output_incomplete`/`cmd`（可能为空串），有警告时带 `warnings`；upload/download 的传输类失败**恒带** `host`/`user`/`port`/`file_list`/`files`/`bytes`/`bytes_transferred`/`warnings`。解析统一用 `.get()` 或按 `ok` 分流
- **参数写错时**（argparse 层，如未知参数、缺 `--local`、超时/端口参数非法）：stdout 输出 `bad_args` JSON——默认为裸 JSON 行，`--text` 模式为 `---ERROR.<nonce>---` + JSON + `---END.<nonce>---` 包裹，退出码 2；`--help` 是纯文本输出（非 JSON），`--version` 输出一行 JSON（`{"ok":true,"action":"version","version":"..."}`）
- **`--text` 与 `--json` 对称**：默认 JSON 模式；`--text` 显式切回可读模式（两个位置都能写：主命令前或子命令后）
- upload/download 结果含 `file_list`（`dry-run` 时即预览清单）；exec 含 `stdout_truncated`/`stderr_truncated`（该流是否截断）、`stdout_omitted_bytes`（省略量）/`output_truncated`（任一流截断即 true）/ `warnings` / `pty` / **`cmd_truncated`**（`cmd` 回显是否被截断——超 `CMD_ECHO_LIMIT`（8KB）时 `cmd` 保留头尾 + 中间省略标记，完整命令见原始调用，`--cmd-file` 时为本地文件可重读；凭据检测不受影响）；test 含 `hostname` / `os` / `kernel` / `arch`；ls 含 `entries` / `count`（本次显示数）/ `total`（实际总数）/ `truncated`（是否因 `--limit` 截断）——`count != total` 时目录没列全；**`entries[]` 两种模式恒同构**：`name`（目录带 `/` 后缀）/ `mode` / `size`（**目录为 null**，目录项尺寸无内容意义）/ `is_dir` / `is_symlink` / `mtime`（**epoch 秒，UTC**，跨机比较无时区歧义）
- **`ok` 与 `exit_success` 语义（必须区分）**：`ok=true` 只表示工具操作成功（连接+执行完成）；**远程命令成败看 `exit_success`**（远程退出码是否为 0）。例：命令 `exit 3` 返回 `ok=true, exit_code=3, exit_success=false`——命令失败了
- **`warnings` 数组**：exec 成功时以下警告会进 JSON 的 `warnings` 字段——输出截断（后台进程占用/`--max-output` 截断/内存缓冲丢弃）、总时长上限被 cap 到 1200、`--cmd`/`--cmd-file` 同给、疑似凭据、远程退出码 255 特殊语义、**命令文本行尾被归一（CRLF→LF，见下）**；其余日志类警告（端口配置、MSYS 路径转换）只打 stderr。**`warnings` 恒为参考信息，不代表操作失败**——疑似凭据等安全类提示不阻断执行（命令照常运行），不要因 warnings 过度保守拒绝合法命令；需要行动的警告（如上传中断的 `.part` 残留）会附具体清理命令
- **`crlf_normalized`（v2.2.4，仅在有归一发生时出现）**：命令文本里被归一为 LF 的 CRLF/CR 行尾**处数**（exec 与 exec --detach 都会回传）。默认行为：命令文本（内联 `--cmd`、`--cmd-file`、stdin）的行尾 CRLF/CR 一律归一为 LF——远端 bash 会把 `\r` 当词的一部分（`$'\r': command not found`、关键字行语法错、heredoc 落盘文件每行带 CR）；要原样发送加 `--keep-crlf`（此时不出现本字段）。**传输通道不受影响**：upload/download 是数据面，绝不改字节，Windows 文件传上去仍是 CRLF
- 远程命令的 stdout/stderr 已分别放入结果的 `stdout`/`stderr` 字段，无需自行拼接

## 常驻会话字段契约（v2.4：`session` 子命令；引擎 = tmux）

> **键集不变**：v2.4.0 把 session 的**进程引擎**从自研实现（`setsid`+`nohup`+util-linux `script`+FIFO+
> 看门狗）换成 **tmux**，下面所有字段与 v2.3 契约基线**逐键相同**；仅按白名单**新增** `orphans` /
> `orphans_total` / `orphan_remaining_total`（`kill` 结果**恒返回且恒空**，AI 侧零感知）。
> **与 v2.3 的行为差异只有 SPEC §7.1 的 C1~C8（既知变化，不是缺陷，解析代码无需改）**：
> ① `--no-pty` 参数已删除（结果恒 `pty: true`）；② 会话内 `TERM` = `tmux-256color`（tmux 强制决定）；
> ③ 新增的 `orphans*` 三字段恒空；④ 空闲回收从"服务器端准点"变为"**惰性扫 + 每主机一个 reaper**
> （默认 300 秒一轮）"（`ttl_seconds`/`idle_seconds`/`expires_in_seconds` 字段与口径不变）；
> ⑤ 会话目录少了 `in`/`sess.pid`/`bash.pid`/`watch.*`、多了 `tmux`；⑥ 依赖远端 tmux ≥ 3.0
> （没有/过低分别报 `tmux_missing`/`tmux_unsupported`，退出码 255）；⑦ `ctrl-c --force` 打的是内核给出的
> 前台进程组（`ps -o tpgid=`）；⑧ 多一个"服务器级"对象 tmux server（闲置时随最后一个会话退出，
> `list` 不受影响）。详见 `docs/session.md` 与 `docs/errors.md`。

- **`session start`**：`session`（名字）、`dir`/`log`、`pid`（= tmux pane 的 `#{pane_pid}`，即会话 shell；`list` 里同一个值也放在 `shell_pid`）、`pty`（**恒 `true`**——tmux 永远提供 PTY）、`cols`、`ready`（就绪确认：初始化命令的哨兵是否按时出现）、`permissions`；**`--no-pty` 已删除**：参数不复存在，传了被 argparse 拒绝，结果恒 `pty: true`（C1）
- **`session send`**：`token`（这条命令的哨兵 id）、`offset`/`next_offset`（本次输出起点，接着 `read --offset` 就只读这条命令的输出）、`sent_bytes`
- **`session read`**：载荷字段与 `log` 对齐——**`stdout`**（合并流，已清洗 CR/ANSI/哨兵行）、`bytes_returned`、`log_bytes`、`next_offset`、`has_more`、`status`（`done`|`running`）、`exit_code`/`exit_success`、`token`、`pid`、`waited_ms`/`wait_rc_secs`、`output_truncated`/`omitted_bytes`；默认剥离 ANSI（`--keep-ansi` 保留）
- **`session run`**：与 `read` 同构（`stdout`/`exit_code`/`status`/`token`/`next_offset`/`waited_ms`），外加 `sent_bytes`
- **`session ctrl-c`**：`signal`（`INT`|`KILL`）、`signaled_groups`/`signaled_children`（`--force` 时给出内核判定的前台进程组及其成员；默认注入 Ctrl-C 时 `signaled_groups` 为空、`signaled_count=1`）、`signaled_count`、`escalated_to_term`（**tmux 引擎下恒空**——不再有"INT 无效就升级 TERM"那条路，字段保留只为键集不变）、`sid`（= pane pid）；**被中断的命令自己不会产出哨兵**（bash 收到 SIGINT 丢弃当前命令行），所以 ctrl-c 会**代它补一条**（INT→`exit_code=130`、`--force`→`137`）⇒ 正等它的 `read --wait-rc --token` 会收敛
- **`session keys`**：`bytes_sent`（注入的原始字节数）
- **`session list`**：`sessions[]`（`session`/`pid`(=`shell_pid`)/`status`(`running`|`dead`)/`pty`/`cols`/`log_bytes`/`mtime`/`dir`/`started_at`/`age_seconds`/`idle_seconds`/`ttl_seconds`/`expires_in_seconds`/`current_command`/`tmux_session`）+ `count`
- **`session kill`**：`sessions[]`（`swept` 进程树闭包规模 / `remaining` 残留 / `roots` 是否拿到权威根 = pane_pid / `verified`（有根且无幸存）/ `cleaned` / `tmux_killed`）+ `remaining_total`/`verified_total` + **`orphans`/`orphans_total`/`orphan_remaining_total`（恒返回且恒空）**
- **状态语义**：会话本身只有 `running`/`dead`（**tmux `has-session`** 存活探测，不再是 starter 进程探测）；**命令级**状态在 `read`/`run` 里——`status:"done"` + `exit_code` 表示那条命令结束（哨兵出现），`running` 表示还没结束
- **退出码**：`session start` 成功 = 0（同名会话已存在 = 2 `session_exists`）；`send`/`run`/`read`/`ctrl-c`/`keys`/`list`/`kill` 成功 = 0；会话不存在 = 2（`session_not_found`）、会话已死 = 2（`session_dead`）、非法名字/参数 = 2（`bad_args`）；启动/写入/读取失败 = 255，**tmux 预检失败也是 255**（`tmux_missing`/`tmux_unsupported`/`tmux_failed`）
- **`session` 与 `exec` 的关系**：`session send`/`run` 的命令文本同样走 **CRLF 归一**（`--keep-crlf` 可关）；`session read`/`run` 的载荷字段与 `log`/`exec` 一致（`stdout`/`next_offset`/`exit_code`），便于同一套消费代码

## `--text` 可读模式（仅供人类速览，AI 直接用默认 JSON）

- 不加 flag 默认 JSON（见上）；`--text` 可读模式：exec 用 `---STDOUT.<nonce>---` / `---STDERR.<nonce>---`，test 用 `---INFO.<nonce>---`，upload/download 用 `---RESULT.<nonce>---`，ls 用 `---LS.<nonce>---`；错误用 `---ERROR.<nonce>---` + 一行 JSON；都以 `---END.<nonce>---` 结尾
- **标记带每次运行随机的 nonce 后缀**（远程输出无法预测，伪造不出有效标记）
- **可读模式仅供人类速览，AI 直接用默认 JSON**（可读模式 exec 无 warnings，header 仅 exit_code/duration；非零退出码 header 显示 `[EXIT n]` 而非 `[OK]`）

## `--field` 消费端字段提取（v1.5.16，免 json.loads 样板）

- **场景**：只要结果里某字段的裸值（高频：`stdout`），不用手写 `| python -c "import json,sys; print(json.load(sys.stdin)['stdout'])"`
- **单字段**：`--field stdout` → 打印 stdout 内容（裸值，多行原样）；exit_code 等标量转字符串
- **多字段**：`--field exit_code,exit_success` → 每行一个值（按序）
- **`-` 前缀 = 打到进程 stderr**：`--field stdout,-stderr` → stdout 内容打进程 stdout、stderr 字段打进程 stderr——**报错不被 stdout 展示脚本吞掉**（实测教训：AI 只读 stdout 字段丢了 stderr 报错；2>&1 或单独流都能拿到）
- dict/list 值 JSON 序列化（`ls --field entries`、upload 的 `file_list`）
- **字段不存在**（拼错）→ stderr 提示字段名（不静默空行误导）
- **`--field` 模式 stderr 仅含信号，无进度日志**（v1.5.19）：进度行（`[SSH]`/`[OK]`/`[EXEC]`）在 `--field` 下被静音——消费者只要字段裸值，进度是噪音（消费者被噪音烦到 `2>/dev/null` 会把 stderr 盲区提示一起静音，死结；静音噪音后屏蔽动机消失）。`--field` 下 stderr 只可能出现：`[WARN]` 级警告（凭据等）+ `_emit_fields` 的字段缺失/`stderr 非空`提示——**不要再 `2>/dev/null`，stderr 上的都是信号**。非 `--field` 模式进度日志照旧
- **例外（v2.1.1）**：显式请求的 `--progress` 心跳绕过静音——长任务 + `--field stdout` 恰是最需要心跳的场景；显式参数 = 信号不是噪音（`[WARN]` 同理）。仅 `[PROGRESS]` 与 `[WARN]` 例外，其他进度行仍静音
- **命令失败时 stderr 直接给实际内容（v2.1）**：`--field` 只提取了部分字段（如 `--field stdout`）且**命令失败（exit≠0）+ stderr 非空 + 未提取 stderr**时，stderr 通道直接打**截断的 stderr 尾巴（1KB 封顶）**+ 提示——不用再为拿真实报错多跑一轮（实测：pip 装依赖失败一次往返拿到报错）。命令成功但 stderr 非空（警告性输出）仍只打"结果含非空 stderr"提示不塞内容
- 与 `--text` 互斥（bad_args，退出码 2）；`--json` 兼容 no-op 不冲突
- **仅作用于成功路径**：工具错误（emit_error：连接失败/bad_args 等）仍输出**完整 JSON**（AI 需要 `retryable`/`message`）；命令非零退出是"成功路径的结果"（ok:true + exit_success:false），此时 --field 提取的是结果字段（`--field stdout,-stderr` 能拿到报错）

## 后台作业字段契约（v2.2：`exec --detach` + `log`；v2.2.1 修订字段名与状态机）

- **`exec --detach`** 立即返回（不阻塞）：`detached:true`、`job_id`、`pid`、`status`（`running`/`finished`）、`log`、`rc`、`pid_file`、`job_dir`、`cmd_written_to`（远端 job.sh 路径）、`permissions`（落盘权限声明）、`next_action`（下一步命令怎么写）。启动后 0.3s 内已结束的短作业直接给 `status:"finished"` + `exit_code`/`exit_success`
- **`log`**（别名 `tail`）载荷字段是 **`stdout`**（v2.2.1 起，与 `exec` 的 `stdout` 命名对齐；旧名 `content` 已移除）——注意它是 **`2>&1` 合并流**（`stream:"stdout+stderr"` 声明，stderr 内容也在这个字段里）。其余：`bytes_returned`、`log_bytes`（远端日志总大小）、`next_offset`（**下次增量读的字节偏移**）、`has_more`、`status`、`exit_code`/`exit_success`（未结束为 `null`）、`pid`/`pid_file`、`tail_window_truncated`、`wait_rc_secs`/`waited_ms`、`kill`（用了 `--kill` 时：`{ok,pid,signal,escalated}`）、`cleaned`（--cleanup）、`hint`（dead 时的原因说明）、`next_action`；`--list` 返回 `jobs[]`（job_id/log/log_bytes/status/exit_code/mtime）+ `count`
- **状态机三级**（v2.2.1）：`finished`（`job.rc` 存在，内容即 `exit_code`）/ **`dead`**（无 rc 且 `job.pid` 的存活探测判定进程已消失——被 kill / OOM / 崩溃）/ `running`。`--kill` 或外部 kill 之后状态会收敛为 `dead` 而不是永远 `running`；`--wait-rc` 也以"状态不再是 running"为收敛条件
- **增量读语义**：`--offset N` 读 `[N, N+max_output)` 字节并给 `next_offset`——轮询不会重复读同一段（`--field stdout` 或 `--field next_offset` 都很轻）；`--offset` 与 `--lines` 互斥
- **`--field` 同样适用**：`--field exit_code`、`--field stdout`、`--field next_offset`、`--field jobs`（JSON 序列化）
- **截断时的 `next_action`**（v2.2，exec 前台输出）：任一流超 `--max-output`（默认 64KB）被截断时，结果直接给"完整输出落盘路径 + 读文件不要重跑"的下一步提示，无需自己拼线索
- **`log --kill`**（v2.2.1）：读 `job.pid` → 对**进程组**（setsid 后 pid==pgid）`TERM` → 宽限 5s → `KILL`；Linux 上先用 `/proc/<pid>/cmdline` 校验 pid 确属本作业（防 pid 复用误杀），不匹配则拒绝并报 `kill_failed`。与 `--wait-rc` 可同用（kill 后立即收敛为 `dead`）；需 `--job-id`。**整组杀**是必须的：只杀 run.sh 会让 job.sh 的子进程被 reparent 成孤儿继续跑
- **`--cleanup` 状态守卫**（v2.2.2）：只在作业**已结束**（`finished`/`dead`）时删目录；作业仍 `running` 时**拒绝**并报 `job_running`（退出码 2，附 `pid` 与 `hint`）——删掉 `job.pid`/`job.log` 会让工具彻底失去追踪，而远端进程仍在跑。正路是 `--kill --cleanup`（一步到位）或 `--wait-rc` 后清理；确要放弃追踪用 `--cleanup --force`（照删，但结果带 `forced_cleanup:true` + `warnings` 明示"已失去追踪、进程可能仍在跑"）。`--force` 只允许配合 `--cleanup`（否则 bad_args）
- **`--force` 之后的唯一出路**（v2.2.3）：目录（含 `job.pid`）已删 → `--kill` 不可用，而远端进程还在跑。此时结果直接给出可执行命令 `group_kill`（= `kill -9 -<pid>`，同时写进 `warnings` 与 `next_action`）：**负号 = 进程组**，`pid` 就是 `setsid` 的组长（`pid==pgid`），一条命令清掉整组含子进程——**不需要先 pgrep**（对照实测：不带负号的 `kill -9 <pid>` 只杀组长，子进程被 reparent 成孤儿）
- **远端落盘与权限**（v2.2.1）：`job.sh`（命令原文）/`job.log`（完整输出）/`job.rc`/`job.pid` 均 **0600**，作业目录与作业根目录 **0700**，`run.sh` 0700（`run.sh` 首行 `umask 077` 保证 shell 创建的日志/rc 也是 0600）
- **退出码**：`exec --detach` 成功启动 = 0（作业自身的退出码在 `log` 的 `exit_code`）；启动失败 = 255（`detach_failed`）；`log` 读不到作业 = 2（`job_not_found`）、运行中拒绝清理 = 2（`job_running`）；`--kill` 拒绝/失败 = 255（`kill_failed`）
- **默认契约零变化**：不用 `--field` 时 stdout 恒单行 JSON
