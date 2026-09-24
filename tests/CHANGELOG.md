# tests/CHANGELOG.md — 测试体系更新记录

> 记录：修了哪些问题、加了哪些测试集、后续计划。**只追加不覆盖，末尾追加、最新在末尾**（同主仓库 CHANGELOG 约定），日期先核实。
> 测试集清单总览见 README.md；新增集流程见文末。

## [2026-09-06] 测试体系重建（脱敏 + 纳入版本管理）

### 背景
- 原 stest_tmp/ 54 个脚本：30+ 硬编码测试服务器 IP/密码（105 处敏感命中）、历史一次性残留混杂、不入版本管理
### 变更
- 新建 tests/：unit（regression 54 / credential 41）+ live（sudo 12）+ _load 公共加载器
- 脱敏：live 凭据全部 env（PYAISSH_TEST_*）；`tests/live/.env` gitignore；零硬编码
- 原则确立：测真实函数不测复制品（历史 gate/assembly 单测是复制型，放弃，行为由 live 覆盖）
- 旧 stest_tmp/ 留本地（含凭据，不入库）

## [2026-09-06] live 套件补全（去重合并）

### 变更
- tests/live/_common.py 公共层（env 凭据/run/target/check/require/finish）
- test_sudo 12 例重构复用 _common；test_exec = exec 12 + field 7 合并（19）；test_transfer 3（含 --parallel 4）
- 全绿：unit 54+41 / live 12+19+3

## [2026-09-06] 统一入口 run_tests.py（集文件保留）

### 变更
- 新增 run_tests.py 编排 5 集（subprocess 调度）；缺 live 凭据预检 SKIP
### 撤销
- 该形态仅保留一次提交即被"单文件化"取代（用户要求全部集在一个文件内，非入口+集文件）

## [2026-09-06] 单文件化（合并 5 集文件 → run_tests.py）

### 变更
- 全部测试集内联 `tests/run_tests.py`（129 断言零丢失）：unit_regression 54 + unit_credential 41 + live_sudo 12 + live_exec_field 19 + live_transfer 3
- 删除 tests/unit、tests/live 子目录与集文件（_load.py/_common.py 逻辑内联为 _module/_Suite/_live_*）
- 运行时选集：交互菜单（数字 0-N）/ --all / --unit / --sudo / --exec / --transfer / --list

## [2026-09-06] 单文件化 + 制品结构集 + 本记录文件

### 新增
- **测试集扩展**：run_tests.py 单文件新增 **unit_artifacts 集（6 例）**：
  - 域边界横幅 11 个存在 + 序 01..11 + 标题非空（构建器 join 自动生成特性防退化）
  - 横幅后紧跟本域 docstring（代码地图分区完整）
  - docstring 分区 ≥12（文件头 + 11 域）
  - VERSION 源码文本与模块一致（防单文件漂移）
- **run_tests.py 维护性**：顶部 docstring 内置代码地图（框架/unit 集/live 集/CLI 函数清单 + 职责）；本 CHANGELOG.md 开始记录维护史。
- 使用：`--artifacts` 单独跑制品集；`--unit` 含它；菜单集 3。

### 说明
- 制品集读被测源码文本（`module.__file__`）——只对 v2.0.0+（含域横幅的构建产物）通过；旧版单文件会 FAIL（横幅是 v2.0 特性，预期）。

## [2026-09-06] live_transfer 补 --parallel 上传与 --resume 续传用例（3 → 6 例）

### 新增
- **T4 upload --parallel 4**：并行上传往返——bytes_transferred + parallel_used==4 + 远端大小核对（exec stat/wc）
- **T5 download --resume 断点续传往返**：伪造本地续传点 `<dl>.part`（前 40%，resume 模式固定名 `local+".part"` 见 `_sftp_get_resume`）→ 下载完成且字节一致
- **T6 download --resume 续传日志**：stderr 出现 [RESUME]/续传标记佐证真实续传（JSON `bytes_transferred` = 文件全量非增量，不能用它断言——已记录此语义坑）

