# pyaissh exec — 执行远程命令（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文只讲 exec 的完整参数语义。
> **何时读**：exec 遇到超时/截断/PTY/长脚本问题，或需要精确理解 exec 参数行为时。

## 用法

```bash
python3 pyaissh.py exec root@1.2.3.4 --cmd 'df -h'
python3 pyaissh.py exec root@1.2.3.4 --cmd-file - <<'EOF'   # 长脚本/复杂命令走 stdin（Windows PowerShell 调用时复杂命令务必如此）
ls -la /var/log
EOF
python3 pyaissh.py exec root@1.2.3.4 --script ./deploy.sh     # 本地脚本文件直达远端执行（v2.5.0：SFTP→bash→自动删，脚本正文不进 JSON）
python3 pyaissh.py exec root@1.2.3.4 --idle-timeout 120 --cmd 'tail -f /var/log/x.log'
python3 pyaissh.py exec root@1.2.3.4 --max-time 1200 --cmd 'apt upgrade'  # 长任务调大总时长上限
python3 pyaissh.py exec root@1.2.3.4 --pty --cmd 'top -b -n 1'           # 需要 TTY 的非交互命令
python3 pyaissh.py exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'watch -n 1 date'  # 剥离 ANSI 供 AI 解析
```

- **两个超时参数别用混**：`--idle-timeout`（旧名 `--exec-timeout` 仍兼容）= **静默超时**（默认 60s，上限 1200，非整数报 `bad_args`）：命令持续有输出就继续等，无输出超过该值判定挂死；`--max-time` = **总时长硬上限**（wall clock，默认 `2×idle-timeout` 且至少 120）：持续输出但不结束的命令（如 `while true; do echo x; done`）超过会被强制中断。`--max-time < --idle-timeout` 报 `bad_args` 退出 2；`--max-time` 上限 1200，传更大值（如 2000）直接报 `bad_args`；`--idle-timeout > 600` 时总上限恒为 1200 并**在 JSON `warnings` 注明**实际生效值
- **CRLF 行尾自动归一（v2.2.4）**：命令文本（内联 `--cmd`、`--cmd-file` 文件、`--cmd-file -` 的 stdin）里的 `\r\n`/孤立 `\r` **默认归一为 `\n`**，结果回传 `crlf_normalized: <处数>` + warnings 说明。远端 bash 会把 `\r` 当词的一部分：典型症状 `$'\r': command not found`、`if/then/fi` 行报语法错、heredoc 落盘的文件每行带 CR（此前得靠 `sed -i 's/\r$//'` 收尾——现在不必了）。要**原样发送**（例如确实要产出 CRLF 数据文件）加 `--keep-crlf`。注意：`--cmd-file` 与 stdin 此前是靠 Python 文本模式的隐式转换"碰巧"归一（代码里没写、也关不掉），本版改为显式、可计数、可关闭；**`upload`/`download` 是数据通道，任何情况下都不改字节**（Windows 文件传上去仍是 CRLF，要在远端用需自行 `dos2unix`/`sed`，或用 `--cmd-file` 送脚本而非上传）
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

### ⚠️ 远端 stdin 恒为 EOF：`--cmd 'python3 -'` 配本地 pipe 不会把脚本送进去（v2.5.1）

**本工具最隐蔽的坑，外部 AI 实踩过**。`exec` 启动后**立即关闭远端 stdin**（否则 `cat` 这类命令会
一直等到静默超时、被误判成挂死）。于是 shell 里"靠 stdin 喂脚本"的写法全部失效：

```bash
# ❌ 静默空跑：rc=0、stdout/stderr 全空，看起来像成功
echo 'print(1)' | python3 pyaissh.py exec root@1.2.3.4 --cmd "python3 -"
cat x.py | python3 pyaissh.py exec root@1.2.3.4 --cmd "bash -s"

# ❌ 把 heredoc 挂在 pyaissh 命令行本地（本地 shell 吃掉；pyaissh 的 stdin 不会转发到远端）
python3 pyaissh.py exec root@1.2.3.4 --cmd "python3 -" << 'EOF'
print(1)
EOF
```

`python3 -` 读到空输入 → 解析空程序 → **exit 0、无任何输出**。v2.5.1 起这种命令形态会在
`warnings[]` 里点明（"远端 stdin 恒为 EOF……exit 0 也可能是空跑"），不再静默。

**能用的三种写法**：

