"""Kwey MCP Server（M2 / T11，卡A W1 改薄）：基于 stdio 的 JSON-RPC 2.0 传输层。

设计要点
--------
- 传输层与能力层分离：工具清单与语义一律来自 ``app.agent.registry``（契约 v1），
  本模块只负责「收到一条 JSON-RPC 消息 → 返回一条 JSON-RPC 响应」的 framing
  （``KweyMCPServer.handle``），stdio 读写由根目录 ``kwey_mcp.py`` 承担，
  便于在进程内直接实例化测试（不起子进程、不监听端口）。
- stdio 是本机父子进程间的私有管道，调用方即启动者，**无需 token 鉴权**；
  未来若改为 WS/SSE 或对外开放网络端口，才需要引入鉴权（见 ARCHITECTURE T11）。
- 所有笔记写操作一律经 ``VaultService``（registry.invoke 注入），禁止直接写盘；
  读操作可直接读盘（ARCHITECTURE §8.3），此处 resources/read 为返回字节级原文。
- 路径一律 vault 相对正斜杠；文件 I/O 显式 UTF-8（读取容忍 BOM）。
- 零新增第三方依赖：仅标准库 json/typing + 项目既有 fastapi 异常类。
- 结果约定：MCP 协议方法（initialize/tools/list/resources/*/ping）返回协议原生
  结构；工具执行结果的 ``content[0].text`` 采用项目统一内部响应风格
  ``{"code":0,"data":...,"message":"ok"}``（异常时 code 取 HTTP 状态码 / 500，
  data=null，isError=true），既对齐 Kwey 约定又不破坏 MCP 客户端解析。
- 错误映射（与 M2 完全一致）：未知 method → -32601；未知工具 → isError code=-1；
  参数校验失败（ContractError 400 / 缺必填）→ -32602；HTTPException → isError
  code=status；其它异常 → isError code=500；handle() 绝不抛穿。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app import __version__
from app.agent import registry
from app.agent.registry import ContractError
from app.api import deps
from app.paths import normalize_rel
from app.vault.service import VaultService

logger = logging.getLogger("kwey.mcp")

# JSON-RPC 2.0 标准错误码
RPC_INVALID_REQUEST = -32600   # 请求结构非法（非 dict / 缺 method）
RPC_METHOD_NOT_FOUND = -32601  # 未知 method
RPC_INVALID_PARAMS = -32602    # 参数不合法
RPC_INTERNAL_ERROR = -32603    # 兜底内部错误

RESOURCE_URI_PREFIX = "kwey://note/"

# stdio 调用方即本机父进程，等效 admin；journal 统一署名。
MCP_ACTOR = {"role": "admin", "name": "mcp-stdio"}


def _json_default(obj: Any) -> Any:
    """JSON 兜底：python-frontmatter 会把 YAML 日期解析成 datetime/date，需转 ISO 字符串。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