### 修的问题
- 初版用 `bytes_transferred < 全量` 断言增量续传 → FAIL：该字段语义是文件总字节。改为 stderr 续传日志佐证（注释记录原因，防后人再踩）

## 新增测试集流程（照此维护）

1. 在 run_tests.py 写 `def suite_xxx(s):`（unit 用 `s.check("名", 条件)`；live 先 `_missing_env(...)` 检查返回 None=SKIP）
2. 在 `SUITES` 注册一行 `(name, 描述, 函数)`；若加 CLI 直选标志，同步加 argparse 参数 + main 的 order 分支
3. 本文件**末尾追加**一条记录（修了什么/加了什么）；README 的测试集清单同步
4. `python tests/run_tests.py --all` 全绿后随改动提交

## [2026-09-07] v2.1.0 五大修复的测试覆盖

### 新增用例
- unit_regression +4：--exclude 匹配单元（目录名/相对路径/剪枝靠名字/不误伤）
- unit_credential +6：`$(cat f)` / `$(<f)` 从文件读值豁免（无明文泄漏不误报）
- live_exec_field +3：field 失败给 stderr 尾巴（命令失败直接给内容不多跑一轮）/
  field 成功 stderr 仅提示不塞内容 / --progress 心跳
- live_transfer +1：upload --exclude 真机（本地树含 node_modules/.git → 远端无 + 字节只算非排除）
### 修的问题
- 初版 exclude 用例误用 fnmatch `**` 跨目录语义（fnmatch 不跨 /）→ 改为"目录剪枝靠名字命中"断言
- 例数同步：unit 58+47+6 / live 12+22+7（README/run_tests 描述）

## [2026-09-07] v2.1.1 --progress 心跳 --field 静音修复的测试覆盖

### 新增
- live_exec_field +1：`--field stdout` + `--progress 1` 心跳可见（长任务+field 恰最需心跳；
  且无 [SSH] 进度噪音、stdout 裸值纯净）——实测教训转正为回归用例
### 例数
- exec_field 22→23；全量 153（unit 111 + live 42）

## [2026-09-07] v2.1.2 host --field 统一（使用注意反馈）

### 变更
- host add 输出走 _emit_result（--field 支持）；副本手动验证（alias/target 裸值 + 默认 JSON 不回归）
- host remove/list 进 dev README backlog（自动化测试跳过——host add 写 .env 需副本，代价高收益低，手动验证已覆盖）

## [2026-09-07] v2.1.3 host remove/list（backlog 兑现）

### 新增
- host remove NAME（删别名+专属凭据行；不存在明确 bad_args）+ host list（entries 不回显密码）
- 副本手动验证全流程（add×3/list/remove/不存在/再 list + .env 行级核对）——host 写 .env 需副本，未自动化（同 v2.1.2 理由）

## [2026-09-08] v2.1.4（P0 误报收敛 + P1 PS 注 + P2 漂移修复）

### 新增
- unit_credential +8：六误报修复用例（grep -p/-P/ffmpeg -pix_fmt/useradd 空/useradd -p 空引号/echo 打印段/java 属性空值）
  +2 真命中边界（echo && mysql 段独立 / useradd -p hash）
- **unit_host 集（7 断言）**：host add/remove/list 自动化——副本 importlib（被测复制 temp 目录 → 写副本 .env）；覆盖写入/幂等/list/不回显/remove/报错。此前"手动验证"缺口补齐
### 变更
- 全部 suite 描述与 README 去硬编码例数（漂移根因）——数字以运行输出为准
- 修 4 处 \$ 无效转义（SyntaxWarning）
### 教训
- host 写"模块同目录 .env"使单测无法直接调被测（会污染仓库根）——副本 importlib 模式解决

