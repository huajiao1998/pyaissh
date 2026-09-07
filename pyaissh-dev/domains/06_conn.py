"""连接层（域 06）：目标解析 -> 认证 -> 建连。

- parse_target（[user@]host[:port]/IPv6/别名 @名称）、_alias_env
- resolve_conn / resolve_jump（凭据优先级：参数 > env > 默认私钥；跳板回落）
- _do_connect（paramiko 惰性 import + host key AutoAddPolicy 缓存）、connect / close_all
被 cmd_* 的连接段（编排 cmd_* 调 resolve_conn/connect）调用。
"""

def parse_target(target):
    """解析 [user@]host[:port] -> (user, host, port)，未指定部分返回 None。
    支持 IPv6：user@[2001:db8::1]:22、[2001:db8::1]、裸 2001:db8::1（无端口）。"""
    user = None
    port = None
    if "@" in target:
        user, _, target = target.partition("@")
        user = user or None
    if target.startswith("["):
        # 带括号的 IPv6：[addr] 或 [addr]:port
        end = target.find("]")
        if end == -1:
            raise SshError("目标格式错误（缺少 ]）: %s" % target, "bad_args")
        else:
            host = target[1:end]
            rest = target[end + 1:]
            if rest and not rest.startswith(":"):
                raise SshError("目标格式错误（] 后只能跟 :port）: %s" % rest, "bad_args")
            if rest.startswith(":"):
                try:
                    port = int(rest[1:])
                except ValueError:
                    raise SshError("目标端口非数字: %s" % rest[1:], "bad_args")
                if not 1 <= port <= MAX_PORT:
                    raise SshError("目标端口超出范围 (1-%d): %s" % (MAX_PORT, rest[1:]), "bad_args")
    elif target.count(":") == 1:
        # 普通 host:port
        host, _, port_s = target.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise SshError("目标端口非数字: %s" % port_s, "bad_args")
        if not 1 <= port <= MAX_PORT:
            raise SshError("目标端口超出范围 (1-%d): %s" % (MAX_PORT, port_s), "bad_args")
    elif target.count(":") > 1:
        # 裸 IPv6（多个冒号）：校验每段是合法 hex（1-4 位），
        # 防 host:22:33 这类 host:port:port 拼错被静默当主机名连错机器。
        # 末段允许带 zone id（fe80::1%eth0，链路本地地址）——括号形式
        # [fe80::1%eth0] 本来就放行，裸形式不能不一致地拒绝
        segs = target.split(":")
        for i, seg in enumerate(segs):
            if not seg:
                continue
            s = seg
            if i == len(segs) - 1 and "%" in s:
                # zone id：接口名（eth0、enp0s3、%25 编码等）
                addr_part, _, zone = s.partition("%")
                if not zone or not _RE_IPV6_ZONE.fullmatch(zone):
                    raise SshError("目标格式错误（IPv6 zone id 非法）: %s" % target, "bad_args")
                s = addr_part
            if i == len(segs) - 1 and _RE_IPV4.fullmatch(s):
                # IPv4-mapped 尾段（::ffff:1.2.3.4）：dotted-quad 是合法 IPv6
                # 字面量的一部分，此前被当非法 hex 段拒绝；逐段校验 0-255
                if any(int(o) > 255 for o in s.split(".")):
                    raise SshError("目标格式错误（IPv4 尾段越界）: %s" % target, "bad_args")
                continue
            if s and not _RE_IPV6_SEG.fullmatch(s):
                raise SshError("目标格式错误（多冒号但非合法 IPv6 地址）: %s" % target, "bad_args")
        host = target
    else:
        # 无端口主机名
        host = target
    return user, host, port


def _alias_env(alias, suffix=""):
    """查主机别名环境变量 PYAISSH_HOST_<别名><suffix>（键名整体大小写不敏感）。

    先按原样/全大写两种键直查，再对整个键做大小写归一扫描：Linux 的
    os.environ 严格区分大小写（.env 写全小写 pssh_host_prod 也要能命中），
    Windows 本身不区分。返回 None 表示未配置。
    """
    for a in dict.fromkeys([alias, alias.upper()]):
        v = os.environ.get("PYAISSH_HOST_%s%s" % (a, suffix))
        if v:
            return v
    want = ("PYAISSH_HOST_%s%s" % (alias, suffix)).upper()
    for k, v in os.environ.items():
        if k.upper() == want:
            return v
    return None


