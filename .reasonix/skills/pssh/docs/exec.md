# pssh exec — 执行远程命令（子文档）

> 这是 pssh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文只讲 exec 的完整参数语义。
> **何时读**：exec 遇到超时/截断/PTY/长脚本问题，或需要精确理解 exec 参数行为时。

## 用法

```bash
python3 pssh.py exec root@1.2.3.4 --cmd 'df -h'
python3 pssh.py exec root@1.2.3.4 --cmd-file - <<'EOF'   # 长脚本/特殊字符走 stdin
ls -la /var/log
EOF
python3 pssh.py exec root@1.2.3.4 --idle-timeout 120 --cmd 'tail -f /var/log/x.log'
python3 pssh.py exec root@1.2.3.4 --max-time 1200 --cmd 'apt upgrade'  # 长任务调大总时长上限
python3 pssh.py exec root@1.2.3.4 --pty --cmd 'top -b -n 1'           # 需要 TTY 的非交互命令
python3 pssh.py exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'watch -n 1 date'  # 剥离 ANSI 供 AI 解析
```

- **两个超时参数别用混**：`--idle-timeout`（旧名 `--exec-timeout` 仍兼容）= **静默超时**（默认 60s，上限 1200，非整数报 `bad_args`）：命令持续有输出就继续等，无输出超过该值判定挂死；`--max-time` = **总时长硬上限**（wall clock，默认 `2×idle-timeout` 且至少 120）：持续输出但不结束的命令（如 `while true; do echo x; done`）超过会被强制中断。`--max-time < --idle-timeout` 报 `bad_args` 退出 2；`--max-time` 上限 1200，传更大值（如 2000）直接报 `bad_args`；`--idle-timeout > 600` 时总上限恒为 1200 并**在 JSON `warnings` 注明**实际生效值
- 两种超时都返回**退出码 124**，`error` 字段细分 `exec_idle_timeout` / `exec_total_timeout`，消息都注明"远程进程可能仍在运行"——重试副作用命令前先 pgrep 确认/清理
- `--max-output`（默认 256KB）：stdout/stderr 单流最大保留字节，超出时**保留头尾各一半且行对齐**（边界退到最近的换行——逐行解析不会拿到缺半的残行；二进制流无换行保持字节边界），省略处插入 `...[pssh: 已截断，省略 N 字节]...` 标记（内存缓冲丢弃的接缝也有 `...[pssh: 中间省略 N 字节]...` 带内标记，两者都按 head/tail 已有换行去重、不产生空行），且 `warnings` 注明省略量——大输出不会被静默截断，AI 看到标记应调大 `--max-output` 取全文或改用 tail
- 截断判定用**结构化字段**（不用肉眼扫文本）：`stdout_truncated`/`stderr_truncated`（该流是否截断/丢中间）、`stdout_omitted_bytes`/`stderr_omitted_bytes`（省略量）、`output_truncated`（任一为真即 true）；`stdout_bytes` 是**原始接收字节**（截断前），**`stdout_bytes - stdout_omitted_bytes` = 实际拿到/保留的字节**（含截断标记附加字节，可据此精确对账 AI 实际拿到了多少输出）
- **截断时完整输出自动落盘**：任一流被截断（`stdout_truncated`/`stderr_truncated` 或 drain 不完整）时，**完整原始输出**（含内存层丢弃的中间字节，读线程边收边写）会写入 spill 文件并把路径回传 JSON（`stdout_spill_file`/`stderr_spill_file`）——AI 需要文件中间内容时直接读该文件，无需重跑 `sed -n` 或调大 `--max-output` 重试；未截断时 spill 文件**自动删除不留垃圾**。目录默认系统临时目录，可用 `--spill-dir <dir>` 指定；文件命名 `pssh-<stdout|stderr>-<随机>.spill`，用完可自行清理
- `--no-credential-warn`：关闭"命令含疑似凭据"的启发式 WARN。`--profile`/`--parallel` 这类双横线长选项已内置排除（`-p` 紧贴形态拒绝双横线前缀），一般不需要关；只在仍有误报时用——注意关闭后命令里的**真实凭据也不再被提示**，结果 JSON 的 `cmd` 字段仍会原样回显命令，脱敏责任回到调用方
- **exec 错误 JSON 恒带 `stdout`/`stderr`/`output_incomplete`**（可能为空串；错误路径 `output_incomplete` **恒为 true**——命令被中断输出必然不完整，区别于成功路径的超限裁剪 `output_truncated`）：空串 = 命令没跑起来或零输出，非空 = 执行到一半中断，据此决定重试策略
- `duration_ms` **含连接耗时**（跳板机/慢网络下偏大），评估命令本身耗时请减去连接时间；错误 JSON 同样带 `duration_ms`（已运行时长）
- **`cmd` 字段与截断**：结果 JSON 的 `cmd` 回显命令原文（含凭据需脱敏，转发前处理）；超 `CMD_ECHO_LIMIT`（8KB）时截断——`cmd` 保留头尾 + 中间省略标记、`cmd_truncated: true`、warnings 提示（完整命令见原始调用，`--cmd-file` 时为本地文件可重读）。截断只影响回显，不影响执行与凭据检测
- 远程退出码直接透传（255 例外：远程恰为 255 时本地返 254，见 SKILL.md 退出码表）
