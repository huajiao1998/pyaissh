# pyaissh 测试（单文件）

全部测试集内联在 **`tests/run_tests.py`** 一个文件里，运行时选择要跑的集。
脱敏：文件内**零硬编码**（无服务器 IP/密码/token）——live 凭据走环境变量。
维护记录/变更史：`tests/CHANGELOG.md`（修了哪些问题、加了哪些集都在那）。

## 运行

```bash
python tests/run_tests.py                # 交互菜单（数字选 1-6 / 0 全部）
python tests/run_tests.py --all          # 全量：unit 先、live 后
python tests/run_tests.py --unit         # 仅单元（无网络，任何机器可跑）
python tests/run_tests.py --artifacts    # 仅制品结构集
python tests/run_tests.py --sudo         # 仅 sudo 集
python tests/run_tests.py --exec         # 仅 exec+field 集
python tests/run_tests.py --transfer     # 仅传输集
python tests/run_tests.py --list         # 列出测试集
```

## 测试集

| # | 集 | 例数 | 说明 |
|---|---|---|---|
| 1 | unit_regression | 54 | 凭据矩阵（L4）/ parse_target（N10）/ 编码机制（M1）/ 契约单元 |
| 2 | unit_credential | 41 | 凭据启发式：真凭据命中 + 工具 flag 不误报 |
| 3 | unit_artifacts | 6 | 制品结构：域边界横幅 11 + docstring 代码地图 + VERSION 一致 |
| 4 | live_sudo | 12 | --sudo 提权/整链/NOPASSWD/失败提示/互斥（真机）|
| 5 | live_exec_field | 19 | exec 行为 + --field 消费端（真机）|
| 6 | live_transfer | 3 | 传输往返字节一致 + --parallel（真机）|

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
3. 跑 `--all` 全绿后提交（连同工具改动）

## 原则

- **测真实函数不测复制品**：unit 调被测模块的 `warn_sensitive_cmd`/`parse_target`/`_SENSITIVE_CMD_RE` 等
- **live 是行为真源**：sudo 门控/组装等端到端由真机断言
- **凭据零硬编码**：live 只用 `PYAISSH_TEST_*` env
