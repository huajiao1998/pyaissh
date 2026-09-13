# pyaissh exec — 执行远程命令（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文只讲 exec 的完整参数语义。
> **何时读**：exec 遇到超时/截断/PTY/长脚本问题，或需要精确理解 exec 参数行为时。

## 用法

```bash
python3 pyaissh.py exec root@1.2.3.4 --cmd 'df -h'
python3 pyaissh.py exec root@1.2.3.4 --cmd-file - <<'EOF'   # 长脚本/复杂命令走 stdin（Windows PowerShell 调用时复杂命令务必如此）
ls -la /var/log
EOF
python3 pyaissh.py exec root@1.2.3.4 --idle-timeout 120 --cmd 'tail -f /var/log/x.log'
python3 pyaissh.py exec root@1.2.3.4 --max-time 1200 --cmd 'apt upgrade'  # 长任务调大总时长上限
python3 pyaissh.py exec root@1.2.3.4 --pty --cmd 'top -b -n 1'           # 需要 TTY 的非交互命令
python3 pyaissh.py exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'watch -n 1 date'  # 剥离 ANSI 供 AI 解析
```

- **两个超时参数别用混**：`--idle-timeout`（旧名 `--exec-timeout` 仍兼容）= **静默超时**（默认 60s，上限 1200，非整数报 `bad_args`）：命令持续有输出就继续等，无输出超过该值判定挂死；`--max-time` = **总时长硬上限**（wall clock，默认 `2×idle-timeout` 且至少 120）：持续输出但不结束的命令（如 `while true; do echo x; done`）超过会被强制中断。`--max-time < --idle-timeout` 报 `bad_args` 退出 2；`--max-time` 上限 1200，传更大值（如 2000）直接报 `bad_args`；`--idle-timeout > 600` 时总上限恒为 1200 并**在 JSON `warnings` 注明**实际生效值
- 两种超时都返回**退出码 124**，`error` 字段细分 `exec_idle_timeout` / `exec_total_timeout`，消息都注明"远程进程可能仍在运行"——重试副作用命令前先 pgrep 确认/清理
- `--max-output`（默认 **64KB**，v2.2 起由 256KB 下调）：stdout/stderr 单流最大保留字节，超出时**保留头尾各一半且行对齐**（边界退到最近的换行——逐行解析不会拿到缺半的残行；二进制流无换行保持字节边界），省略处插入 `...[pyaissh: 已截断，省略 N 字节]...` 标记（内存缓冲丢弃的接缝也有 `...[pyaissh: 中间省略 N 字节]...` 带内标记，两者都按 head/tail 已有换行去重、不产生空行），且 `warnings` 注明省略量——大输出不会被静默截断。**为何下调**：结果 JSON 过大时宿主会裁剪工具结果中段（`[... tool result middle pruned ...]`），连 spill 路径都可能一起被裁掉；64KB 覆盖多数命令全文，超出的完整输出本来就落 spill 文件
- 截断时结果里带 **`next_action`**（v2.2）：直接写明"完整输出已落盘到哪个文件、读文件不要重跑"——不必自己拼线索；需要内联更多时才调大 `--max-output`
- 截断判定用**结构化字段**（不用肉眼扫文本）：`stdout_truncated`/`stderr_truncated`（该流是否截断/丢中间）、`stdout_omitted_bytes`/`stderr_omitted_bytes`（省略量）、`output_truncated`（任一为真即 true）；`stdout_bytes` 是**原始接收字节**（截断前），**`stdout_bytes - stdout_omitted_bytes` = 实际拿到/保留的字节**（含截断标记附加字节，可据此精确对账 AI 实际拿到了多少输出）
- **截断时完整输出自动落盘**：任一流被截断（`stdout_truncated`/`stderr_truncated` 或 drain 不完整）时，**完整原始输出**（含内存层丢弃的中间字节，读线程边收边写）会写入 spill 文件并把路径回传 JSON（`stdout_spill_file`/`stderr_spill_file`）——AI 需要文件中间内容时直接读该文件，无需重跑 `sed -n` 或调大 `--max-output` 重试；未截断时 spill 文件**自动删除不留垃圾**。目录默认系统临时目录，可用 `--spill-dir <dir>` 指定；文件命名 `pyaissh-<stdout|stderr>-<随机>.spill`，用完可自行清理
- `--no-credential-warn`：关闭"命令含疑似凭据"的启发式 WARN。`--profile`/`--parallel` 这类双横线长选项已内置排除（`-p` 紧贴形态拒绝双横线前缀），一般不需要关；只在仍有误报时用——注意关闭后命令里的**真实凭据也不再被提示**，结果 JSON 的 `cmd` 字段仍会原样回显命令，脱敏责任回到调用方
- **exec 错误 JSON 恒带 `stdout`/`stderr`/`output_incomplete`**（可能为空串；错误路径 `output_incomplete` **恒为 true**——命令被中断输出必然不完整，区别于成功路径的超限裁剪 `output_truncated`）：空串 = 命令没跑起来或零输出，非空 = 执行到一半中断，据此决定重试策略
- `duration_ms` **含连接耗时**（跳板机/慢网络下偏大），评估命令本身耗时请减去连接时间；错误 JSON 同样带 `duration_ms`（已运行时长）
- **`cmd` 字段与截断**：结果 JSON 的 `cmd` 回显命令原文（含凭据需脱敏，转发前处理）；超 `CMD_ECHO_LIMIT`（8KB）时截断——`cmd` 保留头尾 + 中间省略标记、`cmd_truncated: true`、warnings 提示（完整命令见原始调用，`--cmd-file` 时为本地文件可重读）。截断只影响回显，不影响执行与凭据检测
- **写远程脚本文件的推荐姿势**：要把脚本内容写到远端文件（如 `nm_switch.sh`、`listen.conf`）再执行时，**用 `--cmd-file -` 从 stdin 喂**（heredoc 零引号顾虑），而不是 `--cmd 'cat > x << "EOF"...'`——后者引号定界符走钢丝，`$`/反引号容易被本地 shell 先展开（实测教训）。推荐：
```bash
pyaissh exec root@1.2.3.4 --cmd-file - <<'EOF'
cat > /tmp/nm_switch.sh <<'REMOTE'
#!/bin/bash
# $ 与反引号在这里安全（'REMOTE' 引住定界符，内容零展开）
echo "HOME=$HOME"
REMOTE
bash /tmp/nm_switch.sh
EOF
```
  （外层 heredoc 喂给 pyaissh 的命令脚本本身也用引住的定界符；命令含 `$`/反引号时 `--cmd-file -` 在 bash/Git Bash 下同样是首选，不只 PowerShell）
