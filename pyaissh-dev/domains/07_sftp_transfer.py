"""SFTP 传输层（域 07）：上传/下载的底层原语。

- open_sftp（惰性 + 看门狗）、_sftp_watchdog（30s 静默保活）
- 并行分片：_parallel_fetch / _parallel_put（多连接，高丢包链路提速）
- 原子性：_sftp_atomic_rename（posix-rename 或退化）、_sftp_put_atomic、.part 机制
- 断点：_sftp_get_resume / _remote_size_is、sftp_makedirs / sftp_walk
- _win_safe_rel_path（Windows 文件名安全化）、_transfer_extra（失败进度上下文）
被 cmd_upload/cmd_download（09）调用。
"""

def _sftp_watchdog(sftp):
    """SFTP 看门狗线程：超过 io_timeout 无数据传输则强制断开。

    背景（实测确认）：paramiko 的 SFTP 读响应走 transport 的 packetizer，
    channel.settimeout / sock.settimeout 对它都无效——服务器静默断链
    （NAT 超时、网络黑洞、服务端 sftp-server 挂起）时 put/get/listdir
    会无限阻塞，违背"任何路径不无限卡住"的承诺。
    看门狗在 open_sftp 时启动（daemon），put/get 通过 callback 刷新
    活动时间；超时后强制 sftp.close()，让阻塞中的操作抛异常返回，
    上层转 upload_failed/download_failed/ls_failed 等错误类型。
    """
    io_timeout = getattr(sftp, "_pyaissh_io_timeout", SFTP_IO_TIMEOUT)
    try:
        while True:
            time.sleep(WATCHDOG_TICK)
            try:
                # paramiko 5.0 的 SFTPClient：self.sock 是 channel（有 closed），
                # transport 通过 sock.get_transport() 取
                if sftp.sock.closed:
                    return
            except Exception:
                return
            if time.time() - sftp._pyaissh_last_activity > io_timeout:
                sftp._pyaissh_watchdog_killed = True
                log("[WARN] SFTP %d 秒无数据传输，强制断开（服务器可能已静默断链）"
                    % io_timeout)
                # 关底层 TCP socket：sftp.close()/channel.close() 都打断不了
                # 阻塞在 packetizer 的读（实测确认），只有 socket.close() 能
                # 让阻塞中的 put/get/listdir 立即抛异常返回
                try:
                    transport = sftp.sock.get_transport()
                    if transport is not None and transport.sock is not None:
                        transport.sock.close()
                except Exception:
                    pass
                try:
                    sftp.close()
                except Exception:
                    pass
                return
    except Exception:
        pass


def open_sftp(client, io_timeout=None):
    """打开带 I/O 超时兜底的 SFTP 会话。

    paramiko 默认不给 SFTP 设超时，连接被 NAT 静默丢弃时 put/get/listdir
    会无限阻塞。这里双重兜底：
      1) channel/sock settimeout（对部分读路径有效）；
      2) 看门狗线程（对 put/get 等阻塞读有效，见 _sftp_watchdog）。
    持续有数据流动的慢传输不受影响（callback 持续刷新活动时间）。
    io_timeout 可对单会话放宽（分片下载工作线程用更长的窗口）。
    """
    io_timeout = io_timeout or SFTP_IO_TIMEOUT
    sftp = client.open_sftp()
    try:
        sftp.get_channel().settimeout(io_timeout)
    except Exception:
        try:
            sftp.sock.settimeout(io_timeout)
        except Exception:
            pass
    sftp._pyaissh_io_timeout = io_timeout
    sftp._pyaissh_last_activity = time.time()
    sftp._pyaissh_watchdog = threading.Thread(target=_sftp_watchdog, args=(sftp,), daemon=True)
    sftp._pyaissh_watchdog.start()
    return sftp


