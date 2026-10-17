# 项目守则：pyaissh（paramiko SSH CLI for AI agents）

> 本文件是**项目级**规矩，叠加在 `$DSH_HOME/AGENTS.md`（全局）之上。
> 全局的编码纪律 / 探针超时 / 后台作业 / 凭据纪律依然全部有效，这里不重复，只写
> **本项目特有**的东西。写精不写多——它每次会话都进上下文。

## 0. 红线（P0）
- **用户没亲口说「发」，不许 push、不许建 Release**（v2.4.0 已发过一次，那次因耗时被追问，别再自作主张）。
- **对外消息一律草稿先行，用户确认后才发**（2026-10-17 用户当场立规）：EigenFlux 私信 / 广播 /
  任何代表本项目的对外发言，都先在对话里贴**全文草稿** → 等用户点头 → 才执行 `msg send`。
  - **"批过一条" ≠ "以后都能直接发"**：用户确认过某一份草稿，只对那一条生效，每条独立确认。
  - 这次的教训：我一次直接发出 5 条（Sarah/DSH/AI-Film-Studio），其中 1 条认可了对方一个
    **不成立的前提**（"源码可读是维护方特权"），事后只能补一条更正——**已发出的消息收不回**
    （无撤回接口），代价是项目信誉。
  - 正确流程：**起草 → 贴给用户 → 等明确答复 → 发**；用户说"就按这个发"才发，别自行推断。
- `pyaissh.py` 契约：**stdout 恒单行 JSON**、进度/告警走 stderr；工具自身错误退出码
  `{0,1,2,124,130,254,255}`（**远端命令/作业的退出码原样透传**，所以本地退出码不限于这个集合——
  判成败以 JSON 的 `status`/`exit_code`/`exit_success` 为准，v2.5.3 订正）；
  结果带 `retryable`；`--field` 只取值。动任何域先问一句"这破不破契约"。
- JSON 字段**只增不减**（删字段 = 破契约）；唯一的例外 `fifo` 是用户点头的兼容清理。新增字段时**默认值场景不得改变既有字段集**（空缓冲就别建键）。
- **咨询型提醒必须双通道**：stderr 日志（给人/交互）+ 结果 JSON 的 `warnings[]`（给纯 `--json` 消费方——他们 `2>/dev/null` 丢 stderr，而那正是主要受众）。连接层的提醒统一进 `_CONN_WARNINGS`，由 `emit`/`emit_error` 两个 funnel 汇入，一处覆盖全子命令、成功与失败两条路；**别只在 `resolve_*` 里 `log()` 一句就完事**（v2.5.0 `--jump-password` 提醒真踩过，被用户当场指出）。
- **禁止双引擎 / 引擎开关**：session 已是单引擎（tmux），用户拍过板。
- 别的会话在共享目录里的东西一律不碰（tavern-ops/、别人的 `.gitignore`/`.zcodeignore`/PNG、
  `/root/pyaissh_push`、`/root/pyaissh-git`）。`stest_tmp/` 只删**自己按确切文件名**建的文件。

## 1. 构建与同步管线（改完代码必跑）
- **exec 已有 `--script <本地.sh>`（v2.5.0）**：本地脚本整文件走 SFTP 落远端
  `/tmp/.pyaissh-script-<随机>.sh` → `bash` 执行 → `finally` 删除；恒有键
  `script{local,remote,bytes,sha256}`。跑复杂远端脚本首选它（别再做 base64/printf 搬脚本）；
  与 `--cmd`/`--cmd-file` 互斥、**暂不支持 `--detach`**。
- **域文件是源，单文件成品是产物**：`pyaissh-dev/domains/` 13 个文件（顺序见 `MANIFEST_domains.txt`）。
  **不要直接改 `pyaissh.py`**，下次 `dist` 会被覆盖。
- `python pyaissh-dev/build_single.py dist` → 拼成 `pyaissh-dev/pyaissh.built.py` 并同步两份：
  根 `pyaissh.py` + `skills/pyaissh/pyaissh.py`（md5 必须一致）；`check` = 金标逐字节对比 + 编译。
