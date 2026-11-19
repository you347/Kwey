"""统一 token 鉴权入口（A4 权限治理）。

Agent Link 全部端点的鉴权收口到本模块：

- ``extract_token(request)``：从 ``X-Kwey-Token``（优先）或 ``Authorization: Bearer`` 提取。
- ``require_token(request, required_role="read")``：REST 端点统一鉴权，返回脱敏记录
  ``{"role","name","created"}``；无 / 无效 token → 401，权限不足 → 403，
  vault 未打开 → 400（token 库存于 vault 内，属前置条件）。
- ``verify_ws_token(token, required_role="read")``：WebSocket 握手专用（浏览器无法带
  自定义 header，token 经 query 参数传入）；失败同样抛 ``HTTPException``，
  由接线方 try/except 后发错误帧并 close（集成卡补丁见 A4 报告）。

错误统一 ``fastapi.HTTPException``，由 server.py 异常处理器包装为 ``{code,data,message}``。
``routes_agent.py`` 内的 ``_auth`` 为既有等价实现（行为一致），集成卡可择机替换为
``guard.require_token``——本卡按任务边界不改该文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request

from app.agent.auth import TokenStore
from app.api import deps

_BEARER_PREFIX = "bearer "


def extract_token(request: Request) -> str:
    """从请求头提取 token；优先级 X-Kwey-Token > Authorization: Bearer <token>。"""
    token = (request.headers.get("X-Kwey-Token") or "").strip()
    if token:
        return token
    auth = (request.headers.get("Authorization") or "").strip()
    if auth[:7].lower() == _BEARER_PREFIX:
        return auth[7:].strip()
    return ""


def _store() -> TokenStore:
    """当前 vault 的 TokenStore（无内存态，每次轻量实例；共享全局锁见 auth.py）。"""
    vault = deps.require_vault()  # vault 未打开 → 400，先于鉴权（无库可查）
    return TokenStore(Path(vault.paths.root))


def require_token(request: Request, required_role: str = "read") -> Dict[str, Any]:
    """REST 端点统一鉴权：命中返回脱敏记录（role/name/created），否则抛 401/403。"""
    return _store().verify(extract_token(request), required_role)


def verify_ws_token(token: str, required_role: str = "read") -> Dict[str, Any]:
    """WebSocket 握手鉴权：token 来自 query 参数（或首帧），无 Request 对象。

    vault 未打开 → 400；无 / 无效 token → 401；权限不足 → 403。
    WS 端点内应 try/except：accept 后 ``send_json({"event":"error","code":<status>})``
    再 ``close(code=4401/4403)``（补丁片段见 A4 报告，本卡不改 ws.py）。
    """
    return _store().verify((token or "").strip(), required_role)
