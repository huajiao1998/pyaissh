# pyaissh-mcp 更新日志（CHANGELOG）

> **维护约定**：每次更新/修改/修复 pyaissh-mcp，必须在本文件**末尾追加**一条记录（最新在最后，文件只增不减，历史条目禁止覆盖或删除）。版本号与 `pyaissh_mcp.py` 的 `SERVER_VERSION` 常量一致。
> 注意：`pyaissh-mcp/pyaissh.py` 是 CLI 的**固定副本**（契约源），随 CLI 升级用 `sync_check.py --update` 同步——副本变更记录在 CLI 的 CHANGELOG（仓库根 / `skills/pyaissh/`），本文件只记 MCP 层自身。

---

## [0.1.0] - 2026-08-29

### 新增

- **MCP stdio 服务器**（`pyaissh_mcp.py`，零 SDK 依赖，手写 newline-delimited JSON-RPC 2.0）：5 个工具 `pyaissh_test` / `pyaissh_exec` / `pyaissh_ls` / `pyaissh_upload` / `pyaissh_download`，与 CLI 子命令一一对应；`initialize` 版本协商（回显支持版本、未知版本回落 2025-06-18）、`tools/list`、`ping`、`resources/list`/`prompts/list` 空清单兜底、通知不应答、坏帧 -32700、未知方法 -32601、未知工具 -32602。
- **单契约源架构**：进程内加载与 CLI 逐字节一致的 `pyaissh.py` 固定副本（`sync_check.py` 校验/同步），每次工具调用经 `sys.argv` 注入直接调用副本的 `main()`（其原生支持进程内复用）+ stdout 捕获——CLI 的超时/截断/错误分类/retryable 契约零旁路零复制。对副本的全部干预仅两个 monkey-patch 点（`connect`/`close_all`）。
- **会话式连接池**（仅 exec/ls，默认开）：池键 = 目标+认证指纹+跳板链；复用前 `open_session` 端到端探活（死连接在命令发出前替换，保证命令恰好执行一次）；keepalive 心跳（默认 20s）防静默断链；空闲 TTL（默认 300s）自动淘汰——无 standing access；`in_use` 计数防止长任务（max-time 可达 1200s）被 janitor 中途断链；池满按最久未用淘汰（上限 8）；池关模式 `PYAISSH_MCP_POOL=0` 全冷行为与 CLI 一致。
- **池边界语义**：`test` 不进池（连通性测试必须真连，复用旧连接=假测试）；传输（upload/download）不进池（与并行分片多连接模型互不干扰）——按 `main()` 的 `_CURRENT_ACTION` 路由。
- **MCP 模式专属校验**：`cmd_file: "-"` 明确拒绝（stdin 属于 MCP 协议通道），bad_args + 指引写本地文件。
- **工具结果映射**：content.text = CLI 原生 JSON 行；`isError=true` 当且仅当契约 `ok:false` 或无法解析输出；CLI stderr 进度日志转 server stderr（带 `[cli-stderr]` 前缀），stdout 只有 MCP 帧。
- **测试套件**：离线协议 25 例（握手/工具清单/错误路径/stdout 纯净度/优雅退出/版本回落/池关模式）+ 真机 31 例（双主机 exec/ls/传输往返 md5/并行分片 parallel_used/sudo 提权/错误分类 retryable/截断+spill/cmd_file/管道并发/TTL 淘汰透明重连/test 不进池/池关模式）。凭据走 gitignored `test/local_creds.json`（含 `.example`）。

### 真机验证数据

- 池温热：同主机冷 ~1.0s → 温热 ~0.35s（2.6×；探活+执行+关闭全部走复用隧道）
- TTL=2s 专用实例：空闲 3.5s 后调用自动重连成功，stderr 有「TTL 淘汰」记录
- 双主机（<node1> / <node2>）独立池条目，各自温热互不干扰
- 传输 1MB 往返 / 5MB `--parallel 4` 下载 md5 双一致，`parallel_used=4` 回传

### 修订（2026-09-13）：凭据卫生 —— 推荐 `.env`，不放 MCP 配置的 `env`（无代码变更，SERVER_VERSION 仍 0.1.0）