## [2026-09-08] fail2ban 事故防护：live 集凭据预检（重要）

### 事故（真实发生）
- sudo 集一次凭据 env 配错 → T1-T12 每用例各试 1 次错误密码 = **连刷 5 次 tester/tester_np 登录失败**
  → 触发服务器 fail2ban「2h 5 次错封 2h」封本机出口 IP 2h → 直连全断，靠 A 机跳板救回（差点 VNC 救援）
- 教训：测试脚本绝不能在凭据错时对真实服务器刷认证失败（pyaissh 工具本身失败即停无责，测试套件有责）
### 防护（已实现）
- live 三集（sudo/exec/transfer）开跑前 `_preflight()`：单次 test 验证凭据（tester/tester_np/root 各 1 次）
  → 失败打印 SKIP 原因并跳过整集（预检只产生 1 次失败，远低于 fail2ban 阈值）
### 验证
- 错误密码场景：预检 SKIP ✓（不再刷用例）；正确密码：sudo 12/exec 23/transfer 7 全绿

## [2026-09-08] unit_host 临时目录改 makedirs（受限令牌修复）

### 修复
- suite_unit_host 原用 tempfile.mkdtemp()：Windows 受限令牌下建 0o700 目录（空 DACL）——
  创建进程自己写不进（os.chmod WinError 5）
- 改为脚本同目录 `.tmp_host_<pid>` + os.makedirs（默认 ACL 继承，可写可删）+ finally rmtree；
  不再 import tempfile
### 验证
- --unit 全绿（host 7/7）；无 .tmp_host 残留

## [2026-09-13] v2.2.0 后台作业（--detach/log）+ 默认保留量 64KB + 场景参数表

### 新增
- unit_regression +8：`_detach_scripts` 生成（命令原文/落日志写 rc/单引号转义）、`_job_files` 路径表、
  `_JOB_ID_RE` 穿越拦截、`DEFAULT_MAX_OUTPUT == 65536`、`_sh_quote`
- live_exec_field +10：detach 启动返回 job_id/log/rc → `log --wait-rc` 拿 exit_code=5 与输出 →
  `--offset` 增量读（next_offset 递增、两段拼接不重复）→ `--cleanup` 生效 → 清理后 `job_not_found` →
  `--list` 可用 → detach+sudo 互斥 / log 缺 job-id / job-id 穿越 三处 bad_args
### 变更
- 例数不再写死在描述里（沿用 v2.1.4 去数字化约定）

## [2026-09-13] v2.2.1 后台作业反馈修复（字段名/权限/kill 收敛/截断提示）

### 新增用例
- unit_regression +1：`run.sh` 含 `umask 077`；`_job_files` 含 `job.pid`
- live_exec_field +7：远端作业权限实测（dir 700 / job.sh·job.log·job.rc·job.pid 600 / run.sh 700，
  用 `stat -c` 经 `--field stdout` 取裸输出）；log 载荷为 `stdout` 且 `content` 已移除、
  `stream=stdout+stderr`；`log --kill --wait-rc` 收敛为 `dead` 且 `waited_ms < 20s`（不再等满超时）；
  `dead` 带 `hint`；尾读截断（`--lines 5000 --max-output 2000`）给 `--offset 0` 提示；
  `--offset 0` 顺序读拿到全文尾部；`--kill` 缺 `--job-id` → bad_args
### 说明
- 两条最初写成 FAIL 的用例是**测试自身写法错误**（用了 `_live_run` 取 `--field` 裸输出→拿到 None；
  小 `--lines` 时回传内容本身就小、不会触发截断）——产品行为经手工复核正确后修正测试

## [2026-09-13] v2.2.2 `--cleanup` 状态守卫 + 无孤儿断言