def resolve_conn(args):
    """合并 target / env / 默认值，返回连接参数 dict"""
    if not args.target:
        raise SshError("未指定目标主机（target）", "bad_args")
    # 主机别名：target 写 @名称，从 PYAISSH_HOST_<名称> 展开（.env 可配）
    alias = None
    target = args.target
    if target.startswith("@"):
        alias = target[1:].strip()
        if not alias:
            raise SshError("主机别名格式应为 @名称", "bad_args")
        val = _alias_env(alias)
        if not val:
            msg = "未配置主机别名 @%s：请在 .env 写 PYAISSH_HOST_%s=user@host:port" % (alias, alias.upper())
            if _CWD_ENV_SKIPPED:
                # 只看 stdout 的 AI 需要"为什么我写了 .env 还是找不到"的答案：
                # 工作目录 .env 默认不加载（供应链防护），真实原因此前只在 stderr
                msg += ("（检测到工作目录 .env 但默认不加载——防恶意仓库注入；如需启用设 "
                        "PYAISSH_ALLOW_CWD_ENV=1，或把 .env 放到 pyaissh 脚本目录）")
            raise SshError(msg, "bad_args")
        log("[ALIAS] @%s -> %s" % (alias, val))
        target = val
    t_user, t_host, t_port = parse_target(target)

    user = t_user or args.user or os.environ.get("PYAISSH_USER")
    host = t_host
    # 显式 -p 优先于 target/别名内嵌端口（与 ssh/scp 惯例一致：命令行显式参数最优先，
    # 用户写 -p 通常就是想纠正 target 里的端口）
    port = args.port if args.port is not None else t_port
    if port is None:
        port = _safe_int(os.environ.get("PYAISSH_PORT"), 22, "PYAISSH_PORT")
    if not port or not 1 <= port <= MAX_PORT:
        # 命令行 --port 已由 argparse 校验（1-65535）；这里只管 env 与 target
        # 内嵌端口：0/负值/越界/非数字一律回退默认 22 并打 WARN
        log("[WARN] 端口 %r 超出范围 (1-%d)，回退默认 22" % (port, MAX_PORT))
        port = 22
    # 凭据优先级：显式参数 > 别名专属（PYAISSH_HOST_<名称>_KEY/_PASSWORD）> 全局 env。
    # 别名配置了任一专属凭据时抑制全局 env：否则"别名只配密码 + 全局 PYAISSH_KEY"
    # 会优先拿全局 key 去认证，key 不匹配时直接 auth_failed，别名密码永远轮不到；
    # 别名主机的凭据应完全由别名决定（显式命令行参数仍最高优先）
    alias_key = _alias_env(alias, "_KEY") if alias else None
    alias_pw = _alias_env(alias, "_PASSWORD") if alias else None
    if alias and (alias_key or alias_pw):
        key = args.key or alias_key
        password = args.password or alias_pw
    else:
        key = args.key or alias_key or os.environ.get("PYAISSH_KEY")  # None 表示用默认密钥
        password = args.password or alias_pw or os.environ.get("PYAISSH_PASSWORD")

    if not user:
        raise SshError("未指定用户名：请在 target 写 user@host 或用 -u / PYAISSH_USER", "bad_args")
    if not host:
        raise SshError("未指定主机", "bad_args")

    return {
        "host": host, "user": user, "port": port,
        "key": key, "password": password,
        "timeout": args.timeout, "strict": args.strict,
    }


