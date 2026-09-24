# session —— 常驻会话（真 PTY）：逐条喂命令 / 状态保留 / 可中断

> 这是 pyaissh skill 的**子文档**（`docs/` 下按需读取，不随 SKILL.md 自动注入）。
> **何时读**：要做**多步、需要状态**的远端操作（部署、排障、交互式安装、逐条试错），
> 或者需要**中断正在执行的命令**时。

## 一句话定位

`exec` 是"一条命令一次调用、无状态"；`exec --detach` 是"一条长命令丢后台、启动后改不了"；
**`session` 是"远端一个常驻 shell，AI 一条一条喂命令"**——每条命令有独立退出码，
`cd`/`export`/函数等状态跨命令保留——**打错了就把那条命令改对再发一遍**（同一条重试，像人在终端里那样：文件名打错 → 报错 → 改对 → 重发 → 成功），执行中的命令**可以中断**。

| 场景 | 该用谁 |
|---|---|
| 单条命令、无状态依赖 | `exec` |
| 一条长命令（装包/编译/备份），中途不需要干预 | `exec --detach` + `log` |
| **多步任务、步骤间有状态、要边看边决定下一步** | **`session`** |
| 交互式程序（要应答 y/n、要 tty） | `session` + `session keys` |
| 想中止一条跑歪的命令而不丢会话 | `session ctrl-c` |

## 典型流程

```bash
pyaissh session start h --name work                      # 起会话：返回 pid/pty/log/ready
pyaissh session run   h --name work --cmd 'cd /opt/app'   # 一步一次调用：跑一条并等结果
pyaissh session run   h --name work --cmd 'git pull'      # 状态保留：cwd 仍在 /opt/app
pyaissh session run   h --name work --cmd 'make -j8' --wait-rc 5   # 5s 没完 → status=running
pyaissh session ctrl-c h --name work                      # 中断 make（会话活着，cwd 还在）
pyaissh session run   h --name work --cmd 'make -j4'      # 换成正确命令继续，不用重来
pyaissh session keys  h --name work --data 'y\n'          # 应答程序提示（y/n、密码…）
pyaissh session kill  h --name work                       # 收尾（进程树全清 + 删目录）
```

> 需要"边跑边看"时用 `send` + `read --offset`（增量读）；只用 `run` 是"跑完给结果"。
> 两者可混用：`session run --no-wait` 等价 `send`。

## 子命令

| 子命令 | 作用 | 关键字段/参数 |
|---|---|---|
| `start <target> [--name main]` | 起会话（`setsid`+`nohup`+util-linux `script` 给真 PTY）| 返回 `pid`/`pty`/`ready`/`log`/`fifo`/[`permissions`](contract.md)；`--cols`（默认 200，防折行）、`--no-pty`、`--wait-ready` |
| **`run <target> --name S --cmd '…'`** | **跑一条并等它结束（推荐；=`send`+等待合成一次调用）** | 返回与 `read` 同构：`stdout`/`exit_code`/`status`(`done`\|`running`)/`token`/`next_offset`；`--wait-rc N`（默认 60，上限 600）超时回 `running`，`--no-wait` 只发送 |
| `send <target> --name S (--cmd '…' \| --cmd-file -)` | 只喂命令不等（需要边跑边增量读时用）| 返回 `token`/`offset`/`next_offset`；命令文本 CRLF 默认归一（`--keep-crlf` 保留）|
| `read <target> --name S [--offset N] [--lines N] [--wait-rc SECS] [--token T]` | 读输出：尾部 / 增量 / 等某条命令结束 | 载荷字段 `stdout`（合并流，已清洗 CR/ANSI/哨兵行）、`next_offset`、`status`(`done`\|`running`)、`exit_code`、`token`；`--keep-ansi` 保留颜色码 |
| `ctrl-c <target> --name S [--force]` | **中断正在执行的命令**（对它的进程组发 SIGINT；`--force` → SIGKILL）| `signaled_groups`/`signaled_children`/`signaled_count`/`escalated_to_term`；会话**不死**，状态保留 |
| `keys <target> --name S --data 'y\n'` | 注入按键/文本（应答提示、Ctrl-D）| `--data` 支持 `\n \r \t \xNN \\`；`--raw` 原样；`--cmd-file` 送原始字节 |
| `list <target>` | 列该主机会话（存活/pty/日志大小/最后活动）| `sessions[]`：`session`/`pid`/`status`/`pty`/`log_bytes`/`mtime` |
| `kill <target> (--name S \| --all) [--keep-dir]` | 结束会话：**进程树闭包** TERM→KILL→校验，默认连目录一起删 | `swept`（扫到几个进程）/`remaining`（残留）/`cleaned` |