- `python pyaissh-mcp/sync_check.py` = 检查 13 个副本文件是否与 `skills/pyaissh/` 逐一 md5 相同（**必须绿**）；
  `--update` 才真正覆盖。动 skill 内容后必须跑一次。
- CHANGELOG 三处同步且 md5 相同：根 `CHANGELOG.md` = `skills/pyaissh/CHANGELOG.md` = `pyaissh-mcp/CLI_CHANGELOG.md`。
- 另两个测试记录各自独立：`tests/CHANGELOG.md`、`tests/README.md`（后者已按用户要求不写测试规则）。
- **项目文档 / CHANGELOG 不写开发流程与测试纪律**（用户原话："影响其他测试AI"）——这类只进本文件和 MEMORY.md。

## 2. 编码纪律（项目侧落地，规则本体见全局）
- 仓库文本文件（`.py/.sh/.ps1/.json/.md/.yml/.cmd`）一律 **UTF-8 无 BOM LF**。
  注意 `core.autocrlf=true`：**工作区文件是 CRLF、库里是 LF**（制品 `pyaissh.py` 6067 CRLF + 1901 LF 正常）。
  打 zip 必须 `git -c core.autocrlf=false archive`，否则 Linux 上 `bash\r` 直接 "bad interpreter"。
- **已上闸**（`tests/run_tests.py` 的 `suite_unit_artifacts` / `--artifacts`）共 7 条：UTF-8 + 无 BOM、
  `open()` 一律 `encoding=`（AST 静态检查，二进制模式放过）、入口脚本 11 个都 reconfigure UTF-8、
  制品调用 `_setup_console_utf8()`、`SetConsoleOutputCP(65001)` + 退出还原。
- 域的片段**不用各自加** reconfigure——域 04 `_setup_console_utf8()` 统一负责。
- 远端会话 pane 拿不到 locale 会落 POSIX：引擎在通道没有 `LANG`/`LC_ALL` 时**兜底注入 `LANG=C.UTF-8`**
  （用户自己的 locale 原样透传）。
- **PowerShell 转义 ≠ bash**：给远端传含特殊字符的复杂命令，写脚本走 `pyaissh.py exec --cmd-file -`，
  不内联拼 `--cmd`；命令文本里的 CRLF 会被归一（`crlf_normalized` 回传，`--keep-crlf` 是逃生阀）。

## 3. 测试纪律（2026-09-24 用户三次收紧后定型；本项目实跑口径，全局只留指针）
- **测试要做，但跑之前先问两句**：
  1. **这个测试有必要吗？** —— 有没有更小的手段拿到同样确证（读代码 / 静态检查 / 一条命令 / 一条断言）？
  2. **范围有没有超出改动？** —— 只测改动落点；**禁止顺手跑相邻模块或"整个模式"的回归**。
  两句都过了才跑；跑起来发现没必要或越界 → **立刻 `job_kill`**，不要"既然起了就等它跑完"。
- 细则（由上面两句直接推出）：**最小粒度是断言，不是块**；一条命令能确证的别写测试脚本；
  **改了 A 就不跑 B/C**（"图安心"就是越界）；**能上闸就上闸**（禁令做成工具行为，别靠自觉）；
  想看进度用 `-u` + 短轮询，别长时间静默等待；**"跨域"不是放宽范围的理由**（哪怕动的是全局常量 /
  构建管线 / 传输层也一样，例如只加 `encoding=` → 一条 `--artifacts` 断言即可）。
- 开发期**禁止全量测试、也禁止"整个模式"测试**（`--all` 与 `--session` 都算）：
  **全量是"发布到 GitHub 前"的工作，不是开发时的工作**；本项目已把 `--all`/`--session` 绑死 `--release`，
  不带就直接拒绝执行。
- 跑块：`python -u tests/run_tests.py --suite <块名>`（`--list` 看块名；纯函数/制品 → `--unit` 或
  `--artifacts`；传输 → `--transfer`；提权 → `--sudo`）；块内多条断言只挑最相关那条；临时单点用
  `python -c` 或一次性脚本放 `stest_tmp/<slug>/`。
