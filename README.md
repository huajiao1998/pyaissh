# pyaissh

**给 AI 用的结构化 SSH 工具 — A structured SSH tool built for AI agents**

pyaissh 是基于 paramiko 的命令行 SSH 工具，专为非交互的 AI / 脚本使用设计。与裸调 `ssh` 不同，它的输出是**结构化、可精确解析**的：stdout 恒为单行 JSON。

pyaissh is a command-line SSH tool built on paramiko, designed for non-interactive AI/script usage. Unlike raw `ssh`, its output is **structured and precisely parseable**: stdout is always a single-line JSON.

```bash
pyaissh exec root@1.2.3.4 --cmd 'uname -a'
pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
pyaissh upload root@1.2.3.4 --local big.bin --remote /tmp/big.bin --parallel 8   # 慢链路大文件上传分片提速（v1.5.8）
pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8
pyaissh test root@1.2.3.4
pyaissh ls root@1.2.3.4 --path /etc --long
```

## 为什么给 AI 用 / Why for AI

| 能力 | 说明 | Capability |
|---|---|---|
| 🧭 结构化契约 | stdout 恒单行 JSON，直接 `json.loads`；24 类错误类型 + `retryable` 机器可读重试建议 | Structured contract: single-line JSON + typed errors with machine-readable retry hints |
| 🛡 防挂死 | 三重超时（静默/总时长/看门狗）——AI 调它永远不会卡死 | Triple timeout protection — never hangs |
| 🔄 可靠传输 | `.part` 原子写 + `--resume` 断点续传 + **并行分片下载/上传**（`--parallel 1-8`，v1.5.8 起上传也支持）+ `file_list` 断点重试 | Atomic transfer + resumable upload/download + **parallel-sharded upload & download** + retryable file lists |
| 🔋 零 token 传输 | 文件内容从不回传 JSON——AI 只消费元数据，大文件不烧上下文 | Zero-token transfer: file content never enters the LLM context |
| ⚡ 快速启动 | paramiko 惰性 import——错误路径启动 296ms → 110ms | Lazy import: error paths start 2.7× faster |
| 🔗 网络能力 | 跳板机（共享隧道）、主机别名（@名称）、IPv6 | Jump hosts, host aliases, IPv6 |
| 🖥 跨平台 | Windows / Linux / macOS（含 Git Bash 路径转换） | Cross-platform incl. Git Bash path handling |

## 输出示例 / Output example

```json
{"ok": true, "action": "exec", "version": "1.5.7", "exit_code": 0, "exit_success": true,
 "stdout": "Linux\n", "stdout_bytes": 7, "stdout_truncated": false, "warnings": []}
```

错误 JSON 恒带 `retryable`（bool）：AI 自动重试策略直接读它，不必解析文本。

Error JSON always carries `retryable` (bool) so AI retry policies can decide without parsing prose.

## 安装 / Install

```bash
pip install paramiko
# Linux / macOS
python3 pyaissh.py <子命令> ...
# Windows cmd
pyaissh.cmd <子命令> ...
# Git Bash
./pyaissh <子命令> ...
```

凭据：`--password` / `--key` 或环境变量 `PYAISSH_PASSWORD` / `PYAISSH_KEY` 等（`.env` 样例见 `.env.example`）。

Credentials: `--password`/`--key` or env vars `PYAISSH_PASSWORD`/`PYAISSH_KEY` (see `.env.example`).

## 文档 / Docs

- 完整 SKILL 文档（含契约、错误类型、传输语义）：`SKILL.md` + `docs/`
- 更新日志：`CHANGELOG.md`
- 命令详情：`pyaissh --help`

Full skill docs: `SKILL.md` + `docs/`. Changelog: `CHANGELOG.md`.

## 技能包 / Skill Package

`skills/pyaissh/` 是标准技能包：`SKILL.md`（frontmatter + 完整文档）+ `docs/` + `pyaissh.py` / `pyaissh.cmd` / `pyaissh`（三平台入口）+ `.env.example`。

**安装到你的 agent**：把 `skills/pyaissh/` 下的内容拷贝到你的智能体技能目录即可（各智能体技能路径约定不同，如 OpenClaw / Hermes Agent / QwenPaw 各有自己的目录）——技能本体与路径无关，任何能加载 `SKILL.md` 的 agent 都能用。

`skills/pyaissh/` is a standard skill package: `SKILL.md` (frontmatter + full docs) + `docs/` + platform entry points + `.env.example`. **Install into your agent**: copy its contents into your agent's skill directory — the skill works regardless of path, any agent that loads `SKILL.md` can use it.

## License

[MIT](LICENSE)