def resolve_jump(args, target_user=None):
    """解析跳板机连接参数，返回 conn dict 或 None（无跳板时）。

    target_user：已解析的目标用户（别名 @name 展开后），跳板缺 user 时回退用它。
    """
    if not args.jump:
        if (args.jump_password or args.jump_key
                or os.environ.get("PYAISSH_JUMP_PASSWORD") or os.environ.get("PYAISSH_JUMP_KEY")):
            log("[WARN] 指定了跳板凭据（--jump-password/--jump-key/PYAISSH_JUMP_*）但未提供 --jump，已忽略")
        return None
    j_user = None
    j_alias = None
    jump_target = args.jump
    if jump_target.startswith("@"):
        # 跳板机也支持 @别名（与 target 同一套 PYAISSH_HOST_* 配置）。
        # 不展开的话 "@bastion" 会被当字面主机名去连，报 DNS 失败极具误导性
        j_alias = jump_target[1:].strip()
        if not j_alias:
            raise SshError("跳板机别名格式应为 @名称", "bad_args")
        val = _alias_env(j_alias)
        if not val:
            raise SshError("未配置跳板机别名 @%s：请在 .env 写 PYAISSH_HOST_%s=user@host:port"
                           % (j_alias, j_alias.upper()), "bad_args")
        log("[ALIAS] 跳板 @%s -> %s" % (j_alias, val))
        jump_target = val
    j_user, j_host, j_port = parse_target(jump_target)
    # 跳板机用户：优先 --jump 字串里的，其次复用目标用户
    # （target 可能是别名 @name，原始字符串解析不出 user，用已解析的 target_user 回退）
    if not j_user:
        j_user = target_user or args.user or os.environ.get("PYAISSH_USER")
    if not j_user:
        raise SshError("跳板机未指定用户名：请在 --jump 写 user@host", "bad_args")
    if not j_host:
        raise SshError("跳板机未指定主机", "bad_args")
    j_port = j_port or 22  # parse_target 已保证 1-65535（越界直接 bad_args），这里只补未指定端口
    # 跳板凭据优先级：显式参数 > 别名专属 > PYAISSH_JUMP_*（别名复用 PYAISSH_HOST_* 的
    # 专属凭据键：同一台机器当 target 和当跳板通常用同一套凭据）。
    # 与 resolve_conn 同规则：别名配了专属凭据时抑制全局 env
    alias_key = _alias_env(j_alias, "_KEY") if j_alias else None
    alias_pw = _alias_env(j_alias, "_PASSWORD") if j_alias else None
    if j_alias and (alias_key or alias_pw):
        key = args.jump_key or alias_key
        password = args.jump_password or alias_pw
    else:
        key = args.jump_key or alias_key or os.environ.get("PYAISSH_JUMP_KEY")  # None = 默认密钥
        # 密码回落链（v1.4.9）：--jump-password > 别名专属 > PYAISSH_JUMP_PASSWORD > PYAISSH_PASSWORD。
        # 跳板与目标共用一套密码是常见场景（同主多机），且跳板【用户名】本就回落目标用户
        # （上方 target_user 回退链）——唯独凭据不回落是设计不一致，实测会多花一次往返才从
        # 错误提示里拿到"请用 --jump-password/PYAISSH_JUMP_PASSWORD"。只回落密码、不回落密钥：
        # 错误的 PYAISSH_KEY 会短路原本可用的默认密钥路径（真回归），而密码错误与缺密码的
        # 失败形态等价、无回归。回退发生时打 stderr WARN 保持行为可见。
        password = args.jump_password or alias_pw or os.environ.get("PYAISSH_JUMP_PASSWORD")
        if not password and os.environ.get("PYAISSH_PASSWORD"):
            password = os.environ["PYAISSH_PASSWORD"]
            log("[JUMP] 跳板未指定专属凭据，密码回退使用 PYAISSH_PASSWORD"
                "（需要独立跳板密码时请设 PYAISSH_JUMP_PASSWORD 或 --jump-password）")
    return {
        "host": j_host, "user": j_user,
        "port": j_port,
        "key": key,
        "password": password,
        "timeout": args.timeout, "strict": args.strict,
    }


def connect(conn, jump_conn=None):
    """建立 SSH 连接，返回 paramiko.SSHClient。
    若指定 jump_conn，先连跳板机，再通过 direct-tcpip 隧道连目标。
    跳板客户端挂在 client._jump_client 上，由 close_all() 一并清理。
    任何异常（含认证失败/超时）都会先关闭跳板连接再 re-raise，避免泄漏。
    """
    if not jump_conn:
        return _do_connect(conn, None)

    # 有跳板：先连跳板，再用隧道连目标；任何失败都先关跳板再 re-raise
    try:
        jump_client = _do_connect(jump_conn, None, is_jump=True)
    except SshError as e:
        # 加 [跳板机] 前缀：AI 需要区分是跳板机还是目标机的凭据问题
        raise SshError("[跳板机 %s@%s] %s" % (jump_conn["user"], jump_conn["host"], e), e.error_type)
    try:
        return _do_connect(conn, jump_client)
    except BaseException:
        try:
            jump_client.close()
        except Exception:
            pass
        raise


def _host_key_known(client, host, port):
    """判断已知主机：paramiko 5.0 的 host key 条目按 "[host]:port" 键存储
    （5.0 起按 host:port 区分，AutoAddPolicy 添加时用带端口格式），但
    known_hosts 文件里的旧条目可能是裸 hostname——两种格式都查。"""
    hk = client.get_host_keys()
    try:
        if hk.lookup("[%s]:%d" % (host, port)) is not None:
            return True
    except Exception:
        pass
    try:
        return hk.lookup(host) is not None
    except Exception:
        return False


_ATOMIC_POLICY = None  # _AtomicAutoAddPolicy 单例缓存（惰性创建，见 _atomic_auto_add_policy）


