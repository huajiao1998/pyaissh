# session —— 常驻会话（真 PTY）：逐条喂命令 / 状态保留 / 可中断

> 这是 pyaissh skill 的**子文档**（`docs/` 下按需读取，不随 SKILL.md 自动注入）。
> **何时读**：要做**多步、需要状态**的远端操作（部署、排障、交互式安装、逐条试错），
> 或者需要**中断正在执行的命令**时。
> **v2.4.0 起：进程引擎 = tmux**（旧的自研实现——`setsid`+`nohup`+util-linux `script`+FIFO+
> 自写看门狗——整体删除）；**契约层完全不变**：stdout 单行 JSON 的字段集、哨兵协议、每条命令
> 独立退出码、字节级 offset 读、CRLF/ANSI 清洗、错误类型都不动。`TERM`、回收方式（惰性扫 + reaper）、
> 依赖 tmux、**参数面收紧（`--no-pty` 已删除）**等变化见下文「边界与注意」与「已知边界」。

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

## 依赖：远端需要 tmux ≥ 3.0（v2.4.0 起）

会话的 PTY、进程生命周期与输出镜像都交给远端 tmux；**没有 tmux，`session` 就不可用**。

| 情况 | 行为 |
|---|---|
| 有 tmux 且主版本 ≥ 3 | 正常（实测 tmux 3.5a / Debian 13） |
| 有 tmux 但 < 3.0 | 报 `tmux_unsupported`（引擎依赖 `window-size manual` 与 `#{pane_pipe}`） |
| 没有 tmux | 报 `tmux_missing`，message/`next_action` 里给**可直接执行**的安装命令 |
| `tmux -V` 解析不出来 | 报 `tmux_failed`（server/socket 起不来也归它） |

- 安装命令（错误里原样给出）：Debian/Ubuntu `apt-get install -y tmux`｜RHEL/CentOS
  `dnf install -y tmux`｜Alpine `apk add tmux`。
- **pyaissh 不会自动安装 tmux**（不替用户改远端系统）；**装不了的环境**（不可变系统 / 无包管理器 /
  air-gapped）⇒ session 不可用，长任务改用 `exec` 或 `exec --detach` + `log`
  ——**这两条路不依赖 tmux，功能不受影响**。
- 旧依赖（`bash` + `mkfifo`、util-linux `script`、`pgrep`）**已随引擎删除**，不用再查；引擎侧只用
  `bash`、`ps`（procps-ng）、`awk`、`base64`、`cat`、`date`、`setsid`/`nohup`（拉起每主机 reaper）
  ——主流发行版默认都有。

## 典型流程

```bash
pyaissh session start h --name work                      # 起会话：返回 pid/pty/log/ready
pyaissh session run   h --name work --cmd 'cd /opt/app'   # 一步一次调用：跑一条并等结果
pyaissh session run   h --name work --cmd 'git pull'      # 状态保留：cwd 仍在 /opt/app
pyaissh session run   h --name work --cmd 'make -j8' --wait-rc 5   # 5s 没完 → status=running
pyaissh session ctrl-c h --name work                      # 中断 make（会话活着，cwd 还在）
pyaissh session run   h --name work --cmd 'make -j4'      # 换成正确命令继续，不用重来
pyaissh session keys  h --name work --data 'y\n'          # 应答程序提示（y/n、密码…）
pyaissh session kill  h --name work                       # 收尾（tmux 会话 + 进程树 + 目录）
```

> 需要"边跑边看"时用 `send` + `read --offset`（增量读）；只用 `run` 是"跑完给结果"。
> 两者可混用：`session run --no-wait` 等价 `send`。
> **`ctrl-c` 之后仍可以对那条命令 `--wait-rc`**：被中断的命令自己不会产出哨兵（bash 收到 SIGINT
> 会丢弃当前命令行），所以 `ctrl-c` 会**代它补一条**（SIGINT→`exit_code=130`、`--force`→`137`），
> 正在等它的 `read --wait-rc --token` 会正常收敛。

## 子命令

