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
- `docs/errors.md`：文末新增"凭据 WARN"注记，指向 `pssh.py` 中 `_SENSITIVE_CMD_RE` 定义处的判定形态与已测案例。

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
- `docs/transfer.md`：`--skip-existing` 仅比大小（原子传输保证 pssh 自产最终文件完整；外部损坏可 md5 抽查）。
- `docs/errors.md`：退出码 254 歧义提示（区分远程真实 254 vs 远程 255 映射，看 JSON 双字段）。

## [1.5.5] - 2026-08-23

### 修改
- **paramiko 惰性 import（启动提速）**：`import paramiko` 从模块顶部挪进 `_do_connect`（唯一建连入口），`_AtomicAutoAddPolicy` 类改为 `_atomic_auto_add_policy()` 工厂（首次调用时 import + 定义 + 缓存单例）。**不需要连接的路径提速 ~2.7 倍**：`--version`/`--help`/`bad_args`/缺用户名/别名未配置 从 ~296ms 降到 ~110ms（纯解释器+标准库基线 59ms，剩余为模块解析冷启动）；极早期信号窗口更短（handler 注册后只剩标准库 import，paramiko 的 ~190ms 不再落在窗口内）。真实连接路径不受影响（paramiko 照常在建连时加载，实测 1766ms 连接正常）。实测依据：paramiko import 188ms（其中 `paramiko.config → invoke` 可选依赖链 ~120ms，pssh 不用 SSHConfig，但 invoke 是否加载取决于环境安装，代码侧无法卸载；惰性只优化错误路径，真实路径提速需环境侧卸载 invoke）。
- **惰性 import 回归修复（第 5 轮审查抓出）**：函数内 `import paramiko` 默认绑定**局部**名，而 `cmd_download`/`cmd_ls`/`_sftp_put_atomic` 引用的是**模块全局** `paramiko`——顶部 import 删除后，这三处的 `getattr(paramiko, "SFTP_NO_SUCH_FILE", 2)`（"路径不存在"错误分类路径）抛 `NameError` 被误归类为 `download_failed`/`ls_failed`（实测"下载不存在远程 → download_failed/1"而非 `bad_args/2`）。修复：惰性 import 处加 `global paramiko` 绑定全局（`_do_connect` 与 `_atomic_auto_add_policy` 两处）。成功路径测试（verify_r3/双机 test/s2_*）均测不到此缺陷，冒烟矩阵的"下载不存在"用例抓出——验证了错误路径用例的价值。修复后双机 download/ls 不存在路径全部 `bad_args/2`。
- **MSYS 路径转换补漏（`--cmd-file` / `--spill-dir`）**：`_fix_msys_local_path`（v1.5.1 起用于 `--local`）漏了两个新参数——Git Bash 经 `./pssh` 包装器（MSYS_NO_PATHCONV=1）运行时，`--cmd-file /tmp/x.sh` 被 Windows Python 解析成盘根而报 `read_cmd_failed`（Errno 2），`--spill-dir /tmp` 的 spill 落错位置（D:\tmp 而非 Git Bash 的 /tmp）且无提示。修复：两处均套用 `_fix_msys_local_path`（内部含 `~` 展开；非 Windows / 无 MSYSTEM / 相对路径原样返回，不影响 Linux 与普通终端）。验证：单元 5 分支全过；真实 Git Bash（MSYS_NO_PATHCONV=1）集成——`--cmd-file /tmp/gb_test.sh` 成功执行、`--spill-dir /tmp` 的 spill 文件落在 cygpath 转换后的真实位置（D:\DSH\temp）且存在。

## [1.5.6] - 2026-08-23

### 新增
- **错误 JSON 新增 `retryable` 字段（机器可读的重试决策）**：所有错误 JSON（含 argparse 层 `bad_args`）恒带 `retryable`（bool）——AI 自动重试策略直接读它，不必解析 message 文本。映射表集中定义（常量区 `_RETRYABLE_ERRORS`）：**true** = 网络/传输/中断类（`connection_timeout`/`connection_refused`/`connection_failed`/`dns_failed`/`connection_lost`/`interrupted`/`upload_failed`/`download_failed`/`upload_timeout`/`download_timeout`/`exec_idle_timeout`/`exec_total_timeout`/`exec_timeout`）；**false** = 凭据/参数/本地文件/命令失败类（`auth_failed`/`host_key_rejected`/`bad_args`/`read_cmd_failed`/`exec_failed`/`jump_failed`）。语义边界：exec 超时类 true 仅表示"值得一试"——远程进程可能仍在运行/命令可能有副作用，重试前必须读 message 的"远程进程可能仍在运行"提示并先 pgrep 确认（bool 给机器"值不值得试"，message 给"怎么试才安全"）。成功结果无此字段。**真机验证 11 例全过**（逐类错误触发确认 retryable 值：auth_failed=false、connection_timeout=true、bad_args=false（argparse 层修复后）、exec_idle_timeout=true、interrupted=true、成功结果无字段等）。
- **单文件上传自动建父目录的可见性（新发现，真机抓的）**：`upload --remote /x/y/z.bin` 本就自动 mkdir -p 父目录（与 scp 预期不同、且文档未记载单文件场景、无任何提示——拼写错误（如 `/usr/loca/bin/x`）会静默造垃圾目录树，与尾斜杠"绝不静默创建"的严格语义不对称）。修复：`sftp_makedirs` 改为返回**本次新建的目录列表**（外层→内层，已存在不算），单文件上传分支新建父目录时 `warnings` + stderr `[MKDIR]` 提示创建了哪些（AI 可见可发现）；父目录已存在不提示；目录上传的自动创建是文档明示行为保持静默；尾斜杠语义不变（bad_args）。docs/transfer.md 补"单文件上传自动创建远端父目录"条目（与尾斜杠语义的区别一并说明）。**真机验证 4 例全过**：新建父目录提示+远端建出、已存在无提示、尾斜杠 bad_args 不变、目录上传静默。
- **依赖缺失独立分类 `dependency_missing`**：环境未装 paramiko 时（`import paramiko` 在 `_do_connect` 抛 ModuleNotFoundError），此前实测误归 `connection_failed` 且 **`retryable: true`**——误导 AI 查网络、且白白重试。修复：`_do_connect` 的 import 包 try/except，转 `SshError("依赖缺失: ...与目标主机/网络无关，重试前请先安装依赖", "dependency_missing")`；不在 `_RETRYABLE_ERRORS` → retryable=false。exec/test/download 三条路径实测全部正确分类（`python -S` 模拟无 paramiko 环境）；正常环境无回归。docs/errors.md 错误表新增该行。