### 新增用例
- live_exec_field +7：运行中 `--cleanup` → `job_running`(exit 2) 且 `pid` 在；拒绝后目录仍在（可继续追踪）；
  `--cleanup --force` → cleaned + `forced_cleanup:true` + warnings 含"仍在运行"；force 后进程确实仍在跑
  （代价可见，`pgrep -af '[s]leep 120'` 命中）；外部 pkill 收尾；`--kill --wait-rc` 收敛 dead + 快速收敛
  （waited_ms<20s）+ hint；**`--kill` 后无孤儿**（`pgrep -af '[s]leep 300'` 无输出）；
  `--force` 缺 `--cleanup` → bad_args
### 说明
- 三条用例最初报 FAIL 是**测试自身写法错误**（`--field` 模式 stdout 是裸值，`_live_run` 的 last_json 为 None）
  ——改用 `_live_sub` 取裸 stdout 后修正，产品行为经手工复核正确
- 孤儿检查用 `[s]leep` 技巧避免 pgrep/pkill **自匹配**（自匹配会把自己的包装 shell 也算进去/杀掉）

## [2026-09-13] v2.2.3 `--force` 后给出可执行整组杀命令

### 新增用例
- live_exec_field +4：`group_kill` == `kill -9 -<pid>` 且与 warnings/next_action 三处一致；force 后
  `next_action` 不再出现 `--offset`（日志已删，旧文案误导）；照 `group_kill` 原样执行 → `GROUP_KILLED`
  （一次清整组，无需 pgrep）；**对照**用正 pid `kill -9 <pid>` → `ORPHAN_LEFT`（证明负号不可省）
### 说明
- 对照组是"负号必要性"的证据：只杀组长时 job.sh 的子进程会被 reparent 成孤儿
- 该组用例仍全部用 `[s]leep` 技巧避免 pgrep/pkill 自匹配

## [2026-09-16] v2.2.4 CRLF 行尾归一

### 新增用例
- unit_regression +6：`_normalize_cmd_newlines` 纯函数（CRLF→LF 计数、孤立 CR、混合、纯 LF 零改动、
  转义 `\\r` 字面量不受影响、`--keep-crlf` 默认关/显式开）
- live_exec_field +11：内联 `--cmd` CRLF 归一并回传 `crlf_normalized=2`；warnings 说明含 `--keep-crlf`；
  `--keep-crlf` 保留 CR（`v` 值含 `\r`）且不回传计数；`--cmd-file` 与 stdin 行为不回退（各带计数）；
  `--cmd-file + --keep-crlf` 保留 CR；纯 LF 文件零改动（无字段无警告）；heredoc 落盘默认无 CR；
  `--keep-crlf` 时 heredoc 落盘保留 CRLF；行尾孤立 CR；detach 回传计数且 job.sh 无 CR
### 说明
- 新增 `_live_sub_bytes` 助手：`subprocess.run(input=str, text=True)` 在 Windows 上会把 `\n` 再翻成
  `\r\n`（CRLF → CRCRLF，计数翻倍）——**测试助手行为，不是被测程序**；stdin 用例一律走字节
- 实测先行的价值：原以为"`--cmd-file` 也暴露"，实测发现它早被 Python 文本模式隐式归一，
  真正未覆盖的是**内联 `--cmd`**——修的是这一条，同时把隐式行为变显式可关

## [2026-09-21] v2.2.4 补：MANIFEST 锚断言 + split 安全化

### 新增用例
- unit_artifacts 6 → 11：MANIFEST 12 域 / 锚全部命中（无过期锚）/ 锚唯一 / 锚顺序与域序一致 /
  锚对应域文件齐全
### 说明
- 起因：`01_globals.py` 的锚仍写 `^VERSION = "1\.5\.19"`（源码已 2.2.x）——`join/dist` 不用锚，
  构建照常，所以此前无人发现；而 `split` 会先清空 `domains/` 再切分，锚失效 = 域文件被删空
- 已把锚改成与版本无关形式（`^VERSION = "`），并给 `split` 加"先算后写 + 不一致拒绝覆盖"保护
- 只读审计脚本确认：其余 11 个锚唯一命中且有序；`check` 金标对比仍逐字节一致（构建管线未受影响）