| 子命令 | 作用 | 关键字段/参数 |
|---|---|---|
| `start <target> [--name main]` | 起会话（远端 tmux，专用 socket `pyaissh`）| 返回 `pid`/`pty`（恒 `true`）/`ready`/`log`/[`permissions`](contract.md)；`--cols`（默认 200，防折行）、`--ttl`、`--attach`、`--wait-ready`；**`--no-pty` 已废弃**（no-op + warning，tmux 永远提供 PTY） |
| **`run <target> --name S --cmd '…'`** | **跑一条并等它结束（推荐；=`send`+等待合成一次调用）** | 返回与 `read` 同构：`stdout`/`exit_code`/`status`(`done`\|`running`)/`token`/`next_offset`；`--wait-rc N`（默认 60，上限 600）超时回 `running`，`--no-wait` 只发送 |
| `send <target> --name S (--cmd '…' \| --cmd-file -)` | 只喂命令不等（需要边跑边增量读时用）| 返回 `token`/`offset`/`next_offset`；命令文本 CRLF 默认归一（`--keep-crlf` 保留）|
| `read <target> --name S [--offset N] [--lines N] [--wait-rc SECS] [--token T]` | 读输出：尾部 / 增量 / 等某条命令结束 | 载荷字段 `stdout`（合并流，已清洗 CR/ANSI/哨兵行）、`next_offset`、`status`(`done`\|`running`)、`exit_code`、`token`；`--keep-ansi` 保留颜色码 |
| `ctrl-c <target> --name S [--force]` | **中断正在执行的命令**：往 pane 注入 Ctrl-C（tty 交给**前台进程组**）；`--force` = 对内核给出的前台进程组（`ps -o tpgid=`）发 SIGKILL | `signal`(`INT`\|`KILL`)/`signaled_groups`/`signaled_children`/`signaled_count`；会话**不死**，状态保留——**但这道命令的退出码哨兵不会出现**（别再 `--wait-rc`） |
| `keys <target> --name S --data 'y\n'` | 向 pane 注入按键/文本（应答提示、Ctrl-D）| 原始字节经 tmux 缓冲区灌入；`--data` 支持 `\n \r \t \xNN \\`；`--raw` 原样；`--cmd-file` 送原始字节 |
| `list <target>` | 列该主机会话（存活/日志大小/最后活动/剩余保活）| `sessions[]`：`session`/`pid`(=`shell_pid`)/`status`(`running`\|`dead`)/`pty`/`log_bytes`/`age_seconds`/`idle_seconds`/`expires_in_seconds`/`current_command`/`tmux_session`；**不续期**，但会顺手做一次惰性回收 |
| `kill <target> (--name S \| --all) [--keep-dir]` | 结束会话：先取 `pane_pid` 的**进程树闭包快照** → `tmux kill-session` → 对幸存者 TERM→KILL→校验 → 删目录 | `swept`（闭包大小）/`remaining`/`roots`（是否拿到权威根 = pane_pid）/`verified`（有根且无幸存）/`cleaned`/`tmux_killed`；`orphans`/`orphans_total`/`orphan_remaining_total` **恒返回且恒空**（见下） |

## 引擎：为什么换成 tmux（v2.4.0）

旧引擎的复杂度几乎全在"**自己实现一个终端**"：

- FIFO 写入的阻塞与半行（写一半断线会串行、`read -p` 会吃掉哨兵）；
- 自写看门狗的判闲与"自杀式清理"、临终带走、防 spin 护栏；
- `kill` 靠 **argv 自证**扫孤儿（starter / `script -qfc` / `watch.sh` 三种形态）；
- pid 文件可能被内核复用 ⇒ 必须"先自证再杀"，否则误杀无关进程。

这些在 tmux 里全是**现成的、被千万台机器验证过的行为**：PTY 与前台进程组由 tty 行规程负责、
进程生命周期归 tmux server、输出镜像有 `pipe-pane`、会话存活有 `has-session`。
换掉之后：

- **契约层一行没变**（字段/哨兵/退出码/offset 读/清洗），AI 侧零感知；
- **每会话不再有常驻辅助进程**（旧：每会话一个看门狗 bash）——只剩每主机**一个** reaper；
- `kill` 的权威根直接取 tmux 给的 `pane_pid`，**不需要自证**（也不会再有 pid 复用误杀）。

