---
name: pssh
description: 通过 pssh（paramiko CLI）执行远程 SSH 操作：exec/upload/download/test/ls，默认输出整行 JSON 供 AI 精确解析；支持跳板机、主机别名（@名称）、大文件并行分片下载、多级超时防挂死
---

# pssh — 结构化 SSH 工具（给 AI 用）

pssh 是基于 paramiko 的命令行 SSH 工具，专为非交互的 AI/脚本使用设计。需要操作远程主机（执行命令、传文件、查目录）时，用本工具而不是裸调 ssh：它的输出是结构化、可精确解析的。

## 速查（先读这 8 条）

1. **默认输出就是整行 JSON**（无需任何 flag），直接 `json.loads` stdout；`--text` 切可读模式（仅供人类）；`--json` 为兼容旧用法的空操作
2. 成功看 `ok`；命令成败看 `exit_success`；失败看 `error` + `message`（+可能的 `host`/`user`）
3. 连接任何主机前先 `test`，失败按错误类型处理，别盲目重试
4. 高频错误一句话动作：`auth_failed` 换凭据 / `connection_timeout` 查网络 / `exec_idle_timeout`/`exec_total_timeout` 按消息调大对应超时（**超时后远程进程可能仍在运行**，副作用命令先 pgrep 确认再重试）/ `connection_lost` 不信任部分结果重跑 / `jump_failed` 查跳板转发限制 / `bad_args` 查参数
5. 退出码仅粗筛，**决策一律以 `error` 字段为准**（超时退出码为 124，与连接失败 255 区分）
6. 多主机/跳板场景：**无论失败发生在跳板机还是目标机，JSON 的 `host`/`user`/`port` 恒指向目标机**；若 message 带 `[跳板机 user@host]` 前缀，说明失败发生在**跳板机侧**；连接期 `bad_args` 无 `host`/`user` 字段（目标尚未解析出来）；**连接成功后的路径类 `bad_args` 带 `host`/`user`/`port`**
7. **大文件下载慢或 `download_timeout`：加 `--parallel 8`**——默认 ≥8MB 自动 4 连接分片；**显式 `--parallel 8` 强制 8 连接（≥64KB 即启用并行）**，实际档位见结果 `parallel_used` 字段（高丢包/跨境链路单连接吞吐塌陷，多连接近似线性提速；**8 不行反试 4/2**）；**所有传输路径**（串行/目录/并行）都写进程唯一的 `.part.<pid>` 成功后原子改名（**下载的 `.part` 在本地、上传的 `.part` 在远端**）——失败/中断不留半截**最终**文件（上传中断可能残留 `.part` 临时文件，warnings 会提示清理命令）；上传先传 `.part` 再 posix-rename 原子覆盖（服务器不支持该扩展时退化为删除+改名并 WARN）
8. **命令含特殊字符时用 `--cmd-file -` 而不是 `--cmd`**：含单引号、多行、`$()`/反引号/`${}`、管道+引号组合的命令，用 `--cmd-file - <<'EOF' ... EOF` 从 stdin 读（heredoc 绕过所有 shell 转义问题）；简单命令（`df -h`、`uname -a`）用 `--cmd '...'` 即可

## 快速开始

- **本 skill 自带 `pssh.py`**（技能目录 `.reasonix/skills/pssh/` 下）：Linux/macOS `python3 <pssh_dir>/pssh.py <子命令> ...`；Windows cmd `<pssh_dir>\pssh.cmd ...`；Git Bash `<pssh_dir>/pssh ...`；环境需 `python3` + `paramiko`（`pip install paramiko`）
- 目标格式 `[user@]host[:port]`（如 `root@1.2.3.4:22`）；**IPv6 必须加方括号**：`user@[2001:db8::1]:22`、`[2001:db8::1]`（裸 IPv6 直接写也行）；支持主机别名 `@名称`、`-p/--port` 优先于内嵌端口；凭据 `--password`/`--key` 或环境变量 `PSSH_PASSWORD`/`PSSH_KEY` 等（也可写**技能目录下**的 `.env`——**配置样例见同目录 `.env.example`**；工作目录 `.env` 默认不加载，完整规则见 docs/setup.md）
- **完整规则**（认证优先级、别名专属凭据、`.env` 加载与供应链安全、IPv6/端口解析细节）见 **docs/setup.md**

## 输出约定（核心，完整版见 docs/contract.md）

