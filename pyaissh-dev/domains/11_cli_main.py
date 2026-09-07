"""CLI 装配与入口（域 11）。

- PsshArgumentParser（--help 纯文本、子命令缺省结构化 bad_args）
- build_parser + add_conn（target/凭据/跳板参数，子命令共用注册）
- 参数类型校验（_port/_max_time/_exec_timeout/_positive_int/_encoding_type）
- 信号：_setup_signal_handlers（极早期注册语义在 00）、_signal_responder（救援线程）
- main()：解析 -> --field/--text 互斥 -> 子命令分发；极早期窗口标志在 00_head
"""

class PsshArgumentParser(argparse.ArgumentParser):
    """参数错误时也在 stdout 输出一行结构化 JSON（AI 可解析）。

    argparse 默认只把 usage/error 打到 stderr，stdout 无任何输出；
    参数写错的 AI 拿不到可解析的错误信息。error() 覆写后 stdout 始终
    有一行 {"ok": false, "error": "bad_args", "message": ...}，退出码仍为 2。
    （与全局一致：默认 JSON；命令行带 --text 时用可读包裹格式。）
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._in_error = False  # 防 parse_known_args 递归进入 error()

    def _want_text_mode(self):
        """判断错误输出是否用 --text 包裹格式。

        不能简单用 `"--text" in sys.argv`：--cmd '--text' 等参数值恰为
        --text 时会误判（argparse 错误输出被切成分段包裹格式，破坏
        "stdout 恒单行 JSON"契约）。改用 parse_known_args 提取真实解析
        结果；parse_known_args 内部再出错（如 --cmd 缺值）会递归调
        error()，由 _in_error 挡板拦截并回退默认 JSON（契约优先）。
        """
        if self._in_error:
            return False
        self._in_error = True
        try:
            ns, _ = self.parse_known_args()
            return not getattr(ns, "json", True)
        except SystemExit:
            return False
        finally:
            self._in_error = False

    def error(self, message):
        if self._in_error:
            # 递归入口（_want_text_mode 的 parse_known_args 内部再出错）：
            # 不打印不输出，由外层 error() 统一打印一次（否则 JSON 会
            # 重复输出两行，破坏"单行 JSON"契约）
            raise SystemExit(2)
        err = {"ok": False, "error": "bad_args", "message": "参数错误: %s" % message,
               "retryable": False}  # 参数错不可重试（重试同样失败）
        if not self._want_text_mode():
            # 默认 JSON 模式：整行 JSON（与成功路径一致，AI 直接 loads）
            print(json.dumps(err, ensure_ascii=False), flush=True)
        else:
            # 可读模式：与 emit_error 一致的 ---ERROR.<nonce>--- 包裹格式
            print("---ERROR.%s---" % _TEXT_NONCE, flush=True)
            print(json.dumps(err, ensure_ascii=False), flush=True)
            print("---END.%s---" % _TEXT_NONCE, flush=True)
        self.print_usage(sys.stderr)
        sys.exit(2)


def _positive_int(value):
    """argparse type：正整数校验（拒绝 0/负值，避免超时参数秒级失效）。
    int() 失败转 ArgumentTypeError：argparse 对 ValueError 只会报
    "invalid _positive_int value"（泄漏内部函数名且无自纠提示）。"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("必须为正整数（收到 %r，如 8）" % (value,))
    if v < 1:
        raise argparse.ArgumentTypeError("必须为正整数（>= 1）")
    return v


def _encoding_type(value):
    """argparse type：编码名校验（codecs.lookup，拼错立刻 bad_args/2，不连远端）。
    裸 str 会把错误拖到解码期 LookupError，被通用 except 误归 exec_failed 误导
    AI 查网络——解析期拦截才是"参数写错"的语义。"""
    try:
        codecs.lookup(value)
    except LookupError:
        raise argparse.ArgumentTypeError(
            "未知编码 %r（如 utf-8 / gbk / shift_jis / latin-1）" % (value,))
    return value


