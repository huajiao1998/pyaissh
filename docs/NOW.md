# NOW — 当前接力棒

更新时间：2026-10-17 21:25:23 +08:00（本机 Get-Date 核时）
对应产品：pyaissh **2.5.4**（工作区已含**未发版**的改动，见「下一步」第一条）
对应 HEAD：**b513cbe**
已发布：**v2.5.4**（2026-10-17，https://github.com/huajiao1998/pyaissh/releases/tag/v2.5.4）
工作区：干净（除他人文件，见文末）

## 授权

- 本地实现、定向断言、`build_single.py dist/check`、`sync_check.py`、明确文件本地提交。
- **不含**：push、建 Release（须用户亲口说「发」）、动 `tavern-ops/` 等他人目录。

## 禁止

- 未经「发」push / 建 Release（AGENTS.md §0 红线）。
- 改 `pyaissh.py` 本体——它是产物，源在 `pyaissh-dev/domains/`。
- 通配删除 `stest_tmp/`（多会话共享，只删自己按名字建的文件）。
- 把口令/token 写进任何文件（IP 可以写）。

## 停点

- v2.5.4 已发布，产品与制品一致（zip sha256 `128f6537…`）。
- 本轮之后无进行中的后台任务、无子代理、无未完成的编辑。

## 下一步（待用户指令，不自动执行）

- **⚠ 下次发版必须记一笔（用户 2026-10-17 定「攒到下次，改完一波再一起发」）**：
  `skills/pyaissh/docs/session.md` 补齐 4 个字段文档后，**制品内容已变但 VERSION 仍是无改动的 2.5.4**。
  下次发版要：三处 CHANGELOG 加一条（字段文档补齐 + 字段文档覆盖闸）、`01_globals.py` 的 VERSION
  升到 2.5.5、本文件「对应产品」同步。**别漏**——这正是当初想 bump 的理由。
- ~~契约可判定性~~ → **已判定不修**（实时观测工具，输出非数据资产，无跨版本比对消费者；详见 `MEMORY.md`「待决/未完成」）。别当 TODO 捡。
- 可选：`live_sudo` 覆盖缺口补齐（见「未验证」）。
- 可选：`tests/CHANGELOG.md` 里记录的 ~20-30s 安全提速项（6 处冗余 kill、`203.0.113.250`→`127.0.0.1:1` 等）。

## 未验证 / 已知缺口

- **`live_sudo` 全量 SKIP**：缺 `PYAISSH_TEST_SUDO_USER/PASSWORD` + `PYAISSH_TEST_SUDO_NP_USER/PASSWORD` 四个 env（测试机未建对应用户）⇒ `--sudo` 正向路径未被全量覆盖。
- 发布前全量 12 分钟量级；最近一轮（v2.5.4）为 ALL PASS + 1 SKIP。

## 他人文件（不碰、不提交）

- `M .gitignore`、`?? .zcodeignore`、`?? MEMORY.md`、`?? TAVERN-MOVED.md`、`?? .routercode/`
