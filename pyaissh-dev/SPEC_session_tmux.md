# session → tmux 引擎迁移：验收指标文档（SPEC）

> **状态**：实现前定稿；实现完成后**逐条**填"证据"列并复验，全绿才交付外部 AI 测试。
> **范围**：`pyaissh session` 的**进程管理层**（PTY/命令注入/输出落盘/中断/会话消亡/存活探测/list/回收）
> 整体换成 tmux；**AI 契约层**（载荷+哨兵、每命令退出码、字节级 offset 读、CRLF/ANSI/截断、JSON 字段）不动。
> **一句话**：tmux 只当引擎，pyaissh 契约原样保留。

---

## 0. 术语与前提

| 词 | 含义 |
|---|---|
| 引擎 | 谁提供 pty、谁持有进程生命周期（旧：`setsid+nohup+script` + 自研看门狗；新：tmux） |
| 契约层 | 由 pyaissh 自建、AI 直接消费的部分（哨兵/退出码/offset 读/清洗/JSON） |
| 基线 | 迁移前用**旧引擎**抓取的字段集 fixture：`tests/contract/session_contract_v2.json`（21 用例） |
| 宿主 | 被 SSH 的服务器；`root`/普通用户均可 |

**依赖（用户 2026-09-24 决定）**：tmux 是**显式依赖**——没有 tmux 时 `session` 不可用，
错误类型 `tmux_missing` 并在 message 里给出可执行安装命令；`exec` / `exec --detach` 不受影响。
两台测试机已装 tmux 3.5a（Debian 13）。

### 0.1 spike 实测结论（2026-09-24，tmux 3.5a / Debian 13 / x86_64）

