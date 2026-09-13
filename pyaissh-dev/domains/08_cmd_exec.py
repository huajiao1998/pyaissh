"""exec 子命令实现（域 08）——cmd_exec 编排 + 三段函数。

- cmd_exec：编排（前置 -> 连接 -> 会话）
- _prepare_exec_command：组装段（命令加载/校验/sudo 组装，哨兵返回）
- _connect_exec：连接段（连接+跳板，错误映射+信号归位）
- _exec_session：执行会话（凭据检测 -> exec_command+stdin 注入 -> 并发读线程 ->
  drain/排水 -> 结果组装/异常映射；内嵌闭包 _partial_extra/_read/_drain_rest）
超时/截断/spill/--field 语义见 SKILL.md 与 docs/exec.md。
"""

def _prepare_exec_command(args):
    """exec 前置（无连接副作用）：加载/校验命令 + --sudo 组装。

    返回 (cmd, warnings, sudo_pw) 成功；
    失败返回 (None, (error_type, message, warnings))——错误输出由 cmd_exec 处理。
    等价搬移自 cmd_exec 原头部（校验快速失败 + sudo 组装），行为零变化。
    """
    start = time.time()  # 计时含连接耗时：duration_ms 在跳板/慢网络下偏大

    warnings = []  # 汇总警告（函数体最前初始化：成功/异常路径都能取到）
    # 先校验命令（快速失败，避免无谓连接）
    cmd = args.cmd
    if cmd and args.cmd_file:
        log("[WARN] --cmd 与 --cmd-file 同时指定，--cmd-file 被忽略")
        warnings.append("--cmd 与 --cmd-file 同时指定，--cmd-file 被忽略（本次用 --cmd）")
    if not cmd and args.cmd_file:
        try:
            if args.cmd_file == "-":
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")
                cmd = sys.stdin.read()
                # 读 stdin 期间可能收到信号（handler 只置标志，阻塞的 read 无法
                # 被中断）：读到内容但信号已到 = 用户取消，不应继续执行命令
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")
            else:
                # utf-8-sig：自动剥离 UTF-8 BOM（\ufeff）——记事本/VS Code 等
                # Windows 工具写出的命令文件带 BOM 时，首行命令会被拼进 BOM
                # 字符而报 "command not found"（与 .env 解析同款处理）
                # _fix_msys_local_path：Git Bash 下 /tmp/x.sh 等 Unix 风格本地路径
                # 转 Windows 路径（与 --local 同款；内部含 ~ 展开），避免 Windows
                # Python 把 /tmp 解析成盘根而报 Errno 2
                with open(_fix_msys_local_path(args.cmd_file), encoding="utf-8-sig") as f:
                    cmd = f.read()
        except KeyboardInterrupt:
            raise  # 中断走 main 的 interrupted/130
        except Exception as e:
            return None, ("read_cmd_failed", str(e), warnings)  # 本地参数/文件问题，由 cmd_exec 输出
    if not cmd or not cmd.strip():
        return None, ("bad_args", "未指定命令（--cmd 或 --cmd-file）", warnings)
    if args.max_time is not None and args.max_time < args.exec_timeout:
        return None, ("bad_args",
                      "--max-time (%d) 不能小于 --idle-timeout (%d)（总时长上限必须覆盖静默窗口）"
                      % (args.max_time, args.exec_timeout), warnings)

    # --sudo 提权组装：
    #  - 复合命令（含 &&/||/;/管道/重定向/$()/反引号/换行）→ bash -c 包裹整链提权
    #    （sudo 只提权首命令，第二段会回到原用户——实测回归项"&& 第二段 uid=0"）
    #  - 简单命令（无 shell 元字符）→ 直连 sudo：保留 sudoers NOPASSWD 按命令
    #    路径匹配（如 NOPASSWD: /usr/bin/apt 对 `sudo apt update` 生效；bash -c
    #    包裹会让 sudo 看到 /usr/bin/bash 而匹配不上免密规则）
    #  - -p ''：压掉 "[sudo] password for ..." 提示符（成功路径 stderr 干净）
    #  - 密码只经 SSH stdin 注入：命令文本/cmd 字段/日志/远端磁盘均无密码
    #  - orig_cmd 保留组装前原文：凭据启发式检测用原命令（组装后的 sudo -S -p ''
    #    前缀命中 _SENSITIVE_CMD_RE 的 -p 模式 -> 100% 误报"疑似凭据"，A 修复）
    orig_cmd = cmd
    sudo_pw = (args.sudo_password if args.sudo_password
               else os.environ.get("PYAISSH_SUDO_PASSWORD")) if args.sudo else None
    if args.sudo:
        if args.pty:
            return None, ("bad_args",
                          "--sudo 与 --pty 互斥（sudo -S 走 stdin 管道而非 pty）", warnings)
        if re.search(r"[\$`\n;|&><]|\(|\)", cmd):
            qcmd = cmd.replace("'", "'\\''")
            cmd = ("sudo -S -p '' bash -c '%s'" % qcmd) if sudo_pw \
                else ("sudo -n bash -c '%s'" % qcmd)
        else:
            cmd = ("sudo -S -p '' %s" % cmd) if sudo_pw else ("sudo -n %s" % cmd)

    return cmd, warnings, sudo_pw, orig_cmd



def _connect_exec(args):
    """连接目标（含跳板解析）；成功返回 (conn, client, None)；失败已 emit，返回 (None, None, 退出码)。

    与原 cmd_exec 连接 try 逐语句等价（SshError 带信号归位 / bad_args 特判 / 兜底 connection_failed）。
    """
    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
        return conn, client, None
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return None, None, 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return None, None, 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return None, None, 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return None, None, 255



