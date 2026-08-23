# pssh 更新日志（CHANGELOG）

> **维护约定（每次更新必须遵守）**：
> 1. 每次更新/修改/修复 pssh，必须在本文件**末尾追加一条记录**——**最新在最后**，文件只增不减，历史条目一律保留、**禁止覆盖或删除**。
> 2. 为什么"最新在最后"而不是"最新在顶部"：**末尾追加是对 AI 最安全的操作**——天然支持 `>>`、编辑器定位到文件末尾、或 edit 工具以文件最后一行作锚点；不存在"往文件开头插入"这种容易误伤标题/维护约定/历史条目的高风险操作，也符合"追加"的字面语义。
> 3. **禁止整文件重写覆盖**。若确实用整文件写入方式更新（如 write 工具），只允许在末尾新增并保留全部历史内容；推荐优先用追加式写入。
> 4. 条目版本号与 `pssh.py` 的 `VERSION` 常量保持一致；条目含日期，按「新增 / 修改 / 修复 / 文档」分类，说明改了什么、为什么改、影响什么（行为/参数/JSON 字段/错误语义变化要写清，AI 靠 `version` 字段与这些说明判断行为差异）。
> 5. 技能目录（`.reasonix/skills/pssh/`）与根目录（`D:\工作目录\Leopold\pyssh\`）各有一份 `pssh.py` / `CHANGELOG.md`，改完**两份同步**（新增条目同样追加到两份的末尾）。

---

## 1.5.0 之前（未系统记录）

本文档自 1.5.0 起开始维护，更早版本未逐一记录。从代码/文档可考的部分里程碑（非完整）：
- v1.4.8：known_hosts 写盘原子化（Linux flock + 临时文件 + os.replace，Windows 原子替换，并发首次连接不丢记录）；极早期信号窗口 handler 提前到模块顶层注册（import 阶段的信号输出结构化 `interrupted` JSON + 退出码 130）。
- v1.4.9：跳板机未配置专属凭据时密码自动回退使用 `PSSH_PASSWORD`（密钥不回落）；多级超时防挂死细化。
- v1.3：远端路径 `~` 自动展开。
- 更早功能（分片下载 `--parallel`、`.part` 原子传输、`--cmd-file -`、错误类型化等）的引入版本待补。

## [1.5.0] - 2026-08-23

### 新增
- exec 新参数 **`--spill-dir <目录>`**：输出被截断时把**完整原始输出**（含内存层丢弃的中间字节——读线程边收边写、排空阶段也写）落盘，JSON 回传 `stdout_spill_file` / `stderr_spill_file` 路径，AI 需要中间内容时直接读文件，无需重跑 `sed -n` 或调大 `--max-output`；未截断自动删除、异常路径 `finally` 兜底清理，不留垃圾。默认目录为系统临时目录，文件命名 `pssh-<stdout|stderr>-<随机>.spill`。
- exec 新参数 **`--no-credential-warn`**：关闭"命令含疑似凭据"的启发式 WARN（误报时用；关闭后命令里真实凭据不再被提示，`cmd` 字段仍原样回显，脱敏责任回到调用方）。

### 修改
- **凭据 WARN 启发式修复误报**：`-p` 紧贴形态正则加 `(?<!-)` 前缀——`--profile` / `--parallel` / `--progress` 等双横线长选项不再被当成 `-psecret` 误报；`-psecret`、`-p secret`、`--password=x`、`mysql -u root -p`、`curl -u user:pass` 等真实凭据形态仍正常告警（10 组正反例验证）。
- `warn_sensitive_cmd()` 增加 `enabled` 参数（由 `--no-credential-warn` 控制）。

### 文档
- `docs/edge-cases.md`：新增**远程命令自杀伤**边界条目——`pkill -f "dsh web"` 这类按自身 cmdline 模式匹配的杀进程命令会把自己（承载 SSH 会话的 bash）一起杀掉 → `connection_lost`、结果不可信；规避方法：`pkill -f '[d]sh web'` 括号转义 / `pgrep -f` 先核对 PID / 模式避开自身文本。
- `SKILL.md`：已知边界列表新增 pkill 自杀伤短条目；其他边界枚举补上该项。
- `docs/exec.md`：补充 `--no-credential-warn`、`--spill-dir` 与 spill 字段（`stdout_spill_file` / `stderr_spill_file`）说明。

## [1.5.1] - 2026-08-23

### 修复
- **stderr/stdout 中文乱码（Windows 管道/import 路径）**：`_setup_console_utf8()` 原先只在 `main()` 里调用，`python -c "import pssh"`、AI 嵌入、测试 harness 等 **import 路径**下输出流保持系统区域编码（GBK/cp936），中文日志（WARN 等）经 UTF-8 解码成乱码。改为**模块级立即调用**（`main()` 保留原调用作幂等兜底），脚本与 import 两条入口路径的 stdout/stderr/stdin 恒为 UTF-8（errors=replace）。

## [1.5.2] - 2026-08-23

### 修改（内部重构，无行为变化；先建 git 基线再动手，可随时回滚）
- **魔数集中到文件顶部 `_CONSTANTS` 区**：约 20 个常量统一收口——`MAX_TIME_CAP=1200`（原散落 ~10 处）、`SFTP_IO_TIMEOUT=30`、`PARALLEL_MIN_SIZE=8MB`、`PARALLEL_IO_TIMEOUT=120`、`RECV_CHUNK=64KB`、`DEFAULT_MAX_OUTPUT=262144`、`PARALLEL_READ_CHUNK=262144`（与前者同值不同义、分开命名）、`RESPONDER_GRACE=0.2`、`WATCHDOG_TICK=5`、`POLL_TICK=0.05`、`BUF_ALIGN_WINDOW=4096`、`MIN_BUF_FLOOR=4096`、`MAX_PORT=65535`、`DRAIN_WINDOW`/`STATUS_GRACE`/`STDERR_EOF_WINDOW`/`SILENCE_GRACE`/`JOIN_GRACE`/`RETRY_SLEEP`/`PUT_RETRY_SLEEP` 等。逻辑与动态错误消息统一引用常量，调参只改一处；静态 help/epilog 文本保持字面量（属文档范畴，随文档走）。
- **正则集中与片段化**：`parse_target` 的 3 个内联 `re.fullmatch` 与 `_win_safe_rel_path` 的字符类清洗上提为模块级编译常量（`_RE_IPV4` / `_RE_IPV6_SEG` / `_RE_IPV6_ZONE` / `_RE_WIN_ILLEGAL`）；`_SENSITIVE_CMD_RE` 巨型 alternation 拆为带注释的命名片段（`_P_SENS_*`）再拼接，每个分支可独立注释/测试。
- **验证（零行为变化证明）**：`_SENSITIVE_CMD_RE` / `_ANSI_RE` 的 `.pattern` 与重构前 git 基线逐字节一致；L4 正反例 38 例匹配行为一致；本地单元回归 `verify_r3` 54/54（含 L4 矩阵 40 例）、极早期信号单元 3/3、进程内复用 40/40 全过；双机冒烟（exec/test）通过。顺带修正：测试脚本里硬编码的旧版本号断言改为合法版本模式匹配（不再随版本漂移）。
- **补漏（同轮收口）**：复审发现 4 处"同值但违背调参只改一处"的遗漏并修正——`cmd_test` 一处 16 空格缩进的 `recv_stderr(65536)`（replace_all 只覆盖了 20 空格版本）；`parse_target`/`resolve_conn`/`_port` 三处错误消息里的 `(1-65535)` 字面量改为 `(1-%d) % MAX_PORT`；`_fix_msys_local_path` 的 `timeout=5`（cygpath 子进程超时）收口为新常量 `CYGPATH_TIMEOUT=5`。docstring/help/epilog 文本保持字面量（属文档范畴）。
- **代码地图（AI 可维护性）**：文件顶部 `VERSION` 下新增"代码地图"——按区域列出关键函数与对应 docs 子文档（函数名作锚点、不写死行号），AI 改代码路径 = 文档导航 → 地图定位 → grep 函数名，无需理解包结构。
- **凭据正则验收案例表（使用者 AI 自查护栏）**：`_SENSITIVE_CMD_RE` 定义处新增 29 条正反例注释（含历史回归点：`--profile`/`--parallel`/`--progress` 双横线误报、`-p 22`/`-p'22'` 纯数字端口、`mysql -u root -p`、工具 `-p` 排除表）——使用者 AI 修正则后对照注释自查，无需测试框架（已逐条验证与真实行为一致）。