- **文档/模板**：快速开始不再把 `PYAISSH_PASSWORD` 写进客户端配置的 `env`，改为推荐同目录 `.env`；新增 `.env.example`（带注释的完整模板）与 `.env`（全注释的填写骨架，安全默认＝不加载任何变量）
- **理由（三条，写进 README）**：① 配置里的 `env` 在 spawn 时即进入服务器进程环境，一次工具没调用密码就在内存里；② 部分客户端会清洗环境变量（DSH 删除匹配 `/KEY|PASSWORD|SECRET|TOKEN/i` 的名字与 `DSH_*`），放 shell/系统环境会被丢掉；③ `.env` 由 CLI 在每次工具调用时才读取（`load_env()`），且文件为本地忽略项
- **澄清**：`load_env()` 只读**脚本目录**的 `.env`（即 `pyaissh-mcp/.env`），工作目录 `.env` 默认不加载（供应链防护）→ 无需给客户端配置 `cwd`，`.env` 位置与启动目录无关
- **测试**：新增 `test/test_env_creds.py`（+6 例）——在**清洗掉凭据类环境变量**（模拟 DSH 的 `scrubbedParentEnv`）的条件下，验证主机别名/密码确实由 `pyaissh-mcp/.env` 提供；并用「有无 `.env` 结果不同」做机制判别（不是只看一次绿灯）。测试客户端 `mcp_client.McpClient` 增加 `scrub_creds` 开关
- **固定副本已同步至 CLI v2.2.0**（`python sync_check.py --update`，副本 md5 `5aff26c140706c53f046f6e288f83200`，与仓库根 / `skills/pyaissh/` 两份逐字节一致）：副本新增 `log` 子命令与 `exec --detach`、默认 `--max-output` 64KB、截断 `next_action`——**MCP 层暂未暴露这两个新能力**（仍是 5 个工具），副本先跟上以免契约源滞后
- **同步后验证**（不做全量回归——副本与已测版本逐字节一致）：离线协议 25 例 + 凭据来源 6 例全绿；另做**接缝冒烟** 2 次 `pyaissh_exec`（池开）确认适配层与新副本的绑定仍正常——两次调用 `ok=true/exit_code=0`，第二次 stderr 有「池命中」（连接池复用 + monkey-patch 接缝在新副本上有效）

## [0.2.0] - 2026-09-13

### 新增：后台作业工具（准流式）——把 CLI v2.2.0 的 detach/log 暴露成 MCP 工具

- **`pyaissh_exec` 新增 `detach`**：后台化（远端 `setsid+nohup`），立即返回 `job_id/log/rc/status/next_action`；SSH 断开与宿主调用超时都不影响作业。与 `sudo`/`pty` 互斥（由 CLI 连接前拒绝）
- **新增 `pyaissh_log` 工具**：`job_id` / `list` / `lines` / `offset` / `wait_rc` / `cleanup` / `job_dir` / `limit` / `max_output` / `encoding` + 认证参数。增量读走 `offset → next_offset`（轮询不重复），`wait_rc` 等结束拿 `exit_code`，`list` 列该主机作业，`cleanup` 清理远端作业目录
- **准流式用法**：`exec(detach=true)` → 循环 `log(offset=next_offset)` → `log(wait_rc)` → `log(cleanup)`。粒度是"每次工具调用读一段"——MCP 工具结果在调用返回时一次性交付，**单次调用内滚动输出需要宿主支持（DSH 的 mcp-client 不发 progressToken、无 progress 处理器，实测确认）**，不在本层能力范围
- **`wait_rc` 上限 45s**（`PYAISSH_MCP_WAIT_RC_MAX` 可调）：客户端 DSH 默认 `toolCallTimeoutMs=60s` 会先掐断调用，超限直接 `bad_args` 并提示"分多次调用或调大客户端超时"——避免 AI 只看到"调用超时"而拿不到任何作业状态
- **`log` 纳入连接池**（原仅 exec/ls）：准流式高频轮询复用连接（温热 ~0.35s vs 冷连 ~1s）
- 实现零新增 SSH 逻辑：与既有 5 个工具同一路径（组 `sys.argv` → 进程内调副本 `main()` → 捕获 stdout → 映射 MCP 结果），契约与 CLI 逐字段一致；CLI-only 智能体走 `pyaissh exec --detach` + `pyaissh log` 是同一套语义
- **测试**：离线 25 → 33 例（新增 `T16a-h`：log schema/exec detach schema/`wait_rc` 超限拦截/缺 job_id/`job_id` 穿越/`list` 互斥/detach+sudo 与 detach+pty 互斥；全部连接前拒绝、零网络）；新增真机聚焦套件 `test/test_live_bg.py`（9 例：detach 启动 → `offset=0` 增量读（running + next_offset）→ 续读不重复 → `lines` 尾部模式 → `wait_rc` 拿 exit_code=0 → log 池命中 → `list` 状态正确 → `cleanup` → 清理后 `job_not_found`+`isError`；自带单次 `test` 前置探活，失败即 SKIP）
- **真机验证数据**（B2 `root@<node1>`）：9/9 PASS；既有工具回归冒烟（test/exec×2/ls）全绿且池命中正常
- 副本要求：`pyaissh.py` ≥ v2.2.0（本次已同步，md5 `5aff26c140706c53f046f6e288f83200`）
- **取代说明**：上文 2026-09-13 修订条里的「MCP 层暂未暴露这两个新能力（仍是 5 个工具）」已被本版本取代——现为 **6 个工具**（新增 `pyaissh_log`，`pyaissh_exec` 增 `detach` 参数）。该条原文按"历史只追加不覆盖"约定保留不动

