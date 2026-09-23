---
name: pyaissh
description: 通过 pyaissh（paramiko CLI）做远程 SSH 运维与排障：exec 执行命令（--sudo 提权、--field 取裸字段、--cmd-file 喂脚本），session 常驻会话（真 PTY：逐条喂命令、cd/export 状态跨命令保留、每条独立退出码、ctrl-c 中断执行中的命令、keys 应答交互提示——多步部署/排障/试错用它），log 读后台作业（exec --detach 起长任务后按 offset 增量读日志、--wait-rc 等退出码、--kill 停掉），upload/download 传文件（大文件并行分片、断点续传），test 探活，ls 列目录，host 管理主机别名；支持跳板机 --jump、默认输出整行 JSON 供 AI 精确解析、多级超时防挂死、传输零 token 消耗（文件内容不回传，只给元数据）。长任务后台化、边跑边看日志、多步交互式操作、服务器故障排查优先用它。
whenToUse: 需要 SSH 到远程主机执行命令、跑长任务（后台作业 + 增量看日志）、需要多步且带状态的远端操作（常驻会话：可中断、可应答提示）、传文件、查目录、探活或管理主机别名时；宿主 shell 会吃掉 $ 等特殊字符（PowerShell/MSYS）需改用 --cmd-file 时
---

# pyaissh — 结构化 SSH 工具（给 AI 用）

pyaissh 是基于 paramiko 的命令行 SSH 工具，专为非交互的 AI/脚本使用设计。需要操作远程主机（执行命令、传文件、查目录）时，用本工具而不是裸调 ssh：它的输出是结构化、可精确解析的。**传输零 token 消耗**：upload/download 文件内容从不回传 JSON——AI 只消费元数据（`files`/`bytes`/`file_list`），大文件/二进制不会烧爆 LLM 上下文。

## 先选对模式（默认姿势，v2.3）

**按任务类别选，不要一律用同一个子命令**——选错的代价是：要么多花一倍调用，要么丢掉 stdout/stderr 分离与"零残留"。

| 任务形态 | 用哪个 | 为什么 |
|---|---|---|
| 单条命令、不依赖上一条的状态（多数情况：`id`/`cat`/`systemctl status`/`ls`/一次性脚本） | **`exec`** | 一次调用拿到结果；**stdout/stderr 分离**；远端零残留；结果可预测（无隐式状态） |
| 一条长命令（装包/编译/备份/大扫描），中途不需要改 | **`exec --detach` + `log`** | 立即返回 `job_id`；`log --offset` 增量看；**`log --kill` 随时中断**；抗 SSH 断开 |
| **多步、步骤间有状态**（`cd` 到目标目录、venv/环境变量、渐进式排查）、**可能要中止**、**要应答交互提示**（`read -p`/密码/y-n） | **`session`**（首选 `session run`） | `cd`/`export` 跨命令保留；跑歪了 `ctrl-c` 中断、改下一条继续；`keys` 应答提示 |
| 传文件（含大文件/断点续传） | `upload` / `download` | 零 token、并行分片、`.part` 原子收尾 |
| 查目录 / 探活 / 管主机别名 | `ls` / `test` / `host` | 结构化字段，无需自己解析 |

**别做这三件事**：① 多步操作硬拼成一条 `&&` 长命令（断在中途无法续、错误定位差）；② 单条命令也起会话（多一次调用 + 远端常驻 shell + 收尾清理，纯负担）；③ 长命令用前台 `exec` 等到超时（该 `--detach` 或 `session`）。

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
- **`--field` 消费端免样板**（v1.5.16）：**标准示例 `--field stdout,-stderr`**——stdout 内容打 stdout、stderr 内容打进程 stderr（**stderr 报错不被吞**——三次实测教训：AI 只读 stdout 丢过认证失败等真实原因）；只要某个字段裸值时用它代替手写 `json.loads`；`-` 前缀=打 stderr 通道，多字段逗号分隔每行一个；与 `--text` 互斥；**错误路径仍输出完整 JSON**；**`--field` 模式 stderr 无进度日志、仅含信号（WARN/提示）——不要 `2>/dev/null`**；不用 `--field` 时契约零变化
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
超时双参数（`--idle-timeout`/`--max-time`，均退出码 124）、输出截断（`--max-output` 默认 64KB，超出自动落 spill 文件并把路径写进 `stdout_spill_file`/`next_action`）、`--pty`/`--pty-strip-ansi`、`--encoding`（GBK 系统日志乱码时指定编码）、`--sudo`（见速查第 10 条）、`--progress [SECS]`（v2.1 长任务心跳：静默每 N 秒打 `[PROGRESS] 仍在运行`，不重置静默计时）完整语义见 **docs/exec.md**；**调参照 `pyaissh exec --help` 末尾的"场景 → 参数"表**（systemctl/apt/编译各该给多少 idle/max）

