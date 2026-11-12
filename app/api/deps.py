"""依赖注入：vault 服务族单例的创建 / 获取 / 重置。

生命周期：进程内单例。``init_services`` 在 POST /api/vault/open 或 --vault 启动时调用；
``reset_services`` 关闭旧实例（watcher 停止、SQLite 连接关闭）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from app.config import AppSettings
from app.events import EventBus
from app.index.database import IndexDatabase
from app.index.indexer import Indexer
from app.index.linkgraph import LinkGraph
from app.paths import AppPaths
from app.vault.links import LinkRewriter
from app.vault.service import VaultService
from app.vault.watcher import VaultWatcher

_lock = threading.RLock()

vault_service: Optional[VaultService] = None
vault_config: Optional[AppSettings] = None
vault_eventbus: Optional[EventBus] = None
vault_db: Optional[IndexDatabase] = None
vault_indexer: Optional[Indexer] = None
vault_linkgraph: Optional[LinkGraph] = None
vault_watcher: Optional[VaultWatcher] = None
vault_rewriter: Optional[LinkRewriter] = None


def init_services(root: Path, open_now: bool = True) -> VaultService:
    """为指定 vault 根目录构建整条服务链；若已有实例先重置。

    Args:
        root: vault 绝对路径。
        open_now: 是否立即执行全量扫描建索引（INDEX_READY）。
    """
    global vault_service, vault_config, vault_eventbus, vault_db
    global vault_indexer, vault_linkgraph, vault_watcher, vault_rewriter

    with _lock:
        reset_services()
        root = Path(root).resolve()
        cfg = AppSettings(root).load()
        bus = EventBus()
        db = IndexDatabase(root)
        paths = AppPaths(root)
        graph = LinkGraph()
        indexer = Indexer(paths, db, graph)
        rewriter = LinkRewriter(paths, db, graph)
        service = VaultService(root, cfg, bus, db, indexer, rewriter)
        watcher = VaultWatcher(service)
        service.bind_watcher(watcher)

        if open_now:
            service.open_vault()

        vault_config, vault_eventbus, vault_db = cfg, bus, db
        vault_indexer, vault_linkgraph = indexer, graph
        vault_service, vault_watcher, vault_rewriter = service, watcher, rewriter
        return service


def reset_services() -> None:
    """停止 watcher、关闭数据库连接、清空单例（vault 切换/退出时调用）。"""
    global vault_service, vault_config, vault_eventbus, vault_db
    global vault_indexer, vault_linkgraph, vault_watcher, vault_rewriter

    with _lock:
        if vault_watcher is not None:
            vault_watcher.stop()
        if vault_db is not None:
            vault_db.close()
        vault_service = None
        vault_config = None
        vault_eventbus = None
        vault_db = None
        vault_indexer = None
        vault_linkgraph = None
        vault_watcher = None
        vault_rewriter = None


def _require(name: str, value):
    if value is None:
        raise HTTPException(status_code=400, detail=f"vault 未打开（{name} 不可用）")
    return value


def get_vault() -> Optional[VaultService]:
    return vault_service


def require_vault() -> VaultService:
    return _require("VaultService", vault_service)


def get_config() -> Optional[AppSettings]:
    return vault_config


def require_config() -> AppSettings:
    return _require("AppSettings", vault_config)


def get_eventbus() -> Optional[EventBus]:
    return vault_eventbus


def require_eventbus() -> EventBus:
    return _require("EventBus", vault_eventbus)


def get_db() -> Optional[IndexDatabase]:
    return vault_db


def require_db() -> IndexDatabase:
    return _require("IndexDatabase", vault_db)


def get_indexer() -> Optional[Indexer]:
    return vault_indexer


def require_indexer() -> Indexer:
    return _require("Indexer", vault_indexer)


def get_linkgraph() -> Optional[LinkGraph]:
    return vault_linkgraph


def require_linkgraph() -> LinkGraph:
    return _require("LinkGraph", vault_linkgraph)


def get_watcher() -> Optional[VaultWatcher]:
    return vault_watcher