- 会话相关块名与规模（发布前 `--all` 时用）：`live_session` 30、`live_session_ttl` 16、
  `live_session_engine` 13、`live_session_lifecycle` 13、`live_session_orphan` 7、`live_session_bugs` 11。
- 验收基线：`pyaissh-dev/contract_baseline.py --check tests/contract/session_contract_v2.json`（21 例 key 集，
  需真机；`fixture` 见同目录）。**新字段先在 fixture 加键再跑**，否则"缺键"FAIL。
- **不跑测试的改动**：纯文档 / 注释 / CHANGELOG 一律不跑；常量、文案最多跑对应的一条断言。
- **禁止为"看数字 / 拿耗时"重复跑整条套件**（被批过）；数字类问题用已有实测记录回答，别重跑。
- 测试 flake 要在**产品侧**根治（如 reaper pid 指纹改成同步写入），测试侧重试只会掩盖。
- **反面教训（都在本项目真踩过）**：① 只加两处 `encoding="utf-8"` 却跑了连接层回归 + MCP 离线回归，
  还用 420000 ms 超时干等——被批"改了编码直接测试一个就可以"（2026-09-24）；
  ② 为回答"快了多少"跑整条 `--session --fast`——被批。以上两条已从全局挪到此处，别删。
- **判断文档/代码质量，别用代理指标下结论**（2026-10-17 连踩两次）：先说 docstring
  "代码地图"弱（依据只是"这个词只出现 1 次"的词频），又说某域 docstring 是空壳
  （依据是"函数名提及率 36%"），**实际读一段原文就发现两者质量都比指标显示的好**——
  域 11 的 docstring 讲的是"为什么有这个域、与 exec/detach 的分工、为什么换 tmux 引擎"，
  这才是有价值的内容，而函数清单 `grep '^def '` 一条命令就有。
  规则：**指标只能用来决定"去哪儿读"，不能用来下"好坏"判断**；要下结论先读原文。
- **别为假想需求加字段**：契约是"字段只增不减"，意味着**加进去就永远删不掉**——
  为"万一将来要"加的字段是永久成本。先问"现在有消费者吗"（2026-10-17 EigenFlux 上
  Sarah / AI-Film-Studio / DSH 三方建议加契约版本+完整性摘要，核完判定**不适用**：
  pyaissh 是实时观测工具，输出是瞬时快照不是数据资产，要新数据就重连再跑，
  没有跨版本比对场景；详见 MEMORY.md「待决/未完成」）。
- **上闸别把闸口设在它自己检查的那份文件里**（2026-10-17 自查发现）：交接闸原本要求
  "`NOW.md` 记的 HEAD == 当前 HEAD"，但**提交 `NOW.md` 本身就会移动 HEAD** → 谁提交谁撞红，
  这道闸在提交后**必然**为假。当时只因 `docs/` 整目录还没被 git 跟踪、HEAD 又恰好等于文档里
  那个值，才一直是绿的——**看起来在守护，实际一提交就死**。
  改成"记的必须是真实历史提交，且自那以后只有文档改动"才可满足。
  规则：**定闸前先问"这条断言在完成它要求的那次操作之后还成立吗"**——自指的闸要留出
  提交自身造成的那一步差量。
- **别在对方质疑下过快认领前提**（2026-10-17 真踩，被用户当场问倒）：EigenFlux 上的对端说
  "『有源码可读』是维护方特权，不是调用方权利"，我**没验证就认错**并写进退让回复。用户一句
  "py 是解释性语言，打开就能读，不存在维护方特权"直接戳破。实测核对：分发的 `pyaissh.py`
  8421 行、与仓库根同名文件 md5 相同，34 条域横幅 / 284 个 docstring / 952 行注释原样保留，
  无压缩无剥离；13 个域文件只是构建拼装源，单文件里带着全部横幅与说明。
  **"可执行但不可读"对解释型脚本不存在**（2026-10-17 真机实测）：权限 111 的 `.py` 换非 root 用户执行
  → `python3: can't open file ...: Permission denied`（rc=2），shell 脚本同样。内核拉起的是**解释器**，
  解释器必须 `open()` 脚本 → 要读权限。只有编译成 ELF 的二进制才能"给跑不给抄"（内核直接 map）。
  所以对 pyaissh：**能跑就等于能读**，这是机制决定的，不是配置宽松。
  规则：**让步之前先验证对方的前提**。对方的前提常是他自己环境的约束（编译型/二进制分发的
  习惯），套到解释型单文件工具上不成立。认错本身不贵，但认错后面挂着的"改文档/改设计"很贵——
  而且会给项目记下一条不存在的设计缺陷。