class _RpcError(Exception):
    """协议层错误：由 handle 顶层转换为 JSON-RPC ``error`` 对象。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KweyMCPServer:
    """无状态 JSON-RPC 处理器；持有（注入的）VaultService，能力执行委托 registry。"""

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_INFO = {"name": "kwey", "version": __version__}

    # 工具清单由契约生成（input_schema → MCP 惯例 inputSchema），单一真源。
    TOOLS: List[Dict[str, Any]] = [
        {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
        for t in registry.contract()["tools"]
    ]

    def __init__(self, vault: Optional[VaultService] = None) -> None:
        # stdio 本地进程无需鉴权：调用方即启动本进程的父进程。
        # 允许注入 vault 便于进程内测试；未注入时取 deps 单例（未打开会抛 400）。
        self.vault: VaultService = vault if vault is not None else deps.require_vault()
        self._tool_names = {t["name"] for t in self.TOOLS}

    # ------------------------------------------------------------------ JSON-RPC 顶层
    def handle(self, msg: dict) -> dict:
        """处理单条 JSON-RPC 2.0 消息，返回单条响应；任何异常都不抛穿。"""
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        try:
            if not isinstance(msg, dict):
                raise _RpcError(RPC_INVALID_REQUEST, "Invalid Request: 消息必须是对象")
            method = msg.get("method")
            if not isinstance(method, str) or not method:
                raise _RpcError(RPC_INVALID_REQUEST, "Invalid Request: 缺少 method")
            params = msg.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise _RpcError(RPC_INVALID_PARAMS, "Invalid params: params 必须是对象")

            result = self._dispatch(method, params)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底，绝不把异常抛给 stdio
            logger.exception("处理 JSON-RPC 消息失败: %s", msg)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": RPC_INTERNAL_ERROR, "message": f"Internal error: {exc}"},
            }

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        """按 method 分发；未知 method 抛 -32601。"""
        if method == "initialize":
            return {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": dict(self.SERVER_INFO),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.TOOLS}
        if method == "tools/call":
            return self._tools_call(params)
        if method == "resources/list":
            return self._resources_list()
        if method == "resources/read":
            return self._resources_read(params)
        raise _RpcError(RPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    # ------------------------------------------------------------------ 协议方法
    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具：返回 MCP CallToolResult；工具异常转 isError=true，不抛穿。"""
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(RPC_INVALID_PARAMS, "Invalid params: tools/call 缺少 name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(RPC_INVALID_PARAMS, "Invalid params: arguments 必须是对象")

        # 未知工具：属于「工具执行」层面的错误，按任务约定返回 isError=true，
        # 与 JSON-RPC 层的未知 method(-32601) 区分开。
        if name not in self._tool_names:
            return self._tool_error(f"未知工具: {name}", code=-1)

        # 必填参数预校验：缺失即 -32602（参数不合法）。registry 内还有一层完整
        # schema 校验（类型/enum/范围），此处保留 M2 的精确错误语义。
        schema = next(t["inputSchema"] for t in self.TOOLS if t["name"] == name)
        for required in schema.get("required", []):
            if required not in arguments or arguments[required] is None:
                raise _RpcError(
                    RPC_INVALID_PARAMS,
                    f"Invalid params: 工具 {name} 缺少必填参数 {required}",
                )

        try:
            data = registry.invoke(name, arguments, vault=self.vault, actor=MCP_ACTOR)
        except ContractError as exc:
            raise _RpcError(RPC_INVALID_PARAMS, exc.message) from exc
        except HTTPException as exc:
            # VaultService 的 404/409/400 属工具执行异常 → isError，不抛穿。
            return self._tool_error(str(exc.detail), code=exc.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具 %s 执行失败", name)
            return self._tool_error(str(exc), code=500)
        return self._tool_ok(data)

    def _resources_list(self) -> Dict[str, Any]:
        """把索引里的全部笔记暴露为 kwey://note/<path> 资源。"""
        notes = self.vault.db.all_notes()
        resources = [
            {
                "uri": f"{RESOURCE_URI_PREFIX}{note['path']}",
                "name": note.get("title") or note["path"],
                "mimeType": "text/markdown",
            }
            for note in notes
        ]
        return {"resources": resources}

    def _resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """读取资源原文（完整 Markdown 文本，含 frontmatter）。"""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith(RESOURCE_URI_PREFIX):
            raise _RpcError(RPC_INVALID_PARAMS, "Invalid params: 需要 kwey://note/<path> 形式的 uri")
        rel = uri[len(RESOURCE_URI_PREFIX):]
        if not rel:
            raise _RpcError(RPC_INVALID_PARAMS, "Invalid params: uri 中缺少笔记路径")
        try:
            # 读操作可直接读盘（ARCHITECTURE §8.3）；to_abs 内含安全校验，UTF-8 容忍 BOM。
            abs_path = self.vault.paths.to_abs(normalize_rel(rel))
            raw = abs_path.read_text(encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001 —— 路径非法/不存在统一转参数错误
            raise _RpcError(RPC_INVALID_PARAMS, f"Invalid params: 无法读取资源 {uri}: {exc}") from exc
        return {
            "contents": [
                {"uri": uri, "mimeType": "text/markdown", "text": raw},
            ]
        }

    # ------------------------------------------------------------------ 结果 framing
    @staticmethod
    def _tool_ok(data: Any) -> Dict[str, Any]:
        """工具成功结果：text 内为项目统一内部响应 {"code":0,"data":...,"message":"ok"}。"""
        payload = {"code": 0, "data": data, "message": "ok"}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=_json_default)}],
            "isError": False,
        }

    @staticmethod
    def _tool_error(message: str, code: int = 500) -> Dict[str, Any]:
        """工具失败结果：isError=true + 错误文本，不抛穿到 JSON-RPC 层。"""
        payload = {"code": code, "data": None, "message": message}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=_json_default)}],
            "isError": True,
        }
