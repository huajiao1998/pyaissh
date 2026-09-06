# -*- coding: utf-8 -*-
"""测试公共加载器（脱敏：不硬编码任何机器/凭据路径）。

被测目标解析：
  1. env PYAISSH_PY=<path>：importlib 从指定文件加载（测 dev built 等任意版本）
  2. 否则：从本仓库根目录 import pyaissh（python tests/unit/xxx.py 直接跑）
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
_MODULE = None


def pyaissh():
    """返回被测模块（惰性加载，带缓存）。"""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    p = os.environ.get("PYAISSH_PY")
    if p:
        spec = importlib.util.spec_from_file_location("pyaissh", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    else:
        sys.path.insert(0, _ROOT)
        import pyaissh as m
    _MODULE = m
    return _MODULE
