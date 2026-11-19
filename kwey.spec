# -*- mode: python ; coding: utf-8 -*-
"""Kwey PyInstaller 打包配置（onedir 单目录）。

用法：
    python -m PyInstaller kwey.spec --noconfirm

产物：
    dist/Kwey/Kwey.exe          # 双击即桌面模式
    dist/Kwey/Kwey.exe --server --port 17630

为什么用 onedir 而不是 onefile：
- onefile 每次启动都要把上百 MB 解压到临时目录，冷启动 5~10s，杀软还会重复扫；
- pythonnet / WebView2 依赖真实 DLL 路径，onefile 的临时目录更容易踩坑。

console=False（窗口化）时没有控制台，main.py 会把 stdout/stderr 重定向到
%LOCALAPPDATA%\\Kwey\\logs\\kwey.log；调试构建可设环境变量 KWEY_CONSOLE=1。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# uvicorn 的协议实现（h11/httptools、websockets/wsproto、lifespan）全是运行时
# 按名字加载的，静态分析看不到，必须显式收集。
hiddenimports = collect_submodules("uvicorn")
hiddenimports += [
    # pywebview：guilib 在函数内按平台选择实现，Windows 走 winforms + EdgeChromium
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    # pythonnet：Python.Runtime.dll 由 hooks-contrib 的 hook-clr 负责收集
    "clr",
    "pythonnet",
    # 运行期才 import 的可选能力
    "reportlab.pdfbase.cidfonts",
    "reportlab.pdfbase.ttfonts",
]

datas = [("static", "static")]

excludes = [
    "pytest",
    "_pytest",
    "httpx",
    "tests",
    "tkinter",
    "chromadb",            # 可选向量后端，未安装时自动降级纯 FTS
    "sentence_transformers",
    "weasyprint",
    "test",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kwey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 会让部分杀软误报，且对启动速度无益
    console=os.environ.get("KWEY_CONSOLE") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Kwey",
)