## 进程与目录结构（tmux 引擎）

| 对象 | 说明 |
|---|---|
| tmux server | `tmux -L pyaissh -f /dev/null`——**专用 socket**（落在 `$TMUX_TMPDIR/tmux-<uid>/`，默认 `/tmp` 下），与用户自己的 tmux **完全隔离**，也**不读**用户的 `~/.tmux.conf`（插件/hook 会咬人）。server 在最后一个会话退出时随之消失（`ls` 报 no server running，属正常路径，不当错误） |
| tmux 会话名 | `py-` + pyaissh 会话名 **UTF-8 字节的 hex**（`main` → `py-6d61696e`）。为什么不用原名：tmux 会**静默改写**名字里的 `.`/`:`（`a.b` 建出来叫 `a_b`，既与真实的 `a_b` 撞名、又让精确匹配失效）——只有自建映射才可逆、无碰撞 |
| pane | 每会话一个 pane，里面是 `bash -i`（真 PTY）；`new-session -d -x <--cols> -y 50` + `set-window-option window-size manual`（人类 attach 时改窗口大小不会把折行搞乱） |
| 会话目录 | `/tmp/pyaissh-sessions/<name>/`（0700；根目录可用 `--session-dir` 改） |
| reaper | 每主机**一个** `<root>/reap.sh --loop`（见「空闲回收」） |

**会话目录内容**（tmux 引擎真正使用的就这五个）：

| 文件 | 内容 / 作用 | 权限 |
|---|---|---|
| `out.log` | 输出镜像（`pipe-pane 'cat >> out.log'`）——`--offset`/`next_offset` 的字节契约就是它 | 0600 |
| `meta` | `1 <cols> <起始 epoch 秒> <ttl>`（第 1 字段 = pty；`age_seconds`/`ttl_seconds` 从这来） | 0600 |
| `beat` | 最后一次 pyaissh 交互的 **epoch 秒**（`idle_seconds` 与回收判据都按它） | 0600 |
| `last.token` | 最近一次 `send`/`run` 的 token（`read --wait-rc` 不带 `--token` 时用它定位） | 0600 |
| `tmux` | 该会话的 tmux 会话名（reaper 与人类 attach 用） | 0600 |

根目录另有 `reap.sh`（reaper 脚本，0700）、`.reaper.pid`（pid + 间隔指纹）、`.reaper.log`
（回收台账，超 64KB 只留尾 200 行）。**会话全清后根目录只剩 `.reaper.log`**：`reap.sh` 与
`.reaper.pid` 会在 reaper 自退或被 `kill` 停掉时删掉（下次 `start` 会重写），台账留着是为回答
"我的会话什么时候被谁收了"。**旧引擎的 `in`(FIFO)/`sess.pid`/`bash.pid`/`watch.*`/`wd.*`/
`err.log` 都不再产生**——如果看到它们，说明这个目录是 tmux 迁移之前起的；pyaissh 不识别这种目录
（当陌生目录处理），自行 `rm -rf` 即可。

### 人类现场排障：attach 看直播

```bash
tmux -L pyaissh ls                                                  # 列专用 socket 下的会话（py-…）
tmux -L pyaissh attach -t "=$(cat /tmp/pyaissh-sessions/work/tmux)"  # 看直播（值形如 py-6d61696e）
```

- attach 不破坏契约：pane 已 `window-size manual`，**输出历史不参与 offset 读**；
- 退回用 `Ctrl-B d`（detach）——**别在里面 `exit`**（那会真的结束会话）；
- **人工输入会被 tmux 回显进 pane，从而混进 `out.log`**（日志变脏，但不影响哨兵切片）。

## 为什么这样实现（都是实测踩出来的）

1. **名字必须自己映射**：tmux `new-session -s 'a.b'` 成功但 `ls` 里变成 `a_b`（`.`/`:` 被静默改写），
   还会与真实存在的 `a_b` 撞名 ⇒ 用 `py-<utf-8 hex>`（可逆、无碰撞、无 tmux 特殊字符），
   `list` 反解；前缀不匹配的 tmux 会话一律无视。