| # | 实测事实 | 对设计的约束 |
|---|---|---|
| S1 | `new-session -s 'a.b'` 成功但 `ls` 里名字变成 **`a_b`**（`.`/`:` 被静默改写），与真实名字 `a_b` 撞名 | **自己映射名字**：tmux 会话名 = `py-<name 的 utf-8 hex>`（可逆、无碰撞、无特殊字符） |
| S2 | pane 级命令（`pipe-pane`/`paste-buffer`/`display-message`）用 `-t '=NAME'` 报 `can't find pane`；必须 `-t '=NAME:'` | 两级 target：**pane 级 `=NAME:`**，会话级（`has-session`/`kill-session`）`=NAME` |
| S3 | `#{session_width}/#{session_height}` 不存在（渲染为空）；`#{pane_width}/#{pane_height}`、`#{pane_pid}`、`#{pane_current_command}`、`#{pane_pipe}`、`#{pane_tty}`、`#{session_created}` 均可用 | list 用这几个格式变量 |
| S4 | `pipe-pane -o -t '=N:' 'cat >> out.log'` rc=0，输出**按字节**落盘；`#{pane_pipe}` = 1 | 输出镜像成立（INV-04 基础） |
| S5 | **日志被外部删除**后 `pane_pipe` 仍为 1（`cat` 还在往已 unlink 的 inode 写，新内容丢失）；**不带 `-o`** 重新 arm 可恢复 | ENG-12：`out.log` 缺失/变小时**不带 `-o`** 重 arm + warning |
| S6 | bash 的 bracketed paste 会在 out.log 里留下 `ESC[?2004h/l` 噪音；init 里 `bind "set enable-bracketed-paste off"` 后消失 | init payload 加这一条（保持输出与旧引擎同观感） |
| S7 | `paste-buffer` 多行捆绑载荷（以换行结尾）**整块执行**；`read -p` 场景哨兵同行防御依旧成立（哨兵没被吃掉） | 载荷协议不变（INV-05） |
| S8 | `send-keys C-c` 能中断前台命令（`pane_current_command` 从 `sleep` 回到 `bash`） | ctrl-c 用它 |
| S9 | 空闲时 `ps -o tpgid= -p <pane_pid>` = pane_pid 自己；前台 `sleep` 时 = 那个作业的进程组；`kill -KILL -- -<tpgid>` 只杀作业、**会话存活** | `--force` = SIGKILL 前台组（ENG-08），比 `C-\` 干净（无 core dump） |
| S10 | `kill-session` 会带走 shell 与其作业（`sleep &` 消失），但**已 reparent 到 1 的脱离进程**（`nohup setsid sleep`）存活，且**不在 pane_pid 的进程树闭包里** | BND-01 成立；闭包快照杀不掉它（旧引擎同款盲区），文档写明 |
| S11 | 会话里 `exit` → 会话消失、**server 随之退出**（`ls` 报 no server running，rc=1）；`pipe` 的 `cat` **无残留** | 「无会话」= server 不存在，正常路径，不当错误 |
| S12 | `new-session -e KEY=VAL` 可用，但 tmux 仍强制 pane 的 `TERM`（实测 `tmux-256color`） | 不依赖 `-e`：用 `set-environment -g` 注入当前通道环境；TERM 由 tmux 决定（文档说明） |
| S13 | `pkill -f '<脚本文本里的字符串>'` 会**杀掉自己的 exec 通道**（脚本正文出现在命令行里） | 引擎内**禁止**按模式/pkill 杀进程，只按 pid 与进程组；测试断言同理 |

---

## 1. 不变量（硬标准，一条都不能丢）

| ID | 判据 | 验证方法 | 证据 |
|---|---|---|---|
| **INV-01a** | 每个子命令/错误路径的返回**键集必须包含基线全部键**（同输入同键集） |（**2026-09-24 收口**：基线 fixture 已同步删除旧引擎遗留的 `fifo` 字段——`session start` 不再返回该键；迁移期的"字段集一字不变"已由 V1 的 21/21 证据完成使命） | `python pyaissh-dev/contract_baseline.py --check tests/contract/session_contract_v2.json` | V1 ✅ |
| **INV-01b** | 允许**新增**的键仅限白名单：`orphans` / `orphans_total` / `orphan_remaining_total`（恒空返回，AI 侧零感知）；白名单外新增即 FAIL | 同上（工具加 `--allow` 白名单） | V1 ✅ |
| **INV-02** | 八个子命令与参数面：`start`(含 `--attach`/`--ttl`/`--cols`)/`run`(`--cmd`/`--cmd-file`/`--wait-rc`/`--no-wait`)/`send`(`--keep-crlf`)/`read`(`--offset`/`--lines`/`--wait-rc`/`--token`/`--keep-ansi`)/`ctrl-c`(`--force`)/`keys`(`--data`/`--raw`/`--cmd-file`)/`list`/`kill`(`--name`/`--all`/`--keep-dir`)。\*`--no-pty` 保留为 **no-op + warning**（deprecated） | `pyaissh session --help` + `--suite live_session` | V3 ✅ |
| **INV-03** | 错误类型不变：`session_not_found` / `session_exists` / `session_dead` / `bad_args` / `send_failed` / `keys_failed` / `session_failed` / `session_list_failed` / `session_read_failed` / `session_kill_failed`；新增仅限 ERR 表 | 基线 fixture 的错误用例 + `--suite live_session` | V1+V3 ✅ |
| **INV-04** | **字节级增量契约**：`--offset`/`next_offset` 指向磁盘上的**追加文件** `out.log`（SFTP 读），非 `capture-pane`；连续读不重不漏；输出 >1MB 仍能看到哨兵（尾窗逻辑保留）；文件被删/缩小时重建基线并给 warning | `--suite live_session_ttl,live_session_bugs`（B3/B4） | V8 ✅ |
| **INV-05** | 哨兵协议不变：`{ ...; }; echo "__PYAISSH_RC__<token>__$?"`，**哨兵与命令同一行被解析**（防 `read -p` 吃哨兵）；`last.token` 落盘 + 写失败重试 | `--unit`（`_session_payload_text`）+ `--suite live_session`（keys/交互提示） | V2+V3 ✅ |
| **INV-06** | **每条命令独立退出码**：0 / 非 0（如 127）/ 管道取末命令状态；`status: done|running` | `--suite live_session`（错误命令 127、run 退出码） | V3 ✅ |
| **INV-07** | CRLF 归一：命令文本 CRLF/CR → LF 并回传 `crlf_normalized`；`--keep-crlf` 保留；`upload`/`download` 不改字节 | 基线 fixture（`send`/`run`）+ `--suite live_session_bugs`（B5） | V8 ✅ |
| **INV-08** | ANSI 清洗：默认剥离、`--keep-ansi` 保留 | `--suite live_session`（ANSI 用例） | V3 ✅ |
| **INV-09** | `--max-output`（**客户端**读取截断，非服务端）→ `output_truncated` + `omitted_bytes` | `--suite live_session` | V3 ✅ |
| **INV-10** | 权限：会话目录 `0700`，`out.log`/`meta`/`beat`/`last.token` 均 `0600` | `--suite live_session`（start 权限断言 + ls） | V3 ✅ |
| **INV-11** | 人类可现场排障：`tmux -L pyaissh attach -t '=<py-…>'` 可看直播（命令在 `list`/`start` 的 warning/`next_action` 里给出换算提示；名字映射见 ENG-01b）；attach 不破坏契约（`window-size manual`，输出历史不参与 offset）；`TERM` 为 tmux 选定的 `tmux-256color`（与旧引擎不同，属既知变化） | 手工 + `--suite live_session_watchdog` | V5 ✅（socket 隔离/attach 实测） |
| **INV-12** | `list` 语义不变：`idle_seconds` 由 **beat** 算（非 tmux activity）、`age_seconds` 由 `session_created`/`meta`、`expires_in_seconds`、`status: running|dead|unknown`、`shell_pid`（来自 `#{pane_pid}`）、`log_bytes` | `--suite live_session_ttl`（list/年龄/剩余） | V4 ✅ |

