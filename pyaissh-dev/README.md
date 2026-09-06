# pyaissh-dev — 重构开发仓库（维护入口）

> 本仓库是 pyaissh 的开发态源码（12 个域文件 + 构建器）。
> **发布物 = `build_single.py join` 生成的单文件**（成品放 skills/pyaissh/pyaissh.py 由用户决定）。
> **维护规矩：只改 `domains/`，绝不手改单文件**（单文件可再生；diff 找 `pyaissh.built.py` 比对）。

## 代码地图（找代码先看这里）

| 域文件 | 内容 | 关键符号 |
|---|---|---|
| `00_head.py` | 文件头 docstring、stdlib import、**极早期信号窗口**、VERSION | `_SIGTERM_RECEIVED`/`_sigterm_handler`/`VERSION` |
| `01_globals.py` | 常量区（超时/轮询/缓冲）、`_RETRYABLE_ERRORS`、可变全局 | `MAX_TIME_CAP`/`PARALLEL_MIN_SIZE`/`_ACTIVE_TRANSPORTS` |
| `02_cred_regex.py` | 凭据启发式正则全家 + 验收注释 + ANSI/文件名正则 | `_SENSITIVE_CMD_RE`/`_P_SENS_*`/`_P_LOOKBEHIND_P` |
| `03_env_paths.py` | `.env` 解析/加载、MSYS 路径修复、远端路径规范化 | `load_env`/`_fix_msys_*`/`_normalize_remote_path` |
| `04_console_out.py` | console UTF-8、log（含 `_QUIET`）、输出 emit 系、异常类 | `log`/`emit`/`_emit_fields`/`emit_error`/`SshError` |
| `05_util.py` | 截断/对齐、转义 hint、凭据检测调用、spill、ANSI 清理 | `_truncate_output`/`_shell_escape_hint`/`warn_sensitive_cmd` |
| `06_conn.py` | target 解析、别名 env、连接解析、`_do_connect`、host key | `parse_target`/`resolve_conn`/`resolve_jump` |
| `07_sftp_transfer.py` | SFTP 传输辅助：watchdog/并行分片/原子改名/断点/win 安全路径 | `open_sftp`/`_parallel_fetch`/`_parallel_put`/`_sftp_atomic_rename` |
| `08_cmd_exec.py` | **exec 编排 + 三段**：`_prepare_exec_command`(组装) / `_connect_exec`(连接) / `_exec_session`(执行会话) | `cmd_exec`/`_exec_session`（内嵌 `_partial_extra`/`_read`/`_drain_rest`）|
| `09_cmd_transfer.py` | `cmd_upload` + `cmd_download`（线性：校验→预置→连接→主 try→结果）| `cmd_upload`/`cmd_download` |
| `10_cmd_test_ls.py` | `cmd_test` + `cmd_ls` | `cmd_test`/`cmd_ls` |
| `11_cli_main.py` | 参数解析器、子命令注册、信号 setup/responder、main | `build_parser`/`add_conn`/`main` |

## 维护/修复标准流程（给 AI 修复小问题）

1. **定位**：按代码地图找域文件（如"凭据 WARN 误报" → `02_cred_regex.py`）
2. **修改**：只编辑 `domains/<域>.py`
3. **构建**：`python pyaissh-dev/build_single.py join`（产物 `pyaissh.built.py`）
4. **验证**：
   - 回归：`PYAISSH_PY=<built> python stest_tmp/verify_r3.py`（54 例）
   - 真机：sudo/field 套件对 built（`v515_sudo_verify.py`/`v516_field_verify.py`，内部已支持对被测目标跑——真机套件连 B2）
5. **对比确认**：金标 diff 语义从"逐字节一致"（搬移期）变为"测试矩阵全绿"（改码期）
6. **提交**：dev 仓库 git commit（可回滚）

## 构建器

- `build_single.py split`：单文件 → 12 域文件（仅搬移期用）
- `build_single.py join`：12 域文件 → 单文件（确定性，逐字节稳定）
- `build_single.py check`：金标对比（built vs 指定单文件）+ 编译
- `MANIFEST_domains.txt`：域文件顺序（显式，非 import 拓扑；顺序尊重顶层执行语义——00 含极早期信号、04 含 console/emit 模块级执行点）

## 测试映射

| 测试 | 位置 | 覆盖 |
|---|---|---|
| verify_r3 | 主仓库 stest_tmp（gitignore）| L4 凭据矩阵/parse_target/编码/stdin 等 54 例 |
| v515_sudo_verify | 同上 | --sudo 12 例真机 |
| v516_field_verify | 同上 | --field 10 例真机 |
| v2_compare(_a) | 同上 | 重构前后行为对比（双机）|