def _port(value):
    """argparse type：端口范围校验（1-65535）"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("端口必须为整数（收到 %r，如 22）" % (value,))
    if not 1 <= v <= MAX_PORT:
        raise argparse.ArgumentTypeError("端口必须在 1-%d 之间" % MAX_PORT)
    return v


def _max_time(value):
    """argparse type：--max-time 总时长上限校验（1-1200 秒）"""
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("总时长上限必须为整数秒（收到 %r，如 600）" % (value,))
    if not 1 <= v <= MAX_TIME_CAP:
        raise argparse.ArgumentTypeError("总时长上限必须在 1-%d 秒之间（构建/编译等长任务最高 %d）"
                                         % (MAX_TIME_CAP, MAX_TIME_CAP))
    return v


def _exec_timeout(value):
    """argparse type：--idle-timeout 静默超时校验（1-1200 秒）。

    上限与 --max-time 一致：静默窗口超过总时长上限时，总上限会先触发并
    把静默挂死误标成"持续输出超时"，两个参数的值域必须对齐。
    """
    try:
        v = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("静默超时必须为整数秒（收到 %r，如 60）" % (value,))
    if not 1 <= v <= MAX_TIME_CAP:
        raise argparse.ArgumentTypeError("静默超时必须在 1-%d 秒之间（与 --max-time 上限一致）"
                                         % MAX_TIME_CAP)
    return v


def build_parser():
    parser = PsshArgumentParser(
        prog="pyaissh",
        description="基于 paramiko 的命令行 SSH 工具（给 AI 用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  pyaissh exec root@1.2.3.4 --cmd 'uname -a'
  pyaissh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限（最高 1200）
  pyaissh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120      # 慢命令调大静默窗口
  pyaissh exec root@1.2.3.4 --cmd-file - <<'EOF'
  ls -la /var/log
  EOF
  pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
  pyaissh upload root@1.2.3.4 --local big.bin --remote /tmp/big.bin --parallel 8  # 高丢包/长 RTT 链路分片上传（收益随链路而定；v1.5.8）
  pyaissh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
  pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8   # --local . 可用（scp 语义）
  pyaissh test root@1.2.3.4
  pyaissh ls root@1.2.3.4 --path /etc --long --limit 500

跳板机 (--jump):
  pyaissh exec root@10.0.0.5 --jump root@1.2.3.4:2222 --jump-password 'xxx' --cmd 'hostname'

主机别名 (.env 配 PYAISSH_HOST_PROD=root@1.2.3.4:22 后):
  pyaissh exec @prod --cmd 'uname -a'
  # 别名专属凭据: PYAISSH_HOST_PROD_PASSWORD / PYAISSH_HOST_PROD_KEY

路径语义:
  远端路径支持 ~ 与 ~/（自动展开为绝对路径，实际路径回显在结果的 remote 字段）；
  不支持通配符（SFTP 无 glob，请先 ls 拿到明确文件名）。传目录时源目录的
  【内容】放入目标目录下（不额外嵌套）；单文件传到已存在目录 = 放入该目录。

输出: 默认纯 JSON（stdout 单行对象，stderr 为进度日志）；--text 切可读标记模式
      （标记带随机 nonce 防远程输出伪造；AI 程序化解析请一律用默认 JSON）
退出码: 0 成功 / 1 传输错误 / 2 参数错误 / 124 执行超时（远程进程可能仍在运行）/
        130 中断 / 255 连接失败；exec 透传远程退出码（远程恰为 255 时本地返 254）
字段: 结果均含 version/action/duration_ms/warnings；exec 另有 exit_success、
      stdout_bytes(原始接收字节)、stdout_truncated(该流是否截断，省略量见
      stdout_omitted_bytes)；upload/download 的 bytes=清单总大小、
      bytes_transferred=实际传输；ls 的 entries 含 mode/mtime(epoch 秒,UTC)/is_symlink
特性: 传输零 token 消耗——upload/download 的文件内容从不回传 JSON，AI 只消费
      元数据（files/bytes/file_list），大文件/二进制不会烧爆 LLM 上下文
环境变量 (.env 或系统): PYAISSH_USER / PYAISSH_PORT / PYAISSH_KEY / PYAISSH_PASSWORD /
                        PYAISSH_JUMP_KEY / PYAISSH_JUMP_PASSWORD / PYAISSH_HOST_<名称>
""",
    )
    parser.add_argument("--version", action="store_true",
                        help="输出版本（stdout 一行 JSON，保持 stdout 恒 JSON 契约）")
    # 默认 JSON 输出；--text 切可读模式。注册顺序有讲究：--text 先注册使默认值
    # (store_false -> True) 生效；--json 仅作兼容入口（显式给出时置回 True）。
    parser.add_argument("--text", dest="json", action="store_false",
                        help="可读文本模式（默认输出纯 JSON 供 AI 精确解析）")
    parser.add_argument("--json", dest="json", action="store_true",
                        help="输出纯 JSON（默认已是 JSON，保留兼容旧用法）")

    sub = parser.add_subparsers(dest="command", required=False, metavar="<子命令>")
    # 子命令不设 required：让 --version 可单独使用；缺子命令时在 main()
    # 给结构化 bad_args（stdout JSON），而非 argparse 的 stderr 纯文本

    def add_conn(p, target_help="目标 [user@]host[:port]"):
        p.add_argument("target", help=target_help)
        p.add_argument("-u", "--user", help="用户名 (env: PYAISSH_USER)")
        p.add_argument("-p", "--port", type=_port, help="端口 (env: PYAISSH_PORT, 默认 22)")
        p.add_argument("-k", "--key", help="私钥路径 (env: PYAISSH_KEY, 默认 ~/.ssh/id_ed25519)")
        p.add_argument("-P", "--password", help="密码 (env: PYAISSH_PASSWORD)")
        p.add_argument("--timeout", type=_positive_int, default=10, help="连接超时秒数 (默认 10)")
        p.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS,
                       help="输出纯 JSON（默认已是 JSON，保留兼容）")
        p.add_argument("--text", dest="json", action="store_false", default=argparse.SUPPRESS,
                       help="可读文本模式（默认 JSON）")
        p.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                       help="消费端字段提取（打印值，不打印 JSON）：--field stdout 打印裸值；"
                            "逗号分隔多字段每行一个；- 前缀字段打到 stderr（如 --field stdout,-stderr，"
                            "stderr 内容不被 stdout 展示吞掉）；与 --text 互斥；错误路径仍输出完整 JSON")
        p.add_argument("--strict", action="store_true", help="严格校验 host key (默认 auto-add)")
        # 跳板机参数
        p.add_argument("--jump", metavar="[user@]host[:port]",
                       help="跳板机地址，通过它隧道连目标 (如 root@1.2.3.4:2222)")
        p.add_argument("--jump-password", dest="jump_password",
                       help="跳板机密码 (跳板机认证独立于目标机)")
        p.add_argument("--jump-key", dest="jump_key",
                       help="跳板机私钥路径 (默认 ~/.ssh/id_ed25519)")

    # exec
    p = sub.add_parser("exec", help="执行远程命令",
                       description="执行远程命令。本地退出码 = 远程退出码；"
                                   "超时 124、连接失败 255、参数错误 2、中断 130。")
    add_conn(p)
    p.add_argument("--cmd", help="要执行的命令")
    p.add_argument("--cmd-file", dest="cmd_file",
                   help="从文件读命令 (- 表示 stdin，适合长脚本/特殊字符)")
    p.add_argument("--idle-timeout", dest="exec_timeout", type=_exec_timeout, default=60,
                   help="静默超时秒数：连续无输出超过该值即终止，默认 60，最高 1200 "
                        "（区别于 --max-time 总时长；输出少的慢命令调大这个）")
    p.add_argument("--progress", type=_positive_int, nargs="?", const=30, metavar="SECS",
                   help="长任务心跳（v2.1）：命令每静默/持续运行超过 N 秒（默认 30）往 stderr "
                        "打一行[PROGRESS]仍在运行——AI 知道进程活着不是挂死；"
                        "不重置静默计时（idle-timeout 仍按真实输出判定）")
    # 兼容别名：v1.3 前叫 --exec-timeout，名字容易被误当成"总超时"而用错
    p.add_argument("--exec-timeout", dest="exec_timeout", type=_exec_timeout,
                   default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p.add_argument("--max-time", dest="max_time", type=_max_time,
                   help="命令总时长上限秒数（wall clock；默认 2×idle-timeout 且至少 120，"
                        "不得小于 --idle-timeout；构建/编译等长任务请调大，最高 1200）")
    p.add_argument("--max-output", dest="max_output", type=_positive_int, default=DEFAULT_MAX_OUTPUT,
                   help="stdout/stderr 单流最大保留字节，超出保留头尾各一半 (默认 256KB)")
    p.add_argument("--no-credential-warn", dest="no_credential_warn", action="store_true",
                   help="关闭\"命令含疑似凭据\"的 WARN 提示（启发式误报时用；仍建议敏感凭据走环境变量注入）")
    p.add_argument("--sudo", action="store_true",
                   help="以 sudo 提权执行（普通用户登录时提权用）。有密码（--sudo-password 或 "
                        "PYAISSH_SUDO_PASSWORD）时组装 sudo -S -p '' bash -c '<cmd>' 并经 SSH "
                        "stdin 注入密码（命令文本不含密码）；无密码时 sudo -n bash -c '<cmd>'"
                        "（免密探测，需密码立即失败不挂）；复合命令（&&/||/;）整链提权；与 --pty 互斥")
    p.add_argument("--sudo-password", dest="sudo_password", default=None,
                   help="sudo 密码（优先于 PYAISSH_SUDO_PASSWORD 环境变量；空串视为未设置→sudo -n "
                        "免密探测；密码只经 SSH stdin 注入，不进命令文本/cmd 字段/日志/远端磁盘）")
    p.add_argument("--spill-dir", dest="spill_dir",
                   help="输出截断时把完整输出落盘的目录（默认系统临时目录；保留的文件路径见结果 "
                        "stdout_spill_file / stderr_spill_file 字段）")
    p.add_argument("--encoding", dest="encoding", type=_encoding_type, default="utf-8",
                   help="远端 stdout/stderr 的解码编码（默认 utf-8；GBK/Shift-JIS 等系统日志用 "
                        "--encoding gbk / shift_jis；非法字节以 U+FFFD 替换，不中断；拼错立即报错不连远端）")
    p.add_argument("--pty", action="store_true",
                   help="分配 PTY 伪终端运行：适用于需要 TTY 的非交互命令"
                        "(watch、top -b、sudo -n、检测 isatty 的脚本)；"
                        "注意 PTY 模式下 stderr 合并进 stdout；vi 等全屏交互程序不可用")
    p.add_argument("--pty-strip-ansi", action="store_true",
                   help="PTY 模式下剥离输出中的 ANSI 转义序列（颜色/光标），供 AI 干净解析")
    p.set_defaults(func=cmd_exec)

    # upload
    p = sub.add_parser("upload", help="上传文件/目录 (本地 -> 远程)",
                       description="上传本地文件或目录到远程。目录自动递归，远程目录自动创建。"
                                   "传目录时源目录的【内容】放入目标目录下（不额外嵌套一层）；"
                                   "单文件传到已存在目录 = 放入该目录（scp 语义）。")
    add_conn(p)
    p.add_argument("--local", required=True, help="本地路径")
    p.add_argument("--remote", required=True,
                   help="远程路径（支持 ~ 展开；不支持通配符）")
    p.add_argument("-r", "--recursive", action="store_true", default=None,
                   help="目录时递归传输 (默认自动递归)")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="对目录不递归：只创建远程目录壳，不传输子项")
    p.add_argument("--dry-run", action="store_true", help="只打印清单不实际传输")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="目标文件已存在且大小一致则跳过（幂等重传，失败重试不重复传）")
    p.add_argument("--exclude", metavar="GLOB[,GLOB...]",
                   help="目录递归时排除匹配项（v2.1）：逗号分隔 glob，命中文件名或相对路径"
                        "即整项跳过——目录整树剪枝、文件不上传不计数；"
                        "例：--exclude node_modules,.git,'*.log'")
    p.add_argument("--resume", action="store_true",
                   help="断点续传：中断后保留远端 .part，重试从断点继续（仅单文件；"
                        "续传点基于大小，极端损坏场景可下载后 md5sum 复核；"
                        "--resume 模式禁用并发写同一目标；与 --parallel 互斥）")
    p.add_argument("--parallel", type=_positive_int, choices=range(1, 9), metavar="1-8",
                   help="单文件分片上传连接数（1-8）：高丢包/慢链路大文件提速，"
                        "对称下载分片（默认单连接；显式给出时 ≥64KB 即分片；"
                        "与 --resume 互斥）")
    p.set_defaults(func=cmd_upload)

    # download
    p = sub.add_parser("download", help="下载文件/目录 (远程 -> 本地)",
                       description="下载远程文件或目录到本地。目录自动递归，本地目录自动创建。"
                                   "传目录时源目录的【内容】放入目标目录下（不额外嵌套一层）；"
                                   "--local 写已存在目录（如 .）= 文件放入该目录（scp 语义）。")
    add_conn(p)
    p.add_argument("--remote", required=True,
                   help="远程路径（支持 ~ 展开；不支持通配符，请先 ls 拿到明确文件名）")
    p.add_argument("--local", required=True, help="本地路径（已存在目录 = 放入该目录）")
    p.add_argument("-r", "--recursive", action="store_true", default=None,
                   help="目录时递归传输 (默认自动递归)")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="对目录不递归：只创建本地目录壳，不下载子项")
    p.add_argument("--dry-run", action="store_true", help="只打印清单不实际传输")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true",
                   help="本地文件已存在且大小一致则跳过（幂等重下，失败重试不重复传）")
    p.add_argument("--resume", action="store_true",
                   help="断点续传：中断后保留本地 .part，重试从断点继续（仅单文件；"
                        "续传点基于大小，极端损坏场景可下载后 md5sum 复核；"
                        "--resume 模式禁用并发写同一目标）")
    p.add_argument("--parallel", type=_positive_int, choices=range(1, 9), metavar="1-8",
                   help="大文件分片下载的并行连接数 (默认自动：≥8MB 用 4，实际值见结果 "
                        "parallel_used 字段；跨境高丢包链路可试 8——若 8 失败"
                        "（链路饱和/服务器 MaxStartups 限制）反试 4/2)")
    p.set_defaults(func=cmd_download)

    # test
    p = sub.add_parser("test", help="测试连接",
                       description="测试 SSH 连接，返回连接状态和服务器信息。")
    add_conn(p)
    p.add_argument("--encoding", dest="encoding", type=_encoding_type, default="utf-8",
                   help="服务器信息输出的解码编码（默认 utf-8；GBK 系统 os-release 等用 --encoding gbk）")
    p.set_defaults(func=cmd_test)

    # ls
    p = sub.add_parser("ls", help="列远程目录",
                       description="列出远程目录内容。上传/下载前可用 ls 确认路径。")
    add_conn(p, target_help="目标 [user@]host[:port]")
    p.add_argument("--path", default=".", help="远程路径 (默认 .；支持 ~ 展开)")
    p.add_argument("-l", "--long", action="store_true",
                   help="额外打印文本清单行（含 mtime；entries 字段两种模式恒同构）")
    p.add_argument("--limit", type=_positive_int, default=2000,
                   help="最多返回条目数 (默认 2000，超出截断并置 truncated=true)")
    p.set_defaults(func=cmd_ls)

    # host（v2.1）：主机别名管理——host add 把别名写进 .env；remove/list（v2.1.3）
    p = sub.add_parser("host", help="主机别名管理 (host add/remove/list)",
                       description="host add：把主机别名与专属凭据写进脚本同目录 .env，"
                                   "之后 pyaissh exec @NAME 直接使用（多主机不同密码不再"
                                   "逐条 --password）；remove/list 管理已配别名。")
    hsub = p.add_subparsers(dest="host_cmd", metavar="{add,remove,list}")
    ha = hsub.add_parser("add", help="添加/更新主机别名",
                         description="例: pyaissh host add prod root@203.0.113.10 --password xxx"
                                     "  → 之后 pyaissh exec @prod 使用别名凭据")
    ha.add_argument("name", help="别名（字母/数字/下划线，不区分大小写）")
    ha.add_argument("host_target", metavar="USER@HOST[:PORT]",
                    help="目标（必须带用户名，如 root@1.2.3.4:22）")
    ha.add_argument("--password", dest="password", default=None,
                    help="该主机专属密码（写 .env；不给则复用全局 PYAISSH_PASSWORD/私钥）")
    ha.add_argument("--key", dest="key", default=None,
                    help="该主机专属私钥路径（写 .env；与密码同时给时 KEY 优先）")
    ha.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field alias 得 @prod；dict/list JSON 序列化）"
                         "——host 与各子命令统一（v2.1.2）")
    ha.set_defaults(func=cmd_host_add)

    hr = hsub.add_parser("remove", help="删除主机别名（含专属密码/密钥）",
                         description="例: pyaissh host remove prod  → 从 .env 删该别名（含其专属密码/密钥行）")
    hr.add_argument("name", help="别名")
    hr.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field removed）")
    hr.set_defaults(func=cmd_host_remove)

    hl = hsub.add_parser("list", help="列出已配置别名（只列 host 行，不回显密码/密钥）",
                         description="例: pyaissh host list  → entries[{name,target}]；--field entries 只取清单")
    hl.add_argument("--field", dest="field", default=argparse.SUPPRESS,
                    help="只取结果字段裸值（如 --field entries 得清单 JSON）")
    hl.set_defaults(func=cmd_host_list)

    return parser


