"""运行时环境适配：源码运行与 PyInstaller 冻结包走同一套路径规则。

三条约束（打包后最容易踩的坑）：
1. **只读资源**（static/）必须从解包目录取：onedir 下是 ``dist/Kwey/_internal``，
   源码下是项目根。绝不能按 cwd 或 exe 同级目录猜。
2. **可写数据**（日志、崩溃记录）必须落在用户目录，不能写安装目录（Program Files
   对普通用户只读）。
3. **窗口化 exe 没有控制台**，``sys.stdout/stderr`` 为 ``None``，logging 的
   StreamHandler 会直接抛异常，必须先把标准流重定向到日志文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否为 PyInstaller 冻结包。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录（``static/`` 的父目录）。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def exec_dir() -> Path:
    """可执行文件所在目录；源码运行时为项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return resource_root()


def data_dir() -> Path:
    """用户级可写目录：Windows ``%LOCALAPPDATA%\\Kwey``，其它平台 XDG 目录。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / "Kwey"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return log_dir() / "kwey.log"


def ensure_std_streams() -> Path | None:
    """补上缺失的标准流，返回日志文件路径（无需补救时返回 None）。

    只在 ``--windowed`` 冻结包触发；``--console`` 与源码运行原样返回。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = log_file()
    stream = open(path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return path
