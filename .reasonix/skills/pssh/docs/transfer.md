# pssh upload / download — 传输文件（子文档）

> 这是 pssh skill 的**子文档**（位于技能目录 `docs/` 子目录下，按需读取、不随 SKILL.md 自动注入）：契约（stdout 单行 JSON / 退出码 / error 类型）以 SKILL.md 为准，本文只讲传输的完整语义。
> **何时读**：upload/download 出错（`upload_failed`/`download_failed`/`*_timeout`）、大文件并行下载、断点重试、dry-run/skip-existing、符号链接/特殊文件处理时。

## 用法

```bash
python3 pssh.py upload root@1.2.3.4 --local ./dist --remote /opt/app/dist
python3 pssh.py upload root@1.2.3.4 --local f.txt --remote /tmp/dir    # 远端是目录 = 放入（scp 语义）
python3 pssh.py download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
python3 pssh.py download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8   # --local . 可用（scp 语义）；高丢包/跨境链路大文件提速
python3 pssh.py download root@1.2.3.4 --remote '~/.bashrc' --local ./bashrc        # ~ 自动展开
python3 pssh.py upload root@1.2.3.4 --local ./dist --remote /tmp/x --dry-run  # 先预览清单
python3 pssh.py upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing  # 跳过已存在且大小一致的文件
```

- **路径语义（scp 风格）**：远端路径支持 `~` 与 `~/` 展开（实际落点回显在结果的 `remote` 字段）；**不支持通配符**（SFTP 无 glob，含 `*?[` 的路径 download/ls 报专属 bad_args 提示、upload 入口直接拒绝——请先 ls 拿明确文件名）；传目录时源目录的**内容**放入目标目录下（不额外嵌套一层）；单文件传到已存在目录 = 放入该目录（download 的 `--local .` 同理）；**远端路径以 `/` 结尾 = 意图是目录**：目标不存在或已存在但非目录时明确报 `bad_args`（绝不静默创建/覆盖同名文件），目标是已存在目录时放入其中
- **大文件并行分片下载**：≥8MB 自动 4 连接；`--parallel 1-8` 显式控制（显式给出时 ≥64KB 即并行）；**实际档位见结果 `parallel_used` 字段**。单条 TCP 流在高丢包/长 RTT 链路吞吐塌陷（实测单流 ~20KB/s，8 连接 ~6 倍提速），下载慢或 `download_timeout` 就加 `--parallel 8`（不行反试 4/2）。经跳板机时共享一条跳板连接开多条隧道（连接数 = 1 跳板 + k 分片）
- **原子性（所有传输路径）**：下载（串行/目录/并行）先写进程唯一的 `<目标>.part.<pid>` 成功后原子改名——并发写同一目标时不会互相污染（完整的一方胜出）；上传先传远端 `.part.<pid>` 再 posix-rename 覆盖（服务器不支持该扩展时退化为删除+改名并 WARN，回退前有守卫防误删）——失败/中断不留"看似完整实则损坏"的半截最终文件。**回退改名也失败时的双丢防护**：旧文件已删、改名又失败的极端场景下**保留 `.part` 不清理**（它是新数据的唯一副本），错误消息明确指出数据保留路径、可手动恢复或重试——绝不会"旧文件没了、新数据也丢"。**上传被信号中断时远端 `.part.<pid>` 可能清不掉**（连接已坏，物理上无法删除）——会重试并**在 `warnings` 里明示残留路径及清理命令**（`rm -f '<残留路径>'`；批量清可用 `find <目标目录> -name '*.part.*' -delete`）；尚未开始写入就失败的（如权限不足）不误报残留。**硬杀**（SIGKILL / Windows terminate / 断电）不触发任何清理，本地与远端都可能留 `.part.<pid>` 孤儿（无自动回收，手动清理安全）
- **字节字段**：`bytes` = 清单总大小（含 skipped，失败时也如实反映），`bytes_transferred` = 实际传输字节（skip-existing 全跳过时为 0；dry-run 恒 0；**上传中断/失败时反映回调记账的真实已传字节**——实测跨境 50MB 传 8 秒被中断报 13139968，据此判断断点而非误判"全没传"再全量重传）
- **`--skip-existing` 仅比大小**（`st_size`，不比内容/时间戳）：跳过的文件**假定完整**——pssh 原子传输（`.part` + 原子改名）保证**自己产生的最终文件必完整**（硬杀时只留 `.part` 孤儿、无最终名），"大小一致内容损坏"只能来自外部（用户自放/磁盘损坏）。若担心外部文件损坏，skip 后可 `md5sum` 抽查
- **目录下载不跟随符号链接**：symlink 条目一律跳过并进 `warnings`（悬空/指向目录的链接跟随会中止整个目录且报错无法理解，lstat 尺寸还会让记账失真）；需要链接指向的内容，请对具体路径单独 download。**FIFO/套接字/设备文件同样跳过 + WARN**（服务端 open FIFO 会阻塞挂死）。**空目录也会重建**（结构完整到达）。**`--parallel` 对目录下载不生效**（逐文件串行，会 WARN 提示）
- `file_list` 每项含 `transferred`/`skipped` 状态：失败/中断时 AI 可精确断点重试
- 目录自动递归，远程目录自动创建；download 保留远程权限位（**setuid/setgid/sticky 特殊位会被掩掉**，防止 root 下载 4755 文件在本地造出提权落点）；跳过不可读目录会进 `warnings`（注意 `file_list` 缺项）
- `file_list` 的 `path`：upload 是**本地**路径，download 是**远程**相对路径（两侧语义不同，勿混用）；`--no-recursive`：只创建目录壳（本地/远程空目录），不传输任何子项
- 目录 upload 到已存在同名远程**文件**报 `bad_args`（"已存在且不是目录"）
- **断点续传（`--resume`，仅单文件）**：中断/失败后**保留续传点**（上传=远端 `.part`，下载=本地 `.part`），重试加 `--resume` 从断点继续，不重传已传部分。**默认不启用**（不加时中断仍清理 `.part`，行为与旧版完全一致）；单文件 ≥ 50MB（常量 `RESUME_MIN_SIZE`，可调）且未启用时，stderr `[TIP]` + 结果 `warnings` 会提示建议启用。要点：
  - **基于大小的续传**（与 `--skip-existing` 同信任级别）：`.part` 已有 N 字节就从 N 继续；**`.part` 大小 ≥ 源文件视为损坏/过时 → 覆盖重传**（绝不续坏尾巴）；完成后大小校验 + 原子改名
  - `.part` 用**固定名**（`<目标>.part`）而非进程唯一名——`--resume` 模式**禁止并发写同一目标**（两进程互续会污染出混合文件）
  - 下载分片（`--parallel`）续传：每分片完成后写 `<目标>.part.done.<i>` 标记，重试时**跳过已完成分片**（省传输量），全部完成后清理标记并原子改名
  - 极端损坏场景：续传后建议 `md5sum` 复核（大小校验不保证内容完全一致）；中断后的续传点若要放弃，手动删除 `<目标>.part`（上传为远端）
  - 目录传输忽略 `--resume`（打 WARN 提示，逐文件语义保持现状）
- 大文件/目录用 upload/download，**exec 只适合文本输出**（二进制内容会被 `errors="replace"` 损坏且无提示）
- 传输前用 `ls` 确认远程路径存在；覆盖语义 + `.part` 原子收尾，中断后全量重试安全
