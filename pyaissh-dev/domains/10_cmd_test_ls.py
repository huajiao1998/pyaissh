"""test/ls 子命令实现（域 10）。

- cmd_test：连通/认证探测（hostname/os/kernel/arch；stdout EOF 后等 exit-status 宽限）
- cmd_ls：SFTP 列表（entries[] name/mode/size/is_dir/is_symlink/mtime；~ 展开；无 glob）
"""

def cmd_test(args):
    start = time.time()  # 计时含连接耗时
    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    try:
        # 取服务器信息（带超时兜底：uname 挂死也不能卡住 test）
        info_cmd = "uname -s; uname -r; uname -m; hostname"
        stdin, stdout, stderr = client.exec_command(info_cmd, timeout=args.timeout)
        try:
            stdin.close()
        except Exception:
            pass
        chan = stdout.channel
        chan.settimeout(1.0)
        deadline = time.time() + args.timeout
        chunks = []
        err_chunks = []  # M4: 读 stderr 辅助诊断（uname 报错信息不丢）
        while time.time() < deadline:
            if _SIGTERM_RECEIVED:
                # 信号已到：read 循环是安全检查点，主动抛 KI 走 interrupted/130
                #（否则 responder 关 socket 后 recv 异常 break 会走正常路径，
                #  拼出 ok:true + 退出码 0，信号被完全吞掉——实测 P1）
                raise KeyboardInterrupt("SIGTERM")
            try:
                data = chan.recv(RECV_CHUNK)
            except (socket.timeout, TimeoutError):
                # 无 stdout 数据：趁机收 stderr（BUG-9：EOF 后 stderr 尾部不能丢）
                try:
                    ed = chan.recv_stderr(RECV_CHUNK)
                    if ed:
                        err_chunks.append(ed)
                except Exception:
                    pass
                continue
            except Exception:
                break
            if not data:
                # stdout EOF：命令已结束（uname 类），短窗口（1s）收完 stderr 尾部
                # 即退出，不再空转到固定 deadline（否则每次 test 固定耗时 --timeout 秒）
                eof_at = time.time()
                while time.time() - eof_at < STDERR_EOF_WINDOW:
                    try:
                        ed = chan.recv_stderr(RECV_CHUNK)
                    except Exception:
                        break
                    if not ed:
                        break
                    err_chunks.append(ed)
                break
            chunks.append(data)
            try:
                ed = chan.recv_stderr(RECV_CHUNK)
                if ed:
                    err_chunks.append(ed)
            except Exception:
                pass
        status_ready = chan.exit_status_ready()
        if not status_ready:
            # stdout EOF 后 exit-status 包可能还在路上（高延迟链路实测会晚到）：
            # 短等 2s 再下结论，避免把成功的探测误报成"超时/未完成"
            _status_wait = time.time() + STATUS_GRACE
            while time.time() < _status_wait and not chan.exit_status_ready():
                time.sleep(POLL_TICK)
            status_ready = chan.exit_status_ready()
        out_s = b"".join(chunks).decode(args.encoding, errors="replace").strip().splitlines()
        err_s = b"".join(err_chunks).decode(args.encoding, errors="replace").strip()
        warnings = []
        if not status_ready:
            # M4: 超时/EOF 未收到退出状态 -> 明确提示探测未完成（不再静默 ok=true）
            warnings.append("服务器信息探测超时/未完成（未收到命令退出状态，结果可能不完整）")
        if err_s:
            warnings.append("服务器信息探测 stderr: %s" % _sanitize_log_text(err_s[:200]))
        if len(out_s) < 4:
            warnings.append("服务器信息探测不完整（uname/hostname 输出缺失，命令可能被限制或系统特殊）")
        os_name = out_s[0] if len(out_s) > 0 else ""
        os_kernel = out_s[1] if len(out_s) > 1 else ""
        os_arch = out_s[2] if len(out_s) > 2 else ""
        hostname = out_s[3] if len(out_s) > 3 else ""
        if _SIGTERM_RECEIVED:
            # 循环退出与 emit 之间的窗口（EOF 快速路径/status 等待期）收到信号：
            # 同样归位 interrupted/130，避免 ok:true + 0 吞掉取消语义
            raise KeyboardInterrupt("SIGTERM")

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "test",
            "version": VERSION,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "hostname": hostname,
            "os": os_name,
            "kernel": os_kernel,
            "arch": os_arch,
            "warnings": warnings,
            "duration_ms": duration,
        }
        header = "[OK]  连接成功 %dms  %s@%s" % (duration, conn["user"], hostname or conn["host"])
        sections = [("INFO", json.dumps(result, ensure_ascii=False, indent=2))]
        _emit_result(args, result, header=header, sections=sections)
        return 0
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除阻塞：按中断而非 test_failed 归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "test_failed", str(e),
                   extra=_conn_extra(locals().get("conn")))
        return 255
    finally:
        close_all(client)