```bash
# ✅ 1. heredoc 写进命令文本（远端 shell 自己供 stdin，实测正常）
python3 pyaissh.py exec root@1.2.3.4 --cmd-file - <<'EOF'
python3 - <<'PY'
print("HELLO_FROM_HEREDOC")
PY
EOF

# ✅ 2. 本地已有脚本文件：--script（v2.5.0，整文件 SFTP 传过去再执行，零转义）
python3 pyaissh.py exec root@1.2.3.4 --script ./deploy.sh

# ✅ 3. 先 cat 写文件再执行（heredoc 里 $/反引号安全）
python3 pyaissh.py exec root@1.2.3.4 --cmd-file - <<'EOF'
cat > /tmp/x.sh <<'REMOTE'
echo "$HOME"
REMOTE
bash /tmp/x.sh
EOF
```

判据故意做得很保守：命令形如 `python3 -` / `bash -s` / 裸 `cat` 且整条命令里**没有 `<`** 才告警——
heredoc（`<<`）、here-string（`<<<`）、输入重定向（`< file`）都算"命令自己供 stdin"，不报；
`python3 -c '...'`（程序在参数里）也不报。`--sudo` 的 `sudo -S` 读的是 pyaissh 注入的密码，不受影响。

### `--script <本地脚本>`：本地脚本直达远端执行（v2.5.0）

**解决什么**：本地已经写好一个 `.sh`（长脚本、heredoc、引号嵌套），此前只有两条路——`--cmd '...'` 内联（PowerShell 吃 `$`、引号定界走钢丝）或 `--cmd-file -`（还得 base64/printf 搬过来、且整段脚本会回显进 JSON 的 `cmd` 字段）。`--script` 把整文件经 **SFTP** 落到远端 `/tmp/.pyaissh-script-<随机>.sh`（0600）再 `bash <path>` 执行，**用完自动删**。

```bash
python3 pyaissh.py exec root@1.2.3.4 --script ./deploy.sh        # 本地脚本 → 远端执行
python3 pyaissh.py exec root@1.2.3.4 --script ./deploy.sh --sudo # 提权执行（sudo bash <path>）
python3 pyaissh.py exec root@1.2.3.4 --script ./deploy.sh --pty  # 需要 TTY 时
```

- **互斥**：与 `--cmd`/`--cmd-file` 三选一（同时给报 `bad_args`）；**暂不支持 `--detach`**——后台作业活得比本连接久，临时脚本没人删。长脚本后台化：先 `upload` 到固定路径，再 `exec --detach --cmd 'bash /root/x.sh'`，或用 `session run --cmd-file`
- **`cmd` 回显只有一行** `bash /tmp/.pyaissh-script-xxxx.sh`——**整段脚本不进 JSON**（不会再撑爆调用方上下文；凭据脱敏的责任随之缩小，但脚本里的密码仍会被同一个启发式扫到并 WARN）
- **新字段 `script`（恒有键，非 `--script` 时为 `null`）**：`local`（本地路径）/ `remote`（远端临时路径，执行后已删）/ `bytes`（字节数）/ `sha256`（上传内容的 sha256，可对账）
- **CRLF 归一同 `--cmd-file`**：Windows 工具写出的脚本默认归一为 LF 并回传 `crlf_normalized`；`--keep-crlf` 可关。BOM 自动剥（记事本/VS Code 产物首行不会被拼进 `﻿`）
- **清理**：成功/失败/超时/异常路径都在 `finally` 里删远端临时文件（best-effort：连接已断时只 WARN，可照 `script.remote` 手动 `rm`）
- **执行体是 `bash`**：脚本不需要可执行位，也不依赖 shebang（要 `python3 x.py` 就在脚本里自己调，或 `--cmd "python3 ..."`）

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
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --require-success          # 本地退出码=作业结果（自动化判活）
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --cleanup                  # 清理远端作业目录
```
- **`--require-success`（v2.5.2）：让本地退出码承载远端作业结果**。默认语义是"**日志读到了**"就 `return 0`——`ok:true` 与本地退出码都只说明读取成功，跟作业成败无关；自动化里 `if ($LASTEXITCODE -eq 0)` 因此会把**失败的作业当成功**（外部使用者真踩过）。加上本参数后：已结束且 `exit_code=0` → `0`；已结束但失败 → **远程退出码**（`255` 映射为 `254`，与 `exec` 同规则）；仍在运行或 `dead`（退出码不可知）→ `1`。**退出码口径**：工具自身的错误仍用既有码（`bad_args`=2、连接失败=255 等，不新增码），**远端作业退出码则原样透传**——所以本地退出码**不是**那个固定集合 `{0,1,2,124,130,254,255}`；判成败一律以 JSON 的 `status`/`exit_code`/`exit_success` 为准，"未完成"与"失败"靠 `status`/`next_action` 区分。不加本参数时行为逐字节不变
- **别再重复收同一份日志（v2.5.2 提示）**：不给 `--offset` 时是**尾部 N 行**（默认 100），连续读同一作业会拿到**同样一段**（发布作业收两遍、`pnpm`/`git diff` 整段重复——外部使用者实踩）。正解是用 `--offset <next_offset>` 增量读（不重复不遗漏）。finished 状态的 `next_action` 现在会主动提醒这一点
- **"理想摘要"配方**（任务 ID / 状态 / 退出码 / 最新几行 / 日志位置，一屏搞定，不必展开原文）：
```bash
# ✅ 增量读：用 --offset（推进游标，不重复不遗漏）；--field 只决定打印什么，不决定读多少
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --offset <next_offset> \
    --field job_id,status,exit_code,exit_success,log,next_offset,has_more,stdout