## [2026-09-21] v2.2.4 补二：移除 split（构建方向定为单向）

### 变更
- `build_single.py` 删除 `split` 与 `_split_text`；跑 `split` 明确回 `SPLIT_REMOVED`（exit 1，不动磁盘）；
  用法行改为 `join|check|dist`
- `MANIFEST_domains.txt` 删除锚列（退回纯顺序清单）；`load_manifest` 兼容旧的 `文件 | 锚` 写法
- `pyaissh-dev/README.md` 构建器章节改写：明确"域文件是源、成品是产物"，并记录 split 移除原因
- `pyaissh-dev/.gitignore` 注释同步（split/join → join）
### 测试
- unit_artifacts 11 → 9：删掉 3 条锚断言（锚已不存在），保留并加强为
  "MANIFEST 12 域 / 域序 00..11 / 域文件齐全"
- 回归：`dist` md5 与移除前一致（`94a7a654…`，说明移除不影响成品）、`check` 逐字节一致 + 编译 OK

## [2026-09-24] v2.3.0 session 常驻会话（真 PTY）

### 新增用例
- **live_session（新套件，19 例）**：start 返回 pid/pty/ready+0700 权限；逐条喂命令退出码 0 与 127；
  错误命令不中断会话且 cwd 保留；长命令 `status=running`；`ctrl-c` 发信号且被中断命令收敛、
  会话存活状态保留；`keys` 应答 `read -p` 提示（`GOT:hello_pty`）且**哨兵未被吃掉**；
  默认剥离 ANSI；`list` 显示 running/pty；`kill` 扫到进程树（swept>=3）+ 无残留 + 目录清理 +
  远端无 `script` 残留；`session_not_found`/非法名/缺命令三条错误路径
- 单元 9 例：会话路径表、会话名正则（拒穿越/空/超长/前导连字符）、`--data` 转义
  （`\n \r \t \xNN \\`）、哨兵包裹（`{ ...; }; echo` 同一行）、哨兵切分（按 token 取各自输出与退出码、
  无 token 取最后一个、目标未出现→running）、输出清洗（CR/ANSI/哨兵行/script 头）
- 制品集随域数更新：13 域 / 横幅 12 个（01..12/13）/ docstring ≥13
### 开发期实测教训（已固化为实现与断言）
- 哨兵单列一行会被命令里的 `read` 吃掉 → 必须 `{ ...; }; echo 哨兵`（同一行解析）
- `kill` 按 sid 清理无效（`script` 的子 shell 自己 setsid）→ 改用 starter 进程树闭包；
  闭包 awk 另有两坑：引号写成 `root=\"$P\"`（值带引号→闭包恒空）与打印用下标而非 `pid[k]`
- `ctrl-c` 的 SIGINT 对 setsid+nohup 起的会话树可能无效 → 自动升级 SIGTERM（`--force`=SIGKILL）
- 测试助手坑：`time` 未导入导致套件中途崩；哨兵 fixture 用了非 hex token（正则按设计不认）

## [2026-09-24] v2.3.0 补：session run（一步一次调用）+ SKILL 默认姿势决策表

### 新增用例
- live_session 19 → 25：run 一次调用拿 exit_code+输出；run 之间状态保留；run --wait-rc 超时 →
  status=running 且 **token 保留**（可续等）；run 起的命令可 ctrl-c 中断并收敛；--no-wait 只发送；
  随后 read 取到结果
- MCP 真机 21 → 23：B17a（经 MCP 透传 run 拿 exit_code+输出）、B17b（run wait_rc 超时 → running
  且 token 保留）；离线 T2a2 的 action 枚举断言同步为 8 项（含 run）+ no_wait/max_output 参数
