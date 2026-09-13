# pyaissh-mcp

**pyaissh 的 MCP 服务器适配层 — MCP server adapter for pyaissh, the AI-first SSH tool**

把 [pyaissh](../README.md)（给 AI 用的结构化 SSH CLI）暴露为 MCP 工具：JSON 传参彻底消灭 shell 引号问题（PowerShell `$` 插值、MSYS 路径改写等整类 bug 失去存在条件），自带**会话式连接池**（干活窗口内复用连接，空闲自动淘汰）。

Exposes pyaissh (the structured SSH CLI built for AI agents) as MCP tools: JSON-RPC argument passing eliminates the whole class of shell-quoting bugs, and a **session-scoped connection pool** reuses connections within a working window and evicts them automatically when idle.

## 包内容 / What's in this directory

本目录是**自包含分发包**（拷出去单独放就能跑，也能当技能包用）：

| 文件 | 说明 |
|---|---|
| `pyaissh_mcp.py` | MCP 服务器（stdio JSON-RPC，零 SDK 依赖）|
| `pyaissh.py` / `pyaissh` / `pyaissh.cmd` | **完整 pyaissh CLI 固定副本**（也经 `sync_check.py` 与 `skills/pyaissh/` 逐一 md5 校验）——可脱离 MCP 直接用 |
| `SKILL.md` + `docs/*.md` | **技能文档完整副本**（SKILL.md 入口 + contract/exec/transfer/errors/jump/setup/edge-cases 七篇）|
| `CLI_CHANGELOG.md` | pyaissh CLI 的变更记录（改名以免与适配器自己的 `CHANGELOG.md` 冲突）|
| `CHANGELOG.md` | 本适配层自己的变更记录 |
| `.env.example` | 凭据模板（适配器侧重；CLI 侧的模板与规则见 `docs/setup.md`）|
| `sync_check.py` | 与 `../skills/pyaissh/` 的**副本漂移检查/同步**（单一源：只改 `skills/pyaissh/`，然后 `python sync_check.py --update`）|

**不含测试**：开发用的测试套件（离线协议 / 凭据来源 / 真机 / 压力）只保留在本地开发树，不进分发包。

This directory is a **self-contained bundle** (drop it anywhere and it runs; it also doubles as a skill package): the MCP server, a byte-identical pinned copy of the complete pyaissh CLI (plus its POSIX/Windows entry points), the **full skill documentation** (`SKILL.md` + `docs/`), the CLI changelog (`CLI_CHANGELOG.md`), this adapter's changelog, `.env.example`, and `sync_check.py`. Development tests are intentionally **not shipped**.

## 快速开始 / Quick start

前置：`python3` + `paramiko`（`pip install paramiko`）。零 MCP SDK 依赖——服务器是手写的 stdio JSON-RPC。

在 MCP 客户端配置里加（以 Claude Desktop / 通用 `mcpServers` 格式为例）——**凭据不要写在这里，见下一节**：

```json
{
  "mcpServers": {
    "pyaissh": {
      "command": "python3",
      "args": ["/path/to/pyssh/pyaissh-mcp/pyaissh_mcp.py"]
    }
  }
}
```

### 凭据放哪里：推荐 `pyaissh-mcp/.env`（别写进 MCP 配置的 `env`）

复制同目录的 `.env.example` 为 `.env`，填上自己的值：

```ini
PYAISSH_PASSWORD=<你的默认密码>
PYAISSH_SUDO_PASSWORD=<普通用户的 sudo 密码，可选>
PYAISSH_HOST_PROD=<user>@<host>:<port>      # 之后工具里用 target: "@PROD"
```

**为什么凭据走 `.env` 而不是 MCP 配置的 `env`**：

1. MCP 配置里的 `env` 会**直接进入服务器进程的环境**——从 spawn 那一刻起密码就在子进程内存里，哪怕一次工具调用都没发生
2. **有些客户端会清洗环境变量**：DSH 会删除匹配 `/KEY|PASSWORD|SECRET|TOKEN/i` 的名字（以及 `DSH_*`），所以 `PYAISSH_PASSWORD` 放在 shell/系统环境里会被丢掉，只有写进配置 `env` 才活——用 `.env` 就没这个坑
3. `.env` 由 CLI 在**每次工具调用时**才读取（`load_env()`），进程环境从启动起不含密码；文件本身是本地忽略项，不进版本库

`.env` 规则（与 CLI 完全一致，完整见 [`docs/setup.md`](docs/setup.md)）：一行一个 `KEY=VALUE`，`#` 开头为注释，值可用引号包裹（`KEY="a # b"`），**已存在的环境变量优先**（`.env` 不覆盖真实环境变量）；主机别名用 `PYAISSH_HOST_<名称>` 定义，工具里写 `target: "@<名称>"`；也支持工具调用参数里的 `password`/`key`。

**只读脚本目录的 `.env`（即 `pyaissh-mcp/.env`）**：工作目录的 `.env` 默认**不**加载（供应链防护——防恶意仓库自带 `.env` 把 AI 的 SSH 连接导向攻击者主机，需显式 `PYAISSH_ALLOW_CWD_ENV=1` 才启用并会打 WARN）。因此**无需给 MCP 客户端配置 `cwd`**，`.env` 位置与启动目录无关。

