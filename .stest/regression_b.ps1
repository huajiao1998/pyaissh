# regression_b.ps1 - pyaissh 全量回归矩阵（服务器 B，快速机）
$ErrorActionPreference = 'Continue'
Set-Location 'D:\工作目录\Leopold\pyssh'
$env:PYAISSH_PASSWORD = 'WKkO0147369'
$env:PYAISSH_JUMP_PASSWORD = 'WKkO0147369'
Remove-Item Env:PYAISSH_USER -ErrorAction SilentlyContinue
Remove-Item Env:PYAISSH_ALLOW_CWD_ENV -ErrorAction SilentlyContinue
Remove-Item Env:PYAISSH_HOST_REG1 -ErrorAction SilentlyContinue
Remove-Item Env:PYAISSH_HOST_REG1_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:pssh_host_reg1 -ErrorAction SilentlyContinue

$log = 'D:\工作目录\Leopold\pyssh\stest_tmp\logs\regression_agent.txt'
$raw = 'D:\工作目录\Leopold\pyssh\stest_tmp\raw'
New-Item -ItemType Directory -Force -Path $raw | Out-Null

function T([string]$id, [scriptblock]$body) {
    $so = Join-Path $raw ("B_" + $id + ".out")
    $se = Join-Path $raw ("B_" + $id + ".err")
    $rcf = Join-Path $raw ("B_" + $id + ".rc")
    "`n===== TEST $id =====" | Out-File -Append -Encoding utf8 $log
    & $body 1> $so 2> $se
    $rc = $LASTEXITCODE
    Set-Content -Path $rcf -Value $rc -Encoding ascii
    $out = Get-Content -Raw -Encoding utf8 $so
    if ($out) {
        if ($out.Length -gt 16000) { $show = $out.Substring(0, 16000) + "...[TRUNC]" } else { $show = $out }
    } else { $show = '' }
    "STDOUT:`n$show" | Out-File -Append -Encoding utf8 $log
    "RC=$rc" | Out-File -Append -Encoding utf8 $log
    $err = Get-Content -Raw -Encoding utf8 $se
    if ($err) {
        if ($err.Length -gt 3000) { $esh = $err.Substring(0, 3000) + "...[TRUNC]" } else { $esh = $err }
        "STDERR:`n$esh" | Out-File -Append -Encoding utf8 $log
    }
    Write-Host ("TEST {0} rc={1}" -f $id, $rc)
}

# ===== 本地准备：2MB 随机文件 + 目录结构 + 中文/空格文件名 =====
$upbig = 'D:\工作目录\Leopold\pyssh\stest_tmp\up_big.bin'
$bytes = New-Object byte[] (2 * 1024 * 1024)
(New-Object System.Random(42)).NextBytes($bytes)
[System.IO.File]::WriteAllBytes($upbig, $bytes)
$updir = 'D:\工作目录\Leopold\pyssh\stest_tmp\up_dir'
New-Item -ItemType Directory -Force -Path "$updir\sub", "$updir\empty" | Out-Null
Set-Content -Path "$updir\a.txt" -Value 'AAA' -Encoding utf8
Set-Content -Path "$updir\sub\b.txt" -Value 'BBB' -Encoding utf8
[System.IO.File]::WriteAllText('D:\工作目录\Leopold\pyssh\stest_tmp\中文文件.txt', '中文内容测试', [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText('D:\工作目录\Leopold\pyssh\stest_tmp\file with space.txt', 'space content', [System.Text.Encoding]::UTF8)

# ===== 1. test 成功（schema）=====
T '01_test_ok' { python pyaissh.py test root@43.226.128.77:26803 }

# ===== 2. 错误密码 / 错误端口 =====
T '02a_wrongpass' {
    $old = $env:PYAISSH_PASSWORD
    $env:PYAISSH_PASSWORD = 'wrongpass'
    python pyaissh.py test root@43.226.128.77:26803
    $env:PYAISSH_PASSWORD = $old
}
T '02b_wrongport' { python pyaissh.py test --timeout 4 root@43.226.128.77 -p 1 }

# ===== 3. DNS 失败 / 黑洞 IP =====
T '03a_dns' { python pyaissh.py test root@no-such-host-zzq987.invalid }
T '03b_blackhole' { python pyaissh.py test --timeout 3 root@10.255.255.1 }

# ===== 4. 缺用户名 =====
T '04_nouser' { python pyaissh.py test 43.226.128.77:26803 }

# ===== 5. exec 退出码透传 =====
T '05a_uname' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'uname -a' }
T '05b_exit3' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'exit 3' }
T '05c_exit255' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'exit 255' }