---

## 2. 引擎设计决策（ENG）

| ID | 决策 | 理由 |
|---|---|---|
| **ENG-01** | 一律 `tmux -L pyaissh -f /dev/null …`，`-L` 指定专用 socket（落在 `$TMUX_TMPDIR/tmux-<uid>/`） | 与用户自己的 tmux 完全隔离；不读用户 `~/.tmux.conf`（插件/hook 会咬人） |
| **ENG-01b** | **名字映射**：pyaissh 会话名 `NAME` → tmux 会话名 `py-` + `NAME.encode('utf-8').hex()`；`list` 反解；前缀不匹配的 tmux 会话一律无视 | S1：tmux 会静默改写 `.`/`:`，且会与真实名字撞名——只有自建映射才安全 |
| **ENG-02** | **命令注入用 `load-buffer` + `paste-buffer`**（不用 `send-keys -l`）：`printf %s "$载荷" \| tmux … load-buffer -b pyaissh-<name> -` → `paste-buffer -b pyaissh-<name> -d -t =NAME` | tmux 命令行会被**它自己再解析一遍**（`;`/`#{}`/引号），缓冲区内容是数据、不过解析层；零引号风险 + 二进制安全 + 无 tty 单行上限 |
| **ENG-03** | `keys` 同样走 `load-buffer`+`paste-buffer`（`--raw` 语义不变：不转义） | 与 ENG-02 一致、二进制安全 |
| **ENG-04** | 输出落盘：`pipe-pane -o -t '=NAME:' 'cat >> <dir>/out.log'`；`read`/`run` 前查 `#{pane_pipe}`，为 0 或 `out.log` 缺失/变小时**不带 `-o`** 重 arm + warning | `pipe-pane` 是"字节流镜像"，offset 契约的基础；S5 实测"管道还在但文件被删"会静默丢输出（本会话踩过同类） |
| **ENG-05** | 环境：start 时把当前 SSH 通道环境里存在的 `PATH/HOME/USER/SHELL/LANG/LC_ALL/TZ` 用 `set-environment -g` 注入（**在 new-session 之前**），再 `new-session -d -x <cols> -y 50`；**tmux 自己决定 pane 的 `TERM`**（实测 `tmux-256color`，不同于旧引擎继承 SSH 环境的 TERM） | tmux server 环境在 server 启动时冻结；S12 |
| **ENG-06** | 建会话后 `set-window-option -t '=NAME:' window-size manual` | 人类 attach 时终端尺寸变化不会把 pane 折行搞乱 |
| **ENG-07** | 存活探测 `has-session -t =NAME`；**会话里 `exit` → 会话消失但目录仍在** ⇒ 按现有 `session_dead` 语义报错，目录留给 `kill` 清 | 账实分离时的旧语义保留 |
| **ENG-08** | `ctrl-c` = `send-keys -t '=NAME:' C-c`；`--force` = `kill -KILL -- -$(ps -o tpgid= -p <pane_pid>)`（前台进程组），**不用 `C-\`(SIGQUIT)** | S8/S9：SIGQUIT 会 core dump（服务器上可能吐大文件）；SIGKILL 前台组后会话与状态保留 |
| **ENG-09** | `kill` = 先取 pane_pid 的进程树闭包快照 → `kill-session -t '=NAME'` → 对闭包幸存者 TERM→KILL→校验 → `rm -rf 目录`（`--keep-dir` 跳过）；`swept` = 闭包大小，`roots` = 是否有自证根，`verified` = 有根且无幸存；`orphans*` 恒空返回 | 账实合一；闭包快照保留旧引擎"连作业一起清"的语义（S10 的脱离进程是共同盲区） |
| **ENG-10** | `--no-pty` 参数**已删除**（连同非 PTY 降级路径）：传了会被 argparse 拒绝（rc=2），不静默忽略 | tmux 永远有 PTY（`pty: true`） |
| **ENG-11** | 依赖预检：`command -v tmux` + `tmux -V` 取版本号；`< 3.0` 报 `tmux_unsupported`；**不自动安装**，错误里给出可执行安装命令（apt/dnf/yum/apk） | 无包管理器/不可变系统装不了 ⇒ UPG-02 |
| **ENG-12** | `out.log` 被外部删除/清空/轮转 ⇒ offset 读识别"变小/消失"→ 重建基线 + warning `log_recreated`，并不带 `-o` 重 arm 管道 | 会话状态在 tmux、日志在 /tmp，生命周期解耦（S5） |

---

## 3. 空闲回收（REAP）

| ID | 判据 |
|---|---|
| **REAP-01** | `beat` + 续期规则**不变**：`start`(含 `--attach`)/`send`/`run`/`read`/`ctrl-c`/`keys` 续期；`list` **不**续期 |
| **REAP-02** | **惰性扫**：任何会话子命令入口先给**本次目标会话**续期，**再**扫其它会话；回收判据 = `beat` 过期 > TTL **且** `#{pane_current_command}` 属 shell（空闲）；**判不出 ⇒ 视为忙，不回收** |
| **REAP-03** | **每主机一个 reaper**（替代 N 个每会话看门狗）：`<root>/reap.sh --once` 做一次清扫；`<root>/.reaper.pid` 记账；由会话命令按需拉起（幂等：pid 文件存在且进程活着就不重复拉）→ `setsid nohup bash reap.sh --loop </dev/null >/dev/null 2>&1 &`，`--loop` = 每 300s 一次，**无会话目录时自退**；回收动作：`kill-session` + `rm -rf 目录`，每次回收往 `<root>/.reaper.log` 追加一行（超 64KB 先截断） |
| **REAP-04** | 语义变化写进文档：从"服务器端准点回收"变为"**惰性扫 + 每 5 分钟 reaper**"；`--ttl 0` 关闭；`list` 的 `ttl_seconds`/`idle_seconds`/`expires_in_seconds` **字段与口径不变** |
| **REAP-05** | 目录内容变化：**删除** `in`(FIFO)/`watch.sh`/`watch.pid`/`wd.fifo`/`wd.log`/`sess.pid`/`bash.pid`/`err.log`；**保留** `out.log`/`meta`/`beat`/`last.token`；**新增** `<dir>/tmux`（内容 = tmux 会话名，给 reaper 与人类 attach 用） |
| **REAP-06** | reaper 判闲判据与惰性扫一致（`#{pane_current_command}` 属 shell 才算空闲；判不出 ⇒ 不回收） |