## 工具 / Tools

| 工具 | 对应 CLI | 说明 |
|---|---|---|
| `pyaissh_test` | `test` | 连通性测试，返回 hostname/os/kernel/arch。**连接新主机前先 test** |
| `pyaissh_exec` | `exec` | 执行命令，返回 CLI 原生 JSON（`exit_code`/`exit_success`/`stdout`/`stderr`/`warnings`）；支持 `sudo` 提权、`encoding`、双超时 |
| `pyaissh_ls` | `ls` | 列目录，`entries[]` 结构化字段 |
| `pyaissh_upload` / `pyaissh_download` | `upload` / `download` | SFTP 传输，**零 token 消耗**（文件内容永不回传，只回元数据）；支持并行分片/断点续传 |
| `pyaissh_log` | `log` | 读**后台作业**日志：默认尾部 100 行；`offset` 增量读（返回 `next_offset` 续读不重复）；`wait_rc` 等结束拿 `exit_code`；`kill` 整组停掉；`cleanup` 清理；`list` 列作业。载荷字段 `stdout`（2>&1 合并流）。配合 `pyaissh_exec(detach=true)` 做准流式 |

**结果就是 pyaissh CLI 的原生单行 JSON 契约**：`ok`=工具操作成功、`exit_success`=远程命令成败、错误看 `error`+`message`+`retryable`。完整契约见 [`skills/pyaissh/SKILL.md`](../skills/pyaissh/SKILL.md) 与 `docs/`。

### 准流式：长任务边跑边看 / Quasi-streaming

超过一次调用能等完的任务（安装、编译、迁移、备份），用**后台作业 + 增量读**：

```
pyaissh_exec(target=..., cmd="apt install -y nginx", detach=true)   → job_id / log / rc / status / next_action
pyaissh_log(target=..., job_id=<id>, offset=0)                      → stdout / next_offset / has_more / status
pyaissh_log(target=..., job_id=<id>, offset=<next_offset>)          → 只给新字节（不重复）
pyaissh_log(target=..., job_id=<id>, wait_rc=30)                    → 等结束，返回 exit_code
pyaissh_log(target=..., job_id=<id>, kill=true)                     → 整组停掉（TERM→宽限 5s→KILL）
pyaissh_log(target=..., job_id=<id>, cleanup=true)                  → 清理远端作业目录
pyaissh_log(target=..., list=true)                                  → 列该主机所有作业（状态/大小/退出码/时间）
```

- **载荷字段是 `stdout`**（v0.2.1 起与 `pyaissh_exec` 的 `stdout` 命名对齐；它是 `2>&1` **合并流**，`stream:"stdout+stderr"` 声明）——别去找 `content`，它已移除
- **粒度是"每次工具调用读一段"**（准流式）——MCP 工具结果在调用返回时一次性交付，没有"单次调用内滚动输出"的通道（那是宿主客户端的职责）
- **状态三级**：`finished`（有 `job.rc` = 退出码）/ **`dead`**（无 rc 且 `job.pid` 存活探测判定进程已消失——被 kill/OOM/崩溃，退出码不可知，带 `hint`）/ `running`。**被 kill 的作业不再永远 running**，`wait_rc` 与轮询都能收敛
- **`wait_rc` 上限 45s**：客户端（DSH 默认 `toolCallTimeoutMs=60s`）会在超时点掐断调用，所以 MCP 层提前拦（bad_args + 提示），避免"只看到调用超时、拿不到任何状态"。要等更久：分多次 `wait_rc`，或调大客户端 `toolCallTimeoutMs` / 设 `PYAISSH_MCP_WAIT_RC_MAX`
- **`log` 也进连接池**：轮询会高频调用，复用连接后单次约 0.35s（不池化则每次重建 SSH，跨境 ~1s）
- **清理有守卫（v0.2.3）**：`cleanup=true` 在作业仍 `running` 时会被**拒绝**（`job_running`）——删掉 `job.pid`/`job.log` 会让工具彻底失去追踪，而远端进程仍在跑。收尾用 `kill=true, cleanup=true`（一步到位：先整组停掉再清理）；确要放弃追踪用 `cleanup=true, force=true`（照删但留痕：`forced_cleanup:true` + warnings）；`force` 必须与 `cleanup` 同用
- **参数容错（v0.2.2）**：数值型参数被传成布尔时不再变成 argparse 的 `expected one argument`——`wait_rc: true`（LLM 最自然的"我要等"）**按 MCP 层上限 45s 执行**并在结果 `warnings` 里回报"已按上限执行"；其他数值参数（`lines`/`offset`/`limit`/`max_output`…）给**可读 `bad_args`**（提示"该参数需要数值"）；开关参数额外接受字符串 `"true"/"false"`（含 yes/no/on/off/1/0）；`wait_rc: 0` 视为不启用
- **注意**：`detach` 会把**命令原文**写到远端 `job.sh`（后台作业必须落盘才能跑）——命令里别写明文凭据，用完 `cleanup`；远端作业目录 0700、`job.sh`/`job.log`/`job.rc`/`job.pid` 0600（v0.2.1 起从严，同机其他用户不可读）；`detach` 与 `sudo`/`pty` 互斥（bad_args）
- 不需要 MCP 的智能体可以用 CLI 走同一条路：`pyaissh exec --detach` + `pyaissh log --offset/--wait-rc/--kill/--cleanup`（同样的 JSON 契约）

