# pyaissh 测试体系

脱敏 + 版本管理：脚本内**不硬编码任何服务器 IP/密码**（真机凭据走环境变量）。

## 结构

```
tests/
├── _load.py          公共被测加载器（PYAISSH_PY 覆盖 / 缺省根 pyaissh.py）
├── unit/             纯逻辑单测（无网络，任何机器可跑）
│   ├── test_regression.py    回归 54 例（凭据矩阵/parse_target/编码/stdin——测真实函数）
│   └── test_credential.py    凭据启发式 41 例（--no-pager 类误报 + 真凭据命中）
└── live/             真机测试（需凭据 env，缺凭据 skip）
    ├── test_sudo.py          --sudo 提权 12 例（tester 用户）
    ├── test_field.py         --field 消费端 10 例
    ├── test_exec_matrix.py   exec 行为矩阵（pty/超时/截断/exit 码/中文）
    └── test_transfer.py      传输往返（upload/download 字节一致）
```

## 运行

```bash
# 单元（零依赖零敏感）
python tests/unit/test_regression.py      # 54 PASS
python tests/unit/test_credential.py      # 41 PASS

# 真机（先配凭据）
# ① 复制 tests/live/env.example -> tests/live/.env（gitignored）填真值；或直接 export
# ② 每个 live 脚本从 env 读凭据；缺失时打印"需配置 PYAISSH_TEST_*"并跳过
python tests/live/test_sudo.py
```

## 被测目标覆盖（测新版本/重构产物）

```bash
PYAISSH_PY=<dev built 或任意 pyaissh.py 路径> python tests/unit/test_regression.py
```

## 加新测试（工具加功能时）

1. unit：`tests/unit/test_xxx.py`，import `tests/_load.py` 的 `pyaissh()` 测真实函数
2. live：`tests/live/test_xxx.py`——凭据一律 `os.environ.get("PYAISSH_TEST_*")`，**禁止硬编码**
3. 运行确认 → 提交（连同工具改动）

## 凭据键（tests/live/env.example）

```
PYAISSH_TEST_HOST=root@<ip>       # 目标机（root）
PYAISSH_TEST_PASSWORD=<pwd>       # root 登录密码
PYAISSH_TEST_SUDO_USER=tester     # sudo 测试用户（需配 sudoers）
PYAISSH_TEST_SUDO_PASSWORD=<pwd>  # sudo 用户登录密码
PYAISSH_TEST_SUDO_NP_USER=tester_np   # NOPASSWD 测试用户
PYAISSH_TEST_SUDO_NP_PASSWORD=<pwd>
```

## 原则（为什么这样）

- **测真实函数不测复制品**：单元测 import 的 pyaissh 行为（如 warn_sensitive_cmd），不复制正则/逻辑——复制品会随代码漂移失真（历史教训：gate/assembly 单测是复制型，已不迁移）
- **live 才是行为真源**：sudo 门控/组装等最终由 live 真机断言（T6/T7/T12）
