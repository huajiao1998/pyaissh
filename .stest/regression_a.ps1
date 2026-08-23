# regression_a.ps1 - pssh 抽测（服务器 A，慢速机）：连接类 + 传输类
$ErrorActionPreference = 'Continue'
Set-Location 'D:\工作目录\Leopold\pyssh'
$env:PSSH_PASSWORD = 'WKkO0147369'
Remove-Item Env:PSSH_USER -ErrorAction SilentlyContinue

$log = 'D:\工作目录\Leopold\pyssh\stest_tmp\logs\regression_agent_A.txt'
$raw = 'D:\工作目录\Leopold\pyssh\stest_tmp\raw'
New-Item -ItemType Directory -Force -Path $raw | Out-Null

function T([string]$id, [scriptblock]$body) {
    $so = Join-Path $raw ("A_" + $id + ".out")
    $se = Join-Path $raw ("A_" + $id + ".err")
    $rcf = Join-Path $raw ("A_" + $id + ".rc")
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

$upbig = 'D:\工作目录\Leopold\pyssh\stest_tmp\up_big_A.bin'
$bytes = New-Object byte[] (2 * 1024 * 1024)
(New-Object System.Random(7)).NextBytes($bytes)
[System.IO.File]::WriteAllBytes($upbig, $bytes)

T 'A01_test' { python pssh.py test root@103.79.184.140 }
T 'A02_wrongpass' {
    $old = $env:PSSH_PASSWORD
    $env:PSSH_PASSWORD = 'wrongpass'
    python pssh.py test root@103.79.184.140
    $env:PSSH_PASSWORD = $old
}
T 'A03_wrongport' { python pssh.py test --timeout 4 root@103.79.184.140 -p 1 }
T 'A04_dns' { python pssh.py test root@no-such-host-zzq987.invalid }
T 'A05_blackhole' { python pssh.py test --timeout 3 root@10.255.255.1 }
T 'A06_exec' { python pssh.py exec root@103.79.184.140 --cmd 'uname -a' }
T 'A07_exit3' { python pssh.py exec root@103.79.184.140 --cmd 'exit 3' }
T 'A08_exit255' { python pssh.py exec root@103.79.184.140 --cmd 'exit 255' }
T 'A09_setup' { python pssh.py exec root@103.79.184.140 --cmd 'rm -rf /tmp/pssh_reg; mkdir -p /tmp/pssh_reg' }
T 'A10_upload' { python pssh.py upload root@103.79.184.140 --local 'stest_tmp\up_big_A.bin' --remote /tmp/pssh_reg/big.bin }
T 'A11_md5' { python pssh.py exec root@103.79.184.140 --cmd 'md5sum /tmp/pssh_reg/big.bin' }
T 'A12_download' { python pssh.py download root@103.79.184.140 --remote /tmp/pssh_reg/big.bin --local 'stest_tmp\dl_big_A.bin' }
T 'A13_idle_to' { python pssh.py exec root@103.79.184.140 --cmd 'sleep 8' --idle-timeout 2 }
T 'A14_cleanup' { python pssh.py exec root@103.79.184.140 --cmd 'rm -rf /tmp/pssh_reg; echo CLEANED_A' }
Write-Host 'ALL_A_TESTS_DONE'
