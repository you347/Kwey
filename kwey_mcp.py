#!/usr/bin/env python3
"""Kwey MCP stdio 入口（M2 / T11）。

用法::

    python kwey_mcp.py --vault "D:/我的知识库"

实现已下沉到 ``app/agent/mcp_stdio.py``，供打包后的 ``Kwey.exe --mcp`` 复用；
本脚本只保留「以脚本方式从任意 cwd 启动都能 import 项目包」的引导逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.agent.mcp_stdio import force_utf8, run  # noqa: E402


if __name__ == "__main__":
    force_utf8()
    raise SystemExit(run(prog="kwey_mcp.py"))