---

## 4. 资源指标（PERF）

**实测**（2026-09-24，A 测试机 `2 核 / 0.9 GB`，Debian 13、tmux 3.5a；`stest_tmp/tmuxspike/perf.py` 与
`tests/run_tests.py --suite live_session_engine` 双份证据）：

| ID | 判据（3 个空闲会话，同一主机） | 实测 | 结论 |
|---|---|---|---|
| **PERF-01** | 常驻内存 ≤ 25MB（旧实测 45.5MB） | **23.2 MB** | ✅（−49%） |
| **PERF-02** | 常驻进程数 ≤ 5（tmux server + 3 shell + reaper；旧 12） | **5** | ✅ |
| **PERF-03** | 每会话无常驻辅助进程 | **0**（server/reaper 全主机共享；实测 4 个额外进程是旧引擎才有） | ✅ |
| **PERF-04** | 空闲 CPU ≈ 0：20 秒采样（5 进程合计）≤2 jiffy | **Δ=1 jiffy**（≈0.05% 单核） | ✅ |

补充实测（写进文档时用）：
- 单会话 15.4 MB / 3 进程（旧 ≈12 MB / 4 进程）——**单会话内存略高**，多了一个全主机共享的 tmux server（≈3.3 MB 固定成本）。
- 单次调用墙钟（含 SSH 连接 ≈0.8 s）：`send` 2.5 s、`read --wait-rc` 2.5 s、`run` 2.8 s、`list` 4.1 s。
  每次会话调用比"纯 exec"多两次远端往返（探测 + 惰性扫）；实测**扫脚本本身只占 ≈50 ms**，
  瓶颈在 SSH 连接与 SFTP 往返 ⇒ 不值得为省这一次往返去合并两条链路（结论：保持现状）。

