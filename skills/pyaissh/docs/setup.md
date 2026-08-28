# pyaissh 前置条件完整规则（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：SKILL.md 的"快速开始"只留调用方式与一句话规则，本文是目标格式/别名/凭据/`.env` 的**完整**参考。
> **何时读**：配置主机别名、凭据优先级疑问、`.env` 加载行为、IPv6/端口解析细节、凭据安全实践时。

## 目标格式与主机别名

- 目标格式：`[user@]host[:port]`，如 `root@1.2.3.4:22`；支持 IPv6：`user@[2001:db8::1]:22`、`[2001:db8::1]`、裸 IPv6 地址；**主机别名**：`.env` 配 `PYAISSH_HOST_<名称>=user@host:port`（如 `PYAISSH_HOST_PROD=root@1.2.3.4:22`），target 写 `@名称` 即可引用（如 `pyaissh test @prod`；**键名整体大小写不敏感**：`PYAISSH_HOST_PROD` / `pyaissh_host_prod` / `PYAISSH_host_prod` 都能命中，Linux 与 Windows 行为一致）；**显式 `-p/--port` 优先于 target/别名内嵌端口**（与 ssh 惯例一致，写 `-p` 通常就是想纠正 target 里的端口）

## 凭据（认证优先级）

**凭据安全实践（v1.5.8，dogfood 实测）**：**不要把 token/密码内联进 `--cmd` 或脚本内容**——`cmd` 字段会原样回显命令（含凭据需脱敏），触发凭据 WARN；正确姿势：凭据走 `--password`/`--key` 参数、`PYAISSH_*` 环境变量、`.env`，或脚本从文件读取（`cat /path/secret`，文件传输内容不进 JSON）。发布类操作（git push token URL 等）同理：token 写文件让脚本 `cat`，不要写进命令文本。

凭据：`--password` / `--key` 参数，或环境变量 `PYAISSH_USER` / `PYAISSH_PORT` / `PYAISSH_KEY` / `PYAISSH_PASSWORD`（也可写同目录 `.env`）；**sudo 提权密码**：`PYAISSH_SUDO_PASSWORD`（exec `--sudo` 用，v1.5.15 起；`--sudo-password` 参数优先，空串视为未设置→sudo -n 免密探测；密码只经 SSH stdin 注入不进命令/日志，样例见 `.env.example`）。**认证优先级**：`--key` / `PYAISSH_KEY` > `--password` / `PYAISSH_PASSWORD` > 默认私钥 `~/.ssh/id_ed25519` > ssh-agent 兜底（仅当以上都没有时）——**注意：显式传 `--password` 也会被 `PYAISSH_KEY`/`--key` 静默压过（key 优先）**；**`--key` 指定的私钥文件不存在时直接报 `auth_failed`，不会回退密码**；**别名专属凭据**：`PYAISSH_HOST_<名称>_KEY` / `PYAISSH_HOST_<名称>_PASSWORD` 优先级介于显式参数与全局 env 之间，且**别名配了任一专属凭据时该主机不再取全局 `PYAISSH_KEY`/`PYAISSH_PASSWORD`**（别名主机凭据完全由别名决定，避免全局 key 抢先导致别名密码永远轮不到）；跳板机凭据同样支持环境变量 `PYAISSH_JUMP_KEY` / `PYAISSH_JUMP_PASSWORD`（**v1.4.9 起：跳板密码在两者都未配置时自动回退使用 `PYAISSH_PASSWORD`，密钥无此回落**）；完全无凭据时报 `auth_failed` 且 message 明确提示缺 `--password`/`PYAISSH_PASSWORD` 或 `--key`/`PYAISSH_KEY`

## 凭据安全

- 命令含疑似凭据（如 `mysql -p'xxx'`、`DB_PASS=...`）时 pyaissh 会在 stderr 打 WARN——照常执行，但注意日志可能泄露敏感信息，**敏感凭据用远程环境变量注入**
- **远程命令原文会打印到日志**（超长截断、终端转义序列被替换为 `<ESC>`），避免在命令里内嵌长期凭据

## `.env` 加载规则（供应链安全）

- **配置样例见技能目录 `.env.example`**（`cp .env.example .env` 后按注释填写即可；人类/AI 均可照样例配置主机、密码、密钥、端口、跳板）
- 环境变量优先（`.env` 不覆盖已存在的）；**默认只加载脚本目录 `.env`**（用户主动放入 pyaissh 工具目录的文件）
- **工作目录 `.env` 默认不加载**——恶意仓库可自带 `.env` 注入 `PYAISSH_HOST_*` / `PYAISSH_PASSWORD` 把 AI 的 SSH 连接导向攻击者主机（钓鱼 SSH），需显式设 `PYAISSH_ALLOW_CWD_ENV=1`（环境变量或脚本目录 `.env` 中）才加载 cwd `.env` 并打 WARN
- 支持行内注释（`KEY=value # comment`，`#` 前需有空格，**引号包裹的值内 `#` 不拆**：`KEY="a # b"` 值为 `a # b`，引号后也可跟注释）；文件编码 UTF-8（含 BOM 也能读）

## 环境依赖

- 环境有 `python3` + `paramiko`（`pip install paramiko`；Debian/Ubuntu 服务器也可 `apt install python3-paramiko`）
- 调用方式（`<pyaissh_dir>` 即技能目录 `skills/pyaissh`）：
  - Linux/macOS：`python3 <pyaissh_dir>/pyaissh.py <子命令> ...`
  - Windows cmd：`<pyaissh_dir>\pyaissh.cmd <子命令> ...`；Git Bash：`<pyaissh_dir>/pyaissh <子命令> ...`
- 若把 pyaissh 复制到了项目根目录，则按上述同目录规则调用
