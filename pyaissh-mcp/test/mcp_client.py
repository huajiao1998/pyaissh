# -*- coding: utf-8 -*-
"""最小 MCP stdio 测试客户端（测试专用，非产品代码）。

子进程方式拉起 pyaissh_mcp.py，按 newline-delimited JSON-RPC 2.0 收发；
stderr 由后台线程持续收集（防管道写满阻塞服务器），供断言诊断信息。
"""
import json
import os
import re
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(BASE, "pyaissh_mcp.py")

# 模拟 DSH 的子进程环境清洗（scrubbedParentEnv）：凭据形状的名字 + DSH_*
_CRED_SHAPED = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.I)


class McpClient:
    def __init__(self, env=None, scrub_creds=False):
        full_env = dict(os.environ)
        if scrub_creds:
            for k in list(full_env):
                if _CRED_SHAPED.search(k) or k.startswith("DSH_"):
                    del full_env[k]
        if env:
            full_env.update(env)   # 与 DSH 一致：配置 env 在清洗【之后】合并
        self.env = full_env
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env, cwd=BASE, text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self.stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip("\n"))
        except Exception:
            pass

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=120):
        """读一行并解析；超过 timeout 秒无输出视为挂死。"""
        # 简单超时： bufsize=1 的 text 模式 readline 无 timeout 参数，
        # 依赖测试外层的总时限；这里不做复杂实现
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("服务器 stdout 意外关闭（EOF）")
        return json.loads(line)

    def request(self, method, params=None, timeout=120):
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        while True:  # 跳过可能混入的其他响应（当前服务器顺序应答，防御性）
            resp = self.recv(timeout)
            if resp.get("id") == mid:
                return resp
        # unreachable

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def call_tool(self, name, arguments, timeout=120):
        return self.request("tools/call",
                            {"name": name, "arguments": arguments}, timeout=timeout)

    def tool_json(self, name, arguments, timeout=120):
        """tools/call 并解析 content 里的 pyaissh JSON 契约，返回 (契约dict, 完整响应)。"""
        resp = self.call_tool(name, arguments, timeout)
        assert "result" in resp, "tools/call 返回了错误而非结果: %r" % (resp,)
        result = resp["result"]
        text = result["content"][0]["text"]
        try:
            parsed = json.loads(text)
        except ValueError:
            raise AssertionError("content 不是合法 JSON: %r (isError=%r)"
                                 % (text[:200], result.get("isError")))
        return parsed, result

    def initialize(self):
        resp = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pyaissh-mcp-test", "version": "0"},
        })
        self.notify("notifications/initialized")
        return resp

    def close(self, expect_exit=True, timeout=15):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            if expect_exit:
                raise AssertionError("服务器未在 %ss 内退出" % timeout)