def _exec_session(args, start, cmd, orig_cmd, sudo_pw, warnings, conn, client):
    """执行会话：凭据检测 -> exec_command(+stdin 注入) -> 并发读线程 -> drain/排水 -> 结果组装。

    原 cmd_exec 连接后主体整体搬入（语句顺序零改动）；闭包子例程
    _partial_extra / _read / _drain_rest 随迁（引用本函数局部 = 原闭包语义）。
    返回本地退出码；异常已 emit（130/255/124）。finally 兜底 spill + close_all。
    """
    def _partial_extra():
        """错误时组装已读到的部分输出/警告，供 AI 判断命令卡在哪一步。

        恒带 stdout/stderr/output_incomplete/stdout_bytes/stderr_bytes 键（可能为空串），
        让 AI 能区分"命令没跑起来（零输出）"与"字段缺失"；有警告时带 warnings。
        闭包引用 cmd_exec 的 out_buf/err_buf/warnings（已在函数体最前初始化，
        任何异常路径都能安全取到）。错误路径的部分输出同样做上限截断。
        output_incomplete（而非 output_truncated）：错误中断输出必然不完整，
        与成功路径"超 --max-output 被裁剪"的 output_truncated 语义不同，
        AI 的后继动作（调大 --max-output vs 调大超时重跑）完全相反。
        """
        # 读线程的头尾滚动缓冲只在退出循环时才刷进共享 buf；总超时/中断时
        # 线程可能还在循环里——先置 stop_drain 让其退出并 join，否则组装到
        # 的是空（实测：总超时时 3.8MB 已读输出全部丢失）
        if stop_drain is not None:
            stop_drain.set()
            t_out.join(JOIN_GRACE)
            t_err.join(JOIN_GRACE)
        out_raw = b"".join(out_buf)
        err_raw = b"".join(err_buf)
        out_cut, _, _ = _truncate_output(out_raw, args.max_output, "stdout")
        err_cut, _, _ = _truncate_output(err_raw, args.max_output, "stderr")
        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        extra = {
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "cmd": cmd_echo,  # 与成功路径对称：错误时也能看到命令原文（含凭据需脱敏；超限截断）
            "cmd_truncated": cmd_cut,  # cmd 回显是否被截断（--cmd-file 大脚本）
            "stdout": _clean_pty_text(out_cut.decode(args.encoding, errors="replace"), args),
            "stderr": _clean_pty_text(err_cut.decode(args.encoding, errors="replace"), args),
            # 与成功路径语义一致：原始字节数（total_counter 在 exec_command
            # 之后定义；exec_command 本身抛错时回退用缓冲长度）
            "stdout_bytes": total_counter[0],
            "stderr_bytes": err_total_counter[0],
            "output_incomplete": True,  # 错误中断，输出必然不完整（区别于超限裁剪）
            "duration_ms": int((time.time() - start) * 1000),
        }
        if warnings:
            extra["warnings"] = list(warnings)
        if cmd_cut:
            extra.setdefault("warnings", []).append(
                "cmd 字段已截断（完整命令 %d 字节，见原始调用；--cmd-file 时为本地文件可重读）" % cmd_n)
        return extra

    out_buf, err_buf = [], []  # _partial_extra 依赖（try 之前初始化）
    # 读线程哨兵（try 之前预置 None）：_partial_extra 用 is not None 判断
    # 而非 NameError 控制流——except NameError 会掩盖未来真正的拼写错误
    stop_drain = None
    t_out = t_err = None
    # 读线程计数器提前预置（try 之前）：exec_command 本身抛错时
    # _partial_extra 也能安全取到，不依赖 locals() 检查
    total_counter = [0]      # stdout 原始字节数
    err_total_counter = [0]  # stderr 原始字节数
    drop_counter = [0]       # stdout 内存缓冲丢弃字节
    err_drop_counter = [0]   # stderr 内存缓冲丢弃字节
    # spill 完整流落盘句柄（try 之前预置 None）：finally 兜底清理，异常路径不留文件
    spill_out_fh = spill_err_fh = None
    spill_out_path = spill_err_path = None
    _spill_handled = False
    try:
        w = warn_sensitive_cmd(orig_cmd, enabled=not getattr(args, "no_credential_warn", False))
        if w:
            warnings.append(w)
        if args.pty_strip_ansi and not args.pty:
            msg = "--pty-strip-ansi 未生效：需同时指定 --pty（本次未剥离 ANSI）"
            log("[WARN] " + msg)
            warnings.append(msg)
        log("[EXEC] %s" % _sanitize_log_text(cmd if len(cmd) <= 200 else cmd[:200] + "..."))
        stdin, stdout, stderr = client.exec_command(cmd, timeout=args.exec_timeout, get_pty=args.pty)
        # 立即关闭 stdin：paramiko 默认不关，远程命令若读 stdin（如 cat）会一直
        # 等输入直到静默超时误判挂死。关闭后远程立即收到 EOF。
        # --sudo 有密码时：先经 stdin 注入密码（sudo -S 从 stdin 读），再关闭
        #（密码只进 SSH 管道，不进 cmd 字段/日志/远端磁盘；sudo -p '' 压提示符）
        try:
            if sudo_pw:
                stdin.write(sudo_pw + "\n")
                stdin.flush()
            stdin.close()
        except Exception:
            pass
        # 并发读 stdout/stderr，避免大输出填满管道窗口导致死锁
        # （若主线程阻塞等远程结束而远程在写 stderr 已满，就会卡死）。
        # 静默超时由读线程掌控：任一流有输出即重置计时，超过 exec_timeout 无输出则退出。
        chan = stdout.channel
        chan.settimeout(1.0)  # 读 tick：无数据时每 1s 醒来检查一次静默计时
        silence_deadline = [time.time() + args.exec_timeout]
        stop_drain = threading.Event()  # 收尾截断信号：设置后读线程尽快退出

        # 有界缓冲：读线程只保留头尾各 max_output//2 字节（与显示截断一致），
        # 中间溢出丢弃并累计 dropped——防止大输出（cat 大文件/恶意流）无限吃内存，
        # --max-output 不只是"最后截显示"，而是真正限制内存占用。
        # （计数器已在 try 之前预置，这里只复用，不重新定义）
        buf_limit = max(args.max_output, MIN_BUF_FLOOR)
        # 头尾配额直接各取 buf_limit//2：不给接缝标记单独预留。之前预留 512B
        # 导致输出量介于 limit-512 与 limit 之间时内存层就提前溢出丢弃（假截断
        # 丢尾，实测 4000 字节/4096 上限丢 416 字节）。head+seam+tail ≤ limit
        # 的约束交给显示层 _truncate_output 保证（它自己会重算 marker 空间并
        # 收缩 half，且内存层数据已 ≤ limit，通常不再二次截断）。
        buf_half = max(buf_limit // 2, 1024)

        def _read(buf, recv_fn, total_cnt, drop_cnt, reason, spill=None):
            head_buf = []
            head_len = 0
            tail_buf = []       # 滚动保留最近 buf_half 字节
            tail_len = 0
            why = "timeout"  # 循环条件退出=静默超时；EOF/异常/stop_drain 会改写
            while time.time() < silence_deadline[0] and not stop_drain.is_set():
                try:
                    data = recv_fn(RECV_CHUNK)
                except (socket.timeout, TimeoutError):
                    continue  # 无数据 tick，回到循环头检查 deadline
                except Exception:
                    why = "error"  # 通道异常（非 EOF）：残留数据可能未完（H2）
                    break  # 通道关闭/其他错误
                if not data:
                    why = "eof"
                    break  # EOF
                silence_deadline[0] = time.time() + args.exec_timeout  # 有输出则重置静默计时
                total_cnt[0] += len(data)
                if spill is not None:
                    spill.write(data)  # 完整流落盘：内存层丢弃中间字节不影响全文
                if head_len < buf_half:
                    room = buf_half - head_len
                    piece = data[:room]
                    head_buf.append(piece)
                    head_len += len(piece)  # 按实际追加量计（块可能小于 room，不能加 room）
                    rest = data[room:]
                    if rest:
                        # 溢出块送入 tail 滚动（不能直接丢：数据块可能恰好跨越
                        # head 边界，丢了会丢失 <max_output 的输出并误报截断）
                        tail_buf.append(rest)
                        tail_len += len(rest)
                else:
                    tail_buf.append(data)
                    tail_len += len(data)
                # 尾部滚动：保留【最近的】buf_half 字节。溢出从头丢，
                # 但首块本身比溢出量大时只丢它的前缀、保留其尾部——
                # 否则单个大块（高延迟链路整块到达）会被整块弹出，
                # 尾部数据全丢，只剩头部
                overflow = tail_len - buf_half
                while overflow > 0 and tail_buf:
                    old = tail_buf[0]
                    if len(old) <= overflow:
                        tail_buf.pop(0)
                        tail_len -= len(old)
                        drop_cnt[0] += len(old)
                        overflow -= len(old)
                    else:
                        tail_buf[0] = old[overflow:]
                        tail_len -= overflow
                        drop_cnt[0] += overflow
                        overflow = 0
            else:
                # 循环条件退出（未 break）：stop_drain（主线程收尾截断）或静默超时
                why = "eof" if stop_drain.is_set() else "timeout"
            # 组装（追加进共享 buf；主线程 b"".join 后还会过 _truncate_output，
            # 此时数据已 ≤ buf_limit，通常不会再截）
            head_part = b"".join(head_buf)
            tail_part = b"".join(tail_buf)
            # 行对齐与接缝标记【只在真实中间丢弃（滚动溢出，pre-snap drop>0）
            # 时做】：数据 ≤ 2×buf_half 时 head+tail 本就连续完整，对齐反而会
            # 误砍「末行无换行」的结尾（printf 'abc\ndef' 的 def 被吞）或对
            # 放得下的输出制造伪截断。head/tail 各自退到最近换行（限窗 4KB，
            # 二进制无换行保持字节边界），snapped 字节计入 drop_cnt（记账精确）。
            real_gap = drop_cnt[0] > 0
            was_line_boundary = False
            if real_gap and head_part:
                was_line_boundary = head_part[-1:] == b"\n"
                nl = head_part.rfind(b"\n", max(0, len(head_part) - BUF_ALIGN_WINDOW))
                if nl != -1:
                    drop_cnt[0] += len(head_part) - (nl + 1)
                    head_part = head_part[:nl + 1]
            if real_gap and tail_part and not was_line_boundary:
                # 边界恰逢行首时 tail 首行本来就完整，不要整行误删。
                # 只有当第一个 \n 之后还有内容（首行之后存在其他行）时才
                # 消费首行；若 \n 恰是 tail 的最后一个字节（单行输出，整个
                # tail 就是一行），该行是完整行——消费它会把尾部整段丢掉
                # （实测 --max-output<=8192 时单行大输出尾部全丢且无 seam
                # 标记，warnings 还谎称"仅保留头尾"），必须保留
                nl2 = tail_part.find(b"\n", 0, BUF_ALIGN_WINDOW)
                if nl2 != -1 and nl2 < len(tail_part) - 1:
                    drop_cnt[0] += nl2 + 1
                    tail_part = tail_part[nl2 + 1:]
            if real_gap:
                # 行对齐会把 head 尾巴 / tail 头部的半个多字节 UTF-8 字符切开
                # （单行无换行 + 超限 + 中文，实测 seam 处出 U+FFFD 半个字）：
                # 组装前各自退到合法字符边界，被吞的字节计入 drop_cnt 保账目一致。
                # 注意顺序：先对 head 做 from_start 回退、再对 tail 做 from_end
                # 回退，否则 head 缩进后 tail 的相对基准会错位。
                if head_part:
                    hp = _utf8_boundary_cut(head_part, len(head_part))
                    drop_cnt[0] += len(head_part) - len(hp)
                    head_part = hp
                if tail_part:
                    tp = _utf8_boundary_cut(tail_part, len(tail_part), from_start=False)
                    drop_cnt[0] += len(tail_part) - len(tp)
                    tail_part = tp
            seam = b""
            if real_gap and tail_part:
                # 中间丢弃过的接缝处插一行带内标记：纯按字节拼接会让相邻两行
                # 拼成"看起来合法"的假数据（如 seq 输出 ...23696\n23 + 78156\n...）
                # seam 自带前导/后随换行：head 若以换行结尾、tail 若以换行开头
                # 会各多一个空行（4096 最小档实测），拼接前先去重避免空行跳号。
                lead = b"" if (head_part and head_part[-1:] == b"\n") else b"\n"
                trail = b"" if (tail_part and tail_part[:1] == b"\n") else b"\n"
                seam_body = ("[pyaissh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
                             % drop_cnt[0]).encode("utf-8")
                seam = lead + seam_body + trail
                # 防显示层二次截断切真实尾部：head+seam+tail 超 buf_limit 时
                # （head/tail 各占一半=limit，seam 是额外字节），单行无换行场景
                # 显示层会把 tail 锚定到 seam 换行、走纯前缀回退——实测 4096 档
                # 尾部 30 个 Z 全丢且 omitted 少报 77 字节。从 tail 前缀削字节
                # 计入 drop_cnt（保留真实尾部），循环收敛 seam 数字位数变化。
                for _ in range(3):
                    over = len(head_part) + len(seam) + len(tail_part) - buf_limit
                    if over <= 0 or not tail_part:
                        break
                    drop_cnt[0] += over
                    tail_part = tail_part[over:]
                    if not tail_part:
                        break
                    # 从头部削可能切开半个多字节字符：重新对齐 UTF-8 边界
                    tp = _utf8_boundary_cut(tail_part, len(tail_part), from_start=False)
                    drop_cnt[0] += len(tail_part) - len(tp)
                    tail_part = tp
                    trail = b"" if (tail_part and tail_part[:1] == b"\n") else b"\n"
                    seam_body = ("[pyaissh: 中间省略 %d 字节（内存缓冲截断，--max-output 调整）]"
                                 % drop_cnt[0]).encode("utf-8")
                    seam = lead + seam_body + trail
            buf.append(head_part + seam + tail_part)
            reason[0] = why  # 退出原因供主线程判定：eof=数据收完，timeout=静默超时

        out_reason = [None]  # 读线程退出原因（"eof"/"timeout"），drain 阶段主线程接管判定
        err_reason = [None]
        # 完整流落盘：读线程边收边写（内存层只保留头尾，落盘才是全文）。
        # 截断时保留并回传路径（stdout_spill_file/stderr_spill_file），未截断则删除。
        spill_out_fh, spill_out_path, spill_err_fh, spill_err_path = _spill_writers(args)
        t_out = threading.Thread(target=_read,
                                 args=(out_buf, chan.recv, total_counter, drop_counter, out_reason,
                                       spill_out_fh),
                                 daemon=True)
        if args.pty:
            # PTY 模式下 SSH 服务端把 stderr 合并进 stdout，无独立 stderr 流：
            # 给 stderr 读线程传"立即返回 EOF"的哑函数，线程秒退，后续
            # is_alive()/join() 逻辑无需分支。
            t_err = threading.Thread(target=_read, args=(err_buf, lambda n: b"",
                                                         err_total_counter, err_drop_counter,
                                                         err_reason, spill_err_fh), daemon=True)
        else:
            t_err = threading.Thread(target=_read, args=(err_buf, chan.recv_stderr,
                                                         err_total_counter, err_drop_counter,
                                                         err_reason, spill_err_fh), daemon=True)
        t_out.start(); t_err.start()

        # 不用 recv_exit_status 干等：它内部无限等待 status_event，
        # 远程静默挂死时主线程会永久卡住。改为轮询 exit_status_ready + 静默超时判定
        # （读线程持续排水，不会触发大输出死锁）。
        # 总超时兜底：静默超时只覆盖"无输出"场景；持续输出但不结束的命令
        # （如 while true; echo x）会无限重置静默计时，必须有硬上限。
        # 默认 max(2×exec-timeout, DEFAULT_MIN_TOTAL)，长任务（构建/编译）用 --max-time 手动调大（最高 MAX_TIME_CAP）。
        total_limit = args.max_time if args.max_time is not None else max(args.exec_timeout * 2, DEFAULT_MIN_TOTAL)
        if total_limit > MAX_TIME_CAP:
            # 告警同时进 JSON warnings：只看 stdout 的 AI 必须知道实际生效上限被改小
            msg = ("总时长上限 %ds 超过 %d，本次按 %d 执行（与 --max-time 上限一致；"
                   "更久任务请用 nohup 后台化 + 轮询）" % (total_limit, MAX_TIME_CAP, MAX_TIME_CAP))
            log("[WARN] " + msg)
            warnings.append(msg)
            total_limit = MAX_TIME_CAP
        total_deadline = time.time() + total_limit
        last_hb = [time.time()]  # --progress 心跳（v2.1）：仅告知仍在运行，不重置静默计时
        while not chan.exit_status_ready():
            if _SIGTERM_RECEIVED:
                # 在我们自己的 Python 帧里抛 KI 是安全的（在 paramiko C 级
                # 代码里抛才是锁损坏根源——handler 已不再 raise）
                raise KeyboardInterrupt("SIGTERM")
            if chan.closed:
                break
            # 先判读线程退出状态再判总时长：--max-time == --exec-timeout 时
            # 静默挂死应报"无输出"而非误标"持续输出"
            if not t_out.is_alive() and not t_err.is_alive():
                # 双读线程都退出但远程未结束
                if out_reason[0] == "error" or err_reason[0] == "error":
                    # 通道异常（非 EOF/非静默超时）：连接问题，不是命令超时
                    raise SshError("连接中断（通道异常），输出可能不完整", "connection_lost")
                if out_reason[0] == "timeout" or err_reason[0] == "timeout":
                    # 读线程因静默超时退出而远程未结束 -> 判定挂死
                    # （留 1s 宽限，避免 exit-status 包还在路上时误判）
                    if time.time() > silence_deadline[0] + SILENCE_GRACE:
                        raise ExecIdleTimeout(
                            "命令执行超时（连续无输出 %ss）。注意：远程进程可能仍在运行"
                            "（断开连接不会杀掉它），副作用类命令重试前请先 pgrep 确认/清理；"
                            "确认命令只是输出少，可用 --idle-timeout 调大静默窗口" % args.exec_timeout)
                # 双读线程都因 EOF 退出（数据收完）：exit-status 包可能还在路上，
                # 属正常收尾，继续等（total_deadline 兜底），不误报"无输出超时"
            if time.time() > total_deadline:
                if not t_out.is_alive() and not t_err.is_alive() \
                        and out_reason[0] == "eof" and err_reason[0] == "eof":
                    # 双流都已 EOF（数据收完）却始终等不到退出状态：是异常关流，
                    # 不是命令超时——报连接中断才能引导正确排查方向
                    raise SshError("连接中断（输出流已结束但未收到退出状态）", "connection_lost")
                if silence_deadline[0] <= time.time():
                    # 静默窗口也已超时（读线程只是还没到 tick 醒来）：按无输出报
                    raise ExecIdleTimeout(
                        "命令执行超时（连续无输出 %ss，总时长 %ds）。注意：远程进程可能仍在运行，"
                        "重试前请先 pgrep 确认/清理；输出少的慢命令可调大 --idle-timeout"
                        % (args.exec_timeout, total_limit))
                raise ExecTotalTimeout(
                    "命令执行超时（持续输出但未结束，总时长超过 %ds）。长任务请用 --max-time "
                    "调大（最高 %d）；注意：远程进程可能仍在运行，重试前请先 pgrep 确认/清理"
                    % (total_limit, MAX_TIME_CAP))
            if args.progress:
                now = time.time()
                if now - last_hb[0] >= args.progress:
                    # 心跳（--progress）：进程活着但静默——打 stderr 让 AI 安心，
                    # 不重置 silence_deadline（否则永远不 idle 超时）；force=True
                    # 绕过 --field 静音（长任务 + --field stdout 恰最需要心跳）
                    log("[PROGRESS] 仍在运行，已持续 %ds（连续 %ds 无输出/未结束；"
                        "更久任务调大 --idle-timeout/--max-time）"
                        % (int(now - start), args.progress), force=True)
                    last_hb[0] = now
            time.sleep(POLL_TICK)
        exit_code = chan.exit_status if chan.exit_status_ready() else -1
        if exit_code == -1:
            # 通道已关闭但未收到退出状态（网络中断/远程异常断开）：
            # 输出可能不完整，不能当作成功返回
            raise SshError("连接中断，未收到远程退出状态（输出可能不完整）", "connection_lost")
        # 收尾排水：exit-status 已就绪，但远程后台子进程可能仍占用通道（无 EOF）。
        # 1) 收尾阶段把静默窗口缩短到 min(exec_timeout, 10)，避免无输出的后台进程
        #    拖满整个 exec_timeout 才返回；
        # 2) drain_deadline 兜底：后台进程持续输出时强制截断并标记
        #    output_truncated（已读数据不会丢，读线程边读边写共享 buf）。
        # 收尾阶段【不】缩短 silence_deadline：读线程必须因 EOF（数据收完）退出才算完整；
        # 若因静默超时退出说明输出未完，会被 drain_deadline 强制截断并标记 truncated，
        # 避免"输出被丢但无标记"让 AI 误判完整。drain_deadline 单独兜底返回时长。
        drain_limit = min(args.exec_timeout, DRAIN_WINDOW)
        drain_deadline = time.time() + drain_limit
        while (t_out.is_alive() or t_err.is_alive()) and time.time() < drain_deadline:
            if _SIGTERM_RECEIVED:
                # 排水期收到信号：不再等读线程自然退出，立即归位中断。
                # 否则 responder 关 socket 会让 drain 读到"通道异常"误标
                # output_truncated，AI 看到"命令成功但输出被截断"而非"被中断"
                raise KeyboardInterrupt("SIGTERM")
            time.sleep(POLL_TICK)
        t_out.join(0.5)
        t_err.join(0.5)
        drain_truncated = (t_out.is_alive() or t_err.is_alive())

        def _drain_rest(recv_fn, buf, deadline, total_cnt=None, spill=None):
            """主线程接管排空通道缓冲的尾部数据（读线程已因静默超时退出时）。

            exit-status 已就绪说明远程 shell 已退出，剩余数据读完即 EOF，
            不会无限阻塞；deadline 兜底防后台子进程持续占用。
            返回 True 表示收到 EOF（完整）。
            """
            got_eof = True
            got = 0
            limit = args.max_output  # 排空也限内存：超限即视为不完整
            while time.time() < deadline:
                try:
                    data = recv_fn(RECV_CHUNK)
                except (socket.timeout, TimeoutError):
                    continue
                except Exception:
                    got_eof = False  # 通道异常中断排空：数据未完，必须标记截断（H1）
                    break
                if not data:
                    break
                if spill is not None:
                    spill.write(data)  # 排空阶段也写完整流（读线程提前退出时兜底）
                if got + len(data) > limit:
                    buf.append(data[:limit - got])
                    if total_cnt is not None:
                        # 超限丢弃的部分也是"已接收"的字节：全额计入原始
                        # 统计（此前漏计，stdout_bytes 低估真实接收量）
                        total_cnt[0] += len(data)
                    got_eof = False  # 超出上限：输出未完，标记截断
                    break
                buf.append(data)
                got += len(data)
                if total_cnt is not None:
                    total_cnt[0] += len(data)  # 排空阶段也计入原始字节统计（L4）
            else:
                got_eof = False  # 循环条件退出（未 break）= 超时未完
            return got_eof

        if drain_truncated:
            stop_drain.set()
            # 读线程可能正阻塞在 recv（最长 1s tick），set 后等它醒来收完
            # 最后一块数据再退出，避免 join 后组装时漏掉最后一块输出
            t_out.join(JOIN_GRACE)
            t_err.join(JOIN_GRACE)
            warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
            log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
        else:
            # 读线程已退出：若因静默超时/通道异常（非 EOF）退出，通道缓冲里可能还有
            # 命令的尾部输出（如 'sleep 3.5; echo END' 的 END），主线程接管排空，
            # 否则尾部数据丢失且无任何标记（BUG：ok=True 但输出不完整）
            if out_reason[0] in ("timeout", "error"):
                # 用新鲜 deadline：外层 drain_deadline 可能已被上面的等待循环耗尽，
                # 复用会让排空窗口为 0、一行尾部数据都读不到
                if not _drain_rest(chan.recv, out_buf, time.time() + drain_limit, total_counter,
                                   spill_out_fh):
                    drain_truncated = True
                    warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
                    log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
            if err_reason[0] in ("timeout", "error"):
                if not _drain_rest(chan.recv_stderr, err_buf, time.time() + drain_limit,
                                   err_total_counter, spill_err_fh):
                    drain_truncated = True
                    warnings.append("输出已截断：命令已结束但输出流仍被后台进程占用，输出可能不完整")
                    log("[WARN] 命令已结束但输出流仍被后台进程占用，已截断（输出可能不完整）")
        # UTF-8 解码（errors="replace"）：exec 只适合文本输出，二进制内容会损坏
        # 超限截断保留头尾：防止大输出撑爆调用方（AI）的上下文窗口
        if _SIGTERM_RECEIVED:
            # drain/排空阶段信号到达的最终兜底：组装前归位中断，防止
            # 以 ok:true + 远程退出码退出（信号被吞、退出码非 130）
            raise KeyboardInterrupt("SIGTERM")
        out_raw = b"".join(out_buf)
        err_raw = b"".join(err_buf)
        if drop_counter[0]:
            warnings.append("stdout 过大，内存缓冲已丢弃中间 %d 字节（仅保留头尾；调大 --max-output 可取更多）"
                            % drop_counter[0])
        if err_drop_counter[0]:
            warnings.append("stderr 过大，内存缓冲已丢弃中间 %d 字节（仅保留头尾；调大 --max-output 可取更多）"
                            % err_drop_counter[0])
        out_cut, out_trunc, out_omitted = _truncate_output(out_raw, args.max_output, "stdout")
        err_cut, err_trunc, err_omitted = _truncate_output(err_raw, args.max_output, "stderr")
        if out_trunc:
            warnings.append("stdout 已截断：省略 %d/%d 字节（--max-output 调整）"
                            % (out_omitted, len(out_raw)))
        if err_trunc:
            warnings.append("stderr 已截断：省略 %d/%d 字节（--max-output 调整）"
                            % (err_omitted, len(err_raw)))
        out_s = _clean_pty_text(out_cut.decode(args.encoding, errors="replace"), args)
        err_s = _clean_pty_text(err_cut.decode(args.encoding, errors="replace"), args)
        duration = int((time.time() - start) * 1000)

        if exit_code == 255:
            warnings.append("远程退出码为 255，本地返回 254（255 保留给连接失败语义）")
        if exit_code != 0:
            # 命令退出码非零 = 最常见的"命令失败"（工具 ok:true 但命令失败）。
            # 含 shell 特殊字符时提示转义（PowerShell 吃 \$ 的坑：远端收到
            # 被改写的命令，行为诡异且退出码非零——用户最需要 hint 的场景）。
            # --sudo 场景抑制转义 hint（E 修复）：命令由工具组装（sudo -S -p ''
            # / bash -c 包裹），用户输入的转义问题已由组装解决，此时 hint 只会
            # 把 AI 从"sudo 密码错/权限错"的真实原因引向"改用 --cmd-file"
            if not getattr(args, "sudo", False):
                hint = _shell_escape_hint(args.cmd)
                if hint:
                    warnings.append("命令退出码非零且含特殊字符。%s" % hint)
            # sudo 专用提示（--sudo 场景）：
            #  - 无密码（-n 探测）且 stderr 是 sudo 报错特征 -> 提示配密码
            #  - 有密码（-S 路径）且 stderr 是 sudo 密码错误特征 -> 提示改密码
            #    （最常发生的"密码错"此前无提示，只有误导性转义 hint——E 修复）
            if args.sudo:
                if not sudo_pw and re.search(
                        r"sudo:[^\n]*(password (is )?required|no password was provided|password required)",
                        err_s, re.IGNORECASE):
                    # -n 路径（无密码探测）提示：与 -S 路径同构——只认带
                    # "sudo:" 前缀的报错行（实测 sudo -n 报错恒带前缀：
                    # "sudo: a password is required"）。命令自身 stderr 恰好
                    # 打出 "a password is required"（无前缀）不触发（对称统一）
                    warnings.append("sudo -n 免密探测失败（sudo 需要密码）：用 --sudo-password "
                                    "或 PYAISSH_SUDO_PASSWORD 环境变量提供密码后重试，"
                                    "或由管理员配置 sudoers NOPASSWD")
                elif sudo_pw and re.search(
                        r"sudo:[^\n]*(incorrect password|no password was provided|password required|authentication failure)",
                        err_s, re.IGNORECASE):
                    # -S 路径密码错提示。门控必须收窄到 sudo 专属报错（带
                    # "sudo:" 前缀）：sudo 与命令的 stderr 同一 channel 无法
                    # 区分来源，宽泛子串（incorrect password / Sorry, try
                    # again / password mismatch）会被"命令自己往 stderr 打
                    # 这些词后失败"的场景误报（R7/R8，与 C 门控同类的坑）。
                    # sudo 密码错的典型 stderr："sudo: no password was
                    # provided" / "sudo: 1 incorrect password attempt"
                    warnings.append("sudo 密码错误（stderr 提示 incorrect password）："
                                    "检查 --sudo-password / PYAISSH_SUDO_PASSWORD 是否与"
                                    "登录密码一致后重试")
        stdout_truncated = bool(out_trunc or drop_counter[0])
        stderr_truncated = bool(err_trunc or err_drop_counter[0])
        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        if cmd_cut:
            warnings.append("cmd 字段已截断（完整命令 %d 字节，见原始调用；--cmd-file 时为本地文件可重读）" % cmd_n)
        result = {
            "ok": True,          # 工具操作成功（连接+执行完成）；命令是否成功看 exit_success / exit_code
            "action": "exec",
            "version": VERSION,
            "exit_code": exit_code,
            "local_exit_code": 254 if exit_code == 255 else exit_code,  # 本地实际退出码（255 时本地返 254）
            "exit_success": exit_code == 0,  # 远程命令退出码是否为 0（AI 判断命令成败用这个）
            "stdout": out_s,
            "stderr": err_s,
            "stdout_bytes": total_counter[0],  # 原始接收字节数（截断/丢弃前；与保留量不同见 omitted）
            "stderr_bytes": err_total_counter[0],
            "stdout_truncated": stdout_truncated,   # 该流是否被截断/丢弃过中间
            "stderr_truncated": stderr_truncated,
            "stdout_omitted_bytes": total_counter[0] - len(out_cut),  # 原始字节-最终展示字节（含 marker 附加；kept+omitted 恒等于 stdout_bytes，AI 可精确对账）
            "stderr_omitted_bytes": err_total_counter[0] - len(err_cut),
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "pty": bool(args.pty),
            "pty_strip_ansi": bool(args.pty_strip_ansi),
            "cmd": cmd_echo,  # 回显命令（含凭据需脱敏；超 CMD_ECHO_LIMIT 截断，见 cmd_truncated）
            "cmd_truncated": cmd_cut,  # cmd 回显是否被截断（--cmd-file 读入的大脚本）
            "output_truncated": bool(drain_truncated or stdout_truncated or stderr_truncated),
            "warnings": warnings,
            "duration_ms": duration,
        }
        # spill 收尾：截断（或 drain 不完整）时保留完整输出文件并把路径回传 JSON；
        # 未截断则删除，不留垃圾。置 _spill_handled 让 finally 跳过（成功路径自己管）。
        out_keep = stdout_truncated or drain_truncated
        err_keep = stderr_truncated or drain_truncated
        _close_spill(spill_out_fh, spill_out_path, keep=out_keep)
        _close_spill(spill_err_fh, spill_err_path, keep=err_keep)
        if out_keep and spill_out_path:
            result["stdout_spill_file"] = spill_out_path
        if err_keep and spill_err_path:
            result["stderr_spill_file"] = spill_err_path
        if out_keep or err_keep:
            # v2.2：截断时给显式下一步——完整输出在本地 spill 文件，读文件比重跑便宜。
            # （默认保留量已降到 64KB：结果 JSON 太大会被宿主裁中段，连这个路径都可能丢）
            kept = [p for p in (spill_out_path if out_keep else None,
                                spill_err_path if err_keep else None) if p]
            result["next_action"] = (
                "输出超 --max-output(%d 字节) 被截断，完整内容已落盘：%s"
                "（本地文件，直接读，不要重跑；需要内联更多才调大 --max-output）"
                % (args.max_output, "、".join(kept)))
        _spill_handled = True
        header = "[%s]  exit_code=%d  duration=%dms" % (
            "OK" if exit_code == 0 else "EXIT %d" % exit_code, exit_code, duration)
        sections = [("STDOUT", out_s), ("STDERR", err_s)]
        _emit_result(args, result, header=header, sections=sections)
        if exit_code == 255:
            # 255 保留给"连接失败"，远程真实退出码 255 时本地改返 254 以免调用方混淆
            log("[WARN] 远程退出码为 255，本地返回 254（255 保留给连接失败语义）")
            return 254
        return exit_code
    except paramiko.SSHException as e:
        if _SIGTERM_RECEIVED:
            # KI 被 paramiko 展开中的 SSHException 替换时按标志归位（同 generic 分支）
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        emit_error(args.json, "exec_failed", str(e), extra=_partial_extra())
        return 255
    except SshError as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_partial_extra())
        return 255
    except KeyboardInterrupt as e:
        # 中断时也带部分输出：AI 判断命令是否已部分执行、能否安全重试
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_partial_extra())
        return 130
    except Exception as e:
        msg = str(e)
        if _SIGTERM_RECEIVED:
            # KI 被 paramiko 展开中的新异常覆盖时按标志归位（同 upload/download）
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_partial_extra())
            return 130
        # 超时类给独立退出码 124（对齐 GNU timeout 惯例）：与"连接失败 255"
        # 区分开——调用方只看退出码也能选对重试方向（调超时 vs 查网络）
        if isinstance(e, ExecIdleTimeout):
            error_type = "exec_idle_timeout"
        elif isinstance(e, ExecTotalTimeout):
            error_type = "exec_total_timeout"
        elif isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower() \
                or "timed out" in msg.lower():
            # 非本工具抛出的超时（paramiko 等）：消息各自写清，直接透传
            error_type = "exec_timeout"
        else:
            error_type = "exec_failed"
        # 超时类机器可读标记：远程进程可能仍在运行（124 幽灵进程）——AI 重试
        # 循环直接读该字段决定是否先 pgrep 确认，不必依赖记住文字提示
        if error_type in ("exec_idle_timeout", "exec_total_timeout", "exec_timeout"):
            timeout_extra = dict(_partial_extra())
            timeout_extra["remote_may_be_running"] = True
        else:
            timeout_extra = _partial_extra()
        # 转义 hint 同样抑制于 --sudo 场景（命令由工具组装，hint 会误导真实
        # 原因定位；与成功路径的抑制保持一致——E 修复）
        if not getattr(args, "sudo", False):
            hint = _shell_escape_hint(args.cmd)
            if hint:
                msg += hint
        emit_error(args.json, error_type, msg, extra=timeout_extra)
        return 124 if error_type != "exec_failed" else 255
    finally:
        # spill 兜底：成功路径已置 _spill_handled；异常/中断路径在此删除，不留垃圾
        if not _spill_handled:
            _close_spill(spill_out_fh, spill_out_path, keep=False)
            _close_spill(spill_err_fh, spill_err_path, keep=False)
        close_all(client)