## 为什么这样实现（都是实测踩出来的）

1. **哨兵必须与命令在同一"解析单元"里**（否则被 `read` 吃掉）——两种模式两种形态：
   - **PTY 模式**：`{ ...; }; echo 哨兵`。命令与哨兵在同一行被 bash 解析；用 `{}` 而非 `()`，
     大括号是**同一个 shell**，`cd`/`export` 状态照常保留。注意 tty 规范模式单行上限 ~4096B，
     所以不要把超长命令压成一行（它天然是多行）。
   - **非 PTY 降级模式**：`eval "$(printf %s '<base64>' | base64 -d)"; echo 哨兵`。
     降级模式的读取循环是**逐行 eval**，多行载荷会被拆开（`{` 单独一行直接 syntax error、
     哨兵永不出现——实测 `--no-pty` 完全不可用）；base64 保证是**一行**，且 eval 在同一 shell
     执行 ⇒ 状态保留、多行命令、长命令都不受限。
2. **会话身份 = starter 的进程树闭包**，不是 sid。实测 `script` 会给子 shell **另起一个会话**
   （starter sid ≠ pty 会话 sid），按 sid 清理 ⇒ 目录删了、会话进程还活着（留孤儿）。
   所以 `kill` 先算一次闭包（父进程被杀后子进程会 reparent，事后算会漏），再 TERM、只对幸存者 KILL。
3. **`ctrl-c` 只发 SIGINT 不够**：会话树由 `setsid nohup` 起，SIGINT 处置可能被继承为忽略
   （实测 `kill -INT <sleep pid>` 返回成功但进程没死）→ 默认 **0.7s 后升级 SIGTERM**，
   要更狠用 `--force`（SIGKILL）。被中断命令的退出码通常是 130/143。
4. **往 FIFO 写 `0x03` 想靠 pty 行规程转 SIGINT 实测无效**（`sleep` 没死）——中断一律走信号路径，
   不依赖终端行规程。
5. **轮询必须刷新 SFTP 看门狗**（v2.3.0）：`open_sftp` 的看门狗线程只看 `_pyaissh_last_activity`，
   会话/`log` 的轮询循环若几十秒不刷新就会被判"静默断链"强杀——实测 `session run sleep 35`
   在 30.7s 处断链（结果整条丢失）、`log --wait-rc 50` 同理（v2.2.0 起潜伏）。
   现在每轮 `stat`/读之前都调 `_sftp_touch_activity()`，`--wait-rc` 上限 600 才真正可用。
6. **哨兵轮询看"末尾窗口"而不是"从 offset 起 1MB"**（v2.3.0）：哨兵总在文件最末，
   从 offset 起读一旦命令输出 >1MB 就永远看不到哨兵（实测 `seq 1 300000` 卡在 ~1MB、只看到
   144960 行）。现在每轮只 `stat`，文件长长了才读**新增那一段**并维护末尾 1MB 输出窗口——
   既保证看得到哨兵，也不在窄带宽链路上每 0.25s 重下整个窗口。

## 边界与注意

- **`read --lines N`**（v2.3.0 起真正生效）：尾部读默认只回 **最后 100 行**（`SESSION_DEFAULT_LINES`），
  传 `--lines N` 回最后 N 行；结果里有 `lines_requested`/`lines_returned` 可核对。
  `--wait-rc` 模式下 `--lines` 同样作用于该命令的输出。**不带 `--lines` 也不会再回传上万行**。
- **怎么判断"这条失败了"**（重试循环靠它）：① 先看 `exit_code`（0 = 成功）——注意它遵循 **shell/POSIX
  语义**：管道取**最后一个命令**的状态（`cat 打错的文件 | wc -c` 仍会是 0，因为 `wc` 成功了）；
  ② 再看输出文本：会话把 **stderr 合并进 `stdout`**，所以 `cat: ...: No such file or directory`
  这类报错**就在 `stdout` 里**——判断"名字打错了"最直接的就是它；③ 命令若在等输入（例如变量名打错
  导致 `cat` 无参去读 stdin），`run` 会等到 `--wait-rc` 超时并回 `status:"running"`，此时用
  `session ctrl-c` 打断再改对重发。
- **依赖**：`bash` + `mkfifo` 是必需的；真 PTY 需要 util-linux 的 `script`（`command -v script`）。
  缺失时自动降级为**非 PTY** 常驻 shell：状态与退出码照常，但没有 tty（需要 TTY 的程序不可用），
  结果里会带 warning 且 `pty: false`。