# =========================================================================
# 入口
# =========================================================================

# SIGTERM 已到达标志：主线程阻塞在 paramiko C 级 I/O（串行 sftp.get/put）时，
# handler raise 的 KeyboardInterrupt 会在 paramiko 展开途中被其 finally 里的
# 新异常（Garbage packet / No existing session 等）覆盖，落到兜底 except 就
# 被误分类成 download_failed/ssh_error。兜底 except 检查本标志可强制归位到
# 中断/计时/单例全局（_SIGTERM_RECEIVED/_INTERRUPT_SOURCE/_MAIN_START/
# _CURRENT_ACTION/_RESPONDER_STARTED）与 _sigterm_handler 已上移至文件顶部
# ——必须在 paramiko 慢 import 之前注册信号 handler（极早期信号窗口，
# 见文件头部注释）。此处保留信号救援线程与注册入口。


def _signal_responder():
    """信号救援线程：置标志后 0.2s 关闭所有活动连接的底层 socket。

    只做裸 sock.close()（不碰任何会拿 paramiko 锁的方法——看门狗死锁正是
    栽在 sftp.close() 的锁上）。解堵后阻塞读以普通异常干净展开，中断走
    正常 except 路径输出 interrupted JSON。关闭幂等，重复几轮兜住分片重建。"""
    deadline = None
    closed_once = False
    while True:
        time.sleep(RESPONDER_TICK)
        if not _SIGTERM_RECEIVED:
            # 空闲自复位：进程内复用（AI 嵌入/测试 harness 同进程多次 main()）
            # 时，上一轮中断的 deadline/closed_once 不残留到下一轮（否则新
            # 一轮中断会跳过 0.2s 缓冲立即强关连接）
            deadline = None
            closed_once = False
            continue
        if deadline is None:
            deadline = time.time() + RESPONDER_GRACE
        if time.time() < deadline:
            continue
        n = 0
        for t in list(_ACTIVE_TRANSPORTS):
            try:
                if t is not None and t.sock is not None:
                    t.sock.close()
                    n += 1
            except Exception:
                pass
        if n and not closed_once:
            closed_once = True
            log("[WARN] 信号中断：已强制断开 %d 条连接解除阻塞" % n)
        time.sleep(RESPONDER_AFTER)


