# pyaissh

**给 AI 用的结构化 SSH 工具 — A structured SSH tool built for AI agents**

裸 `ssh` 给 AI 用有四个坑：

> ① **输出不可解析**——人类文本要正则猜，AI 解析又慢又错
> ② **会无限卡死**——ssh 默认无超时，AI 调它等于把 agent 挂死
> ③ **传文件留半截**——中断后目标文件损坏，`--skip-existing` 还按大小误判
> ④ **传输内容烧爆 LLM 上下文**——大文件内容塞回给 AI，token 直接爆炸

pyaissh 把 SSH 变成 **AI 可精确消费的结构化工具**：stdout 恒为单行 JSON，三重超时防挂死，`.part` 原子传输 + 断点续传，文件内容零回传。

Raw `ssh` has four pain points when used by AI agents: **unparseable output**, **infinite hangs**, **half-written files on interrupt**, and **file contents blowing up the LLM context**. pyaissh turns SSH into a **structured tool AI can consume precisely**: single-line JSON stdout, triple timeout protection, atomic + resumable transfers, and zero file content in the context.

```bash
pyaissh exec root@1.2.3.4 --cmd 'uname -a'
pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
pyaissh upload root@1.2.3.4 --local big.bin --remote /tmp/big.bin --parallel 8   # 高丢包/长 RTT 链路分片上传（收益随链路而定）
pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8
pyaissh test root@1.2.3.4
pyaissh ls root@1.2.3.4 --path /etc --long
```

## 为什么给 AI 用 / Why for AI

| 能力 | 说明 | Capability |
|---|---|---|
| 🧭 结构化契约 | stdout 恒单行 JSON，直接 `json.loads`；24 类错误类型 + `retryable` 机器可读重试建议 | Structured contract: single-line JSON + typed errors with machine-readable retry hints |
| 🛡 防挂死 | 三重超时（静默/总时长/看门狗）——AI 调它永远不会卡死 | Triple timeout protection — never hangs |
| 🔄 可靠传输 | `.part` 原子写 + `--resume` 断点续传 + **并行分片下载/上传**（`--parallel 1-8`）+ `file_list` 断点重试 | Atomic transfer + resumable upload/download + **parallel-sharded upload & download** + retryable file lists |
| 🔋 零 token 传输 | 文件内容从不回传 JSON——AI 只消费元数据，大文件不烧上下文 | Zero-token transfer: file content never enters the LLM context |
| ⚡ 快速启动 | paramiko 惰性 import——错误路径启动 296ms → 110ms | Lazy import: error paths start 2.7× faster |
| 🔗 网络能力 | 跳板机（共享隧道）、主机别名（@名称）、IPv6 | Jump hosts, host aliases, IPv6 |
| 🖥 跨平台 | Windows / Linux / macOS（含 Git Bash 路径转换） | Cross-platform incl. Git Bash path handling |

## 零 token 传输 / Zero-token transfer

**这是 pyaissh 与 MCP-SSH 生态最大的差异**：很多 MCP SSH server 会把传输内容塞进 tool 结果回给 LLM——传 1GB 文件 = 烧爆上下文。pyaissh 的 upload/download **内容从不回传**，AI 只消费元数据（实测 1MB 随机文件传输后，结果 JSON 仅 410 字节）。

This is pyaissh's biggest edge over the MCP-SSH ecosystem: many MCP SSH servers stuff file contents into tool results — a 1GB transfer blows up the context. pyaissh **never echoes file content**; AI only consumes metadata (measured: a 1MB binary transfer returns a 410-byte JSON).

| 场景 | 裸 ssh | MCP-SSH 生态 | **pyaissh** |
|---|---|---|---|
| 输出 | 人类文本 | 常混入内容 | **单行 JSON** |
| 1GB 文件传输后上下文 | — | 烧爆 | **410 字节元数据** |
| 卡死 | 会 | 看实现 | **三重超时** |
| 传大文件中断 | 半截文件 | 半截文件 | **原子写 + 断点续传** |

## 输出示例 / Output example

```json
{"ok": true, "action": "exec", "version": "1.5.8", "exit_code": 0, "exit_success": true,
 "stdout": "Linux\n", "stdout_bytes": 7, "stdout_truncated": false, "warnings": []}
```

错误 JSON 恒带 `retryable`（bool）：AI 自动重试策略直接读它，不必解析文本。

Error JSON always carries `retryable` (bool) so AI retry policies can decide without parsing prose.

## 3 步跑起来 / Get started in 3 steps

```bash
# 1. 依赖
pip install paramiko
# 2. 下载（任选）
git clone https://github.com/huajiao1998/pyaissh.git        # 或 Releases 下载技能 zip
# 3. 跑
./pyaissh test root@1.2.3.4
```