- **没有 tty 的程序**：`vim`/`top` 这类全屏 TUI 在 PTY 会话里能跑、也能注入按键，但它的**输出**是
  终端重绘序列，读起来很乱；"看 TUI"建议用一次性 `exec --pty --pty-strip-ansi`。
- **未闭合的引号**：命令若有未闭合引号，shell 会等续行（PS2），此时哨兵可能被当续行吃掉——
  用 `session ctrl-c` 打断，或重新 `send` 一条正确的。
- **并发发送**：`send` 会串行写入 FIFO；建议"发一条 → 读一条"（用 `send` 返回的 `token`/`offset`）。
- **安全**：会话目录 0700、`in`/`out.log`/`meta`/`sess.pid`/`bash.pid`/`last.token` 均 0600；
  会话里敲过的命令**会留在远端 `out.log`**（含凭据的命令请用完 `session kill`）。
- **状态在远端**：SSH 断开、本地关机都不影响会话；但**远端重启**会丢（和 `--detach` 作业一样）。

## 会话会残留吗？（生命周期与清理，v2.3.0）

**结论：会话是远端常驻进程（`setsid nohup`，"SSH 断开照跑"正是它的设计目的），默认带 10 分钟空闲回收兜底，
用完也可以随时 `session kill` 立刻结束。**

一个 PTY 会话在远端占 **3 个进程**（+ 空闲回收开启时 1 个看门狗 bash，它每轮还有一个 `sleep` 子进程）+ 一个目录：

| 进程 | 作用 | pid 记在 |
|---|---|---|
| `bash -c "exec 9<>FIFO; script …"` | starter（会话组长） | `sess.pid` |
| `script -qfc '…' out.log` | PTY 包装 | — |
| `bash -i` | 交互 shell（`cd`/`export` 状态在它里面） | `bash.pid` |
| `bash watch.sh` | **空闲回收看门狗**（`--ttl 0` 时没有；每轮 `sleep 15` 唤醒，RSS ≈ 3 MB） | `watch.pid` |

目录 `/tmp/pyaissh-sessions/<name>/`（0700）：`in`(FIFO)、`out.log`、`err.log`、`sess.pid`、
`bash.pid`、`watch.pid`、`watch.sh`、`beat`（最后交互时间）、`meta`、`last.token`。非 PTY 降级模式是 2 个进程。
`out.log` **只追加、没有轮转**。

### 空闲回收（idle TTL，默认 600 秒）

"没人用的会话"不该白占远端资源——`start` 时会在会话目录里放一个**独立的看门狗进程**，
**两个条件同时成立**才回收：

1. **提示符空闲**：`pgrep -P <会话 shell>` 为空（没有前台命令在跑）⇒ **构建/安装/长任务不会被误杀**；
2. **距上次交互超过 TTL**：`beat` 文件的 mtime 由每次 pyaissh 交互刷新。

**什么算交互（会续期）**：`start`（含 `--attach`）、`send`、`run`、`read`、`ctrl-c`、`keys`；
**`list` 不算**（看一眼不代表在用）。有命令在跑时看门狗每轮都续期，命令跑完后再从那一刻起算 TTL。
实测（绝对时间戳，逐个命令核对 beat 是否前进）：`read / run / send / keys / ctrl-c / start --attach`
**都续期**，`list` **不续期**（连续两次 `list` beat 都不动）。

**计时口径（重要）**：TTL 是从**最后一次"续期交互"**算起的滑动窗口，不是从会话创建/首次使用算起。
每次交互都把到期时间往后推 TTL：

```
t0   start                     ← beat=t0，到期 t0+TTL
t1   run 'make'                ← beat=t1，到期 t1+TTL（重新计时）
t2   （什么都不做，TTL 内）      ← 仍存活
t3   read/run/send/...         ← beat=t3，到期 t3+TTL（再次重新计时）
t4   干完活，最后一条命令       ← 到期 = t4+TTL；之后没有任何交互才会被回收
```

看门狗每 15 秒检查一次 ⇒ **实际回收落在"最后一次续期 + TTL" 到 "+15 秒"之间**。
`list` 给出的 `expires_in_seconds = TTL - idle_seconds`；它显示 **0 只表示"下一个检查点就会被收"**，
不是立刻消失（实测：`idle=22s / ttl=20s` 时仍是 `running`，下一次 tick 才回收）。

