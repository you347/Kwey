"""Kwey 主入口。

用法：
    python main.py --server                # 纯浏览器模式（uvicorn + 自动开浏览器）
    python main.py                         # 桌面模式（pywebview / WebView2）
    python main.py --vault D:/my/vault     # 启动时直接打开指定 vault
    python main.py --port 17630 --no-browser --server   # QA/冒烟用
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app.server import create_app

DEFAULT_PORT = 17630


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kwey", description="Kwey 个人知识库")
    parser.add_argument(
        "--server", action="store_true",
        help="纯浏览器模式：仅启动 uvicorn 并自动打开浏览器（跳过 pywebview）",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 17630")
    parser.add_argument("--vault", type=str, default=None, help="启动时直接打开的 vault 绝对路径")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    return parser.parse_args(argv)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 —— 无桌面环境时静默
        pass


def run_server_mode(app, port: int, open_browser: bool) -> None:
    """--server：主线程跑 uvicorn，前台阻塞直至 Ctrl+C。"""
    url = f"http://127.0.0.1:{port}"
    print(f"[kwey] 服务模式启动: {url}")
    if open_browser:
        threading.Timer(1.2, lambda: _open_browser(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def run_desktop_mode(app, port: int) -> None:
    """桌面模式：uvicorn 后台线程 + pywebview 窗口；pywebview 不可用则降级浏览器。"""
    url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=uvicorn.run, args=(app,),
                              kwargs={"host": "127.0.0.1", "port": port, "log_level": "info"},
                              daemon=True)
    thread.start()
    time.sleep(1.2)  # 等服务就绪
    try:
        import webview  # pywebview 延迟导入：--server 模式完全不加载
        webview.create_window("Kwey", url, width=1400, height=900, min_size=(960, 640))
        webview.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[kwey] pywebview 不可用（{exc}），降级为浏览器模式: {url}")
        _open_browser(url)
        while True:
            time.sleep(3600)  # 保持进程存活，服务继续可用


def main(argv=None) -> int:
    args = parse_args(argv)
    app = create_app(initial_vault=args.vault, start_watcher=True)
    if args.server:
        run_server_mode(app, args.port, open_browser=not args.no_browser)
    else:
        run_desktop_mode(app, args.port)
    return 0
if __name__ == "__main__":
    sys.exit(main())
