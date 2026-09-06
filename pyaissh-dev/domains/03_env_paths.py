"""配置与路径层（域 03）。

- .env 解析/加载（load_env：技能目录 .env 自动加载、工作目录需 PYAISSH_ALLOW_CWD_ENV=1）
- MSYS/Git Bash 路径修复：_fix_msys_local_path / _fix_msys_remote_path（含 ~ 展开逆转）
- 远端路径规范化：_normalize_remote_path（~ 展开 / 去尾斜杠 / glob 拒绝）
被 06_conn（解析）、各 cmd_*（--local/--remote/--cmd-file 路径）调用。
"""

def _safe_int(value, default, name="端口"):
    """安全把字符串/数字转成 int，失败返回 default 并不抛异常。"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        log("[WARN] %s 值 %r 非数字，用默认 %s" % (name, value, default))
        return default


# =========================================================================
# 配置加载
# =========================================================================

def _parse_env_file(env_path):
    """解析单个 .env 文件并写入 os.environ（不覆盖已存在的环境变量）。

    返回 True 表示解析成功（即使文件为空）。
    """
    try:
        # utf-8-sig：自动剥离 UTF-8 BOM（\ufeff），否则首个变量 key 带 BOM 失效
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 引号包裹的值：找配对引号，引号内 # 不拆（KEY="a # b"）；
                # 引号后只允许 # 注释（KEY="a # b" # c）。未包裹的值：
                # 行内注释仅当 # 前有空格才拆（KEY=a#b 不误拆）
                if value[:1] in ('"', "'"):
                    q = value[0]
                    end_q = value.find(q, 1)
                    if end_q != -1:
                        rest = value[end_q + 1:].strip()
                        if not rest or rest.startswith("#"):
                            value = value[1:end_q].strip()
                elif " #" in value:
                    value = value.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = value
        return True
    except Exception:
        log("[WARN] 读取 .env 失败（编码可能不是 UTF-8）: %s" % env_path)
        return False


def load_env():
    """加载 .env 文件（不覆盖已存在的环境变量）。

    供应链安全设计：**默认只加载脚本目录的 .env**（用户主动放入 pyaissh
    工具目录、自己可控的文件）。工作目录（cwd）的 .env 默认【不】加载——
    恶意仓库可自带 .env 注入 PYAISSH_HOST_* / PYAISSH_PASSWORD 等变量，把 AI
    的 SSH 连接导向攻击者主机（钓鱼 SSH）。仅当显式设置
    PYAISSH_ALLOW_CWD_ENV=1（环境变量或脚本目录 .env 中）时才加载 cwd .env，
    并打 WARN 提示供应链风险；未开启但存在 cwd .env 时也打 WARN 提醒。
    """
    script_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(script_env):
        _parse_env_file(script_env)

    cwd_env = os.path.join(os.getcwd(), ".env")
    if cwd_env == script_env or not os.path.isfile(cwd_env):
        return
    if os.environ.get("PYAISSH_ALLOW_CWD_ENV") == "1":
        log("[WARN] 正在从工作目录加载 .env（供应链风险，因 PYAISSH_ALLOW_CWD_ENV=1 已显式开启）: %s" % cwd_env)
        _parse_env_file(cwd_env)
    else:
        global _CWD_ENV_SKIPPED
        _CWD_ENV_SKIPPED = True
        log("[WARN] 检测到工作目录 .env 但未加载（供应链风险：防止恶意仓库注入 PYAISSH_HOST_* 等"
            "导向攻击者主机；如确需使用请设 PYAISSH_ALLOW_CWD_ENV=1）: %s" % cwd_env)


def _fix_msys_remote_path(path):
    """修复被 Git Bash/MSYS 路径转换破坏的远程路径。

    场景：Git Bash 在没有 MSYS_NO_PATHCONV=1 时，会把 Unix 绝对路径
    （如 /tmp/xxx、/root/xxx、/opt/app）自动转成 Windows 路径，
    传给 pyaissh.py 后导致 SFTP 拿到错误路径。
    此函数检测并逆转常见转换模式。
    """
    if not path or os.name != "nt":
        return path
    # 检测 MSYS 环境
    in_msys = bool(os.environ.get("MSYSTEM"))
    if not in_msys:
        return path
    # 检测是否已设 MSYS_NO_PATHCONV（已禁用转换）
    if os.environ.get("MSYS_NO_PATHCONV") == "1" or os.environ.get("MSYS2_ARG_CONV_EXCL") == "*":
        return path

    # 模式 1：TEMP 目录转换 /tmp/xxx -> C:/Users/<user>/AppData/Local/Temp/xxx
    temp = os.environ.get("TEMP", "").replace("\\", "/")
    if temp and path.startswith(temp):
        rest = path[len(temp):]
        recovered = "/tmp" + rest
        log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pyaissh 命令而非 python pyaissh.py)" % (path, recovered))
        return recovered

    # 模式 2：MSYS 前缀转换 /root/xxx -> C:/Program Files/Git/root/xxx
    # 尝试通过 cygpath 找到 MSYS 根目录
    try:
        msys_root = subprocess.run(["cygpath", "-w", "/"],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
        if msys_root:
            msys_root = msys_root.replace("\\", "/").rstrip("/")
            if path.startswith(msys_root + "/"):
                rest = path[len(msys_root):]
                recovered = rest  # rest is the original /xxx/yyy
                log("[WARN] MSYS 路径转换检测: %s → %s (建议用 pyaissh 命令而非 python pyaissh.py)" % (path, recovered))
                return recovered
    except Exception:
        pass

    # 模式 3：MSYS ~ 展开转换 C:/Users/<user>/xxx -> ~/xxx
    # Git Bash 把 ~ 展开为 /c/Users/<user> 再经 pathconv 变 C:/Users/<user>：
    # --remote 的 ~ 语义是"远端用户 home"，应由 _normalize_remote_path 在远端
    # 展开；此处还原成 ~/xxx，避免在远端创建 /C:/Users/<user>/ 垃圾目录树。
    # 仅当 <user> 匹配本地 Windows 用户名（MSYS home 的典型形态），
    # 远端 Windows 服务器上的 C:/Users/<其他名>/... 不受影响。
    local_user = os.environ.get("USERNAME") or os.environ.get("USER")
    if local_user:
        home_prefix = "C:/Users/%s" % local_user
        if path.startswith(home_prefix + "/"):
            rest = path[len(home_prefix):]
            recovered = "~" + rest
            log("[WARN] MSYS ~ 转换检测: %s → %s (建议给 ~ 加引号或设 MSYS_NO_PATHCONV=1)" % (path, recovered))
            return recovered

    return path


def _sftp_home(sftp):
    """取远端用户 home（SFTP 会话起始目录），缓存在会话上避免重复往返。"""
    home = getattr(sftp, "_pyaissh_home", None)
    if home is None:
        home = sftp.normalize(".")
        sftp._pyaissh_home = home
    return home


def _normalize_remote_path(sftp, path):
    """SFTP 用前规范化远端路径：去尾斜杠 + ~ 展开。

    - '~' 与 '~/' 展开为用户 home：SFTP 协议本身不展开（exec 的 shell 才展开），
      按 SFTP 字面语义处理会静默创建名为 ~ 的目录、文件落到错误位置还报成功；
      展开后的实际路径回显在结果 JSON 的 remote/path 字段，AI 能看到落点。
    - '~user' 形式无法展开：明确报错，绝不按字面路径处理。
    - 去掉尾部 /：POSIX 下 stat("file/") 返回 ENOTDIR，会被误报成"路径不存在"。
    通配符检测不在这里做：下载/列表在"确实不存在"时才提示（文件名合法含 * ? [），
    上传在入口直接拒绝（新建带 glob 字符的路径几乎必是笔误）。
    """
    p = path
    if not p or not p.strip():
        # 空串若兜成 "/" 会整盘递归（download 方向拉全盘）；空/纯空白直接报错
        raise SshError("远端路径为空（--remote/--path 不能是空字符串）", "bad_args")
    if p != "/":
        p = p.rstrip("/") or "/"
    if p == "~":
        p = _sftp_home(sftp)
    elif p.startswith("~/"):
        p = posixpath.join(_sftp_home(sftp), p[2:])
    elif p.startswith("~"):
        raise SshError("远端路径 %s：SFTP 只支持 ~ 与 ~/（~user 形式无法展开），"
                       "请用绝对路径" % p, "bad_args")
    if p != path:
        log("[PATH] 远端路径规范化: %s -> %s" % (path, p))
    return p


def _remote_glob_error(path):
    """构造"路径不存在且含通配符"的专属错误消息（SFTP 无 glob，按字面量找必然不存在）。"""
    return ("远端路径不存在: %s —— 路径含通配符（SFTP 不做 glob 展开），"
            "请先 pyaissh ls 列出目录拿到明确文件名，再逐个传输" % path)


def _fix_msys_local_path(path):
    """Git Bash 经 ./pyaissh 包装器（MSYS_NO_PATHCONV=1）运行时，把 /tmp/... 这类
    Unix 风格【本地】路径转换成真实 Windows 路径。

    背景：包装器禁用了 MSYS 路径转换后，--local /tmp/x 会原样到达 Windows Python，
    被解析成当前盘根 D:\\tmp\\x——shell 视角路径明明存在却报"不存在"，下载方向
    更会写错位置。直接 python pyaissh.py 运行时 MSYS 已提前转换，路径到这儿已是
    Windows 风格，本函数原样返回。
    """
    if not path or os.name != "nt" or not os.environ.get("MSYSTEM"):
        return path
    if path.startswith("~"):
        return os.path.expanduser(path)
    if not path.startswith("/"):
        return path  # 相对路径 / Windows 路径不受影响
    try:
        out = subprocess.run(["cygpath", "-w", path],
                             capture_output=True, text=True, timeout=CYGPATH_TIMEOUT)
        if out.returncode == 0 and out.stdout.strip():
            converted = out.stdout.strip()
            log("[PATH] 本地路径 MSYS 转换: %s -> %s" % (path, converted))
            return converted
    except Exception:
        pass
    log("[WARN] 本地路径 %s 是 Unix 风格但无法转换（cygpath 不可用）：Git Bash 的 /tmp "
        "不是 Windows 的 /tmp，请改用 Windows 路径或相对路径" % path)
    return path


# Git Bash/MSYS 会把 glob 元字符转成私有区字符（实测 * -> U+F000 区）：直接用
# 原始字符判定会漏报"路径含通配符"的专属提示。此集合覆盖转换后形态。
_MSYS_GLOB_CHARS = set("*?[") | set("﹡？［")  # 全角/私有区常见映射
_MSYS_PRIVATE_GLOB = set(map(chr, range(0xF000, 0xF8FF)))  # PUA 区（MSYS 常用落点）


# =========================================================================
# 输出系统：日志 -> stderr，结果 -> stdout
# =========================================================================