def _setup_signal_handlers():
    try:
        import signal
        signal.signal(signal.SIGTERM, _sigterm_handler)
        # SIGINT（Ctrl+C）与 SIGTERM 同源同险：串行传输中按 Ctrl+C 同样可能
        # 死锁，一并纳入标志+救援（消息文案由 _INTERRUPT_SOURCE 区分）
        signal.signal(signal.SIGINT, _sigterm_handler)
    except (ValueError, OSError, ImportError):
        pass  # 非主线程 / 平台不支持（Windows 下注册无副作用）


def main():
    global _MAIN_START, _RESPONDER_STARTED, _CURRENT_ACTION
    global _SIGTERM_RECEIVED, _INTERRUPT_SOURCE, _FIRST_MAIN_DONE
    _MAIN_START = time.time()
    _setup_console_utf8()
    # 极早期信号（import 阶段，约前 30-400ms）处理：顶层已注册的 handler
    # 置了标志——该窗口内的信号不再走默认动作（rc=143、无 JSON），而是
    # 输出结构化 interrupted JSON + 130。不重置标志（它就是"用户要中断"
    # 的事实）；此时 args 未解析、--text 不可知，按默认 JSON 输出。
    # 仅对【进程内第一次】main() 生效：进程内复用（AI 嵌入/测试 harness）
    # 时后续调用里的标志是上一次中断留下的，必须走正常重置流程，否则
    # 会被误判为"本次极早期信号"（实测 0.00s 即 130 的回归）。
    if _SIGTERM_RECEIVED and not _FIRST_MAIN_DONE:
        _FIRST_MAIN_DONE = True
        emit_error(True, "interrupted", _interrupt_msg())
        return 130
    _FIRST_MAIN_DONE = True
    # 进程内复用（AI 嵌入/测试 harness 同进程多次调 main()）时，上一次调用的
    # 全局状态会污染本次：SIGTERM 标志不复位会让 responder 线程强关新连接
    # （实测中断后同进程后续调用 0.00s 即 interrupted/130 失败）；活动连接
    # 清单与上传残留警告不清会串到本次结果。CLI 每命令一进程，重置无副作用。
    _SIGTERM_RECEIVED = False
    _INTERRUPT_SOURCE = "SIGTERM"
    _CURRENT_ACTION = None
    _ACTIVE_TRANSPORTS.clear()
    _PUT_RESIDUE_WARNINGS.clear()
    _setup_signal_handlers()
    # 信号救援线程：解救 KI 在 paramiko C 级 I/O 中展开导致的死锁/长尾。
    # 只启动一次（单例）：每次 main() 都启动会在进程内复用场景泄漏线程
    # （实测 40 次调用 +40 线程/+85 句柄）；单例 + 标志复位 = 每轮独立生效。
    if not _RESPONDER_STARTED:
        _RESPONDER_STARTED = True
        threading.Thread(target=_signal_responder, daemon=True).start()
    args = None  # 预置：KeyboardInterrupt 可能发生在 parse_args 期间

    try:
        # 整个流程包进 try：parse_args / handler 任一阶段被 Ctrl+C 中断
        # 都能输出结构化 interrupted JSON（argparse 的 SystemExit 是
        # BaseException 不会被这里捕获，--help/--version/参数错误不受影响）
        load_env()  # 也在 try 内：cwd 被删除等场景 os.getcwd() 会抛 OSError
        parser = build_parser()
        args = parser.parse_args()
        if getattr(args, "version", False):
            # --version 也遵守 stdout 恒一行 JSON 的契约（人类直接读也直观）
            print(json.dumps({"ok": True, "action": "version", "version": VERSION},
                             ensure_ascii=False), flush=True)
            return 0
        handler = getattr(args, "func", None)
        if handler is None:
            emit_error(args.json if args is not None else True, "bad_args",
                       "未指定子命令（可选: exec / upload / download / test / ls）")
            parser.print_help(sys.stderr)
            return 2
        # --field 与 --text 互斥（--text 要可读包裹、--field 要裸字段值；
        # --json 是兼容 no-op 不冲突）
        if getattr(args, "field", None) and not getattr(args, "json", True):
            emit_error(True, "bad_args",
                       "--field 与 --text 互斥（--field 是消费端字段提取，"
                       "--text 是可读模式；二选一）")
            return 2
        # --field 消费端模式：静音进度日志（stderr 只留 WARN 与盲区提示等信号）
        if getattr(args, "field", None):
            global _QUIET
            _QUIET = True
        # 错误 JSON 的 action 字段：取当前子命令名（供 emit_error 统一填充）
        _CURRENT_ACTION = handler.__name__[4:] \
            if handler.__name__.startswith("cmd_") else handler.__name__
        return handler(args)
    except KeyboardInterrupt as e:
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, e.error_type, str(e))
        return 255
    except Exception as e:
        # 最后防线：任何未预期异常（各子命令内部兜底之外的漏网）也必须
        # 保持"stdout 恒单行 JSON"契约——traceback 只进 stderr 供人类排查，
        # stdout 输出结构化 internal_error 供 AI 解析，绝不裸崩打 traceback
        import traceback
        traceback.print_exc(file=sys.stderr)
        use_json = getattr(args, "json", True) if args else ("--text" not in sys.argv)
        emit_error(use_json, "internal_error",
                   "内部错误: %s: %s" % (type(e).__name__, e))
        return 255


if __name__ == "__main__":
    rc = main()
    if _SIGTERM_RECEIVED:
        # 信号中断路径硬退出：解释器正常关闭会 join/收割线程，中断展开后
        # 偶发在 finalization 阶段 abort（实测 1/10 轮退出码 134）。JSON 已
        # flush、本地 .part 已清理，跳过关闭阶段保住确定的退出码 130
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(rc)
    sys.exit(rc)