def cmd_exec(args):
    """exec 编排：--detach 走后台作业；否则前置校验组装 -> 连接 -> 执行会话（三段独立函数）。"""
    if getattr(args, "detach", False):
        return _cmd_exec_detach(args)
    start = time.time()  # 计时含连接耗时：duration_ms 在跳板/慢网络下偏大

    prepared = _prepare_exec_command(args)
    if prepared[0] is None:
        _, (etype, emsg, pwarnings) = prepared
        extra = {"warnings": pwarnings} if pwarnings else None
        emit_error(args.json, etype, emsg, extra=extra)
        return 2  # 本地参数/校验问题，与 bad_args 同级
    cmd, warnings, sudo_pw, orig_cmd = prepared

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec

    return _exec_session(args, start, cmd, orig_cmd, sudo_pw, warnings, conn, client)


# =========================================================================
# 后台作业（v2.2）：exec --detach 启动 + log 子命令读取
#
# 设计要点：
# - 作业脚本落盘（job.sh = 用户命令原文）+ 运行器（run.sh = 落日志 + 捕获退出码）
#   → 不把命令拼进一行 shell（本地 PowerShell 展 $?/$! 的坑实测踩过三次）
# - setsid + nohup + 三路重定向 → SSH 断开/宿主单次调用超时都不影响作业
# - job.rc 是"作业已结束"的唯一可靠标记（被 kill 则永不出现，status 恒 running）
# - log 子命令增量读（--offset/next_offset）→ 轮询 payload 小、绕开宿主调用上限
# =========================================================================

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
JOB_SCRIPT = "job.sh"      # 作业本体（用户命令原文）
JOB_RUNNER = "run.sh"      # 运行器（跑作业 + 落日志 + 写 rc）
JOB_LOG_NAME = "job.log"   # 输出日志（stdout+stderr 合并）
JOB_RC_NAME = "job.rc"     # 退出码文件（存在 = 作业已结束）
JOB_PID_NAME = "job.pid"   # 进程组 leader pid（setsid 后 pid == pgid == sid，供 --kill / 存活探测）
JOB_POLL_TICK = 1.0        # --wait-rc 轮询间隔（秒）
JOB_TAIL_LINES = 100       # 不给 --lines/--offset 时默认回传的尾部行数
JOB_ERR_TAIL = 2048        # 启动失败时回传的日志尾巴字节数
JOB_KILL_GRACE = 5         # --kill：TERM 后宽限秒数，超时补 KILL


