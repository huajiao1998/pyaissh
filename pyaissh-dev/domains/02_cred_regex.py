"""凭据启发式正则（域 02）——"疑似凭据"WARN 的判定源。

- 模式全家 _P_SENS_*（password=/--password/-p'xxx'/-psecret/-p secret/URL 凭据/env 凭据/mysql/curl）
- 统一防线 _P_LOOKBEHIND_P（(?<![A-Za-z0-9-])-p：词中 -p 全排除，1.5.17 修 --no-pager）
- _SENSITIVE_CMD_RE 组合入口 + 验收注释矩阵（改模式必过矩阵）
- _ANSI_RE / _RE_WIN_ILLEGAL
被 05_util.warn_sensitive_cmd 调用（凭据 WARN 落在 log/结果）。
"""

# --- 正则模式（模块级编译一次；片段化让每个分支可独立注释/测试） ---
_RE_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_RE_IPV6_SEG = re.compile(r"[0-9a-fA-F]{1,4}")
_RE_IPV6_ZONE = re.compile(r"[0-9a-zA-Z._+-]+")
_RE_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

# 疑似凭据模式片段（保守匹配，避免误报）。拼接顺序与历史 alternation 完全一致
#（等价变换，L4 矩阵 40 例验证）。匹配形态：
#   - password=xxx / password: xxx / --password xxx / --password=xxx
#   - -p'xxx' / -p"xxx" / -psecret / -p secret（排除纯数字端口：-p 22 / -p'22' / -p123456）
#   - mysql -u root -p xxx / curl -u user:pass
_P_SENS_PASSWORD = (r"passw[o0]?rd\s*[=:]\s*(?!-)(?![\"']{2}(?:\s|$))\S+"
                     r"|--password(?:\s+|=)(?!-)(?![\"']{2}(?:\s|$))\S+")
_P_SENS_USER = r"--user\s+\S+:\S+"          # curl --user admin:pw 长形式
_P_SENS_URL = r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"   # https://user:pass@host/
# DB_PASS=x / DB_PASS: x / MYSQL_PWD=；v2.1.4：(?!-) 排除赋值后跟 -参数
# （java -Dspring.datasource.password= -jar 实测误报——password= 空、-jar 是下个参数）；
# 空引号对排除（DB_PASS='' 空值无秘密）
_P_SENS_ENV = (r"\b\w*(?:PASS(?:WORD|WD|CODE)?|PWD)\s*[=:]\s*(?!-)(?![\"']{2}(?:\s|$))\S+")
# 统一防线 (?<![A-Za-z0-9-])：-p 作为密码选项时前字符必为空白/行首/引号，
# 绝不可能是字母/数字/连字符——连字符复合词中间的 -p（--no-pager、a-px）与
# 词内 -p 全部排除（1.5.16 修：原 (?<!-) 只挡双横线开头，挡不住 no-pager 的 -p）。
# v2.1.4：(?-i:-p) 局部区分大小写——-p 密码选项恒小写；-P（grep -P 等）是
# Perl/端口 flag 不命中（实测误报 "grep -P 'a+b' file" 因 (?i) 吞了大写）。
_P_LOOKBEHIND_P = r"(?<![A-Za-z0-9-])(?-i:-p)"
_P_SENS_P_QUOTED = _P_LOOKBEHIND_P + r"['\"](?!\d+['\"])[^'\"]+['\"]"   # -p'secret'（排除 -p'22' 纯数字端口/ID）
# -psecret 紧贴形态（-p 后必须非空白，空格形态交给 _P_SENS_P_SPACE）：
# 前缀 lookbehind 排除常见非密码工具（scp/rsync/curl/make/install/find/perl/echo/unzip/gcc/xargs/awk；
# v2.1.4 +ffmpeg/ffprobe——-pix_fmt 实测误报）
_P_SENS_P_ATTACH = (
    r"(?<!scp )(?<!rsync )(?<!curl )(?<!make )(?<!install )"
    r"(?<!find )(?<!perl )(?<!echo )(?<!unzip )(?<!gcc )(?<!xargs )(?<!awk )"
    r"(?<!ffmpeg )(?<!ffprobe )"
    + _P_LOOKBEHIND_P
    + r"(?!['\"]?\d+(?:['\"]|\b))(?!\s)"
    r"(?!rin|rune|thread|pe\b|roxy|ort|ath|ass|lain)\S+"
)
# -p secret（空格分隔）：lookbehind 排除常见非密码工具（cp/mkdir/ls/tar/scp/rsync/curl/
# make/install/unzip/pytest/awk/xargs/wget，覆盖单/双空格；v2.1.4 +grep/egrep/fgrep——
# 实测误报 "grep -p foo /etc/passwd"）；(?!--)/(?!-) 排除 -p 后跟选项；
# 词表排除选项名、工具参数与协议名；[^\s/]+ 排除路径类参数（rsync -p /x、mkdir -p a/b 的兜底）；
# (?!["']{2}...) 排除空引号值（useradd -p '' 实测误报——空值无秘密）
_P_SENS_P_SPACE = (
    r"(?<!cp )(?<!cp  )(?<!ls )(?<!ls  )(?<!tar )(?<!tar  )(?<!scp )(?<!scp  )"
    r"(?<!mkdir )(?<!mkdir  )(?<!rsync )(?<!rsync  )(?<!curl )(?<!curl  )"
    r"(?<!make )(?<!make  )(?<!install )(?<!install  )"
    r"(?<!unzip )(?<!unzip  )(?<!pytest )(?<!pytest  )(?<!awk )(?<!awk  )"
    r"(?<!xargs )(?<!xargs  )(?<!wget )(?<!wget  )"
    r"(?<!grep )(?<!grep  )(?<!egrep )(?<!egrep  )(?<!fgrep )(?<!fgrep  )"
    + _P_LOOKBEHIND_P
    + r"\s+(?!\d+\b)(?!--)(?!-)(?![\"']{2}(?:\s|$))"
    r"(?!proxy\b|roxy\b|port\b|path\b|pass\b|plain\b|"
    r"log\b|diff\b|show\b|status\b|add\b|commit\b|clone\b|pull\b|push\b|remote\b|"
    r"branch\b|checkout\b|merge\b|tag\b|stash\b|init\b|config\b|fetch\b|rebase\b|"
    r"reset\b|rm\b|mv\b|help\b|version\b|verbose\b|git\b|docker\b|nmap\b|"
    r"tcp\b|udp\b|icmp\b)"
    r"[^\s/]+"
)
_P_SENS_MYSQL = r"mysql\s+-u\s*\S+\s+-p\s*\S*"
_P_SENS_CURL_U = r"curl\s+.*-u\s*\S+:\S+"

