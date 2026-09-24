# pyaissh 测试（单文件）

全部测试集内联在 **`tests/run_tests.py`** 一个文件里，运行时选择要跑的集。
脱敏：文件内**零硬编码**（无服务器 IP/密码/token）——live 凭据走环境变量。

## 维护规矩（必读）

**任何对测试的更新、修改、新增测试列——都必须先在 `tests/CHANGELOG.md` 追加一条更新记录**（改了什么/修了什么问题/加了哪个集/日期），再改代码。CHANGELOG 只追加不覆盖，是测试体系的历史台账——后面翻它才知道每个集为什么在、修过什么。新增集流程见文末。

## 运行

**开发期只跑"必要的最小测试"**（2026-09-24 定，用户三次收紧）：跑之前先问"这是不是验证本次改动的
必要最小测试"，不是就别跑——能靠静态检查/读代码/一条命令说清的就不跑套件；文档改动不跑测试；
能只跑一条断言就不跑整块。**全量（`--all` / `--session`）是"发布到 GitHub 前"的工作**
（工具层已上闸：必须显式带 `--release` 或 `PYAISSH_TEST_RELEASE=1`，否则直接拒绝）。

```bash
python tests/run_tests.py                # 交互菜单（数字选 1-6 / 0 全部）
python tests/run_tests.py --suite live_session_engine   # 最常用：只跑改动落点所在的那一块
python tests/run_tests.py --all --release # 全量：**仅发布前**（开发期会被拒绝）
python tests/run_tests.py --unit         # 仅单元（无网络，任何机器可跑）
python tests/run_tests.py --artifacts    # 仅制品结构集
python tests/run_tests.py --sudo         # 仅 sudo 集
python tests/run_tests.py --exec         # 仅 exec+field 集
python tests/run_tests.py --transfer     # 仅传输集
python tests/run_tests.py --list         # 列出测试集
```

## 测试集（例数以运行输出为准——描述不写死数字，防漂移）

| # | 集 | 覆盖 |
|---|---|---|
| 1 | unit_regression | 凭据矩阵（L4）/ parse_target / 编码机制 / --exclude 匹配 |
| 2 | unit_credential | 凭据启发式：真凭据命中 + 工具 flag/空值/打印段/读文件豁免误报 |
| 3 | unit_artifacts | 制品结构：域边界横幅 / docstring 代码地图 / VERSION 一致 |
| 4 | live_sudo | --sudo 提权/整链/NOPASSWD/失败提示/互斥（真机）|
| 5 | live_exec_field | exec 行为 + --field 消费端 + 失败尾巴 + --progress（真机）|
| 6 | live_transfer | 传输往返字节一致 + --parallel + --resume + --exclude（真机）|
| 7 | live_session | 常驻会话（真 PTY）：逐条喂命令/状态保留/退出码/ctrl-c/keys/kill 无孤儿（真机）|

## live 凭据（脱敏，不入库）

```bash
# Windows（PowerShell）：
$env:PYAISSH_TEST_HOST='root@<ip>'; $env:PYAISSH_TEST_PASSWORD='<pwd>'
$env:PYAISSH_TEST_SUDO_USER='tester'; $env:PYAISSH_TEST_SUDO_PASSWORD='<pwd>'
$env:PYAISSH_TEST_SUDO_NP_USER='tester_np'; $env:PYAISSH_TEST_SUDO_NP_PASSWORD='<pwd>'
# Linux/macOS：export 同名变量
```

- sudo 集需 sudo 测试用户：tester（sudoers `ALL=(ALL) ALL` 需密码）、tester_np（`NOPASSWD: /usr/bin/id,/usr/bin/whoami`）
- 缺凭据 → 该集 SKIP（汇总列出跳过，不假装通过）

## 被测目标覆盖（测新版本/重构产物）

```bash
PYAISSH_PY=<pyaissh.py 路径> python tests/run_tests.py --unit   # unit：importlib
PYAISSH_BIN=<pyaissh.py 路径> python tests/run_tests.py --exec  # live：子进程
# 缺省：仓库根 pyaissh.py
```

## 加新测试（工具加功能时）

1. 在 `run_tests.py` 内对应 `suite_xxx()` 函数加 `s.check("名称", 条件)`
2. 或新加集：写 `suite_xxx(s)` + 在 `SUITES` 注册一行
3. **先跑改动落点那一块**（`--suite <块名>`）确认为绿；`--all --release` 只在发布到 GitHub 前跑
4. `tests/CHANGELOG.md` 末尾追加记录

## 原则

- **最小测试**：断言只留"能抓住这次改动会怎么坏"的那几条；不做无谓的重复覆盖
- **测真实函数不测复制品**：unit 调被测模块的 `warn_sensitive_cmd`/`parse_target`/`_SENSITIVE_CMD_RE` 等
- **live 是行为真源**：sudo 门控/组装等端到端由真机断言
- **凭据零硬编码**：live 只用 `PYAISSH_TEST_*` env
- **描述不写死例数**（漂移教训 v2.1.4）：数字以运行输出为准
