# pssh 已知边界（子文档）

> 这是 pssh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文汇集各类边界行为与注意事项。
> **何时读**：PTY/ANSI、host key（AutoAddPolicy/--strict）、连接被服务端拒绝、信号中断行为、Windows/Git Bash 下载与路径、内存/输出边界等异常或特殊场景排查时。

- **`--pty`（基础版）**：exec 加 `--pty` 可分配 PTY 伪终端，支持需要 TTY 的**非交互**命令（`tty`、`watch`、`top -b -n 1`、`sudo -n`、检测 isatty 的脚本）。注意：PTY 模式下 stderr 合并进 stdout（无独立 stderr）；输出带 `\r\n` 会被自动清洗为 `\n`；`--pty-strip-ansi` 可剥离 ANSI 颜色/光标序列供 AI 干净解析；结果 JSON 带 `pty: true` 标志。**全屏交互程序（vi/vim、sudo 密码输入）仍不可用**——AI 编辑远程文件请用 download/upload 或 sed 结构化替换；sudo 密码输入**无法用 stdin 管道**（pssh 执行后立即关闭 stdin，`sudo -S` 不适用），需用 `sudo -n`（免密）或把密码写进远程环境变量
- **非 PTY 输出的 ANSI 风险**：远程命令带色输出（`grep --color`、`ls --color`、安装脚本）在非 PTY 模式下会**原样**进 JSON 的 `stdout` 字段（仅 `--pty` 时才剥离）——做正则/字符串匹配解析前先自行剥离 ANSI，或对这类命令加 `--pty-strip-ansi`
- **远端路径 `~` 已支持自动展开**（v1.3 起，`~` / `~/x` 转为绝对路径并回显在结果里；`~user` 形式不支持）；**通配符始终不支持**（SFTP 无 glob，报错会明确提示先 ls）
- exec 输出默认 `--max-output` 256KB 截断（可调大，见 exec.md）；内存缓冲有上界（约等于 `--max-output`，头尾各半滚动保留），不会因大输出无限吃内存；超大输出（建议 >50MB）仍应分批或改走文件
- 后台进程占用通道时，drain 窗口为 `min(idle_timeout, 10)` 秒，到点强制截断并标记 `output_truncated`（含 ≤数秒收尾，典型总延迟 10~13s）
- 默认 `AutoAddPolicy` 自动接受新 host key；paramiko≥5.0 且 known_hosts 文件存在时会**写回盘**（旧版只加内存不写盘）；**写盘为原子替换（v1.4.8 起：Linux 用 flock 串行化 + 临时文件 + os.replace，Windows 用原子替换）**——多进程并发首次连接同一新主机不丢记录、写盘中断不损坏 known_hosts 文件（原生 save_host_keys 是直接覆写，存在竞态与损坏风险）；**首次连接的新主机（known_hosts 无记录）连接成功后 stderr 会打 `[WARN] 新主机 host key 已隐式接受`**——AI 可据此区分"首次连接"与"主机被劫持/重装"；敏感环境加 `--strict`
- **并发连接上限受服务端限制**：同时 ≥10 条连接（如 10+ 并发 test、8 分片 + 主连接 + 跳板）可能触发 sshd 默认 `MaxStartups 10:30:100` 的概率性拒绝，表现为随机连接报 `ssh_error`（"Error reading SSH protocol banner"，错误消息会附带 MaxStartups 排查提示）——这是服务端配置特性非工具 bug，可降低并发或调大目标机 sshd 的 MaxStartups
- **极早期信号窗口**：信号 handler 已在模块顶层、paramiko 慢 import 之前注册（v1.4.8 起）——进程启动后约 30-50ms 的解释器自身初始化窗口（CPython 尚未执行到注册代码）内收到 SIGTERM/SIGINT 仍会直接终止（rc=143、无 JSON），这是 Python 解释器启动的物理下限；**该窗口之后（含整个 paramiko import 阶段）的信号都会被捕获**：置中断标志，main() 入口输出结构化 `interrupted` JSON + 退出码 130（此前整个 import 阶段都是 rc=143 无 JSON）。调用方的外部超时强杀保护仍建议 >=1s（含进程调度抖动余量）
- `--cmd` 与 `--cmd-file` 同时给出时 `--cmd` 优先，`--cmd-file` 被忽略（**JSON `warnings` 与 stderr 双通道注明**）
- **Windows 下载注意**：远程文件名含 Windows 非法字符（`foo:bar`、`a*b`、`dq"q.txt` 及**控制字符 tab/换行/DEL** 等）时自动替换为 `_` 并在 `warnings` 注明；**两个远端名仅大小写不同或清洗后同名**（NTFS 大小写不敏感）会把后者改名为 `<名>.dup2` 保住两份数据并 WARN；但 **Windows 保留设备名**（`CON`/`NUL`/`COM1`/`LPT1` 等，含 `CON.txt` 变体）与**含反斜杠 `\` 的文件名**无法安全创建，会**跳过该文件**并在 `warnings` 注明
- **Windows Git Bash 下注意**：远程路径参数（`--path`/`--remote`）会被 MSYS 自动转成 Windows 路径，务必用 `./pssh` 包装（已内置 `MSYS_NO_PATHCONV=1`）或加 `MSYS_NO_PATHCONV=1` 前缀；直接用 `python pssh.py` 时远程路径会被改写并打 WARN。经包装器运行时**本地路径**（`--local /tmp/...` 这类 Unix 风格）会自动经 cygpath 转成真实 Windows 路径（Git Bash 的 /tmp ≠ Windows 的 \tmp）
- **远程命令自杀伤（pkill/pkill -f 匹配到自身 cmdline）**：exec 的命令在远端以 `bash -c '…'` 形式执行，**完整命令文本会出现在远端进程的 cmdline 里**。若命令用 `pkill -f "dsh web"` 这类**按自身 cmdline 模式匹配**的方式杀进程，会把自己（承载本 SSH 会话的 bash 包装进程）一起杀掉 → 连接中断、报 `connection_lost`、本次 JSON 结果**不可信**（命令可能已部分执行、也可能根本没跑）。规避三招：①**括号转义**——`pkill -f '[d]sh web'`（方括号使自身 cmdline 不含字面量 `dsh web`，模式匹配不到自己）；②先 `pgrep -f` 列出候选 PID 核对后精确 kill；③把模式写成不含自身命令文本的形态（如按 PID 文件/端口匹配）。`connection_lost` 后按惯例**不信任部分结果重跑**，副作用类命令重试前先 pgrep 确认/清理