## 架构 / Architecture

```
MCP 客户端 ──stdio JSON-RPC──> pyaissh_mcp.py ──进程内 main()──> pyaissh.py（固定副本，唯一契约源）
                                    │
                                    └── 连接池（仅 exec/ls/log）：borrow/探活/keepalive/TTL 淘汰
```

- **单契约源**：`pyaissh.py` 是与 CLI 逐字节一致的固定副本。MCP 层不实现任何 SSH 逻辑——每次工具调用在进程内直接调用副本的 `main()`（其原生支持进程内复用），`sys.argv` 注入参数、stdout 捕获结果。CLI 的全部契约逻辑（三重超时/截断/错误分类/retryable）零旁路零复制；CLI 的回归套件测的就是 MCP 用户拿到的东西。
- **薄到可以审计**：对副本的全部干预只有两个 monkey-patch 点——`connect`（池化借还）与 `close_all`（保传输层、清通道）。
- 副本与 CLI 源的一致性用 `python sync_check.py` 校验（CLI 升级后 `--update` 同步并回归）。

### 会话式连接池 / Session-scoped pool

不是 7×24 常驻——是"干活窗口内温热、空闲后自动消失"：

- **池键** = 目标 host/user/port + 认证指纹 + 跳板链，不同凭据绝不共享连接
- **复用前探活**（`open_session` 走完整隧道端到端）：死连接在**命令发出前**被替换，保证命令恰好执行一次；探活失败/淘汰/重连只发生在调用之间——上次调用的结果已完整交付，透明重连无条件安全
- **keepalive 心跳**（默认 20s）防 NAT/防火墙静默断链；**空闲 TTL**（默认 300s）到期自动关闭——无standing access 暴露
- **in_use 计数**：正在执行的长任务（max-time 可达 1200s）不会被 janitor 当空闲关掉
- **只有 exec/ls/log 进池**：`test` 必须真连（复用旧连接=假连通性测试）；传输与并行分片的多连接模型互不干扰；`log` 进池是因为准流式会高频轮询
- 实测收益：冷调用 ~1.0s → 温热 ~0.35s（同一台跨境主机，2.6×）

配置（环境变量，均有默认值）：`PYAISSH_MCP_POOL`（默认开）、`PYAISSH_MCP_POOL_TTL=300`、`PYAISSH_MCP_POOL_KEEPALIVE=20`、`PYAISSH_MCP_POOL_PROBE=5`、`PYAISSH_MCP_POOL_MAX=8`。

## 测试 / Tests

```bash
# 离线协议测试（不碰网络）：握手/tools/list/错误路径/stdout 纯净度/优雅退出
python test/test_offline.py

# 凭据来源测试（不碰真实主机）：清洗掉凭据类环境变量后，验证凭据确实来自 .env
#   用 TEST-NET-1（192.0.2.1）做探针主机 + "有无 .env 结果不同" 的机制判别
python test/test_env_creds.py

# 真机功能测试：凭据写 test/local_creds.json（参考 .example，已 gitignore）
#   或环境变量 PYAISSH_MCP_HOST1/PYAISSH_MCP_PW1、PYAISSH_MCP_HOST2/PYAISSH_MCP_PW2
python test/test_live.py

# 真机聚焦套件：后台作业准流式（detach→增量读→wait_rc→list→cleanup→job_not_found）
#   约 15 秒、单主机；开跑前自带单次 test 前置探活（凭据失败即 SKIP，不产生连续失败认证）
python test/test_live_bg.py
```

覆盖：协议握手与版本协商、参数校验（连接前拒绝、零网络）、错误分类（`auth_failed`/`connection_timeout` 与 `retryable`）、传输往返 md5、并行分片、sudo 提权、大输出截断+spill、池温热与 TTL 淘汰透明重连、`test` 不进池、管道并发、优雅退出、**凭据来源（`.env` 在环境被清洗时仍生效）**、**后台作业准流式（detach/增量读不重复/wait_rc/清理）**。

## 设计边界 / Non-goals

- 不在 MCP 层实现任何 pyaissh 没有的功能（防止双契约源漂移）
- 不做显式的"连接/断开"管理工具——池是隐式的，AI 永远不管理连接生命周期
- `cmd_file: "-"`（stdin 脚本）不可用——stdin 属于 MCP 协议通道，请写本地文件后传路径
- 工具调用按序执行（v1 串行派发）；取消请求（cancelled 通知）忽略，调用会跑完

许可见仓库根 [LICENSE](../LICENSE)。