- **stdout 才是可解析结果**；进度日志全部在 stderr，不要拿 stderr 当结果
- **默认即 JSON**：整行 JSON 直接 `json.loads`；`--text` 切可读模式（标记带随机 nonce，仅供人类速览，**AI 一律用默认 JSON**）
- **`ok` 与 `exit_success` 区分**：`ok=true` 只表示工具操作成功（连接+执行完成）；**远程命令成败看 `exit_success`**（例：`exit 3` → `ok=true, exit_code=3, exit_success=false`）
- 错误 JSON：`ok:false` + `error` + `message`（各错误类型含义与动作见 **docs/errors.md**）；参数写错输出 `bad_args` JSON（退出码 2）；`--help` 是纯文本输出（非 JSON），`--version` 输出一行 JSON
- **`warnings` 恒为参考信息，不代表操作失败**（疑似凭据等安全类提示不阻断执行，命令照常运行；需要行动的如 `.part` 残留会附清理命令）
- 字段/截断/warnings/`--text` 标记细节：**docs/contract.md**

## 退出码（粗筛，决策以 error 字段为准）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 传输错误 / SFTP 超时（upload/download/ls） |
| 2 | **所有参数错误**（argparse 层/缺命令/路径不存在等，均报 `bad_args` 或 `read_cmd_failed`） |
| 124 | **exec 超时**（`exec_idle_timeout` 或 `exec_total_timeout`；**远程进程可能仍在运行**，副作用命令重试前先 pgrep） |
| 130 | 用户中断（Ctrl+C / SIGTERM，`interrupted`；Windows terminate 是硬杀不走信号） |
| 254 | exec 成功但远程退出码恰为 255（`local_exit_code` 字段） |
| 255 | 连接失败，以及 exec/test 的执行期错误 |

完整退出码说明（含 130 的历史语义与信号设计）：**docs/errors.md**

## 错误类型（完整表见 docs/errors.md）

高频：`auth_failed` / `connection_timeout`（含 SSH banner 超时）/ `connection_refused`（含 TCP 可达但收到错误 banner）/ `connection_failed`（兜底）/ `dns_failed` / `host_key_rejected` / `jump_failed` / `exec_idle_timeout` / `exec_total_timeout` / `exec_timeout` / `exec_failed` / `connection_lost` / `interrupted`（上传中断可能残留 `.part`，warnings 明示清理命令）/ `upload_failed` / `download_failed` / `upload_timeout` / `download_timeout` / `bad_args` / `ssh_error` / `internal_error` / `read_cmd_failed` / `ls_failed` / `ls_timeout` / `test_failed`——每类的含义与建议动作见 **docs/errors.md**

## 子命令速览（完整细节见 docs/ 子文档）

### test — 先测连接
```bash
python3 pssh.py test root@1.2.3.4
```
返回 `hostname` / `os` / `kernel` / `arch`。**连接任何主机前先 test**，失败按错误类型处理。

### exec — 执行远程命令
```bash
python3 pssh.py exec root@1.2.3.4 --cmd 'df -h'
python3 pssh.py exec root@1.2.3.4 --cmd-file - <<'EOF'   # 长脚本/特殊字符走 stdin
ls -la /var/log
EOF
```
超时双参数（`--idle-timeout`/`--max-time`，均退出码 124 + error 细分）、输出截断（`--max-output` 与结构化截断字段）、`--pty`/`--pty-strip-ansi` 完整语义见 **docs/exec.md**；**带色输出命令（`grep --color`/`ls --color`/安装脚本）加 `--pty --pty-strip-ansi`** 供 AI 干净解析

### ls — 列远程目录
```bash
python3 pssh.py ls root@1.2.3.4 --path /etc         # entries JSON
python3 pssh.py ls root@1.2.3.4 --path '~'          # ~ 自动展开
```
`entries[]` 恒含 `name`（目录带 `/` 后缀）/ `mode` / `size`（**目录为 null**）/ `is_dir` / `is_symlink` / `mtime`（epoch 秒 UTC）；不支持通配符

### upload / download — 传输文件
```bash
python3 pssh.py upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
python3 pssh.py download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
python3 pssh.py download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8   # 大文件提速
```
路径语义（`~` 展开、尾斜杠=目录意图、目录内容放入不嵌套）、并行分片、`.part` 原子性/双丢防护/中断残留、`file_list` 断点重试、`--dry-run`/`--skip-existing`/`--no-recursive`、符号链接处理完整语义见 **docs/transfer.md**