# ===== 6. stdout/stderr 分离 =====
T '06_split' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'echo out; echo err >&2' }

# ===== 7. 中文输出 =====
T '07_cjk' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'echo 中文测试' }

# ===== 8. 静默超时 =====
T '08_idle_timeout' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'sleep 8' --idle-timeout 2 }

# ===== 9. 总时长超时 =====
T '09_total_timeout' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'while true; do echo x; sleep 1; done' --idle-timeout 2 --max-time 5 }

# ===== 10. --max-time < --idle-timeout =====
T '10_maxlt_idle' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'echo hi' --max-time 5 --idle-timeout 10 }

# ===== 11. 大输出截断 =====
T '11a_seq_default' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'seq 1 300000' }
T '11b_seq_4096' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'seq 1 300000' --max-output 4096 }

# ===== 12. --cmd / --cmd-file 组合 =====
T '12a_both' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'echo hi' --cmd-file 'stest_tmp\nope_cmd.txt' }
T '12b_cmdfile_missing' { python pyaissh.py exec root@43.226.128.77:26803 --cmd-file 'stest_tmp\nope_cmd.txt' }
T '12c_empty_cmd' { python pyaissh.py exec root@43.226.128.77:26803 --cmd '' }

# ===== 13. --cmd-file - 从 stdin =====
T '13_stdin' { 'echo from-stdin' | python pyaissh.py exec root@43.226.128.77:26803 --cmd-file - }

# ===== 14. PTY =====
T '14a_pty_strip' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'top -b -n 1' --pty --pty-strip-ansi }
T '14b_pty_raw' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'top -b -n 1' --pty }

# ===== 15. 敏感命令告警 =====
T '15_sensitive' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'curl -u admin:pw http://x/' }

# ===== 16. 单文件上传 + skip-existing =====
T '16_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg' }
T '16a_upload' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote /tmp/pssh_reg/big.bin }
T '16a_md5' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'md5sum /tmp/pssh_reg/big.bin' }
T '16b_skip' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote /tmp/pssh_reg/big.bin --skip-existing }
T '16c_reup' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote /tmp/pssh_reg/big.bin }

# ===== 17. 目录递归上传 =====
T '17_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg' }
T '17a_up_dir' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_dir' --remote /tmp/pssh_reg/dir17 }
T '17a_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'find /tmp/pssh_reg/dir17 | sort' }
T '17b_no_rec' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_dir' --remote /tmp/pssh_reg/dir17nr --no-recursive }
T '17b_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'ls -la /tmp/pssh_reg/dir17nr' }
T '17c_dry' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_dir' --remote /tmp/pssh_reg/dir17dry --dry-run }
T '17c_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'test -e /tmp/pssh_reg/dir17dry && echo DRY_EXISTS || echo DRY_ABSENT' }

# ===== 18. 单文件传到已存在目录 =====
T '18_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg/dir18' }
T '18_up' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote /tmp/pssh_reg/dir18 }
T '18_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'ls -l /tmp/pssh_reg/dir18' }

# ===== 19. 下载 =====
T '19_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg/dl/sub /tmp/pssh_reg/dl/empty; echo DLFILE > /tmp/pssh_reg/dl/sub/c.txt; head -c 65536 /dev/urandom > /tmp/pssh_reg/dl/f.bin' }
New-Item -ItemType Directory -Force -Path 'D:\工作目录\Leopold\pyssh\stest_tmp\dl_dir' | Out-Null
T '19a_dl_file' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/dl/f.bin --local 'stest_tmp\dl_f.bin' }
T '19a_md5' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'md5sum /tmp/pssh_reg/dl/f.bin' }
T '19b_dl_into_dir' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/dl/f.bin --local 'stest_tmp\dl_dir' }
T '19c_dl_dir' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/dl --local 'stest_tmp\dl_out' }
T '19d_skip' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/dl/f.bin --local 'stest_tmp\dl_f.bin' --skip-existing }
T '19e_dry' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/dl --local 'stest_tmp\dl_dry' --dry-run }

