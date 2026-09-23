# session —— 常驻会话（真 PTY）：逐条喂命令 / 状态保留 / 可中断

> 这是 pyaissh skill 的**子文档**（`docs/` 下按需读取，不随 SKILL.md 自动注入）。
> **何时读**：要做**多步、需要状态**的远端操作（部署、排障、交互式安装、逐条试错），
> 或者需要**中断正在执行的命令**时。

## 一句话定位

`exec` 是"一条命令一次调用、无状态"；`exec --detach` 是"一条长命令丢后台、启动后改不了"；
**`session` 是"远端一个常驻 shell，AI 一条一条喂命令"**——每条命令有独立退出码，
`cd`/`export`/函数等状态跨命令保留，跑错了改下一条继续，执行中的命令**可以中断**。

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

1. **`{ ...; }; echo 哨兵` 包裹命令**：退出码哨兵必须与命令**在同一行被 shell 解析**。
   早期把哨兵单列一行紧跟在命令后，命令里若有从终端读取的语句（`read -p`、`passwd`），
   它会**吃掉**那一行当输入（实测：`read` 拿到的值就是哨兵文本，真哨兵永不出现）。
   用 `{}` 而非 `()`：大括号是**同一个 shell**，`cd`/`export` 状态照常保留。
2. **会话身份 = starter 的进程树闭包**，不是 sid。实测 `script` 会给子 shell **另起一个会话**
   （starter sid ≠ pty 会话 sid），按 sid 清理 ⇒ 目录删了、会话进程还活着（留孤儿）。
   所以 `kill` 先算一次闭包（父进程被杀后子进程会 reparent，事后算会漏），再 TERM、只对幸存者 KILL。
3. **`ctrl-c` 只发 SIGINT 不够**：会话树由 `setsid nohup` 起，SIGINT 处置可能被继承为忽略
   （实测 `kill -INT <sleep pid>` 返回成功但进程没死）→ 默认 **0.7s 后升级 SIGTERM**，
   要更狠用 `--force`（SIGKILL）。被中断命令的退出码通常是 130/143。
4. **往 FIFO 写 `0x03` 想靠 pty 行规程转 SIGINT 实测无效**（`sleep` 没死）——中断一律走信号路径，
   不依赖终端行规程。

## 边界与注意

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