- **Git Bash 路径**：`--cmd-file` / `--spill-dir` 与 `--local` 同款 MSYS 转换——经 `./pyaissh` 包装器（禁路径转换）时，Unix 风格路径（`/tmp/x.sh`）自动转 Windows 真实路径，不会落错位置或报 Errno 2；Linux 直接运行时原样透传

### 长任务配方（apt 安装 / 大传输 / 镜像拉取 / 编译等 2-10 分钟零输出操作）

真实教训：这类长静默操作**必然撞 `--idle-timeout`（默认 60s）**——apt 安装、332M 传输、镜像拉取都是 2-10 分钟零输出。稳定模式（三次踩 idle-timeout 后形成）：

1. **先预估时长，调大 `--idle-timeout`**：`--idle-timeout 600`（上限 1200s=20min；`--max-time` 默认 2×idle-timeout 且至少 120，**记得同步调大覆盖总时长**，如 `--max-time 1200`）。零输出但继续等：持续有输出就无限续、无输出超 idle-timeout 判挂死——长任务先估总时长给足两个参数
2. **传输类（upload/download 大文件）**：默认 ≥8MB 自动 4 连接并行分片（也可显式 `--parallel 8` 提速）；真超时中断是安全的——写 `.part.<pid>` 原子改名不留半截最终文件，重跑 `--resume` 续传或整传重跑即可（幂等）；**轮询远端确认**：中断后用 `ls`/`test` 查远端文件大小/存在性再决定续传还是重跑（`file_list`/`bytes_transferred` 字段对账）
3. **循环/长命令类**：给命令**自己加心跳输出**——`while ...; do ...; echo "heartbeat $(date +%s)"; done`（循环内周期 echo，让 idle-timeout 不触发）；纯等待类（`sleep 300`）直接给足 `--idle-timeout` 即可；**工具侧心跳 `--progress [SECS]`（v2.1，默认 30）**：命令每静默/持续运行超过 N 秒往 stderr 打一行 `[PROGRESS] 仍在运行，已 Xs`——AI 知道进程活着不是挂死，不用靠猜；**注意心跳不重置静默计时**（idle-timeout 仍按真实输出判定，心跳防不了超时，只解决"是否还活着"的不确定性）
4. **不可预估 / 超 20min 上限 / 想"边跑边看"**：**用 `--detach`（v2.2 一等能力，推荐）**——后台作业 + `pyaissh log` 增量读，见下节；手工兜底才是 `--cmd "nohup <长命令> >/tmp/task.log 2>&1 & echo started"` + 周期 `exec --cmd "tail -3 /tmp/task.log"`（手工拼容易踩转义坑：本地 PowerShell 会展开 `$?`/`$!`，rc 捕获也常写错）
5. **中断后**：先 pgrep/tail 确认远端实际状态（超时类 JSON 带 `remote_may_be_running:true` = 进程可能仍在跑，别盲目重跑副作用命令），再决定续传/重跑/清理