# ✅ 只看最新几行：用 --lines（尾部读，返回的 next_offset 即 EOF）
python3 pyaissh.py log root@1.2.3.4 --job-id <id> --lines 20
```
  ⚠️ **`--offset` 与 `--lines` 互斥**（二选一；同用连接前就 `bad_args` rc=2）。
  ⚠️ **按字节读，不按行对齐**：`--offset` 增量读是字节级精确（不漏不重，`next_offset` 按实际返回字节推进），但**一行可能被切在两次返回之间**——第一次拿到前半行、第二次拿到后半行。按字节拼接是正确的；按行处理的一方要自己缓存行尾残片。这是并发写场景下的正常行为，不是漏读。
  ⚠️ **别"先只取元数据、再用它返回的新 offset 取正文"**：`--field` 不控制读取——
  第一条调用已经把那段日志读走，接着用它返回的 `next_offset` 会**跳过刚才那段**（只能 `--offset 0` 重读补救）。
  要元数据就别在那一条里推进游标：要么按上面第一条把 `stdout` 一起取走，要么改用 `--lines` 尾部读。
  ⚠️ **`--offset` 大于当前文件大小 = 日志被截断/清理/替换过**（v2.5.5 起会告警，不再静默）：本次从文件末尾读、载荷可能为空，`next_offset` 回落到 `log_bytes`。**别把它当成「没有新输出」**——重新 `--offset 0` 读，或对比 `log_bytes` 是否比上次小。没有这条告警时，"文件换了"和"没新内容"在输出上完全一样。
- **机制**：作业脚本落远端 `/tmp/pyaissh-jobs/<job_id>/`（`job.sh` = 你的命令原文、`run.sh` = 运行器），`setsid nohup` 启动 → **SSH 断开、宿主单次调用超时都不影响作业**；输出合并进 `job.log`，结束时退出码写入 `job.rc`，进程组 leader pid 写入 `job.pid`
- **状态机（v2.2.1 三级）**：`finished`（`job.rc` 存在 = 退出码）/ **`dead`**（无 rc 且 `job.pid` 存活探测判定进程已消失——被 kill / OOM / 崩溃，退出码不可知）/ `running`。被 kill 的作业**不再永远 running**，`--wait-rc` 与轮询都能收敛；`dead` 时结果带 `hint` 说明
- **`log` 载荷字段是 `stdout`**（v2.2.1 起，与 `exec` 对齐；它是 `2>&1` **合并流**，`stream:"stdout+stderr"` 声明）：`pyaissh log h --job-id <id> --field stdout` 直接取裸内容
- **停掉作业**：`log --job-id <id> --kill` → 读 `job.pid` 对**进程组** `TERM`（setsid 后 pid==pgid）→ 宽限 5s → `KILL`；Linux 上先校验 `/proc/<pid>/cmdline` 确属本作业（防 pid 复用误杀，不匹配即 `kill_failed` 拒绝）。可与 `--wait-rc` 连用（kill 后立刻收敛为 `dead`）。**整组杀是必须的**：只杀 `run.sh` 会让 `job.sh` 的子进程被 reparent 成孤儿继续跑
- **清理守卫（v2.2.2）**：`--cleanup` 只在作业已结束（finished/dead）时执行——作业还在跑时**拒绝**并报 `job_running`（退出码 2），因为删掉 `job.pid`/`job.log` 会让工具**彻底失去追踪**，而远端进程仍在跑（`--list` 也归零）。收尾推荐 `--kill --cleanup`（一步到位：先整组停掉，再清理）；确要放弃追踪用 `--cleanup --force`（照删但留痕：`forced_cleanup:true` + warnings 提示进程可能仍在跑）
- **已经 `--force` 了怎么把进程停掉（v2.2.3）**：`job.pid` 已删，`--kill` 用不了——结果会直接给你那条命令 `group_kill`：`kill -9 -<pid>`。**负号 = 进程组**，而 `pid` 正是 `setsid` 的组长（`pid==pgid`），所以一条命令连子进程一起清，**不用先 pgrep**（实测：不带负号只杀组长，`job.sh` 的子进程会被 reparent 成孤儿继续跑）。下次遇到长任务直接用 `--kill --cleanup`，不要走到 force 这一步
- **落盘权限（v2.2.1 从严）**：目录 0700、`job.sh`/`job.log`/`job.rc`/`job.pid` 0600、`run.sh` 0700（`run.sh` 首行 `umask 077` 管住 shell 创建的文件）——命令原文与完整输出都在盘上，同机其他用户不可读；`exec --detach` 结果里 `permissions` 字段声明这套权限
- **为什么值得用**：`exec` 前台受**宿主单次调用时长**限制（约 600s，pyaissh 自身可到 1200）；4-5 分钟的更新监控在前台只能结束时一次看到，而 `--detach` + `log --offset` 让"正在发生什么"变成可轮询的小 payload
- **尾部截断的坑**：`--lines N` 模式下若回传内容又超 `--max-output`，中段会被省略（`truncated:true` + `omitted_bytes`），而此时 `has_more=false`、`next_offset` 指向 EOF——**看起来像"读完了"**。v2.2.1 起这种情况 `next_action` 会明确提示"用 `--offset 0` 顺序读补齐"
- **与前台 exec 的差异**：`--detach` 与 `--sudo`/`--pty` 互斥（bad_args）；命令**原文会落盘到远端 `job.sh`**（别写明文凭据；敏感命令用完 `--cleanup`）；`--max-output`/`--encoding` 同样作用于 `log` 的内容回传
- **短作业**：启动后 0.3s 内已结束的作业直接返回 `status:"finished"` + `exit_code`（无需再 log）；启动即死（如命令不可执行）会报 `detach_failed` 并附日志尾巴

- **`--sudo` 提权（普通用户登录时）**：`--sudo --cmd "apt update"` 自动 `sudo -S -p ''` 提权。密码来源：`--sudo-password`（空串视为未设置）> `PYAISSH_SUDO_PASSWORD` env；密码只经 SSH stdin 注入（写完即 close），**命令文本/cmd 字段/日志/远端磁盘均无密码**，`-p ''` 压掉提示符（成功路径 stderr 不含 `password for`）。**组装规则**：简单命令（无 shell 元字符）直连 `sudo -S -p '' <cmd>`——sudoers NOPASSWD 按命令匹配仍生效（`NOPASSWD: /usr/bin/apt` 对 `sudo apt update` 有效）；复合命令（`&&`/`||`/`;`/管道/重定向/`$()`/反引号/换行）→ `sudo -S -p '' bash -c '<单引号转义>'` 整链提权（`&&` 第二段同样 root）。**无密码时自动 `sudo -n` 免密探测**：免密命令直接跑；需密码立即失败不挂，且 stderr 命中 sudo 报错特征（如 `a password is required`）时 warnings 附密码配置提示；**有密码但密码错**（stderr 命中 sudo 专属报错如 `sudo: 1 incorrect password attempt`）时 warnings 提示检查密码——两种提示都只认 sudo 自己的报错（带 `sudo:` 前缀），命令自身 stderr 里的 "Sorry, try again"/"password" 等词不会误触发。`--sudo` 与 `--pty` 互斥（bad_args 退出 2）。`--cmd` 与 `--cmd-file` 两条路径统一处理。**注意**：sudo 无法执行 shell 内建命令（`--sudo --cmd 'exit 3'` 会得到误导性的 "a password is required"，因为 exit 不是可执行文件）——内建命令请外套 `bash -c`（`--sudo --cmd "bash -c 'exit 3'"`）
- 远程退出码直接透传（255 例外：远程恰为 255 时本地返 254，见 SKILL.md 退出码表）
