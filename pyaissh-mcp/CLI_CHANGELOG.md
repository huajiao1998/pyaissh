# pyaissh 更新日志（CHANGELOG）

> **维护约定（每次更新必须遵守）**：
> 1. 每次更新/修改/修复 pyaissh，必须在本文件**末尾追加一条记录**——**最新在最后**，文件只增不减，历史条目一律保留、**禁止覆盖或删除**。
> 2. 为什么"最新在最后"而不是"最新在顶部"：**末尾追加是对 AI 最安全的操作**——天然支持 `>>`、编辑器定位到文件末尾、或 edit 工具以文件最后一行作锚点；不存在"往文件开头插入"这种容易误伤标题/维护约定/历史条目的高风险操作，也符合"追加"的字面语义。
> 3. **禁止整文件重写覆盖**。若确实用整文件写入方式更新（如 write 工具），只允许在末尾新增并保留全部历史内容；推荐优先用追加式写入。
> 4. 条目版本号与 `pyaissh.py` 的 `VERSION` 常量保持一致；条目含日期，按「新增 / 修改 / 修复 / 文档」分类，说明改了什么、为什么改、影响什么（行为/参数/JSON 字段/错误语义变化要写清，AI 靠 `version` 字段与这些说明判断行为差异）。
> 5. 技能目录（`skills/pyaissh/`）与根目录（`D:\工作目录\Leopold\pyssh\`）各有一份 `pyaissh.py` / `CHANGELOG.md`，改完**两份同步**（新增条目同样追加到两份的末尾）。

---

## 1.5.0 之前（未系统记录）

本文档自 1.5.0 起开始维护，更早版本未逐一记录。从代码/文档可考的部分里程碑（非完整）：
- v1.4.8：known_hosts 写盘原子化（Linux flock + 临时文件 + os.replace，Windows 原子替换，并发首次连接不丢记录）；极早期信号窗口 handler 提前到模块顶层注册（import 阶段的信号输出结构化 `interrupted` JSON + 退出码 130）。
- v1.4.9：跳板机未配置专属凭据时密码自动回退使用 `PYAISSH_PASSWORD`（密钥不回落）；多级超时防挂死细化。
- v1.3：远端路径 `~` 自动展开。
- 更早功能（分片下载 `--parallel`、`.part` 原子传输、`--cmd-file -`、错误类型化等）的引入版本待补。

## [1.5.0] - 2026-08-23

### 新增
- exec 新参数 **`--spill-dir <目录>`**：输出被截断时把**完整原始输出**（含内存层丢弃的中间字节——读线程边收边写、排空阶段也写）落盘，JSON 回传 `stdout_spill_file` / `stderr_spill_file` 路径，AI 需要中间内容时直接读文件，无需重跑 `sed -n` 或调大 `--max-output`；未截断自动删除、异常路径 `finally` 兜底清理，不留垃圾。默认目录为系统临时目录，文件命名 `pyaissh-<stdout|stderr>-<随机>.spill`。
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
- **stderr/stdout 中文乱码（Windows 管道/import 路径）**：`_setup_console_utf8()` 原先只在 `main()` 里调用，`python -c "import pyaissh"`、AI 嵌入、测试 harness 等 **import 路径**下输出流保持系统区域编码（GBK/cp936），中文日志（WARN 等）经 UTF-8 解码成乱码。改为**模块级立即调用**（`main()` 保留原调用作幂等兜底），脚本与 import 两条入口路径的 stdout/stderr/stdin 恒为 UTF-8（errors=replace）。

## [1.5.2] - 2026-08-23

### 修改（内部重构，无行为变化；先建 git 基线再动手，可随时回滚）
- **魔数集中到文件顶部 `_CONSTANTS` 区**：约 20 个常量统一收口——`MAX_TIME_CAP=1200`（原散落 ~10 处）、`SFTP_IO_TIMEOUT=30`、`PARALLEL_MIN_SIZE=8MB`、`PARALLEL_IO_TIMEOUT=120`、`RECV_CHUNK=64KB`、`DEFAULT_MAX_OUTPUT=262144`、`PARALLEL_READ_CHUNK=262144`（与前者同值不同义、分开命名）、`RESPONDER_GRACE=0.2`、`WATCHDOG_TICK=5`、`POLL_TICK=0.05`、`BUF_ALIGN_WINDOW=4096`、`MIN_BUF_FLOOR=4096`、`MAX_PORT=65535`、`DRAIN_WINDOW`/`STATUS_GRACE`/`STDERR_EOF_WINDOW`/`SILENCE_GRACE`/`JOIN_GRACE`/`RETRY_SLEEP`/`PUT_RETRY_SLEEP` 等。逻辑与动态错误消息统一引用常量，调参只改一处；静态 help/epilog 文本保持字面量（属文档范畴，随文档走）。
- **正则集中与片段化**：`parse_target` 的 3 个内联 `re.fullmatch` 与 `_win_safe_rel_path` 的字符类清洗上提为模块级编译常量（`_RE_IPV4` / `_RE_IPV6_SEG` / `_RE_IPV6_ZONE` / `_RE_WIN_ILLEGAL`）；`_SENSITIVE_CMD_RE` 巨型 alternation 拆为带注释的命名片段（`_P_SENS_*`）再拼接，每个分支可独立注释/测试。
- **验证（零行为变化证明）**：`_SENSITIVE_CMD_RE` / `_ANSI_RE` 的 `.pattern` 与重构前 git 基线逐字节一致；L4 正反例 38 例匹配行为一致；本地单元回归 `verify_r3` 54/54（含 L4 矩阵 40 例）、极早期信号单元 3/3、进程内复用 40/40 全过；双机冒烟（exec/test）通过。顺带修正：测试脚本里硬编码的旧版本号断言改为合法版本模式匹配（不再随版本漂移）。
- **补漏（同轮收口）**：复审发现 4 处"同值但违背调参只改一处"的遗漏并修正——`cmd_test` 一处 16 空格缩进的 `recv_stderr(65536)`（replace_all 只覆盖了 20 空格版本）；`parse_target`/`resolve_conn`/`_port` 三处错误消息里的 `(1-65535)` 字面量改为 `(1-%d) % MAX_PORT`；`_fix_msys_local_path` 的 `timeout=5`（cygpath 子进程超时）收口为新常量 `CYGPATH_TIMEOUT=5`。docstring/help/epilog 文本保持字面量（属文档范畴）。
- **代码地图（AI 可维护性）**：文件顶部 `VERSION` 下新增"代码地图"——按区域列出关键函数与对应 docs 子文档（函数名作锚点、不写死行号），AI 改代码路径 = 文档导航 → 地图定位 → grep 函数名，无需理解包结构。
- **凭据正则验收案例表（使用者 AI 自查护栏）**：`_SENSITIVE_CMD_RE` 定义处新增 29 条正反例注释（含历史回归点：`--profile`/`--parallel`/`--progress` 双横线误报、`-p 22`/`-p'22'` 纯数字端口、`mysql -u root -p`、工具 `-p` 排除表）——使用者 AI 修正则后对照注释自查，无需测试框架（已逐条验证与真实行为一致）。
- `docs/errors.md`：文末新增"凭据 WARN"注记，指向 `pyaissh.py` 中 `_SENSITIVE_CMD_RE` 定义处的判定形态与已测案例。

## [1.5.3] - 2026-08-23

### 新增
- **断点续传 `--resume`（upload/download，仅单文件）**：中断/失败后保留续传点（上传=远端 `.part`，下载=本地 `.part`），重试加 `--resume` 从断点继续，不重传已传部分。**默认不启用**（不加时行为与旧版完全一致）；单文件 ≥ 50MB（常量 `RESUME_MIN_SIZE` 可调）且未启用时，stderr `[TIP]` + 结果 `warnings` 提示建议启用。要点：
  - 基于**大小**的续传：`.part` 已有 N 字节就从 N 继续；**`.part` ≥ 源大小视为损坏/过时 → 覆盖重传**（绝不续坏尾巴）；完成后大小校验 + 原子改名
  - `.part` 用**固定名**（`<目标>.part`）而非进程唯一名——`--resume` 模式**禁止并发写同一目标**（文档明示）
  - 下载分片（`--parallel`）续传：每分片完成后写 `<目标>.part.done.<i>` 标记，重试时**跳过已完成分片**省传输量，全部完成清理标记
  - 目录传输忽略 `--resume`（WARN 提示）；中断保留续传点时 warnings 明示（"续传点已保留"），放弃续传手动删除 `.part` 即可
- **真机验证 20 例全过**（新机器二 103.79.186.77）：50MB 上传/串行下载/分片下载的 中断→`--resume` 重试→md5 一致 + stderr `[RESUME]` 证明真续传 + 分片 done 跳过后 md5 仍一致 + 大文件 TIP 提示 + 目录 `--resume` 忽略；双机普通往返（非 resume）md5 一致，默认行为零变化。**另以 100MB 文件补测 15 例全过**（含真实中断命中"部分分片完成"场景：5s 已收 75.2MB → done 跳过后续传 md5 一致）。

## [1.5.4] - 2026-08-23

### 修改
- **`cmd` 字段回显截断（防撑爆调用方上下文）**：结果 JSON 的 `cmd` 超过 `CMD_ECHO_LIMIT`（8KB 常量）时保留头尾 + 中间省略标记，新增恒有键 **`cmd_truncated`**（false=完整），warnings 提示"cmd 字段已截断（完整命令在 --cmd-file 本地文件可重读）"。`--cmd-file -` 读入 100KB 大脚本时单行 JSON 不再到 MB 级（实测 118901 字节脚本 → cmd 字段 8256 字节）；成功与失败路径（`_partial_extra`）都截断；凭据检测用完整 cmd 不受影响。**真机验证**：大脚本成功/失败路径 `cmd_truncated: true` + 头尾保留 + 输出/退出码正常，小命令 `false`。
- **`cmd` 截断改字节级（补漏）**：判断与计数改用 `len(cmd.encode("utf-8"))` 而非字符数——多字节内容（中文）下旧实现低估 2-3 倍（marker 报错字节数），且字符数 < 8192 但字节数超限的"该截断没截断"（3000 中文字符 = 9000 字节）。截断边界用 `_utf8_boundary_cut` 对齐合法字符不切半字；marker 措辞改"完整命令见原始调用（--cmd-file 时为本地文件可重读）"（--cmd 来源无本地文件）。单元 5 例 + 真机 36028 字节中文脚本验证（marker 精确报字节数、头尾中文保留）。

### 文档
- `docs/exec.md` / `docs/contract.md`：`cmd_truncated` 字段与截断语义。
- `docs/transfer.md`：`--skip-existing` 仅比大小（原子传输保证 pyaissh 自产最终文件完整；外部损坏可 md5 抽查）。
- `docs/errors.md`：退出码 254 歧义提示（区分远程真实 254 vs 远程 255 映射，看 JSON 双字段）。

## [1.5.5] - 2026-08-23

### 修改
- **paramiko 惰性 import（启动提速）**：`import paramiko` 从模块顶部挪进 `_do_connect`（唯一建连入口），`_AtomicAutoAddPolicy` 类改为 `_atomic_auto_add_policy()` 工厂（首次调用时 import + 定义 + 缓存单例）。**不需要连接的路径提速 ~2.7 倍**：`--version`/`--help`/`bad_args`/缺用户名/别名未配置 从 ~296ms 降到 ~110ms（纯解释器+标准库基线 59ms，剩余为模块解析冷启动）；极早期信号窗口更短（handler 注册后只剩标准库 import，paramiko 的 ~190ms 不再落在窗口内）。真实连接路径不受影响（paramiko 照常在建连时加载，实测 1766ms 连接正常）。实测依据：paramiko import 188ms（其中 `paramiko.config → invoke` 可选依赖链 ~120ms，pyaissh 不用 SSHConfig，但 invoke 是否加载取决于环境安装，代码侧无法卸载；惰性只优化错误路径，真实路径提速需环境侧卸载 invoke）。
- **惰性 import 回归修复（第 5 轮审查抓出）**：函数内 `import paramiko` 默认绑定**局部**名，而 `cmd_download`/`cmd_ls`/`_sftp_put_atomic` 引用的是**模块全局** `paramiko`——顶部 import 删除后，这三处的 `getattr(paramiko, "SFTP_NO_SUCH_FILE", 2)`（"路径不存在"错误分类路径）抛 `NameError` 被误归类为 `download_failed`/`ls_failed`（实测"下载不存在远程 → download_failed/1"而非 `bad_args/2`）。修复：惰性 import 处加 `global paramiko` 绑定全局（`_do_connect` 与 `_atomic_auto_add_policy` 两处）。成功路径测试（verify_r3/双机 test/s2_*）均测不到此缺陷，冒烟矩阵的"下载不存在"用例抓出——验证了错误路径用例的价值。修复后双机 download/ls 不存在路径全部 `bad_args/2`。
- **MSYS 路径转换补漏（`--cmd-file` / `--spill-dir`）**：`_fix_msys_local_path`（v1.5.1 起用于 `--local`）漏了两个新参数——Git Bash 经 `./pyaissh` 包装器（MSYS_NO_PATHCONV=1）运行时，`--cmd-file /tmp/x.sh` 被 Windows Python 解析成盘根而报 `read_cmd_failed`（Errno 2），`--spill-dir /tmp` 的 spill 落错位置（D:\tmp 而非 Git Bash 的 /tmp）且无提示。修复：两处均套用 `_fix_msys_local_path`（内部含 `~` 展开；非 Windows / 无 MSYSTEM / 相对路径原样返回，不影响 Linux 与普通终端）。验证：单元 5 分支全过；真实 Git Bash（MSYS_NO_PATHCONV=1）集成——`--cmd-file /tmp/gb_test.sh` 成功执行、`--spill-dir /tmp` 的 spill 文件落在 cygpath 转换后的真实位置（D:\DSH\temp）且存在。

## [1.5.6] - 2026-08-23

### 新增
- **错误 JSON 新增 `retryable` 字段（机器可读的重试决策）**：所有错误 JSON（含 argparse 层 `bad_args`）恒带 `retryable`（bool）——AI 自动重试策略直接读它，不必解析 message 文本。映射表集中定义（常量区 `_RETRYABLE_ERRORS`）：**true** = 网络/传输/中断类（`connection_timeout`/`connection_refused`/`connection_failed`/`dns_failed`/`connection_lost`/`interrupted`/`upload_failed`/`download_failed`/`upload_timeout`/`download_timeout`/`exec_idle_timeout`/`exec_total_timeout`/`exec_timeout`）；**false** = 凭据/参数/本地文件/命令失败类（`auth_failed`/`host_key_rejected`/`bad_args`/`read_cmd_failed`/`exec_failed`/`jump_failed`）。语义边界：exec 超时类 true 仅表示"值得一试"——远程进程可能仍在运行/命令可能有副作用，重试前必须读 message 的"远程进程可能仍在运行"提示并先 pgrep 确认（bool 给机器"值不值得试"，message 给"怎么试才安全"）。成功结果无此字段。**真机验证 11 例全过**（逐类错误触发确认 retryable 值：auth_failed=false、connection_timeout=true、bad_args=false（argparse 层修复后）、exec_idle_timeout=true、interrupted=true、成功结果无字段等）。
- **单文件上传自动建父目录的可见性（新发现，真机抓的）**：`upload --remote /x/y/z.bin` 本就自动 mkdir -p 父目录（与 scp 预期不同、且文档未记载单文件场景、无任何提示——拼写错误（如 `/usr/loca/bin/x`）会静默造垃圾目录树，与尾斜杠"绝不静默创建"的严格语义不对称）。修复：`sftp_makedirs` 改为返回**本次新建的目录列表**（外层→内层，已存在不算），单文件上传分支新建父目录时 `warnings` + stderr `[MKDIR]` 提示创建了哪些（AI 可见可发现）；父目录已存在不提示；目录上传的自动创建是文档明示行为保持静默；尾斜杠语义不变（bad_args）。docs/transfer.md 补"单文件上传自动创建远端父目录"条目（与尾斜杠语义的区别一并说明）。**真机验证 4 例全过**：新建父目录提示+远端建出、已存在无提示、尾斜杠 bad_args 不变、目录上传静默。
- **依赖缺失独立分类 `dependency_missing`**：环境未装 paramiko 时（`import paramiko` 在 `_do_connect` 抛 ModuleNotFoundError），此前实测误归 `connection_failed` 且 **`retryable: true`**——误导 AI 查网络、且白白重试。修复：`_do_connect` 的 import 包 try/except，转 `SshError("依赖缺失: ...与目标主机/网络无关，重试前请先安装依赖", "dependency_missing")`；不在 `_RETRYABLE_ERRORS` → retryable=false。exec/test/download 三条路径实测全部正确分类（`python -S` 模拟无 paramiko 环境）；正常环境无回归。docs/errors.md 错误表新增该行。

## [1.5.7] - 2026-08-24

### 修改
- **retryable 映射哲学统一（补 ls_failed/ls_timeout/test_failed）**：v1.5.6 初版映射漏了三个同性质类型——`ls_failed`/`ls_timeout` 与 upload/download 的 `_failed/_timeout` 完全同性质（SFTP 网络/通道问题），`test_failed` 是连接成功后的通道/传输异常兜底（test 只跑系统查询，命令本身几乎不会失败；信号中断已单独归 interrupted）。三者挪入 true 集，统一原则："**所有 SFTP 传输/超时类 + test_failed → true**；凭据/参数/本地文件/命令失败类 → false"。验证：静态映射确认 + emit_error 单元输出（ls_failed/ls_timeout/test_failed → true，auth_failed/bad_args → false）；真机 ls 权限拒绝触发因 root 忽略权限位不可行（环境限制，映射逻辑由单元覆盖）。

### 文档
- **零 token 传输卖点文档化**：pyaissh 从设计上就不把文件内容回传 JSON（upload/download 结果只含 `files`/`bytes`/`file_list` 元数据）——对比 MCP SSH 生态普遍把传输内容塞进 LLM 上下文的通病，这是天然卖点。SKILL.md（description + 定位段）与 `--help` epilog 新增"传输零 token 消耗"说明（实测：1MB 随机文件传输后结果 JSON 仅 410 字节纯元数据）。
- **品牌与命名统一为 pyaissh（开源发布准备）**：仓库/命令/文件名/文档全量统一为一个名字——`pyaissh.py`、`pyaissh.cmd`、bash 包装 `pyaissh`、`--help` 的 prog、输出标记（`[pyaissh: 已截断]`、seam/`[pyaissh]` 前缀）、spill 文件前缀（`pyaissh-stdout-`）、SKILL.md 与全部 docs 的调用示例与描述。环境变量 `PYAISSH_*`、内部属性 `_pyaissh_*`、技能目录 `skills/pyaissh/` 与 `SKILL.md name: pyaissh` 全项目一致。
- **环境变量与内部属性统一为 PYAISSH 前缀（破坏性变更）**：环境变量统一 `PYAISSH_*`（`PYAISSH_PASSWORD`/`PYAISSH_KEY`/`PYAISSH_USER`/`PYAISSH_PORT`/`PYAISSH_JUMP_KEY`/`PYAISSH_JUMP_PASSWORD`/`PYAISSH_HOST_<名称>`（含 `_PASSWORD`/`_KEY` 专属凭据）/`PYAISSH_ALLOW_CWD_ENV`，小写示例 `pyaissh_host_prod`）；内部属性统一 `_pyaissh_*`（`_pyaissh_home`/`_pyaissh_last_activity`/`_pyaissh_io_timeout`/`_pyaissh_watchdog`/`_pyaissh_watchdog_killed`/`_pyaissh_posix_rename_warned`）；测试 harness 的 `PYAISSH_PY`、测试脚本 env 全量同步；技能目录 `pyaissh`（`SKILL.md name: pyaissh`）。**注意**：部署/CI/.env 需使用 `PYAISSH_*` 前缀配置（发布前完成，无既有用户受影响）。验证：verify_r3 54/54、v3_sig_unit 3/3、s2_stale 通过、双机 test 正常。

## [1.5.8] - 2026-08-25

### 新增
- **上传分片 `--parallel`（dogfood 实测痛点修复）**：`upload` 新增 `--parallel 1-8`（对称下载分片）——高丢包/慢链路大文件上传提速（实测 318KB 单连接 22.7s 的痛点）。新函数 `_parallel_put`：k 条独立 SSH 连接各上传本地文件一段到远端同一 `.part`（主连接预创建空文件，worker r+b seek 写；共享跳板隧道；信号中断秒级退出；完成大小校验），原子改名单抽取 `_sftp_atomic_rename` 供串行/分片共用（posix-rename + 回退 + 双丢防护语义不变）。**默认行为零变化**（仅显式 `--parallel` 且 ≥64KB 才分片，无自动档）；与 `--resume` 互斥（同时给 WARN 忽略 `--resume`）；中断清理远端 `.part`（keep_part 双丢防护保留）。真机验证：50MB 分片 4 连接成功 + 远端 md5 一致 + 1MB 分片 + 互斥 WARN + 中断 130 且远端零残留。

### 修改
- **复杂命令失败提示 shell 转义（dogfood ①）**：`exec` 的 `--cmd` 来源命令含 shell 特殊字符（`$()`/反引号/换行/管道等）且执行失败时，错误 message 附加"建议改用 --cmd-file - 从 stdin 读脚本，绕过所有转义"（`_shell_escape_hint`；`--cmd-file` 来源与简单命令不加）。单元 6 例验证。

### 文档
- **凭据安全实践强化（dogfood ③）**：SKILL.md 快速开始与 docs/setup.md 新增"不要把 token/密码内联进 `--cmd` 或脚本内容——cmd 字段会原样回显，触发凭据 WARN；凭据走参数/env/.env 或脚本从文件读取"。发布类操作（git push token URL）同理。

## [1.5.9] - 2026-08-25

### 修改
- **shell 转义提示覆盖范围扩展（v1.5.8 修正）**：v1.5.8 的 hint 只在工具异常路径（exec_failed/超时）出现，但**最常见的"命令失败"（退出码非零，ok:true + exit_success:false）没有 hint**——而 PowerShell 吃 `\$` 的真实场景正是"命令行为诡异且退出码非零"。v1.5.9 起：退出码非零且 `--cmd` 含 shell 特殊字符时，warnings 附加 hint（不破坏 ok:true 语义，AI 照常按 exit_success 判断）。真机验证：`echo $(whoami) && exit 1` → warnings 含提示；`--cmd-file` 来源/无特殊字符/成功命令均不加。
- **上传分片"提速"声称诚实化（v1.5.8 修正）**：对照实测（10MB，同链路同文件）单连接 17.1s vs `--parallel 4` 15.9s = **1.08x，收益在噪声内**。诚实结论：分片收益原理上来自高丢包/长 RTT 链路（与下载分片相同）；B2（低丢包短 RTT）无显著加速属预期。功能正确性不受影响（md5 一致、中断清理、互斥 WARN 全部保持）。README/epilog 的"慢链路大文件上传分片提速"改为"高丢包/长 RTT 链路分片上传（收益随链路而定）"。

### 测试
- v60_verify 11 例：T1 分片对照（md5 双一致 + 提速数据）+ T2 hint 五场景 + T3 cmd 字段回显两模式。回归 verify_r3 54/54。

## [1.5.10] - 2026-08-25

### 修改
- **upload 结果补 `parallel_used` 字段（契约对称性修复）**：此前 `_parallel_put` 分支虽赋值 `parallel_used`（局部变量）但未放进结果 JSON——AI 上传后无法确认实际分片档位，与下载不对称，SKILL.md/transfer.md 的"实际档位见结果 `parallel_used` 字段"指引在上传方向落空。修复：cmd_upload 函数开头初始化 `parallel_used = 1`（单连接默认，非分片路径不再 NameError），分片分支赋值保留，结果 dict 新增 `"parallel_used": parallel_used`——下载与上传结果字段完全对称。真机验证：`--parallel 4` 上传 → `parallel_used: 4`，默认单连接 → `parallel_used: 1`；回归 verify_r3 54/54。
- **文档同步**：SKILL.md 速查第 7 条与 docs/transfer.md 改为"大文件传输慢或超时：加 --parallel 8"（覆盖上传，v1.5.8 起），并注明 `parallel_used` 字段下载与上传结果都有。

## [1.5.11] - 2026-08-25

### 修改
- **`_parallel_put` 失败清理窄缝补提示（P3 卫生修复）**：并行分片上传失败且所有 worker 连接已死（如传输中网络整体断开）、主连接兜底 remove 也失败时，远端 `.part` 会残留且此前 warnings 无清理提示（串行路径有 `_PUT_RESIDUE_WARNINGS` 兜底，并行路径漏了）。修复：清理循环全失败时记入 `_PUT_RESIDUE_WARNINGS`（"并行分片上传中断，远端临时文件可能残留: <part>（清理：rm -f ...）"），兑现 SKILL.md 第 7 条"warnings 会提示清理命令"的承诺。`.part` 名带 pid 下次不撞名，纯卫生问题不影响正确性。
- **contract.md 补 `parallel_used` 字段（契约文档权威性）**：docs/contract.md 第 9 行 upload/download 字段枚举补 `parallel_used`（v1.5.8 起，单连接=1，分片=--parallel 值，下载与上传都有）——此前 SKILL.md 指引"见结果 parallel_used 字段"在字段契约文档查不到。
- **README 版本号占位符化**：输出示例 `"version": "1.5.8"` → `"x.y.z"`、安装提示词版本号改为"随发布更新"——硬编码版本号每次发布都过时（上轮已提过），占位符一劳永逸。

### 测试
- 编译 + 回归 verify_r3 54/54；清理窄缝为代码审查 + 逻辑验证（真机需断网场景，不可行）。
- **（v1.5.11 文档修订）SKILL.md 速查第 8 条改为按调用环境选**：bash/常规 shell 下 `--cmd '...'` 完全可靠（标准引号规则），**仅 Windows PowerShell 调用时**复杂命令务必 `--cmd-file -`（PowerShell 会先解析 `$`/`\`）——此前"含特殊字符一律 --cmd-file -"的措辞对 Linux agent（部署主体）过度保守会误导；新增第 9 条 `file_list.path` 语义预警（upload=本地，download=远端，勿混用——transfer.md 有完整版，速查层补齐）；exec.md 示例注释同步。回归 54/54，SKILL.md 13961B。

## [1.5.12] - 2026-08-25

### 新增
- **`exec --encoding`（非 UTF-8 远端输出逃生口）**：远端 stdout/stderr 解码编码可指定（默认 utf-8，如 `--encoding gbk` / `shift_jis`；非法字节仍以 U+FFFD 替换不中断）。GBK/Shift-JIS 系统日志此前一律按 UTF-8 解码成乱码（AI 误判"执行失败"），现在按系统编码可正确读出。真机验证：远端 GBK 字节 `\xc4\xe3\xba\xc3` 默认 utf-8 → `���`，`--encoding gbk` → `你好`。成功与错误路径解码（`_partial_extra`）都生效。
- **超时错误 JSON 新增 `remote_may_be_running`（124 幽灵进程机器可读化）**：`exec_idle_timeout`/`exec_total_timeout`/`exec_timeout` 错误 JSON 恒带 `"remote_may_be_running": true`——AI 重试循环直接读该字段决定是否先 pgrep 确认（不再依赖记住文字提示），避免在额外负载下盲目起第二个重试实例。真机验证：`sleep 5 --idle-timeout 1` → 超时 JSON 含该字段。

### 文档
- **"stdout 恒单行 JSON"声明加例外**：SKILL.md 速查第 1 条、README（中文头条 + 能力表）补"（`--help` 纯文本除外）"——防止盲目 `json.loads(stdout)` 的 AI 代理在 `--help` 上 break（contract.md 原已标注例外，头条声明层补齐）。

## [1.5.13] - 2026-08-25

### 新增
- **`--encoding` 解析期校验（拼错立即 bad_args/2，不连远端）**：新增 `_encoding_type`（codecs.lookup 校验）接入 exec/test——此前 `--encoding utf-9`（拼错）能过 argparse、连上远端后才在解码期 LookupError，被通用 except 误归 exec_failed/255 误导 AI 查网络；现在拼错立刻 `bad_args` + `retryable:false` + 自纠提示（"未知编码 'utf-9'（如 utf-8 / gbk / shift_jis / latin-1）"）。真机验证：exec/test 的 `--encoding utf-9` → bad_args。
- **`test` 支持 `--encoding`**：服务器信息输出（hostname/os-release 等）解码编码可指定（默认 utf-8）——与 exec 一致，GBK 系统 os-release 不再乱码。

### 边界（诚实记录）
- **ls 的 GBK 文件名暂不支持（paramiko 层限制）**：曾实现 ls `--encoding`（surrogateescape 还原原始字节再按指定编码解码），实测发现 paramiko 5.0 已按 **UTF-8+replace** 解码 SFTP 文件名（`e.filename` 中是 U+FFFD，原始字节不可还原）——`--encoding gbk` 得到"锟斤拷"（U+FFFD 再解码的乱码），功能无效，**回滚**并在代码注释记录边界。非 UTF-8 文件名建议保持 UTF-8，或经 exec `ls -b`/base64 自行取原始字节。

### 修复
- **SKILL.md 标题"速查（先读这 8 条）"→ 9 条**：v1.5.11 加第 9 条时标题漏改（小笔误）。

### 测试
- 真机：exec `--encoding gbk` → "你好"（回归有效）、test `--encoding utf-9` → bad_args、回归 verify_r3 54/54、双份 md5 一致。
- **（v1.5.13 文档修订 + Linux 双平台验证）**：① SKILL.md 快速开始调用方式收敛为主路径 `python3 <pyaissh_dir>/pyaissh.py ...`（Windows cmd / Git Bash 折叠到 docs/setup.md）——Linux 智能体看主路径零犹豫；② 审查建议"file_list.path 预警"为 v1.5.11 第 9 条既有内容（无需重复）；③ 审查建议"--cmd 只用于 df -h、其余一律 --cmd-file"**不采纳**——与 v1.5.11"按调用环境选"决定相反（会使 Linux agent 过度保守，bash 下 --cmd 完全可靠）；④ **首次 Linux 平台回归**：服务器（Debian 13, Python 3.13.5, paramiko 5.0.0）跑 verify_r3 54/54 + SIG_UNIT + `test root@localhost` 全过——此前测试全程 Windows 开发机执行，双平台验证补齐。以后发布流程含 Linux 回归步骤。

## [1.5.14] - 2026-08-26

### 修复
- **`--remote ~/...` 被 MSYS 误判为 Windows home（远端目录污染 bug）**：Git Bash（MSYS）在参数传递前把 `~` 展开为 `/c/Users/<user>` 再 pathconv 成 `C:/Users/<user>/...`——`_fix_msys_remote_path` 只逆转了 TEMP 与 MSYS 根两种模式，Windows home 形态漏掉，导致 `--remote ~/x` 在远端创建 `/C:/Users/<user>/x` 垃圾目录树（含顶层 `C:`）+ 自动 mkdir -p 建一串垃圾父目录。修复：新增模式 3——`C:/Users/<本地用户名>/rest` 逆转回 `~/rest`（`--remote` 的 `~` 语义是远端用户 home，由 `_normalize_remote_path` 在远端展开）+ WARN（建议给 `~` 加引号或设 `MSYS_NO_PATHCONV=1`）；仅本地用户名匹配时逆转（远端 Windows 服务器上的 `C:/Users/<其他名>/...` 不受影响）。真机验证：Git Bash 真实复现 `--remote ~/pyaissh_ft_tilde.txt` → JSON `remote: /root/...`（远端 home）+ 远端无 `C:` 目录；单元 5 例（本机 home 逆转 / 非本机用户不动 / 无子路径不动）。回归 verify_r3 54/54，双份 md5 一致。

## [1.5.15] - 2026-08-28

### 新增
- **`exec --sudo` 提权执行（普通用户 sudo 提权，密码受控注入）**：登录普通用户、操作需 sudo 提权（如 Ubuntu 物理机）时的标准姿势。
  - 用法：`--sudo --cmd "apt update"` + 密码来源 `--sudo-password`（空串视为未设置）或 `PYAISSH_SUDO_PASSWORD` 环境变量（env 优先性：参数 > env）。
  - 组装：**简单命令**（无 shell 元字符）直连 `sudo -S -p '' <cmd>`——保留 sudoers NOPASSWD 按命令路径匹配（`NOPASSWD: /usr/bin/apt` 对 `sudo apt update` 生效）；**复合命令**（含 `&&`/`||`/`;`/管道/重定向/`$()`/反引号/换行）→ `sudo -S -p '' bash -c '<单引号转义>'` 整链提权（`&&` 第二段同样 root，防 sudo 只提权首命令）。`--cmd` 与 `--cmd-file` 两条路径统一处理。
  - 密码只经 SSH stdin 注入（写完即 close）：命令文本/cmd 字段/日志/远端磁盘均无密码；`-p ''` 压掉 `[sudo] password for ...` 提示符（成功路径 stderr 干净）；`--sudo` 与 `--pty` 互斥（bad_args）。
  - 无密码 `--sudo` → `sudo -n` 免密探测：免密命令直接跑；需密码立即失败不挂，且 exit 非零时 warnings 附密码配置提示（`--sudo-password` / `PYAISSH_SUDO_PASSWORD` / NOPASSWD）。
- **真机验证 10 例全过**（B2 建 tester/tester_np 用户 + sudoers）：T1 有密码提权 uid=0 / T2 `id && whoami && echo $UID` 三段全 root / T3 stderr 无 password for / T4 密码错失败 / T5 无密码简单命令 NOPASSWD 免密成功 / T6 无密码复合命令 sudo -n 失败 + warnings 提示 / T7 空密码视为未设置 / T8 --pty 互斥 bad_args / T9 cmd 字段无密码 / T10 --cmd-file 整链提权。回归 verify_r3 54/54（Win + Linux 双平台）。

### 设计说明
- **bash -c 包裹的取舍**：初版"所有 --sudo 一律 bash -c 包裹"实测暴露冲突——`NOPASSWD: /usr/bin/apt` 这类按命令配的免密规则匹配不上（sudo 看到的是 /usr/bin/bash）。改为**智能组装**：仅复合命令包裹（保证整链提权），简单命令直连（保留 NOPASSWD 按命令匹配）——两者兼得。
- **（v1.5.15 测试修复，测试 AI 反馈 3 项）**：① 凭据启发式改用**原命令**（`orig_cmd`）检测——组装后的 `sudo -S -p ''` 前缀命中 `_SENSITIVE_CMD_RE` 的 `-p` 模式导致 100% 误报"疑似凭据"，对"密码不进命令"的功能是自我打脸（T11 回归：无误报）；② **过时边界文档更新**——SKILL.md 已知边界与 docs/edge-cases.md 原写"sudo 密码无法用 stdin 管道（sudo -S 不适用）"，与 v1.5.15 功能矛盾，改为指向 `--sudo`（v1.5.15 起 sudo -S 经 SSH stdin 注入）；③ **sudo -n 失败提示收窄**——触发条件加 `stderr 含 password`（原只看 exit_code != 0：sudo 免密成功但命令本身失败如 `id 不存在用户` 会误报"需要密码"误导 AI 去配密码）（T12 回归：命令失败不误报）。另 docs/exec.md 补注：sudo 无法执行 shell 内建命令（`exit` 非可执行文件），内建请外套 `bash -c`。套件扩至 12 例全过 + 回归 54/54（Win + Linux）。
- **（v1.5.15 测试修复，C 门控二次收窄）**：③ 的门控"stderr 含 password"仍有理论缝——NOPASSWD 命令自身 stderr 恰好含 "password" 一词（如 "using password: YES" 后失败）仍误报。收窄为匹配 **sudo 报错特征**（`a password is required` / `no password was provided` / `password required`）——命令自身输出 "using password: YES" 等不再触发。单元 7 例（sudo 特征 3 类 True / 命令含 password 2 类 False / 无关空 False）+ 套件 12/12（T6/T7 仍触发）。
- **（v1.5.15 测试修复，E：sudo 失败混入误导性转义提示）**：`--sudo` + 密码错 + 复合命令时 warnings 只有"命令退出码非零且含特殊字符…改用 --cmd-file -"的转义提示（stderr 却是 `sudo: incorrect password`）——AI 被引向"改用 --cmd-file"而非正确动作"改 sudo 密码"。根因：`_shell_escape_hint` 不排除 sudo 上下文（--sudo 场景命令由工具组装，用户输入的转义问题已由 bash -c 包裹解决，hint 只会误导）；且 sudo 专用提示只在 -n 探测路径（无密码）触发，**密码错误的 -S 路径没有提示**。修复：① `--sudo` 场景成功/异常路径都**抑制转义 hint**；② -S 路径新增密码错误提示（stderr 匹配 `incorrect password`/`Sorry, try again`/`password mismatch` → warnings "sudo 密码错误：检查 --sudo-password / PYAISSH_SUDO_PASSWORD 是否与登录密码一致"）。真机验证：`--sudo --sudo-password WrongPass --cmd "id && whoami"` → warnings 只含"sudo 密码错误"；非 sudo 场景转义 hint 保留（`echo $(whoami) && exit 1` → 仍提示）。套件 12/12 + 回归 54/54。
- **（v1.5.15 测试修复，R7/R8：-S 密码错提示门控收窄到 sudo 专属报错）**：E 修复的 -S 门控正则（`incorrect password`/`Sorry, try again`/`password mismatch` 宽泛子串）与 C 门控同类——sudo 与命令的 stderr 同一 channel 无法区分来源，命令自己往 stderr 打 "Sorry, try again" 或 "password mismatch" 后失败会误报"sudo 密码错误"（R7/R8 实测复现）。收窄为匹配 **sudo 专属报错**（带 `sudo:` 前缀：`sudo: no password was provided` / `sudo: 1 incorrect password attempt` / `sudo: authentication failure` 等）——命令自身输出不再触发；漏报风险（sudo 报错文本不匹配）是"不误导"方向。单元 9 例（sudo 专属 3 类 True / 命令含词 3 类 False / 无关 False / -n 路径回归 2 例）+ 真机 R7（密码正确+命令打 Sorry try again → warnings 空）与 E（密码错 → 仍提示）双验证。套件 12/12 + 回归 54/54。
- **（v1.5.15 测试修复，配置入口与文档同步）**：① `.env.example` 补 `PYAISSH_SUDO_PASSWORD`（凭据配置第一入口此前漏了该变量——SKILL.md/exec.md 都写了，走 .env 路径的用户不知道存在，与 v1.5.11 "contract.md 缺 parallel_used" 同类问题）；② docs/setup.md 凭据段补 sudo 提权密码说明；③ docs/exec.md 的 -n 门控描述从"stderr 含 password"更新为"stderr 命中 sudo 报错特征（如 a password is required）"（二次收窄后文档滞后一轮，教 AI 依赖已收紧的判据）。
- **（v1.5.15 文档瘦身）**：SKILL.md 14882B → **12697B**（接近 15KB 上限，密集/低频内容下放子文档）：退出码表精简为一行粗筛（完整版 errors.md 已有）、子命令示例压缩（--parallel/--resume/--encoding 等低频细节指向对应 docs）、删除"参数默认值"段（`--help` 与文档导航覆盖）、安全规则与快速开始去重、已知边界保留 3 条核心（pty 交互/sudo、host key、pkill 自杀伤）其余指向 edge-cases.md。结构与速查 10 条完整保留。
- **（v1.5.15 测试修复，-n 门控与 -S 对称统一）**：`-n` 路径（无密码探测）提示正则补 `sudo:` 前缀要求，与 `-S` 路径同构——统一规则"sudo 提示只认 `sudo:` 前缀的报错行"（实测 sudo -n 报错恒带前缀，无漏报；命令自身 stderr 打出 "a password is required" 等不再误触发）。维护收益：两个门控行为一致，不再有"一处收窄一处宽"的隐蔽不对称（R7/R8 误报的根源模式）。单元 9 例 + 套件 12/12（T6/T7 的 -n 真机失败仍触发）。

## [1.5.16] - 2026-09-05

### 新增
- **`--field` 消费端字段提取（免 json.loads 样板）**：真实使用反馈——AI 会话中手写 `| python -c "import json,sys; print(json.load(sys.stdin)['stdout'])"` 样板 15+ 次，且曾因展示脚本只打印 stdout 字段把 stderr 报错吞掉。`--field` 直接打印结果字段裸值：`--field stdout` 打印 stdout 内容；**`-` 前缀字段打到进程 stderr**（`--field stdout,-stderr`——stderr 内容经进程 stderr 返回，不被 stdout 展示吞掉）；多字段逗号分隔每行一个；dict/list 值 JSON 序列化（ls entries 等）；字段不存在打 stderr 提示（拼错可发现）；与 `--text` 互斥（bad_args）；**仅作用于成功路径**——工具错误仍输出完整 JSON（AI 需要 retryable/message）；命令非零退出是成功路径结果（--field 提取结果字段，`-stderr` 能拿到报错）；**不用 --field 时 stdout 恒 JSON 契约零变化**。实现：`add_conn` 统一注册（5 个目标类子命令通用）→ `_emit_result` 封装（有 field 走 `_emit_fields`，否则原 emit）→ 5 处成功路径 emit 改调 `_emit_result`。真机验证 10 例全过（裸值/分流/失败 stderr 可见/工具错误完整 JSON/多字段/字段不存在提示/ls entries/互斥/默认契约不变）。回归 verify_r3 54/54。
### 文档
- docs/exec.md 补"**写远程脚本文件的推荐姿势**"：脚本写远端文件用 `--cmd-file -` heredoc（引住定界符零展开），不要 `--cmd 'cat > x << "EOF"'` 引号走钢丝（实测教训：`$`/反引号被本地 shell 展开）；含 `$` 的远程脚本 bash/Git Bash 下 `--cmd-file -` 同样是首选（不改速查第 8 条推荐，尊重 v1.5.11 决定，仅补场景指引）。SKILL.md 输出约定段 + docs/contract.md 补 `--field` 说明。

## [1.5.17] - 2026-09-06

### 修复
- **凭据 WARN 误报：`--no-pager` 命中"-p + 密码"形态（给开发 AI 的数据）**：`git log --no-pager` / `systemctl --no-pager` / `apt-get --no-pager` 等**高频合法命令**被误报"疑似凭据"——`no-pager` 中间的 `-pager`（`-p` 前是 `o` 非 `-`）绕过 v1.5.0 的 `(?<!-)` 防线（只挡双横线开头选项），命中紧贴形态。修复：**统一防线 `(?<![A-Za-z0-9-])`**——`-p` 作为密码选项时前字符必为空白/行首/引号，绝不可能是字母/数字/连字符，复合词中间的 `-p`（`--no-pager`、`a-px`）全部排除；三处形态（紧贴 `_P_SENS_P_ATTACH` / 引号 `_P_SENS_P_QUOTED` / 空格 `_P_SENS_P_SPACE`）统一改为引用 `_P_LOOKBEHIND_P`（一处定义防未来漂移）。验证：补充矩阵 41 例（15 真凭据全命中 + 26 不应命中含 --no-pager 全家 8 例）全过；原 L4 矩阵（verify_r3）54/54 无破坏；真机 `git log --no-pager` 零告警。

## [1.5.18] - 2026-09-06

### 修复/新增（真实长程任务反馈——40+ 调用跨 5 机含 332M 传输后沉淀）
- **A. `--field` stderr 盲区自动提示（根治，第三次教训代价最大）**：使用 AI 用 `--field stdout` 漏掉 `-stderr`，传输失败（188→145）的真实原因（服务商镜像禁用公钥认证 → `Permission denied (password)`）在 stderr 里被吞，多烧一轮排查。修复：`--field` 模式下若结果 `stderr` 非空且本次未提取 stderr，**自动在进程 stderr 打提示**（`[pyaissh: 结果含非空 stderr（N 字节）——用 -stderr 字段查看]`）——提示走进程 stderr 不污染 stdout 裸值，只读 stdout 的消费者也能察觉有 stderr 值得看；已请求 stderr/-stderr 则不重复提示。SKILL.md 标准示例改为 `--field stdout,-stderr`（不再把单字段当示例）。真机验证：`--field stdout` + stderr 非空 → 提示出现；`--field stdout,-stderr` → 无提示 + stderr 内容可见。
- **B. pkill 自杀伤条目补强（新形态）**：括号转义 `pkill -f '[d]ocker compose'` 只保护 pkill 自己那行——**同复合命令后面的 setsid 行合法包含字面量 `docker compose` 时 pkill 匹配整段 bash -c cmdline 照样炸会话**（863ms connection_lost，第二次踩雷）。edge-cases.md/SKILL.md 补：模式不得出现在自己命令行任何位置（含同脚本其他命令）；无法避免用**变量拼接**（`DC='docker'; pkill -f 'docker compose'; $DC compose up`——pkill 时无字面量，之后 `$DC` 展开）或**拆两次独立调用**。
- **C. exec.md 新增"长任务配方"小节**：apt 安装/332M 传输/镜像拉取等 2-10 分钟零输出操作**必然撞 idle-timeout（默认 60s）**——三次踩雷后形成的稳定模式：①先估时长调大 `--idle-timeout`（上限 1200s）+ 同步调大 `--max-time`；②传输类靠并行分片 + `.part` 原子性（中断安全）+ `--resume`/重跑 + 轮询远端大小对账；③循环类命令自己加**心跳输出**（周期 `echo` 让 idle-timeout 不触发）；④超 20min 上限 → 后台化 `nohup ... &` + 轮询日志；⑤中断后先 pgrep/tail 确认（`remote_may_be_running` 字段）再决定续传/重跑。
### 测试
- field 套件 10/10 + sudo 12/12 + verify_r3 54/54（含 A 代码改动的回归）。

## [1.5.19] - 2026-09-06

### 修复（--field 模式 stderr 信号/噪音死结）
- **`--field` 模式静音进度日志，stderr 只剩信号（使用 AI 设计洞察）**：v1.5.18 的 stderr 盲区提示存在**结构死结**——提示有效性依赖"消费者不屏蔽 stderr"，而消费者屏蔽（`2>/dev/null`）的动机恰是 stderr 上的进度噪音（`[SSH]`/`[OK]`/`[EXEC]` 对 --field 消费者零价值）：消费者用 `--field stdout 2>/dev/null` 时把提示和真实报错一起静音，盲区提示对"最有需要的那批调用"失效。修复：**不是教育用户别屏蔽，而是让屏蔽动机消失**——`--field` 模式下 log() 静音（模块级 `_QUIET`，main 解析出 `args.field` 后置位），`[WARN]` 级保留（凭据警告等信号），进度行丢弃。效果：`--field` 的 stderr 只可能出现 `[WARN]` + `_emit_fields` 提示（字段缺失/stderr 非空盲区）——全是信号，无噪音；错误路径不受影响（emit_error 走 stdout 完整 JSON，诊断本来就在 JSON 里）；非 `--field` 模式进度日志照旧。真机验证：`--field stdout` 成功 → stderr 空；stderr 非空 → stderr 只剩盲区提示；含凭据命令 → WARN 保留；非 field → `[SSH]`/`[OK]` 照旧。field 套件 10/10 + 回归 54/54。
### 文档
- contract.md `--field` 章节 + SKILL.md 输出约定同步："`--field` 模式 stderr 无进度日志、仅含信号（WARN/提示）——**不要再 `2>/dev/null`**"。

## [2.0.0] - 2026-09-06

### 重构（行为零变化——v1.5.19 的代码结构重组，非功能变更）
- **背景**：pyaissh.py 单文件 4460 行、cmd_exec 682 行巨函数、5 子命令连接样板重复——维护性到临界。方案经架构审查采纳四条护栏（①可变全局禁 from-import ②构建顺序用显式 MANIFEST ③先证构建器再搬 ④确定性构建+双形态测试）+ 用户决策"开发态多文件、发布态合成单文件"。
- **开发态 = 12 域文件**（`pyaissh-dev/domains/`，独立 git 可回滚）：`00_head`(文件头/极早期信号/VERSION) `01_globals`(常量/错误分类/可变全局) `02_cred_regex`(凭据启发式) `03_env_paths`(.env/MSYS 路径) `04_console_out`(log/emit 系) `05_util`(截断/hint/spill) `06_conn`(连接) `07_sftp_transfer`(传输原语) `08_cmd_exec` `09_cmd_transfer` `10_cmd_test_ls` `11_cli_main`(parser/main)——每个域文件带模块 docstring 代码地图（内容/关键符号/被谁引用）。
- **cmd_exec 彻底分段**：682 行 → 编排 `cmd_exec`(16 行) + `_prepare_exec_command`(组装/哨兵化) + `_connect_exec`(连接) + `_exec_session`(执行会话，内嵌闭包 `_partial_extra`/`_read`/`_drain_rest`)。语句零改动机械等价搬移。
- **构建器**（`pyaissh-dev/build_single.py`）：域 MANIFEST 显式顺序（护栏 2）+ 往返逐字节一致 + 确定性（护栏 4）。发布物 = join 单文件（分发形态不变）。
- **验证（重构前后行为一致性）**：金标往返逐字节一致 → 搬移期 12 域 join = 原文件；改码期 A 机 15/15 + B2 机 18/18 用例 JSON 逐字段一致（exec 各形态/pty/超时/截断/sudo/cmd-file/传输往返）+ 回归 54/54 + sudo 12/12 + field 10/10。
- **代码地图**：每个域文件带模块 docstring（内容/关键符号/被谁引用）；`pyaissh-dev/` 独立 git 全程可回滚。
- **构建器自动生成域边界横幅**：join 时在每个域拼接处插入 `# ===== [域 NN/12] <标题> =====`（标题从域 docstring 首行自动提取）——成品单文件与 12 域文档视觉对应，使用 AI 滚到任意位置知道在哪个域；纯注释、行为零变化、确定性构建。曾评估"函数行号索引"（文件尾跳转表）后**回退**：使用 AI 改文件后行号漂移会成为错误导航（横幅/docstring 是内容标记不依赖行号，稳定可用）。