### exec --detach + log — 后台作业（v2.2，长任务"边跑边看"）
```bash
python3 pyaissh.py exec root@1.2.3.4 --detach --cmd 'apt install -y nginx'   # 立即返回 job_id/log/rc
python3 pyaissh.py log  root@1.2.3.4 --list                                  # 列作业（状态/大小/退出码）
python3 pyaissh.py log  root@1.2.3.4 --job-id <id> --offset 0                # 增量读（返回 next_offset）
python3 pyaissh.py log  root@1.2.3.4 --job-id <id> --offset <next_offset>    # 接着读，不重复
python3 pyaissh.py log  root@1.2.3.4 --job-id <id> --wait-rc 30                    # 等结束拿 exit_code
python3 pyaissh.py log  root@1.2.3.4 --job-id <id> --kill                        # 整组停掉（TERM→KILL）
python3 pyaissh.py log  root@1.2.3.4 --job-id <id> --cleanup                     # 清理远端作业目录
```
- **为什么用**：`exec` 前台受宿主单次调用时长限制（约 600s，pyaissh 自身可到 1200）；`--detach` 把作业丢到远端 `setsid+nohup` 后台（SSH 断开照跑），日志/退出码/pid 落 `/tmp/pyaissh-jobs/<job_id>/{job.log,job.rc,job.pid}`，AI 用 `log` 轮询增量读——分钟级任务的"正在进行中"变得可见
- **载荷字段是 `stdout`**（v2.2.1 起与 exec 对齐，`--field stdout` 取裸内容；它是 `2>&1` 合并流）
- **状态三级**：`finished`（有 job.rc = 退出码）/ `dead`（无 rc 且进程已消失：被 kill/OOM/崩溃，带 `hint`）/ `running`——被 kill 的作业不再永远 running，`--wait-rc` 能收敛；要主动停用 `--kill`
- **落盘权限**：目录 0700、job.sh/job.log/job.rc/job.pid 0600（命令原文与输出都在盘上，同机他人不可读）
- **清理有守卫**：`--cleanup` 在作业仍运行时**拒绝**（job_running，防自断追踪 + 进程还在跑）；收尾用 `--kill --cleanup` 一步到位，或 `--cleanup --force` 显式放弃追踪
- **force 之后要停进程**：结果直接给 `kill -9 -<pid>`（负号=进程组；pid 即 setsid 组长，一条命令清整组含子进程，**不用先 pgrep**）
- **与 `--sudo`/`--pty` 互斥**（bad_args）；命令原文会落远端 `job.sh`（别写明文凭据，用完 `--cleanup`）

### CRLF 行尾自动归一（v2.2.4）

Windows 工具（记事本 / VS Code / PowerShell 重定向 / here-string）写出的命令文本行尾是 `\r\n`，远端 bash 会把 `\r` 当词的一部分（`$'\r': command not found`、`if/then` 行语法错、heredoc 落盘文件每行带 CR）。pyaissh **默认把命令文本的 CRLF/CR 归一为 LF**——`--cmd` 内联、`--cmd-file` 文件、`--cmd-file -` stdin 三条路都覆盖，结果回传 `crlf_normalized: <处数>` + warnings 说明：

```bash
python3 pyaissh.py exec root@1.2.3.4 --cmd-file win_written.sh    # 自动归一，不必再 sed -i 's/\r$//'
python3 pyaissh.py exec root@1.2.3.4 --cmd 'printf "a\r\nb\r\n"'  # 要真 CR 用转义写法（不受影响）
python3 pyaissh.py exec root@1.2.3.4 --cmd-file win.sh --keep-crlf  # 确实要 CRLF 数据时保留原样
```

**`upload`/`download` 是数据通道，任何情况下不改字节**（Windows 上写好再上传的脚本在远端仍是 CRLF：要执行就用 `--cmd-file` 送脚本，或远端 `dos2unix`）。

### session — 常驻会话（真 PTY）：逐条喂命令 / 状态保留 / 可中断（v2.3）

`exec` 无状态、`exec --detach` 启动后改不了；**多步且带状态**的活（部署、排障、试错、交互式安装）用 `session`：

```bash
python3 pyaissh.py session start h --name work                   # 起会话（真 PTY；返回 pid/pty/ready）
python3 pyaissh.py session run   h --name work --cmd 'cd /opt/app && git pull'   # 跑一条并等结果（一次调用）
python3 pyaissh.py session run   h --name work --cmd 'make -j8' --wait-rc 5      # 状态还在（cwd 仍是 /opt/app）
python3 pyaissh.py session ctrl-c h --name work                  # 中断正在跑的 make（会话不死，cwd 还在）
python3 pyaissh.py session keys  h --name work --data 'y\n'      # 应答程序提示（y/n、密码…）
python3 pyaissh.py session read  h --name work --offset 0        # 增量读（send/--no-wait 之后用）
python3 pyaissh.py session kill  h --name work                   # 收尾（进程树全清 + 删目录）
```

