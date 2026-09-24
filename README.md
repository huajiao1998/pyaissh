# pyaissh

**给 AI 用的结构化 SSH 工具 — A structured SSH tool built for AI agents**

当前版本：**v2.4.0**（**`session` 常驻会话换 tmux 引擎**：真 PTY + 前台作业与状态跨调用保持 + 长程任务断线续读，进程开销 3 会话 45.5MB → 23.2MB / 12 → 5 进程；另有 v2.2 后台作业 `exec --detach` + `pyaissh log`、64KB 输出保留 + 截断 `next_action`）

**Current version: v2.4.0** — the `session` persistent shell now runs on **tmux**: real PTY, foreground job + state preserved across calls, long tasks survive disconnects and stay re-readable at ~half the process/memory cost (3 idle sessions: 45.5MB/12 procs → 23.2MB/5). Also includes v2.2 background jobs (`exec --detach` + `pyaissh log`).

裸 `ssh` 给 AI 用有四个坑：

> ① **输出不可解析**——人类文本要正则猜，AI 解析又慢又错
> ② **会无限卡死**——ssh 默认无超时，AI 调它等于把 agent 挂死
> ③ **传文件留半截**——中断后目标文件损坏，`--skip-existing` 还按大小误判
> ④ **传输内容烧爆 LLM 上下文**——大文件内容塞回给 AI，token 直接爆炸

pyaissh 把 SSH 变成 **AI 可精确消费的结构化工具**：stdout 恒为单行 JSON（`--help` 纯文本除外），三重超时防挂死，`.part` 原子传输 + 断点续传，文件内容零回传。

Raw `ssh` has four pain points when used by AI agents: **unparseable output**, **infinite hangs**, **half-written files on interrupt**, and **file contents blowing up the LLM context**. pyaissh turns SSH into a **structured tool AI can consume precisely**: single-line JSON stdout, triple timeout protection, atomic + resumable transfers, and zero file content in the context.

```bash
pyaissh exec root@1.2.3.4 --cmd 'uname -a'
pyaissh exec user@1.2.3.4 --sudo --cmd 'apt-get update'    # 普通用户登录 + sudo 提权
pyaissh exec root@1.2.3.4 --cmd 'echo hi' --field stdout   # 只要 stdout（裸值，进程 stderr 无噪音）
pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
pyaissh upload root@1.2.3.4 --local big.bin --remote /tmp/big.bin --parallel 8   # 高丢包/长 RTT 链路分片上传（收益随链路而定）
pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8
pyaissh test root@1.2.3.4
pyaissh ls root@1.2.3.4 --path /etc --long
pyaissh session start root@1.2.3.4 --name work          # 常驻会话（真 PTY，需远端 tmux ≥ 3.0）
pyaissh session run   root@1.2.3.4 --name work --cmd 'cd /opt/app && make -j8' --wait-rc 30
```

## 为什么给 AI 用 / Why for AI