## [0.2.1] - 2026-09-13

### 跟随 CLI v2.2.1：后台作业工具同步四项修复 + 新增 kill 透传

- **固定副本同步至 CLI v2.2.1**（md5 `215e123a9ef7a773b47b2658e0bb2eeb`，与仓库根 / `skills/pyaissh/` 逐字节一致）
- **`pyaissh_log` 新增 `kill` 参数**（对应 `log --kill`）：整组停掉作业（读 `job.pid` 对进程组 TERM → 宽限 5s → KILL；Linux 上先校验 `/proc/<pid>/cmdline` 防 pid 复用误杀）；可与 `wait_rc` 连用，kill 后状态立即收敛为 `dead`；缺 `job_id` 时连接前 `bad_args`
- **载荷字段名 `content` → `stdout`**（与 `pyaissh_exec` 对齐，旧名移除；`stream:"stdout+stderr"` 声明合并流）——工具描述与 README 同步
- **状态三级**：`finished` / `dead`（无 rc 且进程已消失，带 `hint`）/ `running`；`wait_rc` 以"状态不再是 running"为收敛条件（被 kill 的作业不再永久 running）
- 远端作业落盘权限随 CLI 从严（目录 0700、`job.sh`/`job.log`/`job.rc`/`job.pid` 0600）
- **测试**：离线 33 → 34 例（`T16i`：`kill` 缺 `job_id` 连接前拒绝；`T16a` 断言含 `kill`）；真机聚焦套件 `test/test_live_bg.py` 9 → 11 例（字段改 `stdout` 且断言无 `content`；新增 `B10` kill 整组停掉收敛 `dead`、`B11` `dead` 带 hint 且 `wait_rc` 快速收敛）
- **真机验证数据**（B2 `root@<node1>`）：11/11 PASS；离线 34 + 凭据 6 全绿

## [0.2.2] - 2026-09-13

### 修复：数值参数被传成布尔 → argparse "expected one argument"（招牌功能的最高频误用）

- **现象**（两次踩中）：模型按描述"阻塞等待作业结束"自然传 `wait_rc: true`，而 `_build_argv`
  的 `isinstance(v, bool)` 分支把**所有**参数一律当旗标裸传（`--wait-rc` 无值）→ CLI argparse
  报 `argument --wait-rc: expected one argument` → `bad_args`，白掉两轮
- **根因**：参数 → argv 组装不看 schema 声明的类型，布尔分支对所有参数一视同仁
- **修法（按 schema 声明类型组装，v0.2.2）**：
  - 数值型（integer/number）收到 `true`：`wait_rc` **强制为 MCP 层上限 45s**（"我要等"的本意），
    其余数值参数（`lines`/`offset`/`limit`/`max_output`/`idle_timeout`/`max_time`/`parallel`/`port`/
    `timeout`）给**可读 bad_args**："该参数需要数值（例如 lines: 100），不是开关"
  - 布尔型参数额外接受字符串 `"true"/"false"`（含 `yes/no/on/off/1/0`），以及 1/0
  - 数值型收到 `false`、或 `wait_rc: 0` → 视为"不启用该行为"（不传该参数，不再报"必须为正整数"）
  - 字符串型参数收到布尔 → 可读 bad_args（"该参数需要字符串"）
  - 所有容错改写都**并入结果 `warnings`**（`[MCP 参数容错] …`）+ 打 server stderr——让模型知道
    参数被改写过，而不是以为被无视