def _atomic_auto_add_policy():
    """惰性创建 AutoAddPolicy+原子写盘策略实例（v1.4.8 设计，v1.5.5 惰性化）。

    类继承 paramiko.AutoAddPolicy，模块级定义会强制 import paramiko——改为
    首次调用时 import + 定义 + 缓存实例。paramiko 只在真正建连时才加载，
    错误路径（--version/bad_args 等）不付 ~190ms import 开销。"""
    global _ATOMIC_POLICY
    if _ATOMIC_POLICY is None:
        global paramiko  # 统一绑定全局：类方法引用 paramiko.HostKeys 等
        import paramiko

        class _AtomicAutoAddPolicy(paramiko.AutoAddPolicy):
            """AutoAddPolicy + 原子写盘。

            paramiko 5.0 的 AutoAddPolicy 在 known_hosts 文件存在时会把新 host key
            写回盘，但 save_host_keys 是直接 open(filename, "w") 覆写：
              1) 多进程并发首次连接同一新主机 → read-merge-write 竞态，互相覆盖
                 丢记录；
              2) 写盘中途进程崩溃 → known_hosts 文件本身被截断/半写损坏。
            本策略保持"隐式接受"语义，但写盘改为：
              - Linux：fcntl.flock 对 <known_hosts>.lock 加互斥锁（锁文件固定路径、
                永不被 replace 换 inode），锁内重新加载磁盘最新内容、合并内存键、
                写临时文件、os.replace 原子替换——并发进程串行化合并，既不丢记录
                也不损坏文件；
              - Windows：无 fcntl，仅原子替换（文件不会损坏；极端并发下仍可能
                丢记录，属 paramiko 5.0 语义上限）。
            写盘失败只打 WARN 不阻断连接（与 AutoAddPolicy 一致：内存已接受）。
            """

            def missing_host_key(self, client, hostname, key):
                client._host_keys.add(hostname, key.get_name(), key)
                fn = getattr(client, "_host_keys_filename", None)
                if fn is None:
                    return
                try:
                    self._atomic_save(client._host_keys, fn)
                except Exception as e:
                    log("[WARN] 写 known_hosts 失败（host key 已在内存接受）: %s" % e)

            @staticmethod
            def _atomic_save(host_keys, fn):
                import tempfile
                lock_fd = None
                if os.name == "posix":
                    try:
                        import fcntl
                        lock_fd = open(fn + ".lock", "a+")
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    except Exception:
                        lock_fd = None
                try:
                    merged = paramiko.HostKeys()
                    try:
                        merged.load(fn)  # 锁内重读磁盘最新内容，合并避免覆盖他人更新
                    except Exception:
                        pass
                    for hostname, keys in host_keys.items():
                        for keytype, key in keys.items():
                            merged.add(hostname, keytype, key)
                    fd, tmp = tempfile.mkstemp(
                        dir=os.path.dirname(fn) or ".", prefix=".known_hosts.", suffix=".tmp")
                    try:
                        with os.fdopen(fd, "w") as f:
                            for hostname, keys in merged.items():
                                for keytype, key in keys.items():
                                    f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))
                        os.replace(tmp, fn)
                    except BaseException:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
                finally:
                    if lock_fd is not None:
                        try:
                            import fcntl
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                            lock_fd.close()
                        except Exception:
                            pass

        _ATOMIC_POLICY = _AtomicAutoAddPolicy()
    return _ATOMIC_POLICY


