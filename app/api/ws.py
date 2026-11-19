"""WebSocket /ws/events：EventBus → WebSocket 广播（M1 前端消费；M2 对 agent 复用）。

- 消息即 VaultEvent.to_dict()：{"event","path","old_path","ts"}。
- 心跳：服务端 25s 无事件时发 {"event":"ping"}；客户端可发 "ping" 收 {"event":"pong"}。
- A4 遗留落地（握手鉴权）：query 带 ``?token=`` 时严格校验三级 token；
  无效/过期 → close 4401（401）、权限不足 → close 4403（403）。未带 token 的
  本地回环连接（127.0.0.1 / ::1 / localhost / testclient）放行——本地 UI 的
  connectWS() 不带 token，且 M1 既有 WS 行为不得破坏；非回环且无 token →
  close 4401。安全边界：WS 仅广播事件元数据（path/old_path/ts），不含正文；
  外部 Agent 若要读正文须走带 token 的 REST/SSE（全端点已鉴权）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Set

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.api import deps
from app.api.guard import verify_ws_token
from app.events import EventBus, VaultEvent

logger = logging.getLogger("kwey.ws")

router = APIRouter(tags=["ws"])

HEARTBEAT_INTERVAL = 25.0
# 本地回环主机名（TestClient 用 "testclient"；本机浏览器用 localhost/127.0.0.1/::1）
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


class WsHub:
    """EventBus ↔ WebSocket 桥。线程安全（watcher 线程 → 事件线程桥接）。"""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.clients: Set[asyncio.Queue] = set()
        self._eventbus: Optional[EventBus] = None

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        """在事件循环内调用（app lifespan）。"""
        self.loop = asyncio.get_running_loop()

    def bind_eventbus(self) -> None:
        """订阅当前 EventBus（vault 切换后重新绑定）。"""
        if self._eventbus is not None:
            self._eventbus.unsubscribe(None, self._on_event)
        self._eventbus = deps.get_eventbus()
        if self._eventbus is not None:
            self._eventbus.subscribe(None, self._on_event)

    # ------------------------------------------------------------------ bridge
    def _on_event(self, event: VaultEvent) -> None:
        """EventBus 回调（可能在任意线程）→ 投递到事件循环。"""
        if self.loop is None or self.loop.is_closed():
            return
        data = event.to_dict()
        for queue in list(self.clients):
            try:
                self.loop.call_soon_threadsafe(queue.put_nowait, data)
            except RuntimeError:  # loop 已关闭
                pass

    # ------------------------------------------------------------------ session
    async def run_client(self, ws: WebSocket) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self.clients.add(queue)
        receiver: Optional[asyncio.Task] = None
        try:
            await ws.send_json({"event": "connected", "path": "", "old_path": None, "ts": time.time()})
            receiver = asyncio.create_task(self._receive_loop(ws))
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    await ws.send_json({"event": "ping", "path": "", "old_path": None, "ts": time.time()})
                    continue
                await ws.send_json(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("WS 客户端会话异常")
        finally:
            self.clients.discard(queue)
            if receiver is not None:
                receiver.cancel()

    @staticmethod
    async def _receive_loop(ws: WebSocket) -> None:
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_json({"event": "pong", "path": "", "old_path": None, "ts": time.time()})


hub = WsHub()


async def _reject_ws(websocket: WebSocket, code: int, message: str) -> None:
    """拒绝握手：accept 后发一帧 error 再 close（便于客户端读到原因），不等 receive。"""
    await websocket.accept()
    try:
        await websocket.send_json({"event": "error", "code": code, "message": message})
    except (RuntimeError, WebSocketDisconnect):
        pass
    try:
        await websocket.close(code=4401 if code == 401 else 4403)
    except RuntimeError:
        pass


@router.websocket("/ws/events")
async def ws_events(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None, description="可选：agent token；带则严格校验"),
):
    """渐进式鉴权：带 token → 严格校验；无 token → 仅本地回环放行。"""
    if token is not None and token.strip():
        try:
            verify_ws_token(token, "read")
        except HTTPException as exc:
            await _reject_ws(websocket, exc.status_code, str(exc.detail))
            return
    else:
        client_host = websocket.client.host if websocket.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            await _reject_ws(websocket, 401, "远程连接必须提供 ?token=")
            return
    await websocket.accept()
    await hub.run_client(websocket)