- **测试**：离线 34 → 39 例（`T17a-e`：`wait_rc=true` 强制上限且有回报 / `lines=true` 可读 bad_args /
  `detach="true"` 当旗标 / `wait_rc=0` 视为不启用 / `password=true` 可读 bad_args；全部用
  `127.0.0.1:1`（本地立即 ECONNREFUSED）判定"错误不是 bad_args 即证明已过 argparse"，不碰外部主机）；
  真机套件 `test/test_live_bg.py` 11 → 12 例（`B5` 改用 `wait_rc=True` 实测强制上限后等结束拿
  `exit_code=0`，`B5b` 断言 `warnings` 里有容错回报）
- **真机验证数据**（B2）：12/12 PASS；离线 39 + 凭据 6 全绿

## [0.2.3] - 2026-09-13

### 跟随 CLI v2.2.2：清理守卫（运行中 cleanup 被拒）+ force 出口

- **固定副本同步至 CLI v2.2.2**（md5 `3a2128668d1c4a3e7e2877f639b32861`，与仓库根 / `skills/pyaissh/` 逐字节一致）
- **`cleanup=true` 加状态守卫**：作业仍 `running` 时 CLI 返回 `job_running`（不再照删）——此前会把
  `job.pid`/`job.log`/`job.rc` 一并删掉，`list` 归零、工具彻底失去追踪，而远端进程仍在跑；
  MCP 侧错误映射为 `isError=true`，AI 会看到 `job_running` + `pid` + `hint`（`kill+cleanup` 一步到位）
- **新增 `force` 参数**（对应 `log --force`）：与 `cleanup` 同用表示"明知在跑也要删"，结果带
  `forced_cleanup:true` + warnings 明示"已失去追踪、进程可能仍在跑"；`force` 缺 `cleanup` → 连接前 `bad_args`
- **整组杀澄清**（v0.2.1 起已有，本次补真机断言）：`kill` 走进程组（setsid 后 pid==pgid），
  不会留下被 reparent 的 `job.sh` 子进程孤儿
- **测试**：离线 39 → 40 例（`T18`：`force` 缺 `cleanup` → bad_args）；真机套件 12 → 15 例
  （`B12` 运行中 `cleanup` 被拒且目录保留、`B13` `kill+cleanup` 一步到位（dead + 已清理）、
  `B14` `kill` 整组无孤儿——用 `[s]leep` 技巧避免 pgrep 自匹配）
- **真机验证数据**（B2）：15/15 PASS；离线 40 + 凭据 6 全绿

## [0.2.4] - 2026-09-13

### 打包：本目录成为自包含分发包（含完整 pyaissh 与技能文档），测试不出包

- **内含完整 pyaissh 副本 + 技能文档**：除既有 `pyaissh.py`（CLI 主体）外，新增同步 `pyaissh` /
  `pyaissh.cmd`（POSIX/Windows 入口）、`SKILL.md`、`docs/*.md`（七篇技能文档）、
  `CHANGELOG.md → CLI_CHANGELOG.md`（改名避免与适配器 CHANGELOG 冲突）。本目录拷出去单独放即可
  直接运行 CLI，也可直接当技能包使用（README 增「包内容 / What's in this directory」表）
- **`sync_check.py` 升级为整包同步**：由"只校验 `pyaissh.py`"改为按清单（12 个文件）逐一 md5
  校验/同步；单一源仍是 `skills/pyaissh/`——只改那里，然后 `python sync_check.py --update`
- **测试不出包**：`test/` 从仓库移除（根 `pyaissh-mcp/test/` + 目录内 `.gitignore` 双重忽略，
  本地开发树保留并继续可跑）——分发包只含运行所需
- 本次为**打包与文档调整**，适配器代码逻辑零改动（`SERVER_VERSION` 随之标记 0.2.4）
- **入仓前脱敏**：历史条目里的真机 IP 替换为 `<node1>`/`<node2>` 占位符；暂存内容经严格扫描
  确认无明文凭据（凭据文件 `.env` / `test/local_creds.json` 均被忽略）