def _do_connect(conn, jump_client, is_jump=False):
    """实际建立 SSH 连接。jump_client 非 None 时通过其 direct-tcpip 隧道。

    is_jump=True 表示本次连接的是跳板机本身（认证提示要说对变量名）。
    """
    global paramiko  # 惰性 import 绑定到模块全局（cmd_download/cmd_ls/_sftp_put_atomic
    try:             # 等模块级函数引用 paramiko.X；函数内裸 import 是局部名会 NameError）
        import paramiko
    except ImportError as e:
        # 依赖缺失独立分类：误导 AI 查网络（此前实测归 connection_failed 且
        # retryable:true，AI 会白白重试）。dependency_missing 不在
        # _RETRYABLE_ERRORS → retryable=false（装依赖前重试无意义）
        raise SshError(
            "依赖缺失: %s（pip install paramiko 可安装；pyaissh 的 SSH 底层库未就绪，"
            "与目标主机/网络无关，重试前请先安装依赖）" % e, "dependency_missing")
    if _SIGTERM_RECEIVED:
        # 连接尚未开始即拿到信号（transport 未注册、响应线程无从 close）：
        # 在我们自己的帧里抛 KI 是安全的，走正常中断路径 130
        raise KeyboardInterrupt("SIGTERM")
    sock = None
    if jump_client:
        # 通过跳板机开 direct-tcpip 隧道到目标
        try:
            transport = jump_client.get_transport()
            sock = transport.open_channel(
                "direct-tcpip",
                (conn["host"], conn["port"]),
                ("127.0.0.1", 0),
                timeout=conn["timeout"],  # 网络黑洞时避免无限阻塞
            )
        except Exception as e:
            msg = str(e)
            # 给 AI 可执行的排查提示：常见两类失败原因
            if "administratively prohibited" in msg.lower():
                hint = "（跳板机 sshd 禁止转发该目标，常见原因：AllowTcpForwarding=no 或 PermitOpen 限制）"
            elif "refused" in msg.lower() or "connect failed" in msg.lower():
                hint = "（目标端口拒绝连接，可能未开放、NAT 端口不符或目标服务未启动）"
            else:
                hint = ""
            raise SshError("跳板机隧道失败: %s%s" % (msg, hint), "jump_failed")
        if sock is None:
            raise SshError("跳板机无法打开到 %s:%s 的隧道" % (conn["host"], conn["port"]), "jump_failed")
        log("[JUMP] 隧道已建立 -> %s:%s" % (conn["host"], conn["port"]))

    client = paramiko.SSHClient()
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(known_hosts):
        try:
            client.load_host_keys(known_hosts)
        except Exception:
            pass
    # 说明：_AtomicAutoAddPolicy 接受新 host key 并原子写盘（paramiko >= 5.0
    # 在 known_hosts 文件存在时会写回盘；< 5.0 只加内存不写盘）。写盘用
    # flock + 临时文件 + os.replace（v1.4.8）：多进程并发首次连接同一新主机
    # 不再丢记录、写盘中断不损坏文件（原生 save_host_keys 是直接覆写）。
    # 首次连接后 host key 已持久化，后续连接不再提示；敏感环境请用 --strict。

    if conn["strict"]:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        host_key_pre_known = None  # strict 模式不走 TOFU，无需新主机提示
    else:
        # 记录连接前该主机是否已在 known_hosts：连接成功后若为新主机
        # （AutoAddPolicy 隐式接受），打 WARN——AI 据此区分
        # "首次连接的新主机"与"被劫持/重装后的主机"（TOFU 的固有风险，
        # 至少让它可见；敏感环境请用 --strict）
        try:
            host_key_pre_known = _host_key_known(client, conn["host"], conn["port"])
        except Exception:
            host_key_pre_known = None  # 取不到就不提示，不阻断连接
        # 原子写盘版 AutoAddPolicy（v1.4.8）：并发首连不丢记录、写盘不损坏
        # 文件（flock + 临时文件 + os.replace），见 _AtomicAutoAddPolicy 注释
        client.set_missing_host_key_policy(_atomic_auto_add_policy())

    key_explicit = conn["key"]  # 显式指定的私钥（None 表示未指定，回退默认）
    password = conn["password"]
    default_key = os.path.expanduser("~/.ssh/id_ed25519")

    kwargs = dict(
        hostname=conn["host"], port=conn["port"],
        username=conn["user"], timeout=conn["timeout"],
        allow_agent=False, look_for_keys=False,  # 默认禁 agent/密钥扫描，保证显式凭据优先级
    )
    if sock:
        kwargs["sock"] = sock
    # 认证优先级：显式 --key > 显式 --password > 默认密钥 > ssh-agent 兜底
    if key_explicit:
        key_path = os.path.expanduser(key_explicit)
        if not os.path.isfile(key_path):
            # 防误把私钥内容当路径传入：超长/含换行/含 BEGIN 时脱敏提示
            if len(key_explicit) > 200 or "\n" in key_explicit or "-----BEGIN" in key_explicit:
                raise SshError("--key 疑似传入了私钥内容而非路径：请传私钥文件路径", "auth_failed")
            raise SshError("私钥文件不存在: %s" % key_explicit, "auth_failed")
        kwargs["key_filename"] = key_path
        auth = "key=%s" % key_explicit
        if password:
            # paramiko 支持 key+password 同传（password 兼作 passphrase），但当前
            # 设计 key 优先且不传 password——静默丢弃显式 --password 会误导排障
            log("[WARN] 同时提供了密码与密钥，本次仅用密钥认证（密码被忽略）")
    elif password:
        kwargs["password"] = password
        auth = "password"
    elif os.path.isfile(default_key):
        kwargs["key_filename"] = default_key
        auth = "key=~/.ssh/id_ed25519 (默认)"
    else:
        # 无显式/默认凭据：回退 ssh-agent（仅 agent 场景，如 ssh-add 过密钥）
        kwargs["allow_agent"] = True
        auth = "ssh-agent"

    log("[SSH] 连接 %s@%s:%s (%s) ..." % (conn["user"], conn["host"], conn["port"], auth))
    t0 = time.time()
    try:
        # allow_agent/look_for_keys=False：禁掉 paramiko 默认的 ssh-agent 和
        # ~/.ssh 全密钥扫描，否则显式 --key/--password 的优先级会被 agent 中
        # 或 ~/.ssh/id_rsa 等默认密钥抢先，连上错误身份（与文档声明矛盾）。
        # 显式/默认的 key_filename 已覆盖密钥认证路径。
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException as e:
            if is_jump:
                hint = ("（检查跳板机用户名/密码；密码建议用 PYAISSH_JUMP_PASSWORD 环境变量"
                        "或 --jump-password，密钥用 --jump-key / PYAISSH_JUMP_KEY）")
            else:
                hint = ("（检查用户名/密码是否正确；密码建议用 PYAISSH_PASSWORD "
                        "环境变量，密钥用 --key / PYAISSH_KEY）")
            raise SshError("认证失败: %s%s" % (e, hint), "auth_failed")
        except paramiko.SSHException as e:
            msg = str(e)
            # 无凭据场景（--key/--password/PYAISSH_PASSWORD 都没给且无默认密钥/agent）：
            # paramiko 报的原文很含糊，明确告诉 AI 缺什么
            if "no authentication methods available" in msg.lower():
                raise SshError("未提供可用凭据: %s（请用 --password / PYAISSH_PASSWORD 环境变量，"
                               "或 --key / PYAISSH_KEY 指定私钥）" % msg, "auth_failed")
            # host key 失败分两类（paramiko 5.0 实测消息）：
            #  1) known_hosts 已有记录但指纹不匹配 -> BadHostKeyException
            #  2) --strict + 新主机不在 known_hosts -> RejectPolicy 抛
            #     SSHException("Server 'x' not found in known_hosts")
            # 之前用 "key" in msg 子串判定会漏判 2（消息不含 key）且误报
            # key-exchange 类错误，改用异常类型 + 消息特征双重判定
            if isinstance(e, paramiko.BadHostKeyException) \
                    or "not found in known_hosts" in msg.lower() \
                    or "host key" in msg.lower():
                raise SshError("host key 校验失败: %s（确认目标无误后清理 "
                               "~/.ssh/known_hosts 再试）" % msg, "host_key_rejected")
            # banner 失败两类形态（paramiko 5.0 实测）：
            #  1) 消息直接含 "banner"（"Error reading SSH protocol banner"）
            #  2) banner 读取失败后认证阶段在非活动 transport 上抛
            #     "No existing session"（原始 banner 异常只打到 stderr）——
            #     之前只匹配 1，导致 TCP 可达但无 SSH 服务的场景（黑洞 IP、
            #     错误端口连到别的服务）报晦涩的 ssh_error "No existing session"
            if "banner" in msg.lower() or "no existing session" in msg.lower():
                # 并发连接过多时 sshd MaxStartups（默认 10:30:100）概率性拒绝
                # 也报 banner 错误：保留原排查提示（仍是 ssh_error）
                if "too many" in msg.lower() or "maxstartups" in msg.lower() \
                        or "administratively" in msg.lower():
                    raise SshError("%s（并发连接过多可能触发服务器 MaxStartups 限制："
                                   "稍后重试、降低并发，或调大目标机 sshd 的 MaxStartups）" % msg,
                                   "ssh_error")
                if "timed out" in msg.lower() or "timeout" in msg.lower() \
                        or time.time() - t0 >= conn["timeout"]:
                    # banner 超时：TCP 已连上但服务器在超时窗口内没发 banner——
                    # 典型是慢速/过载服务器或链路丢包，不是"没有 SSH 服务"。
                    # 归 connection_timeout（排障动作：查网络/调大 --timeout 重试），
                    # 此前归 connection_refused 会误导 AI 去检查端口/sshd。
                    # 注意：paramiko 5.0 的 banner 超时异常消息是 "No existing
                    # session"（不含 timeout 字样），消息匹配判不出，故叠加
                    # 耗时判定（t0 在 connect 前：用满 --timeout 窗口仍未拿到
                    # banner 即超时；快速失败则是"目标不是 SSH 服务"）
                    raise SshError(
                        "SSH banner 超时（TCP 已连接但服务器在 %s 秒内未发送 "
                        "banner，可能过载或网络丢包）: %s（可调大 --timeout 重试）"
                        % (conn["timeout"], msg), "connection_timeout")
                # 其余 banner 失败：TCP 可达但目标不是 SSH 服务/协议不符——
                # 归为 connection_refused 并给出可执行排查方向（而非晦涩消息）
                raise SshError("目标端口没有 SSH 服务（TCP 可达但未收到 SSH banner）: %s"
                               "（检查端口是否写对、目标是否运行 sshd）" % msg,
                               "connection_refused")
            raise SshError(msg, "ssh_error")
        except Exception as e:
            # 网络类错误分类 + 排查提示（AI 依据 error_type 和 hint 决定下一步）
            name = type(e).__name__
            msg = str(e)
            if isinstance(e, socket.gaierror):
                raise SshError("DNS 解析失败: %s（主机名可能拼错）" % msg, "dns_failed")
            if "timeout" in name.lower() or "timed out" in msg.lower():
                raise SshError("连接超时 (%ss): %s（检查主机/端口是否可达、防火墙/NAT 映射）"
                               % (conn["timeout"], msg), "connection_timeout")
            if isinstance(e, ConnectionRefusedError) or "refused" in msg.lower() \
                    or "unable to connect" in msg.lower() \
                    or getattr(e, "errno", None) == errno.ECONNREFUSED:
                raise SshError("连接被拒绝: %s（目标端口没有 SSH 服务在监听，"
                               "检查端口 / NAT 映射是否写对）" % msg, "connection_refused")
            raise SshError("连接失败: %s" % msg, "connection_failed")
    except BaseException:
        # connect 失败时 paramiko 不会自动关掉已建立的 transport/socket，
        # 必须显式 close，否则连接悬挂到进程退出（泄漏）。
        # 用 BaseException：Ctrl+C 中断连接时也要先关掉半开连接
        try:
            client.close()
        except Exception:
            pass
        raise

    dur = int((time.time() - t0) * 1000)
    log("[OK]  已连接 (%dms)" % dur)
    if not conn["strict"] and host_key_pre_known is False:
        # 连接前 known_hosts 无此主机、连接后出现 host key（_host_key_known
        # 兼容 paramiko 5.0 的 "[host]:port" 键与旧版裸 hostname）：
        # 新主机被隐式接受（AutoAddPolicy 可能已写盘，见上方注释）
        try:
            if _host_key_known(client, conn["host"], conn["port"]):
                log("[WARN] 新主机 host key 已隐式接受（AutoAddPolicy）："
                    "%s:%s——首次连接或主机 key 已变更；敏感环境请用 --strict"
                    % (conn["host"], conn["port"]))
        except Exception:
            pass
    client._jump_client = jump_client  # 挂载跳板客户端以便 close_all 清理
    try:
        _ACTIVE_TRANSPORTS.append(client.get_transport())
    except Exception:
        pass
    return client