**超过 TTL 才回来的后果**：会话已被回收 ⇒ `run/send/read` 报 `session_not_found`（提示"可能已被空闲回收"），
`start --attach` 会当成不存在而**新建**（`attached: false`，cwd/变量丢失）；`--ttl 0` 可彻底关掉回收。

- 取值：`--ttl 600`（默认）／`--ttl 30s`／`--ttl 10m`／`--ttl 2h`／`--ttl 0`（关闭回收）；
  环境变量 `PYAISSH_SESSION_TTL` 改默认值。检查周期 15 秒 ⇒ 实际回收落在 `TTL ~ TTL+15s`。
- 回收动作与 `kill` 同款：**自证进程树闭包**（argv 含本会话目录才认，防 pid 回收误杀）→ 先删目录 → TERM → KILL；
  看门狗自己随后退出，**不留痕迹**（目录、日志、`out.log` 一起没）。
- `list` 给出 `ttl_seconds`/`idle_seconds`/`expires_in_seconds`，剩余不足 2 分钟会额外提醒一次。
- **保活**：`--ttl 0` 关闭回收（那就必须记得 `kill`），或者在 TTL 内发一条无害命令。
  **代价**：超过 TTL 没交互 = 会话被回收，**cwd/变量一起丢**（下次 `start` 是新会话）。

```bash
python3 pyaissh.py session start root@1.2.3.4 --name work --ttl 10m   # 10 分钟空闲就自动收
python3 pyaissh.py session list  root@1.2.3.4                         # 看 idle_seconds / expires_in_seconds
```

### 接了又断、断了又接：`--attach`

```bash
python3 pyaissh.py session start root@1.2.3.4 --name work --attach    # 活着→接上；没有/被回收→新建
```

- 活着时返回 `attached: true` + `pid`/`age_seconds`/`idle_seconds`，**状态（cwd/变量）全保留**；
- 不存在（包括刚被空闲回收）时正常新建，返回 `attached: false`；
- **不带 `--attach`** 时同名撞车仍报 `session_exists`（安全考虑：不会静默接上另一个 agent 的同名会话），
  但提示已改成"**直接继续用它**：`session run/send/read --name X`，要重开先 `kill`"。

### 其它结束方式

1. **`session kill`**（或 `session kill --all` 清该主机全部）：按进程树闭包 TERM → 校验 → KILL，
   默认**连目录一起删**（要留日志看现场用 `--keep-dir`）。结果里 `swept`=扫到的进程数、
   `remaining`=幸存者、`roots`=自证的根数、`verified`=是否确认"会话进程已不在"、`cleaned`=目录是否已删。
2. **会话里的 shell 自己退出**（`exit` / `Ctrl-D`，或命令把 shell 弄崩）：进程消失，
   但 **`/tmp` 下的目录与日志仍在**——下次 `session kill --name` 会把目录收掉，否则要等机器重启。
3. **远端重启**：进程与 `/tmp` 一起清掉。

**`kill` 不会误报"清干净"**（v2.3.0 加固）：清理前先看四个候选根（`sess.pid` / `bash.pid` / `watch.pid` /
会话 shell 的父进程 `script`），每个根都要**自证**（`ps -o args=` 里含本会话目录，防止陈旧 pid
被无关进程复用时误杀）。为什么需要这些兜底：starter 若被 OOM 或外力杀掉，`script` 与 `bash -i`
会被 reparent 到 1 号进程，只按 `sess.pid` 算闭包会得空集。所有根都不可用时，pyaissh **不猜也不杀**：
返回 `roots: 0`、`verified: false` + 一条 warning（附 `ps -eo pid,ppid,tty,args | grep -E 'script -qfc|pyaissh-sessions'`
自查命令），而不是宣称已清理。看到 `verified: false` 就按 note 手工确认一次。

**怎么发现"忘了关"的会话**：`session list` 给出 `age_seconds`/`started_at`/`log_bytes`/`idle_seconds`；
挂了超过 24 小时的会话会额外给一条 warning。

```bash
python3 pyaissh.py session list root@1.2.3.4                     # 看有几个、挂了多久、多大、还剩多久
python3 pyaissh.py session kill root@1.2.3.4 --name work         # 立刻结束（进程树 + 目录）
python3 pyaissh.py session kill root@1.2.3.4 --name work --keep-dir   # 只杀进程、留日志
python3 pyaissh.py session kill root@1.2.3.4 --all               # 一次清掉该主机全部会话（含 argv 扫到的孤儿）
```

**已知边角**：如果会话目录被**手工 `rm -rf`** 掉，它的进程就失去了 pid 记录 —— 逐目录枚举看不见它们，
所以 `session kill --all`（或 `--name X`）会**额外按 argv 扫一遍孤儿**（见下），把这类残留收掉。

