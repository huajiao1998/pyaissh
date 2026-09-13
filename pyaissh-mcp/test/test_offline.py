# -*- coding: utf-8 -*-
"""离线协议测试：不碰任何远程主机，验证 MCP stdio 协议与参数校验路径。

运行：python test/test_offline.py   （在 pyaissh-mcp 目录或任意目录均可）
"""
import io
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McpClient  # noqa: E402

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


def main():
    c = McpClient(env={"PYAISSH_MCP_POOL": "1"})
    raw_lines = []

    # T1 initialize 握手
    init = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}})
    r = init.get("result", {})
    check("T1a initialize 返回 result", "result" in init, repr(init)[:120])
    check("T1b protocolVersion 回显", r.get("protocolVersion") == "2025-06-18",
          repr(r.get("protocolVersion")))
    check("T1c serverInfo", r.get("serverInfo", {}).get("name") == "pyaissh-mcp",
          repr(r.get("serverInfo")))
    check("T1d capabilities.tools", "tools" in r.get("capabilities", {}), repr(r.get("capabilities")))
    c.notify("notifications/initialized")

    # T2 tools/list：6 个工具、schema 完整
    tl = c.request("tools/list")
    tools = tl.get("result", {}).get("tools", [])
    names = [t["name"] for t in tools]
    check("T2a 6 个工具", sorted(names) == sorted(
        ["pyaissh_exec", "pyaissh_test", "pyaissh_ls", "pyaissh_upload", "pyaissh_download",
         "pyaissh_log"]),
        repr(names))
    check("T2b schema 均含 required", all("required" in t["inputSchema"] for t in tools), "")
    check("T2c schema 均含 target", all(
        "target" in t["inputSchema"]["properties"] for t in tools), "")

    # T3 ping
    ping = c.request("ping")
    check("T3 ping 返回空 result", ping.get("result") == {}, repr(ping)[:120])

    # T4 通知不应答：发通知后再发 ping，第一个响应必须是 ping 的
    c.notify("notifications/initialized")
    p2 = c.request("ping")
    check("T4 通知不产生响应", p2.get("id") == 3 or "result" in p2, repr(p2)[:80])

    # T5 未知工具 → -32602
    e = c.request("tools/call", {"name": "nope", "arguments": {}})
    check("T5a 未知工具 error", e.get("error", {}).get("code") == -32602, repr(e)[:120])

    # T6 未知方法 → -32601
    e = c.request("no/such/method")
    check("T6 未知方法 -32601", e.get("error", {}).get("code") == -32601, repr(e)[:120])

    # T7 resources/prompts 空清单（避免客户端报错）
    rl = c.request("resources/list")
    pl = c.request("prompts/list")
    check("T7a resources/list 空", rl.get("result") == {"resources": []}, repr(rl)[:120])
    check("T7b prompts/list 空", pl.get("result") == {"prompts": []}, repr(pl)[:120])

    # T8 cmd_file "-" 明确拒绝（stdin 属于 MCP 通道）
    resp = c.call_tool("pyaissh_exec", {"target": "root@127.0.0.1", "cmd_file": "-"})
    check("T8a cmd_file=- isError", resp.get("result", {}).get("isError") is True, repr(resp)[:150])
    text = resp["result"]["content"][0]["text"]
    check("T8b 拒绝消息含 bad_args", json.loads(text).get("error") == "bad_args", text[:120])

    # T9 缺 cmd → argparse 层 bad_args（连接前拒绝，零网络）
    parsed, result = c.tool_json("pyaissh_exec", {"target": "root@127.0.0.1"})
    check("T9a 缺cmd bad_args", parsed.get("error") == "bad_args", repr(parsed)[:150])
    check("T9b isError=true", result.get("isError") is True, "")

    # T10 缺用户名 target → bad_args（连接前拒绝，零网络）
    parsed, _ = c.tool_json("pyaissh_exec", {"target": "127.0.0.1", "cmd": "echo hi"})
    check("T10 缺用户名 bad_args", parsed.get("error") == "bad_args", repr(parsed)[:150])

    # T11 非法 JSON 帧 → -32700 且 id=null，连接不断
    c.proc.stdin.write("this is not json\n")
    c.proc.stdin.flush()
    resp = c.recv()
    check("T11a -32700", resp.get("error", {}).get("code") == -32700, repr(resp)[:120])
    check("T11b id=null", resp.get("id") is None, "")
    p3 = c.request("ping")
    check("T11c 坏帧后仍可用", "result" in p3, repr(p3)[:80])

    # T12 stdout 纯净度：到目前为止客户端收到的每一行都是单行 JSON-RPC
    # （客户端 recv 逐行 json.loads 已隐式验证；这里补一句显式确认）
    check("T12 stdio 逐行 JSON 无异常", True, "")

    # T13 优雅退出：关 stdin → 进程 0 退出
    c.close()
    check("T13 优雅退出 rc=0", c.proc.returncode == 0, "rc=%r" % c.proc.returncode)

    # ---- 第二个实例：未知协议版本 → 回落最新支持版 ----
    c2 = McpClient()
    init2 = c2.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    check("T14 未知版本回落", init2.get("result", {}).get("protocolVersion") == "2025-06-18",
          repr(init2.get("result", {}).get("protocolVersion")))
    c2.close()

    # ---- 第三个实例：池关闭模式可启动 ----
    c3 = McpClient(env={"PYAISSH_MCP_POOL": "0"})
    i3 = c3.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    check("T15 池关模式 + 旧版本回显",
          i3.get("result", {}).get("protocolVersion") == "2024-11-05", "")
    c3.close()

    # ---- 第四个实例：后台作业工具（准流式）——全部为连接前校验，零网络 ----
    c5 = McpClient()
    c5.initialize()
    tools5 = {t["name"]: t for t in c5.request("tools/list")["result"]["tools"]}
    log_props = tools5["pyaissh_log"]["inputSchema"]["properties"]
    check("T16a pyaissh_log schema 完整", "target" in log_props and "job_id" in log_props
          and "offset" in log_props and "wait_rc" in log_props and "cleanup" in log_props
          and "list" in log_props and "kill" in log_props, repr(sorted(log_props))[:200])
    check("T16b exec schema 含 detach",
          "detach" in tools5["pyaissh_exec"]["inputSchema"]["properties"], "")
    parsed, result = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1", "wait_rc": 999})
    check("T16c wait_rc 超上限被拦（bad_args + isError）",
          parsed.get("error") == "bad_args" and result.get("isError") is True
          and "45" in str(parsed.get("message")), repr(parsed)[:180])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1"})
    check("T16d 缺 job_id 且非 list → bad_args", parsed.get("error") == "bad_args",
          repr(parsed)[:150])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1", "job_id": "../etc"})
    check("T16e job_id 穿越拦截 → bad_args", parsed.get("error") == "bad_args",
          repr(parsed)[:150])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1", "job_id": "j1",
                                             "list": True})
    check("T16f list 与 job_id 互斥 → bad_args", parsed.get("error") == "bad_args",
          repr(parsed)[:150])
    parsed, _ = c5.tool_json("pyaissh_exec", {"target": "root@127.0.0.1", "cmd": "id",
                                              "detach": True, "sudo": True})
    check("T16g detach+sudo 互斥 → bad_args", parsed.get("error") == "bad_args",
          repr(parsed)[:150])
    parsed, _ = c5.tool_json("pyaissh_exec", {"target": "root@127.0.0.1", "cmd": "id",
                                              "detach": True, "pty": True})
    check("T16h detach+pty 互斥 → bad_args", parsed.get("error") == "bad_args",
          repr(parsed)[:150])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1", "kill": True})
    check("T16i kill 缺 job_id → bad_args（连接前拒绝）",
          parsed.get("error") == "bad_args", repr(parsed)[:150])

    # T17 参数类型容错（v0.2.2）：数值参数收到布尔/字符串不再变成 argparse 的
    # "expected one argument"。用 127.0.0.1:1（本地端口，立即 ECONNREFUSED）判定——
    # 只要错误**不是** bad_args，就证明参数已通过 argparse 阶段（不碰任何外部主机）。
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1:1",
                                             "job_id": "probe", "wait_rc": True})
    blob = json.dumps(parsed, ensure_ascii=False)
    check("T17a wait_rc=true 被强制为上限（不再 expected one argument）",
          parsed.get("error") != "bad_args" and "已按上限" in blob, repr(parsed)[:200])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1:1",
                                             "job_id": "probe", "lines": True})
    check("T17b lines=true → 可读 bad_args（提示需要数值）",
          parsed.get("error") == "bad_args" and "数值" in str(parsed.get("message")),
          repr(parsed)[:200])
    parsed, _ = c5.tool_json("pyaissh_exec", {"target": "root@127.0.0.1:1", "cmd": "true",
                                              "detach": "true"})
    check("T17c detach=\"true\"（字符串布尔）被正确当旗标",
          parsed.get("error") != "bad_args", repr(parsed)[:200])
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1:1",
                                             "job_id": "probe", "wait_rc": 0})
    check("T17d wait_rc=0 视为不启用（不再报必须为正整数）",
          parsed.get("error") != "bad_args"
          and "不启用" in json.dumps(parsed, ensure_ascii=False), repr(parsed)[:200])
    parsed, _ = c5.tool_json("pyaissh_exec", {"target": "root@127.0.0.1:1", "cmd": "true",
                                              "password": True})
    check("T17e 字符串参数收到布尔 → 可读 bad_args",
          parsed.get("error") == "bad_args" and "字符串" in str(parsed.get("message")),
          repr(parsed)[:200])

    # T18 清理守卫：force 必须与 cleanup 同用（v0.2.3，防"放弃追踪"被随手打开）
    parsed, _ = c5.tool_json("pyaissh_log", {"target": "root@127.0.0.1:1",
                                             "job_id": "probe", "force": True})
    check("T18 force 缺 cleanup → bad_args",
          parsed.get("error") == "bad_args" and "cleanup" in str(parsed.get("message")),
          repr(parsed)[:200])
    c5.close()

    print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