def close_all(client):
    """关闭 SSH 连接及其跳板机连接（如有）"""
    try:
        _ACTIVE_TRANSPORTS.remove(client.get_transport())
    except Exception:
        pass
    jump = getattr(client, "_jump_client", None)
    try:
        client.close()
    except Exception:
        pass
    if jump:
        try:
            jump.close()
        except Exception:
            pass


# =========================================================================
# SFTP 辅助：远程目录操作
# =========================================================================


def _sftp_touch_activity(sftp):
    """刷新 SFTP 看门狗活动时间：任何 SFTP 操作（含 listdir/stat/mkdir/walk）
    前调用，防止高延迟链路下目录操作被看门狗误杀。"""
    sftp._pyaissh_last_activity = time.time()


def _make_sftp_touch(sftp):
    """生成 put/get 的进度回调：刷新看门狗的活动时间戳（有数据流动=活着）。

    paramiko 回调签名 func(transferred, total)，用闭包把 sftp 传进去。
    """
    def _cb(transferred, total):
        sftp._pyaissh_last_activity = time.time()
    return _cb




# =========================================================================
# host 子命令（v2.1）：host add NAME user@host[:port] —— 把主机别名写进 .env
# =========================================================================

def _env_write_value(env_path, key, value):
    """把 key=value 写入 .env（已有同名行则整行替换，否则追加）；返回是否新增。

    行内值含空格/#/引号时用双引号包裹（解析器支持引号内 # 不拆）；
    值内含双引号时拒写（密码等凭据建议 --key 认证替代）。
    """
    lines = None
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
    newline_eol = "\r\n" if lines and any(l.endswith("\r\n") for l in lines) else "\n"
    if value and any(ch in value for ch in ' "#\''):
        if '"' in value:
            return None  # 拒写信号
        value = '"%s"' % value
    line = "%s=%s%s" % (key, value, newline_eol)
    if lines is None:
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w", encoding="utf-8", newline="") as f:
            f.write("# pyaissh 主机别名配置（host add 写入；.env 明文请勿提交 git）" + newline_eol + line)
        return True
    out, replaced, done = [], False, False
    for ln in lines:
        stripped = ln.split("=", 1)
        if len(stripped) == 2 and stripped[0].strip() == key:
            if not done:
                out.append(line)
                done = True
                replaced = True
            continue  # 丢弃旧的重复行
        out.append(ln)
    if not done:
        out.append(line)
    with open(env_path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))
    return not replaced