## [2.1.0] - 2026-09-07

### 新增/修复（来自真实使用 AI 反馈，按烦人程度排序）
1. **`--field` 命令失败直接给 stderr 尾巴**（修复"多跑一轮真金白银"）：`--field stdout` + exit≠0 + stderr 未提取时，stderr 通道直接打**截断尾巴（1KB 封顶）**，不再只给"结果含非空 stderr"提示让 AI 再跑一次取 stderr；命令成功但 stderr 非空仍保持原提示（内容非报错不塞）。实测：pip 装依赖失败一次往返拿到真实报错。
2. **`host add` 子命令（多主机不同密码闭环）**：`pyaissh host add prod root@1.2.3.4 --password xxx` 把别名写进脚本同目录 .env（幂等更新；含 #/空格的密码自动引号包裹）→ 之后 `pyaissh exec @prod` 直接用别名专属凭据，不再逐条 `--password`（进程列表可见 + WARN 刷屏）。KEY 专属私钥同支持。
3. **upload `--exclude GLOB[,GLOB...]`**（部署排除）：目录递归时排除匹配项——目录整树剪枝、文件不上传不计数（命中文件名或相对路径的 fnmatch glob）；`--exclude node_modules,.git` 部署不再白传 11MB。
4. **凭据扫描豁免从文件读值**：命令含 `$(cat f)` / `$(<f)` 整条不报 WARN（值来自文件、不进命令行文本、无明文泄漏——DB_PASS=$(cat /srv/x) 类实测误报消除）；真凭据字面（-psecret 等）仍命中。
5. **exec `--progress [SECS]`（长任务心跳）**：命令静默/持续运行每 N 秒（默认 30）往 stderr 打 `[PROGRESS] 仍在运行，已 Xs`——AI 知道进程活着不是挂死；**不重置静默计时**（idle-timeout 仍按真实输出判定，心跳不防超时）。
### 变更
- 凭据 WARN 的 `$(cat` 豁免在 warn_sensitive_cmd 内实现（_READ_FROM_FILE_RE，05/02 域）
- host add 实现于 06_conn 域（_env_write_value/cmd_host_add）；.env 写入与 load_env 同路径（脚本同目录）
### 测试
- 测试体系扩至 152 断言：unit 58+47+6 + live 12+22+7（$(cat 豁免 6 / exclude 匹配 4+真机 1 / field 失败尾巴 2 / progress 心跳 1），B2 真机全绿
- host add 手动验证：副本 .env 写入（含引号密码/幂等更新/别名调用 @prod 解析正确）
### 文档
- SKILL/docs 同步见仓库文档更新（transfer.md --exclude / exec.md --progress + field 尾巴 / setup.md host add / .env.example）

## [2.1.1] - 2026-09-07

### 修复
- **`--progress` 心跳在 `--field` 模式下被静音（v2.1.0 缺陷）**：心跳走 log() 被 v1.5.19 的 `--field` 噪音静音机制吞掉——而**长任务恰恰最常用 `--field stdout`**（消费端只等字段，静默期最需要"还在跑"的信号）。修复：log() 加 force 参数，心跳 `force=True` 绕过 `_QUIET`（显式请求 = 信号不是噪音；`[WARN]` 同理本就例外）。实测：`--field stdout` + `--progress 1` 心跳行正常输出，其余进度行仍静音。
### 测试
- live_exec_field +1：`--field` 下 `--progress` 心跳可见（且无 [SSH] 进度噪音、stdout 裸值纯净）——exec_field 22→23，全量 153 断言
### 文档
- contract.md `--field` stderr 契约补例外（v2.1.1）：`[PROGRESS]`/`[WARN]` 绕过静音，其他进度行仍静音

## [2.1.2] - 2026-09-07

### 改进（使用注意实测反馈）
1. **host add 接入 `--field`**（子命令统一消费端模式）：`pyaissh host add prod root@1.2.3.4 --password x --field alias` → 裸值 `@prod`（--field target/tips 等同理）；默认 JSON 输出不变。此前 host 是唯一没接 field 机制的子命令（--field 报 bad_args）。
2. **dry-run 消费姿势文档化**：`--dry-run` 默认完整 JSON 里 file_list 是清单全集（stdout 字段为空属预期）——正确姿势 `--dry-run --field file_list` 只取清单。transfer.md 示例行补此姿势（v2.1.2 提示）。
### 待办（backlog）
- host `remove`/`list`（add 已实现；错误提示已带指引）——见 pyaissh-dev/README.md backlog
### 测试
- host --field 手动副本验证（alias/target 裸值 + 默认 JSON 不回归）；全量 153 断言不受影响（纯输出路径调整，回归已绿）
### 文档
- transfer.md dry-run field 姿势；pyaissh-dev/README backlog 节

## [2.1.3] - 2026-09-07

### 新增（backlog 兑现）
1. **`host remove NAME`**：从 .env 删除别名——连同其专属 `_PASSWORD`/`_KEY` 行（`已删除 N 行` 明示）；别名不存在报 bad_args 并提示 `host list` 查看。支持 `--field removed`。
2. **`host list`**：列出 .env 已配置别名——`entries[{name,target}]`（**只读 host 行，绝不回显密码/密钥**）；`--field entries` 只取清单。add/remove/list 三者闭环，别名管理不再需要手改 .env。
### 测试
- 副本手动验证：add×3 → list(3，无密码) → list --field entries → remove test(删 2 行) → remove 不存在(明确 bad_args) → 再 list(剩 2)；.env 行级核对干净
### 文档
- SKILL host 节 + setup.md 补 remove/list；pyaissh-dev/README backlog 标记已实现

## [2.1.4] - 2026-09-08

### 修复（P0：凭据 WARN 误报 6/20→收敛，逐条实测）
1. **-p 系列区分大小写**（(?-i:-p)）：-P 不再命中（grep -P 'a+b' 误报）
2. **ATTACH 排除 ffmpeg/ffprobe**：-pix_fmt 误报修复
3. **SPACE 排除 grep/egrep/fgrep**：grep -p foo 误报修复
4. **空引号值排除**（-p '' / password='' / DB_PASS=''）：useradd -p '' 类空值无秘密不报
5. **赋值形态排除 - 开头参数**（(?!-)）：java -D…password= -jar 误报修复（= 后空、-jar 是下个参数）
6. **echo/printf 打印段豁免**（warn 按 shell 分隔拆段）：echo 'PASSWORD=' 字符串打印无执行语义不报；&&/| 后真命令段独立保留（echo x && mysql -u r -psecret 照报）
### 修复（P1：PowerShell @别名被吞——PS 层行为，文档注记）
- SKILL host 节加注：PowerShell 下 `@名称` 需加引号（`pyaissh test "@prod"`）
### 修复（P2：测试与文档漂移）
- run_tests 描述去硬编码例数（54/19/3 等旧数字漂移根因）——例数以运行输出为准；README 同步去数字化
- 修 4 处 `\$` 无效转义（SyntaxWarning，未来 Python 会变错误）
- **host add/remove/list 自动化集**（unit_host，7 断言）：被测复制到临时副本 importlib 加载 → host 写副本 .env（此前 CHANGELOG 自述"手动验证"的缺口）；覆盖 add 写入（含 # 密码引号包裹）/幂等更新/list 条目与不回显凭据/remove 及不存在报错/非法名
### 测试
- unit 扩至 126（58+55+6+7，新增 P0 六误报 + 2 真命中边界 + host 7）
### 文档
- SKILL（PS 注）；tests README/CHANGELOG 同步

## [2.2.0] - 2026-09-13

### 新增：后台作业（--detach + log 子命令）——长任务"边跑边看"
- **`exec --detach`**：远端 `setsid+nohup` 起作业，**立即返回** `job_id/pid/status/log/rc/job_dir/next_action`；
  作业脚本落 `/tmp/pyaissh-jobs/<job_id>/`（`job.sh` = 命令原文、`run.sh` = 运行器），
  输出合并 `job.log`、退出码写 `job.rc`——**SSH 断开/宿主单次调用超时都不影响作业**
- **`log`（别名 `tail`）子命令**：`--list` 列作业；默认回传尾部 100 行（`--lines N`）；
  `--offset N` **增量读**（返回 `next_offset`，轮询不重复）；`--wait-rc SECS` 等结束拿退出码（≤600s）；
  `--cleanup` 清理远端作业目录；支持 `--field content/exit_code/next_offset/jobs`
- 状态判定以 `job.rc` 存在为准（存在=finished+exit_code；被 kill 的作业恒 running）
- 互斥与安全：与 `--sudo`/`--pty` 互斥（bad_args）；`--job-id` 严格校验防路径穿越；
  命令原文会落远端 `job.sh`（凭据 WARN 会额外提示此事）
- 新错误类型：`detach_failed`（255）/ `job_not_found`（2）/ `log_failed`（255）
### 变更：默认输出保留量 256KB → 64KB（防宿主裁掉工具结果中段）
- 根因：结果 JSON 过大时宿主裁剪工具结果中段（`[... tool result middle pruned ...]`），
  连 spill 路径都可能一起丢——使用者只看到"丢了一段"又得重跑
- 完整输出本来就落 spill 文件；**截断时新增 `next_action` 字段**直接写明"完整输出在哪个文件、
  读文件不要重跑"（不用自己拼线索）
### 新增：`exec --help` 末尾"场景 → 参数"表
- systemctl restart → `--idle-timeout 120`；apt/docker pull → `--idle-timeout 120 --max-time 900`；
  编译构建 → `--idle-timeout 300 --max-time 1200`；要边跑边看 → `--detach`；
  静默确认活着 → `--progress 30`；大输出 → 读 spill；含 `$` 特殊字符 → `--cmd-file -`
- 同时 `log --help` 给出典型用法与状态判定说明
### 测试
- unit_regression +8（作业脚本生成/单引号转义/job-id 穿越拦截/默认 64KB）；live_exec_field +10
  （detach 启动→wait-rc 拿退出码→增量读不重复→cleanup→清理后 job_not_found→--list→互斥校验）

## [2.2.1] - 2026-09-13

> 针对 v2.2.0 后台作业（--detach/log）的使用反馈修复四项 + 新增 --kill。v2.2.0 未发布，
> 本版即后台作业的首次发布形态（可只发 2.2.1）。

### 修复
- **log 载荷字段 content → stdout**（与 exec 的 stdout 命名对齐，消费端不再猜错字段）：
  旧名移除；新增 `stream:"stdout+stderr"` 声明它是 2>&1 **合并流**；`log --help` 明确列出
  载荷字段名（此前 help 只列了 omitted_bytes 之类的元数据，AI 按 stdout 解析连续拿到 null）
- **远端落盘权限从严**：作业目录与作业根目录 0700、`job.sh`(命令原文)/`job.log`/`job.rc`/
  `job.pid` 0600、`run.sh` 0700（此前只有 run.sh 是 0700，job.sh 0644、目录 0755——同机其他
  用户可读到命令正文与完整输出）；`run.sh` 首行加 `umask 077` 管住 shell 创建的文件；
  detach 结果新增 `permissions` 字段声明这套权限
- **kill 语义收敛**：新增 `job.pid` 落盘 + 存活探测（`kill -0`），状态机由二级
  （finished/running）改为**三级（finished/dead/running）**——被 kill/OOM/崩溃的作业不再
  永久 running，`--wait-rc` 以"状态不再是 running"为收敛条件（此前只能等满超时）；
  `dead` 时带 `hint` 说明"无退出码、退出码不可知"
- **尾部截断提示**：`--lines` 模式回传内容又被 `--max-output` 截中段时（`truncated:true` +
  `omitted_bytes` 但 `has_more:false`/`next_offset`=EOF，看着像读完了）→ `next_action`
  明确提示"用 `--offset 0` 顺序读补齐"

### 新增
- **`log --kill`**：读 `job.pid` 对**进程组**（setsid 后 pid==pgid）`TERM` → 宽限 5s → `KILL`；
  Linux 上先校验 `/proc/<pid>/cmdline` 确属本作业（防 pid 复用误杀，不匹配则拒绝）；
  可与 `--wait-rc` 连用（kill 后立即收敛 dead）；新错误类型 `kill_failed`（255，带 reason：
  no_pid/already_gone/pid_mismatch/kill_failed）
- **`log --list` 状态也三级**：无 rc 的条目做一次批量存活探测（单条 exec，避免 N 次往返），dead 可见

### 测试
- unit_regression +1（run.sh umask 077；job 路径表含 job.pid）
- live_exec_field +7（远端权限 0700/0600 实测、log 载荷字段 stdout 且无 content、
  `--kill` 整组停掉并收敛 dead、kill 后 wait-rc 快速收敛（waited_ms < 20s）、dead 带 hint、
  尾读截断给 `--offset 0` 提示、`--offset 0` 顺序读拿全文、`--kill` 缺 job-id 拦截）

## [2.2.2] - 2026-09-13

### 修复：`log --cleanup` 对运行中作业是脚枪（自断追踪 + 留孤儿）

- **现象**：`log --job-id X --cleanup` 在作业仍 `running` 时照删目录（结果里明明写着 status=running）：
  `job.pid`/`job.log`/`job.rc` 一并消失 → `--list` 归零 → **工具彻底失去对该作业的追踪**，
  而 `ps` 里 `run.sh`/`job.sh`/子进程仍在跑（只剩人工 pgrep/kill 一条路）
- **修法**：`--cleanup` 加**状态守卫**——只在作业已结束（`finished`/`dead`）时执行；仍 `running` 时
  拒绝并报 `job_running`（退出码 2），消息给出 pid、两条正路（`--kill` / `--wait-rc`）与
  `hint`（`--kill --cleanup` 一步到位）；新增 `--force` 作为唯一的"明知在跑也要删"出口，
  照删但留痕：结果带 `forced_cleanup:true` + `warnings` 明示"已失去追踪、远端进程可能仍在跑"。
  `--force` 只允许配合 `--cleanup`（否则 bad_args）
- **配套澄清（整组杀）**：只杀 `run.sh`（`kill -9 <pid>`）会让 `job.sh` 与它的子进程被 reparent
  成孤儿继续跑——`--kill` 走**进程组**（setsid 后 pid==pgid，`kill -TERM -<pgid>`）正是为此；
  本次补真机断言卡住"kill 后无孤儿进程"
- **测试**：live_exec_field +7（运行中 `--cleanup` 被拒且目录保留、`--cleanup --force` 强制清理且留痕、
  force 后进程确实仍在跑（代价可见）、外部 pkill 收尾、`--kill` 收敛 dead + 快速收敛 + hint、
  **`--kill` 后无孤儿进程**（`pgrep -af '[s]leep 300'` 为空）、`--force` 缺 `--cleanup` → bad_args）
- 真机验证数据（B2）：47/47 PASS

## [2.2.3] - 2026-09-13

### 改进：`--force` 之后直接给出可执行的整组杀命令（省掉一次 pgrep）

- **背景**：`--cleanup --force` 之后 `job.pid` 已随目录删除 → `--kill` 用不了，而远端进程还在跑。
  旧警告只写"要停掉请人工 pgrep/kill"——其实 `exec --detach` 返回的 `pid` 就是 `setsid` 的
  **进程组组长**（`pid==pgid`），`kill -9 -<pid>` 加个负号即可一次清整组（含子进程），不必 pgrep
- **改动**：
  - 强制清理结果新增 **`group_kill`** 字段（= `kill -9 -<pid>`，可直接执行），同一命令同时写进
    `warnings` 与 `next_action`
  - `next_action` 修掉误导：此前 force 后仍按 running 给"继续增量读：--offset N"，而 `job.log`
    已被删除——现在明确"目录已清理，无法再追踪也无法用 --kill"，并给出整组杀命令
  - `log --help` 的 `--force` 说明与 epilog 状态章节同步写入 `kill -9 -<pid>`（负号 = 进程组）
- **实测对照**（真机）：照结果里的 `group_kill` 直接执行 → `GROUP_KILLED`（一次干净，无需 pgrep）；
  对照组用**正** pid `kill -9 <pid>` → 只杀组长，`sleep` 子进程被 reparent 成孤儿（`ORPHAN_LEFT`）
  ——证明负号不可省
- **测试**：live_exec_field +4（group_kill/warning/next_action 三处一致、next_action 不再误导为
  「继续增量读」、照 group_kill 执行即一次清整组、正 pid 对照组留孤儿）；真机 51/51 全绿

## [2.2.4] - 2026-09-16

### 新增：命令文本 CRLF 行尾自动归一（--keep-crlf 可关，结果回传 crlf_normalized）

- **背景（实测先行，纠正了原来的假设）**：Windows 工具（记事本 / VS Code / PowerShell 重定向 /
  here-string）写出的命令文本行尾是 `\r\n`，远端 bash 把 `\r` 当词的一部分。但分通路实测发现：
  - `--cmd-file <文件>` 与 `--cmd-file -`（stdin）**早已被隐式归一**——靠 Python 文本模式的
    universal newlines 副作用（代码注释只写了 BOM，没写 CRLF，也无法关闭）
  - **内联 `--cmd` 完全没有归一**（argv 里的真 CR 直达 bash，`v=1\r` → 变量值混进 `\r`）——
    这是真正还在咬人的一条，也覆盖"内联 heredoc 落盘的文件每行带 CR"（此前靠 `sed -i 's/\r$//'` 收尾）
- **改动**：
  - 新增 `_normalize_cmd_newlines()`（纯函数）：CRLF/孤立 CR → LF，并返回归一处的行尾数
  - exec 命令文本三条路（`--cmd` / `--cmd-file` 文件 / stdin）**统一显式归一**：文件读取改用
    `newline=""` 不做隐式转换，stdin 改读字节再解码（不再依赖 TextIOWrapper 的隐式行为）
  - 新增 `--keep-crlf`：保留原样（例如确实要产出 CRLF 数据文件）；归一发生时结果回传
    **`crlf_normalized: <处数>`**（exec 与 exec --detach 都回传），并进 `warnings` 说明 + 给出逃生阀
  - **传输通道不动**：upload/download 是数据面，任何情况下不改字节（Windows 文件传上去仍是 CRLF）
- **测试**：unit_regression +6（纯函数：CRLF/孤立 CR/混合/纯 LF 零改动/转义 `\\r` 字面量不受影响/
  `--keep-crlf` 默认关）；live_exec_field +11（内联归一并回传计数、warnings 含逃生阀、
  `--keep-crlf` 保留 CR、cmd-file 与 stdin 行为不回退且有计数、纯 LF 文件零改动、
  heredoc 落盘默认无 CR 而 `--keep-crlf` 保留 CRLF、行尾孤立 CR、detach 计数 + job.sh 无 CR）
- 真机验证：B2 62/62 全绿

### 开发工具与测试（2026-09-21 补）
- **修正 MANIFEST 过期锚**：`01_globals.py` 的锚还停留在 `^VERSION = "1\.5\.19"`（源码早已 2.2.x）
  → 改为与版本无关的 `^VERSION = "`（唯一且不再随发版过期）
- **`build_single.py split` 安全化**：旧实现**先清空 `domains/` 再切分**，锚一过期就是
  "12 个域文件被删空 + 退出 1"；且锚是**域内标识**而非域边界（域文件真正起点是各自首行，
  单文件里的 `# ===== [域 NN/12] …` 横幅由 join 生成），按锚切分本就无法还原域文件。
  现改为：先算后写（校验完成前不碰磁盘）+ 逐块比对，不一致时**拒绝覆盖 `domains/`**，
  `--force` 只写 `domains.split/` 供人工比对
- **测试兜底**：`unit_artifacts` 6 → 11 例，新增 MANIFEST 锚"全部命中 / 唯一 / 有序 / 域文件齐全"
  ——`join/dist` 不用锚，锚失效不影响构建，所以只能靠测试发现（这正是本次漏网的原因）
- **`build_single.py split` 已彻底移除**（2026-09-21，取代上一条的"安全化"）：方向确定为单向——
  **`domains/` 是源，成品单文件是产物**（改代码改 domains/ 后 `dist`；直接改 `pyaissh.py`
  下次 dist 会被覆盖，`check` 会报不一致）。跑 `split` 现在明确回 `SPLIT_REMOVED` 并退出 1，
  不碰磁盘。MANIFEST 的**锚列一并删除**（锚的唯一消费者就是 split，留着只会再烂一次——
  这次的过期锚就是这么来的）；MANIFEST 退回纯顺序清单，`unit_artifacts` 仍校验
  "12 域 / 域序 00..11 / 域文件齐全"

## [2.3.0] - 2026-09-24

### 新增：`session` 常驻会话（真 PTY）——逐条喂命令 / 状态保留 / 可中断

- **解决的痛点**：`exec` 无状态（`cd`/`export` 不跨调用）、`exec --detach` 启动后**改不了**
  （长任务开头命令写错只能 kill 重来）。`session` 在远端起一个常驻 shell，AI **逐条喂命令**：
  每条独立退出码；**打错了就把那条命令改对再发一遍**（同一条重试，不是换下一条）：报错 → 改名/改参数 →
  重发 → 成功，状态（cwd/env/函数）全在，所以重发时上下文与上次一致；执行中的命令**可中断**
- **真 PTY**：`setsid`+`nohup`+util-linux `script` 分配真终端（`test -t 0` 为真、`tty`=/dev/pts/N），
  所以**能应答交互提示**（`read -p`、y/n、密码）——`session keys --data 'y\n'` 实测可答上；
  远端缺 `script` 时自动降级为非 PTY（状态与退出码照常，无 tty）
- **子命令**：`start` / `send` / `read`（尾部·增量·`--wait-rc` 等某条命令结束）/ `ctrl-c`
  （中断执行中的命令，`--force`=SIGKILL）/ `keys`（注入按键文本）/ `list` / `kill`
- **契约与 exec/log 对齐**：`read` 载荷字段 `stdout`（合并流）/`next_offset`/`status`(`done`|`running`)/
  `exit_code`/`token`；默认剥离 ANSI（`--keep-ansi` 保留）、清洗 CR 与 `script` 头；
  命令文本同样走 CRLF 归一（`--keep-crlf` 可关）；会话目录 0700、文件 0600
- **三个实测坑（都有测试护栏）**：
  1. **哨兵必须与命令同一行被解析**（`{ ...; }; echo 哨兵`）——早期单列一行会被命令里的
     `read -p` 当输入**吃掉**（实测 `read` 拿到的值就是哨兵文本，真哨兵永不出现）
  2. **会话身份是 starter 的进程树闭包，不是 sid**——实测 `script` 给子 shell **另起会话**
     （starter sid ≠ pty sid），按 sid 清理 ⇒ 目录删了、会话进程还活着（留孤儿）；
     `kill` 现在先算一次闭包再 TERM→(幸存者)KILL→校验，实测 `swept=3`/`remaining=0`/无 `script` 残留
  3. **`ctrl-c` 只发 SIGINT 不够**——会话树由 `setsid nohup` 起、SIGINT 处置可能被继承为忽略
     （实测 `kill -INT <sleep pid>` 返回成功但进程没死）→ 0.7s 后自动升级 SIGTERM；
     另：往 FIFO 写 `0x03` 想靠 pty 行规程转 SIGINT **实测无效**，故不依赖该路径
- **测试**：新增真机套件 `live_session`（19 例：PTY/权限/逐条退出码/状态保留/错误命令不中断会话/
  running 检测/ctrl-c 收敛与会话存活/keys 应答提示/ANSI 清洗/list/kill 进程树与无孤儿/错误路径）+
  单元 9 例（路径表/名字正则/`--data` 转义/哨兵包裹/哨兵切分/清洗）；单元集随之升到 13 域
  （新增 `11_cmd_session.py`，`12_cli_main.py` 顺延）
- **文档**：新增 `docs/session.md`（定位对比/子命令/三个实测坑/边界）；SKILL.md 增章节与触发词；
  contract.md 增会话字段契约；errors.md 增 `session_*` 错误类型

### 补充：`session run`（一步一次调用）+ SKILL 默认姿势决策表

- **`session run <target> --name S --cmd '…'`** = `send` + 等哨兵 + 回传该命令输出与 `exit_code`，
  **合成一次调用**：此前每步要 `send`+`read` 两次调用（比 `exec` 贵一倍），这是"会话式当不了多步任务
  首选"的最大摩擦；现在会话式每步与 `exec` 同成本
  - `--wait-rc N`（默认 60，上限 600）超时回 `status:"running"` 且**保留 `token`**（可
    `read --wait-rc --token` 续等或 `ctrl-c` 中断）；`--no-wait` 只发送（等价 `send`）
  - 顺带修一处缺陷：超时分支曾把 `token` 覆写成 `None`，消费者无法续等（实测发现）
- **SKILL.md 顶部新增「先选对模式（默认姿势）」决策表**：按任务类别给默认——
  `exec`（单条/无状态：stdout-stderr 分离、一次调用、零残留）/ `exec --detach`+`log`
  （单条长命令：可 `--kill` 中断、抗断线）/ `session run`（多步·需状态·可能要中断·要应答提示）/
  传输 / 探查；并写明三条"别做"：多步硬拼 `&&`、单条命令也起会话、长命令用前台 exec 等到超时
  - 依据：本会话 382 次 pyaissh 调用实测——139 次 `exec` 中仅 **14%** 带 `cd`/`export`/`source`
    （状态敏感、会话才有增益），86% 是单条无状态命令；故**不把 session 设为全局默认**，
    而是"按任务类别给默认"
- **测试**：`live_session` 19 → 25 例（run 一次调用拿退出码+输出、run 之间状态保留、
  `--wait-rc` 超时回 running 且 token 保留、run 起的命令可 ctrl-c 中断收敛、`--no-wait` 只发送、
  随后 read 取结果）；MCP 真机 21 → 23（`B17a/B17b` 经 MCP 透传 run）

### 修复：外部评审真机复现的 5 个缺陷（v2.3.0 发布前）

- **B1(P0) SFTP 看门狗误杀长等待**：`open_sftp` 的看门狗线程只看 `_pyaissh_last_activity`，
  而 session/log 的轮询循环从不刷新它——实测 `session run --cmd 'sleep 35'` 在 **30.7s** 处断链
  （"Server connection dropped"，整条结果丢失）、`read --wait-rc 40` 同理；
  **`log --wait-rc >30` 同病（v2.2.0 起潜伏**，实测 45s 作业的 `--wait-rc 50` 在 ~30s 被杀、
  随后报"读不到日志文件"）——这直接把 `--wait-rc` 上限 600 的设计架空了。
  修：session 的 `_session_sftp_read`/`_session_sftp_size`/轮询循环与 log 的等待循环，
  每轮操作前调 `_sftp_touch_activity()`；`_sftp_read_pid` 读前也刷新
- **B2(P0) `--no-pty` 降级路径完全不可用**（静默挂死式坏）：① 载荷 `{ ...; }; echo 哨兵` 是多行，
  降级模式**逐行 eval** ⇒ `{` 单独一行 syntax error、哨兵永不出现；② 降级分支把会话输出重定向到
  `err.log`（PTY 分支写 `out.log`）⇒ `out.log` 永远为空、`read`/`run` 永远回 running+空输出。
  修：降级模式改用**单行 base64 + eval** 载荷（`eval "$(printf %s '<b64>' | base64 -d)"; echo 哨兵`
  ——一行内解析，状态保留、多行/长命令不受限），并把降级分支输出改回 `out.log`；
  `_session_pty_mode()` 读 start 时写的 meta 决定用哪种形态
- **B3(P1) `read --lines N` 从未被消费**：argparse 注册了、互斥校验也有，但三个读取分支都没用它——
  实测 3000 行日志 `--lines 5` 回传 **3000 行**（既违约又烧 token）。修：尾部读应用
  `--lines`（默认 100 行），`--wait-rc` 模式同样生效，并回传 `lines_requested`/`lines_returned`
- **B4(P1) 哨兵轮询窗口语义错**：从 offset 起读、上限 1MB ⇒ 命令输出 >1MB 时哨兵落在窗外，
  明明跑完了也永远回 running（实测 `seq 1 300000` 卡在 ~1MB、只看到 144960 行）。
  修：每轮只 `stat`，从 `max(offset, size-1MB)` 读**末尾窗口**并只追**新增段**（顺带消除
  每 0.25s 重下整个 1MB 窗口的自残）
- **B5(P2) session run/send 不回传 `crlf_normalized`**：归一执行了但字段缺失、与 exec 契约不一致。
  修：两者都回传
- **顺带加固（自测发现）**：判 `dead` 前给 `job.rc` 落盘留 `JOB_RC_GRACE`(0.6s) 宽限——
  实测 `sleep 45` 作业**刚结束的那一瞬间**会被读成 `dead`（进程已退出、rc 还没落盘）
- **测试**：`live_session` 28 → 36（B1 长等待/B2 --no-pty 就绪+run+状态/B3 --lines/B4 >1MB 哨兵/
  B5 crlf_normalized×2），`live_exec_field` 62 → 64（B1 log --wait-rc 50 等满 32s + 结束不误判 dead）；
  评审指出"`--no-pty` 无用例"的缺口已补
- 排障记录：首次跑新用例时 `exec --detach` 偶发未返回 job_id（未复现，isolated 3/3 正常）——
  已把原始 JSON 打进失败详情，便于下次定位

### 新增：`PYAISSH_SFTP_IO_TIMEOUT`（看门狗窗口逃生阀，v2.3.0）

- **背景**：SFTP 看门狗守护的是"**对端是否还活着**"（判据：多久**没有一次成功的 SFTP 往返**），
  不是"操作时长"。B1 的正确修法是把轮询循环的往返**记账**（`_sftp_touch_activity`），
  而不是绕过看门狗（那会让静默死链在长等待里无限阻塞）或盲目调大（探测延迟被一起放大）。
- **但有一类窄场景确实需要调大**：极慢链路（≲33KB/s）上**单次**大读可能超过 30 秒——
  例如会话首轮读 1MB 尾窗。故新增环境变量（**默认不变，仍是 30 秒**）：
  `PYAISSH_SFTP_IO_TIMEOUT=90`（接受小数；非法值/非正值只打 WARN 并回落默认）。
  优先级：**显式 `open_sftp(io_timeout=…)` > 环境变量 > 默认 30 秒**（分片下载线程仍用显式值）。
- **测试（只跑相关套件）**：`--unit` +4（未设/生效/小数/非法值回落）；`--transfer` +2
  （设 90 后 `ls` 正常；非法值只回落不影响功能）

### 文档：SKILL.md 瘦身（v2.3.0）

- **背景**：session 模式文档一次性塞进 SKILL.md 后体积涨到 **22,543 B**（v2.3.0 前 18,917 B）——
  SKILL.md 正文是**每次进上下文都要付 token** 的部分，涨 19% 不能接受。
- **原则（用户定）**：**session 按"完整保留"处理**（它是**第二工作模式**，正文要有可用的最小闭环），
  精简只动**冗余与已能在 `docs/` 查到**的细节：
  正文只留"该怎么用 + 什么情况用 + 坑在哪"，长解释一律下移到 `docs/*.md`（按需读取）。
- **结果**：**22,543 → 19,821 B（-2,722 B，-12.1%）**；相比 v2.3.0 前基线只 +904 B，
  而这段时间**净增**了「先选对模式」决策表（1,592 B）+ session 段（1,460 B）——
  即旧内容实际比基线更瘦。frontmatter `description` 417 字符（仍 < 模型目录 500 字符截断阈值）。
- **保留**：决策表（4 种任务形态 → 子命令）、session 段（`start/run/ctrl-c/kill` 四行示例 +
  每条独立退出码/"打错了改对再发一遍"/`run --wait-rc` → running+token/子命令清单）、
  速查 10 条、退出码与错误类型摘要、已知边界的高频坑。
- **精简**：`exec --detach + log` 2,213→1,515 B、CRLF 段 1,042→754 B、`--field`/`retryable`
  两条 1,138→826 B、`### host` 907→836 B、快速开始/错误类型清单去重括注
  （凭据实践统一指向「安全规则」节）、删掉 session 段与正文重复的收尾两行。
- **顺手修**：`## 文档导航` 表**缺 `docs/session.md` 行**（v2.3.0 新增文档没登记）——已补。
- **同步**：根 `CHANGELOG.md` = `skills/pyaissh/CHANGELOG.md` = `pyaissh-mcp/CLI_CHANGELOG.md`（同一内容）；
  `pyaissh-mcp/SKILL.md` 经 `sync_check.py --update` 同步（13 个镜像文件全部一致）。

### 文档：SKILL.md 瘦身补正（v2.3.0）

- **问题（用户质疑后复查）**：`--field` 条精简时删掉了一句**"什么时候该用它"**——
  原句"只要某个字段裸值时用它代替手写 `json.loads`"，剩下的"消费端免样板"标题偏抽象，
  严格读下来 AI 可能只知道有这个参数、不知道**该在什么场景下换成它**。已回补为
  "**要某字段的裸值时用它代替手写 `json.loads`**"（v1.5.16 的原始动机）。
- **同批复查结论（`retryable` 条与其余各条无功能信息丢失）**：`retryable` 的 true/false 语义、
  "超时类 true 只表示值得一试"、"先看 `remote_may_be_running`"（超时类恒有 + 副作用命令先 pgrep）、
  `bad_args` 退出码 2、`docs/errors.md` 指针**全部保留**；本次只补回 `bool` 类型标注。
  其余被删的都是可推断或已在别处出现的表述：`--help` 是否 JSON（速查第 1 条已写"纯文本除外"）、
  "不用 `--field` 时契约零变化"（默认姿势即如此，无决策价值）、"三次实测"这类过程修饰。
- **纪律**：SKILL.md 是模型**每次都要读**的正文，但**"省 token"不能省掉"什么场景用哪个参数"**——
  正文的取舍线是"**怎么做 + 什么时候用 + 坑在哪**"留，**为什么/历史/实测过程**下移 `docs/`。

### 文档：SKILL.md 瘦身审计与回补（v2.3.0）

- **起因**：用户追问"其他有没有也砍过头的"——把 `97022b2`（瘦身前）与当前逐行比对，
  17 个差异块逐个判定，结论是**有 9 处属于"把怎么做/什么时候用/怎么判"也砍掉了**，已全部回补：
  1. 速查 7 丢了**不传参时的默认行为**（下载 ≥8MB 自动 4 连接分片）——读者会以为必须手动加 `--parallel`；
  2. detach 丢了前台**时长上限的实际数值**（pyaissh `--max-time` 可到 1200）；
  3. detach 的 `dead` 丢了成因枚举（被 kill/OOM/崩溃）——影响"这是崩溃还是我杀的对不对"的判断；
  4. `--cleanup` 被拒时的 **error 名 `job_running`** 与**放弃追踪的写法 `--cleanup --force`**（原文只剩孤立的 `--force`，指代不明）；
  5. CRLF 段丢了调用环境限定（**PowerShell** 重定向）与"**不必再 `sed -i 's/\r$//'`**"（少了这句 AI 会多做一步无用功）；
  6. session `start` 丢了回传字段 `ready`/`pty`（判断有没有真 PTY 要看它）；
  7. session **`ctrl-c` 的信号升级与 `--force`=SIGKILL** 丢失（只剩裸命令名）；
  8. session **`keys --data 'y\n'` / `read --offset 0` 两条示例**被删——`keys` 的用法在正文里只剩子命令清单里的一个词，
     而"能应答交互提示"恰是 session 区别于 `exec` 的核心能力之一；
  9. session `read` 只留了 `stdout`，丢了 `status`(`done`|`running`)/`exit_code`/`next_offset`——轮询逻辑要靠这些字段。
- **判定为"该砍"、不再回补的**（属冗余/历史/实现细节，`docs/` 有或可推断）：`.part` 的 posix-rename 退化细节、
  `--text` 的 nonce 标记、"三次实测教训"这类过程修饰、"不用 `--field` 时契约零变化"、`--help` 是否 JSON
  （速查第 1 条已写）、`--sudo -p ''` 的具体参数拼法、"三个实测坑"的标题式枚举（保留 `docs/session.md` 指针）。
- **结果**：19,890 → **20,462 B**（9 处回补 +572 B）。相比 v2.3.0 前基线 18,917 B 增 1,545 B，
  而净增内容是「先选对模式」决策表（1,592 B）+ session 段（约 1,800 B，**含 6 条示例**）——
  即**旧内容仍比基线瘦**，新内容按"第二工作模式"完整保留。
- **教训（写进取舍线）**：正文可以砍"为什么/历史/实测过程"，**不能砍"默认行为、字段名、错误名、可选写法"**——
  这些是 AI 做决策和写重试逻辑的输入，删掉后它只能去猜或白跑一次。

### 加固：会话残留治理（v2.3.0，R1）

- **起因（用户提问）**："session 能退出干净吗？AI 用了不管它，会自己退出吗？" —— 结论：
  **不会自退，用完必须 `kill`**。会话是 `setsid nohup` 起的远端常驻进程（"SSH 断开照跑"正是设计目的），
  代码里**没有任何 idle 超时/TTL/自动回收**（`SESSION_WAIT_MAX` 等只是客户端等待上限）；
  本地侧则永远干净——每次 `pyaissh …` 都是短命客户端，不会阻塞继续用 `exec`。
  结束只有三条路：`session kill`（默认连目录删）/ 会话里的 shell 自己 `exit`（进程没了但
  `/tmp` 目录与 `out.log` 仍在）/ 远端重启。
- **读码发现两个真问题**：
  1. **`kill` 会无声误报"清干净"**：清理集合原先是"从 `sess.pid` 的 starter 做进程树闭包"。
     starter 若被 OOM/外力杀掉，`script` 与 `bash -i` 会被 reparent 到 1 号进程 ⇒ 闭包为空 ⇒
     返回 `swept=0, remaining=0, cleaned=true`，**而会话仍在跑**。
  2. **文案过时**：`kill --help`、docstring、warning 仍写"按 **sid** 全量清理"——实现早在 B 系列
     修复时就改成进程树闭包（代码注释自己写着"为什么不用 sid：script 的子 shell 自己 setsid"）。
- **改法**：
  - 根候选扩为三个：`sess.pid`(starter) / `bash.pid`(会话 shell) / 会话 shell 的父进程(`script`)，
    闭包取并集去重（starter 已死的场景由 `script` 兜底——它的 argv 里带 out.log 路径，
    闭包覆盖 script + bash -i + 正在跑的命令）。
  - 每个根必须**自证**：`ps -o args=` 里含本会话目录，不自证就不作为根 ⇒ **堵住 pid 回收误杀
    无关进程树**（旧实现同样有这隐患，只是没触发过）。
  - 三个根都不可用时（`ROOTS=0`）**不猜也不杀**：回 `roots: 0` + `verified: false` + warning
    （附 `ps -eo pid,ppid,tty,args | grep -E 'script -qfc|pyaissh-sessions'` 自查命令），
    不再宣称清理完成；结果新增 `remaining_total`/`verified_total` 汇总。
  - `session list` 增加 `started_at`/`age_seconds`（meta 第三个字段本来就写了启动时间，此前被忽略），
    并对"挂了超过 24 小时仍活着"的会话给一条提醒（`out.log` 只增不减，别白占远端磁盘）。
  - 修掉 3 处"按 sid"死文案（CLI `--help` + 子命令 description、`cmd_session_kill` docstring、warning）。
- **文档**：SKILL.md 会话段加"**用完必须 `kill`**"一条；`docs/session.md` 新增
  「会话会残留吗？（生命周期与清理）」——3 进程 + 目录清单、结束的三条路、`verified` 语义、
  `kill --all`/`--keep-dir`、以及"本地侧为何永远干净"。
- **验证（离线四项，已过）**：① 新 kill 命令 `bash -n` 语法；② awk 闭包在合成进程表上的行为
  （正常会话 root=100→{100,200,300}、**孤儿场景** root=200→{200,300}、陈旧 pid→空集不误杀）；
  ③ 两根交集去重后 SWEPT 计数正确；④ 自证闸门（argv 不带会话目录 → reject）。
  `--unit` +5（kill 命令结构/自证闸门/ROOTS-HAD 回传/awk 用循环变量/24h 常量），
  `live_session` +8 项（孤儿兜底、无根不误报、闲置不自退、list 年龄字段、kill roots+verified）。
- **真机验证（测试机 A，`--session` 套件）**：**43 PASS / 0 FAIL**，新增 8 项全绿——
  `list` 的 `started_at`/`age_seconds` 正常；`kill` 报 `roots>=1` + `verified=true` 且事后
  `pgrep -x script` 为 0；**孤儿场景**（`kill -9` 掉 starter 后 script/bash -i 被 reparent）
  仍 `swept>=2`、`remaining=0`、`verified=true`、无残留进程；**无根场景**（两个根都杀 + 删 pid 文件）
  回 `roots:0` + `verified:false` + warning（未获确认）而不再谎报；**闲置 35 秒**后会话仍能跑命令
  （不自退、无 idle 超时）。

### 补充：会话"本地关机 / 半截写入"实测 + R2 加固（v2.3.0）

- **用户追问**："任务做完本地直接关机，服务器上进程还会留着吗？" —— **会**（这正是 `setsid+nohup`
  的设计目的），并做了两个针对性真机实验（测试机 A）：
  - **无客户端 26 秒**（等价本地关机，期间一条连接都没有）：`list` 仍 `running`；连回来后
    `read --wait-rc` 直接拿到 `status:done`、`exit_code:0`，输出含 `AFTER_RECONNECT_OK` 与 `pwd=/etc`
    —— **命令跑完了、退出码与输出在、`cd` 状态也在**。"本地关机"不是问题，**会话不会自己收尾**才是。
  - **卡住场景**：`kill` 后收尾核查 `ls -d /tmp/pyaissh-sessions/*` 为空、`pgrep -x script` 为 0；
    注意 `pgrep -f pyaissh-sessions` 会**匹配到自查命令自己**（命令行里含该字符串）——判残留要用
    `ps -eo pid,ppid,tty,args | grep '[p]yaissh-sessions'` 这类不自匹配的写法。
- **R2：本地在"写命令半途"断线 → 会话被卡死（真机复现）**：pyaissh 把整条命令写进 FIFO，
  本地若在写入中途关机/断网，远端 tty 输入缓冲会留下**没有换行的半行**；下一轮写入与它**串成一行**
  ⇒ 实测 `bash: syntax error near unexpected token '}'`，且**哨兵永不出现**（`run` 只回 running、
  拿不到 `exit_code`，AI 被卡住，只能人工 `ctrl-c`）。
  修：**载荷以换行开头**（PTY 与非 PTY 两种形态都加）——先把那半行终结掉（它会被当**一条独立命令**
  执行，可能是半截命令、报错留在 `out.log`，值得扫一眼），随后真正的命令在干净输入行里解析。
  实测同一场景：修前 `exit_code=None` + syntax error；修后 `exit_code=0`、输出 `PARTIAL_HALF\nSECOND_OK`。
- **文档**：`docs/session.md` 新增「本地关机 / 断网会怎样（实测）」与「半截写入（R2）」两节
  （含 `ctrl-c` 恢复步骤）；SKILL 会话段补"本地关机/断网不影响它和正在跑的命令"。
- **验证**：`--unit` +1（载荷以换行开头，93 PASS）；真机 `--session` **44 PASS / 0 FAIL**
  （新增「R2 半行残留后下一条命令仍拿得到退出码」）；另跑独立探针脚本覆盖 26s 无客户端、
  半行前后对比、收尾零残留三件事。

### 新增：MCP 会话归属与退出清理（pyaissh-mcp v0.3.1）

- **起因（用户设计质疑）**："session 是给交互任务用的，本地关了任务就结束了——服务端进程是不是该跟着关？
  如果是后台任务，用之前的模式不就行了？" 结论分两层：
  - **架构事实**：CLI 是"每次调用一条新连接"，两次调用之间本地**没有任何进程**存在，所以会话只能
    `setsid+nohup` 脱离连接——否则 `session start` 一返回它就死了。"随本地进程关闭"在 CLI 路径
    能落地的形式只有空闲 TTL/租约（本次未做，保持显式 `kill`）。
  - **MCP 路径则真有"本地长命进程"**：`pyaissh-mcp` 就是。于是把"会话归属"落在它身上——
    **MCP 正常退出时清掉它自己启动过的会话**，正好对应"本地这轮工作结束了"。
- **实现（`pyaissh-mcp/pyaissh_mcp.py`）**：
  - 归属表 `_OWNED`（target → 会话名集合 + 最小认证参数，仅内存）；`pyaissh_session` 的 `start`
    成功才登记，`kill`（含 `all=true`）同步注销；`start` 回 `session_exists` 时**不登记**。
  - `cleanup_owned_sessions()` 在 `main()` 的 `finally` 里、**关连接池之前**执行（清理要借池里的连接）：
    逐会话发 `session kill --name X` 并透传原认证参数；best-effort，失败只记 stderr 日志，绝不拖住退出；
    总时长预算 `PYAISSH_MCP_EXIT_CLEANUP_TIMEOUT`（默认 10s，`<=0` 关闭）。
  - 覆盖范围诚实标注：`SIGKILL`/断电/宿主崩溃不执行 `finally` ⇒ 残留仍在，靠 `session list`
    （`age_seconds`）与 `session kill all=true` 兜底。
  - `SERVER_VERSION` 0.3.0 → 0.3.1；`pyaissh_session` 工具描述与 README 补该行为。
- **安全边界（重点）**：只清**本进程自己起的**会话——别的 agent、别的 MCP 实例、用 CLI 直接起的
  会话**一律不碰**（宁可留下也不误杀）。
- **验证**：
  - 离线（`test/test_offline.py` 41 → **49 PASS**）：T19a~T19h 覆盖登记去重 / 只留认证参数 /
    按名注销 / 清理 argv 与认证透传 / 清理后清表 / 失败记 failed 不抛 / `<=0` 关闭 / 空表 no-op。
  - 真机（新增 `test/test_live_session.py`，**12 PASS / 0 FAIL**）：MCP 起会话 → 关闭 MCP（等价本地
    agent 会话结束）→ 该会话与目录都被清掉；**同时用 CLI 直接起的会话不受影响**（S2 安全断言）；
    显式 `kill` 过的会话退出时不重复清（S1d）；收尾零残留目录、零 `script -qfc` 进程（S4b）。
  - 文档同步：`docs/session.md` 增「MCP 通道的例外」一节；`pyaissh-mcp/README.md` 增
    「会话归属与退出清理」一节。

### 新增：会话空闲回收（idle TTL）+ `start --attach`（v2.3.0）

- **用户设计（照做）**："服务器的进程 10 分钟后如果提示符空闲、没有命令在跑就回收；这 10 分钟内又发起连接
  就接着用之前的会话；旧会话关了才创建新的。" 除 TTL 数值外全部采纳，10 分钟按用户要求作默认值；
  另两条安全细节按讨论结论实现：**`start` 撞名仍报 `session_exists`（不静默接上别人的同名会话）**，
  另给 `--attach` 做显式幂等；**回收不留痕**。
- **实现（`11_cmd_session.py`）**：
  - `start` 时在**会话目录里放一个独立看门狗进程**（`watch.sh`，pid 记 `watch.pid`，独立 `setsid` ——
    不能挂在会话树里，否则 `start` 返回时随连接 SIGHUP 死；独立也让它清理时不会先杀掉自己）。
  - 回收判据（**两个条件同时成立**）：① `pgrep -P <会话 shell>` 为空（提示符空闲 ⇒ 构建/安装不被误杀）；
    ② `beat` 文件 mtime（每次 pyaissh 交互刷新）距今超过 TTL。检查周期 15 秒。
  - `--ttl 600`（默认）／`30s`／`10m`／`2h`／`0`（关闭）；环境变量 `PYAISSH_SESSION_TTL`；
    `list` 回 `ttl_seconds`/`idle_seconds`/`expires_in_seconds`（剩余 <2 分钟额外提醒）。
  - 续期 = `start`（含 `--attach`）/`send`/`run`/`read`/`ctrl-c`/`keys` 各刷一次 `beat`；**`list` 不算**。
  - 回收动作与 `kill` 同款（**自证进程树闭包** + 先删目录再 TERM→KILL），看门狗随后自退；
    目录消失即退出，**不留常驻循环**。
  - **`session start --attach`**：活着 → `attached: true` + `pid`/`age_seconds`/`idle_seconds`（状态保留）；
    不存在/被回收 → 正常新建 `attached: false`。不带 `--attach` 时 `session_exists` 的提示改为
    "**直接继续用它**（run/send/read），要重开先 kill"。
  - `session kill` 的根候选加 `watch.pid`（否则 kill 后看门狗要多活一个周期）+ 收尾删 `watch.pid`。
  - 会话缺失的错误提示改为"从没起过或**已被空闲回收**（默认 600s，`--ttl 0` 可关）→ `start` 重建
    （要接上活着的旧会话加 `--attach`）"。
- **踩到并修掉的两个真机 bug（都是新代码引入、真机套件抓到的）**：
  1. **看门狗启动组继承了 SSH 通道的 stderr** ⇒ 通道永不 EOF ⇒ `recv_exit_status()` 干等到超时，
     `session start` 20 秒后报 `session_failed`（消息为空）**而会话其实已经起好了**。修：用
     `{ ...; } >/dev/null 2>&1 </dev/null &` 把整组后台任务包住（只给 `setsid` 那条加重定向不够）。
  2. **`kill` 之后看门狗要多活一个检查周期（≤15s）** ⇒ "kill 后无残留进程"断言失败。修：`start` 记录
     `watch.pid`，`kill` 把它作为第三个自证根一起扫。
- **测试**：`--unit` 102 PASS（+10：TTL 解析 5 例、看门狗脚本关键片段、TTL=0 不装看门狗、meta 4 字段、
  beat 初始化、路径表含 beat/watch、kill 根候选含 watch.pid）；真机 `--session` **52 PASS / 0 FAIL**
  （+8：T1a~c 空闲 32s 自动回收且目录进程都没了、T2a~b 命令在跑跨过看门狗检查仍 running + 长命令跑完
  拿到输出、T3a~c --attach 接上/提示/新建）；MCP 离线 49 PASS、MCP 真机会话 12 PASS 不受影响。
- **离线校验（可复用）**：TTL 解析表、看门狗脚本 `bash -n`、关键片段断言、TTL=0 无看门狗、
  判据三态（刚交互不回收 / 命令在跑续期 / beat 陈旧回收 / beat 缺失只续期）——Git Bash 无 `pgrep`，
  本地用桩验分支逻辑，真 `pgrep -P` 行为由真机套件覆盖。
- **文档**：`docs/session.md` 生命周期一节改写（进程清单加看门狗、空闲回收规则、`--attach`、
  "本地关机"结论更新、保留"手工 rm -rf 目录后孤儿看不见"的已知边角）；SKILL 会话段与示例同步；
  MCP `pyaissh_session` 描述 + README 增 TTL/attach 说明。

### 新增：`session kill` 按 argv 扫孤儿（v2.3.0）

- **起因**：真机实验里发现（并亲手制造过）一个残留盲区——**手工 `rm -rf` 掉会话目录**后，进程失去
  pid 记录，而 `session kill` 是**按目录枚举**的 ⇒ 那些进程永远看不见（真实发生过：一个 starter +
  `script` + `bash -i` 留在机器上无人认领）。用户拍板：给 `kill --all` 加"按 argv 扫孤儿"。
- **实现**：
  - `kill --all`（以及 `kill --name X`）在正常清理之后跑一次 `ps -eo pid=,args=`，用**纯函数**
    `_session_orphan_candidates()` 挑出候选；再对每个候选执行"该 pid 的**进程树闭包**"清理
    （`_session_pid_kill_cmd()`，TERM → 宽限 → KILL → 校验），结果放进 `orphans[]`
    （`via: "argv-scan"` / `kind` / `swept` / `remaining` / `verified`），并汇总
    `orphans_total` / `orphan_remaining_total`。
  - **自证身份**（避免误杀"只是提到路径"的进程）：同时满足三条才算孤儿——① argv 里出现
    `<会话根>/<名字>/` 且名字合法（防路径穿越）；② argv 含 `script -qfc`／`<根>/<名字>/in`
    （starter 的 FIFO）／`<根>/<名字>/watch.sh`（看门狗）之一；③ 该名字的目录**不在**
    （目录还在就交给正常 kill 路径，不重复处理）。
  - **仍然找不到的**（文档写明）：starter 与 `script` 都死、只剩 `bash -i` 时，它的 argv 只有
    `bash -i`（无任何路径）⇒ 无法自证，工具不碰它（宁可留下也不误杀）。
- **测试**：
  - `--unit` 102 → **108 PASS**：合成 `ps` 输出覆盖 starter / pty 包装 / 看门狗三种类型 +
    `tail -f .../out.log`、argv 带路径的旁观进程（**断言不杀**）+ 目录还在的会话（不在扫描范围）+
    路径穿越名字（拒）；孤儿清理命令含闭包 awk 与 SWEPT/LEFT 回传。
  - 真机 `--session` 52 → **56 PASS / 0 FAIL**：O1a 扫到孤儿（`orphans_total>=1`）、O1b 无残留且
    `verified`、O1c 事后无 `script` 包装与 starter、**O1d 诱饵旁观进程仍活着**（不被误杀）。
- **顺带修的自身失误**：加函数时误删了 `_session_clean_text()` 的 `def` 行（函数体被并进上一个函数，
  语法合法、运行必炸）——被"重建 + 立即调用一次"的检查抓到，已修复并复验。

### 修正：看门狗只占 1 个进程（启动方式）+ 测试 runner 崩溃不再静默中断（v2.3.0）

- **看门狗启动方式（实测发现问题）**：上一版为了修"启动组挂住 SSH 通道导致 `session start`
  20 秒超时"，把整组后台任务写成 `{ printf …; chmod …; setsid …; } >/dev/null 2>&1 </dev/null &`。
  超时是修好了，但 `&&`/`;` 链被 `&` **整体后台化**会多留一个 **wrapper 进程**：它的 argv 继承 start
  命令原文、以 init 为父、还要等看门狗退出才结束；`watch.pid` 记的也是这个 wrapper 而不是看门狗
  —— 是 `ps --ppid` 才看见真正的看门狗（实测 `watch.pid` 的 argv 是整条 start 命令）。
  改：**前台写好脚本（管道 stderr 丢 /dev/null）→ 用 `;` 分隔 → `&` 只作用于 `setsid nohup bash watch.sh`
  那一条**（三个流都重定向）。两段之间写 `&&` 会退回"wrapper + 挂住通道"（这个坑也踩了一次）。
  实测结果：`start` 2.4s 返回；`watch.pid` 的真身就是 `bash …/watch.sh`（ppid=1）；
  `kill` 后 `swept=5`（starter/script/bash -i/看门狗/sleep）、`remaining=0`、`verified=true`、零残留。
  每个会话因此只多 **1 个** 进程（看门狗 bash，另有一个 `sleep 15` 子进程，RSS ≈ 3 MB）。
- **测试 runner 崩溃处理**：套件中途抛异常（本次就发生过一次：新断言引用了还没赋值的 `_sc6`）会让
  整个 runner 带 traceback 退出 ⇒ **后面的套件根本没跑**，而 PowerShell 管道里"最后一个命令成功"
  又让外层看起来是 exit 0——我因此先把一次失败误读成"unit 通过"。
  改：`_run_suite()` 接住异常 → 打印 traceback、**记 FAIL、继续跑其余套件**、整体退出码非 0。
  验证：把 unit 第一个套件替换成"必崩 + 后面套件打标记"，`rc=1` 且 `calls=['boom','after']`（后面的
  确实跑了）；正常路径 `--unit` 109 PASS 不变。
- **测试**：`--unit` 108 → **109 PASS**（新增"看门狗启动：前台写脚本 + 单条后台启动"断言）；
  真机 `--session` **56 PASS / 0 FAIL** 在最终启动方式下复跑通过。

### 文档：空闲回收的计时口径（实测钉死，v2.3.0）

- **用户提问**："接上之前的会话算续期吧？我用了一会、过五分钟不动、又过五分钟回来干活、干完退出——
  回收是按之前那次退出算，还是按新的这次重新算 10 分钟？"
- **实测结论（绝对时间戳逐个核对 beat 文件是否前进）**：**按"最后一次续期交互"重新计时**，
  是滑动窗口，不是从会话创建算起。逐命令核对结果：`read` / `run` / `send` / `keys` / `ctrl-c` /
  `start --attach` **都续期**；`list` **不续期**（连续两次 `list`，beat 一动不动）。
- **回收窗口**：看门狗每 15 秒查一次 ⇒ 实际回收落在"最后一次续期 + TTL"到"+15 秒"之间。
  实测 TTL=20：idle 走到 19s/23s/27s 仍 `running`，下一次 tick 后目录消失（≈ idle 28s）。
- **`expires_in_seconds = 0` 的含义**：只是"下一个检查点就会被收"，不是立刻消失
  （实测 `idle=22s / ttl=20s` 时仍是 `running`）；`list` 会在剩余 ≤2 分钟时给一条提醒。
- **把用户的时间线写进文档**：`t0 start → t1 run → （停 5 分钟）→ t2 再交互 → （停 5 分钟）→
  t3 干完退出`，到期时间依次是 `t0+TTL → t1+TTL → t2+TTL → t3+TTL`；超过 TTL 才回来则已被回收
  （`session_not_found`，`--attach` 会新建且状态丢失）。
- 另：顺带用 45 秒采样确认**看门狗在"提示符空闲"时不会自己刷新 beat**（TTL=60 期间 beat 45 秒未动、
  也无子进程）——即"空闲计时"确实只由 pyaissh 的交互驱动，不是看门狗自己在续命。

### 文档：空闲回收看门狗的开销实测（v2.3.0）

- **用户提问**："每 15 秒查一次，会不会对服务器有性能影响？内存占多大？"
- **实测（测试机：2 核 / 0.9 GB 内存，120 秒 = 8 个检查点）**：
  - 常驻内存：`bash watch.sh` **RSS 3284→3300 KB**（≈3.2 MB）+ 它的 `sleep 15` 子进程 ≈1.9 MB
    ⇒ **≈5 MB/会话**；整个空闲会话（starter 3.3 + script 2.1 + bash -i ≈3 + 看门狗 5）≈13 MB。
  - CPU：累计 `utime+stime` 从 0 → **3 个 jiffy**（30 ms）⇒ **每个检查点 ≈3.75 ms**，
    即 **≈0.025% 单核**（一天 ≈21 秒 CPU）。负载观察期间 loadavg 稳定在 0.0x。
  - 每个检查点只做 4 次短命 fork（`cat` / `pgrep -P` / `date` / `stat`）+ ≈8 次上下文切换，
    **不做全表 `ps` 扫描**（`ps -eo pid=,ppid=` + awk 闭包只在"真的回收"那一刻跑一次：15 ms / 22 ms）。
  - 结论：15 秒一次的开销可忽略；按规模算 50 个闲置会话 ≈250 MB（看门狗部分）+ 每 15 秒 190 ms CPU
    —— 内存吃紧的小机器上**限制项是内存不是 CPU**；不需要回收就 `--ttl 0`（连进程带内存一起没有）。
- 该结论已写进 `docs/session.md`（「空闲回收」小节末尾的"开销实测"表）。

### 优化：空闲回收看门狗改单进程（设计 C，v2.3.0）

- **用户提问引出的改动**："护栏会不会又多一个进程？能不能放弃旧写法、全部用 bash 内建，这样复杂度不叠加？"
  结论：护栏**不 fork、不加进程**（只用 bash 内建 `$SECONDS` 做整数比较）；而且可以顺手把外部命令换成
  内建，于是**只有一套实现**（不是"sleep 版 + read 版"两条路）。
- **改法（原 `sleep 15` → 单进程）**：
  - 睡眠：`read -t "$TICK" -r -u 9` 读**自持读写**的 `wd.fifo`（写端握在自己手里 ⇒ 永不 EOF，
    实测每轮精确 15.00s、rc=142、0 jiffy CPU）——省掉 `sleep` 那 **1.9 MB + 1 个进程**。
  - 读文件：`B=$(<"$BPID")` / `LAST=$(<"$BEAT")`（bash 内建，不 fork `cat`）；
    时间：`NOW=${EPOCHSECONDS:-$(date +%s)}`（bash≥5 不 fork `date`，老 bash 自动回落）；
    于是每检查点的 fork 从 **4 次降到 1 次**（只剩 `pgrep -P`），并去掉 `stat -c` 这处 GNU 依赖。
  - `beat` 从"空文件 + mtime"改成**内容为 epoch 秒**（客户端 `_session_touch` 与 `start` 都改写；
    `list` 的 `idle_seconds` 仍按 mtime，两边都成立）。
  - **防 spin 护栏**：每轮用内建 `$SECONDS` 量耗时，连续 3 次"`read -t` 立刻返回"就写一行
    `wd.log` 并退回外部 `sleep`。数据支撑：同一故障下**无护栏 5 秒烧掉 613 jiffy ≈ 6.1 秒 CPU
    （跑满一个核）**；有护栏 3 次内切回、兜底期间 35 秒只涨 ≤3 jiffy。
  - 循环首行保留 `[ -d "$D" ] || exit 0`：**目录消失必须退出而不是继续循环**——这条是我写探针时
    踩出来的（把条件写成 `[ ! -f stop ]`，目录删掉后条件恒真 ⇒ 进程永不退出，留了 3 个残渣）。
- **测试**：`--unit` **ALL PASS**（+7：read -t/自持 fd、护栏三件套、目录消失即退、内建读+EPOCHSECONDS
  且无 `cat`/`stat -c`、beat 非法只续期、无 `%%` 残留、start 的 beat 初始化写 epoch、`_session_touch`
  发出的命令就是写 epoch）；真机 `--session` 56 → **62 PASS / 0 FAIL**，新增：
  - W1：只有 **1 个** watch 进程、**无 sleep 子进程**、RSS < 4 MB、32 秒（2 检查点）CPU ≤2 jiffy、
    `beat` 是 epoch 秒且与远端 now 相差 < TTL；
  - W2：把**真实生成的看门狗脚本**的 fd 换成 `/dev/null`（复现"read 立刻返回"）→ `wd.log` 出现护栏
    记录，兜底期间 CPU ≤3 jiffy（不忙循环）。
- **文档**：`docs/session.md` 更新进程/文件清单（新增 `wd.fifo`、护栏触发才有的 `wd.log`）、
  开销实测表（1 进程 3.2 MB、每检查点 1 次 fork、整会话 ≈12 MB）与新旧对比，并补一节
  **支持的系统**（bash/util-linux/coreutils/procps-ng/awk + 已核实的各发行版 bash 版本；
  Alpine/BusyBox 与 macOS/BSD 明确不在范围内）。

### 文档：删掉刚加的「支持的系统」小节（v2.3.0）

- 用户意见："主流的都支持，不用写支持矩阵"。已从 `docs/session.md` 删除该小节——
  它本来也和同文档「边界与注意」里既有的**依赖**一条重复（`bash` + `mkfifo` 必需、
  真 PTY 需要 util-linux `script`、缺失自动降级为非 PTY）。
- 版本核实结论仍留在本轮 CHANGELOG 里备查（Ubuntu 20.04/22.04/24.04 = bash 5.0/5.1/5.2，
  Debian 13 = 5.2.37 真机实测，Mint 22.3 = 5.2.21，Fedora 43/44 = 5.3/5.3.9，Arch = 5.3.20，
  openSUSE Tumbleweed 5.3.15 / Leap 15.6 4.4），但**不再写进对外技能文档**以免啰嗦。

### 新增：看门狗"临终带走" + 修复一个真实误回收回归（v2.3.0）

- **用户/其他 AI 反馈**：有人 `rm -rf` 掉会话目录后，starter / `script` / `bash -i` 会变成无主孤儿
  （`kill --all` 的 argv 扫描能兜底，但得有人去跑）。要求：**看门狗临终带走**。
- **实现（看门狗脚本）**：
  - 每轮**开头**用内建 `$(<...)` 记下 `sess.pid`/`bash.pid`/`meta`（不 fork；**空读不覆盖旧值**），
    所以目录一被删，手里已经有 pid（不再有"头 15 秒没读 pid"的窗口）。
  - 目录消失时不再裸退：走**共用的 `cleanup_tree` 函数**做最后一次自证闭包清理（与 TTL 到期同一条实现），
    然后自退。**杀前必须自证**（`ps -o args=` 仍含本会话目录）——pid 文件已没，记住的 pid 可能被
    内核复用给无关进程，这一步不能省；并排除自身 `$$`。
  - **归属自检两道**（防"老看门狗反噬同名新会话"）：`watch.pid` 存在且 ≠ `$$` ⇒ 退出；
    `meta` 内容与启动时不同 ⇒ 退出（覆盖"新会话 mkdir 到写 watch.pid"之间约 0.5s 的窗口）。
  - 按用户要求**不写审计行**（该路径目录已没，写 `wd.log` 本来也必然失败）。
- **同时修掉一个我自己引入的真回归（套件抓到）**：上一版把 pid 记忆挪到循环开头后，
  **第一轮 tick 读取时 `bash.pid` 还没被 init payload 写出来**（约 0.4s）⇒ 子进程判据被跳过 ⇒
  把正在 `sleep 40` 的会话判成空闲并**误回收**（`--ttl 5` 时 15 秒就没了；`T2a/T2b` 失败）。
  用 1 秒粒度采样抓到现场（看门狗 etimes=15 时删目录）。修：**判闲前重读一次 shell pid** +
  **pid 未知一律视为忙**（没有 shell pid 就无法证明空闲，宁可不收也不误杀）。
  降级会话（`bash.pid` 缺失）不会被空闲回收，`list` 会新增告警提示"空闲回收对它不生效，用完请 kill"。
- **测试**：`--unit` 120 → **121 PASS**（新增"判闲前重读 + pid 未知视为忙"断言）；
  真机 `--session` 62 → **68 PASS / 0 FAIL**，新增：
  - **L1** `start --ttl 300` → 等 20s → `rm -rf` 目录（先断言孤儿确实 ≥3 个进程）→ 等 20s →
    该会话进程全消失、看门狗自退，随后 `kill --all` 无孤儿可扫（证明是看门狗清的）；
  - **L2** 伪造 `sess.pid`/`bash.pid` 指向无关 `sleep`（argv 不含会话路径）→ `rm -rf` 后
    那个进程**仍活着**、看门狗自退（自证闸门有效，防 pid 复用误杀）；
  - **L3** `rm -rf` 后立刻同名重建 → 新会话仍可执行命令、老看门狗已自退（防反噬）；
  - **O1a~O1d 改用 `--ttl 0` 会话**：有了临终带走后，带看门狗的会话不再把孤儿留给 `kill --all`，
    argv 扫描必须在"没有看门狗"的场景验证（语义对齐，非削弱）。
  - **T2a/T2b** 保持原样——它们正是这次回归的护栏。

### 测试工程：按块选择（拆块 + tick 可配 + 全量需 `--release`）+ 自证前缀匹配修复（v2.3.0）

- **测试工具支持按块运行**：
  1. **`live_session` 拆成 6 个可按块选的套件**：`live_session`(core) / `_ttl` / `_watchdog` /
     `_lifecycle` / `_orphan` / `_bugs`（沿用原有注释分块标记切分，每块自带宽前前置：缺凭据跳过 + 清场）。
     新增 **`--suite <名字>[,<名字>]`** 精确选择；`--session`/`--all` 仍是全跑。
  2. **看门狗 tick 可配**：`PYAISSH_SESSION_TTL_TICK`（整数 1~600，默认 **15**；非法值 WARN 回落；
     只收整数——脚本里用 `TICK=%d` 落值，小数会被截成 0 从而退化成忙循环）。测试新增 **`--fast`**
     （tick=3），用例里"等一个 tick"的等待换成 `_wait(n)` 随 tick 缩短（2 个 tick：33s → 9s）。
  3. **全量需显式确认**：`--all` / `--session` 要带 `--release`（或 `PYAISSH_TEST_RELEASE=1`）才执行，
     否则 `exit 2` 并给出 `--suite <块名>` 的用法提示；交互式选"全部"同样被拦。
- **顺带修掉一个真 bug（自证前缀匹配）**：清理时的"自证"用 `case "$A" in *"$D"*)`，`$D` 是会话目录
  **不带尾部斜杠** ⇒ 会话名互为前缀时会误匹配（`/tmp/pyaissh-sessions/work` 命中 `.../work2/out.log`）
  ⇒ `work` 的看门狗/`kill` 可能把 `work2` 的进程当自己的杀掉。改判据为 `*"$D/"*`（会话自身进程的
  argv 里目录后面必然跟 `/`：starter 的 `…/in`、`script` 的 `…/out.log`、看门狗的 `…/watch.sh`）。
  现象来源：整条 `--session --fast` 跑动时 `B1`（`sleep 32` 长等待）会话在 17.4s 消失，怀疑是被同前缀
  会话的清理误伤。
- **本次验证（遵守新规，只跑动过的路径）**：
  - `--unit` **123 PASS**（新增 tick 6 种取值 + `_wait` 断言）；
  - 闸门自证：`--all` / `--session` 均被拒（exit 2，秒回不联网）；
  - `--suite live_session_watchdog,live_session_ttl --fast`：**14 PASS / 0 FAIL，105 秒**
    （对比整条 `--session --fast` 406 秒、默认 tick 下 ~10 分钟）；
  - `--suite live_session_lifecycle,live_session,live_session_bugs --fast`：见 tests/CHANGELOG。

## [2.4.0] - 2026-09-24

### 变更：`session` 进程引擎整体换成 tmux（契约层一字未改）

- **一句话**：tmux 只当**引擎**（PTY / 进程生命周期 / 输出镜像），pyaissh 保留**契约层**
  （哨兵协议、每条命令独立退出码、字节级 `--offset` 读、CRLF·ANSI 清洗、单行 JSON 字段集）。
  **字段集不变**：契约基线 21 用例的键集原样保留，只按白名单新增 `orphans` / `orphans_total` /
  `orphan_remaining_total`（恒返回且恒空，AI 侧零感知）。
- **为什么换**：旧引擎的复杂度几乎全在"自己实现一个终端"——FIFO 写入的阻塞与半行、看门狗的判闲与
  自杀式清理（含临终带走、防 spin 护栏）、`kill` 按 argv 扫孤儿的自证、pid 复用误杀……
  这些在 tmux 里都是现成的、被验证过的行为。旧引擎代码**整体删除**（不留 `PYAISSH_SESSION_ENGINE`
  开关、不双引擎）。换掉后**每会话不再有常驻辅助进程**（旧：每会话一个看门狗 bash）。
- **依赖变成显式**（用户 2026-09-24 决定）：远端需要 **tmux ≥ 3.0**（实测 3.5a / Debian 13）。
  没有 → `tmux_missing`（message/`next_action` 给可执行安装命令：`apt-get install -y tmux` /
  `dnf install -y tmux` / `apk add tmux`）；版本过低 → `tmux_unsupported`；`tmux -V` 解析不出或
  server 起不来 → `tmux_failed`。**不自动安装**；装不了的环境（不可变系统/无包管理器/air-gapped）
  ⇒ session 不可用，长任务用 `exec` / `exec --detach`（这两条路不依赖 tmux）。
- **引擎形态（实测决定，见 `pyaissh-dev/SPEC_session_tmux.md` S1~S13）**：
  - 专用 socket `tmux -L pyaissh -f /dev/null`——与用户自己的 tmux **完全隔离**，不读用户 `~/.tmux.conf`；
  - **名字映射** `py-<会话名 utf-8 的 hex>`（tmux 会静默改写名字里的 `.`/`:`：`a.b` 变 `a_b`，
    且与真实存在的 `a_b` 撞名）；pane 级 target 必须写 `=NAME:`，会话级用 `=NAME`；
  - 命令注入走 `load-buffer` + `paste-buffer`（缓冲区是**数据**、不经 tmux 自己的命令行解析 ⇒
    零引号风险、二进制安全），哨兵协议与旧引擎逐字相同；
  - 输出镜像 `pipe-pane -o 'cat >> out.log'`；`out.log` 被外部删/变小时**不带 `-o`** 重 arm
    （否则 `cat` 继续往已 unlink 的 inode 写、新输出静默丢失）+ `log_recreated` 提示；
  - `ctrl-c` = `send-keys C-c`（tty 行规程交给前台进程组）；`--force` = 对内核给出的前台进程组
    `ps -o tpgid=` 发 SIGKILL（**不用 `C-\`(SIGQUIT)**：会 core dump）；被中断的命令自己不会产出
    哨兵（bash 收到 SIGINT 丢弃当前命令行），所以 `ctrl-c` **代它补一条**（INT→`exit_code=130`、
    `--force`→`137`）——正等它的 `read --wait-rc --token` 会正常收敛（`last.token` 那条）；
  - `kill` = pane_pid 的**进程树闭包快照**（必须在 `kill-session` 之前算，父进程被杀后子进程会被
    reparent）→ `tmux kill-session` → 幸存者 TERM→KILL→校验 → 删目录；权威根直接取 tmux 的
    `pane_pid`，不再需要 argv 自证（pid 复用误杀风险一并消失）；
  - 人类排障：`tmux -L pyaissh attach -t '=<会话目录里 tmux 文件的内容>'` 可看直播。
- **空闲回收语义变化**：从"每会话看门狗准点回收（TTL..TTL+TICK）"改为**惰性扫 + 每主机一个 reaper**
  ——会话子命令（send/run/read/ctrl-c/keys/list）入口**先给本次目标续期、再扫其它**（`kill` 本就是清理、不走惰性扫；`list` 不续期但会顺手扫；`start` 收尾扫一次
  并拉起 reaper）；reaper 是 `reap.sh --loop`，默认 **300 秒**一轮、**没有会话目录时自退**，
  `kill` 清空会话后**立刻**停掉它。没人再回来时最迟 **TTL + 5 分钟**被收；`--ttl 0` 关闭。
  `list` 的 `ttl_seconds`/`idle_seconds`/`expires_in_seconds` **字段与口径不变**。
- **会话目录内容变化**：**去掉** `in`(FIFO)/`err.log`/`sess.pid`/`bash.pid`/`watch.sh`/`watch.pid`/
  `wd.fifo`/`wd.log`，**保留** `out.log`/`meta`/`beat`/`last.token`，**新增** `tmux`
  （内容 = tmux 会话名，给 reaper 与人类 attach 用）；根目录新增 `reap.sh` / `.reaper.pid` / `.reaper.log`。
- **升级路径**：tmux 迁移之前遗留的会话目录**不再识别**（没有 `tmux` 名文件的目录一律当陌生目录）——
  `run/send/read` 按 `meta` 在不在分别报 `session_dead`/`session_not_found`
  并给 warning，用 `session kill` 清目录后重新 `start`。
- **既知行为变化（不是 bug）**：① `--no-pty` **参数已删除**（传了被 argparse 拒绝），结果恒 `pty: true`；
  ② 会话内 `TERM` = `tmux-256color`（tmux 强制决定，旧引擎继承 SSH 通道环境、常为空/dumb）；
  ③ `kill` 结果**总是**含 `orphans: []`/`orphans_total: 0`/`orphan_remaining_total: 0`（旧：空时省略）；
  ④ 空闲回收从"服务器端准点"变为"惰性扫 + 5 分钟一轮的 reaper"；⑤ 会话目录文件集变化（见上）；
  ⑥ 依赖 tmux ≥ 3.0；⑦ `ctrl-c --force` 打的是内核给出的前台进程组（更准）；
  ⑧ 多一个"服务器级"对象 tmux server（闲置时随最后一个会话退出，`list` 不受影响）。
- **已知边界（写进文档、不当作 bug）**：会话内**自己 `setsid`/`nohup` 起的脱离进程不随 `kill`
  消失**（与终端/tmux 语义一致，**旧引擎同款盲区**）；惰性扫与 reaper 都被绕过时残留的 tmux 会话
  **对人类可见**（`tmux ls`），且每会话只是一个闲置 shell；人类 attach 时人工输入会回显进
  `out.log`（日志变脏，不影响哨兵切片）；无 tmux 环境 session 不可用；会话里 `exit` 后目录仍在
  （`list` 显示 `dead`，用 `kill` 清）。
- **成本对比（真机实测，与旧引擎同机对比）**：3 个空闲会话常驻内存 **45.5 MB → 23.2 MB**（−49%）、
  常驻进程 **12 → 5**（1 tmux server + 3 `bash -i` + 1 reaper）、每会话常驻辅助进程 **1 → 0**、
  空闲 CPU 20 秒采样 5 进程合计 **1 jiffy**；单个空闲会话 15.4 MB / 3 进程（旧 ≈12 MB / 4 进程）
  ——单会话内存略高（多了全主机共享的 tmux server），多会话明显更省。数字已回填
  `docs/session.md` 的成本表（原先的 `TODO(perf)` 占位已替换）。
- **测试**：会话测试块按 tmux 引擎重写（**惰性回收 + reaper** 取代看门狗 tick、孤儿扫描用例删除、
  `PYAISSH_SESSION_TTL_TICK` → `PYAISSH_SESSION_REAP_INTERVAL`），详见 `tests/CHANGELOG.md`。
  真机会话块 6 个（core/ttl/engine/lifecycle/orphan/bugs）全绿，其中 `live_session_engine` 含
  socket 隔离、输出镜像、`log_recreated` 自愈、reaper 收敛与 PERF-01~04 实测断言。
- **文档**：`docs/session.md` 的引擎/生命周期/回收/兼容性整段重写（新增「依赖」「为什么换成 tmux」
  「进程与目录结构」「已知边界」）；`SKILL.md` 会话段、`pyaissh-mcp/pyaissh_mcp.py` 的
  `pyaissh_session` 工具描述与参数说明、`pyaissh-mcp/README.md` 同步更新（FIFO/`script`/看门狗/
  非 PTY 降级之类的旧措辞全部清掉）。
- **兼容包袱清理（同日收口）**：既然只有两台测试机用过旧引擎、且它们已无遗留目录，删掉全部旧引擎兼容面——
  `_session_files` 的遗留路径键（`in`/`sess.pid`/`bash.pid`/`watch.pid`/`err.log`）、kill 模板的 `LEGACY`
  探测与 `legacy_engine` 字段、`list`/`_session_load` 的"旧引擎遗留目录"分支、清洗里对 util-linux
  `script` 头尾行的过滤、`_session_info` 的 beat `st_mtime` 兜底；**并把 `--no-pty` 参数整体删除**
  （传了会被 argparse 拒绝，不再 no-op）。`fifo` 作为**契约字段**保留（恒 `null`），字段集不变量不变。
  净减：session 域 1935 → 1898 行（−37），制品 427 → 423 KB；行为面唯一变化是"陌生目录不再被特别标注"。
- **`fifo` 字段删除（同日）**：`session start` 不再返回旧引擎遗留的 `fifo`（曾经恒 `null`，只为迁移期
  字段集不变）；契约基线 `tests/contract/session_contract_v2.json` 的 `start_created`/`start_attach`
  两个用例同步去掉该键，`docs/contract.md`/`docs/session.md` 的说明一并删除。迁移期的"字段集一字不变"
  已由 SPEC V1 的 21/21 证据完成使命，不再保留恒空字段。
