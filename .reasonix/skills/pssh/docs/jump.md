# pyaissh 跳板机（子文档）

> 这是 pyaissh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文只讲跳板机（`--jump`）的完整语义。
> **何时读**：需要通过跳板机连接目标主机、跳板报错（`jump_failed` 或跳板认证/连接失败）时。

## 用法

```bash
python3 pyaissh.py exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-password 'xxx' --cmd 'hostname'
python3 pyaissh.py exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-key ~/.ssh/id_rsa --cmd 'hostname'  # 跳板用密钥
python3 pyaissh.py exec root@10.0.0.5 --jump @bastion --cmd 'hostname'   # 跳板也支持 @别名（PSSH_HOST_BASTION）
```

- 跳板机凭据优先级：`--jump-key` / `PSSH_JUMP_KEY` > `--jump-password` / `PSSH_JUMP_PASSWORD` > **`PSSH_PASSWORD` 回落（v1.4.9 起，仅密码；密钥不回落——错误的 `PSSH_KEY` 会短路原本可用的默认密钥路径）** > 默认私钥 > agent；回退生效时 stderr 打 `[JUMP] ... 密码回退使用 PSSH_PASSWORD`；`--jump @别名` 复用 `PSSH_HOST_<名称>` 配置及其专属凭据（配了别名凭据时抑制 `PSSH_JUMP_*` 与 `PSSH_PASSWORD` 回落）
- `--jump` 里不写用户名（如 `--jump 1.2.3.4:2222`）时回退用**目标机用户名**（与 `-u`/`PSSH_USER` 同源）
- 跳板机自身认证/连接失败的错误 message 带 `[跳板机 user@host]` 前缀（注意：错误 JSON 的定位字段 `host`/`user`/`port` 仍是目标机，需靠 message 前缀区分失败对象）
- 跳板 + 大文件并行分片下载：只建 **1 条**跳板 SSH，k 个分片各开一条 direct-tcpip 隧道共享它