def cmd_host_add(args):
    """pyaissh host add <name> <user@host[:port]> [--password P] [--key PATH]

    把主机别名写进脚本同目录 .env（幂等：同名别名整行更新），随后可用
    `pyaissh exec @name ...` 直接调用（凭据由别名专属环境变量提供）。
    密码存 .env 是明文（与 PYAISSH_PASSWORD 同风险），脚本目录 .env 不会被
    供应链意外加载（仅同目录自动读），但切勿提交 git/分享。
    """
    name = getattr(args, "name", "") or ""
    target = getattr(args, "host_target", "") or ""
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        emit_error(args.json, "bad_args",
                   "别名只允许字母/数字/下划线: %r" % name)
        return 2
    try:
        user, host, port = parse_target(target)
    except SshError as e:
        emit_error(args.json, "bad_args", "目标格式错误: %s" % e)
        return 2
    if not user:
        emit_error(args.json, "bad_args",
                   "别名 target 必须写 user@host[:port]（别名凭据完全由 target 决定）")
        return 2
    key = "PYAISSH_HOST_%s" % name.upper()
    canonical = "%s@%s" % (user, host)
    if port and port != 22:
        canonical += ":%d" % port
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    added = _env_write_value(env_path, key, canonical)
    pw = getattr(args, "password", None)
    key_path = getattr(args, "key", None)
    if pw is not None:
        if _env_write_value(env_path, key + "_PASSWORD", pw) is None:
            emit_error(args.json, "bad_args",
                       "密码含双引号无法安全写入 .env——建议用密钥认证（--key）替代")
            return 2
    if key_path:
        _env_write_value(env_path, key + "_KEY", key_path)
    tips = ["别名 %s -> %s（调用: pyaissh exec @%s ...）" % (name, canonical, name.lower())]
    if pw is None and not key_path:
        tips.append("未存密码/密钥：将复用全局 PYAISSH_PASSWORD 或默认私钥；"
                    "要专属凭据可重跑加 --password/--key")
    tips.append(".env 是明文（路径 %s），请勿提交 git/分享" % env_path)
    result = {"ok": True, "action": "host", "alias": "@%s" % name.lower(),
              "target": canonical, "env_path": env_path, "tips": tips}
    # v2.1.2：统一走 _emit_result——host 也支持 --field（如 --field alias 只取别名）
    _emit_result(args, result)
    return 0


