# -*- coding: utf-8 -*-
"""pyaissh 单文件构建器（域版）：单文件 <-> domains/ 多域文件往返（护栏 2/3/4）。

用法:
  python build_single.py split   # 单文件(SOURCE)按 MANIFEST_domains 切成 domains/
  python build_single.py join    # domains/ 按 MANIFEST_domains 拼回 pyaissh.built.py
  python build_single.py check   # 往返验证：built vs 原单文件 逐字节一致 + 编译

确定性（护栏 4）：同一 domains/ 两次 join 逐字节相同（MANIFEST 固定顺序、无时间戳）。
换行（护栏 3 实测）：原文件 CRLF——split/join 一律 newline="" 保原样。
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")


def shutil_copy(src, dst):
    import shutil
    shutil.copyfile(src, dst)

ROOT = os.path.dirname(os.path.abspath(__file__))
MAIN_REPO = os.path.dirname(ROOT)
SOURCE = os.path.join(MAIN_REPO, "pyaissh.py")      # 现行成品单文件（split 的源 / check 的对标）
DOMAINS_DIR = os.path.join(ROOT, "domains")
OUTPUT = os.path.join(ROOT, "pyaissh.built.py")
MANIFEST = os.path.join(ROOT, "MANIFEST_domains.txt")


def load_manifest():
    items = []
    for line in open(MANIFEST, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fname, _, anchor = line.partition("|")
        items.append((fname.strip(), anchor.strip()))
    return items


def _split_text(text, items):
    """按锚把文本切成 len(items) 块（每锚=块起点；块0从文件头；末块到 EOF）。"""
    positions = []
    search_from = 0
    for name, anchor in items:
        m = re.search(anchor, text[search_from:], re.M)
        if not m:
            print("SPLIT_FAIL: 锚未找到 %s | %s" % (name, anchor))
            sys.exit(1)
        positions.append(search_from + m.start())
        search_from = positions[-1] + 1
    bounds = [0] + positions[1:] + [len(text)]
    return [text[bounds[i]:bounds[i + 1]] for i in range(len(items))]


def split():
    text = open(SOURCE, encoding="utf-8", newline="").read()
    items = load_manifest()
    os.makedirs(DOMAINS_DIR, exist_ok=True)
    for f in os.listdir(DOMAINS_DIR):
        os.remove(os.path.join(DOMAINS_DIR, f))
    chunks = _split_text(text, items)
    for (fname, _), chunk in zip(items, chunks):
        with open(os.path.join(DOMAINS_DIR, fname), "w", encoding="utf-8", newline="") as f:
            f.write(chunk)
    print("SPLIT_OK: %d 域文件 -> %s" % (len(items), DOMAINS_DIR))


def _title_of(text):
    """从域文件头部 docstring 首行提取一句话标题（横幅用）。"""
    m = re.search(r'^"""([^。—\n]{2,30})', text, re.M)
    title = m.group(1).strip() if m else (text.splitlines()[0][:24] if text.strip() else "")
    title = re.sub(r"（域 \d+）", "", title).strip()
    return title


def join():
    items = load_manifest()
    total = len(items)
    newline = "\r\n"
    parts = []
    for i, (fname, _) in enumerate(items):
        p = os.path.join(DOMAINS_DIR, fname)
        if not os.path.exists(p):
            print("JOIN_FAIL: 缺域文件 %s" % p)
            sys.exit(1)
        content = open(p, encoding="utf-8", newline="").read()
        if i > 0:
            # 域边界横幅：仅第 0 域（文件头）不插——拼接后单文件也分域可读
            title = _title_of(content)
            num = fname.split("_")[0]
            banner = ("# ================= [域 %s/%d] %s "
                      "================= %s" % (num, total, title, newline))
            parts.append(newline + banner)
        parts.append(content)
    out = "".join(parts)
    with open(OUTPUT, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    print("JOIN_OK: %s (%d bytes, %d 域横幅)"
          % (OUTPUT, len(out.encode("utf-8")), total - 1))


def _sha256(p):
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def check():
    import subprocess
    if not os.path.exists(OUTPUT):
        join()
    a = _sha256(SOURCE)
    b = _sha256(OUTPUT)
    ok = a == b
    print("金标 diff: %s" % ("逐字节一致 OK" if ok else "不一致! 原 %s vs built %s" % (a, b)))
    rc = subprocess.call([sys.executable, "-m", "py_compile", OUTPUT])
    print("built 编译: %s" % ("OK" if rc == 0 else "FAIL rc=%d" % rc))
    return 0 if ok and rc == 0 else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "join"
    if cmd == "split":
        split()
    elif cmd == "join":
        join()
    elif cmd == "check":
        sys.exit(check())
    elif cmd == "dist":
        # 构建 + 双份同步（根 pyaissh.py + skills/pyaissh/pyaissh.py，md5 一致）
        join()
        import hashlib
        targets = [os.path.join(MAIN_REPO, "pyaissh.py"),
                   os.path.join(MAIN_REPO, "skills", "pyaissh", "pyaissh.py")]
        digests = set()
        for t in targets:
            if not os.path.exists(os.path.dirname(t)):
                print("DIST_FAIL: 目标目录不存在 %s" % os.path.dirname(t))
                sys.exit(1)
            shutil_copy(OUTPUT, t)
            digests.add(hashlib.md5(open(t, "rb").read()).hexdigest())
        ok = len(digests) == 1
        print("DIST_OK: 双份同步 %s (%s)" % ("md5 一致 " + next(iter(digests)) if ok else "md5 不一致!", "OK" if ok else "FAIL"))
        sys.exit(0 if ok else 1)
    else:
        print("用法: build_single.py split|join|check|dist")
        sys.exit(1)