## [1.5.7] - 2026-08-24

### 修改
- **retryable 映射哲学统一（补 ls_failed/ls_timeout/test_failed）**：v1.5.6 初版映射漏了三个同性质类型——`ls_failed`/`ls_timeout` 与 upload/download 的 `_failed/_timeout` 完全同性质（SFTP 网络/通道问题），`test_failed` 是连接成功后的通道/传输异常兜底（test 只跑系统查询，命令本身几乎不会失败；信号中断已单独归 interrupted）。三者挪入 true 集，统一原则："**所有 SFTP 传输/超时类 + test_failed → true**；凭据/参数/本地文件/命令失败类 → false"。验证：静态映射确认 + emit_error 单元输出（ls_failed/ls_timeout/test_failed → true，auth_failed/bad_args → false）；真机 ls 权限拒绝触发因 root 忽略权限位不可行（环境限制，映射逻辑由单元覆盖）。

### 文档
- **零 token 传输卖点文档化**：pssh 从设计上就不把文件内容回传 JSON（upload/download 结果只含 `files`/`bytes`/`file_list` 元数据）——对比 MCP SSH 生态普遍把传输内容塞进 LLM 上下文的通病，这是天然卖点。SKILL.md（description + 定位段）与 `--help` epilog 新增"传输零 token 消耗"说明（实测：1MB 随机文件传输后结果 JSON 仅 410 字节纯元数据）。
- **品牌改名 pssh → pyaissh（开源发布准备）**：仓库/命令/文件名/文档全量改名——pssh.py→pyaissh.py、pssh.cmd→pyaissh.cmd、bash 包装 pssh→pyaissh、--help 的 prog、输出标记（[pssh: 已截断]→[pyaissh: 已截断]、seam/[pssh] 前缀）、spill 文件前缀（pssh-stdout-→pyaissh-stdout-）、SKILL.md 与全部 docs 的调用示例与描述。**刻意保留**：PSSH_* 环境变量（运行时协议，改名是破坏性变更，pssh_host_prod 小写示例同步保留）、_pssh_* 内部属性（paramiko 对象上的实现细节）、技能目录路径 .reasonix/skills/pssh/ 与 SKILL.md 
ame: pssh（skill 加载 id 稳定）、pssh.py.bak（历史备份）、历史 CHANGELOG 条目（记录不改）。git 历史中的旧文件名随提交记录保留。
- **环境变量与内部属性改名（彻底对齐 PYAISSH）**：PSSH_* 环境变量 → PYAISSH_*（PSSH_PASSWORD→PYAISSH_PASSWORD、PSSH_KEY→PYAISSH_KEY、PSSH_USER→PYAISSH_USER、PSSH_PORT→PYAISSH_PORT、PSSH_JUMP_KEY/PSSH_JUMP_PASSWORD→PYAISSH_JUMP_*、PSSH_HOST_<名称>→PYAISSH_HOST_<名称>（含 _PASSWORD/_KEY 专属凭据）、PSSH_ALLOW_CWD_ENV→PYAISSH_ALLOW_CWD_ENV、小写示例 pssh_host_prod→pyaissh_host_prod）；_pssh_* 内部属性 → _pyaissh_*（_pssh_home/_pssh_last_activity/_pssh_io_timeout/_pssh_watchdog/_pssh_watchdog_killed/_pssh_posix_rename_warned）；测试 harness 的 PSSH_PY→PYAISSH_PY、测试脚本 env 全量同步；技能目录改名为 pyaissh（SKILL.md name: pyaissh）。**注意：这是破坏性变更**——旧 PSSH_* 配置不再生效，部署/CI/.env 需迁移到 PYAISSH_*（发布前完成，无既有用户受影响）。验证：verify_r3 54/54、v3_sig_unit 3/3、s2_stale 通过、双机 test 正常。
