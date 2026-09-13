#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pyaissh-mcp —— pyaissh 的 MCP 薄适配层（stdio，零 SDK 依赖）

架构总纲（为什么这样设计）：
    pyaissh.py 是唯一契约源。本进程通过 importlib 加载同目录的 pyaissh.py
    （md5 与 CLI 源一致的固定副本），每次工具调用直接调它的 main()——
    main() 原生支持进程内复用（v1.5.x 为 AI 嵌入/测试 harness 做过全局状态
    复位），sys.argv 注入参数、stdout/stderr 重定向捕获结果。CLI 的全部契约
    逻辑（三重超时/截断/错误分类/retryable/warnings）零旁路、零复制——
    MCP 用户拿到的 JSON 与 CLI 用户逐字节同源，回归套件测的就是它。

连接池（会话式，仅 exec/ls）：
    - 池键 = 目标 host/user/port + 认证指纹 + 跳板链，不同凭据不共享连接
    - 复用前 open_session 探活：死连接在【发送命令前】被替换，保证命令
      恰好执行一次（不存在"命令发进死 TCP 黑洞后重试=双份执行"）
    - keepalive 心跳防 NAT/防火墙静默断链（半开连接由探活兜底）
    - 空闲 TTL 自动淘汰：不干活的连接几分钟后自动消失，无 7x24 常驻暴露
    - 淘汰/陈旧只发生在【调用之间】——上一次调用的结果已完整交付，
      透明重连无条件安全；调用中途断开仍走 CLI 原生 connection_lost 语义
    - 传输（upload/download）不进池：与并行分片的多连接模型互不干扰
    - 认证成功后密码仍留在 conn dict（池需要它做透明重连）——MCP server
      进程本来就持有启动环境里的凭据，不新增暴露类；TTL 淘汰即释放

MCP 协议：手写 newline-delimited JSON-RPC 2.0（MCP stdio 规范），
    不引 SDK——pyaissh 全项目只依赖 paramiko，这里保持。

配置（环境变量，均有默认值）：
    PYAISSH_MCP_POOL            1/0 连接池开关（默认 1）
    PYAISSH_MCP_POOL_TTL        空闲淘汰秒数（默认 300）
    PYAISSH_MCP_POOL_KEEPALIVE  心跳间隔秒（默认 20）
    PYAISSH_MCP_POOL_PROBE      复用探活超时秒（默认 5，≈1 个 RTT 的余量）
    PYAISSH_MCP_POOL_MAX        池上限（默认 8；超出淘汰最久未用的）
    凭据照旧走 pyaissh 的体系：PYAISSH_PASSWORD / .env（本目录）/ 参数
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(BASE_DIR, "pyaissh.py")
SERVER_VERSION = "0.2.3"
SERVER_NAME = "pyaissh-mcp"

SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LATEST_PROTOCOL_VERSION = "2025-06-18"