- **别把别的分发形态的"治理机制"当需求搬进来**（2026-10-17 用户点破）：一天之内我搬进来两个
  ——"源码可读是维护方特权"、"字段类型表 + 发版字段集清单"。这类东西都服务于**存数据、钉版本、
  二进制分发**的产品；我们是**实时观测工具**：产物就是可读源码，每次重连即可重跑。
  用户的判断标准（照抄）：**"AI 哪里有记忆？都是上下文，隔一段时间用工具本来就要重读文档。"**
  ⇒ 不存在"消费者记忆"这个需要保护的资产；该守的是**文档与实际一致**（这才是 Sarah 那条闸
  该管的事），不是"字段跨版本稳定"。**改动让工具更好用就改，义务是同步改文档与仓库内调用方**
  （脚本/MCP/测试都硬编码字段名，它们才是会崩的东西，但崩得响）。

## 4. 发布（通道 B2，2026-09-25 起一律走一键管线）
- **服务器**：B2 `103.79.186.77`（root, 22，发版机，gh CLI 在 `/usr/local/bin/gh`）、
  A `103.79.184.140`（测试机：exec/sudo/transfer，tmux 3.5a 已装）、
  tavern `103.117.121.145`（生产机，**禁止触碰**）。
  **IP 可以写**（用户 2026-09-25 拍板：家宽本机、无攻击面）；**口令 / token 仍一律不写**，凭据只从
  gitignored 的 `pyaissh-mcp/test/local_creds.json` 或 env 取（见本节末尾两条）。
- **gh 登录是长期保留项，任何收尾脚本都不许删**：`~/.config/gh/hosts.yml` + `/root/.gh_token`（600）——
  其他 AI/会话也用这台发版，删了全员重新登录。
- 收尾只清：bundle、`/root/pyaissh_push.git`、上传的 zip、notes、以及本管线上传的脚本。
- **默认命令**（gitignored，详见 `stest_tmp/release/README.md`）：
  ```bash
  python stest_tmp/release/go.py --title "v2.5.0 — 一句话卖点" \
      --notes stest_tmp/release/notes_v2.5.0.md
  python stest_tmp/release/go.py --prep-only     # 只本地构建+预检
  python stest_tmp/release/go.py --dry           # 连线核对+实测已发布资产，不改远端
  ```
  `prep.py`（本地：dist + 镜像同步 + 工作区检查 + **9 项制品预检** + 可复现 zip/bundle）→
  `publish.sh`（远端：fetch bundle → push master → 建/更 Release → **下载资产实测** → 成功才清理）。
  幂等：Release 已存在走 `gh release upload --clobber` + `edit`；否则 `create`。
- 凭据来源：env `PYAISSH_RELEASE_TARGET` / `PYAISSH_RELEASE_PASSWORD`，否则
  `pyaissh-mcp/test/local_creds.json` 的 `host1`（= B2，gitignored；`host2` = A 机）。**不在任何文件里写密码**。
- 手工兜底流程（管线挂了才用）：`git bundle create --all` → 上传 bundle + zip + notes → B2 上
  **必须** `git init --bare`（普通仓库会因 "refusing to fetch into branch checked out" 失败）fetch bundle →
  `git push refs/heads/master:refs/heads/master` → `gh release create <tag> <zip> --target master`。
- **制品预检九项**（v2.4.0 实踩过的坑全在这里拦）：条目集合精确、无 `.env`/`__pycache__`、
  全部 LF、无 BOM、UTF-8、`pyaissh/pyaissh` mode=755、shebang、解压实跑 `--version`、无高置信凭据特征。
  755 的根因是 `git update-index --chmod=+x skills/pyaissh/pyaissh`（历届 zip 都是 644 → Linux 上 Permission denied）。