### 说明
- 修复实测发现的缺陷：run 超时分支把结果里的 token 覆写成 None（消费者无法用 --token 续等）
- MCP 侧一开始报 `invalid choice: 'run'` —— 根因是 **pyaissh-mcp/pyaissh.py 副本未同步**
  （dist 只更新根+skills），`sync_check.py --update` 后即通过：改 CLI 后别忘了同步 MCP 副本
- 决策表依据：本会话 382 次 pyaissh 调用中 139 次 exec 仅 14% 状态敏感 → 不设全局默认

## [2026-09-24] v2.3.0 补二：修正 session 用法措辞 + 固化"人类式重试"

### 背景（用户指出）
- 我此前把用法写成"错了**改下一条**继续"——说小了。真实用法是**打错了就把那条命令改对再发一遍**
  （同一条重试，像人在终端里：文件名打错 → 报错 → 改对 → 重发 → 成功），状态（cwd/变量）与上次完全一致
### 文档措辞修正（6 处）
- `11_cmd_session.py` docstring、`12_cli_main.py` session epilog、根 CHANGELOG v2.3.0 条目、
  SKILL.md（决策表 + session 小节）、docs/session.md 定位段 → 统一改为"改对再发一遍（同一条重试）"
- docs/session.md 新增「怎么判断这条失败了」（重试循环靠它）：exit_code 遵循 POSIX 管道语义
  （取最后一个命令）+ **stderr 合并进 stdout**，所以报错文本可直接判断"名字打错了"；
  命令等输入时会等到超时回 running，用 ctrl-c 打断再重发
### 新增用例
- live_session 25 → 28：打错的命令报错（exit_code!=0 + No such file）；改对后重发同一条命令即成功
  （用的正是会话里 export 的变量）；重试期间 cwd 保持不变
### 说明
- 验证过程中的两次 FAIL 都是**夹具问题**：① 拿 nginx 做样例但目标机没装 nginx（产品行为正确）；
  ② `cat $打错的变量 | wc -c` —— 变量为空导致 `cat` 无参读 stdin 阻塞（人类会按 Ctrl-C），
  且管道 exit_code 取末尾命令（POSIX），故断言应看输出文本而非 exit_code

## [2026-09-24] v2.3.0 补三：外部评审 5 个真机缺陷的回归护栏

### 新增用例（先逐个复现、再修、再回归）
- live_session 28 → 36：B1 会话长等待 >30s 不被 SFTP 看门狗误杀（sleep 32 跑完）；
  B2 `--no-pty` 就绪（ready=True/pty=False）+ run 拿到输出与退出码 + 多行复合命令与状态保留；
  B3 `read --lines 5` 真的只回 ≤5 行（曾回 3000 行）；B4 输出 >1MB 仍能看到哨兵（`seq 1 300000`
  → done + 尾部 BIG_DONE，曾永远 running）；B5 run/send 回传 `crlf_normalized=1`（真 CRLF 夹具）
- live_exec_field 62 → 64：B1 `log --wait-rc 50` 等满 32s 不被误杀（finished + exit 0）；
  刚结束的作业不误判 dead（rc 落盘宽限）
### 说明
- 复现脚本发现的两个**夹具/流程坑**：① 我用 LF 文件测 CRLF 归一 ⇒ 字段缺失是正常行为（夹具错）；
  ② 套件跑的是**根 `pyaissh.py`**，只跑 `join` 不跑 `dist` ⇒ 新修复没进被测二进制（教训：改完 code
  必须 `dist`）；③ 新块里用了未导入的 tempfile/shutil ⇒ 套件中途 NameError 中断
- 另修一处**自测发现的竞态**：判 `dead` 前给 job.rc 落盘 0.6s 宽限（`sleep 45` 刚结束那一瞬
  曾被读成 dead）
- 首次跑新用例出现一次 `exec --detach` 未返回 job_id（未复现，isolated 3/3 正常），失败详情
  已改为打印原始 JSON
