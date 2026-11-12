"""事件总线：内存 pub/sub。

- 生产方只调 ``EventBus.publish(VaultEvent)``（VaultService / Watcher）。
- 消费方 ``subscribe(event_type, handler)``；``event_type=None`` 订阅全部。
- 事件只描述“发生了什么”（类型/path），不携带全文——消费方按需回读 API。
- 广播 JSON 格式：{"event": "...", "path": "...", "old_path": "...", "ts": ...}
- M1 内部消费（WS 出口 ws.py）；M2 直接对 agent 暴露（T11 预留）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Union

logger = logging.getLogger("kwey.events")


class EventType(str, Enum):
    """库变更事件类型（与 DESIGN.md 推送格式对齐）。"""

    CREATED = "created"          # 新建笔记/附件
    UPDATED = "updated"          # 内容/属性变更
    DELETED = "deleted"          # 删除（进 .trash/）
    MOVED = "moved"              # 改名/移动（payload 携带 old_path/new_path）
    LINKS_CHANGED = "links_changed"  # 链接图变化
    INDEX_READY = "index_ready"  # 首次扫描/整库重建完成


@dataclass
class VaultEvent:
    """单条库变更事件。"""

    event: EventType
    path: str = ""
    old_path: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "event": self.event.value if isinstance(self.event, EventType) else str(self.event),
            "path": self.path,
            "old_path": self.old_path,
            "ts": self.ts,
        }


Handler = Callable[[VaultEvent], None]


class EventBus:
    """线程安全内存 pub/sub。handler 同步调用，单 handler 异常不影响其他。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[Optional[str], List[Handler]] = {}

    def subscribe(
        self,
        event_type: Optional[Union[EventType, str]],
        handler: Handler,
    ) -> None:
        """注册 handler；event_type 为 None 表示订阅全部事件。"""
        key = event_type.value if isinstance(event_type, EventType) else event_type
        with self._lock:
            self._subscribers.setdefault(key, []).append(handler)

    def unsubscribe(self, event_type: Optional[Union[EventType, str]], handler: Handler) -> None:
        key = event_type.value if isinstance(event_type, EventType) else event_type
        with self._lock:
            handlers = self._subscribers.get(key, [])
            if handler in handlers:
                handlers.remove(handler)

    def publish(self, event: VaultEvent) -> None:
        """同步分发事件到所有匹配订阅者（含全事件订阅者）。"""
        key = event.event.value if isinstance(event.event, EventType) else str(event.event)
        with self._lock:
            handlers = list(self._subscribers.get(key, []))
            handlers += list(self._subscribers.get(None, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 —— 单个订阅者异常不阻断广播
                logger.exception("事件处理器异常: %s", key)