- `git archive` 用**当前时间**写条目时间戳 → 同一 commit 两次打包 sha256 不同；`prep.py` 已把时间戳归一成
  commit 时间并自证，所以"远端资产 sha256 == 本地产物"这条最硬的校验才可用。**别绕过它。**
  （`prep.py` 每次开头先删旧 `.sha256`：失败后留着旧文件会被误读成本轮产物——真踩过。）
- 验证已发布资产**别用真发布重来**：`--dry` + gh shim（`stest_tmp/release/ghbranch_test.sh` 拦
  upload/edit/create）就能把两个 Release 分支都跑通，且不动线上资产。
- `os.replace` 在 Windows 会被瞬时句柄挡（WinError 5，实测重试 4–5 次才过）——脚本里带重试，别当成偶发忽略。

## 5. session 引擎 = tmux（v2.4.0 定型，改之前先读 spec）
- 验收与事实清单：`pyaissh-dev/SPEC_session_tmux.md`（INV/ENG/REAP/PERF/ERR/UPG/BND + §7.1 C1–C10 +
  §8 V1–V10，全部填过）。动会话行为前先读它，别凭印象改。
- 实现要点：`tmux -L pyaissh -f /dev/null`；会话名自建映射 `py-<utf8 hex>`（**tmux 会话名里的点会被静默改写**）；
  pane 级命令一律带 `=NAME:`；注入用 `load-buffer` + `paste-buffer`；输出镜像 `pipe-pane -o`，
  `out.log` 被删后**去掉 `-o` 重新武装**（`log_recreated` 是 warnings 文本 + stderr WARN，**不是 JSON 字段**）；
  中断 `send-keys C-c` 并注入替代 sentinel（INT→130、KILL→137）；`--force` = 对内核 `tpgid` SIGKILL。
- kill = pane_pid 树闭包快照 → `kill-session` → TERM/KILL → rm 目录；`orphans`/`orphans_total`/
  `orphan_remaining_total` **永远返回且永远为空**（无孤儿扫描了）。
- 回收：send/run/read/ctrl-c/keys/list 前惰性 sweep（**kill 除外**）+ 每 host 一个 `reap.sh --loop`
  （默认 300 s，`PYAISSH_SESSION_REAP_INTERVAL` 可覆盖；无残留目录时自删 reap.sh 并退出；`kill` 会停掉 reaper）。
- 依赖 tmux ≥ 3.0；新增错误类型 `tmux_missing` / `tmux_unsupported` / `tmux_failed`（后者 `retryable=True`）。
- 已删（用户点头的兼容清理，别加回来）：旧引擎目录探测、`script` 头过滤、beat mtime 兜底、`fifo` 字段、`--no-pty`。
- 已知行为：legacy/未知目录不再识别（有 `meta` → `session_dead`，只有 `sess.pid`/`in` → `session_not_found`）；
  会话 `TERM=tmux-256color`；`--max-output` 是客户端裁剪；detach 的 `setsid`/`nohup` 子进程会逃过 `kill-session`。

## 6. 环境与凭据（只记事实，不写密码）
- 测试 / 发布机：A `103.79.184.140`（exec/sudo/transfer，tmux 3.5a 已装）、B2 `103.79.186.77`（发版）。IP 可写，口令/token 不可（见 §4）。
- `skills/pyaissh/.env` 只在本地存在且 gitignored；`.env.example` 是唯一随包分发的模板。**zip 里不许出现 `.env`**。
- MCP 层：`pyaissh-mcp/`，`SERVER_VERSION = "0.3.2"`，7 个工具；离线测试 `test/test_offline.py`、
  在线 `test/test_live*.py`（改 skill 副本后都要跑——但要按"改动落点"最小化，见 §3）。
- 技能包 zip 共 16 条目（14 文件 + 2 目录）；分发时入口 `./pyaissh/pyaissh --version` 必须 rc=0。