def _log(msg):
    """诊断日志只进 stderr（stdout 是 MCP 帧，一格都不许占）"""
    try:
        sys.stderr.write("[pyaissh-mcp] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


# =========================================================================
# 加载固定副本的 pyaissh.py（契约源）
# =========================================================================

def _load_pyaissh():
    spec = importlib.util.spec_from_file_location("pyaissh", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pyaissh"] = mod
    spec.loader.exec_module(mod)  # 模块级 _setup_console_utf8 顺带把本进程流配成 UTF-8
    return mod


pyaissh = _load_pyaissh()

# import 副本后立刻接管信号：副本的 handler 只置标志不退出（CLI 语义），
# MCP server 收到 Ctrl+C/SIGTERM 应当【退出】而不是继续活着。
_SHUTDOWN = threading.Event()


def _server_signal_handler(signum, frame):
    raise SystemExit(130)


try:
    signal.signal(signal.SIGINT, _server_signal_handler)
    signal.signal(signal.SIGTERM, _server_signal_handler)
except (ValueError, OSError):
    pass  # 非主线程 / 平台不支持（Windows 的 SIGTERM 是硬杀，注册无副作用）


# =========================================================================
# 会话式连接池
# =========================================================================

class ConnPool:
    """按 (目标+凭据+跳板链) 复用已认证连接；探活、心跳、TTL 淘汰。"""

    def __init__(self):
        self.enabled = os.environ.get("PYAISSH_MCP_POOL", "1") not in ("0", "false", "no")
        self.ttl = _env_float("PYAISSH_MCP_POOL_TTL", 300.0)
        self.keepalive = _env_float("PYAISSH_MCP_POOL_KEEPALIVE", 20.0)
        self.probe_timeout = _env_float("PYAISSH_MCP_POOL_PROBE", 5.0)
        self.max_entries = int(_env_float("PYAISSH_MCP_POOL_MAX", 8))
        self._entries = {}  # key -> {"client": SSHClient, "used": monotonic}
        self._lock = threading.Lock()

    # ---- 池键：不同目标/凭据/跳板链绝不共享连接 ----
    @staticmethod
    def _auth_tag(conn):
        raw = "%s|%s" % (conn.get("key") or "", conn.get("password") or "")
        return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()[:12]

    @classmethod
    def _key(cls, conn, jump_conn):
        k = "%s@%s:%s#%s" % (conn["user"], conn["host"], conn["port"], cls._auth_tag(conn))
        if jump_conn:
            k += ">%s@%s:%s#%s" % (jump_conn["user"], jump_conn["host"], jump_conn["port"],
                                   cls._auth_tag(jump_conn))
        return k

    # ---- 复用前探活：保证命令恰好执行一次 ----
    def _healthy(self, client):
        try:
            t = client.get_transport()
            if t is None or not t.is_active():
                return False
            ch = t.open_session(timeout=self.probe_timeout)  # 走完整隧道（含跳板），端到端
            ch.close()
            return True
        except Exception:
            return False

    def _close_entry(self, client):
        _orig_close_all(client)  # 含 _jump_client 一并关（CLI 原生清理逻辑）

    def _keepalive(self, client):
        for c in (client, getattr(client, "_jump_client", None)):
            try:
                t = c.get_transport()
                if t is not None:
                    t.set_keepalive(self.keepalive)
            except Exception:
                pass

    def borrow(self, key, connect_fn):
        """取连接：命中且探活 → 复用；否则淘汰旧条目并新建。

        connect_fn 只在未命中时调用（保证"复用"路径零握手开销）。
        探活失败 = 连接陈旧（NAT 断/half-open）：在命令发出前替换，
        本函数返回后 CLI 才开始跑命令——重连发生在调用之间，无条件安全。
        in_use 计数防止 janitor 把【正在执行长命令】的连接当空闲关掉
        （max-time 1200 的长任务可以远超 TTL）。
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                idle = time.monotonic() - entry["used"]
                if self._healthy(entry["client"]):
                    entry["used"] = time.monotonic()
                    entry["in_use"] += 1
                    _log("池命中: %s (空闲 %.0fs)" % (key, idle))
                    return entry["client"]
                _log("池条目陈旧，淘汰重连: %s" % key)
                self._close_entry(entry["client"])
                del self._entries[key]
            client = connect_fn()
            client._mcp_pooled = True
            self._keepalive(client)
            self._entries[key] = {"client": client, "used": time.monotonic(), "in_use": 1}
            self._evict_over_limit_locked()
            return client

    def release(self, client):
        """调用结束（close_all 被调）时归还：in_use -1，供 janitor 放行。"""
        with self._lock:
            for entry in self._entries.values():
                if entry["client"] is client:
                    entry["in_use"] = max(0, entry["in_use"] - 1)
                    entry["used"] = time.monotonic()
                    return

    def _evict_over_limit_locked(self):
        while len(self._entries) > self.max_entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k]["used"])
            entry = self._entries.pop(oldest_key)
            _log("池满（>%d），淘汰最久未用: %s" % (self.max_entries, oldest_key))
            self._close_entry(entry["client"])

    def janitor_tick(self):
        """TTL 淘汰 + 死条目清理（后台线程周期调用）。"""
        now = time.monotonic()
        with self._lock:
            for key in list(self._entries):
                entry = self._entries[key]
                if entry.get("in_use", 0) > 0:
                    continue  # 正在执行调用的连接不淘汰（防长任务被中途断链）
                idle = now - entry["used"]
                t = None
                try:
                    t = entry["client"].get_transport()
                except Exception:
                    pass
                if idle >= self.ttl or t is None or not t.is_active():
                    _log("%s: %s (空闲 %.0fs)" % ("TTL 淘汰" if idle >= self.ttl else "死条目清理",
                                                  key, idle))
                    self._close_entry(entry["client"])
                    del self._entries[key]

    def close_all(self):
        with self._lock:
            for key, entry in self._entries.items():
                try:
                    self._close_entry(entry["client"])
                except Exception:
                    pass
            self._entries.clear()


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


POOL = ConnPool()

# 等待作业结束的上限：客户端（DSH 默认 toolCallTimeoutMs=60s）会在超时点掐断调用，
# 所以在 MCP 层先拦一道，留出余量；要等更久就调客户端超时或分多次调用。
WAIT_RC_MAX = int(_env_float("PYAISSH_MCP_WAIT_RC_MAX", 45))

# ---- monkey-patch：仅 connect / close_all 两个入口，其余契约逻辑零改动 ----
_orig_connect = pyaissh.connect
_orig_close_all = pyaissh.close_all


def _pooled_connect(conn, jump_conn=None):
    # 池只服务 exec/ls/log：test 必须真连（不然"连通性测试"复用旧连接是假测试）；
    # 传输不进池（与并行分片的多连接模型互不干扰）。main() 在派发前把
    # 子命令名写进 _CURRENT_ACTION（cmd_xxx 去前缀），此处按它路由。
    # log 进池尤其重要：准流式会高频轮询，不进池每次都要重建 SSH（跨境 ~1s）。
    if (not POOL.enabled
            or getattr(pyaissh, "_CURRENT_ACTION", None) not in ("exec", "ls", "log")):
        return _orig_connect(conn, jump_conn)
    key = POOL._key(conn, jump_conn)
    return POOL.borrow(key, lambda: _orig_connect(conn, jump_conn))


def _pooled_close_all(client):
    if not getattr(client, "_mcp_pooled", False):
        return _orig_close_all(client)
    POOL.release(client)  # 归还：in_use -1，janitor 恢复 TTL 计时
    # 池化连接：只清当前调用的通道（exec channel/SFTP session），保住传输层
    try:
        ts = pyaissh._ACTIVE_TRANSPORTS
        t = client.get_transport()
        try:
            ts.discard(t)
        except AttributeError:
            ts.remove(t)
    except Exception:
        pass
    try:
        t = client.get_transport()
        chans = getattr(t, "_channels", None) if t is not None else None
        if chans:
            for ch in list(chans.values()):
                try:
                    ch.close()
                except Exception:
                    pass
    except Exception:
        pass


pyaissh.connect = _pooled_connect
pyaissh.close_all = _pooled_close_all


def _janitor_loop():
    interval = max(2.0, min(POOL.ttl / 3.0, 20.0))
    while not _SHUTDOWN.wait(interval):
        try:
            POOL.janitor_tick()
        except Exception as e:
            _log("janitor 异常: %r" % e)


threading.Thread(target=_janitor_loop, daemon=True, name="pyaissh-mcp-janitor").start()


# =========================================================================
# 工具定义（schema 与 CLI 参数一一对应；细节语义以 CLI --help / docs 为准）
# =========================================================================

_AUTH_PROPS = {
    "password": {"type": "string", "description": "SSH 密码（推荐走服务端环境变量 PYAISSH_PASSWORD / .env，避免出现在调用记录里）"},
    "key": {"type": "string", "description": "私钥文件路径（优先于密码）"},
    "port": {"type": "integer", "description": "端口，默认 22（target 内嵌 :端口 优先级低于此参数）"},
    "timeout": {"type": "integer", "description": "连接超时秒数，默认 10"},
    "jump": {"type": "string", "description": "跳板机 [user@]host[:port]（也支持 @别名）"},
    "jump_password": {"type": "string", "description": "跳板机密码"},
    "jump_key": {"type": "string", "description": "跳板机私钥路径"},
}

TOOLS = [
    {
        "name": "pyaissh_test",
        "description": "测试 SSH 连接并返回主机信息（hostname/os/kernel/arch）。连接任何主机前先 test，失败按错误类型处理，别盲目重试。",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "[user@]host[:port]，如 root@1.2.3.4:22；@别名"}, **_AUTH_PROPS},
            "required": ["target"],
        },
    },
    {
        "name": "pyaissh_exec",
        "description": "在远程主机执行命令，返回结构化 JSON（exit_code/exit_success/stdout/stderr/warnings）。ok=true 只表示执行完成，命令成败看 exit_success。长任务（>1 分钟/不受客户端调用时长限制/想边跑边看）用 detach=true 后台化，再用 pyaissh_log 增量读日志。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "[user@]host[:port]；@别名"},
                "cmd": {"type": "string", "description": "远程命令（多行/复杂脚本建议用 cmd_file）"},
                "cmd_file": {"type": "string", "description": "本地脚本文件路径（内容作为命令执行；MCP 模式不支持 \"-\"）"},
                "detach": {"type": "boolean", "description": "后台运行（远端 setsid+nohup，SSH 断开不影响）：立即返回 job_id/log/rc/next_action，之后用 pyaissh_log 增量读、等退出码、清理。与 sudo/pty 互斥"},
                "sudo": {"type": "boolean", "description": "sudo 提权执行（普通用户登录时）；密码来源 sudo_password 或服务端环境变量 PYAISSH_SUDO_PASSWORD；无密码时自动 sudo -n 探测"},
                "sudo_password": {"type": "string", "description": "sudo 密码（仅经 SSH stdin 注入，不进命令文本/日志）"},
                "encoding": {"type": "string", "description": "远端输出解码编码（默认 utf-8，非 UTF-8 远端如 gbk/shift_jis）"},
                "idle_timeout": {"type": "integer", "description": "静默超时秒（连续无输出上限，默认 60）"},
                "max_time": {"type": "integer", "description": "总时长上限秒（默认 2×idle_timeout 且至少 120，上限 1200）"},
                "max_output": {"type": "integer", "description": "单流输出最大保留字节（默认 64KB，超出保留头尾+完整输出落盘 spill 文件，路径见 stdout_spill_file 与 next_action）"},
                "pty": {"type": "boolean", "description": "分配 PTY（仅非交互命令；与 sudo 互斥）"},
                "pty_strip_ansi": {"type": "boolean", "description": "剥离 ANSI 颜色序列（需配合 pty）"},
                **_AUTH_PROPS,
            },
            "required": ["target", "cmd"],
        },
    },
    {
        "name": "pyaissh_log",
        "description": "读后台作业（pyaissh_exec detach=true 启动）的日志与结束状态：默认回传尾部 100 行；offset 增量读（用返回的 next_offset 续读，不重复）；wait_rc 阻塞等结束拿 exit_code；kill 整组停掉；cleanup 清理远端作业目录；list 列该主机所有作业。载荷字段是 stdout（2>&1 合并流）。status 三级：finished（有退出码）/ dead（无 rc 且进程已消失，被 kill/OOM/崩溃）/ running。准流式用法：exec(detach) → 循环 log(offset=next_offset) → log(wait_rc) → log(cleanup)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "[user@]host[:port]；@别名"},
                "job_id": {"type": "string", "description": "作业 id（detach 的返回；只允许字母/数字/下划线/点/连字符）"},
                "path": {"type": "string", "description": "直接指定日志文件路径（名为 job.log 时自动配对同目录 job.rc/job.pid）"},
                "list": {"type": "boolean", "description": "列出该主机作业根目录下所有作业（job_id/status/log_bytes/exit_code/mtime）；与 job_id/path 互斥"},
                "lines": {"type": "integer", "description": "回传尾部 N 行（默认 100；与 offset 互斥）"},
                "offset": {"type": "integer", "description": "从该字节偏移增量读；返回 next_offset 供下次续读（与 lines 互斥）"},
                "wait_rc": {"type": "integer", "description": "阻塞等待作业结束最多 N 秒（MCP 层上限 45s，受客户端 toolCallTimeoutMs 约束），结束即返回 exit_code（作业被 kill/OOM 时状态收敛为 dead）"},
                "kill": {"type": "boolean", "description": "整组停掉作业：读 job.pid 对进程组 TERM → 宽限 5s → KILL（Linux 上先校验 pid 确属本作业防误杀）；需 job_id，可与 wait_rc 连用（kill 后立即收敛 dead）"},
                "cleanup": {"type": "boolean", "description": "读完后删除远端作业目录（job.sh/run.sh/job.log/job.rc/job.pid）；需 job_id；作业仍在运行时会被拒绝（job_running，防自断追踪），先 kill 或用 force"},
                "force": {"type": "boolean", "description": "配合 cleanup：作业仍在运行时也强制清理（本工具不再追踪该作业，远端进程可能仍在跑）；须与 cleanup 同用"},
                "job_dir": {"type": "string", "description": "作业根目录（默认 /tmp/pyaissh-jobs）"},
                "limit": {"type": "integer", "description": "list 最多返回条数（默认 50）"},
                "max_output": {"type": "integer", "description": "单次回传内容上限字节（默认 64KB；截断时看 omitted_bytes，增量读用 offset 继续）"},
                "encoding": {"type": "string", "description": "日志解码编码（默认 utf-8）"},
                **_AUTH_PROPS,
            },
            "required": ["target"],
        },
    },
    {
        "name": "pyaissh_ls",
        "description": "列远程目录，返回 entries[]（name/mode/size/is_dir/is_symlink/mtime epoch 秒）。不支持通配符，先 ls 拿明确文件名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "[user@]host[:port]；@别名"},
                "path": {"type": "string", "description": "远程目录路径（默认 .；支持 ~ 展开）"},
                "long": {"type": "boolean", "description": "额外输出文本清单行"},
                "limit": {"type": "integer", "description": "最多返回条目数（默认 2000，超出 truncated=true）"},
                **_AUTH_PROPS,
            },
            "required": ["target"],
        },
    },
    {
        "name": "pyaissh_upload",
        "description": "上传本地文件/目录到远端（SFTP）。结果只含元数据（files/bytes/file_list），文件内容零回传。失败/中断不留半截最终文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "[user@]host[:port]；@别名"},
                "local": {"type": "string", "description": "本地路径（文件或目录）"},
                "remote": {"type": "string", "description": "远端路径（~ 展开；尾斜杠=目录意图）"},
                "parallel": {"type": "integer", "description": "并行分片上传连接数 1-8（高丢包/长 RTT 链路提速，收益随链路而定）"},
                "resume": {"type": "boolean", "description": "断点续传（仅单文件 ≥50MB 有意义；与 parallel 互斥）"},
                "skip_existing": {"type": "boolean", "description": "跳过远端已存在且同大小的文件"},
                "dry_run": {"type": "boolean", "description": "只预览清单不传输"},
                "no_recursive": {"type": "boolean", "description": "目录源不递归子目录"},
                **_AUTH_PROPS,
            },
            "required": ["target", "local"],
        },
    },
    {
        "name": "pyaissh_download",
        "description": "下载远端文件/目录到本地（SFTP）。结果只含元数据，文件内容零回传。大文件慢/超时加 parallel 8（多连接近似线性提速）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "[user@]host[:port]；@别名"},
                "local": {"type": "string", "description": "本地目标路径（目录=放入其中）"},
                "remote": {"type": "string", "description": "远端路径（~ 展开；尾斜杠=目录意图）"},
                "parallel": {"type": "integer", "description": "并行分片下载连接数 1-8（≥8MB 自动 4 连接；显式指定 ≥64KB 即分片）"},
                "resume": {"type": "boolean", "description": "断点续传（仅单文件）"},
                "skip_existing": {"type": "boolean", "description": "跳过本地已存在且同大小的文件"},
                "dry_run": {"type": "boolean", "description": "只预览清单不传输"},
                "no_recursive": {"type": "boolean", "description": "目录源不递归子目录"},
                **_AUTH_PROPS,
            },
            "required": ["target", "local", "remote"],
        },
    },
]

# 工具参数 → CLI flag 映射（snake_case → --kebab-case；bool=true 才加 flag）
_FLAG_MAP = {
    "cmd": "--cmd", "cmd_file": "--cmd-file", "sudo": "--sudo", "sudo_password": "--sudo-password",
    "encoding": "--encoding", "idle_timeout": "--idle-timeout", "max_time": "--max-time",
    "max_output": "--max-output", "pty": "--pty", "pty_strip_ansi": "--pty-strip-ansi",
    "path": "--path", "long": "--long", "limit": "--limit",
    "local": "--local", "remote": "--remote", "parallel": "--parallel", "resume": "--resume",
    "skip_existing": "--skip-existing", "dry_run": "--dry-run", "no_recursive": "--no-recursive",
    "password": "--password", "key": "--key", "port": "--port", "timeout": "--timeout",
    "jump": "--jump", "jump_password": "--jump-password", "jump_key": "--jump-key",
    # v0.2.0 后台作业（准流式）
    "detach": "--detach", "job_id": "--job-id", "list": "--list", "lines": "--lines",
    "offset": "--offset", "wait_rc": "--wait-rc", "kill": "--kill", "cleanup": "--cleanup",
    "force": "--force", "job_dir": "--job-dir",
}
_TOOL_SUB = {t["name"]: t["name"][len("pyaissh_"):] for t in TOOLS}

# 参数声明类型表（工具名 → 参数 → JSON Schema type）：
# 用来把"类型传错"变成**可读错误或合理强制**，而不是丢给 argparse 报
# "expected one argument"（LLM 最自然的输入 wait_rc=true 曾踩中两次）。
_SCHEMA_TYPES = {t["name"]: {k: v.get("type") for k, v in t["inputSchema"]["properties"].items()}
                 for t in TOOLS}
_TRUTHY = ("true", "yes", "on", "1")
_FALSY = ("false", "no", "off", "0", "")
# 数值型参数收到 `true`（"帮我把这个打开"）时的强制值：按 MCP 层上限执行更贴近本意
_BOOL_TRUE_DEFAULT = {"wait_rc": WAIT_RC_MAX}
# 数值参数传 0 = "不要这个行为"（wait_rc: 0 → 不等待），而非报"必须为正整数"
_ZERO_MEANS_OFF = ("wait_rc",)


def _type_error(tool, key, value, hint):
    return {"ok": False, "error": "bad_args", "retryable": False,
            "message": "参数 %s 类型不对（收到 %r）：%s（工具 %s）"
                       % (key, value, hint, tool)}


def _build_argv(tool, args):
    """工具参数 → sys.argv。按 schema 声明类型做容错与校验，返回 (argv, error_json, notes)。

    容错规则（v0.2.2，针对 LLM 常见写法）：
    - 布尔型参数收到字符串 "true"/"false"（含 yes/no/on/off/1/0）→ 按布尔处理
    - **数值型参数收到 `true`**：wait_rc 按上限强制（"我要等"），其余给可读 bad_args
      （此前一律当旗标裸传 → argparse "expected one argument"，两次踩中）
    - 数值型参数收到 `false` / wait_rc 收到 0 → 视为"不要这个行为"，不传该参数
    """
    argv = ["pyaissh", _TOOL_SUB[tool], str(args["target"])]
    notes = []
    declared = _SCHEMA_TYPES.get(tool, {})
    for k, v in args.items():
        if k == "target":
            continue
        flag = _FLAG_MAP.get(k)
        if flag is None:
            _log("忽略未知参数 %s=%r（工具 %s）" % (k, v, tool))
            continue
        dtype = declared.get(k)

        if dtype == "boolean":
            if isinstance(v, bool):
                if v:
                    argv.append(flag)
                continue
            if isinstance(v, str) and v.strip().lower() in _TRUTHY + _FALSY:
                if v.strip().lower() in _TRUTHY:
                    argv.append(flag)
                continue
            if isinstance(v, (int, float)):
                if v:
                    argv.append(flag)
                continue
            return None, _type_error(tool, k, v, "该参数是开关，请传 true/false"), notes

        if dtype in ("integer", "number"):
            if isinstance(v, bool):
                if not v:
                    continue
                if k in _BOOL_TRUE_DEFAULT:
                    v = _BOOL_TRUE_DEFAULT[k]
                    notes.append("%s 收到 true（该参数需要数值），已按上限 %s 执行"
                                 % (k, v))
                else:
                    return None, _type_error(
                        tool, k, v, "该参数需要数值（例如 %s: 100），不是开关" % k), notes
            elif isinstance(v, str) and v.strip().lower() in _TRUTHY + _FALSY:
                low = v.strip().lower()
                if low in _TRUTHY and k in _BOOL_TRUE_DEFAULT:
                    raw = v
                    v = _BOOL_TRUE_DEFAULT[k]
                    notes.append("%s 收到字符串 %r（该参数需要数值），已按上限 %s 执行"
                                 % (k, raw, v))
                elif low in _FALSY and k in _ZERO_MEANS_OFF:
                    continue
                else:
                    return None, _type_error(
                        tool, k, v, "该参数需要数值（例如 %s: 100），不是布尔" % k), notes
            elif isinstance(v, float) and dtype == "integer" and not float(v).is_integer():
                return None, _type_error(tool, k, v, "该参数需要整数"), notes
            elif isinstance(v, int) and v == 0 and k in _ZERO_MEANS_OFF:
                notes.append("%s=0 视为不启用该行为" % k)
                continue
        elif dtype == "string" and isinstance(v, bool):
            return None, _type_error(tool, k, v, "该参数需要字符串"), notes

        argv += [flag, str(v)]

    if str(args.get("cmd_file", "")).strip() == "-":
        return None, {"ok": False, "error": "bad_args", "retryable": False,
                      "message": "MCP 模式不支持 cmd_file=\"-\"（stdin 属于 MCP 协议通道）；"
                                 "请把脚本写成本地文件后用 cmd_file 传路径"}, notes
    if tool == "pyaissh_log":
        # wait_rc 上限：客户端会在 toolCallTimeoutMs（DSH 默认 60s）处掐断调用，
        # 与其被掐断（AI 只看到"调用超时"、拿不到任何作业状态），不如提前拒绝并给出两条出路。
        try:
            wait = int(args.get("wait_rc") or 0)
        except (TypeError, ValueError):
            wait = 0
        if wait > WAIT_RC_MAX:
            return None, {"ok": False, "error": "bad_args", "retryable": False,
                          "message": "wait_rc=%d 超过 MCP 层上限 %ds（客户端调用超时会先掐断，"
                                     "等不到结果）：改用 wait_rc<=%d 分多次调用，或调大客户端的 "
                                     "toolCallTimeoutMs / 环境变量 PYAISSH_MCP_WAIT_RC_MAX"
                                     % (wait, WAIT_RC_MAX, WAIT_RC_MAX)}, notes
    return argv, None, notes


def _call_cli(argv):
    """进程内调用 pyaissh.main()：sys.argv 注入 + stdout/stderr 捕获。

    返回 (parsed_json_or_None, raw_stdout, stderr_text, exit_code)。
    main() 原生支持进程内复用（每调用复位全局状态、单例救援线程），
    SystemExit（argparse 层 bad_args）穿透 main 由这里接住——缓冲里
    已有一行 bad_args JSON。
    """
    buf_out, buf_err = io.StringIO(), io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    rc = None
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            try:
                rc = pyaissh.main()
            except SystemExit:
                rc = 2
    finally:
        sys.argv = old_argv
    out, err = buf_out.getvalue(), buf_err.getvalue()
    parsed = None
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    if lines:
        try:
            candidate = json.loads(lines[-1])
            if isinstance(candidate, dict) and "ok" in candidate:
                parsed = candidate
        except (ValueError, IndexError):
            pass
    return parsed, out, err, rc


# =========================================================================
# MCP stdio 服务（newline-delimited JSON-RPC 2.0）
# =========================================================================

_write_lock = threading.Lock()


def _send(obj):
    line = json.dumps(obj, ensure_ascii=False)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _reply(msg_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _send(resp)


def _rpc_error(code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _handle_initialize(msg_id, params):
    requested = ""
    if isinstance(params, dict):
        requested = str(params.get("protocolVersion") or "")
    pv = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    _reply(msg_id, {
        "protocolVersion": pv,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "pyaissh：AI 专用的结构化 SSH 工具。所有工具返回 pyaissh CLI 的原生单行 JSON 契约："
            "ok=工具操作成功，exit_success=远程命令成败，错误看 error+message+retryable。"
            "传输类工具零 token 消耗（文件内容永不回传，只回元数据）。"
            "连接新主机前先 pyaissh_test；超时类错误重试前先确认远程进程（remote_may_be_running）。"
        ),
    })


def _handle_tools_call(msg_id, params):
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _reply(msg_id, error=_rpc_error(-32602, "无效参数：缺少 tool name"))
    name = params["name"]
    if name not in _TOOL_SUB:
        return _reply(msg_id, error=_rpc_error(-32602, "未知工具: %s" % name))
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _reply(msg_id, error=_rpc_error(-32602, "无效参数：arguments 必须是对象"))
    try:
        argv, err_json, notes = _build_argv(name, args)
    except (KeyError, TypeError, ValueError) as e:
        return _reply(msg_id, error=_rpc_error(-32602, "无效参数: %r" % e))
    if err_json is not None:
        parsed, ok_flag = err_json, True
    else:
        t0 = time.monotonic()
        parsed, raw_out, err_text, rc = _call_cli(argv)
        dt = time.monotonic() - t0
        _log("%s %s -> %.2fs rc=%s" % (name, args.get("target", "?"), dt, rc))
        if err_text.strip():
            for ln in err_text.strip().splitlines():
                _log("[cli-stderr] %s" % ln)
        ok_flag = isinstance(parsed, dict) and parsed.get("ok") is False
    if notes and isinstance(parsed, dict):
        # 容错改写要让模型看见（否则它会以为参数被无视）：并入结果 warnings + 打 server stderr
        warns = parsed.get("warnings")
        if not isinstance(warns, list):
            warns = []
            parsed["warnings"] = warns
        for n in notes:
            warns.append("[MCP 参数容错] " + n)
            _log("[MCP 参数容错] %s" % n)
    if parsed is None:
        text = (raw_out.strip() or "（CLI 无输出）")
        return _reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})
    _reply(msg_id, {
        "content": [{"type": "text", "text": json.dumps(parsed, ensure_ascii=False)}],
        "isError": ok_flag,
    })


def _handle_message(msg):
    """返回 True=已处理（可能是通知，无响应）；None=需退出循环。"""
    if not isinstance(msg, dict):
        _send({"jsonrpc": "2.0", "id": None, "error": _rpc_error(-32600, "消息必须是对象")})
        return True
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = msg_id is None
    if method == "initialize":
        if is_notification:
            return True
        _handle_initialize(msg_id, msg.get("params"))
    elif method in ("notifications/initialized", "notifications/cancelled",
                    "notifications/roots/list_changed"):
        pass  # 通知：不应答（cancel 语义 v1 忽略——调用会跑完，响应照常返回）
    elif method == "ping":
        if not is_notification:
            _reply(msg_id, {})
    elif method == "tools/list":
        if not is_notification:
            _reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        if is_notification:
            return True
        _handle_tools_call(msg_id, msg.get("params"))
    elif method in ("resources/list", "prompts/list"):
        if not is_notification:
            _reply(msg_id, {"resources": []} if method == "resources/list" else {"prompts": []})
    elif method is None:
        if not is_notification:
            _reply(msg_id, error=_rpc_error(-32600, "缺少 method"))
    else:
        if not is_notification:
            _reply(msg_id, error=_rpc_error(-32601, "未知方法: %s" % method))
    return True


def main():
    _log("%s v%s 启动 (CLI 副本: %s)" % (SERVER_NAME, SERVER_VERSION, CLI_PATH))
    _log("连接池: %s (TTL=%.0fs keepalive=%.0fs probe=%.0fs max=%d)"
         % ("开" if POOL.enabled else "关", POOL.ttl, POOL.keepalive, POOL.probe_timeout,
            POOL.max_entries))
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break  # EOF：客户端关了 stdio
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError as e:
                _send({"jsonrpc": "2.0", "id": None,
                       "error": _rpc_error(-32700, "JSON 解析失败: %s" % e)})
                continue
            _handle_message(msg)
    except SystemExit:
        pass  # 信号 handler 触发的退出
    finally:
        _log("关闭：清理连接池…")
        POOL.close_all()
        _log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
