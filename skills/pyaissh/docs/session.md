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

## 会话会残留吗？（生命周期与清理，v2.3.0 加固）

**结论：会话不会自己退出，用完必须 `session kill`。** 它是 `setsid nohup` 起的远端常驻进程——
"SSH 断开照跑"正是它的设计目的，因此**没有任何 idle 超时 / TTL / 自动回收**。

一个 PTY 会话在远端占 **3 个进程** + 一个目录：

| 进程 | 作用 | pid 记在 |
|---|---|---|
| `bash -c "exec 9<>FIFO; script …"` | starter（会话组长） | `sess.pid` |
| `script -qfc '…' out.log` | PTY 包装 | — |
| `bash -i` | 交互 shell（`cd`/`export` 状态在它里面） | `bash.pid` |

目录 `/tmp/pyaissh-sessions/<name>/`（0700）：`in`(FIFO)、`out.log`、`err.log`、`sess.pid`、
`bash.pid`、`meta`、`last.token`。非 PTY 降级模式是 2 个进程。
`out.log` **只追加、没有轮转**——长期挂着的会话会一直占 `/tmp` 磁盘。

结束会话只有三条路：

1. **`session kill`**（或 `session kill --all` 清该主机全部）：按进程树闭包 TERM → 校验 → KILL，
   默认**连目录一起删**（要留日志看现场用 `--keep-dir`）。结果里 `swept`=扫到的进程数、
   `remaining`=幸存者、`verified`=是否确认"会话进程已不在"、`cleaned`=目录是否已删。
2. **会话里的 shell 自己退出**（`exit` / `Ctrl-D`，或命令把 shell 弄崩）：进程消失，
   但 **`/tmp` 下的目录与日志仍在**——下次 `session kill --name` 会把目录收掉，否则要等机器重启。
3. **远端重启**：进程与 `/tmp` 一起清掉。

**`kill` 不会误报"清干净"**（v2.3.0 加固）：清理前先看三个候选根（`sess.pid` / `bash.pid` /
会话 shell 的父进程 `script`），每个根都要**自证**（`ps -o args=` 里含本会话目录，防止陈旧 pid
被无关进程复用时误杀）。为什么需要 `bash.pid`/`script` 兜底：starter 若被 OOM 或外力杀掉，
`script` 与 `bash -i` 会被 reparent 到 1 号进程，只按 `sess.pid` 算闭包会得空集。
三个根都不可用时，pyaissh **不猜也不杀**：返回 `roots: 0`、`verified: false` + 一条 warning
（附 `ps -eo pid,ppid,tty,args | grep -E 'script -qfc|pyaissh-sessions'` 自查命令），
而不是宣称已清理。看到 `verified: false` 就按 note 手工确认一次。

**怎么发现"忘了关"的会话**：`session list` 给出 `age_seconds`/`started_at`/`log_bytes`；
挂了超过 24 小时的会话会额外给一条 warning（提示不再需要时 kill）。

```bash
python3 pyaissh.py session list root@1.2.3.4                     # 看有几个、挂了多久、多大
python3 pyaissh.py session kill root@1.2.3.4 --name work         # 结束（进程树 + 目录）
python3 pyaissh.py session kill root@1.2.3.4 --name work --keep-dir   # 只杀进程、留日志
python3 pyaissh.py session kill root@1.2.3.4 --all               # 一次清掉该主机全部会话
```

**本地侧永远是干净的**：每次 `pyaissh …` 都是短命客户端，不会因为远端有会话而占本地资源，
也不会阻塞你继续用 `exec`——残留只在远端（几个进程 + `/tmp` 文件）。
