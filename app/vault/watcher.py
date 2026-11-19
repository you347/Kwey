"""watchfiles 防抖监听 → 自写去重 → 增量索引 → 广播外部变更事件（风险点①）。

- 防抖参数来自 settings（watch_debounce_ms / watch_step_ms）。
- 自写文件（VaultService 落盘记录 + 时间窗口）跳过，杜绝回环。
- 外部改动只增量更新受影响索引，不触发全库重扫（P0-1 验收）。
- ``_process_changes`` 独立暴露，供单测在不启动真实监听线程的情况下验证。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Optional

from app.events import EventType, VaultEvent
from app.paths import is_temp_path
from app.vault.service import VaultService

logger = logging.getLogger("kwey.watcher")

try:
    from watchfiles import Change, watch
    HAS_WATCHFILES = True
except ImportError:  # pragma: no cover - watchfiles 是必装依赖，此分支仅兜底
    Change = None
    watch = None
    HAS_WATCHFILES = False


class VaultWatcher:
    """vault 文件监听器（后台线程）。"""

    def __init__(self, service: VaultService) -> None:
        self.service = service
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not HAS_WATCHFILES:
            logger.warning("watchfiles 未安装，文件监听不可用")
            return
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kwey-watcher", daemon=True)
        self._thread.start()
        self.running = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self.running = False

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        cfg = self.service.config
        debounce_ms = int(cfg.get("watch_debounce_ms", 300))
        step_ms = int(cfg.get("watch_step_ms", 400))
        try:
            for changes in watch(
                self.service.paths.root,
                watch_filter=self._filter,
                debounce=debounce_ms,
                step=step_ms,
                stop_event=self._stop,
            ):
                if self._stop.is_set():
                    break
                self._process_changes(changes)
        except Exception:  # noqa: BLE001
            if not self._stop.is_set():
                logger.exception("watcher 线程异常退出")
        finally:
            self.running = False

    def _filter(self, change, path_str: str) -> bool:
        """watchfiles 过滤：忽略 .kwey/.trash/.git 与 *.kwey-tmp 临时文件（B5）。"""
        try:
            rel = self.service.paths.to_rel(Path(path_str))
        except Exception:  # noqa: BLE001
            return False
        return not is_temp_path(rel)

    # ------------------------------------------------------------------ handling
    def _process_changes(self, changes) -> None:
        """处理一批（防抖后）变更：自写去重 → 增量索引 → 广播。

        兼容两种 watchfiles 产出：
        - dict {Path: Change}（单元测试直接构造）
        - set {(Change, Path), ...}（watch() 实际产出）
        """
        if isinstance(changes, dict):
            pairs = [(p, c) for p, c in changes.items()]
        else:
            pairs = [(p, c) for c, p in changes]
        for abs_path, change in sorted(pairs, key=lambda pc: str(pc[0])):
            try:
                rel = self.service.paths.to_rel(abs_path)
            except Exception:  # noqa: BLE001
                continue
            if is_temp_path(rel):
                continue  # B5：临时文件/保留目录不索引、不广播
            # 自写去重：VaultService 落盘记录过的事件直接跳过
            if self.service.is_self_write(rel):
                continue
            self._handle_one(rel, change)

    def _handle_one(self, rel: str, change) -> None:
        is_md = rel.lower().endswith((".md", ".markdown"))
        service = self.service
        if change == Change.deleted:
            if is_md:
                service.indexer.remove_note(rel, break_links=True)
            else:
                service.indexer.remove_file_entry(rel)
            service.eventbus.publish(VaultEvent(EventType.DELETED, path=rel))
            return

        # B6：目录级新增/修改事件不是文件，不得登记进链接图 files 表
        if not service.paths.to_abs(rel).is_file():
            return

        existed = service.db.get_note(rel) is not None
        if is_md:
            service.indexer.reindex_note(rel)
        else:
            service.indexer.linkgraph.upsert_file(rel)
        event_type = EventType.UPDATED if existed else EventType.CREATED
        service.eventbus.publish(VaultEvent(event_type, path=rel))