def _sh_quote(s):
    """POSIX 单引号转义（远端 shell 命令里安全嵌入路径/文本）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _job_dir_path(job_dir, job_id):
    """作业目录（远端 POSIX 路径；job_dir 允许尾部 /）。"""
    return "%s/%s" % (str(job_dir).rstrip("/") or "/", job_id)


def _job_files(job_dir, job_id):
    """作业文件路径表：dir / job / run / log / rc / pid。"""
    d = _job_dir_path(job_dir, job_id)
    return {"dir": d,
            "job": "%s/%s" % (d, JOB_SCRIPT),
            "run": "%s/%s" % (d, JOB_RUNNER),
            "log": "%s/%s" % (d, JOB_LOG_NAME),
            "rc": "%s/%s" % (d, JOB_RC_NAME),
            "pid": "%s/%s" % (d, JOB_PID_NAME)}


def _detach_scripts(paths, cmd):
    """生成 (job.sh 内容, run.sh 内容)——纯函数（单测覆盖）。

    job.sh = 用户命令**原文**（不转义/不拼接：多行脚本天然支持，无二次解析坑）
    run.sh = 跑 job.sh，stdout+stderr 合并进 job.log，随后退出码写入 job.rc
             （rc 是"作业已结束"的可靠标记；被 kill 则永不出现——此时靠 job.pid
              的存活探测判定 dead）
    v2.2.1 起 run.sh 首行 umask 077：job.log/job.rc 由 shell 创建，若按默认 umask
    会是 0644（同机其他用户可读命令输出/日志），收严到 0600。
    """
    body = cmd if cmd.endswith("\n") else cmd + "\n"
    job_sh = ("#!/bin/bash\n"
              "# pyaissh 后台作业本体（用户命令原文；自动生成，勿手改）\n" + body)
    run_sh = ("#!/bin/bash\n"
              "# pyaissh 后台作业运行器（自动生成，勿手改）\n"
              "umask 077\n"
              "bash %s > %s 2>&1\n"
              "echo $? > %s\n"
              % (_sh_quote(paths["job"]), _sh_quote(paths["log"]), _sh_quote(paths["rc"])))
    return job_sh, run_sh


def _sftp_mkdirs(sftp, path):
    """远端递归建目录（已存在不报错）。"""
    parts = [p for p in str(path).split("/") if p]
    cur = "/" if str(path).startswith("/") else ""
    for p in parts:
        cur = (cur.rstrip("/") + "/" + p) if cur else p
        try:
            sftp.mkdir(cur)
        except Exception:
            pass


def _sftp_write_bytes(sftp, path, data):
    """SFTP 写文件（覆盖；父目录需已存在）。"""
    with sftp.open(path, "wb") as f:
        f.write(data)


def _sftp_read_rc(sftp, rc_path):
    """读 rc 文件取退出码；文件不存在/内容非法 → None（None = 作业未正常结束）。"""
    if not rc_path:
        return None
    try:
        with sftp.open(rc_path, "rb") as f:
            raw = f.read(64)
    except Exception:
        return None
    try:
        return int(raw.decode("utf-8", errors="replace").strip())
    except (TypeError, ValueError):
        return None


def _sftp_read_pid(sftp, pid_path):
    """读作业进程组 leader pid；缺失/非法 → None。"""
    if not pid_path:
        return None
    try:
        with sftp.open(pid_path, "rb") as f:
            raw = f.read(64)
    except Exception:
        return None
    try:
        return int(raw.decode("utf-8", errors="replace").strip())
    except (TypeError, ValueError):
        return None


def _sftp_chmod(sftp, path, mode):
    """远端 chmod（尽力而为：不支持/权限不足不报错，不影响主流程）。"""
    if not path:
        return
    try:
        sftp.chmod(path, mode)
    except Exception:
        pass


def _pid_alive(client, pid):
    """远端存活探测（kill -0）。返回 True/False；探测不可用时 None。

    v2.2.1：作业被 kill/OOM/崩溃时 job.rc 永不出现——只认 rc 会让状态机
    永远停在 running（--wait-rc 轮询不收敛）。存活探测是唯一通用的收敛依据
    （比"我们自己 kill 时写合成 rc"通用：外部 kill 同样能判死）。
    """
    if not pid:
        return None
    try:
        cmd = "kill -0 %d 2>/dev/null && echo pyaissh_alive=1 || echo pyaissh_alive=0" % int(pid)
        _in, out, _err = client.exec_command(cmd, timeout=10)
        try:
            _in.close()
        except Exception:
            pass
        txt = out.read().decode("utf-8", errors="replace")
        try:
            out.channel.recv_exit_status()
        except Exception:
            pass
        if "pyaissh_alive=1" in txt:
            return True
        if "pyaissh_alive=0" in txt:
            return False
        return None
    except Exception:
        return None


def _alive_map(client, pids):
    """一次远端调用批量探测多个 pid（--list 场景避免 N 次 exec）。返回 {pid: bool}。"""
    pids = sorted({int(p) for p in pids if p})
    if not pids:
        return {}
    loop = ("for p in %s; do if kill -0 $p 2>/dev/null; then echo \"$p=1\"; "
            "else echo \"$p=0\"; fi; done" % " ".join(str(p) for p in pids))
    out = {}
    try:
        _in, so, _err = client.exec_command(loop, timeout=10)
        try:
            _in.close()
        except Exception:
            pass
        txt = so.read().decode("utf-8", errors="replace")
        try:
            so.channel.recv_exit_status()
        except Exception:
            pass
        for line in txt.splitlines():
            if "=" in line:
                k, _, v = line.strip().partition("=")
                try:
                    out[int(k)] = (v.strip() == "1")
                except ValueError:
                    pass
    except Exception:
        pass
    return out


def _job_status(sftp, client, files):
    """作业状态三级判定 → (status, rc_val, pid)。

    finished：job.rc 存在（内容即退出码）
    dead    ：无 rc 且 pid 已消失（被 kill / OOM / 崩溃——不会再产出 rc）
    running ：无 rc 且进程仍在（或探测不可用，保守判 running）

    "dead" 的存在意义：让 --wait-rc 与消费端轮询**收敛**——否则被 kill 的作业
    永远是 running，AI 只能靠超时放弃（v2.2.1 修复）。
    """
    files = files or {}
    rc_val = _sftp_read_rc(sftp, files.get("rc"))
    pid = _sftp_read_pid(sftp, files.get("pid"))
    if rc_val is not None:
        return "finished", rc_val, pid
    if _pid_alive(client, pid) is False:
        return "dead", None, pid
    return "running", None, pid


def _detach_cleanup(sftp, paths):
    """删除作业目录及其中文件；返回成功删除的路径列表（尽力而为，不抛错）。"""
    removed = []
    for key in ("job", "run", "log", "rc", "pid"):
        p = paths.get(key)
        if not p:
            continue
        try:
            sftp.remove(p)
            removed.append(p)
        except Exception:
            pass
    try:
        sftp.rmdir(paths["dir"])
    except Exception:
        pass
    return removed


def _job_kill(client, sftp, files, grace=JOB_KILL_GRACE):
    """杀掉整个作业进程组：TERM → 宽限 grace 秒 → KILL。

    setsid 起的作业是独立会话/进程组，leader pid == pgid，故用负 pid 整组杀
    （否则只杀 run.sh，它派生的子进程会变孤儿继续跑）。

    返回 (ok, info_dict)：ok=False 时 info 里有 reason（如无 pid / 进程不在）。
    pid 校验：pid 文件只在作业目录内、且作业结束后会被读到"不在"，但 pid 复用
    理论上可能撞上无关进程——Linux 上额外校验 /proc/<pid>/cmdline 是否提到本作业
    的 run.sh 路径；非 Linux（无 /proc）跳过校验（此时按 pid 直杀，风险由用户知悉）。
    """
    pid = _sftp_read_pid(sftp, files.get("pid"))
    if not pid:
        return False, {"reason": "no_pid", "message": "没有 job.pid（作业可能已结束或被清理）"}
    if _pid_alive(client, pid) is False:
        return False, {"reason": "already_gone", "pid": pid,
                       "message": "进程 %d 已不存在（作业已结束/被 kill）" % pid}
    run_path = files.get("run") or ""
    guard = ("P=%d; "
             "if [ -r /proc/$P/cmdline ] && ! tr '\\0' ' ' < /proc/$P/cmdline | grep -qF %s; "
             "then echo pyaissh_kill=mismatch; exit 3; fi; "
             "kill -TERM -$P 2>/dev/null || kill -TERM $P 2>/dev/null; echo pyaissh_kill=term"
             % (pid, _sh_quote(run_path)))
    try:
        _in, so, se = client.exec_command(guard, timeout=max(10, grace + 5))
        try:
            _in.close()
        except Exception:
            pass
        txt = so.read().decode("utf-8", errors="replace")
        err = se.read().decode("utf-8", errors="replace")
        try:
            rc = so.channel.recv_exit_status()
        except Exception:
            rc = None
        if "pyaissh_kill=mismatch" in txt:
            return False, {"reason": "pid_mismatch", "pid": pid,
                           "message": "pid %d 的进程与作业脚本不匹配（pid 复用？）——拒绝杀，"
                                      "请人工确认" % pid}
        if "pyaissh_kill=term" not in txt:
            return False, {"reason": "kill_failed", "pid": pid,
                           "message": "TERM 未发出（rc=%s）：%s" % (rc, (err or txt).strip()[:200])}
    except Exception as e:
        return False, {"reason": "kill_failed", "pid": pid, "message": "TERM 失败：%s" % e}

    # 宽限等待：进程消失即算成功；仍在则 KILL
    deadline = time.time() + max(1, int(grace))
    while time.time() < deadline:
        time.sleep(JOB_POLL_TICK)
        if _pid_alive(client, pid) is False:
            return True, {"pid": pid, "signal": "TERM", "escalated": False}
    try:
        _in, so, _se = client.exec_command(
            "kill -KILL -%d 2>/dev/null || kill -KILL %d 2>/dev/null; echo done" % (pid, pid),
            timeout=15)
        try:
            _in.close()
        except Exception:
            pass
        so.read()
        try:
            so.channel.recv_exit_status()
        except Exception:
            pass
    except Exception:
        pass
    return True, {"pid": pid, "signal": "KILL", "escalated": True}


def _cmd_exec_detach(args):
    """exec --detach：远端后台运行命令，立即返回作业句柄（job_id/log/rc）。"""
    start = time.time()
    if args.sudo:
        emit_error(args.json, "bad_args",
                   "--detach 与 --sudo 互斥（sudo 密码需经 SSH stdin 注入，后台作业无 stdin；"
                   "提权请在命令内自行处理，或改用前台 exec）")
        return 2
    if args.pty or args.pty_strip_ansi:
        emit_error(args.json, "bad_args",
                   "--detach 与 --pty/--pty-strip-ansi 互斥（后台作业无终端）")
        return 2
    prepared = _prepare_exec_command(args)
    if prepared[0] is None:
        _, (etype, emsg, pwarnings) = prepared
        emit_error(args.json, etype, emsg,
                   extra={"warnings": pwarnings} if pwarnings else None)
        return 2
    cmd, warnings, _sudo_pw, orig_cmd = prepared

    job_dir = getattr(args, "job_dir", None) or DEFAULT_JOB_DIR
    job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-%d" % os.getpid()
    paths = _job_files(job_dir, job_id)
    job_sh, run_sh = _detach_scripts(paths, cmd)

    # 凭据启发式：后台作业把命令原文落盘到远端 job.sh（比前台更需注意别写明文凭据）
    w = warn_sensitive_cmd(orig_cmd, enabled=not getattr(args, "no_credential_warn", False))
    if w:
        warnings.append(w + "；且 --detach 会把命令原文写入远端 %s（勿留明文凭据，用完清理）"
                        % paths["job"])

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec

    dir_made = False
    try:
        sftp = open_sftp(client)
        _sftp_mkdirs(sftp, paths["dir"])
        dir_made = True
        # 权限从严（v2.2.1）：目录 0700、job.sh 0600——命令正文与完整输出都落盘，
        # 同机其他用户不得读取；job.log/job.rc 由 run.sh 首行 umask 077 保证 0600
        _sftp_chmod(sftp, job_dir, 0o700)
        _sftp_chmod(sftp, paths["dir"], 0o700)
        _sftp_write_bytes(sftp, paths["job"], job_sh.encode("utf-8"))
        _sftp_write_bytes(sftp, paths["run"], run_sh.encode("utf-8"))
        _sftp_chmod(sftp, paths["job"], 0o600)
        _sftp_chmod(sftp, paths["run"], 0o700)
        # 启动：setsid+nohup 完全脱离会话；同一条命令里落 pid、sleep 0.3 探一次状态——
        # 短作业直接给 finished+exit_code，长作业给 running，立即死掉给 dead
        # （umask 077：这里创建的 job.pid 也是 0600）
        launch = (
            "umask 077\n"
            "setsid nohup bash %s >/dev/null 2>&1 </dev/null &\n"
            "P=$!\n"
            "echo \"$P\" > %s\n"
            "sleep 0.3\n"
            "if [ -f %s ]; then echo \"pyaissh_rc=$(cat %s)\";\n"
            "elif kill -0 $P 2>/dev/null; then echo pyaissh_state=running;\n"
            "else echo pyaissh_state=dead; fi\n"
            "echo \"pyaissh_pid=$P\"\n"
            % (_sh_quote(paths["run"]), _sh_quote(paths["pid"]),
               _sh_quote(paths["rc"]), _sh_quote(paths["rc"]))
        )
        stdin, stdout, stderr = client.exec_command(launch, timeout=args.exec_timeout)
        try:
            stdin.close()
        except Exception:
            pass
        launch_out = stdout.read().decode(args.encoding, errors="replace")
        launch_rc = stdout.channel.recv_exit_status()
        launch_err = stderr.read().decode(args.encoding, errors="replace")
        m_pid = re.search(r"pyaissh_pid=(\d+)", launch_out)
        m_rc = re.search(r"pyaissh_rc=(-?\d+)", launch_out)
        pid = int(m_pid.group(1)) if m_pid else None
        rc_val = int(m_rc.group(1)) if m_rc else None
        if launch_rc != 0:
            _detach_cleanup(sftp, paths)
            emit_error(args.json, "detach_failed",
                       "后台作业启动失败（启动命令退出码 %d）：%s"
                       % (launch_rc, (launch_err or launch_out).strip()[:JOB_ERR_TAIL]))
            return 255
        if rc_val is None and "pyaissh_state=running" not in launch_out:
            # 启动后 0.3s 进程已不在且没有 rc：读日志尾巴帮定位（路径保留供人工查看）
            tail = ""
            try:
                with sftp.open(paths["log"], "rb") as f:
                    data = f.read(JOB_ERR_TAIL)
                tail = data.decode(args.encoding, errors="replace")
            except Exception:
                pass
            emit_error(args.json, "detach_failed",
                       "后台作业启动后立即退出且未写退出码（可能命令不可执行/脚本语法错）。"
                       "日志 %s%s" % (paths["log"], ("：\n" + tail) if tail else "（空）"),
                       extra={"job_id": job_id, "log": paths["log"], "rc": paths["rc"],
                              "pid": pid})
            return 255

        cmd_echo, cmd_cut, cmd_n = _truncate_cmd(cmd)
        if cmd_cut:
            warnings.append("cmd 字段已截断（完整命令 %d 字节；远端作业脚本 %s 保留全文）"
                            % (cmd_n, paths["job"]))
        status = "finished" if rc_val is not None else "running"
        target_hint = "%s@%s" % (conn["user"], conn["host"])
        result = {
            "ok": True,
            "action": "exec",
            "version": VERSION,
            "detached": True,
            "job_id": job_id,
            "pid": pid,
            "status": status,
            "job_dir": paths["dir"],
            "log": paths["log"],
            "rc": paths["rc"],
            "pid_file": paths["pid"],
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "cmd": cmd_echo,
            "cmd_truncated": cmd_cut,
            "cmd_written_to": paths["job"],
            "permissions": {"job_dir": "0700", "job.sh": "0600", "run.sh": "0700",
                            "job.log": "0600", "job.rc": "0600", "job.pid": "0600"},
            "warnings": warnings,
            "duration_ms": int((time.time() - start) * 1000),
        }
        if rc_val is not None:
            result["exit_code"] = rc_val
            result["exit_success"] = rc_val == 0
            result["next_action"] = (
                "作业已结束（exit_code=%d）。用 pyaissh log %s --job-id %s 读日志（载荷字段 stdout）；"
                "--cleanup 清理远端作业目录" % (rc_val, target_hint, job_id))
        else:
            result["next_action"] = (
                "作业在远端后台运行（SSH 断开不影响）。读日志：pyaissh log %s --job-id %s"
                "（载荷字段 stdout；增量：--offset <next_offset>）；等结束拿退出码：--wait-rc 30；"
                "停掉：--kill；清理：--cleanup" % (target_hint, job_id))
        header = "[DETACHED %s] job_id=%s pid=%s log=%s" % (
            status, job_id, pid if pid is not None else "?", paths["log"])
        _emit_result(args, result, header=header)
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        if dir_made:
            log("[WARN] --detach 失败，远端可能残留作业目录 %s（可手工删除）" % paths["dir"])
        emit_error(args.json, "detach_failed", str(e))
        return 255
    finally:
        close_all(client)


def _log_list(sftp, client, job_dir, args, start):
    """列出作业目录下的所有作业（状态/日志大小/退出码/时间）。

    状态三级（v2.2.1）：finished（有 rc）/ dead（无 rc 且进程已消失）/ running；
    无 rc 的条目会做一次批量存活探测（单条 exec，避免 N 次往返）。
    """
    jobs = []
    try:
        attrs = sftp.listdir_attr(job_dir)
    except Exception:
        attrs = []
    pending = []           # 无 rc 的作业：待批量存活探测
    for a in attrs:
        try:
            if not stat.S_ISDIR(a.st_mode):
                continue
        except Exception:
            continue
        d = "%s/%s" % (str(job_dir).rstrip("/"), a.filename)
        log_p, rc_p = "%s/%s" % (d, JOB_LOG_NAME), "%s/%s" % (d, JOB_RC_NAME)
        pid_p = "%s/%s" % (d, JOB_PID_NAME)
        try:
            log_bytes = sftp.stat(log_p).st_size
        except Exception:
            log_bytes = None
        rc_val = _sftp_read_rc(sftp, rc_p)
        entry = {"job_id": a.filename, "log": log_p, "log_bytes": log_bytes,
                 "status": "finished" if rc_val is not None else "running",
                 "exit_code": rc_val, "mtime": int(getattr(a, "st_mtime", 0))}
        if rc_val is None:
            entry["_pid"] = _sftp_read_pid(sftp, pid_p)
            pending.append(entry)
        jobs.append(entry)
    if pending:
        # 一次 exec 批量探测（list 可能几十条，逐个 exec 太贵）
        amap = _alive_map(client, [e.get("_pid") for e in pending])
        for e in pending:
            pid = e.pop("_pid", None)
            if pid and amap.get(pid) is False:
                e["status"] = "dead"     # 无 rc 且进程已消失：被 kill / OOM / 崩溃
    jobs.sort(key=lambda e: e["mtime"], reverse=True)
    result = {"ok": True, "action": "log", "version": VERSION,
              "job_dir": job_dir, "count": len(jobs),
              "jobs": jobs[:args.limit], "warnings": [],
              "duration_ms": int((time.time() - start) * 1000)}
    if len(jobs) > len(result["jobs"]):
        result["truncated"] = True
    return result


def _log_read(sftp, client, args, job_dir, conn, start):
    """读单个作业日志：--offset 增量 / 默认尾部 N 行；带结束状态、--kill 与清理。

    返回结果 dict；读不到日志时 emit_error 并返回 None（调用方返回 2）。

    v2.2.1 两处语义修订：
    - 载荷字段 `content` → **`stdout`**（与 exec 的 stdout/stderr 命名一致，
      消费端不用猜；合并流用 `stream: "stdout+stderr"` 声明）
    - 状态三级：finished（有 rc）/ **dead**（无 rc 且进程已消失）/ running
      ——被 kill 的作业不再永远 running，--wait-rc 与轮询都能收敛
    """
    paths = _job_files(job_dir, args.job_id) if args.job_id else None
    if args.path:
        log_path = _normalize_remote_path(sftp, args.path)
        d = log_path.rsplit("/", 1)[0] if "/" in log_path else "."
        if log_path.endswith("/" + JOB_LOG_NAME):
            rc_path = "%s/%s" % (d, JOB_RC_NAME)
            pid_path = "%s/%s" % (d, JOB_PID_NAME)
        else:
            rc_path = pid_path = None
        job_id = args.job_id or (d.rsplit("/", 1)[-1] if rc_path else None)
        job_dir_out = d
        files = {"dir": d, "log": log_path, "rc": rc_path, "pid": pid_path}
    else:
        log_path, job_id = paths["log"], args.job_id
        job_dir_out, files = paths["dir"], paths
        rc_path, pid_path = paths["rc"], paths["pid"]

    # --kill：先整组停掉（TERM → 宽限 → KILL），随后照常读状态/等收敛
    kill_info = None
    if getattr(args, "kill", False):
        if not paths:
            emit_error(args.json, "bad_args", "--kill 需配合 --job-id（要靠作业目录里的 job.pid）")
            return None
        ok, info = _job_kill(client, sftp, paths)
        kill_info = dict(info)
        kill_info["ok"] = ok
        if not ok and info.get("reason") not in ("already_gone",):
            emit_error(args.json, "kill_failed", info.get("message") or "杀作业失败",
                       extra={"job_id": job_id, "pid": info.get("pid"),
                              "reason": info.get("reason")})
            return None

    # --wait-rc：轮询到状态不再是 running（rc 出现 或 进程消失）或超时
    status, rc_val, pid = _job_status(sftp, client, files)
    waited_ms = 0
    if args.wait_rc and status == "running":
        t0 = time.time()
        deadline = t0 + args.wait_rc
        while time.time() < deadline:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            time.sleep(JOB_POLL_TICK)
            status, rc_val, pid = _job_status(sftp, client, files)
            if status != "running":
                break
        waited_ms = int((time.time() - t0) * 1000)

    try:
        size = sftp.stat(log_path).st_size
    except Exception as e:
        emit_error(args.json, "job_not_found",
                   "读不到日志文件 %s：%s（用 --list 看现有作业；作业目录 %s）"
                   % (log_path, e, job_dir_out))
        return None

    content_raw = b""
    next_offset = None
    has_more = False
    tail_window_cut = False
    if args.offset is not None:
        off = max(0, int(args.offset))
        if off > size:
            off = size
        with sftp.open(log_path, "rb") as f:
            f.seek(off)
            content_raw = f.read(args.max_output)
        next_offset = off + len(content_raw)
        has_more = next_offset < size
    else:
        n = args.lines if args.lines else JOB_TAIL_LINES
        back = min(size, JOB_TAIL_WINDOW)
        start_off = size - back
        tail_window_cut = start_off > 0
        with sftp.open(log_path, "rb") as f:
            f.seek(start_off)
            data = f.read(back)
        text = data.decode(args.encoding, errors="replace")
        picked = text.splitlines()[-n:]
        content_raw = "\n".join(picked).encode(args.encoding, errors="replace")
        if picked:
            content_raw += b"\n"
        next_offset = size

    cut, truncated, omitted = _truncate_output(content_raw, args.max_output, "log")
    result = {
        "ok": True, "action": "log", "version": VERSION,
        "job_id": job_id, "log": log_path, "rc": rc_path, "pid_file": pid_path,
        "pid": pid, "status": status,
        "exit_code": rc_val,
        "exit_success": (rc_val == 0) if rc_val is not None else None,
        "log_bytes": size,
        "stdout": cut.decode(args.encoding, errors="replace"),
        "stream": "stdout+stderr",     # job.log 是 2>&1 合并流，别误以为只有 stdout
        "bytes_returned": len(cut),
        "next_offset": next_offset, "has_more": has_more,
        "tail_window_truncated": tail_window_cut,
        "truncated": truncated, "omitted_bytes": omitted,
        "wait_rc_secs": args.wait_rc or None, "waited_ms": waited_ms,
        "host": conn["host"], "user": conn["user"], "port": conn["port"],
        "warnings": [], "duration_ms": int((time.time() - start) * 1000),
    }
    if kill_info is not None:
        result["kill"] = kill_info
    if args.cleanup and paths:
        result["cleaned_paths"] = _detach_cleanup(sftp, paths)
        result["cleaned"] = True
    if status == "finished":
        result["next_action"] = ("作业已结束（exit_code=%s）。%s"
                                 % (rc_val, "已清理远端作业目录"
                                    if result.get("cleaned") else
                                    "如需清理：加 --cleanup"))
    elif status == "dead":
        result["hint"] = ("无 job.rc 且进程已消失（被 kill / OOM / 崩溃）：退出码不可知，"
                          "状态机按 dead 收敛，不要再用 --wait-rc 等")
        result["next_action"] = ("作业已死（无退出码）。看日志尾部确认原因；%s"
                                 % ("已清理远端作业目录" if result.get("cleaned")
                                    else "清理：加 --cleanup"))
    else:
        result["next_action"] = ("作业仍在运行。继续增量读：--offset %s（或稍后重读）；"
                                 "等结束：--wait-rc 30；停掉：--kill"
                                 % (next_offset if next_offset is not None else 0))
    if truncated and args.offset is None:
        # 尾部模式被 _truncate_output 截中段：has_more=false + next_offset=EOF 会让人
        # 以为"读完了"，其实中段缺失（省略 omitted 字节）——明确给出补齐路径
        result["next_action"] = ("内容被截断（省略 %d 字节）；完整读取请用 --offset 0 顺序读"
                                 "（按 next_offset 续读）。" % omitted) + result["next_action"]
    return result


def cmd_log(args):
    """log（别名 tail）：读后台作业日志 + 结束状态，支持增量读与清理。

    与其他子命令同契约：stdout 单行 JSON；--field 可提取 content/exit_code/jobs 等。
    """
    start = time.time()
    job_dir = getattr(args, "job_dir", None) or DEFAULT_JOB_DIR
    if args.list_jobs and (args.job_id or args.path):
        emit_error(args.json, "bad_args", "--list 不与 --job-id/--path 同用（列清单即可）")
        return 2
    if not args.list_jobs and not (args.job_id or args.path):
        emit_error(args.json, "bad_args", "需指定 --job-id 或 --path（列作业清单用 --list）")
        return 2
    if args.job_id and not _JOB_ID_RE.match(args.job_id):
        emit_error(args.json, "bad_args",
                   "非法 --job-id %r（只允许字母/数字/下划线/点/连字符，防路径穿越）"
                   % (args.job_id,))
        return 2
    if args.lines is not None and args.offset is not None:
        emit_error(args.json, "bad_args", "--lines 与 --offset 互斥（尾部 N 行 / 增量读，二选一）")
        return 2
    if args.wait_rc and args.wait_rc > JOB_WAIT_MAX:
        emit_error(args.json, "bad_args",
                   "--wait-rc 上限 %d 秒（宿主单次调用约 600s 上限；更久作业请稍后轮询）"
                   % JOB_WAIT_MAX)
        return 2
    if args.cleanup and not args.job_id:
        emit_error(args.json, "bad_args", "--cleanup 需配合 --job-id（清理整个作业目录）")
        return 2
    if args.cleanup and args.wait_rc:
        emit_error(args.json, "bad_args", "--cleanup 与 --wait-rc 互斥（先等结束再清理，分两次调用）")
        return 2
    if args.kill and not args.job_id:
        emit_error(args.json, "bad_args", "--kill 需配合 --job-id（要靠作业目录里的 job.pid 整组停掉）")
        return 2
    if args.kill and args.list_jobs:
        emit_error(args.json, "bad_args", "--kill 不与 --list 同用")
        return 2

    conn, client, conn_ec = _connect_exec(args)
    if conn_ec is not None:
        return conn_ec
    try:
        sftp = open_sftp(client)
        if args.list_jobs:
            result = _log_list(sftp, client, job_dir, args, start)
            _emit_result(args, result, header="[JOBS] %d in %s" % (result["count"], job_dir))
            return 0
        result = _log_read(sftp, client, args, job_dir, conn, start)
        if result is None:
            return 2
        head = ("[FINISHED exit_code=%s]" % result["exit_code"]
                if result["status"] == "finished" else
                "[DEAD 无退出码]" if result["status"] == "dead" else "[RUNNING]")
        _emit_result(args, result, header="%s job_id=%s log=%s"
                     % (head, result.get("job_id"), result["log"]),
                     sections=[("LOG", result["stdout"])])
        return 0
    except KeyboardInterrupt:
        emit_error(args.json, "interrupted", _interrupt_msg())
        return 130
    except SshError as e:
        emit_error(args.json, e.error_type, str(e))
        return 255
    except Exception as e:
        emit_error(args.json, "log_failed", str(e))
        return 255
    finally:
        close_all(client)