def cmd_ls(args):
    start = time.time()  # 计时含连接耗时
    # 仅 None（未指定）才用默认 "."；空串/纯空白交给 _normalize_remote_path
    # 报 bad_args（与 download/upload 的空路径守卫一致，or "." 会把空串也兜掉）
    path = _fix_msys_remote_path(args.path if args.path is not None else ".")

    try:
        conn = resolve_conn(args)
        client = connect(conn, resolve_jump(args, conn["user"]))
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 连接期收到信号（transport 未注册，响应线程救了也来不及救）：按标志归位中断
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 255
    except Exception as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程关闭 socket 解除连接阻塞：按中断而非连接失败归类
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "connection_failed", str(e), extra=_conn_extra(locals().get("conn")))
        return 255

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠；~user 形式明确报错）
        path = _normalize_remote_path(sftp, path)
        log("[SFTP] 列目录 %s" % path)
        try:
            _sftp_touch_activity(sftp)  # 防慢链路大目录被看门狗误杀（M4）
            entries = sftp.listdir_attr(path)
        except (socket.timeout, TimeoutError):
            raise  # 重抛给外层统一报 ls_timeout（M2：timeout 是 IOError 子类，先拦会吞掉分类）
        except IOError as e:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")  # 交给外层按中断处理（安全帧内）
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
                raise  # 外层按看门狗分支报 ls_timeout
            if e.errno == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                # OpenSSH 对"不存在"和"不是目录"都返回 SFTP_NO_SUCH_FILE，无法区分；
                # 确实不存在且含通配符时，真实原因是"SFTP 无 glob"而非路径拼错
                if any(c in path for c in "*?["):
                    msg = _remote_glob_error(path)
                else:
                    msg = "远程路径不存在或不是目录: %s" % path
                emit_error(args.json, "bad_args", msg,
                           extra=_conn_extra(locals().get("conn")))
                return 2
            emit_error(args.json, "ls_failed",
                       "无法读取远程路径 %s（可能不是目录或权限不足）: %s" % (path, e),
                       extra=_conn_extra(locals().get("conn")))
            return 1

        entries.sort(key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower()))
        total = len(entries)
        truncated = total > args.limit
        if truncated:
            entries = entries[:args.limit]

        items = []
        lines = []
        names = []
        for e in entries:
            is_dir = stat.S_ISDIR(e.st_mode)
            is_symlink = stat.S_ISLNK(e.st_mode)
            mode = stat.filemode(e.st_mode)
            mtime = int(e.st_mtime)  # epoch 秒（UTC）：AI 跨机比较时间无时区歧义
            # 目录的 st_size 是目录项/inode 尺寸而非内容大小：置 null 防误读
            fsize = None if is_dir else e.st_size
            # 文件名编码说明：paramiko 按 UTF-8+replace 解码 SFTP 文件名（原始字节
            # 不可还原）——GBK 等非 UTF-8 文件名会以 U+FFFD 显示（实测确认，
            # v1.5.13 曾试 --encoding 还原，paramiko 层信息已丢，回滚）；文件名
            # 建议保持 UTF-8，或经 exec `ls -b`/base64 自行取原始字节
            name = e.filename + ("/" if is_dir else "")  # 目录名带 / 后缀，AI 拼路径时先去尾
            # entries schema 恒定（不随 --long 变化）：--long 只额外打印文本清单行
            items.append({"name": name, "mode": mode, "size": fsize,
                          "is_dir": is_dir, "is_symlink": is_symlink, "mtime": mtime})
            mtime_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
            lines.append("%s %8s %s %s" % (mode,
                                           "-" if fsize is None else str(fsize),
                                           mtime_s, name))
            names.append(name)
        content = "\n".join(lines if args.long else names)

        if truncated:
            content += "\n[pyaissh] 共 %d 条，仅显示前 %d 条（--limit 调整）" % (total, len(items))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "ls",
            "version": VERSION,
            "path": path,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "count": len(items),
            "total": total,          # 目录内条目总数（截断前）
            "truncated": truncated,  # 超过 --limit 被截断
            "entries": items,
            "warnings": [],          # 与其他子命令 schema 对齐（恒有该键）
            "duration_ms": duration,
        }
        header = "[OK]  %d 项, %dms" % (len(items), duration)
        sections = [("LS", content)]
        _emit_result(args, result, header=header, sections=sections)
        return 0
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        emit_error(args.json, "ls_timeout",
                   "SFTP 超时：%d 秒无任何数据（连接可能已被断开）" % SFTP_IO_TIMEOUT,
                   extra=_conn_extra(locals().get("conn")))
        return 1
    except SshError as e:
        # _normalize_remote_path 的 ~user 拒绝等结构化错误：保持 bad_args/2
        # 语义（落到 generic 会被误报 ls_failed/1）
        emit_error(args.json, e.error_type, str(e), extra=_conn_extra(locals().get("conn")))
        return 2 if e.error_type == "bad_args" else 1
    except Exception as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(),
                       extra=_conn_extra(locals().get("conn")))
            return 130
        # 看门狗强制断开（服务器静默断链）报 ls_timeout 而非 ls_failed（与 upload/download 一致）
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
            emit_error(args.json, "ls_timeout",
                       "SFTP 超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_conn_extra(locals().get("conn")))
        else:
            emit_error(args.json, "ls_failed", str(e),
                       extra=_conn_extra(locals().get("conn")))
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


# =========================================================================
# 参数解析
# =========================================================================

