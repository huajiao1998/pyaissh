# 统一入口：无会话上下文接手 pyaissh

更新时间：2026-10-17 18:35:24 +08:00（文档结构版本，**不代表产品状态**）
产品状态以 [NOW](<NOW.md>) 为准；规则以 [AGENTS](../AGENTS.md) 为准。

## 1. 三分钟接手顺序

1. 显式读 [AGENTS](../AGENTS.md)（§0 红线 / §1 构建 / §3 测试纪律 / §4 发布）——自动注入不代替阅读。
2. 读 [NOW](<NOW.md>)：当前版本、HEAD、已发布到哪、本轮授权与禁止、停点、下一步。
3. 核实际工作区：`git log -1 --format='%h %ci %s'`、`git status --porcelain`。
   **NOW 写的 HEAD 必须等于实际 HEAD**（有断言看守，不一致会 FAIL）。
4. 复述：版本 / HEAD / 已发布版本 / 本轮能做什么不能做什么 / 未验证项 / 一个下一步。
5. 用户没说「发」就不要 push、不要建 Release。文档是许可记录，**不创造授权**。

## 2. 权威落点（按需读，别通读）

| 需要什么 | 去哪里 | 读到哪为止 |
|---|---|---|
| 红线、禁令、授权边界 | [AGENTS](../AGENTS.md) §0 | 全读（短） |
| 构建/同步管线 | [AGENTS](../AGENTS.md) §1 | 全读 |
| 测试纪律、块名、跑法 | [AGENTS](../AGENTS.md) §3 | 全读 |
| 发布通道与制品预检 | [AGENTS](../AGENTS.md) §4 | 全读 |
| 当前版本 / HEAD / 停点 / 授权 | [NOW](<NOW.md>) | 全读（≤50 行） |
| 产品变更史 | [CHANGELOG](../CHANGELOG.md) | 只看顶部条目 |
| 测试体系变更史 | [tests/CHANGELOG](../tests/CHANGELOG.md) | 只看顶部条目 |
| 踩过的坑与决策 | [MEMORY](../MEMORY.md) | 按标题定位，不通读 |
| 给使用者看的用法 | [SKILL](../skills/pyaissh/SKILL.md) + `skills/pyaissh/docs/` | 按子命令 |
| 域源与构建器 | `pyaissh-dev/domains/`、`pyaissh-dev/build_single.py` | 改哪读哪 |

## 3. 状态词义（别混用）

- **已实现** = 代码在，有断言。
- **已验证** = 有真机/离线实测回执，且代码之后没变。
- **已发布** = git 推到远端 + Release 建好 + 远端资产实测通过。
- 三者不能互相替代：本地通过 ≠ 页面可用 ≠ 已发布。

## 4. 检查

```bash
python -u tests/run_tests.py --artifacts     # 含交接文档闸（NOW 版本/HEAD/行数、START 链接）
python -u pyaissh-dev/build_single.py check  # 金标逐字节
python -u pyaissh-mcp/sync_check.py          # 13 副本 md5
```

`--artifacts` 里与交接相关的断言：`NOW.md ≤50 行`、`NOW 版本 == VERSION`、
`NOW HEAD == git rev-parse --short HEAD`、`START.md 相对链接均存在`。

## 5. 跨会话启动语

> 先读 docs/START.md、AGENTS.md §0、docs/NOW.md，核实际 git；
> 复述版本、HEAD、已发布版本、本轮授权与禁止、未验证项、一个下一步。
> 用户没说「发」就不 push、不建 Release。