### 孤儿兜底：`kill` 按 argv 自证身份扫一遍

`session kill --all`（以及 `--name X`）在正常清理之后，会跑一次 `ps -eo pid=,args=`，挑出
**目录已不在、但命令行里还带着会话路径**的会话进程并清掉（整棵进程树 TERM→KILL→校验，
结果在 `orphans[]` 里，带 `via: "argv-scan"`/`kind`/`swept`/`remaining`/`verified`）。

**只认"以会话身份出现"的进程**，避免误杀只是"提到路径"的东西（`tail -f …/out.log`、编辑器、
备份脚本、以及 argv 里恰好带路径的旁观进程）。判定要同时满足：

1. argv 里出现 `<会话根>/<名字>/`，且名字合法（防路径穿越）；
2. argv 里含 `script -qfc`（PTY 包装）／`<会话根>/<名字>/in`（starter 的 FIFO 路径）／
   `<会话根>/<名字>/watch.sh`（看门狗）之一；
3. 该名字的目录**不在**（目录还在 ⇒ 走正常 kill 路径，不在这里重复处理）。

真机用例里专门放了一个"argv 里带会话路径的旁观进程"当诱饵，断言它**不被杀**。

**仍然找不到的**：如果 starter 与 `script` 都死了、只剩一个 `bash -i`，它的 argv 只有 `bash -i`
（没有任何路径）⇒ 无法自证身份，工具**不会**碰它（宁可留下也不误杀）。这种残留只能靠
`ps -eo pid,tty,args | grep bash` 人工判断，或远端重启。

**本地侧永远是干净的**：每次 `pyaissh …` 都是短命客户端，不会因为远端有会话而占本地资源，
也不会阻塞你继续用 `exec`——残留只在远端（几个进程 + `/tmp` 文件）。

### 本地关机 / 断网会怎样（实测）

**会话和"正在跑的命令"都留在远端，什么都不丢**——这正是 `setsid+nohup` 的目的。实测（v2.3.0）：
`send 'cd /etc; sleep 20; echo AFTER_RECONNECT_OK; pwd'` 之后 **26 秒完全不连服务器**（等价本地关机），
期间 `list` 仍报 `running`；连回来后 `read --wait-rc` 直接拿到 `status:done`、`exit_code:0`、
输出里的 `AFTER_RECONNECT_OK` 与 `pwd=/etc`——**命令跑完了、退出码与输出在、`cd` 状态也在**。
所以"本地关机"不是问题：任务照跑，回来接着读；**唯一要记得的是它不会自己收尾**（默认 10 分钟空闲回收，
或 `kill`/`kill --all` 立刻收）。

### 半截写入：本地在"写命令半途"断线（R2，已加固）

pyaissh 是"把整条命令写进 FIFO"的：如果本地正好在写入中途关机/断网，远端 tty 的输入缓冲里会留下
**没有换行的半行**。下一轮写入若直接接上，就会与这半行**串成同一行**——加固前的实测后果是
`bash: syntax error near unexpected token`，且**哨兵永不出现**（`run` 只能回 running、拿不到退出码，
AI 被卡住，只能人工 `ctrl-c`）。

v2.3.0 起载荷**以换行开头**：先把那半行终结掉（它会被当**一条独立命令**执行——可能是半截命令，
报错会留在 `out.log` 里，值得扫一眼），随后真正的命令在干净的输入行里解析，哨兵照常出现。
加固后实测同一个场景：`exit_code: 0`、输出 `PARTIAL_HALF\nSECOND_OK`。

**遇到卡住怎么办**：`session ctrl-c`（清掉未提交的输入行 + 打断前台命令）后重发即可——
实测 `ctrl-c` 之后重发命令 `exit_code: 0`、输出正常。

### MCP 通道的例外：进程退出时自动清理

CLI 路径没有"本地长命进程"可以依附，所以会话只能显式 `kill`。**MCP 路径（`pyaissh_session`）不同**：
`pyaissh-mcp` 是本地长命进程，它**正常退出时会自动清掉自己 `start` 过的会话**——stdin 关闭、
`SIGINT`/`SIGTERM` 都会触发（`SIGKILL`、断电不会）。只清自己起的：别的 agent 或用 CLI 直接起的
会话不受影响；显式 `kill` 过的会同步注销，退出时不重复清。预算
`PYAISSH_MCP_EXIT_CLEANUP_TIMEOUT`（默认 10s，`<=0` 关闭），退出路径 best-effort。
