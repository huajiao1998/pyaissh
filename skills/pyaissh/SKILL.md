---
name: pyaissh
description: 通过 pyaissh（paramiko CLI）执行远程 SSH 操作：exec/upload/download/test/ls，默认输出整行 JSON 供 AI 精确解析；支持跳板机、主机别名（@名称）、大文件并行分片下载、多级超时防挂死、断点续传；传输零 token 消耗（文件内容永不回传，AI 只消费元数据）
---

# pyaissh — 结构化 SSH 工具（给 AI 用）

pyaissh 是基于 paramiko 的命令行 SSH 工具，专为非交互的 AI/脚本使用设计。需要操作远程主机（执行命令、传文件、查目录）时，用本工具而不是裸调 ssh：它的输出是结构化、可精确解析的。**传输零 token 消耗**：upload/download 文件内容从不回传 JSON——AI 只消费元数据（`files`/`bytes`/`file_list`），大文件/二进制不会烧爆 LLM 上下文。

## 速查（先读这 10 条）

1. **默认输出就是整行 JSON**（无需任何 flag），直接 `json.loads` stdout（**`--help` 纯文本除外**——需要用法时先跑 `--help` 读文本，其余一律 JSON）；`--text` 切可读模式（仅供人类）；`--json` 为兼容旧用法的空操作
2. 成功看 `ok`；命令成败看 `exit_success`；失败看 `error` + `message`（+可能的 `host`/`user`）
3. 连接任何主机前先 `test`，失败按错误类型处理，别盲目重试
4. 高频错误一句话动作：`auth_failed` 换凭据 / `connection_timeout` 查网络 / `exec_idle_timeout`/`exec_total_timeout` 按消息调大对应超时（**超时后远程进程可能仍在运行**，副作用命令先 pgrep 确认再重试）/ `connection_lost` 不信任部分结果重跑 / `jump_failed` 查跳板转发限制 / `bad_args` 查参数
5. 退出码仅粗筛，**决策一律以 `error` 字段为准**（超时退出码为 124，与连接失败 255 区分）
6. 多主机/跳板场景：**无论失败发生在跳板机还是目标机，JSON 的 `host`/`user`/`port` 恒指向目标机**；若 message 带 `[跳板机 user@host]` 前缀，说明失败发生在**跳板机侧**；连接期 `bad_args` 无 `host`/`user` 字段（目标尚未解析出来）；**连接成功后的路径类 `bad_args` 带 `host`/`user`/`port`**
7. **大文件传输慢或超时：加 `--parallel 8`**——下载默认 ≥8MB 自动 4 连接分片；**显式 `--parallel 8` 强制 8 连接（≥64KB 即启用并行）**，上传同参数（v1.5.8 起 `upload --parallel 1-8`，显式时 ≥64KB 分片，与 `--resume` 互斥），**实际档位见结果 `parallel_used` 字段**（下载与上传结果都有；高丢包/跨境链路单连接吞吐塌陷，多连接近似线性提速；**8 不行反试 4/2**）；**所有传输路径**（串行/目录/并行）都写进程唯一的 `.part.<pid>` 成功后原子改名（**下载的 `.part` 在本地、上传的 `.part` 在远端**）——失败/中断不留半截**最终**文件（上传中断可能残留 `.part` 临时文件，warnings 会提示清理命令）；上传先传 `.part` 再 posix-rename 原子覆盖（服务器不支持该扩展时退化为删除+改名并 WARN）
8. **`--cmd` 与 `--cmd-file -` 按调用环境选**：bash/常规 shell 下 `--cmd '...'` 完全可靠（标准引号规则，`$()`/反引号/管道/引号组合都安全，单引号包住即原样传远端）；**仅当调用环境是 Windows PowerShell 时**——`--cmd` 字符串会被 PowerShell 先解析（`$` 插值/子表达式执行、`\` 非转义），含 `$()`/反引号/多行/引号组合的复杂命令务必改用 `--cmd-file -`（heredoc/stdin 绕过 PowerShell 字符串层）；拿不准就在 PowerShell 里用 `--cmd-file -`
9. **`file_list.path` 语义两侧不同，勿混用**：upload 的 `path` 是**本地**路径/相对路径（重试本地定位用），download 的 `path` 是**远端**相对路径（重试远端定位用）——写重试逻辑时按方向取对侧的路径
10. **普通用户登录需要提权：加 `--sudo`**——`--sudo --cmd "apt update"` 自动 `sudo -S -p ''` 提权；密码走 `--sudo-password` 或 `PYAISSH_SUDO_PASSWORD` 环境变量（密码只经 SSH stdin 注入，命令/cmd 字段/日志均无密码）；**无密码时自动 `sudo -n` 免密探测**（免密直接跑；需密码立即失败不挂，warnings 提示密码配置）；复合命令（`&&`/`;`/管道）自动 bash -c 整链提权；`--sudo` 与 `--pty` 互斥

## 快速开始

- **本 skill 自带 `pyaissh.py`**（技能目录 `skills/pyaissh/` 下）：调用主路径 `python3 <pyaissh_dir>/pyaissh.py <子命令> ...`（Windows cmd / Git Bash 的额外调用方式见 docs/setup.md）；环境需 `python3` + `paramiko`（`pip install paramiko`）
- 目标格式 `[user@]host[:port]`（如 `root@1.2.3.4:22`）；**IPv6 必须加方括号**：`user@[2001:db8::1]:22`、`[2001:db8::1]`（裸 IPv6 直接写也行）；支持主机别名 `@名称`、`-p/--port` 优先于内嵌端口；凭据 `--password`/`--key` 或环境变量 `PYAISSH_PASSWORD`/`PYAISSH_KEY` 等（也可写**技能目录下**的 `.env`——**配置样例见同目录 `.env.example`**；工作目录 `.env` 默认不加载，完整规则见 docs/setup.md）；**凭据安全实践：不要把 token/密码写进 `--cmd` 或脚本内容**——`cmd` 字段会原样回显命令（含凭据需脱敏），且会触发凭据 WARN；凭据走 `--password`/`--key` 参数、`PYAISSH_*` 环境变量、`.env`、或让脚本从文件读取（`cat /path/secret`），绝不内联进命令
- **完整规则**（认证优先级、别名专属凭据、`.env` 加载与供应链安全、IPv6/端口解析细节）见 **docs/setup.md**

## 输出约定（核心，完整版见 docs/contract.md）

- **stdout 才是可解析结果**；进度日志全部在 stderr，不要拿 stderr 当结果
- **默认即 JSON**：整行 JSON 直接 `json.loads`；`--text` 切可读模式（标记带随机 nonce，仅供人类速览，**AI 一律用默认 JSON**）
- **`--field` 消费端免样板**（v1.5.16）：只要结果某个字段的裸值，不用手写 `json.loads`——`--field stdout` 打印 stdout 内容；`--field stdout,-stderr` 把 stderr 字段打到进程 stderr（**报错不被 stdout 展示吞掉**——实测教训：AI 只读 stdout 字段丢了 stderr 报错）；多字段逗号分隔每行一个；`-` 前缀=打 stderr 通道；与 `--text` 互斥；**错误路径仍输出完整 JSON**；不用 `--field` 时契约零变化
- **`ok` 与 `exit_success` 区分**：`ok=true` 只表示工具操作成功（连接+执行完成）；**远程命令成败看 `exit_success`**（例：`exit 3` → `ok=true, exit_code=3, exit_success=false`）
- 错误 JSON：`ok:false` + `error` + `message` + **`retryable`**（bool，机器可读的重试建议：true=重试可能成功且安全，false=改输入或放弃；exec 超时类 true 仅表示值得一试，重试前读 message 确认远程进程，或**直接读 `remote_may_be_running` 字段**（超时类恒有，true=进程可能仍在跑，副作用命令先 pgrep 再重试），见 **docs/errors.md**）；参数写错输出 `bad_args` JSON（退出码 2）；`--help` 是纯文本输出（非 JSON），`--version` 输出一行 JSON
- **`warnings` 恒为参考信息，不代表操作失败**（疑似凭据等安全类提示不阻断执行，命令照常运行；需要行动的如 `.part` 残留会附清理命令）
- 字段/截断/warnings/`--text` 标记细节：**docs/contract.md**

## 退出码粗筛（完整见 docs/errors.md）

`0`=成功 / `1`=传输错误（upload/download/ls）/ `2`=参数错误（bad_args/read_cmd_failed）/ `124`=exec 超时（**远程进程可能仍在运行**，副作用命令重试前先 pgrep）/ `130`=中断 / `254`=远程恰为 255 / `255`=连接失败。**决策以 error 字段为准，退出码仅粗筛。**

## 错误类型（完整表见 docs/errors.md）

高频：`auth_failed` / `connection_timeout`（含 SSH banner 超时）/ `connection_refused`（含 TCP 可达但收到错误 banner）/ `connection_failed`（兜底）/ `dns_failed` / `host_key_rejected` / `jump_failed` / `exec_idle_timeout` / `exec_total_timeout` / `exec_timeout` / `exec_failed` / `connection_lost` / `interrupted`（上传中断可能残留 `.part`，warnings 明示清理命令）/ `upload_failed` / `download_failed` / `upload_timeout` / `download_timeout` / `bad_args` / `ssh_error` / `internal_error` / `read_cmd_failed` / `ls_failed` / `ls_timeout` / `test_failed`——每类的含义与建议动作见 **docs/errors.md**

## 子命令速览（完整细节见 docs/ 子文档）

### test — 先测连接
```bash
python3 pyaissh.py test root@1.2.3.4
```
返回 `hostname` / `os` / `kernel` / `arch`。**连接任何主机前先 test**，失败按错误类型处理。

### exec — 执行远程命令
```bash
python3 pyaissh.py exec root@1.2.3.4 --cmd 'df -h'
python3 pyaissh.py exec root@1.2.3.4 --cmd-file - <<'EOF'   # 长脚本走 stdin（PowerShell 调用时复杂命令务必如此）
ls -la /var/log
EOF
```
超时双参数（`--idle-timeout`/`--max-time`，均退出码 124）、输出截断（`--max-output`）、`--pty`/`--pty-strip-ansi`、`--encoding`（GBK 系统日志乱码时指定编码）、`--sudo`（见速查第 10 条）完整语义见 **docs/exec.md**

### ls — 列远程目录
```bash
python3 pyaissh.py ls root@1.2.3.4 --path /etc         # entries JSON
```
`entries[]` 恒含 `name`（目录带 `/` 后缀）/ `mode` / `size`（**目录为 null**）/ `is_dir` / `is_symlink` / `mtime`（epoch 秒 UTC）；`~` 自动展开；不支持通配符

### upload / download — 传输文件
```bash
python3 pyaissh.py upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
python3 pyaissh.py download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
```
并行分片（`--parallel 8` 大文件提速）、断点续传（`--resume`）、`.part` 原子性/双丢防护/中断残留、`file_list` 断点重试、`--dry-run`/`--skip-existing`/`--no-recursive`、路径语义完整见 **docs/transfer.md**

### 跳板机
```bash
python3 pyaissh.py exec root@10.0.0.5 --jump root@1.2.3.4:2222 --cmd 'hostname'
```
跳板密码未配置时自动回退 `PYAISSH_PASSWORD`（v1.4.9 起，仅密码不回落）；跳板也支持 `@别名`；凭据优先级、错误前缀、分片共享隧道见 **docs/jump.md**

## 安全规则

- 凭据优先环境变量 / `.env`，**不要写进命令行参数**（进程列表可见）——完整凭据实践见 docs/setup.md
- **JSON 结果的 `cmd`/`stdout`/`stderr` 字段同样含凭据且不截断**：把结果转发/落盘/写入任务记录前先脱敏
- 命令含疑似凭据（如 `mysql -p'xxx'`）时 pyaissh 在 stderr 打 WARN——照常执行，但日志可能泄露敏感信息

## 已知边界（需警惕的几条，完整见 docs/edge-cases.md）

- **`--pty` 下全屏交互程序（vi/vim、sudo 密码输入）不可用**；**sudo 提权用 `--sudo`**（v1.5.15 起：`sudo -S` 经 SSH stdin 注入密码，命令文本/cmd 字段无密码；见速查第 10 条与 docs/exec.md）；免密环境也可 `sudo -n` 探测
- **默认 AutoAddPolicy 隐式接受新 host key**（首次连接新主机 stderr 打 `[WARN] 新主机 host key 已隐式接受`）；敏感环境加 `--strict`
- **远程命令自杀伤**：`pkill -f "dsh web"` 这类按自身 cmdline 模式匹配的杀进程命令，会把自己（承载 SSH 会话的 bash）一起杀掉 → `connection_lost`。用 `pkill -f '[d]sh web'` 括号转义规避
- 其他边界（MSYS 路径改写、ANSI 风险、MaxStartups、信号窗口、Windows 文件名安全化、后台进程 drain 等）见 **docs/edge-cases.md**

## 推荐操作序列

1. `test` 确认连通与认证 → 2. `ls` 确认目标路径 → 3. `exec` 或 `upload/download` → 4. 解析 JSON 的 `ok`/`error`，按错误类型处理失败

## 文档导航（按需读取，节省上下文）

> **维护约定**：每次更新/修改/修复 pyaissh，必须在 **CHANGELOG.md** **末尾追加**一条记录（最新在最后，历史条目禁止覆盖/删除；版本号与 `pyaissh.py` 的 `VERSION` 常量一致），并同步技能目录（`/skills/pyaissh/`） `pyaissh.py` / `CHANGELOG.md`。

| 场景 | 读哪个文档 |
|---|---|
| 更新历史（每次更新/修复必须追加，禁止覆盖） | **CHANGELOG.md** |
| 前置条件完整规则（别名/凭据优先级/.env 安全） | **docs/setup.md** |
| 输出约定完整版（字段/截断/warnings/--text 标记） | **docs/contract.md** |
| 退出码完整说明 + 错误类型完整表 | **docs/errors.md** |
| exec 超时参数 / 输出截断 / PTY / 长脚本 / --encoding / --sudo | **docs/exec.md** |
| upload/download 并行分片 / 原子性 / 断点重试 / 符号链接 | **docs/transfer.md** |
| 跳板机凭据 / 隧道 / 分片 | **docs/jump.md** |
| PTY/ANSI / host key / MaxStartups / 信号 / Windows / Git Bash 等边界 | **docs/edge-cases.md** |
| 单个参数的准确语义（默认值/取值范围） | `python3 pyaissh.py <子命令> --help` |
| 契约本身（JSON 字段 / 退出码 / 错误类型） | 本文档（速查 + 输出约定核心 + 退出码摘要 + 错误类型摘要） |
