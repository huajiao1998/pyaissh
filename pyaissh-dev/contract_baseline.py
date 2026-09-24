#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约基线抓取：跑一遍 session 生命周期，把**每个子命令/错误路径返回的键集合**存成 fixture。

用途：INV-01「字段集不变」的可执行判据——tmux 引擎迁移后，用同一脚本重抓并逐键比对。
判据（INV-01a/b）：
  * 基线里的键**一个都不能少**（空值字段也必须继续返回）→ 缺键即 FAIL；
  * 多出来的键只有在 `--allow` 白名单里才算合法 → 白名单外的多键即 FAIL。

凭据只从环境变量读（PYAISSH_TEST_HOST / PYAISSH_TEST_PASSWORD），不落盘。
用法：
    python pyaissh-dev/contract_baseline.py --out tests/contract/session_contract_v2.json
    python pyaissh-dev/contract_baseline.py --check tests/contract/session_contract_v2.json
    python pyaissh-dev/contract_baseline.py --check tests/contract/session_contract_v2.json \
        --allow orphans,orphans_total,orphan_remaining_total
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "pyaissh.py")


def _run(args, timeout=120):
    env = dict(os.environ)
    env["PYAISSH_PASSWORD"] = os.environ.get("PYAISSH_TEST_PASSWORD", "")
    p = subprocess.run([sys.executable, "-B", BIN] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=timeout)
    j = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            c = json.loads(line)
            if isinstance(c, dict) and "ok" in c:
                j = c
                break
        except ValueError:
            continue
    return j if j is not None else {"_no_json": True, "_raw": (p.stdout or "")[-200:]}


def capture(tgt, name):
    """返回 {case: 键集合排序列表}。会话名带随即后缀，避免与并行测试撞车。"""
    cases = {}

    def rec(case, args, timeout=120):
        j = _run(args, timeout=timeout)
        cases[case] = sorted(j.keys())
        return j

    _run(["session", "kill", tgt, "--all"])
    rec("kill_all_empty", ["session", "kill", tgt, "--all"])
    rec("start_created", ["session", "start", tgt, "--name", name, "--ttl", "120"])
    rec("start_exists_error", ["session", "start", tgt, "--name", name, "--ttl", "120"])
    rec("start_attach", ["session", "start", tgt, "--name", name, "--ttl", "120", "--attach"])
    rec("send", ["session", "send", tgt, "--name", name, "--cmd", "echo BASELINE_OK"])
    time.sleep(1)
    rec("read_lines", ["session", "read", tgt, "--name", name, "--lines", "5"])
    rec("read_offset", ["session", "read", tgt, "--name", name, "--offset", "0"])
    rec("read_waitrc", ["session", "read", tgt, "--name", name, "--wait-rc", "5"])
    rec("run", ["session", "run", tgt, "--name", name, "--cmd", "echo RUN_OK", "--wait-rc", "15"])
    rec("run_nowait", ["session", "run", tgt, "--name", name, "--cmd", "echo NW", "--no-wait"])
    time.sleep(0.5)
    rec("ctrl_c", ["session", "ctrl-c", tgt, "--name", name])
    rec("keys", ["session", "keys", tgt, "--name", name, "--data", "\n"])
    rec("list", ["session", "list", tgt])
    rec("not_found", ["session", "read", tgt, "--name", name + "nope"])
    rec("bad_args_send", ["session", "send", tgt, "--name", name])
    rec("bad_args_name", ["session", "start", tgt, "--name", "../evil"])
    rec("ctrl_c_force", ["session", "ctrl-c", tgt, "--name", name, "--force"])
    # 会话死掉：杀掉会话 shell（旧引擎是 bash.pid，tmux 引擎换 pane_pid → 用 send exit 更通用）
    rec("send_exit", ["session", "send", tgt, "--name", name, "--cmd", "exit"])
    time.sleep(2)
    rec("dead_or_missing", ["session", "read", tgt, "--name", name])
    rec("kill", ["session", "kill", tgt, "--name", name])
    rec("kill_again", ["session", "kill", tgt, "--name", name])
    _run(["session", "kill", tgt, "--all"])
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="写入 fixture 路径")
    ap.add_argument("--check", help="与 fixture 比对（迁移后验收用）")
    ap.add_argument("--allow", default="",
                    help="允许新增的键（逗号分隔白名单；缺键永远 FAIL）")
    ap.add_argument("--name", default="cbase")
    a = ap.parse_args()
    tgt = os.environ.get("PYAISSH_TEST_HOST")
    if not tgt or not os.environ.get("PYAISSH_TEST_PASSWORD"):
        print("需要 PYAISSH_TEST_HOST / PYAISSH_TEST_PASSWORD")
        return 2
    got = capture(tgt, a.name)

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(got, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print("已写入 %s（%d 个用例）" % (a.out, len(got)))
        for k in sorted(got):
            print("  %-20s %s" % (k, " ".join(got[k])))
        return 0

    if a.check:
        with open(a.check, encoding="utf-8") as f:
            want = json.load(f)
        allow = set(x.strip() for x in a.allow.split(",") if x.strip())
        bad = 0
        for case in sorted(set(want) | set(got)):
            w, g = set(want.get(case, [])), set(got.get(case, []))
            missing, added = sorted(w - g), sorted(g - w)
            illegal = [k for k in added if k not in allow]
            if missing or illegal:
                bad += 1
                print("FAIL %-20s 缺=%s 非法新增=%s%s" % (
                    case, missing, illegal,
                    " 白名单新增=%s" % sorted(set(added) - set(illegal)) if len(added) > len(illegal) else ""))
            else:
                print("PASS %-20s (%d 键%s)" % (
                    case, len(g),
                    "，白名单 +%s" % added if added else ""))
        print("\n契约字段集：%s%s" % (
            "全部一致" if not bad else "%d 个用例不一致" % bad,
            "（白名单：%s）" % sorted(allow) if allow else ""))
        return 1 if bad else 0
    print("需要 --out 或 --check")
    return 2


if __name__ == "__main__":
    sys.exit(main())
