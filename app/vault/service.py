"""VaultService：全库唯一写路径（G5 核心）。

任何进程内代码（UI API、未来 agent）写 vault 必须经此，禁止直接 open()/Path.write_text()。

写流程（时序 5.1）：
    RLock 加锁 → paths.validate 安全校验 → 临时文件 + os.replace 原子写盘
    → 记录自写路径+时间戳（防 watcher 回环）→ reindex_note 增量索引
    → EventBus 广播（UPDATED / MOVED / DELETED / LINKS_CHANGED / INDEX_READY）

领域异常统一抛 fastapi HTTPException（由 server.py 包装为 {code,data,message}）：
    404 不存在 / 409 已存在或 mtime 冲突 / 400 路径非法。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.config import AppSettings
from app.events import EventBus, EventType, VaultEvent
from app.index.database import IndexDatabase
from app.index.indexer import Indexer
from app.parser import frontmatter as fm_mod
from app.parser.wikilink import parse_wikilink_text, split_anchor
from app.paths import (
    AppPaths,
    PathError,
    is_ignored,
    normalize_rel,
    sanitize_filename,
    validate_relative,
)
from app.vault.links import LinkRewriter

MARKDOWN_EXTS = {".md", ".markdown"}


def _validate(rel: str) -> str:
    """路径校验：PathError 统一转 400 HTTPException。"""
    try:
        return validate_relative(rel)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class VaultService:
    """vault 门面：读接口 + 唯一写路径 + 文件管理。"""

    SELF_WRITE_WINDOW = 15.0  # 自写记录有效期（秒），防 watcher 回环

    def __init__(
        self,
        root: Path,
        config: AppSettings,
        eventbus: EventBus,
        db: IndexDatabase,
        indexer: Indexer,
        rewriter: LinkRewriter,
    ) -> None:
        self.paths = AppPaths(root)
        self.config = config
        self.eventbus = eventbus
        self.db = db
        self.indexer = indexer
        self.rewriter = rewriter
        self.lock = threading.RLock()
        self.recent_self_writes: Dict[str, float] = {}
        self._watcher = None  # 由 deps 装配；start_watcher/stop_watcher 转发

    # ------------------------------------------------------------------ lifecycle
    def bind_watcher(self, watcher) -> None:
        self._watcher = watcher

    def start_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.start()

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()

    def open_vault(self) -> Dict[str, Any]:
        """全量扫描建索引，广播 INDEX_READY，返回 vault 信息。"""
        with self.lock:
            count = self.indexer.full_scan()
            self.eventbus.publish(VaultEvent(EventType.INDEX_READY, path=""))
            info = self.info()
            info["note_count"] = count
            return info

    def info(self) -> Dict[str, Any]:
        return {
            "name": self.paths.name,
            "path": str(self.paths.root) if self.paths.root else "",
            "note_count": self.db.count_notes(),
            "tag_count": len(self.db.tag_counts()),
        }

    # ------------------------------------------------------------------ self-write
    def _mark_self_write(self, rel: str) -> None:
        self.recent_self_writes[normalize_rel(rel)] = time.time()

    def is_self_write(self, rel: str) -> bool:
        """watcher 回环防护：命中自写记录则消费掉并返回 True。"""
        rel = normalize_rel(rel)
        now = time.time()
        # 清理过期记录
        expired = [k for k, ts in self.recent_self_writes.items() if now - ts > self.SELF_WRITE_WINDOW]
        for k in expired:
            self.recent_self_writes.pop(k, None)
        ts = self.recent_self_writes.pop(rel, None)
        return ts is not None and (now - ts) <= self.SELF_WRITE_WINDOW

    # ------------------------------------------------------------------ io helpers
    @staticmethod
    def _read_text(abs_path: Path) -> str:
        return abs_path.read_text(encoding="utf-8-sig")

    def _atomic_write(self, abs_path: Path, text: str) -> None:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = abs_path.with_name(abs_path.name + ".kwey-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, abs_path)

    def _atomic_write_bytes(self, abs_path: Path, data: bytes) -> None:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = abs_path.with_name(abs_path.name + ".kwey-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, abs_path)

    # ------------------------------------------------------------------ read
    def read_note(self, rel: str) -> Dict[str, Any]:
        rel = _validate(rel)
        abs_path = self.paths.to_abs(rel)
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
        raw = self._read_text(abs_path)
        fm, body = fm_mod.parse_frontmatter(raw)
        title = str(fm.get("title") or "").strip() or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return {
            "path": rel,
            "title": title,
            "content": body,
            "frontmatter": fm,
            "mtime": abs_path.stat().st_mtime,
        }

    # ------------------------------------------------------------------ create
    def create_note(
        self,
        rel: str,
        content: str = "",
        frontmatter: Optional[Dict[str, Any]] = None,
        dedup: bool = False,
    ) -> Dict[str, Any]:
        with self.lock:
            rel = _validate(rel)
            if not rel.lower().endswith(tuple(MARKDOWN_EXTS)):
                rel += ".md"
            abs_path = self.paths.to_abs(rel)
            if abs_path.exists():
                if not dedup:
                    raise HTTPException(status_code=409, detail=f"笔记已存在: {rel}")
                rel = AppPaths.unique_path(rel, exists_fn=lambda p: self.paths.to_abs(p).exists())
                abs_path = self.paths.to_abs(rel)

            title = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            fm = dict(frontmatter or {})
            fm = fm_mod.normalize_frontmatter_for_write(fm, is_new=True)
            if content == "":
                content = f"# {title}\n\n"
            text = fm_mod.dump_frontmatter(fm, content)
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            self.indexer.reindex_note(rel)
            revived = self.indexer.revive_for(rel)
            self.eventbus.publish(VaultEvent(EventType.CREATED, path=rel))
            if revived:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            meta = self.db.get_note(rel) or {}
            meta["path"] = rel
            meta["title"] = str(fm.get("title") or title)
            return meta

    # ------------------------------------------------------------------ write
    def write_note(
        self,
        rel: str,
        content: str,
        frontmatter: Optional[Dict[str, Any]] = None,
        base_mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        """统一写路径。base_mtime 乐观锁：与磁盘不符抛 409。"""
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")

            disk_mtime = abs_path.stat().st_mtime
            if base_mtime is not None and abs(disk_mtime - float(base_mtime)) > 1e-6:
                raise HTTPException(
                    status_code=409,
                    detail=f"笔记已被外部修改（mtime 冲突），请刷新后重试: {rel}",
                )

            if frontmatter is None:
                frontmatter, _ = fm_mod.parse_frontmatter(self._read_text(abs_path))
            fm = fm_mod.normalize_frontmatter_for_write(dict(frontmatter), is_new=False)
            text = fm_mod.dump_frontmatter(fm, content)
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            links_changed = self.indexer.reindex_note(rel)
            self.eventbus.publish(VaultEvent(EventType.UPDATED, path=rel))
            if links_changed:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            return {
                "updated": True,
                "links_changed": links_changed,
                "mtime": abs_path.stat().st_mtime,
            }

    def write_raw(self, rel: str, text: str, publish_event: bool = True) -> bool:
        """原样写文本（链接重写器内部使用）：统一锁/原子写/自写标记/增量索引。"""
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                return False
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            links_changed = self.indexer.reindex_note(rel)
            if publish_event:
                self.eventbus.publish(VaultEvent(EventType.UPDATED, path=rel))
                if links_changed:
                    self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            return True

    # ------------------------------------------------------------------ move/rename
    def rename_note(self, rel: str, new_name: str) -> Dict[str, Any]:
        rel = _validate(rel)
        new_name = sanitize_filename(new_name)
        if not new_name.lower().endswith(tuple(MARKDOWN_EXTS)):
            new_name += ".md"
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        new_rel = f"{parent}/{new_name}" if parent else new_name
        return self._move(rel, new_rel)

    def move_note(self, rel: str, new_dir: str) -> Dict[str, Any]:
        rel = _validate(rel)
        new_dir = normalize_rel(new_dir)
        basename = rel.rsplit("/", 1)[-1]
        new_rel = f"{new_dir}/{basename}" if new_dir else basename
        return self._move(rel, new_rel)

    def _move(self, rel: str, new_rel: str) -> Dict[str, Any]:
        with self.lock:
            if rel == new_rel:
                return {"new_path": rel, "rewritten_links": 0}
            new_rel = _validate(new_rel)
            old_abs = self.paths.to_abs(rel)
            new_abs = self.paths.to_abs(new_rel)
            if not old_abs.is_file():
                raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
            if new_abs.exists():
                raise HTTPException(status_code=409, detail=f"目标路径已存在: {new_rel}")
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_abs, new_abs)
            self._mark_self_write(rel)
            self._mark_self_write(new_rel)

            # 索引迁移：旧条目移除、新条目建立（含内存图）
            self.indexer.remove_note(rel)
            self.indexer.reindex_note(new_rel)
            # 复活此前无法解析、现在指向新路径的链接
            revived = self.indexer.revive_for(new_rel)
            # 全库重写指向旧路径的链接（含 ![[嵌入]] 与附件路径，0 断链）
            rewritten = self.rewriter.rewrite_on_rename(rel, new_rel, writer=self)

            self.eventbus.publish(
                VaultEvent(EventType.MOVED, path=new_rel, old_path=rel)
            )
            if rewritten or revived:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=new_rel, old_path=rel))
            return {"new_path": new_rel, "rewritten_links": rewritten + revived}

    # ------------------------------------------------------------------ delete
    def delete_note(self, rel: str) -> str:
        """删除笔记 → 移入 .trash/（保持相对结构，重名追加序号）。返回 trash 相对路径。"""
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
            trash_rel = AppPaths.unique_path(
                f".trash/{rel}", exists_fn=lambda p: (self.paths.root / p).exists()
            )
            trash_abs = self.paths.root / trash_rel
            trash_abs.parent.mkdir(parents=True, exist_ok=True)
            os.replace(abs_path, trash_abs)
            self._mark_self_write(rel)
            self._mark_self_write(trash_rel)
            self.indexer.remove_note(rel, break_links=True)
            self.eventbus.publish(VaultEvent(EventType.DELETED, path=rel))
            return trash_rel

    def delete_dir(self, rel: str, confirm: bool = False) -> str:
        """删除目录 → 整目录移入 .trash/。非空目录需 confirm=True。"""
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_dir():
                raise HTTPException(status_code=404, detail=f"目录不存在: {rel}")
            if not confirm and any(abs_path.rglob("*")):
                raise HTTPException(status_code=409, detail="目录非空，需要 confirm 确认")

            # B3 修复：移动前枚举目录内全部文件（笔记 + 附件等），逐一清理索引与链接图
            child_rels: List[str] = []
            for p in abs_path.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    child_rel = self.paths.to_rel(p)
                except Exception:  # noqa: BLE001
                    continue
                if is_ignored(child_rel) or child_rel.endswith(".kwey-tmp"):
                    continue
                child_rels.append(child_rel)

            trash_rel = AppPaths.unique_path(
                f".trash/{rel}", exists_fn=lambda p: (self.paths.root / p).exists()
            )
            trash_abs = self.paths.root / trash_rel
            trash_abs.parent.mkdir(parents=True, exist_ok=True)
            os.replace(abs_path, trash_abs)
            self._mark_self_write(rel)
            self._mark_self_write(trash_rel)

            # 清理索引与内存图：笔记（含断链标记）+ 附件等非笔记文件的图登记（B3）
            for child_rel in sorted(child_rels):
                if child_rel.lower().endswith(tuple(MARKDOWN_EXTS)):
                    self.indexer.remove_note(child_rel, break_links=True)
                else:
                    self.indexer.remove_file_entry(child_rel)
                self.eventbus.publish(VaultEvent(EventType.DELETED, path=child_rel))
            self.eventbus.publish(VaultEvent(EventType.DELETED, path=rel))
            return trash_rel

    # ------------------------------------------------------------------ dirs
    def create_dir(self, rel: str) -> str:
        rel = _validate(rel)
        abs_path = self.paths.to_abs(rel)
        if abs_path.exists():
            raise HTTPException(status_code=409, detail=f"目录已存在: {rel}")
        abs_path.mkdir(parents=True, exist_ok=True)
        return rel

    # ------------------------------------------------------------------ attachments
    def save_attachment(self, filename: str, data: bytes) -> str:
        """保存附件到配置目录（重名自动追加序号），登记进链接图。"""
        with self.lock:
            name = sanitize_filename(filename)
            attach_dir = normalize_rel(str(self.config.get("attachment_dir") or "attachments"))
            rel = AppPaths.unique_path(
                f"{attach_dir}/{name}",
                exists_fn=lambda p: self.paths.to_abs(p).exists(),
            )
            abs_path = self.paths.to_abs(rel)
            self._atomic_write_bytes(abs_path, data)
            self._mark_self_write(rel)
            self.indexer.linkgraph.upsert_file(rel)
            self.eventbus.publish(VaultEvent(EventType.CREATED, path=rel))
            return rel

    # ------------------------------------------------------------------ tree
    def tree(self) -> List[Dict[str, Any]]:
        """嵌套文件树（隐藏 .kwey/.trash/.git；目录在前、名称不区分大小写排序）。"""

        def build(dir_rel: str) -> List[Dict[str, Any]]:
            entries: List[Dict[str, Any]] = []
            for p in self.paths.list_dir(dir_rel):
                try:
                    rel = self.paths.to_rel(p)
                except Exception:  # noqa: BLE001
                    continue
                if is_ignored(rel):
                    continue
                if p.is_dir():
                    entries.append(
                        {"name": p.name, "path": rel, "type": "dir", "children": build(rel)}
                    )
                else:
                    kind = "note" if p.suffix.lower() in MARKDOWN_EXTS else "file"
                    entries.append({"name": p.name, "path": rel, "type": kind})
            entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
            return entries

        return build("")
