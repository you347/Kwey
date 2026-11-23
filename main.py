"""Kwey 主入口。

用法：
    python main.py --server                # 纯浏览器模式（uvicorn + 自动开浏览器）
    python main.py                         # 桌面模式（pywebview / WebView2）
    python main.py --vault D:/my/vault     # 启动时直接打开指定 vault
    python main.py --port 17630 --no-browser --server   # QA/冒烟用

打包（PyInstaller）后同样支持以上参数；窗口化 exe 没有控制台，标准流会重定向到
``%LOCALAPPDATA%\\Kwey\\logs\\kwey.log``。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app.runtime import ensure_std_streams, is_frozen, log_file
from app.server import create_app

DEFAULT_PORT = 17630
PORT_SCAN_SPAN = 20


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
    parser.add_argument(
        "--mcp", action="store_true",
        help="MCP stdio 服务模式（供外部 Agent 调用；stdout 只输出 JSON-RPC）",
    )
    parser.add_argument(
        "--diag", action="store_true",
        help="自检：打印资源目录/依赖可用性后退出（打包后排障用，结果同时写入 diag.json）",
    )
    return parser.parse_args(argv)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 —— 无桌面环境时静默
        pass


# --------------------------------------------------------------------- 端口
def _port_busy(port: int) -> bool:
    """能否在本机回环地址上绑定该端口；绑定失败即视为被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _is_running_kwey(port: int) -> bool:
    """该端口上是否已经跑着一个 Kwey（按 /api/health 契约判定）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1.0
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 —— 连不上/不是 JSON 都算不是
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, dict) and data.get("status") == "ok"


def resolve_port(preferred: int) -> tuple[int, bool]:
    """返回 ``(port, reuse)``。

    - 端口空闲 → 用它；
    - 端口上是另一个 Kwey 实例 → 复用（避免二次启动直接崩端口占用）；
    - 被其它程序占用 → 顺序探测后续 20 个端口。
    """
    if not _port_busy(preferred):
        return preferred, False
    if _is_running_kwey(preferred):
        return preferred, True
    for port in range(preferred + 1, preferred + PORT_SCAN_SPAN + 1):
        if not _port_busy(port):
            return port, False
    raise RuntimeError(
        f"端口 {preferred}~{preferred + PORT_SCAN_SPAN} 均被占用，请用 --port 指定其它端口"
    )


def _setup_logging(debug: bool) -> None:
    """冻结包把日志落到用户目录，便于用户反馈问题时就地取证。"""
    import logging

    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if not is_frozen():
        return
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


def _wait_ready(port: int, timeout: float = 15.0) -> bool:
    """等 uvicorn 起来；固定 sleep 在慢机器上会开出白屏窗口。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_running_kwey(port):
            return True
        time.sleep(0.15)
    return False


