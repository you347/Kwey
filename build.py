"""一键打包脚本：python build.py [--console] [--dist <dir>]

做的事：
1. 调 PyInstaller 按 ``kwey.spec`` 生成 onedir 包；
2. 跑一次 ``Kwey.exe --diag``，确认静态资源 / WebView2 依赖 / PDF 引擎都在包里；
3. 打印产物路径与体积。

``--console`` 生成带控制台的调试版（看得到 traceback）；默认窗口化发布版。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DIST = "dist"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Kwey 桌面版")
    parser.add_argument("--console", action="store_true", help="保留控制台（调试用）")
    parser.add_argument("--dist", default=DEFAULT_DIST, help=f"产物目录，默认 {DEFAULT_DIST}")
    parser.add_argument("--clean", action="store_true", help="丢弃 PyInstaller 缓存重新分析")
    args = parser.parse_args()

    env = dict(os.environ)
    env["KWEY_CONSOLE"] = "1" if args.console else "0"

    cmd = [
        sys.executable, "-m", "PyInstaller", "kwey.spec",
        "--noconfirm", "--distpath", args.dist, "--workpath", "build",
    ]
    if args.clean:
        cmd.append("--clean")
    print("[build]", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        print("[build] 打包失败")
        return result.returncode

    exe = (ROOT / args.dist / "Kwey" / "Kwey.exe").resolve()
    if not exe.is_file():
        print(f"[build] 未找到产物: {exe}")
        return 1

    print(f"[build] 产物: {exe}")
    print(f"[build] 体积: {sum(f.stat().st_size for f in exe.parent.rglob('*') if f.is_file()) / 1024 / 1024:.1f} MB")

    # 自检：冻结包里的资源与桌面壳依赖是否齐备
    diag = subprocess.run([str(exe), "--diag"], capture_output=True, text=True, timeout=120)
    try:
        info = json.loads(diag.stdout[diag.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        print("[build] 自检输出无法解析，请手工运行 Kwey.exe --diag")
        return 1

    checks = {
        "static": info["static"]["exists"] and info["static"]["index_html"],
        "webview": info["webview"]["ok"],
        "edgechromium": info["edgechromium"]["ok"],
        "clr": info["clr"]["ok"],
    }
    for name, ok in checks.items():
        print(f"[build] {name:14s} {'OK' if ok else 'FAIL'}")
    if not all(checks.values()):
        print("[build] 自检未通过：该产物在目标机器上可能无法启动")
        return 1
    print("[build] 完成。分发时请整目录打包（Kwey.exe 与 _internal 必须同级）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