2. **两级 target**：pane 级命令（`pipe-pane`/`paste-buffer`/`send-keys`/`display-message`）
   必须用 `=NAME:`，只给 `=NAME` 会报 `can't find pane`；会话级（`has-session`/`kill-session`）
   用 `=NAME`。`=` 前缀 = 精确匹配（防前缀误配）。
3. **命令注入走 `load-buffer` + `paste-buffer`**（不用 `send-keys -l`）：tmux 命令行会被**它自己
   再解析一遍**（`;`、`#{}`、引号都是它的语法），而缓冲区内容是**纯数据**、不过解析层——
   零引号风险、二进制安全（`keys --cmd-file` 可喂任意字节），也没有 tty 单行上限。
   用完 `-d` 即删缓冲，不在 server 里堆积。
4. **哨兵必须与命令在同一"解析单元"里**（否则被 `read` 吃掉）：载荷形态不变，仍是
   `{ ...; }; echo 哨兵`——命令与哨兵在同一行被 bash 解析；用 `{}` 而非 `()`，大括号是**同一个
   shell**，`cd`/`export` 状态照常保留。（`--no-pty` 参数与非 PTY 降级模式都已删除。）
5. **输出镜像是"字节流"，但管道会静默死于文件被删**：`pipe-pane` 的 `cat` 若还在往**已 unlink 的
   inode** 写，新输出就丢了（`#{pane_pipe}` 仍是 1，看不出来）。所以 `read`/`run` 前会查
   `#{pane_pipe}` 与 `out.log` 是否存在：管道死了**带 `-o`** 重 arm（已有管道时是 no-op）；
   文件被外部删了则**不带 `-o`** 重 arm（让 `cat >>` 重建文件），并在 `warnings[]` 里给一条提示