# --------------------------------------------------------------------- 运行
def run_server_mode(app, port: int, open_browser: bool) -> None:
    """--server：主线程跑 uvicorn，前台阻塞直至 Ctrl+C。"""
    url = f"http://127.0.0.1:{port}"
    print(f"[kwey] 服务模式启动: {url}")
    if open_browser:
        threading.Timer(1.2, lambda: _open_browser(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


class KweyBridge:
    """pywebview js_api 桥：前端经 window.pywebview.api.pick_folder() 调用。"""

    def pick_folder(self):
        """弹出系统文件夹选择框，返回所选绝对路径；取消返回 None。"""
        import webview
        # pywebview 6.x：对话框为窗口方法；枚举优先用 FileDialog.FOLDER
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return None
        dialogs = getattr(webview, "FileDialog", webview)
        result = window.create_file_dialog(dialogs.FOLDER)
        if result:
            return result[0]
        return None


def _browse_forever(url: str) -> None:
    _open_browser(url)
    while True:
        time.sleep(3600)  # 保持进程存活，服务继续可用


def run_desktop_mode(app, port: int, reuse: bool = False) -> None:
    """桌面模式：uvicorn 后台线程 + pywebview 窗口；pywebview 不可用则降级浏览器。

    ``reuse=True`` 表示端口上已有本程序实例，只开窗口不再起第二个服务。
    """
    url = f"http://127.0.0.1:{port}"
    if not reuse:
        thread = threading.Thread(
            target=uvicorn.run,
            args=(app,),
            kwargs={"host": "127.0.0.1", "port": port, "log_level": "info"},
            daemon=True,
        )
        thread.start()
        if not _wait_ready(port):
            print(f"[kwey] 服务未在预期时间内就绪: {url}")
    try:
        import webview  # pywebview 延迟导入：--server 模式完全不加载
        webview.create_window("Kwey", url, width=1400, height=900, min_size=(960, 640),
                              js_api=KweyBridge())
        webview.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[kwey] pywebview 不可用（{exc}），降级为浏览器模式: {url}")
        _browse_forever(url)


def main(argv=None) -> int:
    # MCP 模式下 stdout 是协议通道：绝不能重定向到日志文件，也不能打印任何横幅。
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--mcp" in raw:
        return _run_mcp(raw)
    ensure_std_streams()
    try:
        return _run(raw)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 —— 窗口化 exe 崩溃必须留证据，不能静默消失
        _report_fatal()
        return 1


def _run_mcp(raw: list[str]) -> int:
    """``Kwey.exe --mcp --vault <dir>``：复用与 kwey_mcp.py 相同的 stdio 实现。"""
    from app.agent.mcp_stdio import force_utf8, run as run_mcp

    if sys.stdout is None or sys.stdin is None:
        # 从资源管理器直接双击时没有管道：给出明确提示而不是抛 AttributeError
        message = "[kwey-mcp] 未检测到 stdin/stdout 管道，MCP 必须由 Agent 以子进程方式启动"
        try:
            with open(log_file(), "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except OSError:
            pass
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2

    force_utf8()
    return run_mcp([a for a in raw if a != "--mcp"], prog="Kwey --mcp")


def _report_fatal() -> None:
    """窗口化模式下没有控制台：堆栈写日志，并弹一个原生对话框告知用户。"""
    import traceback

    text = traceback.format_exc()
    try:
        with open(log_file(), "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(text)
    if not is_frozen():
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            f"Kwey 启动失败。\n\n日志：{log_file()}\n\n{text.strip().splitlines()[-1][:300]}",
            "Kwey",
            0x10,
        )
    except Exception:  # noqa: BLE001
        pass


def _run_diag() -> int:
    """自检：确认冻结包里的静态资源、桌面壳依赖、PDF 引擎是否就位。"""
    import importlib.util
    import platform
    import subprocess

    from app import __version__
    from app.runtime import data_dir, resource_root

    static_dir = resource_root() / "static"

    def probe(name: str, fn):
        try:
            return {"ok": True, "value": fn()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _import_webview():
        import webview

        return getattr(webview, "__version__", "unknown")

    def _import_platform(mod: str):
        __import__(mod)
        return mod

    def _git():
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or f"code={out.returncode}"

    info = {
        "version": __version__,
        "frozen": is_frozen(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "resource_root": str(resource_root()),
        "static": {
            "dir": str(static_dir),
            "exists": static_dir.is_dir(),
            "index_html": (static_dir / "index.html").is_file(),
            "css_files": len(list((static_dir / "css").glob("*.css"))) if static_dir.is_dir() else 0,
        },
        "data_dir": probe("data_dir", lambda: str(data_dir())),
        "webview": probe("webview", _import_webview),
        "winforms": probe("winforms", lambda: _import_platform("webview.platforms.winforms")),
        "edgechromium": probe("edgechromium", lambda: _import_platform("webview.platforms.edgechromium")),
        "clr": probe("clr", lambda: _import_platform("clr")),
        "reportlab": {"ok": importlib.util.find_spec("reportlab") is not None},
        "chromadb": {"ok": importlib.util.find_spec("chromadb") is not None},
        "sqlite_vec": probe("sqlite_vec", lambda: __import__("app.index.sqlite_vec", fromlist=["x"]).available()),
        "onnxruntime": {"ok": importlib.util.find_spec("onnxruntime") is not None},
        "embed_model": probe("embed_model", lambda: __import__("app.index.models", fromlist=["x"]).status()),
        "git": probe("git", _git),
        "port_busy": {str(p): _port_busy(p) for p in (DEFAULT_PORT,)},
    }
    core_ok = info["static"]["exists"] and info["static"]["index_html"] and info["webview"]["ok"]

    text = json.dumps(info, ensure_ascii=False, indent=2)
    try:
        (data_dir() / "diag.json").write_text(text, encoding="utf-8")
    except OSError:
        pass
    print(text)
    return 0 if core_ok else 1


def _run(argv=None) -> int:
    args = parse_args(argv)
    if args.diag:
        return _run_diag()
    _setup_logging(args.debug)

    try:
        port, reuse = resolve_port(args.port)
    except RuntimeError as exc:
        print(f"[kwey] {exc}")
        return 1

    if args.server:
        if reuse:
            # 已有实例：不重复起服务，把地址告诉用户即可
            print(f"[kwey] 已有 Kwey 实例在运行: http://127.0.0.1:{port}")
            return 0
        app = create_app(initial_vault=args.vault, start_watcher=True)
        run_server_mode(app, port, open_browser=not args.no_browser)
        return 0

    app = None if reuse else create_app(initial_vault=args.vault, start_watcher=True)
    run_desktop_mode(app, port, reuse=reuse)
    return 0


if __name__ == "__main__":
    sys.exit(main())
