"""WebSocket /ws/events：EventBus → WebSocket 广播（M1 前端消费；M2 对 agent 复用）。

- 消息即 VaultEvent.to_dict()：{"event","path","old_path","ts"}。
- 心跳：服务端 25s 无事件时发 {"event":"ping"}；客户端可发 "ping" 收 {"event":"pong"}。
- M2 预留：届时在握手处加 token 鉴权字段位。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api import deps
from app.events import EventBus, VaultEvent

logger = logging.getLogger("kwey.ws")

router = APIRouter(tags=["ws"])

HEARTBEAT_INTERVAL = 25.0


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


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    await hub.run_client(websocket)