**Agent 安装（零人工）**：把下面这段提示词复制给任意 AI 会话，AI 会自动完成下载、放置、装依赖、验证：

```text
帮我安装 pyaissh 技能（给 AI 用的结构化 SSH 工具），步骤：
1. 下载最新技能包：从 https://github.com/huajiao1998/pyaissh/releases
   下载 pyaissh-skill-v*.zip（或 git clone https://github.com/huajiao1998/pyaissh
   后取 skills/pyaissh/ 目录）
2. 解压得到 pyaissh/ 目录（含 SKILL.md、docs/、pyaissh.py、pyaissh.cmd、pyaissh），
   放到我的智能体技能目录（如 ~/.dsh/skills/pyaissh/ 或对应 agent 的技能路径约定）
3. 安装依赖：pip install paramiko（已装则跳过）
4. 验证：运行 python3 <技能目录>/pyaissh.py --version，
   应输出单行 JSON 且含 "version": "1.5.8"（或最新版本号）
5. 确认 SKILL.md 的 frontmatter（name: pyaissh）能被技能加载器识别
遇到报错先查仓库 README 的安装说明或 CHANGELOG。
```

**For agents**: copy the block above into any AI session — the agent downloads the skill package, places it into the skill directory, installs `paramiko`, and verifies, all by itself.

凭据：`--password` / `--key` 或环境变量 `PYAISSH_PASSWORD` / `PYAISSH_KEY` 等（`.env` 样例见 `.env.example`）。

Credentials: `--password`/`--key` or env vars `PYAISSH_PASSWORD`/`PYAISSH_KEY` (see `.env.example`).

## 使用场景 / Real-world usage

**AI 巡检 10 台机器 + 断点续传 2GB 日志**（shell 调用序列）：

```bash
# 巡检 10 台机器
for host in 10.0.0.{1..10}; do
  pyaissh test root@$host                    # 1. 连通性 + 系统信息（单行 JSON）
  pyaissh ls root@$host --path /var/log      # 2. 列目录（JSON entries）
  pyaissh exec root@$host --cmd 'df -h'      # 3. 执行命令（exit_success + stdout）
done
pyaissh download root@10.0.0.1 --remote /var/log/big.log --local . --resume   # 4. 断点续传 2GB 日志
# 5. 中断/失败重试：错误 JSON 的 retryable + file_list 精确续传，md5 复核
```

## 文档 / Docs

- 完整 SKILL 文档（含契约、错误类型、传输语义）：`skills/pyaissh/SKILL.md` + `skills/pyaissh/docs/`
- 更新日志：`CHANGELOG.md`
- 命令详情：`pyaissh --help`

Full skill docs: `skills/pyaissh/SKILL.md` + `skills/pyaissh/docs/`. Changelog: `CHANGELOG.md`.

## 技能包 / Skill Package

`skills/pyaissh/` 是标准技能包：`SKILL.md`（frontmatter + 完整文档）+ `docs/` + `pyaissh.py` / `pyaissh.cmd` / `pyaissh`（三平台入口）+ `.env.example`。

**安装到你的 agent**：把 `skills/pyaissh/` 下的内容拷贝到你的智能体技能目录即可（各智能体技能路径约定不同，如 OpenClaw / Hermes Agent / QwenPaw 各有自己的目录）——技能本体与路径无关，任何能加载 `SKILL.md` 的 agent 都能用。**也可以直接从 [Releases](https://github.com/huajiao1998/pyaissh/releases) 下载 `pyaissh-skill-vX.Y.Z.zip`**（完整技能包，解压出 `pyaissh/` 目录拷入即可）。

`skills/pyaissh/` is a standard skill package: `SKILL.md` (frontmatter + full docs) + `docs/` + platform entry points + `.env.example`. **Install into your agent**: copy its contents into your agent's skill directory — the skill works regardless of path, any agent that loads `SKILL.md` can use it. You can also grab the **`pyaissh-skill-vX.Y.Z.zip` from [Releases](https://github.com/huajiao1998/pyaissh/releases)** (full skill package, unzip to a `pyaissh/` folder and copy it in).

## 工程可信度 / Engineering rigor

每个版本都经**真实服务器**验证（真机执行 + md5 校验 + 中断/超时/信号测试），验证记录见 `CHANGELOG.md`（如 v1.5.6 retryable 11 例、v1.5.8 上传分片 50MB md5 一致）。

Every release is verified against **real servers** (real execution + md5 checks + interrupt/timeout/signal tests); verification records are in `CHANGELOG.md`.

## License

[MIT](LICENSE)