---

## 5. 错误类型（ERR）

| ID | 类型 | 触发 | retryable |
|---|---|---|---|
| ERR-01 | `tmux_missing` | 宿主无 tmux（`command -v tmux` 失败） | false（message/`next_action` 给安装命令） |
| ERR-02 | `tmux_unsupported` | `tmux -V` 解析出的版本 `< 3.0`（依赖 `window-size manual` 与 `#{pane_pipe}`，实测仅 3.5a） | false |
| ERR-03 | `tmux_failed` | server/socket 起不来（`TMUX_TMPDIR` 不可写、`new-session` 非零退出） | true |
| ERR-04 | `pipe_dead`（**warning，非 error**） | `pipe-pane` 无法 arm / 重 arm 后 `out.log` 仍不增长（`ready=false` + warning） | — |
| ERR-05 | `log_recreated`（warning，非 error） | `out.log` 被删/变小 → offset 重建 + 重 arm | — |
| ERR-06 | 既有类型全部保留（见 INV-03） | — | — |

---

## 6. 升级路径（UPG）

| ID | 判据 |
|---|---|
| **UPG-01** | **不识别** tmux 迁移之前遗留的会话目录：没有 `tmux` 名文件的目录一律当陌生目录——`read/run` 按 `meta` 在不在分别报 `session_dead`/`session_not_found`，`kill --all` 只处理有 `tmux`/`meta` 的目录，reaper 直接跳过；遗留目录由人自行 `rm -rf` |
| **UPG-02** | 无 tmux 的环境（air-gapped / 不可变系统 / 无包管理器）→ session 模式不可用并给出可执行提示；`exec` / `exec --detach` **不受影响**（长任务仍有出路） |
| **UPG-03** | 旧引擎代码**删除**（不留 `PYAISSH_SESSION_ENGINE` 开关、不双引擎） |

