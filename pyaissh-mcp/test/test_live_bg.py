# -*- coding: utf-8 -*-
"""真机聚焦套件：后台作业（准流式）——detach → 增量读 → wait_rc → cleanup → job_not_found。

为什么单独一个文件：改 MCP 层（后台作业工具）时只需跑这一套（约 15 秒、1 台主机、
2 次认证内），不必每次重跑 test_live.py 的整套 26 例（含 1MB/5MB 传输）。

跑法：python test/test_live_bg.py
凭据：test/local_creds.json（与 test_live.py 相同，gitignored）

安全：开跑前先做**单次 test 探活**（凭据错/被墙就 SKIP，不产生连续失败认证）。
会在远端 /tmp/pyaissh-jobs 下建作业目录，结束时 --cleanup 清理。
"""
import io
import json
import os
import sys
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


def load_creds():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_creds.json")
    if not os.path.isfile(path):
        print("SKIP 未找到 test/local_creds.json（参考 local_creds.json.example）")
        sys.exit(0)
    d = json.load(io.open(path, encoding="utf-8"))
    return d["host1"]


def main():
    h1 = load_creds()
    auth = {"target": h1["target"], "password": h1["password"]}

    # ---- 前置探活：单次 test，失败即 SKIP（避免凭据错误触发连续失败认证）----
    c0 = McpClient()
    c0.initialize()
    pre, _ = c0.tool_json("pyaissh_test", auth)
    c0.close()
    if pre.get("ok") is not True:
        print("SKIP 前置探活失败（error=%s）——不跑后续用例" % pre.get("error"))
        return 0
    print("前置探活 OK（%s hostname=%s）" % (h1["target"], pre.get("hostname")))

    c = McpClient()
    c.initialize()
    parsed, _ = c.tool_json("pyaissh_exec", dict(
        auth, cmd="for i in 1 2 3 4 5 6; do echo stream$i; sleep 1; done", detach=True))
    jid = parsed.get("job_id")
    check("B1 detach 启动返回 job_id/log/rc/status",
          parsed.get("ok") is True and parsed.get("detached") is True and bool(jid)
          and str(parsed.get("log", "")).endswith("/job.log")
          and str(parsed.get("rc", "")).endswith("/job.rc"), repr(parsed)[:200])

    if not jid:
        c.close()
        print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
        return 1

    # 立即增量读：作业仍在跑 → 部分输出 + next_offset
    p1, _ = c.tool_json("pyaissh_log", dict(auth, job_id=jid, offset=0))
    off1 = p1.get("next_offset", 0)
    check("B2 offset=0 增量读（running + next_offset>0 + 有输出）",
          p1.get("ok") is True and p1.get("status") == "running" and off1 > 0
          and "stream1" in p1.get("stdout", "") and p1.get("stream") == "stdout+stderr"
          and "content" not in p1, repr(p1)[:200])

    time.sleep(3)

    # 续读：只应拿到新片段（拼接后 stream1 只出现一次）
    p2, _ = c.tool_json("pyaissh_log", dict(auth, job_id=jid, offset=off1))
    joined = p1.get("stdout", "") + p2.get("stdout", "")
    check("B3 续读只给新片段（不重复）",
          p2.get("ok") is True and p2.get("next_offset", 0) > off1
          and joined.count("stream1") == 1, "off1=%s p2=%r" % (off1, repr(p2)[:160]))

    # 尾部行数模式
    p3, _ = c.tool_json("pyaissh_log", dict(auth, job_id=jid, lines=3))
    check("B4 lines=3 尾部行模式", p3.get("ok") is True
          and len([l for l in p3.get("stdout", "").splitlines() if l.strip()]) <= 3,
          repr(p3)[:160])

    # 等结束拿退出码（wait_rc=True 是 LLM 最自然的写法：MCP 层按上限 45s 强制，
    # 并在 warnings 里回报容错——v0.2.2 修）
    p4, _ = c.tool_json("pyaissh_log", dict(auth, job_id=jid, wait_rc=True))
    check("B5 wait_rc=True 被强制为上限并等结束拿 exit_code=0",
          p4.get("ok") is True and p4.get("status") == "finished"
          and p4.get("exit_code") == 0, repr(p4)[:180])
    check("B5b 容错回报可见（warnings 含 MCP 参数容错）",
          any("MCP 参数容错" in w for w in (p4.get("warnings") or [])),
          repr(p4.get("warnings"))[:160])
    check("B6 log 复用池连接（stderr 有池命中）",
          any("池命中" in ln for ln in c.stderr_lines), str(c.stderr_lines[-4:]))

    # list 能看到该作业
    pl, _ = c.tool_json("pyaissh_log", dict(auth, list=True))
    ids = [j.get("job_id") for j in pl.get("jobs", [])]
    check("B7 list 含该作业且状态 finished",
          pl.get("ok") is True and jid in ids
          and any(j.get("job_id") == jid and j.get("status") == "finished"
                  and j.get("exit_code") == 0 for j in pl.get("jobs", [])), repr(pl)[:200])

    # 清理 + 清理后不可读
    p5, _ = c.tool_json("pyaissh_log", dict(auth, job_id=jid, cleanup=True))
    check("B8 cleanup 清理远端作业目录", p5.get("ok") is True and p5.get("cleaned") is True,
          repr(p5)[:160])
    p6, r6 = c.tool_json("pyaissh_log", dict(auth, job_id=jid))
    check("B9 清理后 job_not_found + isError",
          p6.get("error") == "job_not_found" and r6.get("isError") is True, repr(p6)[:160])

    # ---- kill：长作业整组停掉 → 状态收敛 dead（v2.2.1）----
    pk, _ = c.tool_json("pyaissh_exec", dict(auth, cmd="sleep 120", detach=True))
    kjob = pk.get("job_id")
    if kjob:
        p7, _ = c.tool_json("pyaissh_log", dict(auth, job_id=kjob, kill=True, wait_rc=30))
        check("B10 kill 整组停掉并收敛 dead",
              p7.get("ok") is True and p7.get("status") == "dead"
              and p7.get("exit_code") is None and p7.get("kill", {}).get("ok") is True
              and bool(p7.get("pid")), repr(p7)[:200])
        check("B11 dead 带 hint 且 wait_rc 快速收敛",
              "job.rc" in (p7.get("hint") or "") and p7.get("waited_ms", 99999) < 20000,
              repr(p7)[:180])
        c.tool_json("pyaissh_log", dict(auth, job_id=kjob, cleanup=True))
    else:
        check("B10 kill 整组停掉并收敛 dead", False, "detach 未返回 job_id")

    # ---- 清理守卫（v0.2.3）：运行中 cleanup 被拒；kill+cleanup 一步到位 ----
    pg, _ = c.tool_json("pyaissh_exec", dict(auth, cmd="sleep 90", detach=True))
    gjob = pg.get("job_id")
    if gjob:
        p8, _ = c.tool_json("pyaissh_log", dict(auth, job_id=gjob, cleanup=True))
        check("B12 运行中 cleanup 被拒（job_running + 目录保留）",
              p8.get("error") == "job_running" and bool(p8.get("pid")), repr(p8)[:200])
        p9, _ = c.tool_json("pyaissh_log", dict(auth, job_id=gjob, kill=True, cleanup=True))
        check("B13 kill+cleanup 一步到位（dead + 已清理）",
              p9.get("ok") is True and p9.get("status") == "dead"
              and p9.get("cleaned") is True, repr(p9)[:200])
        po, _ = c.tool_json("pyaissh_exec", dict(
            auth, cmd="pgrep -af '[s]leep 90' >/dev/null && echo ORPHAN || echo CLEAN"))
        check("B14 kill 整组无孤儿", "CLEAN" in (po.get("stdout") or ""),
              repr(po.get("stdout"))[:120])
    else:
        check("B12 运行中 cleanup 被拒（job_running + 目录保留）", False, "detach 未返回 job_id")

    c.close()
    print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
