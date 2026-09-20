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


def split(force=False):
    """按锚切分单文件（**注意语义**：锚是域内标识、不是边界，见 MANIFEST 头部）。

    安全设计（2026-09-16）：
      - 先算后写：切分/校验全部完成前**绝不碰 domains/**（旧版先 os.remove 清空目录，
        锚一旦过期就是"域文件被删空 + 退出 1"）
      - 与现有域文件逐块比对：不一致（锚是域内标识，通常如此）→ **拒绝覆盖 domains/**，
        改写到 domains.split/ 供人工比对；只有逐块一致（真正的往返）才写回 domains/
    """
    text = open(SOURCE, encoding="utf-8", newline="").read()
    items = load_manifest()
    chunks = _split_text(text, items)          # 失败在这里退出，磁盘未被触碰

    mismatch = []
    for (fname, _), chunk in zip(items, chunks):
        p = os.path.join(DOMAINS_DIR, fname)
        cur = open(p, encoding="utf-8", newline="").read() if os.path.exists(p) else None
        if cur != chunk:
            mismatch.append((fname, len(chunk), len(cur) if cur is not None else -1))

    if mismatch and not force:
        print("SPLIT_REFUSED: 切分结果与现有域文件不一致（%d/%d 域）——锚是【域内标识】而非"
              "域边界（见 MANIFEST 头部），按锚切分无法还原域文件。" % (len(mismatch), len(items)))
        for fname, got, cur in mismatch:
            print("    %-20s 切分 %6d 字节 vs 现有 %6d 字节" % (fname, got, cur))
        print("  · join/dist 不受影响（不使用锚）；要重建域文件请人工处理或改 MANIFEST 设计")
        print("  · 确要看切分产物：build_single.py split --force（写入 domains.split/，"
              "不覆盖 domains/）")
        return 1

    out_dir = DOMAINS_DIR if not mismatch else os.path.join(ROOT, "domains.split")
    os.makedirs(out_dir, exist_ok=True)
    for (fname, _), chunk in zip(items, chunks):
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8", newline="") as f:
            f.write(chunk)
    print("SPLIT_OK: %d 域文件 -> %s%s" % (len(items), out_dir,
                                          "（逐块一致，真往返）" if not mismatch
                                          else "（与 domains/ 不一致，仅作比对，未覆盖源）"))
    return 0


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
        sys.exit(split(force="--force" in sys.argv))
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
