# 项目守则：pyaissh（paramiko SSH CLI for AI agents）

> 本文件是**项目级**规矩，叠加在 `$DSH_HOME/AGENTS.md`（全局）之上。
> 全局的编码纪律 / 探针超时 / 后台作业 / 凭据纪律依然全部有效，这里不重复，只写
> **本项目特有**的东西。写精不写多——它每次会话都进上下文。

## 0. 红线（P0）
- **用户没亲口说「发」，不许 push、不许建 Release**（v2.4.0 已发过一次，那次因耗时被追问，别再自作主张）。
- `pyaissh.py` 契约：**stdout 恒单行 JSON**、进度/告警走 stderr；退出码 `{0,1,2,124,130,254,255}`；
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