| 能力 | 说明 | Capability |
|---|---|---|
| 🧭 结构化契约 | stdout 恒单行 JSON（`--help` 纯文本除外），直接 `json.loads`；20+ 类型化错误 + `retryable` 机器可读重试建议（完整表见 `skills/pyaissh/docs/errors.md`）| Structured contract: single-line JSON (except `--help`) + typed errors with machine-readable retry hints |
| 🔍 `--field` 字段提取 | `--field stdout,-stderr` 直接消费单字段（裸值到 stdout/stderr，多字段每行一个）——省去 `json.loads` 样板；工具错误仍完整 JSON | Field extraction: consume one field at a time without JSON boilerplate; tool errors still return full JSON |
| ⚙️ `--sudo` 提权 | 普通用户登录 + `--sudo` 提权执行（`--sudo-password` / `PYAISSH_SUDO_PASSWORD`；无密码自动免密探测），命令整链提权 | Sudo elevation for normal-user logins (password via flag/env; NOPASSWD auto-detected) |
| 🛡 防挂死 | 三重超时（静默/总时长/看门狗）——AI 调它永远不会卡死 | Triple timeout protection — never hangs |
| 🖥 常驻会话（真 PTY）| `session`：远端常驻真 PTY shell——`cd`/`export`/函数跨调用保持，**前台命令在 SSH 断开后继续跑**；断线回来 `read --wait-rc` 接着拿退出码与输出；可 `ctrl-c` 中断、可 `keys` 应答交互提示、可喂多步长任务（引擎=tmux，远端需 tmux ≥ 3.0）| Persistent session: real PTY, state + foreground job preserved across calls and disconnects, interruptible, can answer interactive prompts |
| 🔄 可靠传输 | `.part` 原子写 + `--resume` 断点续传 + **并行分片下载/上传**（`--parallel 1-8`）+ `file_list` 断点重试 | Atomic transfer + resumable upload/download + **parallel-sharded upload & download** + retryable file lists |
| 🔋 零 token 传输 | 文件内容从不回传 JSON——AI 只消费元数据，大文件不烧上下文 | Zero-token transfer: file content never enters the LLM context |
| 🔤 `--encoding` | exec/test 输出按指定字符集解码（如 GBK）——处理非 UTF-8 服务器 | Specify output decoding charset (e.g. GBK) for non-UTF-8 servers |
| ⚡ 快速启动 | paramiko 惰性 import——错误路径启动 296ms → 110ms | Lazy import: error paths start 2.7× faster |
| 🔗 网络能力 | 跳板机（共享隧道）、主机别名（@名称）、IPv6 | Jump hosts, host aliases, IPv6 |
| 🖥 跨平台 | Windows / Linux / macOS（含 Git Bash 路径转换） | Cross-platform incl. Git Bash path handling |

## 零 token 传输 / Zero-token transfer

**这是 pyaissh 与 MCP-SSH 生态最大的差异**：很多 MCP SSH server 会把传输内容塞进 tool 结果回给 LLM——传 1GB 文件 = 烧爆上下文。pyaissh 的 upload/download **内容从不回传**，AI 只消费元数据（实测 1MB 随机文件传输后，结果 JSON 仅 410 字节）。

This is pyaissh's biggest edge over the MCP-SSH ecosystem: many MCP SSH servers stuff file contents into tool results — a 1GB transfer blows up the context. pyaissh **never echoes file content**; AI only consumes metadata (measured: a 1MB binary transfer returns a 410-byte JSON).

| 场景 | 裸 ssh | MCP-SSH 生态 | **pyaissh** |
|---|---|---|---|
| 输出 | 人类文本 | 常混入内容 | **单行 JSON** |
| 1GB 文件传输后上下文 | — | 烧爆 | **410 字节元数据** |
| 卡死 | 会 | 看实现 | **三重超时** |
| 传大文件中断 | 半截文件 | 半截文件 | **原子写 + 断点续传** |

## 输出示例 / Output example

```json
{"ok": true, "action": "exec", "version": "x.y.z", "exit_code": 0, "exit_success": true,
 "stdout": "Linux\n", "stdout_bytes": 7, "stdout_truncated": false, "warnings": []}
```

错误 JSON 恒带 `retryable`（bool）：AI 自动重试策略直接读它，不必解析文本。

Error JSON always carries `retryable` (bool) so AI retry policies can decide without parsing prose.

## 3 步跑起来 / Get started in 3 steps

```bash
# 1. 依赖
pip install paramiko
# 2. 下载（任选）
git clone https://github.com/huajiao1998/pyaissh.git        # 或 Releases 下载技能 zip
# 3. 跑
./pyaissh test root@1.2.3.4
```

**Agent 安装（零人工）**：把下面这段提示词复制给任意 AI 会话，AI 会自动完成下载、放置、装依赖、验证：

```text
帮我安装 pyaissh 技能（给 AI 用的结构化 SSH 工具），步骤：
1. 下载最新技能包：从 https://github.com/huajiao1998/pyaissh/releases
   下载 pyaissh-skill-v*.zip（或 git clone https://github.com/huajiao1998/pyaissh
   后取 skills/pyaissh/ 目录）
2. 解压得到 pyaissh/ 目录（含 SKILL.md、docs/、pyaissh.py、pyaissh.cmd、pyaissh），
   放到我的智能体技能目录（如 ~/.dsh/skills/pyaissh/ 或对应 agent 的技能路径约定）
3. 安装依赖：pip install paramiko（已装则跳过）
4. 验证：运行 python3 <技能目录>/pyaissh.py --version，
   应输出单行 JSON 且含 "version"（版本号随发布更新，见 Releases 最新 tag）
5. 确认 SKILL.md 的 frontmatter（name: pyaissh）能被技能加载器识别
遇到报错先查仓库 README 的安装说明或 CHANGELOG。
```

