# -*- coding: utf-8 -*-
"""凭据来源测试：验证在【清洗掉凭据类环境变量】的条件下，凭据确实来自 pyaissh-mcp/.env。

背景：MCP 客户端配置里的 `env` 会在 spawn 时把密码放进服务器进程环境；更干净的做法
是放进 `pyaissh-mcp/.env`（CLI 每次工具调用时才读，且部分客户端会清洗环境变量——
DSH 删除匹配 /KEY|PASSWORD|SECRET|TOKEN/i 的名字与 DSH_*，写在 shell 环境里会失效）。

判别方式（不是只看一次绿灯）：同一别名跑两次——
  有 .env  → 别名解析成功，命令真的发往 .env 里配置的主机
  无 .env  → 同一别名解析失败（bad_args）
两次结果不同 ⇒ 证明正向结论来自 .env，而不是环境里的残留变量。

安全：探针主机用 TEST-NET-1（192.0.2.0/24，RFC 5737 保留、不可路由），**不碰任何真实主机**，
连接超时设 2s；探针密码是假值。测试前后备份/还原 .env。

运行：python test/test_env_creds.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McpClient, BASE, _CRED_SHAPED  # noqa: E402

ENV_FILE = os.path.join(BASE, ".env")
ALIAS = "ENVPROBE"
PROBE_HOST = "192.0.2.1"          # TEST-NET-1：保留地址，不可路由
PROBE_ENV = (
    "# 临时探针（test_env_creds.py 写入，测试结束自动还原）\n"
    "PYAISSH_HOST_%s=probeuser@%s:22\n"
    "PYAISSH_PASSWORD=envprobe-secret\n" % (ALIAS, PROBE_HOST)
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def probe(scrub=True):
    """起一个服务器实例调用 pyaissh_test @ALIAS，返回 (契约 dict, 原始响应)。"""
    c = McpClient(env={"PYAISSH_MCP_POOL": "0"}, scrub_creds=scrub)
    try:
        check_env_clean = [k for k in c.env if _CRED_SHAPED.search(k) or k.startswith("DSH_")]
        c.initialize()
        parsed, raw = c.tool_json("pyaissh_test",
                                  {"target": "@%s" % ALIAS, "timeout": 2})
        return parsed, raw, check_env_clean
    finally:
        c.close(expect_exit=False)


def main():
    had_env = os.path.isfile(ENV_FILE)
    backup = None
    if had_env:
        with open(ENV_FILE, encoding="utf-8") as f:
            backup = f.read()
    try:
        # ── 正向：有 .env + 环境已清洗 → 别名/密码由 .env 提供 ──
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(PROBE_ENV)
        parsed, raw, dirty = probe(scrub=True)
        check("T1 子进程环境已清洗凭据类变量（模拟 DSH）", not dirty, repr(dirty))
        check("T2 tools/call 返回规范结果（content 块）",
              isinstance(raw, dict) and "content" in raw, repr(raw)[:160])
        err = parsed.get("error")
        check("T3 别名由 .env 解析成功（非 bad_args）", err != "bad_args",
              "error=%r message=%r" % (err, str(parsed.get("message"))[:160]))
        blob = json.dumps(parsed, ensure_ascii=False)
        check("T4 目标主机来自 .env（出现 %s）" % PROBE_HOST, PROBE_HOST in blob,
              blob[:200])
        check("T5 失败类型属连接类（探针主机不可路由）",
              err in ("connection_timeout", "connection_failed", "connection_refused"),
              "error=%r" % (err,))

        # ── 反向：删掉 .env → 同一别名应解析失败（机制判别） ──
        os.remove(ENV_FILE)
        parsed2, raw2, _ = probe(scrub=True)
        check("T6 无 .env 时同一别名报 bad_args（证明 T3/T4 来自 .env）",
              parsed2.get("error") == "bad_args",
              "error=%r message=%r" % (parsed2.get("error"),
                                       str(parsed2.get("message"))[:160]))
    finally:
        if backup is not None:
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write(backup)
        elif os.path.isfile(ENV_FILE):
            os.remove(ENV_FILE)

    print("\n=== %s: %d PASS / %d FAIL ===" % ("test_env_creds", PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
