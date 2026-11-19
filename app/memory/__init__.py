"""F3 个人信息库（记忆层）。

- :class:`MemoryStore`：``<vault>/.kwey/memory/`` 三分区（projects / profile / lessons）
  的读写、50/30/20 配额注入、去重合并与衰减。
- :mod:`app.api.routes_memory`：UI 面板与 A6/organize 使用的 REST 端点。

记忆文件是**普通 Markdown**，用户可直接查看 / 编辑 / 删除；``.kwey`` 已被
``app.paths.IGNORED_DIRS`` 排除，不会进入笔记树、全文索引与 watcher 事件。
"""

from app.memory.store import (
    SECTIONS,
    SECTION_LABELS,
    SECTION_QUOTAS,
    MemoryEntry,
    MemoryStore,
)

__all__ = [
    "MemoryStore",
    "MemoryEntry",
    "SECTIONS",
    "SECTION_LABELS",
    "SECTION_QUOTAS",
]
