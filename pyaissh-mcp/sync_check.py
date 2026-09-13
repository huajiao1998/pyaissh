#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能包副本同步检查：pyaissh-mcp/ 内的 pyaissh 完整副本是否与 skills/pyaissh/ 一致。

背景：MCP 层以【固定副本】为契约源（发布时 pin 到 CLI 的某个版本）。为了让
pyaissh-mcp/ 成为一个**自包含的分发包**（适配器 + 完整 pyaissh + 技能文档：
拿出去单独放就能跑、也能当技能包用），它内含 skills/pyaissh/ 的完整副本——
单一源原则：只改 skills/pyaissh/，改完用本脚本同步 + 校验漂移。

    python sync_check.py            # 检查（不一致则退出码 1）
    python sync_check.py --update   # 用 skills/pyaissh/ 覆盖副本

同步清单（源 → 副本）：
    pyaissh.py        → pyaissh.py        （CLI 主体，进程内被 pyaissh_mcp.py 调用）
    pyaissh           → pyaissh           （POSIX 入口）
    pyaissh.cmd       → pyaissh.cmd       （Windows 入口）
    SKILL.md          → SKILL.md          （技能入口）
    CHANGELOG.md      → CLI_CHANGELOG.md  （CLI 变更记录；改名避免与适配器 CHANGELOG 冲突）
    docs/*.md         → docs/*.md         （技能文档七篇）
不同步：.env.example（适配器有自己的凭据模板）、test/（测试不出包）。

退出码：0 = 全部一致；1 = 有漂移或源缺失。
"""
import hashlib
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(os.path.dirname(BASE), "skills", "pyaissh")

# (源相对 skills/pyaissh 的路径, 副本相对 pyaissh-mcp 的路径)
PAIRS = [
    ("pyaissh.py", "pyaissh.py"),
    ("pyaissh", "pyaissh"),
    ("pyaissh.cmd", "pyaissh.cmd"),
    ("SKILL.md", "SKILL.md"),
    ("CHANGELOG.md", "CLI_CHANGELOG.md"),
]
DOCS = ("contract.md", "edge-cases.md", "errors.md", "exec.md", "jump.md",
        "setup.md", "transfer.md")
PAIRS += [(os.path.join("docs", d), os.path.join("docs", d)) for d in DOCS]


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def main():
    update = "--update" in sys.argv
    if not os.path.isdir(SKILL):
        print("未找到 skills/pyaissh/（pyaissh-mcp 目录须位于 pyssh 仓库内）")
        return 1

    drift, copied, missing_src = [], [], []
    for src_rel, dst_rel in PAIRS:
        src = os.path.join(SKILL, src_rel)
        dst = os.path.join(BASE, dst_rel)
        if not os.path.exists(src):
            missing_src.append(src_rel)
            continue
        if not os.path.exists(dst) or md5(src) != md5(dst):
            if update:
                d = os.path.dirname(dst)
                if d and not os.path.isdir(d):
                    os.makedirs(d)
                shutil.copy2(src, dst)
                copied.append(dst_rel)
            else:
                drift.append(dst_rel)

    if update:
        if copied:
            print("已同步 %d 个文件: %s" % (len(copied), ", ".join(sorted(copied))))
            print("提示：副本变更后请跑 test/test_offline.py + test/test_live*.py 全量回归")
        else:
            print("已是最新，无需同步（%d 个文件逐一 md5 一致）" % len(PAIRS))
        return 1 if missing_src else 0

    if missing_src:
        print("源缺失: %s" % ", ".join(missing_src))
        return 1
    if drift:
        print("不一致（副本未跟上 skills/pyaissh/）: %s" % ", ".join(sorted(drift)))
        print("升级方式: python sync_check.py --update，然后全量回归")
        return 1
    print("一致: pyaissh-mcp/ 内 %d 个副本文件与 skills/pyaissh/ 逐一 md5 相同"
          "（pyaissh.py md5 %s）" % (len(PAIRS), md5(os.path.join(SKILL, "pyaissh.py"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
