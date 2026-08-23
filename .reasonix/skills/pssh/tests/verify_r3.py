# -*- coding: utf-8 -*-
"""round-3 修复验证脚本：L4 正则矩阵 / N10 parse_target / M1 编码机制 / L6 stdin 单元。
用法: python stest_tmp/verify_r3.py   （在 pyssh 目录下运行）"""
import io
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import pssh  # noqa: E402

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


def hit(cmd):
    return bool(pssh._SENSITIVE_CMD_RE.search(cmd))


print("=== L4: _SENSITIVE_CMD_RE 矩阵 ===")
# 不应误报（工具 flag）
for cmd in [
    "find / -print0 | xargs -0 rm",
    "find . -prune -o -print",
    "find /tmp -printf '%f\\n'",
    "perl -pe 's/a/b/g' file",
    "pytest -p no:cacheprovider",
    "awk -p '{print}' f",
    "unzip -p x.zip",
    "unzip -pfile.zip",
    "gcc -pthread -O2 main.c",
    "gcc -pthreads main.c",
    "xargs -p rm",
    "wget -p https://example.com/",
    "echo -pabc",
    "make -p x",
    "xmake -p x",
    "pip install -p x",
    "cp -p a b",
    "mkdir -p a/b",
    "tar -p x.tar",
    "ssh -p 22 root@host",
    "ssh -p'22' root@host",
    "ps -p 1234",
    "ps -p 1234,5678",
    "git -p status",
    "scp -p a b",
    "rsync -p /x y",
]:
    check("no-match: %r" % cmd, not hit(cmd), "但命中了: %r" % cmd)

# 应命中（疑似凭据）
for cmd in [
    "mysql -u root -p secret",
    "mysql -p secret",
    "mysql -psecret",
    "-psecret",
    "-p secret",
    "curl -u admin:pw http://x",
    "curl --user admin:pw http://x",
    "curl https://user:pass@example.com/",
    "export DB_PASS=s3cr3t",
    "PASSWORD=abc123",
    "MYSQL_PWD=zzz mysql -e 'select 1'",
    "--password xxx",
    "--password=yyy",
]:
    check("match: %r" % cmd, hit(cmd), "漏报!")

print("\n=== N10: parse_target IPv4-mapped / zone ===")
cases = [
    ("fe80::1%eth0", (None, "fe80::1%eth0", None)),
    ("[fe80::1%eth0]:22", (None, "fe80::1%eth0", 22)),
    ("::ffff:1.2.3.4", (None, "::ffff:1.2.3.4", None)),
    ("[::ffff:1.2.3.4]:2222", (None, "::ffff:1.2.3.4", 2222)),
    ("2001:db8::1", (None, "2001:db8::1", None)),
]
for target, want in cases:
    try:
        got = pssh.parse_target(target)
        check("parse %r" % target, got == want, "got=%r want=%r" % (got, want))
    except pssh.SshError as e:
        check("parse %r" % target, False, "意外拒绝: %s" % e)
for target in ["host:22:33", "fe80::1%", "::ffff:1.2.3.999", "gg::1"]:
    try:
        pssh.parse_target(target)
        check("reject %r" % target, False, "竟然接受了")
    except pssh.SshError:
        check("reject %r" % target, True)

print("\n=== M1/L6: reconfigure 逻辑单元（FakeStream 模拟 Linux 流） ===")


class FakeStream:
    def __init__(self, enc, err):
        self.encoding = enc
        self.errors = err

    def reconfigure(self, **kw):
        self.encoding = kw.get("encoding", self.encoding)
        self.errors = kw.get("errors", self.errors)


def linux_branch(streams):
    # 与 pssh._setup_console_utf8 Linux 分支同逻辑（os.name != "nt" 时无法真跑，复制验证）
    for stream in streams:
        try:
            if stream and stream.reconfigure and (
                    (stream.encoding or "").lower().replace("-", "") != "utf8"
                    or stream.errors != "replace"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


f1 = FakeStream("ascii", "strict")      # LANG=C
f2 = FakeStream("utf-8", "strict")      # UTF-8 locale（errors 默认 strict，必须重配）
f3 = FakeStream("utf-8", "replace")     # 已正确，不动
linux_branch([f1, f2, f3])
check("LANG=C 流 -> utf-8/replace", f1.encoding == "utf-8" and f1.errors == "replace",
      "got %r/%r" % (f1.encoding, f1.errors))
check("utf-8+strict 流 -> errors=replace", f2.encoding == "utf-8" and f2.errors == "replace",
      "got %r/%r" % (f2.encoding, f2.errors))
check("已 replace 流不被重复配置", f3.encoding == "utf-8" and f3.errors == "replace",
      "got %r/%r" % (f3.encoding, f3.errors))

# 机制演示：lone surrogate 打印到 strict utf-8 流必炸；replace 流输出 U+FFFD 且 JSON 合法
buf_strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
buf_rep = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")
try:
    buf_strict.write("\udcff")
    check("strict 流打印 surrogate 抛异常", False, "竟然没炸")
except UnicodeEncodeError:
    check("strict 流打印 surrogate 抛异常", True)
buf_rep.write(json.dumps({"msg": "bad-\udcff-name"}, ensure_ascii=False))
buf_rep.flush()
out = buf_rep.buffer.getvalue().decode("utf-8")
# 编码错误的 replace 替换字符是 "?"（U+003F；U+FFFD 是解码错误的替换），
# 关键契约：strict 流必炸、replace 流永远可打印且 JSON 仍单行合法
check("replace 流输出仍是合法单行 JSON", json.loads(out)["msg"] == "bad-?-name",
      "got %r" % out)

print("\n=== 版本 ===")
check("VERSION 为合法 X.Y.Z 且 >= 1.5", re.match(r"^\d+\.\d+\.\d+$", pssh.VERSION) is not None
      and pssh.VERSION >= "1.5.0", "got %s" % pssh.VERSION)

print("\n结果: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
