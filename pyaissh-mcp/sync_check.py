#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本同步检查：pyaissh-mcp/pyaissh.py 是否与 skills/pyaissh/pyaissh.py 一致。

背景：MCP 层以【固定副本】的 pyaissh.py 为契约源（发布时 pin 到 CLI 的
某个版本）。CLI 升级后应运行本脚本检查；确认升级后：
    python sync_check.py --update   # 用新版覆盖副本
然后跑 test/test_offline.py + test/test_live.py 全量回归。

退出码：0 = 一致；1 = 不一致（--update 可自动同步）。
"""
import hashlib
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
COPY = os.path.join(BASE, "pyaissh.py")
SOURCE_CANDIDATES = [
    os.path.join(os.path.dirname(BASE), "skills", "pyaissh", "pyaissh.py"),
]


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def main():
    update = "--update" in sys.argv
    src = next((p for p in SOURCE_CANDIDATES if os.path.exists(p)), None)
    if src is None:
        print("未找到 skills/pyaissh/pyaissh.py（MCP 目录须位于 pyssh 仓库内）")
        return 1
    if not os.path.exists(COPY):
        print("副本缺失: %s" % COPY)
        if update:
            shutil.copy2(src, COPY)
            print("已从源复制: %s" % src)
            return 0
        return 1
    if md5(COPY) == md5(src):
        print("一致: pyaissh-mcp/pyaissh.py == skills/pyaissh/pyaissh.py (md5 %s)" % md5(src))
        return 0
    print("不一致！副本 md5=%s, 源 md5=%s (源: %s)" % (md5(COPY), md5(src), src))
    if update:
        shutil.copy2(src, COPY)
        print("已用新版覆盖副本——请跑 test/test_offline.py + test/test_live.py 全量回归")
        return 0
    print("升级方式: python sync_check.py --update，然后全量回归")
    return 1


if __name__ == "__main__":
    sys.exit(main())
