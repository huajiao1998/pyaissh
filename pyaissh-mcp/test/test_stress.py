# -*- coding: utf-8 -*-
"""压力/边界测试：跳板池化、编码、sudo 复合、大文件并行、并发、浸泡、超时契约、非法配置。

凭据来源同 test_live.py（env 或 test/local_creds.json）。
运行：python test/test_stress.py
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McpClient, BASE  # noqa: E402
from test_live import load_creds, md5_file  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    # 本地定义（不导入 test_live.check——它引用的是 test_live 模块的全局计数器，
    # 跨模块导入会导致本套件统计恒为 0）
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def main():
    global PASS, FAIL
    h1, h2 = load_creds()
    # 并行测试隔离：独立远端/本地目录（与 test_live 同一环境变量）
    tmp_local = os.environ.get("PYAISSH_MCP_TEST_TMPDIR") or os.path.join(BASE, "test", "_tmp")
    remote_dir = os.environ.get("PYAISSH_MCP_TEST_REMOTE_DIR", "/tmp/pyaissh_mcp_test")
    os.makedirs(tmp_local, exist_ok=True)

    c = McpClient()
    c.initialize()

    # ---- S1 跳板链：经 host2 连 host1；池键含跳板，第二次温热 ----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "jump": h2["target"], "jump_password": h2["password"], "cmd": "true"})
    check("S1a 跳板 exec ok", parsed.get("ok") is True and parsed.get("exit_success") is True,
          repr(parsed)[:200])
    d1 = parsed.get("duration_ms", 99999)
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "jump": h2["target"], "jump_password": h2["password"], "cmd": "true"})
    d2 = parsed.get("duration_ms", 99999)
    check("S1b 跳板连接复用温热 (d1=%d -> d2=%dms)" % (d1, d2), d2 < d1, "d1=%s d2=%s" % (d1, d2))

    # ---- S2 非默认编码：GBK 字节按 --encoding gbk 正确解码 ----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "cmd": r"printf '\xc4\xe3\xba\xc3'", "encoding": "gbk"})
    check("S2 --encoding gbk 解码", parsed.get("exit_success") is True
          and "你好" in parsed.get("stdout", ""), repr(parsed.get("stdout", ""))[:80])

    # ---- S3 sudo 复合命令（bash -c 整链提权路径）----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "sudo": True, "cmd": "id -u && whoami"})
    check("S3 sudo 复合整链 root", parsed.get("exit_success") is True
          and "0" in parsed.get("stdout", "") and "root" in parsed.get("stdout", ""),
          repr(parsed.get("stdout", ""))[:80])

    # ---- S4 10MB 上传 --parallel 8 + 下载 md5 ----
    src = os.path.join(tmp_local, "rt_10m.bin")
    with open(src, "wb") as f:
        f.write(os.urandom(10 * 1024 * 1024))
    src_md5 = md5_file(src)
    parsed, _ = c.tool_json("pyaissh_upload", {
        "target": h1["target"], "password": h1["password"],
        "local": src, "remote": remote_dir + "/rt_10m.bin", "parallel": 8})
    check("S4a 10MB parallel 8 上传 ok", parsed.get("ok") is True, repr(parsed)[:200])
    dst = os.path.join(tmp_local, "rt_10m_down.bin")
    parsed, _ = c.tool_json("pyaissh_download", {
        "target": h1["target"], "password": h1["password"],
        "remote": remote_dir + "/rt_10m.bin", "local": dst, "parallel": 8})
    check("S4b 10MB parallel 8 下载 md5 一致 (parallel_used=%s)" % parsed.get("parallel_used"),
          parsed.get("ok") is True and md5_file(dst) == src_md5, repr(parsed)[:150])

    # ---- S5 5 路管道并发 ----
    mids = []
    for i in range(5):
        c._id += 1
        mids.append(c._id)
        c.send({"jsonrpc": "2.0", "id": c._id, "method": "tools/call",
                "params": {"name": "pyaissh_exec", "arguments": {
                    "target": h1["target"], "password": h1["password"],
                    "cmd": "echo n%d" % i}}})
    ok5 = True
    got = []
    for mid in mids:
        r = c.recv()
        ok5 = ok5 and r.get("id") == mid
        got.append(json.loads(r["result"]["content"][0]["text"]).get("stdout", "").strip())
    check("S5 5 路并发按序且结果正确", ok5 and got == ["n0", "n1", "n2", "n3", "n4"], repr(got))

    # ---- S6 20 连击浸泡：全 ok，池命中次数 >= 18（首次冷 + 19 温热 + 传输混入）----
    hit_before = sum(1 for ln in c.stderr_lines if "池命中" in ln)
    all_ok = True
    for i in range(20):
        parsed, _ = c.tool_json("pyaissh_exec", {
            "target": h1["target"], "password": h1["password"],
            "cmd": "true" if i % 3 else "echo soak%d" % i})
        all_ok = all_ok and parsed.get("ok") is True
    hit_after = sum(1 for ln in c.stderr_lines if "池命中" in ln)
    check("S6a 20 连击全 ok", all_ok, "")
    check("S6b 池命中 >= 18 次 (本轮 +%d)" % (hit_after - hit_before),
          hit_after - hit_before >= 18, "delta=%d" % (hit_after - hit_before))

    # ---- S7 超时契约字段透传（remote_may_be_running / retryable）----
    parsed, r = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "cmd": "sleep 5", "idle_timeout": 1})
    check("S7 超时契约：exec_idle_timeout + retryable + remote_may_be_running",
          parsed.get("error") == "exec_idle_timeout" and parsed.get("retryable") is True
          and parsed.get("remote_may_be_running") is True, repr(parsed)[:200])

    # ---- S8 ls --limit 截断语义 ----
    parsed, _ = c.tool_json("pyaissh_ls", {
        "target": h1["target"], "password": h1["password"],
        "path": "/etc", "limit": 5})
    check("S8 ls limit=5 截断", parsed.get("count") == 5 and parsed.get("truncated") is True,
          repr({k: parsed.get(k) for k in ("count", "total", "truncated")}))

    # ---- S9 upload --dry-run 预览不传输 ----
    parsed, _ = c.tool_json("pyaissh_upload", {
        "target": h1["target"], "password": h1["password"],
        "local": src, "remote": remote_dir + "/dryrun.bin", "dry_run": True})
    fl = parsed.get("file_list", [])
    check("S9 dry_run 预览清单", parsed.get("ok") is True and len(fl) == 1
          and fl[0].get("action") != "uploaded", repr(parsed)[:200])

    c.close()
    check("S10 优雅退出", c.proc.returncode == 0, "rc=%r" % c.proc.returncode)

    # ---- S11 非法配置回退（TTL=abc → 默认值，服务器正常起）----
    c2 = McpClient(env={"PYAISSH_MCP_POOL_TTL": "abc"})
    c2.initialize()
    parsed, _ = c2.tool_json("pyaissh_exec",
                             {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    check("S11 非法 TTL 回退默认并正常服务", parsed.get("ok") is True, repr(parsed)[:120])
    c2.close()

    # ---- 清理远端 ----
    try:
        cc = McpClient()
        cc.initialize()
        cc.tool_json("pyaissh_exec", {"target": h1["target"], "password": h1["password"],
                                      "cmd": "rm -rf %s" % remote_dir})
        cc.close()
    except Exception:
        pass

    print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