_SENSITIVE_CMD_RE = re.compile(
    r"(?i)(%s|%s|%s|%s|%s|%s|%s|%s|%s)" % (
        _P_SENS_PASSWORD, _P_SENS_USER, _P_SENS_URL, _P_SENS_ENV,
        _P_SENS_P_QUOTED, _P_SENS_P_ATTACH, _P_SENS_P_SPACE,
        _P_SENS_MYSQL, _P_SENS_CURL_U,
    ))

# "从文件读值"形态（v2.1 豁免：$(cat f) / $(<f)）——凭据不进命令行文本，
# 日志无明文可泄，warn_sensitive_cmd 对含此形态的命令整条放行（实测误报：
# DB_PASS=$(cat /srv/x)、export PASS=$(cat /tmp/p)、mysql -p $(cat f)）。
_READ_FROM_FILE_RE = re.compile(r"\$\(\s*(?:cat\b|<)")

# ---- 验收案例（改 _P_SENS_* 片段必对照自查；完整矩阵见开发机 verify_r3 L4）----
# 应命中（疑似凭据）：
#   -psecret / -p secret / -p'xxx' / -p"xxx"       紧贴与空格形态
#   --password=abc / --password abc / password=abc 长选项与赋值
#   mysql -u root -p secret / curl -u user:pass    工具专有形态
#   https://user:pass@host/ / DB_PASS=abc / MYSQL_PWD=abc  URL/环境变量
# 不应命中（工具 flag/端口/路径，历史误报点）：
#   --profile x / --parallel 4 / --progress        双横线长选项（1.5.0 修）
#   --no-pager / --no-color / --dry-run           复合长选项词中 -p（1.5.16 修：
#     (?<![A-Za-z0-9-]) 统一防线——-p 密码选项前必空白/行首，词中/复合词 -p 全挡）
#   -p 22 / -p'22' / -p123456 / ssh -p 22 root@h   纯数字端口/ID
#   mkdir -p a/b / tar -p x / cp -p a b / gcc -pthread  工具 -p
#   rsync -p /x / wget -p https://... / pytest -p x     路径/参数
#   echo -pabc / git push / --port 22              其他
# 注意：_P_SENS_P_ATTACH 与 _P_SENS_P_SPACE 的排除表（cp/mkdir/tar/...）
# 靠 lookbehind 前缀精确匹配——改排除表时上面每条都要重新过一遍。

_ANSI_RE = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b.")