# ===== 20. 不存在路径 / 通配符 =====
T '20a_dl_missing' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/nope.txt --local 'stest_tmp\x' }
T '20b_dl_glob' { python pyaissh.py download root@43.226.128.77:26803 --remote '/tmp/pssh_reg/abc*' --local 'stest_tmp\x' }
T '20c_up_glob' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote '/tmp/pssh_reg/abc*' }

# ===== 21. ls =====
T '21_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg/subdir; for i in $(seq 1 8); do touch /tmp/pssh_reg/f$i; done' }
T '21a_ls' { python pyaissh.py ls root@43.226.128.77:26803 --path /tmp/pssh_reg }
T '21b_ls_long' { python pyaissh.py ls root@43.226.128.77:26803 --path /tmp/pssh_reg --long }
T '21c_ls_limit' { python pyaissh.py ls root@43.226.128.77:26803 --path /tmp/pssh_reg --limit 5 }
T '21d_ls_missing' { python pyaissh.py ls root@43.226.128.77:26803 --path /tmp/pssh_reg/nope }
T '21e_ls_home' { python pyaissh.py ls root@43.226.128.77:26803 --path '~' }
T '21f_ls_tilde_user' { python pyaissh.py ls root@43.226.128.77:26803 --path '~otheruser' }

# ===== 22. ~ 展开上传 =====
T '22_up_home' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\up_big.bin' --remote '~/pssh_reg_home/' }
T '22_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'ls -l /root/pssh_reg_home/' }

# ===== 23. 中文/空格文件名往返 =====
T '23_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg' }
T '23a_up_cjk' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\中文文件.txt' --remote '/tmp/pssh_reg/中文文件.txt' }
T '23b_up_space' { python pyaissh.py upload root@43.226.128.77:26803 --local 'stest_tmp\file with space.txt' --remote '/tmp/pssh_reg/file with space.txt' }
T '23c_check' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'ls /tmp/pssh_reg; cat "/tmp/pssh_reg/中文文件.txt"; cat "/tmp/pssh_reg/file with space.txt"' }
T '23d_dl_cjk' { python pyaissh.py download root@43.226.128.77:26803 --remote '/tmp/pssh_reg/中文文件.txt' --local 'stest_tmp\dl_中文.txt' }
T '23e_dl_space' { python pyaissh.py download root@43.226.128.77:26803 --remote '/tmp/pssh_reg/file with space.txt' --local 'stest_tmp\dl space.txt' }

# ===== 24. 特殊字符远端文件名下载清洗 =====
T '24_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg/spec; cd /tmp/pssh_reg/spec && touch "a:b.txt" "con.txt" "a*b.txt" && ls' }
T '24_dl_spec' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg/spec --local 'stest_tmp\dl_spec' }

# ===== 25. 跳板 A -> B =====
T '25_jump_ok' {
    $env:PYAISSH_JUMP_PASSWORD = 'WKkO0147369'
    python pyaissh.py test root@43.226.128.77:26803 --jump root@103.79.184.140
}
T '26_jump_badpw' {
    $env:PYAISSH_JUMP_PASSWORD = 'wrong'
    python pyaissh.py test root@43.226.128.77:26803 --jump root@103.79.184.140
}

# ===== 27. 别名 =====
T '27a_alias' {
    Remove-Item Env:PYAISSH_HOST_REG1 -ErrorAction SilentlyContinue
    $env:PYAISSH_HOST_REG1 = 'root@43.226.128.77:26803'
    python pyaissh.py test '@REG1'
}
T '27b_alias_lower' {
    Remove-Item Env:PYAISSH_HOST_REG1 -ErrorAction SilentlyContinue
    $env:pssh_host_reg1 = 'root@43.226.128.77:26803'
    python pyaissh.py test '@reg1'
}
T '28_alias_pw' {
    Remove-Item Env:PYAISSH_HOST_REG1 -ErrorAction SilentlyContinue
    Remove-Item Env:pssh_host_reg1 -ErrorAction SilentlyContinue
    Remove-Item Env:PYAISSH_HOST_REG1_PASSWORD -ErrorAction SilentlyContinue
    $env:PYAISSH_HOST_REG1 = 'root@43.226.128.77:26803'
    $env:PYAISSH_HOST_REG1_PASSWORD = 'WKkO0147369'
    $old = $env:PYAISSH_PASSWORD
    $env:PYAISSH_PASSWORD = 'wrongpass'
    python pyaissh.py test '@REG1'
    $env:PYAISSH_PASSWORD = $old
    Remove-Item Env:PYAISSH_HOST_REG1 -ErrorAction SilentlyContinue
    Remove-Item Env:PYAISSH_HOST_REG1_PASSWORD -ErrorAction SilentlyContinue
}

