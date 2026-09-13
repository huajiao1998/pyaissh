# -*- coding: utf-8 -*-
"""真机功能测试：exec/ls/传输/池温热/TTL/并发/错误分类。

凭据来源（优先级）：
  1. 环境变量 PYAISSH_MCP_HOST1 / PYAISSH_MCP_HOST2（格式 root@1.2.3.4）与
     PYAISSH_MCP_PW1 / PYAISSH_MCP_PW2
  2. test/local_creds.json（gitignored，格式见 local_creds.json.example）

运行：python test/test_live.py
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McpClient, BASE  # noqa: E402

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


def load_creds():
    h1p, h2p = os.environ.get("PYAISSH_MCP_HOST1"), os.environ.get("PYAISSH_MCP_HOST2")
    if h1p and h2p:
        return ({"target": h1p, "password": os.environ.get("PYAISSH_MCP_PW1", "")},
                {"target": h2p, "password": os.environ.get("PYAISSH_MCP_PW2", "")})
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_creds.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["host1"], d["host2"]


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    h1, h2 = load_creds()
    # 并行测试隔离：多实例同时跑时用独立远端/本地目录（避免互删对方传输文件）
    tmp_local = os.environ.get("PYAISSH_MCP_TEST_TMPDIR") or os.path.join(BASE, "test", "_tmp")
    os.makedirs(tmp_local, exist_ok=True)
    remote_dir = os.environ.get("PYAISSH_MCP_TEST_REMOTE_DIR", "/tmp/pyaissh_mcp_test")

    c = McpClient()
    c.initialize()

    # ---- C1 test + exec 基线（首次调用必然冷连接）----
    parsed, r = c.tool_json("pyaissh_test", {"target": h1["target"], "password": h1["password"]})
    check("C1a test ok", parsed.get("ok") is True and parsed.get("hostname"), repr(parsed)[:150])
    d_cold_test = parsed.get("duration_ms", 99999)

    parsed, _ = c.tool_json("pyaissh_exec",
                            {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    d1 = parsed.get("duration_ms", 99999)
    check("C1b exec ok/exit_success", parsed.get("ok") is True and parsed.get("exit_success") is True,
          repr(parsed)[:150])

    # ---- C2 池温热：专用新实例，首次冷、后续温（同目标连跑 3 次）----
    cw = McpClient()
    cw.initialize()
    parsed, _ = cw.tool_json("pyaissh_exec",
                             {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    d1 = parsed.get("duration_ms", 99999)  # 冷：完整 TCP+KEX+认证
    durs = []
    for i in range(2):
        parsed, _ = cw.tool_json("pyaissh_exec",
                                 {"target": h1["target"], "password": h1["password"], "cmd": "true"})
        durs.append(parsed.get("duration_ms", 99999))
    d2, d3 = durs
    check("C2a 第二次调用快于冷调用 (d1=%dms -> d2=%dms)" % (d1, d2), d2 < d1, "d1=%s d2=%s" % (d1, d2))
    check("C2b 第三次仍温热 (d3=%dms)" % d3, d3 < d1, "d1=%s d3=%s" % (d1, d3))
    check("C2c stderr 有池命中记录", any("池命中" in ln for ln in cw.stderr_lines),
          str(cw.stderr_lines[-5:]))
    cw.close()

    # ---- C3 第二台主机：独立池条目 ----
    parsed, _ = c.tool_json("pyaissh_exec",
                            {"target": h2["target"], "password": h2["password"], "cmd": "true"})
    check("C3 第二台主机 exec ok", parsed.get("ok") is True and parsed.get("exit_success") is True,
          repr(parsed)[:150])
    parsed2, _ = c.tool_json("pyaissh_exec",
                             {"target": h2["target"], "password": h2["password"], "cmd": "true"})
    check("C3b 第二台主机温热 (%d->%dms)" % (parsed.get("duration_ms", 0),
                                            parsed2.get("duration_ms", 0)),
          parsed2.get("duration_ms", 99999) < parsed.get("duration_ms", 99999), "")

    # ---- C4 ls ----
    parsed, _ = c.tool_json("pyaissh_ls",
                            {"target": h1["target"], "password": h1["password"], "path": "/etc"})
    entries = parsed.get("entries", [])
    check("C4a ls /etc 有条目", parsed.get("ok") is True and parsed.get("count", 0) > 0,
          repr(parsed)[:150])
    check("C4b entries 同构字段", entries and
          set(entries[0].keys()) == {"name", "mode", "size", "is_dir", "is_symlink", "mtime"},
          repr(sorted(entries[0].keys())) if entries else "[]")
    check("C4c ls 复用池连接", any("池命中" in ln for ln in c.stderr_lines[-3:]),
          str(c.stderr_lines[-3:]))

    # ---- C5 上传/下载往返 md5（1MB）----
    src = os.path.join(tmp_local, "rt_1m.bin")
    with open(src, "wb") as f:
        f.write(os.urandom(1024 * 1024))
    src_md5 = md5_file(src)
    parsed, _ = c.tool_json("pyaissh_upload", {
        "target": h1["target"], "password": h1["password"],
        "local": src, "remote": remote_dir + "/rt_1m.bin"})
    check("C5a 上传 ok", parsed.get("ok") is True, repr(parsed)[:200])
    check("C5b 上传零回传（无内容字段）",
          all(k not in parsed for k in ("stdout", "content", "data")), "")
    dst = os.path.join(tmp_local, "rt_1m_down.bin")
    parsed, _ = c.tool_json("pyaissh_download", {
        "target": h1["target"], "password": h1["password"],
        "remote": remote_dir + "/rt_1m.bin", "local": dst})
    check("C5c 下载 ok + md5 一致", parsed.get("ok") is True and md5_file(dst) == src_md5,
          repr(parsed)[:150])

    # ---- C6 并行分片下载（5MB --parallel 4）----
    src5 = os.path.join(tmp_local, "rt_5m.bin")
    with open(src5, "wb") as f:
        f.write(os.urandom(5 * 1024 * 1024))
    src5_md5 = md5_file(src5)
    c.tool_json("pyaissh_upload", {"target": h1["target"], "password": h1["password"],
                                   "local": src5, "remote": remote_dir + "/rt_5m.bin"})
    dst5 = os.path.join(tmp_local, "rt_5m_down.bin")
    parsed, _ = c.tool_json("pyaissh_download", {
        "target": h1["target"], "password": h1["password"],
        "remote": remote_dir + "/rt_5m.bin", "local": dst5, "parallel": 4})
    check("C6 并行下载 md5 一致 + parallel_used=4",
          parsed.get("ok") is True and md5_file(dst5) == src5_md5
          and parsed.get("parallel_used") == 4, repr(parsed)[:200])

    # ---- C7 sudo（root 身份 sudo -n）----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"],
        "sudo": True, "cmd": "id -u"})
    check("C7 --sudo root 提权", parsed.get("exit_success") is True
          and "0" in parsed.get("stdout", ""), repr(parsed)[:150])

    # ---- C8 错误分类：错密码 / 不可达 ----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h2["target"], "password": h2["password"] + "x", "cmd": "true"})
    check("C8a 错密码 auth_failed + retryable=false",
          parsed.get("error") == "auth_failed" and parsed.get("retryable") is False,
          repr(parsed)[:150])
    parsed, _ = c.tool_json("pyaissh_test", {
        "target": "root@192.0.2.1", "timeout": 3})
    check("C8b 不可达 connection_timeout + retryable=true",
          parsed.get("error") == "connection_timeout" and parsed.get("retryable") is True,
          repr(parsed)[:150])

    # ---- C9 大输出截断 + spill 落盘 ----
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"], "cmd": "seq 1 50000"})
    check("C9a 截断标志", parsed.get("stdout_truncated") is True
          and parsed.get("output_truncated") is True, repr({k: parsed.get(k) for k in
          ("stdout_truncated", "output_truncated")}))
    spill = parsed.get("stdout_spill_file")
    if spill and os.path.exists(spill):
        os.remove(spill)
        check("C9b spill 文件落盘且可清理", True, spill)
    else:
        check("C9b spill 文件落盘且可清理", False, repr(spill))

    # ---- C10 cmd_file 本地脚本 ----
    script = os.path.join(tmp_local, "mcp_script.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write("echo line_a\necho line_b\n")
    parsed, _ = c.tool_json("pyaissh_exec", {
        "target": h1["target"], "password": h1["password"], "cmd_file": script})
    check("C10 cmd_file 执行", parsed.get("exit_success") is True
          and "line_a" in parsed.get("stdout", "") and "line_b" in parsed.get("stdout", ""),
          repr(parsed)[:150])

    # ---- C11 管道并发：两个请求连续发出，响应按序且都成功 ----
    c._id += 1
    mid1 = c._id
    c.send({"jsonrpc": "2.0", "id": mid1, "method": "tools/call",
            "params": {"name": "pyaissh_exec", "arguments": {
                "target": h1["target"], "password": h1["password"], "cmd": "echo one"}}})
    c._id += 1
    mid2 = c._id
    c.send({"jsonrpc": "2.0", "id": mid2, "method": "tools/call",
            "params": {"name": "pyaissh_exec", "arguments": {
                "target": h1["target"], "password": h1["password"], "cmd": "echo two"}}})
    r1 = c.recv()
    r2 = c.recv()
    check("C11a 并发响应按序", r1.get("id") == mid1 and r2.get("id") == mid2,
          "%r %r" % (r1.get("id"), r2.get("id")))
    t1 = json.loads(r1["result"]["content"][0]["text"])
    t2 = json.loads(r2["result"]["content"][0]["text"])
    check("C11b 并发结果正确", "one" in t1.get("stdout", "") and "two" in t2.get("stdout", ""),
          repr(t1.get("stdout")) + repr(t2.get("stdout")))

    # ---- C12 优雅退出 ----
    c.close()
    check("C12 优雅退出 rc=0", c.proc.returncode == 0, "rc=%r" % c.proc.returncode)

    # ---- C13 TTL 淘汰 + 透明重连（专用实例 TTL=2s）----
    c2 = McpClient(env={"PYAISSH_MCP_POOL_TTL": "2"})
    c2.initialize()
    parsed, _ = c2.tool_json("pyaissh_exec",
                             {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    check("C13a 首次 ok", parsed.get("ok") is True, "")
    time.sleep(3.5)  # > TTL=2s，janitor 应淘汰
    parsed, _ = c2.tool_json("pyaissh_exec",
                             {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    check("C13b TTL 过期后调用仍成功（透明重连）", parsed.get("ok") is True
          and parsed.get("exit_success") is True, repr(parsed)[:150])
    check("C13c stderr 有 TTL 淘汰记录", any("TTL 淘汰" in ln for ln in c2.stderr_lines),
          str(c2.stderr_lines[-5:]))
    c2.close()

    # ---- C14 池关闭模式：全部冷连接但功能完好 ----
    c3 = McpClient(env={"PYAISSH_MCP_POOL": "0"})
    c3.initialize()
    p1, _ = c3.tool_json("pyaissh_exec",
                         {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    p2, _ = c3.tool_json("pyaissh_exec",
                         {"target": h1["target"], "password": h1["password"], "cmd": "true"})
    check("C14a 池关模式两次 exec ok", p1.get("ok") is True and p2.get("ok") is True, "")
    check("C14b 池关模式无命中记录", not any("池命中" in ln for ln in c3.stderr_lines), "")
    c3.close()

    # ---- C15 test 不进池（连通性测试必须真连，复用旧连接=假测试）----
    c4 = McpClient()
    c4.initialize()
    p1, _ = c4.tool_json("pyaissh_test", {"target": h1["target"], "password": h1["password"]})
    p2, _ = c4.tool_json("pyaissh_test", {"target": h1["target"], "password": h1["password"]})
    check("C15a 两次 test 都 ok", p1.get("ok") is True and p2.get("ok") is True, "")
    check("C15b 第二次 test 仍是冷连接 (d=%sms)" % p2.get("duration_ms"),
          p2.get("duration_ms", 0) > 500, "d2=%s" % p2.get("duration_ms"))
    check("C15c test 无池命中记录", not any("池命中" in ln for ln in c4.stderr_lines),
          str(c4.stderr_lines[-4:]))
    c4.close()

    # ---- 后台作业（准流式）见 test/test_live_bg.py（独立聚焦套件：detach→增量读→wait_rc→cleanup）
    #      单独拆出是为了"改 MCP 层不必每次跑整套真机回归"——本套件保持原有覆盖不变。

    # ---- 清理远端 ----
    try:
        cc = McpClient()
        cc.initialize()
        cc.tool_json("pyaissh_exec", {
            "target": h1["target"], "password": h1["password"],
            "cmd": "rm -rf %s" % remote_dir})
        cc.close()
    except Exception:
        pass

    print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