（文本含 `log_recreated`，同时打一条 stderr `[WARN]`）——**它不是一个独立的 JSON 字段**。
6. **`ctrl-c` 靠 tty 行规程，不靠自己发信号**：`send-keys C-c` 由 pane 的行规程把 SIGINT 送到
   **前台进程组**——这正是终端里 Ctrl-C 的语义（`pane_current_command` 从 `sleep` 回到 `bash`）。
   `--force` 用内核给出的前台组 `ps -o tpgid= -p <pane_pid>` + `kill -KILL -- -PGID`：只杀那个作业、
   **会话与状态保留**；**不用 `C-\`(SIGQUIT)**——那会 core dump，服务器上可能吐大文件。
7. **轮询必须刷新 SFTP 看门狗**（v2.3.0 起，仍有效）：`open_sftp` 的看门狗线程只看
   `_pyaissh_last_activity`，会话/`log` 的轮询循环若几十秒不刷新就会被判"静默断链"强杀
   （实测 `session run sleep 35` 在 30.7s 处断链、结果整条丢失）。现在每轮 `stat`/读之前都调
   `_sftp_touch_activity()`，`--wait-rc` 上限 600 才真正可用。
8. **哨兵轮询看"末尾窗口"而不是"从 offset 起 1MB"**（v2.3.0 起，仍有效）：哨兵总在文件最末，
   从 offset 起读一旦命令输出 >1MB 就永远看不到哨兵（实测 `seq 1 300000` 卡在 ~1MB）。
   现在每轮只 `stat`，文件长长了才读**新增那一段**并维护末尾 1MB 窗口——既看得到哨兵，
   也不在窄带宽链路上每 0.25s 重下整个窗口。
9. **每主机的 reaper 必须收敛到恰好一个**：`start` 会重写 `reap.sh`（内容/间隔可能变），
   而**旧代的 reaper 进程还在 `sleep`**——它们用旧间隔、还会同时扫同一批会话（实测一次冒烟里
   攒出 4 个）。所以拉起时按"pid 文件 + `ps -o args=` 自证是本 root 的 `reap.sh` + 间隔指纹"
   三重核对，并停掉其余正在跑的同类进程。
10. **引擎内禁止按模式杀进程**：`pkill -f '<脚本文本里的字符串>'` 会**杀掉自己的 exec 通道**
    （脚本正文出现在命令行里）——清理一律**只按 pid 与进程组**。

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
- **会话内的 `TERM` = `tmux-256color`**（tmux 强制决定，与旧引擎"继承 SSH 通道环境"不同，属既知变化）
  ——对需要 256 色的程序反而更准。
- **`--no-pty` 已删除**：参数不复存在，传了会被 argparse 直接拒绝（`rc=2`，不会静默忽略）；
  tmux 永远提供 PTY，结果恒 `pty: true`（旧的"非 PTY 降级常驻 shell"路径整体删除）。
- **被中断的命令由 ctrl-c 代补退出码**：`ctrl-c`（默认 SIGINT 或 `--force` SIGKILL）之后，被中断的
  命令自己不会产出哨兵（`bash` 收到 SIGINT 会丢弃当前命令行），所以 pyaissh **代它补一条**
  （SIGINT→`exit_code=130`、`--force`→`137`）；正等它的 `read --wait-rc --token` 会收敛。
  补的是 `last.token`（最近一条命令）——中断的几乎总是它。
- **没有 tty 的程序**：`vim`/`top` 这类全屏 TUI 在 PTY 会话里能跑、也能注入按键，但它的**输出**是
  终端重绘序列，读起来很乱；"看 TUI"建议用一次性 `exec --pty --pty-strip-ansi`。
- **未闭合的引号**：命令若有未闭合引号，shell 会等续行（PS2），此时哨兵可能被当续行吃掉——
  用 `session ctrl-c` 打断，或重新 `send` 一条正确的。
- **并发发送**：命令经 tmux 缓冲区灌进 pane（`load-buffer`+`paste-buffer`，tmux 侧串行）；
  仍然建议"发一条 → 读一条"（用 `send` 返回的 `token`/`offset`）。
- **安全**：会话目录 0700，`out.log`/`meta`/`beat`/`last.token`/`tmux` 均 0600；会话里敲过的命令
  **会留在远端 `out.log`**（含凭据的命令请用完 `session kill`）。
- **状态在远端**：SSH 断开、本地关机都不影响会话；但**远端重启**会丢（tmux server 与 `/tmp` 一起没）。

## 会话会残留吗？（生命周期与清理，v2.4.0 引擎）

**结论：会话由远端 tmux 常驻（"SSH 断开照跑"正是它的设计目的），默认带 10 分钟空闲回收兜底
（惰性扫 + 每主机 5 分钟一轮的 reaper），用完也可以随时 `session kill` 立刻结束。**

一个会话 = tmux server 里的**一个 pane**（`bash -i`）+ 一个目录；**没有任何"每会话常驻辅助进程"**
（旧引擎每会话挂一个看门狗 bash，那是被删掉的那部分复杂度）。每个主机只有一个 reaper 进程，
且在没有会话目录时自退。

### 空闲回收（惰性扫 + 每主机一个 reaper，新语义）

**回收判据**（与旧引擎一致，两个条件**同时**成立）：

1. **提示符空闲**：tmux 的 `#{pane_current_command}` 属于 shell（`bash`/`sh`/`dash`/`zsh`/`ksh`）
   ⇒ 没有前台命令在跑（**构建/安装/长任务不会被误杀**）；**判不出 ⇒ 视为忙，不回收**（宁可多留）；
2. **距上次交互超过 TTL**：`beat`（epoch 秒）过期。

**两个回收入口**：

- **惰性扫**：会做会话探针的子命令（`run`/`send`/`read`/`ctrl-c`/`keys`）入口**先给本次目标会话
  续期**、**再**扫一遍其它会话；`start` 在 `--ttl > 0` 时收尾也扫一次并顺手拉起 reaper；
  **`list` 不续期但会顺手扫**（`kill` 本身就是清理，不走这条）。所以"回来干活"这件事本身就会
  把过期又空闲的旧会话收掉。
- **每主机一个 reaper**：`<root>/reap.sh --loop`（`setsid nohup` 起，**默认 300 秒一轮**；
  环境变量 `PYAISSH_SESSION_REAP_INTERVAL` 可改，主要给测试用）。它做与惰性扫同一件事，
  **没有任何会话目录时下一轮自退**；`session kill` 把会话清空后会**立刻**把它停掉（不留后台进程）。