**For agents**: copy the block above into any AI session — the agent downloads the skill package, places it into the skill directory, installs `paramiko`, and verifies, all by itself.

凭据：`--password` / `--key` 或环境变量 `PYAISSH_PASSWORD` / `PYAISSH_KEY` 等（`.env` 样例见 `.env.example`）。

Credentials: `--password`/`--key` or env vars `PYAISSH_PASSWORD`/`PYAISSH_KEY` (see `.env.example`).

## 使用场景 / Real-world usage

**AI 巡检 10 台机器 + 断点续传 2GB 日志**（shell 调用序列）：

```bash
# 巡检 10 台机器
for host in 10.0.0.{1..10}; do
  pyaissh test root@$host                    # 1. 连通性 + 系统信息（单行 JSON）
  pyaissh ls root@$host --path /var/log      # 2. 列目录（JSON entries）
  pyaissh exec root@$host --cmd 'df -h'      # 3. 执行命令（exit_success + stdout）
done
pyaissh download root@10.0.0.1 --remote /var/log/big.log --local . --resume   # 4. 断点续传 2GB 日志
# 5. 中断/失败重试：错误 JSON 的 retryable + file_list 精确续传，md5 复核
```

## 常驻会话：真 PTY + 长程任务 / Persistent session (real PTY, long-running)

`exec` 是"一条命令一次调用"，**无状态**；需要多步、要保留状态、要跑长任务、要中途改主意时用 `session`：

```bash
pyaissh session start root@1.2.3.4 --name work --ttl 10m     # 起会话（返回 pid/pty/log；空闲 10 分钟自动回收）
pyaissh session run   root@1.2.3.4 --name work --cmd 'cd /opt/app && git pull' --wait-rc 60
pyaissh session run   root@1.2.3.4 --name work --cmd 'make -j8' --wait-rc 5    # 超时回 status=running（状态还在）
pyaissh session read  root@1.2.3.4 --name work --offset <next_offset>          # 增量读输出（字节级 offset）
pyaissh session ctrl-c root@1.2.3.4 --name work                               # 中断正在跑的命令（会话不死）
pyaissh session keys  root@1.2.3.4 --name work --data 'y\n'                   # 应答 y/n、密码等交互提示
pyaissh session kill  root@1.2.3.4 --name work                                # 收尾（tmux 会话 + 进程树 + 目录）
```

- **真 PTY**：`tty` 真分配（`test -t 0` 为真），能跑需要 TTY 的程序、能应答 `read -p` 这类提示；每条命令**独立退出码**。
- **前台保持**：会话由远端 tmux 常驻，**不会被 SSH 断开/本地关机带走**——正在跑的命令继续跑，重连后 `read --wait-rc` 接着拿退出码与输出，`cd`/`export`/函数等状态原样还在。
- **长程任务**：`run --no-wait`（或 `send`）+ 循环 `read --offset` 边跑边看；跑错了 `ctrl-c` 中止再发一条（同一条改对重发，上下文不变）；适合构建/部署/长时脚本。
- **开销**：远端每主机只有 1 个 tmux server + 每会话 1 个 shell + 1 个 reaper（3 个空闲会话实测 **23.2MB / 5 进程**；旧实现 45.5MB / 12 进程）。空闲回收：提示符空闲且 TTL（默认 600s）内无交互就自动回收，`--ttl 0` 关闭。
- **依赖**：远端需 **tmux ≥ 3.0**；没有会明确报 `tmux_missing` 并给出安装命令（不自动安装）——装不了的环境仍可用 `exec` / `exec --detach` 跑长任务。

**Persistent session** — for multi-step work, state that must survive between calls, long-running jobs, and changing your mind mid-flight: real PTY (independent exit code per command), state and foreground job preserved across SSH disconnects, byte-level incremental output reads (`--offset`), `ctrl-c` to interrupt without killing the session, `keys` to answer prompts, and idle auto-reclaim (default 10 min, `--ttl 0` to disable). Requires **tmux ≥ 3.0** on the remote host; missing tmux is reported explicitly (`tmux_missing`) with install hints. Idle cost measured at **23.2MB / 5 processes for 3 sessions** (previous implementation: 45.5MB / 12).