### 跳板机
```bash
python3 pssh.py exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-password 'xxx' --cmd 'hostname'
python3 pssh.py exec root@10.0.0.5 --jump @bastion --cmd 'hostname'   # 跳板也支持 @别名
```
跳板未配置专属密码（`PSSH_JUMP_PASSWORD`/`--jump-password` 都没有）时自动回退用 `PSSH_PASSWORD`（v1.4.9 起，仅密码、密钥不回落）；凭据优先级、用户名回退、错误前缀、分片共享隧道细节见 **docs/jump.md**

## 安全规则

- 凭据优先用环境变量 `PSSH_PASSWORD` / `PSSH_KEY` 或 `.env`，**不要写进命令行参数**（进程列表可见）
- 命令含疑似凭据（如 `mysql -p'xxx'`、`DB_PASS=...`）时 pssh 会在 stderr 打 WARN——照常执行，但注意日志可能泄露敏感信息
- **JSON 结果的 `cmd`/`stdout`/`stderr` 字段同样含凭据且不截断**：把结果转发/落盘/写入任务记录前先脱敏

## 参数默认值

`ls --path` 默认 `.`、`ls --limit` 默认 2000（超出置 `truncated=true`）、`--timeout` 默认 10s、`--idle-timeout` 默认 60s（旧名 `--exec-timeout` 仍兼容）、`--max-time` 默认 `2×idle-timeout` 且至少 120、端口默认 22（`PSSH_PORT` 越界/非法回退 22 并打 WARN，命令行 `--port` 非法报错退出 2）

## 已知边界（需警惕的几条，完整见 docs/edge-cases.md）

- **`--pty` 下全屏交互程序（vi/vim、sudo 密码输入）不可用**；sudo 密码无法用 stdin 管道（`sudo -S` 不适用），需 `sudo -n`（免密）或密码写进远程环境变量
- **默认 AutoAddPolicy 隐式接受新 host key**（paramiko≥5.0 会写回 known_hosts，写盘为原子替换：并发不丢记录、不损坏文件；首次连接的新主机 stderr 会打 `[WARN] 新主机 host key 已隐式接受`）；敏感环境加 `--strict`
- **Git Bash 下远程路径参数会被 MSYS 改写**（用 `./pssh` 包装或加 `MSYS_NO_PATHCONV=1` 前缀）
- **远程命令自杀伤**：`pkill -f "dsh web"` 这类按自身 cmdline 模式匹配的杀进程命令，会把自己（承载 SSH 会话的 bash）一起杀掉 → `connection_lost`、结果不可信。用 `pkill -f '[d]sh web'` 括号转义规避（详见 docs/edge-cases.md）
- 其他边界（ANSI 风险、MaxStartups 并发上限、极早期信号窗口、Windows 下载文件名安全化、后台进程 drain 窗口、pkill 自杀伤等）见 **docs/edge-cases.md**

## 推荐操作序列

1. `test` 确认连通与认证 → 2. `ls` 确认目标路径 → 3. `exec` 或 `upload/download` → 4. 解析 JSON 的 `ok`/`error`，按错误类型处理失败

## 文档导航（按需读取，节省上下文）

> **维护约定**：每次更新/修改/修复 pssh，必须在 **CHANGELOG.md** **末尾追加**一条记录（**最新在最后**，文件只增不减、历史条目禁止覆盖或删除——末尾追加是对 AI 最安全的更新方式，避免"往开头插入"误伤标题/约定；版本号与 `pssh.py` 的 `VERSION` 常量一致），并同步技能目录（`/skills/pssh/`） `pssh.py` / `CHANGELOG.md`。

| 场景 | 读哪个文档 |
|---|---|
| 更新历史（每次更新/修复必须追加，禁止覆盖） | **CHANGELOG.md** |
| 前置条件完整规则（别名/凭据优先级/.env 安全） | **docs/setup.md** |
| 输出约定完整版（字段/截断/warnings/--text 标记） | **docs/contract.md** |
| 退出码完整说明 + 错误类型完整表 | **docs/errors.md** |
| exec 超时参数 / 输出截断 / PTY / 长脚本 | **docs/exec.md** |
| upload/download 并行分片 / 原子性 / 断点重试 / 符号链接 | **docs/transfer.md** |
| 跳板机凭据 / 隧道 / 分片 | **docs/jump.md** |
| PTY/ANSI / host key / MaxStartups / 信号 / Windows / Git Bash 等边界 | **docs/edge-cases.md** |
| 单个参数的准确语义（默认值/取值范围） | `python3 pssh.py <子命令> --help` |
| 契约本身（JSON 字段 / 退出码 / 错误类型） | 本文档（速查 + 输出约定核心 + 退出码表 + 错误类型摘要） |