**"没人再回来"时**，最迟 **TTL + 5 分钟**被回收（一个 reaper 轮次）。`list` 的
`ttl_seconds`/`idle_seconds`/`expires_in_seconds` **字段与口径不变**。

**续期规则（不变）**：`start`（含 `--attach`）/`send`/`run`/`read`/`ctrl-c`/`keys` 都续期；
**`list` 不算**（看一眼不代表在用）。TTL 是从**最后一次续期交互**算起的**滑动窗口**，
不是从会话创建算起：

```
t0   start                     ← beat=t0，到期 t0+TTL
t1   run 'make'                ← beat=t1，到期 t1+TTL（重新计时）
t2   （什么都不做，TTL 内）      ← 仍存活
t3   read/run/send/...         ← beat=t3，到期 t3+TTL（再次重新计时）
t4   干完活，最后一条命令       ← 到期 = t4+TTL；之后没有任何交互才会被回收
```

**超过 TTL 才回来的后果**：会话已被回收 ⇒ `run/send/read` 报 `session_not_found`（提示"可能已被
空闲回收"），`start --attach` 会当成不存在而**新建**（`attached: false`，cwd/变量丢失）。

**回收动作**：`tmux kill-session` + `rm -rf 目录`，并往 `<root>/.reaper.log` 追加一行
（`reclaim <name> idle=…s ttl=…s at <epoch>`）。过期但 tmux 会话已经不在的残留目录
（在会话里 `exit` 过）也会被顺手收掉。

- 取值：`--ttl 600`（默认）／`--ttl 30s`／`--ttl 10m`／`--ttl 2h`／`--ttl 0`（**关闭回收**）；
  环境变量 `PYAISSH_SESSION_TTL` 改默认值。
- `--ttl 0` 的会话**不参与回收**（`meta` 里 TTL=0，惰性扫与 reaper 都会跳过它），`start --ttl 0`
  也不会拉起 reaper——那就必须记得 `kill`。
- `list` 给出 `ttl_seconds`/`idle_seconds`/`expires_in_seconds`，剩余不足 2 分钟会额外提醒一次。

```bash
python3 pyaissh.py session start root@1.2.3.4 --name work --ttl 10m   # 10 分钟空闲就自动收
python3 pyaissh.py session list  root@1.2.3.4                         # 看 idle_seconds / expires_in_seconds
```

### 成本对比（进程 / 内存）

两列都是**真机实测**（测试机：2 核 / 0.9 GB 内存，Debian 13、tmux 3.5a；v2.3.0 旧引擎与
v2.4.0 tmux 引擎在同一台机器上分别测过）：

| 项目 | 旧引擎（v2.3.0，对照） | tmux 引擎（v2.4.0） |
|---|---|---|
| 3 个空闲会话常驻内存 | ≈45.5 MB | **23.2 MB**（−49%） |
| 3 个空闲会话常驻进程数 | 12 | **5**（1 tmux server + 3 `bash -i` + 1 reaper） |
| 每会话常驻辅助进程 | 1 个看门狗 bash（RSS ≈3.2 MB；早期版本另有 `sleep` 子进程） | **0 个**（server 与 reaper 是全主机共享的） |
| 单个空闲会话 | ≈12 MB / 4 个进程 | 15.4 MB / 3 个进程（tmux server ≈3.3 MB 是**固定成本**，会话越多越划算） |
| 空闲 CPU | 每 15 秒一个检查点（≈0.025% 单核） | **20 秒采样 5 个进程合计 1 jiffy**（≈0.05% 单核；reaper 每 300 秒一轮） |
| 单次会话调用耗时 | — | `send`/`read`(wait-rc) ≈2.5 s、`run` ≈2.8 s、`list` ≈4.1 s（其中 SSH 连接本身 ≈0.8 s；每次调用多一次"探测"与一次"惰性扫"远端往返） |

