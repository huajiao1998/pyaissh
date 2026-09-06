# -*- coding: utf-8 -*-
"""传输往返真机测试（脱敏，凭据走 env）：upload/download 字节一致 + 元数据。

覆盖（v2_compare 传输用例提炼）：
  - upload 300KB 随机文件 -> 远端
  - download 回本地 -> 与原文件逐字节一致
  - 结果 JSON 元数据（bytes 一致）
  - 远端清理
"""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

C.require("PYAISSH_TEST_HOST", "PYAISSH_TEST_PASSWORD")
TGT = C.target()
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    local = os.path.join(HERE, "_tmp_xfer.bin")
    dl = os.path.join(HERE, "_tmp_xfer_dl.bin")
    with open(local, "wb") as f:
        f.write(os.urandom(300000))
    # upload
    rc, j, _ = C.run(["upload", TGT, "--local", local, "--remote", "/tmp/_t_xfer.bin"],
                     timeout=300)
    C.check("upload 300KB", j and j.get("ok") is True
            and j.get("bytes_transferred") == 300000)
    # download 回 + 字节一致
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = C.run(["download", TGT, "--remote", "/tmp/_t_xfer.bin", "--local", dl],
                     timeout=300)
    ok_dl = j and j.get("ok") is True
    if ok_dl and os.path.exists(dl):
        src = open(local, "rb").read()
        got = open(dl, "rb").read()
        ok_dl = got == src
    C.check("download 往返字节一致", ok_dl)
    # 并行下载（高吞吐路径）
    if os.path.exists(dl):
        os.remove(dl)
    rc, j, _ = C.run(["download", TGT, "--remote", "/tmp/_t_xfer.bin", "--local", dl,
                      "--parallel", "4"], timeout=300)
    ok_par = j and j.get("ok") is True and j.get("parallel_used") == 4
    if ok_par and os.path.exists(dl):
        ok_par = open(dl, "rb").read() == open(local, "rb").read()
    C.check("download --parallel 4", ok_par)
    # 远端清理 + 本地清理
    C.run(["exec", TGT, "--cmd", "rm -f /tmp/_t_xfer.bin"])
    os.remove(local)
    if os.path.exists(dl):
        os.remove(dl)
    sys.exit(C.finish("test_transfer"))


if __name__ == "__main__":
    main()