- **`session run` = send + 等结果，一步一次调用**（与 `exec` 同成本）；`--wait-rc N` 超时则回 `status:"running"`（带 `token`，可 `read --wait-rc` 续等或 `ctrl-c` 中断）；`--no-wait` 只发送（等价 `send`）
- **每条命令独立退出码**（`exit_code`）；**`cd`/`export`/函数跨命令保留**——错了改下一条继续，不用重来
- **能中断执行中的命令**：`ctrl-c`（SIGINT→自动升级 TERM；`--force` = SIGKILL），会话与状态都保住
- **能应答交互提示**：`keys --data 'y\n'`（支持 `\n \r \t \xNN`）
- `read` 载荷字段 `stdout`（合并流，自动清洗 CR/ANSI/哨兵行）/`next_offset`/`status`(`done`|`running`)/`exit_code`
- 依赖：真 PTY 需 util-linux `script`（缺则自动降级为非 PTY，状态与退出码照常但没有 tty）
- 详细设计、三个实测坑（哨兵被 `read` 吃掉 / kill 按 sid 会留孤儿 / SIGINT 需升级）见 `docs/session.md`

### ls — 列远程目录
```bash
python3 pyaissh.py ls root@1.2.3.4 --path /etc         # entries JSON
```
`entries[]` 恒含 `name`（目录带 `/` 后缀）/ `mode` / `size`（**目录为 null**）/ `is_dir` / `is_symlink` / `mtime`（epoch 秒 UTC）；`~` 自动展开；不支持通配符

### upload / download — 传输文件
```bash
python3 pyaissh.py upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
python3 pyaissh.py upload root@1.2.3.4 --local ./web --remote /var/www --exclude node_modules,.git   # 部署排除（v2.1）
python3 pyaissh.py download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
```
并行分片（`--parallel 8` 大文件提速）、断点续传（`--resume`）、`.part` 原子性/双丢防护/中断残留、`file_list` 断点重试、`--dry-run`/`--skip-existing`/`--no-recursive`、`--exclude` 目录排除（v2.1：逗号分隔 glob，目录整树剪枝/文件不上传）、路径语义完整见 **docs/transfer.md**

### host — 主机别名（多主机不同密码闭环，v2.1）
```bash
python3 pyaissh.py host add prod root@1.2.3.4 --password 'xxx'   # 写 .env（幂等更新）
python3 pyaissh.py exec @prod --cmd 'df -h'                       # 之后用 @别名 走专属凭据
```
**PowerShell 注意（v2.1.4 实测）**：PS 会把行首 `@名称` 当特殊语法吞掉（`pyaissh test @prod` 报缺 target）——PowerShell 下给别名**加引号**：`pyaissh test "@prod"`。
两台机器不同密码不再逐条 `--password`（进程列表可见 + WARN 刷屏）：`host add` 把 `PYAISSH_HOST_<NAME>`（+`_PASSWORD`/`_KEY`）写进脚本同目录 .env，`@别名` 调用自动用专属凭据；密码是明文存 .env，勿提交 git/分享。管理：`host list`（列别名，不回显密码）/ `host remove NAME`（删别名含专属凭据）；均支持 `--field`（如 `host list --field entries`）

### 跳板机
```bash
python3 pyaissh.py exec root@10.0.0.5 --jump root@1.2.3.4:2222 --cmd 'hostname'
```
跳板密码未配置时自动回退 `PYAISSH_PASSWORD`（v1.4.9 起，仅密码不回落）；跳板也支持 `@别名`；凭据优先级、错误前缀、分片共享隧道见 **docs/jump.md**

## 安全规则

- 凭据优先环境变量 / `.env`，**不要写进命令行参数**（进程列表可见）——完整凭据实践见 docs/setup.md；多主机别名用 `host add`（上节）
- **JSON 结果的 `cmd`/`stdout`/`stderr` 字段同样含凭据且不截断**：把结果转发/落盘/写入任务记录前先脱敏
- 命令含疑似凭据（如 `mysql -p'xxx'`）时 pyaissh 在 stderr 打 WARN——照常执行，但日志可能泄露敏感信息；**从文件读值**（`PW=$(cat f)` / `$(<f)`）不报（v2.1：值不进命令行文本，无明文泄漏）

## 已知边界（需警惕的几条，完整见 docs/edge-cases.md）

- **`--pty` 下全屏交互程序（vi/vim、sudo 密码输入）不可用**；**sudo 提权用 `--sudo`**（v1.5.15 起：`sudo -S` 经 SSH stdin 注入密码，命令文本/cmd 字段无密码；见速查第 10 条与 docs/exec.md）；免密环境也可 `sudo -n` 探测
- **默认 AutoAddPolicy 隐式接受新 host key**（首次连接新主机 stderr 打 `[WARN] 新主机 host key 已隐式接受`）；敏感环境加 `--strict`
- **远程命令自杀伤**：`pkill -f "dsh web"` 这类按自身 cmdline 模式匹配的杀进程命令，会把自己（承载 SSH 会话的 bash）一起杀掉 → `connection_lost`。用 `pkill -f '[d]sh web'` 括号转义规避——**且模式不得出现在同命令行任何位置**（含同脚本其他命令如 setsid 行，照样炸）；无法避免用变量拼接或拆两次调用（详见 docs/edge-cases.md）
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
