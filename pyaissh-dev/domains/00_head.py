#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pyaissh - 基于 paramiko 的命令行 SSH 工具（给 AI 用）

工作原理：
    封装 paramiko，提供 exec / upload / download / test / ls 五个子命令。
    默认输出纯 JSON（stdout 整行一个对象，进度日志一律走 stderr），
    AI 直接解析 stdout 即可；--text 切换为「可读 + ---MARKER---」标记模式。
    大输出自动截断（保留头尾），命令有静默/总时长双重超时，SFTP 有 I/O 超时，
    任何路径都不会无限卡住，也不会撑爆调用方上下文。

环境变量（也可写进脚本目录 .env；工作目录 .env 需显式 PYAISSH_ALLOW_CWD_ENV=1 才加载，
          默认不加载以防供应链注入）：
    PYAISSH_USER      默认用户名
    PYAISSH_PORT      默认端口（22）
    PYAISSH_KEY       私钥路径（env 形式，优先级等同 --key，高于 PYAISSH_PASSWORD）
    PYAISSH_PASSWORD  默认密码
    PYAISSH_JUMP_KEY / PYAISSH_JUMP_PASSWORD   跳板机私钥 / 密码（密码未配置时回退 PYAISSH_PASSWORD，v1.4.9）
    PYAISSH_HOST_<名称>=user@host:port      主机别名，target 写 @名称 即可引用；
        可配 PYAISSH_HOST_<名称>_PASSWORD / PYAISSH_HOST_<名称>_KEY 作为该主机专属凭据
    PYAISSH_ALLOW_CWD_ENV=1                 显式允许加载工作目录 .env（默认不加载，
        防止恶意仓库注入 PYAISSH_HOST_* 等把 AI 导向攻击者主机；加载时会打 WARN）
    认证优先级：--key / PYAISSH_KEY > --password / PYAISSH_PASSWORD > 默认私钥 ~/.ssh/id_ed25519
        （别名配置了专属 KEY/PASSWORD 时，该主机不再取全局 PYAISSH_KEY/PYAISSH_PASSWORD）

退出码：
    exec     = 远程命令退出码（超时 124；连接失败 255；远程退出码恰为 255 时本地返回 254 以免混淆）
    其他     成功 0，传输错误 1，参数错误 2，超时 124，中断 130，连接错误 255

安全提示：
    1. --password / --jump-password 会出现在进程参数里（本地 ps 可见），
       敏感场景请优先用密钥认证，或改用环境变量 PYAISSH_PASSWORD / .env。
    2. exec 的命令原文会打印到 stderr 日志与结果 JSON，含凭据的命令（如 mysql -p'xxx'、
       curl -u user:pass）会留在记录里；建议敏感凭据用远程环境变量注入。

用法示例：
    pyaissh exec root@1.2.3.4 --cmd 'uname -a'
    pyaissh exec root@1.2.3.4 --cmd 'apt upgrade' --max-time 1200   # 长任务调大总时长上限
    pyaissh exec root@1.2.3.4 --cmd 'make' --idle-timeout 120       # 静默超时（连续无输出的窗口）
    pyaissh exec @prod --cmd 'uname -a'                            # 主机别名（.env 配 PYAISSH_HOST_PROD）
    pyaissh exec root@1.2.3.4 --pty --cmd 'tty'                    # 分配 PTY（需要 TTY 的非交互命令）
    pyaissh exec root@1.2.3.4 --pty --pty-strip-ansi --cmd 'top -b -n 1'  # PTY + 剥离 ANSI 供 AI 解析
    pyaissh exec root@1.2.3.4 --cmd-file - <<'EOF'
    ls -la /var/log
    EOF
    pyaissh upload root@1.2.3.4 --local ./dist --remote /opt/app/dist --skip-existing
    pyaissh download root@1.2.3.4 --remote /var/log/x.log --local ./x.log
    pyaissh download root@1.2.3.4 --remote big.tar.gz --local . --parallel 8  # 高丢包链路提速
    pyaissh test root@1.2.3.4
    pyaissh ls root@1.2.3.4 --path /etc --long

    # 通过跳板机连接（跳板用密码，目标用密钥）
    pyaissh exec root@10.0.0.5 --jump root@156.233.234.206:22024 \
        --jump-password 'xxx' --cmd 'hostname'

路径语义：
    远端路径支持 ~ 与 ~/（SFTP 协议本身不展开，pyaissh 自动转换为绝对路径，
    实际路径回显在结果的 remote/path 字段）；不支持通配符（SFTP 无 glob，
    请先 ls 拿到明确文件名）。传目录时源目录的【内容】放入目标目录下
    （不额外嵌套一层）；单文件传到已存在的目录 = 放入该目录（scp 语义）。
"""

import argparse
import codecs
import errno
import json
import os
import posixpath
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

# ===== 极早期信号窗口（v1.4.8）=====
# 这些全局标志与信号 handler 必须定义/注册在 paramiko 慢 import（约 0.3s）
# 之前：进程启动后该窗口内收到 SIGTERM/SIGINT 不再走默认动作（rc=143、
# 无 JSON），而是置标志——main() 入口检查到标志后输出结构化 interrupted
# JSON + 130（窗口从 ~400ms 缩到解释器启动的 ~30ms 物理下限）。
# handler 函数体在信号到达时才解析全局名，此时模块已加载完成，安全。
_SIGTERM_RECEIVED = False
_INTERRUPT_SOURCE = "SIGTERM"
_MAIN_START = None
_CURRENT_ACTION = None
_RESPONDER_STARTED = False
# 进程内第一次 main() 是否已进入：极早期信号检查只对首次调用生效
# （import 阶段），进程内复用时后续调用里的标志是上一次中断留下的，
# 必须走正常重置流程，否则被误判为"本次极早期信号"（实测回归）
_FIRST_MAIN_DONE = False


def _sigterm_handler(signum, frame):
    """SIGTERM/SIGINT 只置标志，【绝不 raise】。

    历史教训（v1.3.x 实测）：在 handler 里 raise KeyboardInterrupt，若主线程
    正阻塞在 paramiko 的 packetizer 读（串行 sftp.get/put），KI 会在持有
    非重入锁的状态下打断 C 级代码，展开途中同线程再次抢锁=自我死锁——
    连强关 socket 都救不了（看门狗/close_all 同样陷进 futex）。
    现在的设计：本函数只置 `_SIGTERM_RECEIVED`；由 _signal_responder 线程
    关闭底层 socket，让阻塞读以普通 socket 错误【在 paramiko 自己的异常
    帧里干净展开】（锁被正常释放），各命令的兜底 except 按标志归位
    interrupted/130；纯 Python 等待点（exec 轮询/目录文件循环）主动检查
    标志抛 KI（在我们的帧里 raise 是安全的）。仅 POSIX 有效——Windows 的
    terminate() 是 TerminateProcess 硬杀，不走信号（注册本身无副作用）。"""
    global _SIGTERM_RECEIVED, _INTERRUPT_SOURCE
    _SIGTERM_RECEIVED = True
    _INTERRUPT_SOURCE = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"


try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
except (ValueError, OSError, ImportError):
    pass  # 非主线程 / 平台不支持（Windows 下注册无副作用）

# 注意：paramiko 不在此 import——v1.5.5 起惰性化，只在真正建连（_do_connect）
# 时才 import。错误路径（--version/--help/bad_args/缺用户名/别名未配置）从
# ~300ms 降到 ~30ms；极早期信号窗口也更短（handler 注册后只剩标准库 import）。