# ===== 29. cwd .env 供应链防护（在独立 cwd 下验证防护逻辑）=====
$evil = 'D:\工作目录\Leopold\pyssh\stest_tmp\evilcwd'
New-Item -ItemType Directory -Force -Path $evil | Out-Null
Set-Content -Path "$evil\.env" -Value 'PYAISSH_HOST_EVIL=root@127.0.0.1:1' -Encoding utf8
T '29a_env_noflag' {
    Push-Location 'D:\工作目录\Leopold\pyssh\stest_tmp\evilcwd'
    python 'D:\工作目录\Leopold\pyssh\pyaissh.py' test '@EVIL'
    Pop-Location
}
T '29b_env_flag' {
    $env:PYAISSH_ALLOW_CWD_ENV = '1'
    Push-Location 'D:\工作目录\Leopold\pyssh\stest_tmp\evilcwd'
    python 'D:\工作目录\Leopold\pyssh\pyaissh.py' test '@EVIL'
    Pop-Location
    Remove-Item Env:PYAISSH_ALLOW_CWD_ENV -ErrorAction SilentlyContinue
}
T '29c_workdir_env' {
    # 注：workdir == 脚本目录，.env 属"可信脚本环境"，按设计直接加载（供记录实况）
    Set-Content -Path 'D:\工作目录\Leopold\pyssh\.env' -Value 'PYAISSH_HOST_EVIL=root@127.0.0.1:1' -Encoding utf8
    python pyaissh.py test '@EVIL'
    Remove-Item 'D:\工作目录\Leopold\pyssh\.env' -ErrorAction SilentlyContinue
}

# ===== 30. CLI 契约 =====
T '30a_version' { python pyaissh.py --version }
T '30b_no_sub' { python pyaissh.py }
T '30c_bad_sub' { python pyaissh.py frobnicate root@43.226.128.77:26803 }
T '30d_text' { python pyaissh.py --text test root@43.226.128.77:26803 }

# ===== 31. 中断（SIGINT harness）=====
T '31_sigint' {
    $env:PYAISSH_PY = 'D:/工作目录/Leopold/pyssh/pyaissh.py'
    $env:SIG = 'INT'
    python .stest\sigterm_harness.py 2000 -- exec root@43.226.128.77:26803 --cmd 'sleep 30'
}

# ===== 32. 输出略大于 max-output =====
T '32a_3k_4096' { python pyaissh.py exec root@43.226.128.77:26803 --cmd "python -c \"print('a'*3000)\"" --max-output 4096 }
T '32b_3k_5000' { python pyaissh.py exec root@43.226.128.77:26803 --cmd "python -c \"print('a'*3000)\"" --max-output 5000 }
T '32c_5k_4096' { python pyaissh.py exec root@43.226.128.77:26803 --cmd "python -c \"print('a'*5000)\"" --max-output 4096 }

# ===== 33. 不可读子目录（root 下验证性观察，可选）=====
T '33_setup' { python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg/locked; echo SECRET > /tmp/pssh_reg/locked/f.txt; chmod 000 /tmp/pssh_reg/locked' }
T '33_dl_locked' { python pyaissh.py download root@43.226.128.77:26803 --remote /tmp/pssh_reg --local 'stest_tmp\dl_locked' }

# ===== 清理 =====
T '99_cleanup_remote' {
    python pyaissh.py exec root@43.226.128.77:26803 --cmd 'rm -rf /tmp/pssh_reg; rm -rf /root/pssh_reg_home; echo CLEANED'
}
Write-Host 'ALL_B_TESTS_DONE'