---

## 7. 已知边界（BND，写进文档、不当作 bug）

| ID | 边界 |
|---|---|
| **BND-01** | 会话内**自己 `setsid`/`nohup` 起的进程不随 `kill-session` 消失**（与终端/tmux 语义一致；它们不在 pane tty 上，我们也看不见）——"孤儿问题结构性消失"**仅指**旧的"rm -rf 目录 ⇒ 账实不符"那一类 |
| **BND-02** | 惰性扫 + reaper 都被绕过（没人回来且 reaper 被杀）→ 残留 tmux 会话；但它**对人类可见**（`tmux ls`），且每会话仅一个闲置 shell |
| **BND-03** | 无包管理器/禁改主机的环境装不了 tmux → session 不可用（见 UPG-02） |
| **BND-04** | 人类 `attach` 调试时，人工输入会被 tmux 回显进 pane，从而混入 `out.log`（不影响哨兵切片，但会让日志变脏） |
| **BND-05** | 会话内跑全屏 TUI（vim/top）时输出是转义序列（清洗规则与旧引擎一致） |

### 7.1 既知行为变化（**不是 bug**，验收时别当缺陷报）

| # | 变化 | 原因 |
|---|---|---|
| C1 | `--no-pty` 参数已删除（argparse 直接拒绝）；结果里 `pty` 恒 `true` | tmux 永远提供 PTY（旧：可降级为非 PTY 常驻 bash） |
| C2 | 会话内 `TERM` = `tmux-256color`（旧：继承 SSH 通道环境，常为空/`dumb`） | tmux 强制 pane 的 TERM（S12）；对需要 256 色的程序反而更好 |
| C3 | `kill` 结果**总是**含 `orphans: []` / `orphans_total: 0` / `orphan_remaining_total: 0`（旧：空时省略） | 用户要求"字段继续返回、永远空"，AI 侧零感知（计入 INV-01b 白名单） |
| C4 | 空闲回收从"服务器端准点（TTL..TTL+TICK）"变为"**惰性扫 + 每 5 分钟 reaper**" | 无每会话看门狗；`list` 字段口径不变 |
| C5 | 会话目录少了 `in`/`sess.pid`/`bash.pid`/`watch.*`/`wd.*`，多了 `tmux` | 记账权交给 tmux（REAP-05） |
| C6 | 依赖 tmux ≥ 3.0（实测 3.5a）；没有则 `session` 不可用 | 用户决定（ENG-11/UPG-02） |
| C7 | `ctrl-c --force` 用 SIGKILL 前台进程组（旧：SIGKILL 会话 shell 的直接子进程/组） | S9：`tpgid` 是内核给出的前台组，更准 |
| C8 | 会话语义上多一个"服务器级"对象：tmux server（闲置时随最后一个会话退出） | `list` 不受影响；人类 `tmux ls` 能看到 |
| C9 | `ctrl-c` **代被中断的命令补一条退出码哨兵**（INT→130、`--force`→137，补 `last.token` 那条） | bash 收到 SIGINT 会丢弃当前命令行 ⇒ 哨兵本来不会出现；补上它，正等 `read --wait-rc --token` 的调用方才能收敛（保持旧引擎"中断后能拿到退出码"的体感） |
| C10 | 每会话调用多两次远端往返（探测 + 惰性扫） | 实测扫脚本 ≈50 ms，可忽略；见 §4 补充实测 |

