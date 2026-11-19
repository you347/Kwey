"""MCP stdio 服务实现（JSON-RPC 2.0，逐行请求/响应）。

被两个入口复用：
- ``kwey_mcp.py --vault <dir>``（源码运行 / 外部 Agent 直接调用脚本）；
- ``Kwey.exe --mcp --vault <dir>``（打包后的单文件入口，避免用户还要装 Python）。

协议约定：**stdout 只承载协议 JSON**，所有日志/异常写 stderr。
stdin 为本地父子进程私有管道，调用方即启动者，无需 token 鉴权。
不启动 watcher 线程，EOF 时释放 SQLite 连接。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from app.agent.mcp_server import KweyMCPServer
from app.api import deps


def force_utf8() -> None:
    """Windows 控制台默认可能是 cp936/gbk，强制 stdio 使用 UTF-8。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 —— 某些被替换的流不支持 reconfigure
            pass


def _emit(resp: Dict[str, Any]) -> None:
    """写一行 JSON 到 stdout（唯一允许写 stdout 的地方）。"""
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _parse_error(message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": message}}


def run(argv: Optional[list] = None, prog: str = "kwey-mcp") -> int:
    """读取 stdin 逐行 JSON-RPC 请求，写 stdout 响应；返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Kwey MCP Server（stdio / JSON-RPC 2.0）。逐行读取 stdin 请求，stdout 返回响应。",
        epilog=(
            "示例：kwey_mcp.py --vault \"D:/我的知识库\"\n"
            "交互示例（一行一条）：{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n"
            "说明：stdout 仅输出协议 JSON，日志/异常均在 stderr；Ctrl-D/EOF 优雅退出；不启动 watcher 线程。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vault", required=True, help="vault 根目录的绝对路径")
    args = parser.parse_args(argv)

    vault_root = Path(args.vault).expanduser().resolve()
    if not vault_root.is_dir():
        _log(f"[kwey-mcp] vault 目录不存在或不是目录: {vault_root}")
        return 2

    try:
        # 全量建索引；不调用 start_watcher，避免 stdio 场景残留线程。
        service = deps.init_services(vault_root, open_now=True)
    except Exception as exc:  # noqa: BLE001
        _log(f"[kwey-mcp] 初始化失败: {exc}")
        return 1

    server = KweyMCPServer(service)
    _log(f"[kwey-mcp] 已就绪 vault={vault_root} notes={service.db.count_notes()}（未启动 watcher）")

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                _log(f"[kwey-mcp] 无法解析 JSON: {exc}")
                _emit(_parse_error(f"Parse error: {exc}"))
                continue

            try:
                resp = server.handle(msg)
            except Exception as exc:  # noqa: BLE001 —— handle 已兜底，这里再兜一层
                _log(f"[kwey-mcp] handle 异常: {exc}")
                mid = msg.get("id") if isinstance(msg, dict) else None
                resp = {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }

            # JSON-RPC 通知（无 id）不应有响应体：仅记日志，不写 stdout。
            if isinstance(msg, dict) and "id" not in msg:
                _log(f"[kwey-mcp] 忽略通知: {msg.get('method')}")
                continue
            _emit(resp)
    except KeyboardInterrupt:
        _log("[kwey-mcp] 收到中断信号，准备退出")
    finally:
        _log("[kwey-mcp] EOF，退出")
        try:
            deps.reset_services()  # 优雅释放 SQLite 连接；未启动的 watcher.stop 应无副作用
        except Exception as exc:  # noqa: BLE001
            _log(f"[kwey-mcp] 清理服务时忽略异常: {exc}")
    return 0