def _parallel_fetch(conn, args_, remote, local, size, k, resume=False):
    """多连接分片下载：k 条独立 SSH 连接各下载一段，写入同一本地文件。

    背景（实测）：paramiko 单连接的 SFTP 读是"发一个请求等一个响应"，
    高丢包/长 RTT 链路（如跨境）上单条 TCP 流吞吐塌陷（~20KB/s 且会
    长时间停滞触发看门狗）；独立连接数近似线性提升吞吐（8 连接 ~5.5 倍）。
    全部分片用独立连接（不与主会话共用 transport）：空闲主会话的看门狗
    强断时会连带杀死同 transport 的其他通道（实测踩坑）。
    本地文件生命周期：调用方应传 <目标>.part 路径——全部连接建立成功后
    才创建/清空 .part，任一分片失败抛 SshError（download_failed/download_timeout）
    或建连失败抛连接类 SshError，由调用方负责删除 .part 并原子改名收尾。
    resume=True（--resume 分片续传）：不清空已有 .part；每个分片完成后写
    <目标>.part.done.<i> 标记，重跑时存在标记的分片直接跳过（省已完成分片
    的传输量）。调用方在全部完成后负责清理 .done.* 并 os.replace。"""
    workers = []   # [client, sftp, start, end, got]
    errors = []
    shared_jump = None
    try:
        bounds = [(i * size // k, (i + 1) * size // k if i < k - 1 else size)
                  for i in range(k)]
        # 跳板只建【一条】连接，k 个分片各开一条 direct-tcpip 隧道共享它
        # （否则每分片各建一条跳板 SSH：8 分片 = 16 条连接；共享后 = 9 条，
        #   弱跳板/限连接数环境下差异显著。转发隧道不受 sshd MaxSessions 限制）
        jump_conn = resolve_jump(args_, conn["user"])
        if jump_conn:
            try:
                shared_jump = _do_connect(jump_conn, None, is_jump=True)
            except SshError as e:
                raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e),
                               e.error_type)
            log("[JUMP] 分片共享跳板连接 -> %s@%s:%s"
                % (jump_conn["user"], jump_conn["host"], jump_conn["port"]))
        # 先建全部连接再动本地文件：建连失败（服务器并发会话限制/认证被限流/
        # 跳板资源不足）时，调用方的本地目标文件保持原样不受损
        for i, (start, end) in enumerate(bounds):
            client = None  # 每轮重置：_do_connect 抛出时 except 里不会误关上一轮的 client
            try:
                client = _do_connect(conn, shared_jump)
                sftp = open_sftp(client, io_timeout=PARALLEL_IO_TIMEOUT)
            except BaseException:
                # 半建的 client 不在 workers 里，finally 不会关它：这里先关再抛
                #（_do_connect 内部失败时已自关，重复 close 幂等无害）
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                raise
            workers.append([client, sftp, start, end, 0])
        if resume:
            open(local, "ab").close()  # 续传：保留已有数据（ab 模式不清空；不存在则创建）
        else:
            with open(local, "wb"):
                pass  # 连接全部就绪后才预创建清空 .part，分片线程以 r+b 各自定位写入

        def _run(i):
            client, sftp, start, end, _ = workers[i]
            done_path = "%s.done.%d" % (local, i)
            if resume and os.path.exists(done_path):
                # 该分片上次已完整写完（done 标记存在）：跳过，节省传输量
                workers[i][4] = end - start
                log("[SKIP] 分片 %d/%d 已完成（续传点），跳过" % (i + 1, k))
                return
            rf = lf = None
            try:
                rf = sftp.open(remote, "rb")
                lf = open(local, "r+b")  # 各线程独立句柄定位写入
                off = start
                while off < end:
                    if _SIGTERM_RECEIVED:
                        # 信号已到：不再发新读请求，尽快退出让主线程 join 收尾。
                        # 不加这个检查点，慢链路下 worker 会阻塞在 rf.read()
                        # 的 paramiko 内部读里，socket 被 _signal_responder 关闭后
                        # Windows recv 未必立即报错，拖到 120s 看门狗才释放
                        # （实测 --parallel 2/4 中断延迟 90-145s）。
                        raise KeyboardInterrupt("SIGTERM")
                    rf.seek(off)
                    data = rf.read(min(PARALLEL_READ_CHUNK, end - off))
                    if not data:
                        raise IOError("远端在偏移 %d 处提前 EOF（文件传输中被修改？）" % off)
                    lf.seek(off)
                    lf.write(data)
                    off += len(data)
                    workers[i][4] = off - start
                    _sftp_touch_activity(sftp)
                # 分片完整写完：写 done 标记（--resume 重跑时跳过已完成分片）
                if resume:
                    try:
                        open(done_path, "wb").close()
                    except Exception:
                        pass
            except KeyboardInterrupt:
                # 信号中断归位：不记入 errors（否则主线程会把信号误报成
                # download_failed），由主线程检查 _SIGTERM_RECEIVED 走 130 路径
                pass
            except Exception as e:
                errors.append((i, e))
            finally:
                # 异常路径也要关句柄：Windows 下本进程打开的句柄会让
                # 上层 os.remove(.part) 抛 PermissionError，残留半截文件。
                # close 失败（磁盘满等，缓冲可能未落盘）也计入 errors：
                # 否则按内存计数判成功、rename 上位的可能是坏文件
                for h in (lf, rf):
                    try:
                        if h is not None:
                            h.close()
                    except Exception as ce:
                        errors.append((i, ce))

        log("[PART] %d 连接分片下载 %s（每片约 %s）"
            % (k, format_size(size), format_size(size // k)))
        threads = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(k)]
        for t in threads:
            t.start()
        # join 用短超时轮询 + 信号检查：worker 阻塞在 rf.read() 的 paramiko
        # 内部读时（慢链路 + 信号关闭 socket 后 Windows recv 不立即报错），
        # 无超时 join 会干等到 120s 看门狗。置标志后不等慢 worker，直接进
        # finally 关闭连接收尾（daemon 线程随之消亡），保证信号秒级退出。
        while any(t.is_alive() for t in threads):
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            for t in threads:
                t.join(0.1)
        if errors:
            # 任一分片被看门狗杀/读超时都按超时归类（不能只看 errors[0]：
            # 首个错误可能是普通失败、后面的才是超时）
            timed_out = any(
                getattr(workers[i][1], "_pyaissh_watchdog_killed", False)
                or isinstance(e, (socket.timeout, TimeoutError))
                for i, e in errors)
            if timed_out:
                raise SshError("并行分片下载超时（%d 连接，单片 %d 秒无数据；"
                               "高丢包链路可调整 --parallel 重试——过高反而可能适得其反"
                               "（8 不行试 4/2），或稍后重试）"
                               % (k, PARALLEL_IO_TIMEOUT), "download_timeout")
            i, e = errors[0]
            raise SshError("并行分片 %d/%d 下载失败: %s" % (i + 1, k, e), "download_failed")
        got = sum(w[4] for w in workers)
        if got != size:
            raise SshError("下载不完整：得到 %d/%d 字节" % (got, size), "download_failed")
        return got
    finally:
        for client, sftp, start, end, _ in workers:
            try:
                sftp.close()
            except Exception:
                pass
            close_all(client)
        # 显式兜底：首个 worker 建连即失败时 workers 为空，shared_jump
        # 无人顺带关闭（close_all 幂等，重复关无害）
        if shared_jump is not None:
            close_all(shared_jump)


def _parallel_put(conn, args_, local, remote, size, k):
    """多连接分片上传：k 条独立 SSH 连接各上传本地文件的一段，写入远端同一 .part。

    对称于 _parallel_fetch（下载分片）：高丢包/长 RTT 链路单连接 SFTP 写吞吐
    同样塌陷，多连接近似线性提升（dogfood 实测：慢链路上传大文件是真实痛点，
    --parallel 仅下载生效的缺口在此补齐）。共享跳板隧道（只建一条跳板连接）。
    远端 .part 由主连接预创建（worker 以 r+b seek 写；并发 w+b 会互截空），
    全部写满后**用活跃的 worker 连接完成原子改名**（_sftp_atomic_rename）——
    不能用调用方的主 sftp：分片上传期间主连接空闲 30s 会被看门狗强断（实测
    dogfood 踩坑：50MB 分片 257s 后 rename 用主连接 -> 连接已死 upload_timeout）。
    失败/中断时用活跃 worker 连接清理 .part（keep_part 双丢防护保留）。"""
    part = _part_path(remote)  # 进程唯一名（分片上传不做续传）
    workers = []   # [client, sftp, start, end, got]
    errors = []
    shared_jump = None
    try:
        bounds = [(i * size // k, (i + 1) * size // k if i < k - 1 else size)
                  for i in range(k)]
        jump_conn = resolve_jump(args_, conn["user"])
        if jump_conn:
            try:
                shared_jump = _do_connect(jump_conn, None, is_jump=True)
            except SshError as e:
                raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e),
                               e.error_type)
            log("[JUMP] 分片共享跳板连接 -> %s@%s:%s"
                % (jump_conn["user"], jump_conn["host"], jump_conn["port"]))
        for i, (start, end) in enumerate(bounds):
            client = None
            try:
                client = _do_connect(conn, shared_jump)
                sftp = open_sftp(client, io_timeout=PARALLEL_IO_TIMEOUT)
            except BaseException:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                raise
            workers.append([client, sftp, start, end, 0])
        # 主连接预创建远端空 .part（worker 的 r+b 需要文件已存在）
        _sftp_touch_activity(workers[0][1])
        f0 = workers[0][1].open(part, "wb")
        f0.close()

        def _run(i):
            client, sftp, start, end, _ = workers[i]
            lf = rf = None
            try:
                lf = open(local, "rb")
                rf = sftp.open(part, "r+b")  # 各线程独立句柄定位写入
                off = start
                while off < end:
                    if _SIGTERM_RECEIVED:
                        # 信号已到：不再发新写请求，尽快退出让主线程 join 收尾
                        #（同 _parallel_fetch：不加检查点会阻塞在慢链路的
                        #  paramiko 内部读写里，拖到看门狗才释放）
                        raise KeyboardInterrupt("SIGTERM")
                    lf.seek(off)
                    data = lf.read(min(PARALLEL_READ_CHUNK, end - off))
                    if not data:
                        raise IOError("本地文件在偏移 %d 处提前 EOF" % off)
                    rf.seek(off)
                    rf.write(data)
                    off += len(data)
                    workers[i][4] = off - start
                    _sftp_touch_activity(sftp)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                errors.append((i, e))
            finally:
                for h in (lf, rf):
                    try:
                        if h is not None:
                            h.close()
                    except Exception as ce:
                        errors.append((i, ce))

        log("[PART] %d 连接分片上传 %s（每片约 %s）"
            % (k, format_size(size), format_size(size // k)))
        threads = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(k)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            for t in threads:
                t.join(POLL_TICK)
        if errors:
            timed_out = any(
                getattr(workers[i][1], "_pyaissh_watchdog_killed", False)
                or isinstance(e, (socket.timeout, TimeoutError))
                for i, e in errors)
            if timed_out:
                raise SshError("并行分片上传超时（%d 连接，单片 %d 秒无数据；"
                               "高丢包链路可调整 --parallel 重试——过高反而可能适得其反"
                               "（8 不行试 4/2），或稍后重试）"
                               % (k, PARALLEL_IO_TIMEOUT), "upload_timeout")
            i, e = errors[0]
            raise SshError("并行分片 %d/%d 上传失败: %s" % (i + 1, k, e), "upload_failed")
        got = sum(w[4] for w in workers)
        if got != size:
            raise SshError("上传不完整：得到 %d/%d 字节" % (got, size), "upload_failed")
        # 大小校验：所有分片写满区间后 .part 应等于本地大小
        _sftp_touch_activity(workers[0][1])
        rsize = workers[0][1].stat(part).st_size
        if rsize != size:
            raise SshError("分片上传大小校验失败（远端 %d != 本地 %d）"
                           % (rsize, size), "upload_failed")
        # 原子改名用活跃的 worker 连接（主 sftp 空闲 30s 会被看门狗杀）
        _sftp_atomic_rename(workers[0][1], part, remote)
    except BaseException as e:
        # 失败/中断：用活跃 worker 连接清理 .part（主连接可能已被看门狗杀；
        # keep_part 双丢防护：改名回退失败时 part 是新数据唯一副本，保留不删）
        if not getattr(e, "keep_part", False):
            cleaned = False
            for _, sftp, *_ in workers:
                try:
                    sftp.remove(part)
                    cleaned = True
                    break
                except Exception:
                    continue
            if not cleaned:
                # 极窄的静默残留窗口：所有 worker 连接已死（如传输中网络整体
                # 断开）→ 远端 .part 清不掉。part 名带 pid 下次不撞名，纯卫生
                # 问题；但 SKILL.md 第 7 条承诺"warnings 会提示清理命令"——
                # 串行路径由 _PUT_RESIDUE_WARNINGS 兜底，并行路径这里补上
                msg = ("并行分片上传中断，远端临时文件可能残留: %s"
                       "（清理：rm -f '%s'）" % (part, part))
                log("[WARN] " + msg)
                _PUT_RESIDUE_WARNINGS.append(msg)
        raise
    finally:
        for client, sftp, start, end, _ in workers:
            try:
                sftp.close()
            except Exception:
                pass
            close_all(client)
        if shared_jump is not None:
            close_all(shared_jump)


def _remote_size_is(sftp, rpath, size):
    """远程文件存在且大小一致（--skip-existing 的判断依据：仅比大小，不比内容/时间）"""
    try:
        _sftp_touch_activity(sftp)
        return sftp.stat(rpath).st_size == size
    except (socket.timeout, TimeoutError):
        raise  # 超时是连接问题：不能当"远端不存在"误判成需要重传（M2）
    except IOError:
        if getattr(sftp, "_pyaissh_watchdog_killed", False):
            raise  # 看门狗强制断开：同上，交给外层报 timeout
        return False


def sftp_makedirs(sftp, remote_dir):
    """递归创建远程目录（类似 os.makedirs，已存在则跳过）。

    返回本次【新建】的目录列表（外层→内层顺序；已存在的不算）。
    v1.5.6 起：单文件上传用它自动建父目录后，调用方据返回值提示
    AI 新建了哪些目录（拼写错误的垃圾目录树可被发现）；目录传输的
    自动创建是文档明示行为，调用方不检查返回值即保持静默。"""
    remote_dir = remote_dir.rstrip("/")
    if remote_dir in ("", ".", "/"):
        return []
    try:
        _sftp_touch_activity(sftp)
        st = sftp.stat(remote_dir)
        # 已存在但不是目录：必须报错。静默通过会让目录上传假成功
        # （--no-recursive 变 ok:true 零传输）或后续 put 报出误导性错误
        if not stat.S_ISDIR(st.st_mode):
            raise SshError("远程路径已存在且不是目录: %s（请换目标路径或先处理该文件）"
                           % remote_dir, "bad_args")
        return []  # 已存在
    except (socket.timeout, TimeoutError):
        raise
    except SshError:
        raise
    except IOError:
        pass
    parent = posixpath.dirname(remote_dir)
    created = []
    if parent and parent != remote_dir:
        created = sftp_makedirs(sftp, parent)
    try:
        _sftp_touch_activity(sftp)
        sftp.mkdir(remote_dir)
    except (socket.timeout, TimeoutError):
        raise
    except SshError:
        raise
    except IOError:
        # 再 stat 一次：并发下已创建可接受；权限不足/父项是文件等真实错误要上报
        try:
            _sftp_touch_activity(sftp)
            sftp.stat(remote_dir)
        except (socket.timeout, TimeoutError):
            raise
        except IOError:
            raise
        return created  # 并发下已存在：不算本次新建
    created.append(remote_dir)  # 递归先建父、自身后建：列表保持外层→内层
    return created


def sftp_walk(sftp, remote_dir, warnings=None):
    """递归遍历远程目录，yield (relpath, fullpath, attr, is_dir)。

    目录与文件都产出（目录先于其子项）：下载端据此创建本地目录——
    空目录也能重建（只 yield 文件的话空目录会静默消失）。
    warnings 列表非 None 时，把跳过不可读目录的警告收集进去（进结果 JSON）。
    """
    stack = [(remote_dir, "")]
    while stack:
        current, rel = stack.pop()
        try:
            _sftp_touch_activity(sftp)  # 目录操作也刷新看门狗（防慢链路误杀）
            entries = sftp.listdir_attr(current)
        except (socket.timeout, TimeoutError):
            raise  # 连接超时不是"目录不可读"：继续遍历会连环误报（M2）
        except IOError as e:
            if getattr(sftp, "_pyaissh_watchdog_killed", False):
                raise
            msg = "跳过无法读取的远程目录: %s (%s)" % (current, e)
            log("[WARN] " + _sanitize_log_text(msg))
            if warnings is not None:
                warnings.append(_sanitize_log_text(msg))
            continue
        # 排序保证遍历顺序跨运行稳定：Windows 冲突改名的 .dupN 映射依赖处理
        # 顺序（OpenSSH listdir 顺序不保证），稳定序才能让重复下载幂等
        entries.sort(key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower()))
        for entry in entries:
            # 拒绝危险文件名：SFTP 条目按规范不含 /，含 / 或 .. 说明服务端被入侵
            # 或异常，直接拼接会造成本地路径穿越
            if "/" in entry.filename or entry.filename in (".", ".."):
                log("[WARN] 跳过危险文件名: %r" % _sanitize_log_text(entry.filename))
                continue
            full = posixpath.join(current, entry.filename)
            r = posixpath.join(rel, entry.filename) if rel else entry.filename
            if stat.S_ISDIR(entry.st_mode):
                yield r, full, entry, True
                stack.append((full, r))
            else:
                yield r, full, entry, False


# =========================================================================
# 子命令实现
# =========================================================================

def _part_path(target):
    """进程唯一的临时文件名：<target>.part.<pid>。
    固定名 .part 在两个进程并发写同一目标时被共享：POSIX 下慢方在快方
    os.replace 后仍持 fd 继续写（写的是改名后的 inode），会把快方已报成功
    的文件原地污染成两源的混合垃圾——唯一名让并发退化为"完整的一方胜出"，
    败方干净报错，绝不产出损坏文件。"""
    return "%s.part.%d" % (target, os.getpid())


def _atomic_local_write(write_fn, local, resume=False):
    """本地原子落盘：write_fn(part) 写 .part，成功后 os.replace。
    失败/中断默认删除 .part——绝不留下"看似完整实则损坏"的半截文件
    （空洞文件会被 --skip-existing 按大小误判为已传完）。
    resume=True（--resume 续传模式）：.part 用固定名 <目标>.part（跨进程
    可续传），失败/中断【保留】它供下次 --resume 续传（log TIP 提示）。"""
    part = local + ".part" if resume else _part_path(local)
    try:
        write_fn(part)
        os.replace(part, local)
    except BaseException:
        if not resume:
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
            # 续传模式：保留 .part 作为续传点，提示下次可续传
            log("[TIP] 已保留续传点 %s（下次重试加 --resume 从断点继续）" % part)
        raise


def _sftp_atomic_rename(sftp, part, remote):
    """远端原子改名：posix-rename 覆盖；服务器不支持时退化为 remove+rename
    （非原子，WARN 一次）；回退前守卫防误删他人成果；回退也失败保留 part
    （keep_part 双丢防护标记，外层跳过清理）。从 _sftp_put_atomic 抽出，
    供串行上传与分片上传共用同一原子收尾。"""
    try:
        sftp.posix_rename(part, remote)
    except Exception as rename_err:
        # 回退前守卫：part 不在了说明状态已异常（如被外部动过），
        # 此时 remove(remote) 可能删掉别人的成果——抛原始的 rename 错误
        # （不能 bare raise：那会重抛内层 stat 的异常，错误消息指向不准）
        try:
            sftp.stat(part)
        except Exception:
            raise rename_err
        if not getattr(sftp, "_pyaissh_posix_rename_warned", False):
            sftp._pyaissh_posix_rename_warned = True
            log("[WARN] 服务器不支持原子改名（posix-rename 扩展），"
                "本次用删除+改名代替（该服务器上中断可能留下 .part 或旧文件）")
        try:
            sftp.remove(remote)
        except IOError:
            pass  # 目标原本不存在，直接改名即可
        try:
            sftp.rename(part, remote)
        except Exception as fallback_err:
            # 回退也失败：绝不删 .part——它是新数据的唯一副本，删了就是
            # "旧文件已删 + 新数据也丢"的双丢。保留 part 并明确告警，
            # AI 能手动恢复或安全重试（keep_part 标记让外层跳过清理）
            msg = ("远端原子改名失败且回退改名也失败（旧文件已删除，新数据"
                   "保留在临时文件 %s）：%s" % (part, fallback_err))
            log("[WARN] " + msg)
            _PUT_RESIDUE_WARNINGS.append(msg)
            err = SshError("上传失败：远端原子改名失败且回退也失败，"
                           "新数据保留在 %s（旧文件已删除，请手动恢复或重试）: %s"
                           % (part, fallback_err), "upload_failed")
            err.keep_part = True
            raise err


def _sftp_put_atomic(sftp, local, remote, progress=None, resume=False):
    """SFTP 上传 + 远端原子改名：先传 .part（默认进程唯一名 <pid>；--resume 时
    固定名 <remote>.part 且支持断点续传），成功后 posix-rename 覆盖。服务器不
    支持 posix-rename 扩展时退化为 remove+rename（非原子，WARN 一次）；回退前
    先确认 .part 仍在，防止把别的进程刚改完名的成果误删。progress 可选 [0]
    列表：累计已传字节（中断/失败时结果 JSON 能报真实进度，而不是恒 0）。
    resume=True：远端 .part 已存在且小于本地大小 → 从断点续传；大于等于本地
    大小 → 视为已完成/损坏，覆盖重传；中断保留续传点（不清理、不告警残留）。"""
    part = remote + ".part" if resume else _part_path(remote)
    # .part 是否已被创建/写入：put 回调置位。清理失败时据此区分
    # "残留几乎必然存在"（已开始写入）与"可能根本没创建"（put 开头就失败），
    # 配合 stat 确认决定是否告警（详见 except 分支注释）
    part_touched = [False]
    last = [0]  # paramiko put 回调的 transferred 是本次调用内的累计值（增量记账）

    def _cb(transferred, total):
        part_touched[0] = True
        if progress is not None:
            progress[0] += transferred - last[0]
        last[0] = transferred
        sftp._pyaissh_last_activity = time.time()

    # 续传决策（--resume）：远端 .part 大小决定走全量 / 续传 / 覆盖重传
    resume_from = 0
    if resume:
        local_size = os.path.getsize(local)
        try:
            _sftp_touch_activity(sftp)
            part_size = sftp.stat(part).st_size
        except (socket.timeout, TimeoutError):
            raise
        except IOError:
            part_size = 0  # 不存在：全量传
        if part_size > 0 and part_size < local_size:
            resume_from = part_size
            log("[RESUME] 远端续传点 %s 已有 %s，从断点继续"
                % (part, format_size(part_size)))
        elif part_size >= local_size:
            # 续传点大小异常（>= 源大小）：绝不续一个坏尾巴，覆盖重传
            try:
                sftp.remove(part)
            except (socket.timeout, TimeoutError):
                raise
            except Exception:
                pass
            log("[WARN] 续传点大小异常（>= 源大小），覆盖重传: %s" % part)

    try:
        if resume_from:
            # 手动续传：本地从 offset 读，远端 part 以 r+ 定位写（paramiko put 不支持 offset）
            lf = open(local, "rb")
            lf.seek(resume_from)
            rf = sftp.open(part, "r+b")
            rf.seek(resume_from)
            got = resume_from
            try:
                while got < local_size:
                    if _SIGTERM_RECEIVED:
                        raise KeyboardInterrupt("SIGTERM")
                    data = lf.read(RECV_CHUNK)
                    if not data:
                        raise IOError("本地文件在偏移 %d 处提前 EOF" % got)
                    rf.write(data)
                    got += len(data)
                    part_touched[0] = True
                    if progress is not None:
                        progress[0] += len(data)
                    _sftp_touch_activity(sftp)
            finally:
                lf.close()
                try:
                    rf.close()
                except Exception:
                    pass
            if got != local_size:
                raise IOError("上传续传不完整：得到 %d/%d 字节" % (got, local_size))
            log("[FILE] 续传完成 %s (%s)" % (remote, format_size(local_size)))
        else:
            sftp.put(local, part, callback=_cb)
        _sftp_atomic_rename(sftp, part, remote)
    except BaseException as e:
        if getattr(e, "keep_part", False):
            raise  # 回退双丢防护：.part 是新数据唯一副本，保留不清理（已告警）
        if resume:
            # 续传模式：保留远端 .part 作为续传点（不清理、不告警残留），
            # 提示下次重试加 --resume 从断点继续
            log("[TIP] 已保留远端续传点 %s（下次重试加 --resume 从断点继续）" % part)
            _PUT_RESIDUE_WARNINGS.append(
                "上传中断，远端续传点已保留: %s（下次重试加 --resume 从断点继续）" % part)
            raise
        # 连接坏掉时清不掉远端 .part：重试一次，仍失败必须 WARN 并记录进
        # 结果 warnings（静默残留会让远端磁盘按次泄漏且 AI 无从得知）。
        # 判定 .part 是否真的还在：
        #  - put 已开始写入（part_touched）：残留几乎必然存在，无条件告警；
        #  - put 开头就失败（未 touched，如权限不足 open 失败）：stat 确认，
        #    只有明确 SFTP_NO_SUCH_FILE 才断定无残留；
        #  - stat 因连接死/权限也失败：无法确认，按"可能残留"告警（宁多勿漏
        #    ——连接死恰恰是残留概率最高的场景，此前误判成"已不在"静默泄漏）
        removed = False
        for _attempt in range(2):
            try:
                sftp.remove(part)
                removed = True
                break
            except Exception:
                time.sleep(PUT_RETRY_SLEEP)
        if not removed and not part_touched[0]:
            # 未开始写入：用 stat 确认；只有明确"文件不存在"才不告警
            try:
                sftp.stat(part)
            except IOError as stat_err:
                if getattr(stat_err, "errno", None) == getattr(paramiko, "SFTP_NO_SUCH_FILE", 2):
                    removed = True  # 明确不存在，已清理干净
            except Exception:
                pass
        if not removed:
            msg = ("远端临时文件可能残留: %s（连接中断无法清理。清理命令："
                   "rm -f '%s'；批量清理 pyaissh 中断残留可用 "
                   "find <目标目录> -name '*.part.*' -delete。重试上传前应先清掉，"
                   "否则 .part 会按次累积）" % (part, part))
            log("[WARN] " + msg)
            _PUT_RESIDUE_WARNINGS.append(msg)
        raise


def _sftp_get_resume(sftp, remote, part, total_size):
    """下载断点续传（--resume 串行路径）：本地 .part 已有 N 字节 → 从 N 续传剩余。

    规则：part 不存在 → 全量写；已有 < 远端大小 → seek 续传；已有 >= 远端大小
    → 视为损坏/过时，清空覆盖重传（绝不续一个坏尾巴）。完成时 .part 大小必须
    等于 total_size（大小校验）。返回本次实际传输字节数（增量）。"""
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    if existing > total_size:
        log("[WARN] 本地续传点 %s 大小异常（%d > 远端 %d），覆盖重传" % (part, existing, total_size))
        existing = 0
    if existing:
        log("[RESUME] 本地续传点 %s 已有 %s，从断点继续" % (part, format_size(existing)))
    rf = sftp.open(remote, "rb")
    rf.seek(existing)
    lf = open(part, "r+b" if existing else "wb")
    lf.seek(existing)
    got = existing
    try:
        while got < total_size:
            if _SIGTERM_RECEIVED:
                raise KeyboardInterrupt("SIGTERM")
            data = rf.read(min(RECV_CHUNK, total_size - got))
            if not data:
                raise IOError("远端在偏移 %d 处提前 EOF（文件传输中被修改？）" % got)
            lf.write(data)
            got += len(data)
            _sftp_touch_activity(sftp)
    finally:
        try:
            rf.close()
        except Exception:
            pass
        try:
            lf.close()
        except Exception:
            pass
    if got != total_size:
        raise IOError("下载续传不完整：得到 %d/%d 字节" % (got, total_size))
    return got - existing


def _transfer_extra(conn, **kw):
    """传输类错误/中断的统一 extra：恒含 host/user/port（与连接期错误一致，
    AI 用 (host,port) 做多主机记账时失败样本不丢键），再叠加传输上下文。"""
    extra = _conn_extra(conn)
    extra.update(kw)
    return extra


def _win_safe_rel_path(rel, used, warnings):
    """Windows 下载的相对路径安全化（目录与文件通用）。

    逐段：拒绝保留设备名/归一化越界（.../.. 尾随点空格）反斜杠；
    清洗非法字符与控制字符（0x00-0x1f、0x7f）；并按大小写不敏感检测
    （NTFS 特性 + 清洗后）的本地名冲突，后者改名 <名>.dupN 保住两份数据。
    返回安全 rel；条目被整体排斥（危险段）时返回 None（调用方跳过，
    警告已记入 warnings）。used 为已占用本地名（小写）集合，含目录条目。
    """
    parts = rel.split("/")
    safe_parts = []
    for p in parts:
        # 防 Windows 路径归一化穿越：... / .. / 尾随点/空格变体
        # 会被 Win32 解析成 ..（如 "...", ".. ", ". "），可写出 local 目录；
        # 'a..' 尾随点会被 Win32 归一化为 'a' 覆盖本地同名文件，同样拒绝；
        # 反斜杠是 Win32 路径分隔符，含 \ 的远端名（Linux 合法）直接拒绝（H1）；
        # 保留设备名（CON/NUL/COM1 等含扩展名变体）无法创建会中止整个下载
        base = p.split(".")[0].upper()
        if (p.rstrip(". ") != p or p.strip() in ("", ".", "..")
                or "\\" in p or base in _WIN_RESERVED_NAMES):
            warnings.append(_sanitize_log_text(
                "跳过危险路径段 %r（Windows 保留设备名或归一化越界）" % p))
            return None
        # 控制字符（0x00-0x1f、0x7f）在 Windows 文件名里非法：
        # open() 会抛 Errno 22 中止整个下载——同样替换为 _
        safe_parts.append(_RE_WIN_ILLEGAL.sub("_", p))
    safe_rel = "/".join(safe_parts)
    if safe_rel != rel:
        warnings.append(_sanitize_log_text(
            "文件名含 Windows 非法字符，已替换: %s -> %s" % (rel, safe_rel)))
    # 大小写不敏感冲突：两个远端名（仅大小写不同，或清洗后同名）落到同一
    # 本地名会静默覆盖（实测 ok=true 丢一份数据）——后者改 <名>.dupN 保两份
    key = safe_rel.lower()
    if key in used:
        base, dot, ext = safe_rel.rpartition(".")
        n = 2
        if dot:
            cand = "%s.dup%d.%s" % (base, n, ext)
            while cand.lower() in used:
                n += 1
                cand = "%s.dup%d.%s" % (base, n, ext)
        else:
            cand = "%s.dup%d" % (safe_rel, n)
            while cand.lower() in used:
                n += 1
                cand = "%s.dup%d" % (safe_rel, n)
        warnings.append(_sanitize_log_text(
            "本地路径冲突（Windows 大小写不敏感或清洗后同名）:%s 改名为 %s 保住两份数据"
            % (safe_rel, cand)))
        safe_rel = cand
    used.add(safe_rel.lower())
    return safe_rel


