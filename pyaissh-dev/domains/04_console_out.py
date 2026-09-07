"""控制台与输出层（域 04）。

- _setup_console_utf8()：Windows 控制台 UTF-8（模块级调用 + main 幂等）
- _QUIET / log()：进度日志（stderr；--field 静音模式只放 [WARN] 信号，1.5.19）
- 结果输出：emit（JSON/--text）、_emit_result（--field 分流）、_emit_fields（字段提取+
  stderr 盲区提示）、emit_error（错误完整 JSON）
- 异常基类 SshError / ExecIdleTimeout / ExecTotalTimeout
被所有 cmd_*（输出/报错）调用；log/emit 的语义契约见 SKILL.md 输出约定。
"""

def _setup_console_utf8():
    """Windows 下解决中文乱码。

    背景：Python 在 Windows 上默认按本地代码页（如 GBK/936）编码 stdout/stderr，
    而 Git Bash/mintty、Windows Terminal、新版 conhost 等终端按 UTF-8 解码，
    导致中文日志显示为乱码。本函数做两件事：
      1) stdout 是控制台时，用 ctypes 把控制台输出代码页切到 65001 (UTF-8)，
         进程退出时恢复原代码页（不污染用户后续使用的终端）；
      2) 无条件把 stdout/stderr 重配为 UTF-8 输出——重定向/管道场景同样适用
         （--json 输出给 AI 解析时也必须保证是 UTF-8）。
    """
    if os.name != "nt":
        # Linux/macOS：locale 可能非 UTF-8（如 LANG=C/LC_ALL=C），stdout 编码
        # 会是 ASCII，打印中文（错误消息、--json 结果）直接 UnicodeEncodeError 崩溃。
        # errors="replace"：argv/文件名/env 经 surrogateescape 解码可能带 lone
        # surrogate（非法 UTF-8 字节），strict 编码打印必抛 UnicodeEncodeError——
        # 这是唯一能让"stdout 恒单行 JSON"契约破裂的入口（upload 非 UTF-8 文件名、
        # 异常消息含 surrogate 等实测都会炸）。replace 保证永远可打印（surrogate
        # →U+FFFD，JSON 仍单行合法）。UTF-8 locale 下 errors 默认 strict，也要重配
        # （条件同时看 encoding 与 errors）。stdin 同配：--cmd-file - 读 UTF-8 内容
        # 在 LANG=C 下不会 UnicodeDecodeError。
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            try:
                if stream and stream.reconfigure and (
                        (stream.encoding or "").lower().replace("-", "") != "utf8"
                        or stream.errors != "replace"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return
    old_cp = None
    kernel32 = None
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if sys.stdout and sys.stdout.isatty():
            old_cp = kernel32.GetConsoleOutputCP()
            kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass  # 非控制台环境（管道/无 ctypes 权限等）跳过代码页切换
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if stream and stream.reconfigure and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if old_cp and kernel32:
        try:
            import atexit
            atexit.register(lambda: kernel32.SetConsoleOutputCP(old_cp))
        except Exception:
            pass


# 模块级立即生效（不只在 main()）：import 路径（`python -c "import pyaissh"`、
# AI 嵌入、测试 harness）下 stdout/stderr 也保证 UTF-8，否则管道捕获时
# 中文日志（WARN 等）按本地代码页（GBK）写出会被 UTF-8 解码成乱码。
# main() 里再调一次是幂等兜底（脚本路径双跑无副作用）。
_setup_console_utf8()


# --field 消费端模式的进度日志静音开关：--field 消费者只要字段裸值 + 信号，
# [SSH]/[OK]/[EXEC] 进度行是噪音（v1.5.19：消费者被噪音烦到 2>/dev/null，
# 把 stderr 盲区提示一起静音——死结；静音噪音后 stderr 只剩信号，屏蔽动机消失）。
# 置位点：main() 解析出 args.field 后（见 main）。
_QUIET = False


def log(msg, force=False):
    """进度日志，打到 stderr（两种模式都打），不污染 stdout。
    --field 静音模式下只保留 [WARN] 级信号（凭据警告等），丢进度行。
    force=True 时绕过 _QUIET（v2.1：--progress 心跳是显式请求的信号——
    长任务 + --field stdout 恰是最需要心跳的场景，不该被噪音静音吞掉）。"""
    if _QUIET and not force and not msg.startswith("[WARN]"):
        return
    print(msg, file=sys.stderr, flush=True)


def emit(result, header=None, sections=None, use_json=False):
    """统一结果输出到 stdout。

    - use_json=True：整行打印一个 JSON 对象
    - use_json=False：打印 header + 各 ---MARKER--- 区块 + ---END---
    """
    if use_json:
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    if header:
        print(header, flush=True)
    if sections:
        for marker, content in sections:
            print("---%s.%s---" % (marker, _TEXT_NONCE), flush=True)
            if content:
                print(content, flush=True)
    print("---END.%s---" % _TEXT_NONCE, flush=True)


def _emit_fields(result, field_spec):
    """--field 消费端字段提取（成功路径）：打印顶层字段值，不打印 JSON。

    - 无 - 前缀字段 -> 打进程 stdout（单字段裸值；多字段每行一个值）
    - - 前缀字段（如 -stderr）-> 打进程 stderr（调用方 2>&1 或单独流可见，
      不被 stdout 展示脚本吞掉——实测教训：AI 只读 stdout 字段丢了 stderr 报错）
    - dict/list 值 JSON 序列化（如 ls 的 entries、upload 的 file_list）
    - 值本身多行（如 stdout 内容）原样保留
    - 字段不存在（拼错）-> stderr 提示字段名（不静默空行误导）
    - 结果 stderr 字段非空且本次未提取 stderr（未给 stderr/-stderr）-> 自动在
      进程 stderr 打提示（A 升级：--field stdout 吞 stderr 教训第三次应验——
      传输失败真实原因在 stderr 里被吞，多烧一轮排查；提示走进程 stderr 不
      污染 stdout 裸值，只读 stdout 的消费者也能察觉有 stderr 值得看）
    错误路径不走这里（emit_error 保持完整 JSON，AI 需要 retryable/message）。"""
    names = [s.strip().lstrip("-") for s in field_spec.split(",") if s.strip()]
    for spec in field_spec.split(","):
        spec = spec.strip()
        if not spec:
            continue
        to_stderr = spec.startswith("-")
        name = spec[1:] if to_stderr else spec
        if name not in result:
            print("[pyaissh: 字段不存在: %s（可用字段见默认 JSON 输出的键名）]" % name,
                  file=sys.stderr, flush=True)
            continue
        value = result[name]
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        elif value is None:
            text = ""
        else:
            text = str(value)
        if to_stderr:
            print(text, file=sys.stderr, flush=True)
        else:
            print(text, flush=True)
    # stderr 盲区处理（v2.1 升级：命令失败直接给内容，不再让 AI 多跑一轮取 stderr）
    err_val = result.get("stderr")
    if err_val is not None and str(err_val).strip() and "stderr" not in names:
        err_s = str(err_val)
        rc = result.get("exit_code")
        failed = result.get("ok") is False or (rc not in (0, None))
        if failed:
            # 失败路径：直接打 stderr 尾巴（1KB 封顶，报错通常在尾部）——AI 一次
            # 往返拿到真实报错（实测教训：pip 装依赖失败只给提示要多烧一轮真金白银）
            tail = err_s if len(err_s) <= 1024 else "…" + err_s[-1024:]
            print("[pyaissh: 命令失败(exit_code=%s) 且本次未提取 stderr——"
                  "stderr 尾巴%s（完整内容用 -stderr 字段: --field stdout,-stderr）:\n%s]"
                  % (rc, "" if len(err_s) <= 1024 else "（仅尾部 1KB）", tail),
                  file=sys.stderr, flush=True)
        else:
            print("[pyaissh: 结果含非空 stderr（%d 字节）——本次 --field 未提取 stderr，"
                  "真实报错可能在其中；用 -stderr 字段（--field stdout,-stderr）查看]"
                  % len(err_s), file=sys.stderr, flush=True)


def _emit_result(args, result, header=None, sections=None):
    """统一结果输出：--field 消费端模式打印字段；否则标准 emit（JSON / --text）。"""
    field = getattr(args, "field", None)
    if field:
        _emit_fields(result, field)
    else:
        emit(result, header=header, sections=sections,
             use_json=getattr(args, "json", True))


def emit_error(use_json, error_type, message, extra=None):
    """错误输出到 stdout（不返回退出码，由调用方自行 return）。

    打印期间屏蔽 KeyboardInterrupt：中断处理路径里再被信号打断会撕裂
    单行 JSON 输出（部分写入 + 上层再打一行），破坏"单行单对象"契约。
    错误对象恒含 version/action/duration_ms/warnings（与成功结果一致，
    help 契约"结果均含"）：action 取当前子命令（解析前/未知为 None），
    duration_ms 自 main() 启动计时，warnings 为空列表（extra 可覆盖）。
    """
    err = {"ok": False, "error": error_type, "message": message,
           "retryable": error_type in _RETRYABLE_ERRORS,
           "version": VERSION,
           "action": _CURRENT_ACTION,
           "duration_ms": int((time.time() - _MAIN_START) * 1000) if _MAIN_START else None,
           "warnings": []}
    if extra:
        err.update(extra)
    try:
        if use_json:
            print(json.dumps(err, ensure_ascii=False), flush=True)
        else:
            log("[ERROR] %s: %s" % (error_type, message))
            print("---ERROR.%s---" % _TEXT_NONCE, flush=True)
            print(json.dumps(err, ensure_ascii=False), flush=True)
            print("---END.%s---" % _TEXT_NONCE, flush=True)
    except KeyboardInterrupt:
        # 双重中断（第二击恰好落在构造与打印之间）会零输出：补打一次，
        # 再被打断就放弃（退出码 130 仍能表意）
        try:
            print(json.dumps(err, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            pass


def _interrupt_msg():
    """生成中断消息文案：区分 SIGTERM / SIGINT（Ctrl+C）来源。

    历史版本两信号共用 handler、KI 消息恒为 "SIGTERM"，Ctrl+C 用户会看到
    误导性字样。这里统一取 _INTERRUPT_SOURCE；纯 Ctrl+C（无信号标志，如
    argparse 阶段）回落 "Ctrl+C"。"""
    if _SIGTERM_RECEIVED:
        return "用户中断（%s）" % _INTERRUPT_SOURCE
    return "用户中断（Ctrl+C）"


# =========================================================================
# 辅助函数
# =========================================================================