### `--detach` + `log`：后台作业（v2.2，边跑边看 / 绕开宿主调用上限）
```bash
python3 pyaissh.py exec root@1.2.3.4 --detach --cmd 'apt install -y nginx'   # 立即返回
#   → {detached:true, job_id, pid, status, log, rc, job_dir, next_action}
python3 pyaissh.py log root@1.2.3.4 --list                                   # 列作业：状态/日志大小/退出码/时间
python3 pyaissh.py log root@1.2.3.4 --job-id <id>                            # 尾部 100 行（--lines N 可调）
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --offset 0                 # 增量读，返回 next_offset
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --offset <next_offset>     # 接着上次读（轮询不重复）
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --wait-rc 300              # 等结束（≤600s）拿 exit_code
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --cleanup                  # 清理远端作业目录
```
- **机制**：作业脚本落远端 `/tmp/pyaissh-jobs/<job_id>/`（`job.sh` = 你的命令原文、`run.sh` = 运行器），`setsid nohup` 启动 → **SSH 断开、宿主单次调用超时都不影响作业**；输出合并进 `job.log`，结束时退出码写入 `job.rc`，进程组 leader pid 写入 `job.pid`
- **状态机（v2.2.1 三级）**：`finished`（`job.rc` 存在 = 退出码）/ **`dead`**（无 rc 且 `job.pid` 存活探测判定进程已消失——被 kill / OOM / 崩溃，退出码不可知）/ `running`。被 kill 的作业**不再永远 running**，`--wait-rc` 与轮询都能收敛；`dead` 时结果带 `hint` 说明
- **`log` 载荷字段是 `stdout`**（v2.2.1 起，与 `exec` 对齐；它是 `2>&1` **合并流**，`stream:"stdout+stderr"` 声明）：`pyaissh log h --job-id <id> --field stdout` 直接取裸内容
- **停掉作业**：`log --job-id <id> --kill` → 读 `job.pid` 对**进程组** `TERM`（setsid 后 pid==pgid）→ 宽限 5s → `KILL`；Linux 上先校验 `/proc/<pid>/cmdline` 确属本作业（防 pid 复用误杀，不匹配即 `kill_failed` 拒绝）。可与 `--wait-rc` 连用（kill 后立刻收敛为 `dead`）
- **落盘权限（v2.2.1 从严）**：目录 0700、`job.sh`/`job.log`/`job.rc`/`job.pid` 0600、`run.sh` 0700（`run.sh` 首行 `umask 077` 管住 shell 创建的文件）——命令原文与完整输出都在盘上，同机其他用户不可读；`exec --detach` 结果里 `permissions` 字段声明这套权限
- **为什么值得用**：`exec` 前台受**宿主单次调用时长**限制（约 600s，pyaissh 自身可到 1200）；4-5 分钟的更新监控在前台只能结束时一次看到，而 `--detach` + `log --offset` 让"正在发生什么"变成可轮询的小 payload
- **尾部截断的坑**：`--lines N` 模式下若回传内容又超 `--max-output`，中段会被省略（`truncated:true` + `omitted_bytes`），而此时 `has_more=false`、`next_offset` 指向 EOF——**看起来像"读完了"**。v2.2.1 起这种情况 `next_action` 会明确提示"用 `--offset 0` 顺序读补齐"
- **与前台 exec 的差异**：`--detach` 与 `--sudo`/`--pty` 互斥（bad_args）；命令**原文会落盘到远端 `job.sh`**（别写明文凭据；敏感命令用完 `--cleanup`）；`--max-output`/`--encoding` 同样作用于 `log` 的内容回传
- **短作业**：启动后 0.3s 内已结束的作业直接返回 `status:"finished"` + `exit_code`（无需再 log）；启动即死（如命令不可执行）会报 `detach_failed` 并附日志尾巴

- **`--sudo` 提权（普通用户登录时）**：`--sudo --cmd "apt update"` 自动 `sudo -S -p ''` 提权。密码来源：`--sudo-password`（空串视为未设置）> `PYAISSH_SUDO_PASSWORD` env；密码只经 SSH stdin 注入（写完即 close），**命令文本/cmd 字段/日志/远端磁盘均无密码**，`-p ''` 压掉提示符（成功路径 stderr 不含 `password for`）。**组装规则**：简单命令（无 shell 元字符）直连 `sudo -S -p '' <cmd>`——sudoers NOPASSWD 按命令匹配仍生效（`NOPASSWD: /usr/bin/apt` 对 `sudo apt update` 有效）；复合命令（`&&`/`||`/`;`/管道/重定向/`$()`/反引号/换行）→ `sudo -S -p '' bash -c '<单引号转义>'` 整链提权（`&&` 第二段同样 root）。**无密码时自动 `sudo -n` 免密探测**：免密命令直接跑；需密码立即失败不挂，且 stderr 命中 sudo 报错特征（如 `a password is required`）时 warnings 附密码配置提示；**有密码但密码错**（stderr 命中 sudo 专属报错如 `sudo: 1 incorrect password attempt`）时 warnings 提示检查密码——两种提示都只认 sudo 自己的报错（带 `sudo:` 前缀），命令自身 stderr 里的 "Sorry, try again"/"password" 等词不会误触发。`--sudo` 与 `--pty` 互斥（bad_args 退出 2）。`--cmd` 与 `--cmd-file` 两条路径统一处理。**注意**：sudo 无法执行 shell 内建命令（`--sudo --cmd 'exit 3'` 会得到误导性的 "a password is required"，因为 exit 不是可执行文件）——内建命令请外套 `bash -c`（`--sudo --cmd "bash -c 'exit 3'"`）
- 远程退出码直接透传（255 例外：远程恰为 255 时本地返 254，见 SKILL.md 退出码表）
