# NOW — 当前接力棒

更新时间：2026-10-23 17:54:43 +08:00（本机 Get-Date 核时）
对应产品：pyaissh **2.5.5**（已构建，发布中）
对应 HEAD：**fbe209c**
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

- **v2.5.5 内容已定稿**（用户 2026-10-23 说「发」）：九项修复/修改已进 CHANGELOG 末尾，VERSION →
  2.5.5、三处 CHANGELOG md5 一致、dist/金标/13 副本全绿。发布走 `stest_tmp/release/go.py`。
  本轮之后**欠账清零**，下次发版重新累计。
- ~~字段类型表 / 发版附字段集清单~~ → **已判定不修**（用户 2026-10-17 拍板）。两者都是"数据资产契约"的
  做法：我们分发的是可读源码、每次重连即可重跑、AI 上下文本就临时、每次使用都要读文档。真正的轴是
  "**文档与实际一致**"，不是"字段跨版本稳定"。**别当 TODO 捡**（理由同下一条）。
- ~~`log --offset` 静默截断~~ → **已修**（`293c3a1`）：越界时 stderr `[WARN]` + `warnings[]` 双通道，
  不加字段不加 flag（`log_bytes` 本来就有）。外部对端提的三条里，只有这条落在"保真"轴上。
- ~~契约可判定性~~ → **已判定不修**（实时观测工具，输出非数据资产，无跨版本比对消费者；详见 `MEMORY.md`「待决/未完成」）。别当 TODO 捡。
- 可选：`live_sudo` 覆盖缺口补齐（见「未验证」）；`tests/CHANGELOG.md` 里记录的 ~20-30s 安全提速项（6 处冗余 kill、`203.0.113.250`→`127.0.0.1:1` 等）。

## 未验证 / 已知缺口

- **`live_sudo` 全量 SKIP**：缺 `PYAISSH_TEST_SUDO_USER/PASSWORD` + `PYAISSH_TEST_SUDO_NP_USER/PASSWORD` 四个 env（测试机未建对应用户）⇒ `--sudo` 正向路径未被全量覆盖。
- 发布前全量 12 分钟量级；最近一轮（v2.5.4）ALL PASS + 1 SKIP。

## 他人文件（不碰、不提交）

- `M .gitignore`、`?? .zcodeignore`、`?? TAVERN-MOVED.md`、`?? .routercode/`（`MEMORY.md` 已于 10-17 入库）
