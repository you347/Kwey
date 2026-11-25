"""依赖注入：vault 服务族单例的创建 / 获取 / 重置。

生命周期：进程内单例。``init_services`` 在 POST /api/vault/open 或 --vault 启动时调用；
``reset_services`` 关闭旧实例（watcher 停止、SQLite 连接关闭）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from app.agent.provider import AgentProvider
from app.config import AppSettings
from app.duty.approval import ApprovalGrants
from app.duty.apply import ProposalApplier
from app.duty.links import IndexDutyLinks
from app.duty.reader import VaultDutyReader
from app.duty.runtime import DutyRuntime
from app.duty.store import DutyStore
from app.duty.supervisor import DutySupervisor
from app.events import EventBus
from app.index.database import IndexDatabase
from app.index.indexer import Indexer
from app.index.linkgraph import LinkGraph
from app.paths import AppPaths
from app.vault.links import LinkRewriter
from app.vault.service import VaultService
from app.vault.snapshots import SnapshotService
from app.vault.watcher import VaultWatcher

logger = logging.getLogger("kwey.deps")
_lock = threading.RLock()

vault_service: Optional[VaultService] = None
vault_config: Optional[AppSettings] = None
vault_eventbus: Optional[EventBus] = None
vault_db: Optional[IndexDatabase] = None
vault_indexer: Optional[Indexer] = None
vault_linkgraph: Optional[LinkGraph] = None
vault_watcher: Optional[VaultWatcher] = None
vault_rewriter: Optional[LinkRewriter] = None
vault_snapshots: Optional[SnapshotService] = None

# A0 值班接线：控制存储 / 只读读取器 / 运行时 / supervisor（Timer+worker+审批）
vault_duty_store: Optional[DutyStore] = None
vault_duty_reader: Optional[VaultDutyReader] = None
vault_duty_runtime: Optional[DutyRuntime] = None
vault_duty_supervisor: Optional[DutySupervisor] = None

# create_app 在启动时设置：纯 --server 模式无桌面原生审批桥，apply 默认关闭。
_runtime_mode = "server"


def set_runtime_mode(mode: str) -> str:
    """由 create_app 调用；desktop 模式才有 pywebview 原生审批桥。"""
    global _runtime_mode
    _runtime_mode = "desktop" if str(mode) == "desktop" else "server"
    return _runtime_mode


def get_runtime_mode() -> str:
    return _runtime_mode


def init_services(root: Path, open_now: bool = True) -> VaultService:
    """为指定 vault 根目录构建整条服务链；若已有实例先重置。

    Args:
        root: vault 绝对路径。
        open_now: 是否立即执行全量扫描建索引（INDEX_READY）。
    """
    global vault_service, vault_config, vault_eventbus, vault_db
    global vault_indexer, vault_linkgraph, vault_watcher, vault_rewriter
    global vault_snapshots
    global vault_duty_store, vault_duty_reader, vault_duty_runtime
    global vault_duty_supervisor

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

        # M2：git 快照服务。默认关闭，开启时才在 vault 内初始化 .git；
        # 初始化失败不影响 vault 打开（快照是增强能力，不是事实源）。
        snap_cfg = cfg.get("snapshots") or {}
        snapshots = SnapshotService(
            root, vault=service, enabled=bool(snap_cfg.get("enabled", False))
        )
        service.bind_snapshots(snapshots)
        if snapshots.enabled:
            try:
                snapshots.ensure_repo()
            except Exception:  # noqa: BLE001
                logger.exception("git 快照初始化失败，已降级为不记录快照")

        if open_now:
            service.open_vault()

        vault_config, vault_eventbus, vault_db = cfg, bus, db
        vault_indexer, vault_linkgraph = indexer, graph
        vault_service, vault_watcher, vault_rewriter = service, watcher, rewriter
        vault_snapshots = snapshots

        # A0 值班装配：store / reader / runtime / supervisor（不启动线程，
        # 由 server lifespan 或 POST /api/vault/open 显式 start_duty）。
        vault_duty_store = vault_duty_reader = vault_duty_runtime = None
        vault_duty_supervisor = None
        try:
            duty_store = DutyStore.for_vault(root)
            duty_reader = VaultDutyReader(service, db)
            inbox_cfg = cfg.get("inbox") or {}
            duty_dirs = {
                "inbox": str(inbox_cfg.get("dir") or "inbox").strip("/") or "inbox",
                "daily": str(cfg.get("daily_dir") or "daily").strip("/") or "daily",
                "report": "reports",
            }
            duty_runtime = DutyRuntime(
                store=duty_store,
                reader=duty_reader,
                dirs=duty_dirs,
                generation=1,
                links=IndexDutyLinks(rewriter, graph),
            )
            grants = ApprovalGrants()
            applier = ProposalApplier(
                store=duty_store,
                vault=service,
                reader=duty_reader,
                grants=grants,
                runtime=duty_runtime,
            )
            supervisor = DutySupervisor(
                store=duty_store,
                reader=duty_reader,
                runtime=duty_runtime,
                vault=service,
                applier=applier,
                grants=grants,
                mode=_runtime_mode,
                provider_factory=lambda: AgentProvider(root),
            )
            vault_duty_store = duty_store
            vault_duty_reader = duty_reader
            vault_duty_runtime = duty_runtime
            vault_duty_supervisor = supervisor
        except Exception:  # noqa: BLE001 —— 值班是增强能力，不阻断 vault 打开
            logger.exception("值班服务装配失败，已降级为不可用")
            if vault_duty_store is not None:
                try:
                    vault_duty_store.close()
                except Exception:  # noqa: BLE001
                    pass
            vault_duty_store = vault_duty_reader = vault_duty_runtime = None
            vault_duty_supervisor = None
        return service


def reset_services() -> None:
    """停止 watcher、关闭数据库连接、清空单例（vault 切换/退出时调用）。"""
    global vault_service, vault_config, vault_eventbus, vault_db
    global vault_indexer, vault_linkgraph, vault_watcher, vault_rewriter
    global vault_snapshots
    global vault_duty_store, vault_duty_reader, vault_duty_runtime
    global vault_duty_supervisor

    with _lock:
        # 先停值班：解绑旧 bus、bump generation、停 worker，再关库。
        if vault_duty_supervisor is not None:
            try:
                vault_duty_supervisor.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止值班 supervisor 失败")
        if vault_duty_store is not None:
            try:
                vault_duty_store.close()
            except Exception:  # noqa: BLE001
                pass
        vault_duty_store = None
        vault_duty_reader = None
        vault_duty_runtime = None
        vault_duty_supervisor = None

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
        vault_snapshots = None


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


def get_snapshots() -> Optional[SnapshotService]:
    return vault_snapshots


def require_snapshots() -> SnapshotService:
    """快照单例 + 运行时刷新（设置开关 / bind_vault / 补初始化 repo）。

    原 routes_history._get_snapshot 的按 root 缓存收口到此：init_services
    重建服务族时单例自然更替，无需独立缓存。enabled 以 settings 为准
    （缺省关闭，不擅自 git init 用户 vault），支持设置页运行时开启。
    """
    svc = _require("SnapshotService", vault_snapshots)
    vault = require_vault()
    svc.bind_vault(vault)
    cfg = get_config()
    raw = cfg.get("snapshots") if cfg is not None else None
    enabled = bool(raw.get("enabled", False)) if isinstance(raw, dict) else False
    svc.enabled = enabled
    if enabled and not (svc.root / ".git").exists():
        svc.ensure_repo()
    return svc


# ---------------------------------------------------------------------- 值班 A0
def get_duty_store() -> Optional[DutyStore]:
    return vault_duty_store


def require_duty_store() -> DutyStore:
    return _require("DutyStore", vault_duty_store)


def get_duty_reader() -> Optional[VaultDutyReader]:
    return vault_duty_reader


def require_duty_reader() -> VaultDutyReader:
    return _require("VaultDutyReader", vault_duty_reader)


def get_duty_runtime() -> Optional[DutyRuntime]:
    return vault_duty_runtime


def require_duty_runtime() -> DutyRuntime:
    return _require("DutyRuntime", vault_duty_runtime)


def get_duty_supervisor() -> Optional[DutySupervisor]:
    return vault_duty_supervisor


def require_duty_supervisor() -> DutySupervisor:
    return _require("DutySupervisor", vault_duty_supervisor)


def start_duty() -> None:
    """启动 Timer / worker / EventBus 订阅；已启动则幂等。"""
    supervisor = vault_duty_supervisor
    if supervisor is not None:
        supervisor.start()


def stop_duty() -> None:
    """退出 / 切库：停 worker、解绑旧 bus、bump generation。幂等。"""
    supervisor = vault_duty_supervisor
    if supervisor is not None:
        supervisor.stop()