def _env_remove_keys(env_path, prefix):
    """从 .env 删除所有以 prefix 开头的键行（返回删除行数）。"""
    if not os.path.isfile(env_path):
        return 0
    with open(env_path, "r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    newline_eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
    kept, removed = [], 0
    for ln in lines:
        key = ln.split("=", 1)[0].strip()
        if key.startswith(prefix):
            removed += 1
            continue
        kept.append(ln)
    if removed:
        with open(env_path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(kept))
    return removed


def _env_host_entries(env_path):
    """读 .env 的 PYAISSH_HOST_<NAME>=user@host 行 -> [(name, target), ...]
    （只读 host 行，不含密码/密钥——list 绝不回显凭据）。"""
    if not os.path.isfile(env_path):
        return []
    out = []
    with open(env_path, "r", encoding="utf-8") as f:
        for ln in f:
            line = ln.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("PYAISSH_HOST_"):
                continue
            if "_PASSWORD" in line or "_KEY" in line:
                continue
            key, _, val = line.partition("=")
            name = key[len("PYAISSH_HOST_"):].strip()
            target = val.strip().strip('"').strip("'")
            if name and target:
                out.append((name.lower(), target))
    return out


def cmd_host_remove(args):
    """pyaissh host remove <name> —— 从 .env 删除别名（含专属密码/密钥行）。"""
    name = getattr(args, "name", "") or ""
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        emit_error(args.json, "bad_args", "别名只允许字母/数字/下划线: %r" % name)
        return 2
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    prefix = "PYAISSH_HOST_%s" % name.upper()
    removed = _env_remove_keys(env_path, prefix)
    if removed == 0:
        emit_error(args.json, "bad_args",
                   "别名 %s 不存在（host list 可查看已配置别名）" % name)
        return 2
    result = {"ok": True, "action": "host", "removed": "@%s" % name.lower(),
              "env_path": env_path,
              "note": "已删除 %d 行（host 目标及其专属密码/密钥，如有）" % removed}
    _emit_result(args, result)
    return 0


def cmd_host_list(args):
    """pyaissh host list —— 列出 .env 已配置的别名（只列 host 行，不回显密码/密钥）。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    entries = [{"name": n, "target": t} for n, t in _env_host_entries(env_path)]
    result = {"ok": True, "action": "host_list", "count": len(entries),
              "entries": entries,
              "tip": "调用: pyaissh exec @<name> ...（别名专属密码/密钥不在此列出）"}
    _emit_result(args, result)
    return 0