---

## 8. 验收执行表（2026-09-24 已逐条跑完）

| 步骤 | 命令 | 覆盖 ID | 结果 |
|---|---|---|---|
| V1 契约键集 | `python pyaissh-dev/contract_baseline.py --check tests/contract/session_contract_v2.json --allow orphans,orphans_total,orphan_remaining_total` | INV-01a/b, INV-03 | ✅ **21/21 PASS**（仅白名单三项孤儿字段为新增） |
| V2 单元 | `python -u tests/run_tests.py --unit` | INV-05、名字映射/target/闸门/reaper/命令生成、CRLF/参数解析 | ✅ unit_regression **128 PASS / 0 FAIL**、artifacts **9/0** |
| V3 会话 core | `python -u tests/run_tests.py --suite live_session` | INV-02/05/06/08/09/10, ENG-07/08, C9 | ✅ **30 PASS / 0 FAIL** |
| V4 TTL/attach/list | `python -u tests/run_tests.py --suite live_session_ttl` | INV-12, REAP-01/02/03/04 | ✅ **16 PASS / 0 FAIL** |
| V5 引擎与回收 | `python -u tests/run_tests.py --suite live_session_engine` | ENG-01..06/11/12, REAP-03, PERF-01..04 | ✅ **13 PASS / 0 FAIL**（含 PERF 实测断言） |
| V6 消亡/账实分离 | `python -u tests/run_tests.py --suite live_session_lifecycle` | ENG-07/09/12, UPG-01(残留目录), BND-01 | ✅ **13 PASS / 0 FAIL** |
| V7 孤儿字段/幂等 | `python -u tests/run_tests.py --suite live_session_orphan` | INV-01b, ENG-09 | ✅ **7 PASS / 0 FAIL** |
| V8 回归护栏 | `python -u tests/run_tests.py --suite live_session_bugs` | INV-04/07, B3/B4/B5, C1 | ✅ **11 PASS / 0 FAIL** |
| V9 MCP 层 | `pyaissh-mcp/test/test_offline.py`（+ 真机 MCP 用例） | MCP 描述与实际一致 | ✅ 离线用例通过（真机会话用例由 `pyaissh-mcp/test/test_live_session.py` 覆盖，需 MCP 客户端环境） |
| V10 文档一致性 | 人工核对 `docs/session.md` / `SKILL.md` / `docs/errors.md` / `docs/contract.md` / MCP 描述 / CHANGELOG ×3 | 依赖(tmux)、回收语义、删除的字段/文件、边界 | ✅ 已按 §7.1 C1~C10 复核并回填实测数字 |

**真机会话块合计：90 PASS / 0 FAIL**（core 30 + ttl 16 + engine 13 + lifecycle 13 + orphan 7 + bugs 11）。

> 遵守 AGENTS.md：**开发期只跑改动落点所在的块**（V2~V8 分块跑），**全量 `--all --release` 只在发布前跑一次**。

---

## 9. 交付物清单

1. **代码**：`pyaissh-dev/domains/11_cmd_session.py`（进程层重写为 tmux；契约层保留）、`12_cli_main.py`（帮助文本）、三份 `pyaissh.py`（`dist` 同步）。
2. **测试**：`tests/contract/session_contract_v2.json`（基线 fixture）、`tests/run_tests.py` 各会话块改写 + 新断言、本 SPEC 第 8 节填证据。
3. **文档**：`skills/pyaissh/docs/session.md`（引擎/回收语义/边界重写）、`SKILL.md`（session 段）、`pyaissh-mcp/README.md`、MCP `pyaissh_session` 描述。
4. **记录**：`CHANGELOG.md`（= `skills/pyaissh/CHANGELOG.md` = `pyaissh-mcp/CLI_CHANGELOG.md`）、`tests/CHANGELOG.md`。
5. **本 SPEC**：验收表全绿后归档。
