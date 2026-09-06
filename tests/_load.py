# -*- coding: utf-8 -*-
"""测试公共被测加载器（脱敏）：PYAISSH_PY 覆盖 / 缺省仓库根 pyaissh.py。"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE = None


def pyaissh():
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