## 文档 / Docs

- 完整 SKILL 文档（含契约、错误类型、传输语义）：`skills/pyaissh/SKILL.md` + `skills/pyaissh/docs/`
- 更新日志：`CHANGELOG.md`
- 命令详情：`pyaissh --help`

Full skill docs: `skills/pyaissh/SKILL.md` + `skills/pyaissh/docs/`. Changelog: `CHANGELOG.md`.

## 技能包 / Skill Package

`skills/pyaissh/` 是标准技能包：`SKILL.md`（frontmatter + 完整文档）+ `docs/` + `pyaissh.py` / `pyaissh.cmd` / `pyaissh`（三平台入口）+ `.env.example`。

**安装到你的 agent**：把 `skills/pyaissh/` 下的内容拷贝到你的智能体技能目录即可（各智能体技能路径约定不同，如 OpenClaw / Hermes Agent / QwenPaw 各有自己的目录）——技能本体与路径无关，任何能加载 `SKILL.md` 的 agent 都能用。**也可以直接从 [Releases](https://github.com/huajiao1998/pyaissh/releases) 下载 `pyaissh-skill-vX.Y.Z.zip`**（完整技能包，解压出 `pyaissh/` 目录拷入即可）。

`skills/pyaissh/` is a standard skill package: `SKILL.md` (frontmatter + full docs) + `docs/` + platform entry points + `.env.example`. **Install into your agent**: copy its contents into your agent's skill directory — the skill works regardless of path, any agent that loads `SKILL.md` can use it. You can also grab the **`pyaissh-skill-vX.Y.Z.zip` from [Releases](https://github.com/huajiao1998/pyaissh/releases)** (full skill package, unzip to a `pyaissh/` folder and copy it in).

## MCP 适配层 / MCP adapter (`pyaissh-mcp/`)

给支持 MCP 的智能体（Claude Desktop / Cursor / DSH 等）用：把 pyaissh 暴露为 7 个 MCP 工具——`pyaissh_test` / `exec` / `log` / `ls` / `upload` / `download` / **`session`**（常驻会话：真 PTY + 多步状态 + 长程任务）。JSON 传参彻底消灭 shell 引号问题；**会话式连接池**（exec/ls/log 复用连接，空闲 300s 自动淘汰）；**后台作业准流式**（`exec(detach=true)` → 循环 `log(offset=next_offset)` → `log(wait_rc)` → `log(kill,cleanup)`，长任务边跑边看）。

它是 pyaissh 的**薄适配层**：进程内直接调用与 CLI 逐字节一致的固定副本（`sync_check.py` 校验），对副本的全部干预只有两个 monkey-patch 点（`connect`/`close_all`），CLI 的超时/截断/错误分类/retryable 契约**零旁路零复制**；不实现任何 CLI 没有的 SSH 逻辑。凭据走同目录 `.env`（由 CLI 每次调用时读取；**不要**写进 MCP 客户端配置的 `env`——那会在 spawn 时把密码放进进程环境）。详见 [`pyaissh-mcp/README.md`](pyaissh-mcp/README.md)。

**Adapter for MCP-capable agents** — exposes pyaissh as 7 MCP tools (incl. `session`) with JSON arguments (no shell-quoting bugs), a session-scoped connection pool, and quasi-streaming background jobs (`exec(detach=true)` + incremental `log`). It is a **thin adapter**: it calls a byte-identical pinned copy of the CLI in-process (two monkey-patch seams only) and duplicates no SSH logic. Credentials belong in `pyaissh-mcp/.env`, not in the client's `env` block.

## 工程可信度 / Engineering rigor

每个版本都经**真实服务器**验证（真机执行 + md5 校验 + 中断/超时/信号测试）；v2.0.0 重构经**双机行为一致性对比**（33 用例 JSON 逐字段一致）。验证记录见 `CHANGELOG.md`。

Every release is verified against **real servers** (real execution + md5 checks + interrupt/timeout/signal tests); the v2.0.0 restructuring passed **two-machine behavior-identity comparison** (33 cases, field-by-field JSON match). Verification records are in `CHANGELOG.md`.

## License

[MIT](LICENSE)
