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
- **`warnings` 数组**：exec 成功时以下警告会进 JSON 的 `warnings` 字段——输出截断（后台进程占用/`--max-output` 截断/内存缓冲丢弃）、总时长上限被 cap 到 1200、`--cmd`/`--cmd-file` 同给、疑似凭据、远程退出码 255 特殊语义；其余日志类警告（端口配置、MSYS 路径转换）只打 stderr。**`warnings` 恒为参考信息，不代表操作失败**——疑似凭据等安全类提示不阻断执行（命令照常运行），不要因 warnings 过度保守拒绝合法命令；需要行动的警告（如上传中断的 `.part` 残留）会附具体清理命令
- 远程命令的 stdout/stderr 已分别放入结果的 `stdout`/`stderr` 字段，无需自行拼接

## `--text` 可读模式（仅供人类速览，AI 直接用默认 JSON）

- 不加 flag 默认 JSON（见上）；`--text` 可读模式：exec 用 `---STDOUT.<nonce>---` / `---STDERR.<nonce>---`，test 用 `---INFO.<nonce>---`，upload/download 用 `---RESULT.<nonce>---`，ls 用 `---LS.<nonce>---`；错误用 `---ERROR.<nonce>---` + 一行 JSON；都以 `---END.<nonce>---` 结尾
- **标记带每次运行随机的 nonce 后缀**（远程输出无法预测，伪造不出有效标记）
- **可读模式仅供人类速览，AI 直接用默认 JSON**（可读模式 exec 无 warnings，header 仅 exit_code/duration；非零退出码 header 显示 `[EXIT n]` 而非 `[OK]`）
