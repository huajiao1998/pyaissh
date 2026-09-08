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
