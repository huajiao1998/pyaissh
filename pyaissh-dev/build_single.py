# -*- coding: utf-8 -*-
"""pyaissh 单文件构建器（域版）：domains/ 多域文件 -> 成品单文件（护栏 2/3/4）。

用法:
  python build_single.py join    # domains/ 按 MANIFEST_domains 拼回 pyaissh.built.py
  python build_single.py check   # 金标对比：built vs 原单文件 逐字节一致 + 编译
  python build_single.py dist    # join + 双份同步（根 pyaissh.py + skills/pyaissh/pyaissh.py）

方向是**单向的**：域文件是源，成品单文件是产物。改代码请改 domains/，然后 dist；
直接改 pyaissh.py 会在下次 dist 被覆盖（check 会告诉你两边是否一致）。

确定性（护栏 4）：同一 domains/ 两次 join 逐字节相同（MANIFEST 固定顺序、无时间戳）。
换行（护栏 3 实测）：域文件与成品一律 CRLF——join 用 newline="" 保原样。

历史：曾有的 `split`（单文件 -> 域文件，按 MANIFEST 锚切分）已于 2026-09-16 **移除**：
锚是域内标识而非域边界（域文件真正起点是各自首行，单文件里的 `# ===== [域 NN/12] …`
横幅由 join 生成），它本就无法还原域文件；且旧实现会先清空 domains/ 再切分，锚一过期
就是"域文件被删空"。成品不需要再切回域文件，故连同锚一并删除（需要时见 git 历史）。
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
SOURCE = os.path.join(MAIN_REPO, "pyaissh.py")      # 现行成品单文件（check 的对标）
DOMAINS_DIR = os.path.join(ROOT, "domains")
OUTPUT = os.path.join(ROOT, "pyaissh.built.py")
MANIFEST = os.path.join(ROOT, "MANIFEST_domains.txt")


def load_manifest():
    """读 MANIFEST：每行 `<序号>_<域名>.py`（顺序即拼接顺序；`#` 行与空行忽略）。"""
    items = []
    for line in open(MANIFEST, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fname = line.split("|")[0].strip()   # 兼容历史上带锚的 `文件 | 锚` 写法
        items.append(fname)
    return items


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
    for i, fname in enumerate(items):
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
        # 2026-09-16 移除：成品不再切回域文件（改 domains/ 后跑 dist 即可）
        print("SPLIT_REMOVED: split 已移除（2026-09-16）——方向是单向的：改 domains/ 后跑 "
              "build_single.py dist。若确需从成品单文件重建域文件，见 git 历史中的旧实现，"
              "并按【域文件首行】而非锚来切分。")
        sys.exit(1)
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
        print("用法: build_single.py join|check|dist（split 已于 2026-09-16 移除）")
        sys.exit(1)
