"""upload/download 子命令实现（域 09）。

- cmd_upload：校验 -> 预置失败上下文 -> 连接 -> sftp 主流程（单文件/递归/并行/
  --resume/--dry-run/--skip-existing）-> 结果（file_list/bytes/parallel_used）
- cmd_download：对称；断点重试/符号链接/路径安全化
底层原语在 07_sftp_transfer.py；失败上下文 _transfer_extra 见 07。
"""

def _excluded_by(rel, name, pats):
    """--exclude 匹配（v2.1）：任一模式（fnmatch glob）命中文件名或相对路径即排除。
    rel 传入正斜杠相对路径（Windows 也先归一）。"""
    for p in pats:
        if not p:
            continue
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p):
            return True
    return False


def cmd_upload(args):
    start = time.time()  # 计时含连接耗时
    local = _fix_msys_local_path(args.local)
    remote = _fix_msys_remote_path(args.remote)
    # 用户意图标记：--remote 以 / 结尾 = 期望目标是目录（scp 语义）。
    # _normalize_remote_path 会剥掉尾斜杠，这里先记录，供单文件分支区分
    # "目标应是目录但不存在"——静默创建同名文件是静默错误（应报错）
    remote_ends_slash = remote.endswith("/")
    if not os.path.exists(local):
        msg = "本地路径不存在: %s" % local
        if any(c in local for c in _MSYS_GLOB_CHARS) \
                or any(c in _MSYS_PRIVATE_GLOB for c in local):
            msg += "（路径含通配符：pyaissh 不做本地 glob 展开，请先在 shell 展开成明确路径）"
        emit_error(args.json, "bad_args", msg)
        return 2

    # 失败上下文预置（try 之前）：任何异常路径的 extra 字段都齐全且一致
    files_transferred = 0
    files_skipped = 0
    total_bytes = 0        # 清单总大小（含 skipped；实际传了多少看 bytes_transferred）
    bytes_transferred = 0  # 实际传输字节（skip-existing 全跳过时为 0，与 bytes 区分）
    file_list = None       # None=尚未开始；空列表=刚开始就失败（同样有断点价值）
    walk_warnings = []
    bytes_uploaded = [0]  # put 回调累计已传字节：中断/失败时 JSON 报真实进度
    parallel_used = 1     # 本次实际并行连接数（结果回显，AI 无需猜测档位；对称下载）
    if args.dry_run and args.skip_existing:
        # dry-run 零远端 I/O，无法预演 skip 判定；stderr 与结果 warnings 双通道说明
        msg = "dry-run 不做远端 I/O，--skip-existing 未预演（实跑时才判定）"
        log("[WARN] " + msg)
        walk_warnings.append(msg)

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

    def _fail_extra():
        """失败/中断的统一 extra：host/user/port（与连接期错误一致）+ 传输进度。"""
        return _transfer_extra(
            conn,
            file_list=file_list or [],
            files=files_transferred,
            skipped=files_skipped,
            bytes=total_bytes,
            bytes_transferred=max(bytes_transferred, bytes_uploaded[0]),
            warnings=list(walk_warnings) + list(_PUT_RESIDUE_WARNINGS))

    is_dir = os.path.isdir(local)
    no_recur = (args.recursive is False)  # 显式 --no-recursive
    tag = "递归" if (is_dir and not no_recur) else ("目录(不递归)" if is_dir else "单文件")
    log("[SFTP] 上传 %s -> %s (%s%s)" % (
        local, remote, tag, ", dry-run" if args.dry_run else ""))
    # --exclude（v2.1）：逗号分隔 glob，目录整树剪枝 / 文件跳过（不上传不计数）
    exclude_pats = [p.strip() for p in (args.exclude or "").split(",") if p.strip()]
    if exclude_pats:
        log("[EXCL] 排除模式: %s" % ", ".join(exclude_pats))

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠）；上传是"新建路径"，含 glob 字符
        # 几乎必是笔误——按字面量会创建出名为 * 的文件/目录，直接拒绝
        remote = _normalize_remote_path(sftp, remote)
        if any(c in remote for c in "*?["):
            raise SshError("远端路径 %s 含通配符：SFTP 不做 glob 展开（会按字面量创建），"
                           "请写明确的完整路径" % remote, "bad_args")
        file_list = []  # 每项 {"path","size","transferred","skipped"}：失败时 AI 可精确断点重试

        if args.resume and is_dir:
            msg = "--resume 仅对单文件上传生效；目录上传忽略 --resume"
            log("[WARN] " + msg)
            walk_warnings.append(msg)

        if is_dir and no_recur:
            # 目录但 --no-recursive: 不递归，只创建远程目录壳
            if not args.dry_run:
                sftp_makedirs(sftp, remote)
            log("[SKIP] 目录 + --no-recursive: 只创建远程目录，不传子项")
        elif is_dir:
            # 目录：递归上传（onerror 收集不可读目录，避免静默部分成功）
            def _walk_onerror(err):
                walk_warnings.append(_sanitize_log_text(
                    "跳过无法读取的本地目录: %s (%s)" % (getattr(err, "filename", "?"), err)))
            for root, dirs, filenames in os.walk(local, onerror=_walk_onerror):
                rel_root = os.path.relpath(root, local)
                remote_root = remote if rel_root == "." else posixpath.join(
                    remote, rel_root.replace(os.sep, "/"))
                if exclude_pats:
                    dirs[:] = [d for d in dirs
                               if not _excluded_by(os.path.join(rel_root, d).replace(os.sep, "/"),
                                                   d, exclude_pats)]
                    filenames = [f for f in filenames
                                 if not _excluded_by(os.path.join(rel_root, f).replace(os.sep, "/"),
                                                     f, exclude_pats)]
                if not args.dry_run:
                    sftp_makedirs(sftp, remote_root)
                for fn in filenames:
                    if _SIGTERM_RECEIVED:
                        raise KeyboardInterrupt("SIGTERM")  # 文件间是安全检查点
                    local_file = os.path.join(root, fn)
                    remote_file = posixpath.join(remote_root, fn)
                    size = os.path.getsize(local_file)
                    rel = os.path.relpath(local_file, local).replace(os.sep, "/")
                    skip = bool(args.skip_existing and (not args.dry_run)
                                and _remote_size_is(sftp, remote_file, size))
                    entry = {"path": rel, "size": size, "transferred": False, "skipped": skip}
                    file_list.append(entry)
                    total_bytes += size
                    if skip:
                        files_skipped += 1
                        log("[SKIP] %s (%s, 远端已存在同大小文件)" % (rel, format_size(size)))
                        continue
                    if not args.dry_run:
                        _sftp_put_atomic(sftp, local_file, remote_file, progress=bytes_uploaded)  # 权限由服务器 umask 决定（默认如 644）
                        entry["transferred"] = True
                        bytes_transferred += size
                    log("[FILE] %s (%s)" % (rel, format_size(size)))
                    files_transferred += 1
        else:
            # 单文件
            size = os.path.getsize(local)
            name = os.path.basename(local)
            if size >= RESUME_MIN_SIZE and not args.resume:
                tip = ("文件 %s ≥ %s，建议加 --resume 断点续传"
                       "（中断后重试不重传已传部分）" % (name, format_size(RESUME_MIN_SIZE)))
                log("[TIP] " + tip)
                walk_warnings.append(tip)
            rstat = None
            if not args.dry_run:  # dry-run 应零远端 I/O
                try:
                    _sftp_touch_activity(sftp)  # 刷新看门狗活动时间
                    rstat = sftp.stat(remote)
                except (socket.timeout, TimeoutError):
                    raise  # 外层统一报 upload_timeout（M2：不能吞掉超时）
                except IOError:
                    rstat = None  # 不存在，正常创建
                if rstat is None and remote_ends_slash:
                    # 尾斜杠 + 目标不存在：用户意图是目录（scp 语义下尾斜杠
                    # 表示"放进此目录"），静默创建同名文件是静默错误——
                    # 此前实测会创建 /root/newdir 同名文件且 remote 回显
                    # 无尾斜杠路径，AI 误判落点是目录。明确报 bad_args
                    raise SshError(
                        "远端路径 %s 以 / 结尾（意图是目录）但目标不存在："
                        "请先创建该目录，或去掉尾斜杠改为文件路径" % remote,
                        "bad_args")
                if rstat is not None and remote_ends_slash and not stat.S_ISDIR(rstat.st_mode):
                    # 尾斜杠 + 目标存在但【不是目录】：同样意图是目录（fix ⑨
                    # 只挡了"不存在"半边，这里补"是文件"半边）——此前实测会
                    # 静默覆盖文件且 ok:true。明确报 bad_args，绝不静默覆盖
                    raise SshError(
                        "远端路径 %s 以 / 结尾（意图是目录）但目标已存在且不是目录："
                        "请改为文件路径，或先删除该文件" % remote,
                        "bad_args")
                if rstat is not None and stat.S_ISDIR(rstat.st_mode):
                    # scp 语义：目标是已存在目录 -> 放入目录内（不报错、不嵌套）
                    remote = posixpath.join(remote, name)
                    log("[PATH] 远端目标是目录，改为放入: %s" % remote)
                    rstat = None
                    try:
                        rstat = sftp.stat(remote)
                    except (socket.timeout, TimeoutError):
                        raise
                    except IOError:
                        rstat = None
            if args.dry_run and remote_ends_slash:
                # dry-run 承诺零远端 I/O，无法确认尾斜杠目标的类型：实跑时
                # 目标不存在/已存在文件都会报 bad_args，仅已存在目录会放入其中
                walk_warnings.append(
                    "dry-run 未验证尾斜杠目标 %s 的类型（实跑时目标不存在或"
                    "非目录会报 bad_args；已存在目录则放入其中）" % remote)
            skip = bool(args.skip_existing and rstat is not None and rstat.st_size == size)
            entry = {"path": name, "size": size, "transferred": False, "skipped": skip}
            file_list.append(entry)  # 先入清单再传输：失败时 AI 能定位到具体文件
            total_bytes = size  # 失败时 bytes 也要反映清单大小（提前赋值，不能只在成功路径设）
            if skip:
                files_skipped = 1
                log("[SKIP] %s (%s, 远端已存在同大小文件)" % (name, format_size(size)))
            elif not args.dry_run:
                parent = posixpath.dirname(remote)
                created = []
                if parent:
                    created = sftp_makedirs(sftp, parent)
                if created:
                    # 单文件上传自动 mkdir -p 父目录：warnings 提示新建了哪些目录
                    #（AI 可见；拼写错误的垃圾目录树（如 /usr/loca/bin/x）可被发现）
                    tip = ("已自动创建远端父目录: %s（单文件上传 mkdir -p 语义；"
                           "若为路径拼写错误请检查 remote）" % ", ".join(created))
                    log("[MKDIR] " + tip)
                    walk_warnings.append(tip)
                if args.parallel and size >= 64 * 1024:
                    # 显式 --parallel：分片上传（对称下载分片，慢链路提速）。
                    # 默认不加自动档（upload 行为零变化，用户主动提速才用）；
                    # 与 --resume 互斥：分片上传不做续传
                    if args.resume:
                        walk_warnings.append("--parallel 上传与 --resume 互斥，忽略 --resume"
                                             "（分片上传不做续传）")
                        log("[WARN] --parallel 上传与 --resume 互斥，忽略 --resume")
                    parallel_used = args.parallel
                    try:
                        # _parallel_put 内部用活跃 worker 连接完成原子改名与
                        # 失败清理（主 sftp 空闲 30s 会被看门狗杀，不能参与）
                        _parallel_put(conn, args, local, remote, size, args.parallel)
                    except BaseException:
                        # 兜底清理：worker 连接清理失败时主连接再试一次
                        try:
                            sftp.remove(_part_path(remote))
                        except Exception:
                            pass
                        raise
                else:
                    _sftp_put_atomic(sftp, local, remote, progress=bytes_uploaded,
                                     resume=bool(args.resume))
                entry["transferred"] = True
                files_transferred = 1
                bytes_transferred = size
            else:
                files_transferred = 1  # dry-run：假装会传（bytes_transferred 保持 0，真实反映零传输）
            log("[FILE] %s (%s)" % (name, format_size(size)))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "upload",
            "version": VERSION,
            "local": local,
            "remote": remote,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "is_dir": is_dir,
            "files": files_transferred,
            "skipped": files_skipped,
            "bytes": total_bytes,           # 清单总大小（含 skipped）
            "bytes_transferred": bytes_transferred,  # 实际传输字节（skip 全跳过时为 0）
            "parallel_used": parallel_used,  # 实际分片连接数：单连接=1，分片=--parallel 值（AI 确认档位，对称下载）
            "file_list": file_list,
            "dry_run": bool(args.dry_run),
            "warnings": list(walk_warnings) + list(_PUT_RESIDUE_WARNINGS),
            "duration_ms": duration,
        }
        header = "[OK]  %d 文件, %s, %dms%s%s" % (
            files_transferred, format_size(total_bytes), duration,
            ", 跳过 %d" % files_skipped if files_skipped else "",
            " (dry-run)" if args.dry_run else "")
        sections = [("RESULT", json.dumps(result, ensure_ascii=False))]
        _emit_result(args, result, header=header, sections=sections)
        return 0
    except KeyboardInterrupt as e:
        # 中断也带清单（transferred 标记精确到文件）：AI 判断重试策略
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_fail_extra())
        return 130
    except SshError as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        # sftp_makedirs 的"远程路径已存在且不是目录"、通配符拒绝等参数类错误：
        # 退出码 2（改参数可解决），不是传输失败 1
        emit_error(args.json, e.error_type, str(e), extra=_fail_extra())
        return 2 if e.error_type == "bad_args" else 1
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        emit_error(args.json, "upload_timeout",
                   "SFTP 传输超时：%d 秒无任何数据（连接可能已被 NAT/网络静默断开）" % SFTP_IO_TIMEOUT,
                   extra=_fail_extra())
        return 1
    except Exception as e:
        # 失败带清单：未完成的那条 transferred=false，AI 知道差哪些
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
            emit_error(args.json, "upload_timeout",
                       "SFTP 传输超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_fail_extra())
        else:
            emit_error(args.json, "upload_failed", str(e), extra=_fail_extra())
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


def cmd_download(args):
    start = time.time()  # 计时含连接耗时
    local = _fix_msys_local_path(args.local)
    remote = _fix_msys_remote_path(args.remote)
    if not local:
        # --local '' 之类：os.replace('.part.<pid>', '') 会抛 FileNotFoundError
        # （实测 download_failed + 晦涩消息）。空目标路径是参数错误，明确 bad_args
        emit_error(args.json, "bad_args", "本地目标路径为空（--local 未指定有效路径）")
        return 2

    # 失败上下文预置（try 之前）：任何异常路径的 extra 字段都齐全且一致
    files_transferred = 0
    files_skipped = 0
    total_bytes = 0        # 清单总大小（含 skipped；实际传了多少看 bytes_transferred）
    bytes_transferred = 0
    file_list = None       # None=尚未开始；空列表=刚开始就失败（同样有断点价值）
    parallel_used = 1      # 本次实际并行连接数（结果回显，AI 无需猜测档位）
    dl_warnings = []

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

    def _fail_extra():
        """失败/中断的统一 extra：host/user/port（与连接期错误一致）+ 传输进度。"""
        # --resume 中断：本地固定续传点保留时，warnings 提示下次可续传
        #（串行路径的 _atomic_local_write 只 log stderr TIP，这里补 JSON 通道；
        #  分片路径的 except 已单独加过，靠内容去重）
        if getattr(args, "resume", False) and os.path.exists(local + ".part"):
            tip = "下载中断，本地续传点已保留: %s（下次重试加 --resume 从断点继续）" % (local + ".part")
            if tip not in dl_warnings:
                dl_warnings.append(tip)
        return _transfer_extra(
            conn,
            file_list=file_list or [],
            files=files_transferred,
            skipped=files_skipped,
            bytes=total_bytes,
            bytes_transferred=bytes_transferred,
            warnings=list(dl_warnings))

    sftp = None
    try:
        sftp = open_sftp(client)
        # 远端路径规范化（~ 展开 / 去尾斜杠；~user 形式明确报错）
        remote = _normalize_remote_path(sftp, remote)
        try:
            _sftp_touch_activity(sftp)  # 刷新看门狗活动时间
            rstat = sftp.stat(remote)
        except (socket.timeout, TimeoutError):
            raise  # 外层统一报 download_timeout（M2）
        except IOError as e:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")  # 交给命令层 KI 分支按中断处理
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
                raise  # 外层按看门狗分支报 download_timeout
            if e.errno == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                # 确实不存在时若路径含通配符，说明真实原因是"SFTP 无 glob"而非文件缺失
                if any(c in remote for c in "*?["):
                    msg = _remote_glob_error(remote)
                else:
                    msg = "远程路径不存在: %s" % remote
                emit_error(args.json, "bad_args", msg,
                           extra=_conn_extra(locals().get("conn")))
            else:
                # 权限不足等真实错误：不能误导为"路径不存在"
                emit_error(args.json, "download_failed",
                           "无法访问远程路径 %s（不存在或权限不足）: %s" % (remote, e),
                           extra=_conn_extra(locals().get("conn")))
                return 1
            return 2

        is_dir = stat.S_ISDIR(rstat.st_mode)
        no_recur = (args.recursive is False)  # 显式 --no-recursive
        tag = "递归" if (is_dir and not no_recur) else ("目录(不递归)" if is_dir else "单文件")
        log("[SFTP] 下载 %s -> %s (%s%s)" % (
            remote, local, tag, ", dry-run" if args.dry_run else ""))

        file_list = []  # 每项 {"path","size","transferred","skipped"}：失败时 AI 可精确断点重试

        if args.resume and is_dir:
            msg = "--resume 仅对单文件下载生效；目录下载忽略 --resume"
            log("[WARN] " + msg)
            dl_warnings.append(msg)

        if is_dir and os.path.isfile(local):
            # 本地目标已存在且是文件：os.makedirs(..., exist_ok=True) 会抛
            # FileExistsError（实测 download_failed 消息晦涩），且与"目录下载"
            # 语义冲突——明确报 bad_args（远端是文件时覆盖本地文件仍允许）
            emit_error(args.json, "bad_args",
                       "本地目标 %s 已存在且是文件，不能作为目录下载目标"
                       "（请换目录路径或先删除该文件）" % local,
                       extra=_transfer_extra(conn, file_list=file_list or []))
            return 2

        if is_dir and no_recur:
            # 目录但 --no-recursive: 不递归，只创建本地目录壳
            if not args.dry_run:
                os.makedirs(local, exist_ok=True)
            log("[SKIP] 目录 + --no-recursive: 只创建本地目录，不下载子项")
        elif is_dir:
            if not args.dry_run:
                os.makedirs(local, exist_ok=True)
            if args.parallel is not None:
                # 显式 --parallel 对目录下载不生效（逐文件串行），明说防误导
                dl_warnings.append("--parallel 仅对单文件分片下载生效；"
                                   "目录下载为逐文件串行（parallel_used=1）")
            used_local_names = set()  # Windows 大小写不敏感/清洗后同名的冲突检测
            for rel, full, attr, entry_is_dir in sftp_walk(sftp, remote, warnings=dl_warnings):
                if _SIGTERM_RECEIVED:
                    raise KeyboardInterrupt("SIGTERM")  # 条目间是安全检查点
                if entry_is_dir:
                    # 空目录也要重建：walk 现在产出目录条目（先于其子项），
                    # 只处理文件的话空目录会静默消失
                    if os.name == "nt":
                        # 目录名同样要过 Windows 安全化（保留设备名/控制字符/
                        # 大小写冲突），否则危险目录名可能让整树无法创建
                        safe = _win_safe_rel_path(rel, used_local_names, dl_warnings)
                        if safe is None:
                            continue
                        rel = safe
                    if not args.dry_run:
                        os.makedirs(os.path.join(local, rel.replace("/", os.sep)),
                                    exist_ok=True)
                    continue
                if stat.S_ISLNK(attr.st_mode):
                    # 符号链接一律跳过而不是跟随：悬空链接 sftp.get 报
                    # "No such file"、指向目录的报 "Failure"，都会中止整个
                    # 目录下载且消息无法理解；lstat 的 size 是链接串长度，
                    # 跟随下载还会让 file_list/bytes 记账失真、skip-existing
                    # 永不命中。需要内容的请对指向的具体路径单独 download。
                    dl_warnings.append(_sanitize_log_text(
                        "跳过符号链接 %s（SFTP 目录下载不跟随链接；如需内容请对指向的具体路径单独 download）" % rel))
                    continue
                if stat.S_ISFIFO(attr.st_mode) or stat.S_ISSOCK(attr.st_mode) \
                        or stat.S_ISCHR(attr.st_mode) or stat.S_ISBLK(attr.st_mode):
                    # FIFO/套接字/设备文件：服务端 open(FIFO) 会阻塞写端等读端，
                    # sftp.get 挂到 30s 看门狗后中止整个目录（实测 0 文件到达）
                    dl_warnings.append(_sanitize_log_text(
                        "跳过非常规文件 %s（FIFO/套接字/设备文件无法经 SFTP 下载）" % rel))
                    continue
                if os.name == "nt":
                    # Windows 下 Linux 文件名（控制字符/foo:bar/a*b 等）会导致
                    # 整目录下载失败：统一清洗/跳过危险段/冲突改名（与目录条目
                    # 同一逻辑，见 _win_safe_rel_path）
                    safe = _win_safe_rel_path(rel, used_local_names, dl_warnings)
                    if safe is None:
                        continue  # 跳过整个文件
                    rel = safe
                local_file = os.path.join(local, rel.replace("/", os.sep))
                size = attr.st_size
                skip = bool(args.skip_existing and os.path.isfile(local_file)
                            and os.path.getsize(local_file) == size)
                entry = {"path": rel, "size": size, "transferred": False, "skipped": skip}
                file_list.append(entry)
                total_bytes += size
                if skip:
                    files_skipped += 1
                    log("[SKIP] %s (%s, 本地已存在同大小文件)" % (_sanitize_log_text(rel), format_size(size)))
                    continue
                if not args.dry_run:
                    parent = os.path.dirname(local_file)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    _atomic_local_write(
                        lambda part, _r=full: sftp.get(_r, part, callback=_make_sftp_touch(sftp)),
                        local_file)
                    # 保留远程权限位但掩掉 setuid/setgid/sticky（0o4000/0o2000/0o1000）：
                    # 远端文件属性不受本方控制，root 下载 4755 文件会在本地造出
                    # setuid-root 二进制，属本地提权落点
                    try:
                        os.chmod(local_file, stat.S_IMODE(attr.st_mode) & 0o777)
                    except OSError:
                        # chmod 失败不能算传输失败：文件已原子改名就位，降级为
                        # warning（Windows 只读属性等场景；报 download_failed 会让
                        # AI 误判重下已经就位的文件）
                        dl_warnings.append("设置文件权限失败（已下载，权限未应用）: %s"
                                           % _sanitize_log_text(rel))
                    entry["transferred"] = True
                    bytes_transferred += size
                log("[FILE] %s (%s)" % (_sanitize_log_text(rel), format_size(size)))
                files_transferred += 1
        else:
            size = rstat.st_size
            name = os.path.basename(remote)
            if size >= RESUME_MIN_SIZE and not args.resume:
                tip = ("文件 %s ≥ %s，建议加 --resume 断点续传"
                       "（中断后重试不重传已传部分）" % (name, format_size(RESUME_MIN_SIZE)))
                log("[TIP] " + tip)
                dl_warnings.append(tip)
            if not name:
                emit_error(args.json, "bad_args",
                           "无法从远程路径 %s 确定文件名（根目录/以 / 结尾），"
                           "请写完整的远程文件路径" % remote,
                           extra=_transfer_extra(conn, file_list=file_list or []))
                return 2
            if os.path.isdir(local):
                # scp 语义：--local 是已存在目录 -> 文件放入该目录（--local . 可用）
                local = os.path.join(local, name)
                log("[PATH] 本地目标是目录，改为放入: %s" % local)
            skip = bool(args.skip_existing and os.path.isfile(local)
                        and os.path.getsize(local) == size)
            entry = {"path": name, "size": size, "transferred": False, "skipped": skip}
            file_list.append(entry)  # 先入清单再传输：失败时 AI 能定位到具体文件
            total_bytes = size  # 失败时 bytes 也要反映清单大小（提前赋值，不能只在成功路径设）
            if skip:
                files_skipped = 1
                log("[SKIP] %s (%s, 本地已存在同大小文件)" % (name, format_size(size)))
            elif not args.dry_run:
                parent = os.path.dirname(local)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                k = args.parallel
                if k is None:
                    k = 4 if size >= PARALLEL_MIN_SIZE else 1
                # 显式 --parallel 时只要求 64KB 就并行（尊重用户意图）；
                # 自动档 1MB 下限（k 已由 8MB 阈值把关，这里是双保险）
                if k > 1 and size >= (64 * 1024 if args.parallel is not None else 1024 * 1024):
                    parallel_used = k
                    # 大文件多连接分片：规避单 TCP 流在高丢包链路的吞吐塌陷。
                    # 先关空闲的主 SFTP 会话：它的看门狗（30s）会强断主 transport
                    # 的 socket，连带杀死复用该 transport 的分片（实测踩坑）。
                    # 写 .part 成功后原子改名：失败/中断删除 .part，绝不留下
                    # "看似完整实则损坏"的半截文件（空洞会被 --skip-existing 误判）
                    try:
                        sftp.close()
                    except Exception:
                        pass
                    sftp = None
                    part = local + ".part" if args.resume else _part_path(local)
                    try:
                        _parallel_fetch(conn, args, remote, part, size, k,
                                        resume=bool(args.resume))
                        # 清理分片 done 标记（--resume 续传时产生），再原子改名
                        if args.resume:
                            for i in range(k):
                                dp = "%s.done.%d" % (part, i)
                                if os.path.exists(dp):
                                    try:
                                        os.remove(dp)
                                    except OSError:
                                        pass
                        os.replace(part, local)
                    except BaseException:
                        if not args.resume:
                            try:
                                if os.path.exists(part):
                                    os.remove(part)
                            except OSError:
                                # Windows 下句柄未释放等会让删除失败：稍等重试一次，
                                # 仍失败至少留 WARN（静默残留违背"不留半截文件"承诺）
                                time.sleep(RETRY_SLEEP)
                                try:
                                    os.remove(part)
                                except OSError:
                                    log("[WARN] 清理临时文件失败（可能有进程占用）： %s" % part)
                        else:
                            # 续传模式：保留 .part + done 标记作为续传资产；
                            # 中断若发生在分片建连阶段（.part 未创建）则如实说明
                            if os.path.exists(part):
                                log("[TIP] 已保留本地续传点 %s（下次重试加 --resume 从断点继续）" % part)
                                dl_warnings.append(
                                    "下载中断，本地续传点已保留: %s（下次重试加 --resume 从断点继续）" % part)
                            else:
                                log("[TIP] 下载中断于分片建连阶段，未产生续传点；"
                                    "下次重试加 --resume 全量重传")
                        raise
                else:
                    # 串行单连接同样走 .part + 原子改名：原子性不应随文件大小/
                    # 并行度静默变化（小文件中断也曾留下最终名的半截文件）
                    if args.resume:
                        # --resume：从本地 .part 已有字节续传（_sftp_get_resume 内部
                        # 处理"不存在全量/已有续传/大小异常覆盖"三种情形）
                        _atomic_local_write(
                            lambda part: _sftp_get_resume(sftp, remote, part, size),
                            local, resume=True)
                    else:
                        _atomic_local_write(
                            lambda part: sftp.get(remote, part, callback=_make_sftp_touch(sftp)),
                            local)
                try:
                    os.chmod(local, stat.S_IMODE(rstat.st_mode) & 0o777)  # 掩掉 setuid 等特殊位
                except OSError:
                    # 文件已就位，chmod 失败降级为 warning（同目录路径的处理）
                    dl_warnings.append("设置文件权限失败（已下载，权限未应用）: %s"
                                       % _sanitize_log_text(name))
                entry["transferred"] = True
                files_transferred = 1
                bytes_transferred = size
            else:
                files_transferred = 1  # dry-run：假装会传（bytes_transferred 保持 0，真实反映零传输）
            log("[FILE] %s (%s)" % (name, format_size(size)))

        duration = int((time.time() - start) * 1000)
        result = {
            "ok": True,
            "action": "download",
            "version": VERSION,
            "remote": remote,
            "local": local,
            "host": conn["host"],
            "user": conn["user"],
            "port": conn["port"],
            "is_dir": is_dir,
            "files": files_transferred,
            "skipped": files_skipped,
            "bytes": total_bytes,           # 清单总大小（含 skipped）
            "bytes_transferred": bytes_transferred,  # 实际传输字节（skip 全跳过时为 0）
            "parallel_used": parallel_used,
            "file_list": file_list,
            "dry_run": bool(args.dry_run),
            "warnings": list(dl_warnings),
            "duration_ms": duration,
        }
        header = "[OK]  %d 文件, %s, %dms%s%s" % (
            files_transferred, format_size(total_bytes), duration,
            ", 跳过 %d" % files_skipped if files_skipped else "",
            " (dry-run)" if args.dry_run else "")
        sections = [("RESULT", json.dumps(result, ensure_ascii=False))]
        _emit_result(args, result, header=header, sections=sections)
        return 0
    except KeyboardInterrupt as e:
        emit_error(args.json, "interrupted", _interrupt_msg(),
                   extra=_fail_extra())
        return 130
    except SshError as e:
        if _SIGTERM_RECEIVED:
            # 信号响应线程强断连接会让 _parallel_fetch 的分片报 "Socket is closed"
            # 并以 SshError 抛出——按标志归位为中断，而非 download_failed
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        # _parallel_fetch / 分片建连抛出的结构化错误：保留 error_type
        #（download_timeout 不能被泛化成 download_failed；worker 建连失败属连接类 255）
        emit_error(args.json, e.error_type, str(e), extra=_fail_extra())
        if e.error_type == "bad_args":
            return 2
        if e.error_type in ("auth_failed", "connection_timeout", "connection_refused",
                            "connection_failed", "dns_failed", "jump_failed",
                            "host_key_rejected", "ssh_error"):
            return 255
        return 1
    except (socket.timeout, TimeoutError):
        if _SIGTERM_RECEIVED:
            # 信号打断串行 SFTP I/O 时 KI 常被 paramiko 展开中的新异常覆盖，
            # 落到这里——按标志强制归位为 interrupted/130
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        emit_error(args.json, "download_timeout",
                   "SFTP 传输超时：%d 秒无任何数据（连接可能已被 NAT/网络静默断开）" % SFTP_IO_TIMEOUT,
                   extra=_fail_extra())
        return 1
    except Exception as e:
        if _SIGTERM_RECEIVED:
            emit_error(args.json, "interrupted", _interrupt_msg(), extra=_fail_extra())
            return 130
        if "sftp" in locals() and getattr(sftp, "_pyaissh_watchdog_killed", False):
            emit_error(args.json, "download_timeout",
                       "SFTP 传输超时：%d 秒无数据（服务器静默断链，已强制断开）" % SFTP_IO_TIMEOUT,
                       extra=_fail_extra())
        else:
            emit_error(args.json, "download_failed", str(e), extra=_fail_extra())
        return 1
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        close_all(client)