> 结论：**单会话内存略高**（多了一个全主机共享的 tmux server），**多会话与进程数明显更省**
> ——省掉的正是"每会话一个看门狗、每个看门狗一次判闲扫描"。
> 内存吃紧的小机器上把 `--ttl` 调小（用完自动消失）或用 `exec`（零残留）更划算。

### 接了又断、断了又接：`--attach`

```bash
python3 pyaissh.py session start root@1.2.3.4 --name work --attach    # 活着→接上；没有/被回收→新建
```

- 活着时返回 `attached: true` + `pid`/`age_seconds`/`idle_seconds`，**状态（cwd/变量）全保留**；
- 不存在（包括刚被空闲回收）时正常新建，返回 `attached: false`；
- **不带 `--attach`** 时同名撞车仍报 `session_exists`（安全考虑：不会静默接上另一个 agent 的同名会话），
  但提示已改成"**直接继续用它**：`session run/send/read --name X`，要重开先 `kill`"。

### 其它结束方式

1. **`session kill`**（或 `session kill --all` 清该主机全部）：先取 `pane_pid` 的**进程树闭包快照**
   （必须在 `kill-session` **之前**算——父进程被杀后子进程会被 reparent，事后再算会漏），
   再 `tmux kill-session`，然后对闭包幸存者 **TERM → 校验 → KILL → 再校验**，默认**连目录一起删**
   （要留日志看现场用 `--keep-dir`）。结果里 `swept`=闭包大小、`remaining`=幸存者数、
   `roots`=是否拿到权威根（pane_pid）、`verified`=有根且无幸存、`cleaned`=目录是否已删、
   `tmux_killed`=tmux 会话是否确认关掉。**`--all` 还会把"目录已被外部删掉、只剩 tmux 会话"的
   一并收掉**；会话全清后立刻停掉本主机的 reaper。
2. **会话里的 shell 自己退出**（`exit` / `Ctrl-D`，或被人 `kill-session`）：tmux 会话消失，
   但 **`/tmp` 下的目录与日志仍在**——`list` 会把它显示成 `dead` 并提醒"用 `session kill` 清理残留
   目录"，否则要等空闲回收的 reaper 顺手收掉，或等机器重启。
3. **远端重启**：tmux server 与 `/tmp` 一起清掉。

**`orphans` 三个字段恒返回且恒空**：`kill` 结果**总是**含 `orphans: []` / `orphans_total: 0` /
`orphan_remaining_total: 0`。旧引擎里"目录没了但进程还在"的**结构性孤儿**（`rm -rf` 目录后进程
失去记账）在 tmux 引擎下**不复存在**——进程生命周期归 tmux，账实天然合一；字段保留只为契约稳定，
**AI 侧零感知**（不必再检查它们）。

**`kill` 不会误报"清干净"**：`verified: true` 才表示"拿到过 pane_pid 且闭包无幸存"；
没拿到 pane_pid（会话已先一步消失、被外部 kill）时返回 `roots: 0`、`verified: false` + 一条 note 与
warning（附 `ps -eo pid,ppid,tty,args | grep pyaissh-sessions` 自查命令），而**不是**宣称已清理。
看到 `verified: false` 就按 note 手工确认一次。

**目录里只有 `meta`、没有 tmux 会话**（会话里 `exit` 或被外部 `kill-session` 之后的正常残留）：
`run/send/read` 报 `session_dead`，`list` 显示 `status: dead` 并提示清理；它不会被惰性扫/reaper 收走
（没有 `tmux` 名文件一律跳过），用 `session kill` 清目录后重新 `start`。

**怎么发现"忘了关"的会话**：`session list` 给出 `age_seconds`/`started_at`/`log_bytes`/`idle_seconds`；
挂了超过 24 小时的会话会额外给一条 warning（`out.log` 只增不减）。

```bash
python3 pyaissh.py session list root@1.2.3.4                     # 看有几个、挂了多久、多大、还剩多久
python3 pyaissh.py session kill root@1.2.3.4 --name work         # 立刻结束（tmux 会话 + 进程树 + 目录）
python3 pyaissh.py session kill root@1.2.3.4 --name work --keep-dir   # 只杀进程、留日志
python3 pyaissh.py session kill root@1.2.3.4 --all               # 一次清掉该主机全部会话
```

