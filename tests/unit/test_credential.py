# -*- coding: utf-8 -*-
"""凭据启发式单测：真凭据命中 + 复合词/端口/工具 flag 不误报（脱敏，无网络）。

用例来源：v516_cred_unit.py（--no-pager 修复 41 例）+ verify_r3 L4 凭据矩阵。
运行：python tests/unit/test_credential.py
被测目标：env PYAISSH_PY 覆盖（测 dev built），缺省根 pyaissh.py。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _load import pyaissh  # noqa: E402

m = pyaissh()

hit = ["-psecret", "-p secret", "-p'xxx'", '-p"xxx"', "--password=abc", "--password abc",
       "mysql -u root -p secret", "https://user:pass@host/", "DB_PASS=abc", "MYSQL_PWD=abc",
       "curl -u user:pass http://x", "sshpass -p topsecret cmd", "-p's3cr3t!'",
       "mysqldump -u root -pdb_pass123 db", "--password 'abc def'"]
nohit = ["--profile x", "--parallel 4", "--progress", "--no-pager", "git log --no-pager",
         "--no-color", "--dry-run", "-p 22", "-p'22'", "-p123456", "ssh -p 22 root@h",
         "mkdir -p a/b", "tar -p x", "cp -p a b", "gcc -pthread", "rsync -p /x",
         "wget -p https://x", "pytest -p x", "echo -pabc", "git push", "--port 22",
         "systemctl --no-pager status ssh", "ls --no-pager", "--no-pager=true",
         "apt-get --no-pager list", "--no-plugins"]

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


for c in hit:
    check("应命中 %r" % c[:30], bool(m.warn_sensitive_cmd(c, enabled=True)))
for c in nohit:
    check("不应命中 %r" % c[:30], not m.warn_sensitive_cmd(c, enabled=True))

print("test_credential: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
