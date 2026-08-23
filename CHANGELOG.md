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