### 已知边界（照实写，不当作 bug）

- **会话内自己 `setsid`/`nohup` 起的脱离进程不随 `kill` 消失**：它们 reparent 到 1、不在 pane 的
  进程树闭包里，`tmux kill-session` 也带不走——这与终端/tmux 语义一致（**旧引擎同款盲区**），
  要清得自己按 pid 处理。
- **惰性扫与 reaper 都被绕过时**（没人再回来 + reaper 被杀）会残留 tmux 会话：但它**对人类可见**
  （`tmux -L pyaissh ls` / `tmux ls`），且每个残留只是一个闲置 shell。
- **无 tmux 的环境**（无包管理器/不可变系统/air-gapped）⇒ session 不可用，用 `exec` /
  `exec --detach` 跑长任务。
- **人类 attach 调试时**，人工输入会被 tmux 回显进 pane，从而混入 `out.log`（不影响哨兵切片，
  但会让日志变脏）。
- **全屏 TUI**（vim/top）的输出是转义序列（清洗规则与旧引擎一致）。
- **会话里 `exit` 之后目录仍在**：`list` 显示 `dead`，用 `kill` 清（或等 reaper 顺手收）。

### 本地关机 / 断网会怎样

**会话和"正在跑的命令"都留在远端，什么都不丢**——命令跑在远端 tmux pane 里，输出由 `pipe-pane`
写进 `out.log`，本地只是个"按 offset 读文件"的客户端：断连期间没有任何东西随 SSH 一起消失。

行为面与旧引擎实测一致（旧引擎 v2.3.0 的实测：`send 'cd /etc; sleep 20; echo AFTER_RECONNECT_OK;
pwd'` 之后 **26 秒完全不连服务器**，期间 `list` 仍报 `running`；连回来后 `read --wait-rc` 直接拿到
`status:done`/`exit_code:0`/输出里的 `AFTER_RECONNECT_OK` 与 `pwd=/etc`）。所以"本地关机"不是问题：
任务照跑，回来接着读。

**唯一要记得的是它不会自己收尾**（默认 10 分钟空闲回收，或 `kill`/`kill --all` 立刻收）。

### 半截写入：本地在"写命令半途"断线（R2，加固保留）

pyaissh 是"把整条命令灌进会话"的：如果本地正好在写入中途关机/断网，远端 tty 的输入缓冲里会留下
**没有换行的半行**。下一轮写入若直接接上，就会与这半行**串成同一行**——加固前的实测后果是
`bash: syntax error near unexpected token`，且**哨兵永不出现**（`run` 只能回 running、拿不到退出码，
AI 被卡住，只能人工 `ctrl-c`）。

v2.3.0 起载荷**以换行开头**（先把那半行终结掉，它会被当**一条独立命令**执行——可能是半截命令，
报错会留在 `out.log` 里，值得扫一眼），随后真正的命令在干净的输入行里解析，哨兵照常出现。
**这条加固逻辑与旧引擎逐字相同**（tmux 引擎下命令经 `load-buffer`+`paste-buffer` 原样灌进 pane 的
输入流，与旧引擎写 FIFO 的字节流等价），旧引擎实测结论（同一场景 `exit_code: 0`、输出
`PARTIAL_HALF\nSECOND_OK`）照旧成立。

**遇到卡住怎么办**：`session ctrl-c`（清掉未提交的输入行 + 打断前台命令）后重发即可。

### MCP 通道的例外：进程退出时自动清理

CLI 路径没有"本地长命进程"可以依附，所以会话只能显式 `kill`。**MCP 路径（`pyaissh_session`）不同**：
`pyaissh-mcp` 是本地长命进程，它**正常退出时会自动清掉自己 `start` 过的会话**——stdin 关闭、
`SIGINT`/`SIGTERM` 都会触发（`SIGKILL`、断电不会）。只清自己起的：别的 agent 或用 CLI 直接起的
会话不受影响；显式 `kill` 过的会同步注销，退出时不重复清。预算
`PYAISSH_MCP_EXIT_CLEANUP_TIMEOUT`（默认 10s，`<=0` 关闭），退出路径 best-effort。
