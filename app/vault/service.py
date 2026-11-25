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

import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import HTTPException

from app.config import AppSettings
from app.events import EventBus, EventType, VaultEvent
from app.index.database import IndexDatabase
from app.index.indexer import Indexer
from app.parser import frontmatter as fm_mod
from app.parser.wikilink import parse_wikilink_text, split_anchor
from app.paths import (
    AppPaths,
    ILLEGAL_FILENAME_CHARS,
    PathError,
    is_ignored,
    normalize_rel,
    sanitize_filename,
    validate_relative,
)
from app.vault.links import LinkRewriter
from app.vault.provenance import (
    ActorContext,
    FIELD_GENERATED,
    FIELD_HASH_VERSION,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWED_HASH,
    FIELD_RUN_ID,
    PAYLOAD_SERIALIZATION_VERSION,
    ProvenanceStore,
    SYSTEM_ACTOR,
    actor_fields,
    describe_note,
    payload_hash,
    read_declared_fields,
    resolve_actor,
)

MARKDOWN_EXTS = {".md", ".markdown"}
TRASH_ROOT = ".trash"  # 回收站根目录（vault 相对，IGNORED_DIRS 内）

logger = logging.getLogger("kwey.vault")


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
        provenance: Optional[ProvenanceStore] = None,
    ) -> None:
        self.paths = AppPaths(root)
        self.config = config
        self.eventbus = eventbus
        self.db = db
        self.indexer = indexer
        self.rewriter = rewriter
        self.provenance = provenance if provenance is not None else ProvenanceStore(root)
        self.lock = threading.RLock()
        self.recent_self_writes: Dict[str, float] = {}
        self._watcher = None  # 由 deps 装配；start_watcher/stop_watcher 转发
        self._snapshots = None  # M2 git 快照服务，由 deps 装配
        self._suppress_snapshots = 0  # >0 时暂停自动快照（批量导入/迁移用）

    # ------------------------------------------------------------------ lifecycle
    def bind_watcher(self, watcher) -> None:
        self._watcher = watcher

    def bind_snapshots(self, snapshots) -> None:
        """注入 M2 git 快照服务（可选能力，关闭时不影响主写路径）。"""
        self._snapshots = snapshots

    @contextmanager
    def bulk_write(self) -> Iterator[None]:
        """批量写模式：期间跳过逐篇自动快照，结束后统一提交一次。

        导入 800 篇笔记若逐篇 commit，会生成 800 次 git 快照（分钟级开销）；
        批量模式下只在收尾提交一条 ``bulk: N 篇`` 记录。
        """
        with self.lock:
            self._suppress_snapshots += 1
        try:
            yield
        finally:
            with self.lock:
                self._suppress_snapshots = max(0, self._suppress_snapshots - 1)

    def _auto_snapshot(self, action: str, rel: str = "") -> None:
        """统一写路径的快照挂钩：写盘成功后按设置生成 commit。

        快照只是增强能力，任何失败都只记日志，绝不阻断笔记写入。
        ``enabled`` 每次从 settings 实时读取，支持设置页运行时开关。
        """
        svc = self._snapshots
        cfg = self.config.get("snapshots") or {}
        if svc is None or not cfg.get("enabled", False):
            return
        if not cfg.get("auto_commit", True):
            return
        if self._suppress_snapshots:
            return
        try:
            svc.enabled = True
            if not (svc.root / ".git").exists():
                svc.ensure_repo()
            svc.commit(f"{action}: {rel}" if rel else action)
        except Exception:  # noqa: BLE001
            logger.warning("git 快照失败（不影响写盘）: %s %s", action, rel, exc_info=True)

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

    # ------------------------------------------------------------------ provenance
    def _with_managed_fields(
        self,
        rel: str,
        fm: Dict[str, Any],
        actor: Optional[ActorContext],
    ) -> Dict[str, Any]:
        """写入前注入 ``kwey_`` 宿主字段；generated 具粘性，不洗掉历史 Agent 派生。"""
        ctx = resolve_actor(actor)
        previous = self.provenance.get_baseline(rel) or {}
        declared = read_declared_fields(fm)
        fields = actor_fields(
            ctx,
            generated=bool(previous.get("generated")) or declared["generated"],
        )
        fm.update(fields)
        if not ctx.run_id:
            fm.pop(FIELD_RUN_ID, None)  # 未提供批次就不保留旧关联
        return fm

    def _record_provenance(
        self,
        rel: str,
        raw: str,
        actor: Optional[ActorContext],
        action: str,
        *,
        generated: bool = False,
    ) -> None:
        """写盘后记录最后已知基线；溯源失败只记日志，绝不阻断笔记写入。"""
        try:
            self.provenance.record_write(
                rel, raw, resolve_actor(actor), action=action, generated=generated
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "溯源基线记录失败（不影响写盘）: %s %s", action, rel, exc_info=True
            )

    def provenance_info(self, rel: str) -> Dict[str, Any]:
        """单篇溯源视图（供 provenance read 路由复用）。"""
        rel = _validate(rel)
        abs_path = self.paths.to_abs(rel)
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
        return describe_note(self.provenance, rel, self._read_text(abs_path))

    def record_external_change(self, rel: str) -> bool:
        """watcher 用：有基线且 raw hash 变化时只记事件/状态，绝不重写外部文件。"""
        try:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                return False
            raw = self._read_text(abs_path)
            if not self.provenance.detect_external(rel, raw):
                return False
            return self.provenance.record_external(rel, raw)
        except Exception:  # noqa: BLE001
            logger.warning("外部变化识别失败: %s", rel, exc_info=True)
            return False

    # ------------------------------------------------------------------ create
    def create_note(
        self,
        rel: str,
        content: str = "",
        frontmatter: Optional[Dict[str, Any]] = None,
        dedup: bool = False,
        *,
        actor: Optional[ActorContext] = None,
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
            fm = self._with_managed_fields(rel, fm, actor)
            fm = fm_mod.normalize_frontmatter_for_write(fm, is_new=True)
            if content == "":
                content = f"# {title}\n\n"
            text = fm_mod.dump_frontmatter(fm, content)
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            self._record_provenance(
                rel, text, actor, "create", generated=bool(fm.get(FIELD_GENERATED))
            )
            self.indexer.reindex_note(rel)
            revived = self.indexer.revive_for(rel)
            self.eventbus.publish(VaultEvent(EventType.CREATED, path=rel))
            if revived:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            meta = self.db.get_note(rel) or {}
            meta["path"] = rel
            meta["title"] = str(fm.get("title") or title)
            self._auto_snapshot("create", rel)
            return meta

    # ------------------------------------------------------------------ write
    def review_note(
        self,
        rel: str,
        *,
        expected_payload_hash: str,
        reviewed_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """人工审阅专用写路径：只刷新 ``kwey_reviewed_*`` 管理字段。

        与 ``write_note`` 的区别：不调用 ``_with_managed_fields``，因此不会把
        writer_kind/last_writer/run_id/generated 改写成审阅者身份；也不记录新的
        写入基线，只在原基线上更新 reviewed_* 与 raw/payload hash。这样 Agent
        来源在审阅后仍可追溯，普通人工保存仍按 ``write_note`` 正常更新写入者。
        """
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")

            raw = self._read_text(abs_path)
            current = payload_hash(raw)
            expected = (expected_payload_hash or "").strip().lower()
            if not expected:
                raise HTTPException(status_code=400, detail="expected_payload_hash 不能为空")
            if expected != current:
                raise HTTPException(
                    status_code=409,
                    detail="笔记内容已变化，请刷新当前版本后重新审阅",
                )
            if self.provenance.get_baseline(rel) is None:
                raise HTTPException(
                    status_code=409,
                    detail="该笔记没有本机写入基线，无法标记为已审",
                )

            stamp = reviewed_at or datetime.now().astimezone().isoformat(timespec="seconds")
            fm, body = fm_mod.parse_frontmatter(raw)
            fm[FIELD_REVIEWED_AT] = stamp
            fm[FIELD_REVIEWED_HASH] = current
            fm[FIELD_HASH_VERSION] = PAYLOAD_SERIALIZATION_VERSION
            fm = fm_mod.normalize_frontmatter_for_write(fm, is_new=False)
            text = fm_mod.dump_frontmatter(fm, body)
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            # 不调用 _record_provenance：审阅不是一次新的写入，基线写入者保持不变。
            if not self.provenance.record_review(
                rel, reviewed_hash=current, reviewed_at=stamp, raw=text
            ):
                raise HTTPException(status_code=409, detail="溯源基线已失效，无法审阅")

            links_changed = self.indexer.reindex_note(rel)
            self.eventbus.publish(VaultEvent(EventType.UPDATED, path=rel))
            if links_changed:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            self._auto_snapshot("update", rel)
            return {"path": rel, "updated": True, "mtime": abs_path.stat().st_mtime}

    def write_note(
        self,
        rel: str,
        content: str,
        frontmatter: Optional[Dict[str, Any]] = None,
        base_mtime: Optional[float] = None,
        *,
        actor: Optional[ActorContext] = None,
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
            fm = dict(frontmatter)
            fm = self._with_managed_fields(rel, fm, actor)
            fm = fm_mod.normalize_frontmatter_for_write(fm, is_new=False)
            text = fm_mod.dump_frontmatter(fm, content)
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            self._record_provenance(
                rel, text, actor, "write", generated=bool(fm.get(FIELD_GENERATED))
            )
            links_changed = self.indexer.reindex_note(rel)
            self.eventbus.publish(VaultEvent(EventType.UPDATED, path=rel))
            if links_changed:
                self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            self._auto_snapshot("update", rel)
            return {
                "updated": True,
                "links_changed": links_changed,
                "mtime": abs_path.stat().st_mtime,
            }

    def write_raw(
        self,
        rel: str,
        text: str,
        publish_event: bool = True,
        *,
        actor: Optional[ActorContext] = None,
        action: str = "write_raw",
    ) -> bool:
        """原样写文本（链接重写器内部使用）：统一锁/原子写/自写标记/增量索引。"""
        with self.lock:
            rel = _validate(rel)
            abs_path = self.paths.to_abs(rel)
            if not abs_path.is_file():
                return False
            self._atomic_write(abs_path, text)
            self._mark_self_write(rel)
            # 链接重写等内部原样写：无显式/上下文身份时记 system，不伪造成作者。
            ctx = resolve_actor(actor)
            if ctx.kind == "unknown":
                ctx = SYSTEM_ACTOR
            self._record_provenance(rel, text, ctx, action)
            links_changed = self.indexer.reindex_note(rel)
            if publish_event:
                self.eventbus.publish(VaultEvent(EventType.UPDATED, path=rel))
                if links_changed:
                    self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=rel))
            self._auto_snapshot("update", rel)
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
            self._auto_snapshot("move", new_rel)
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
            self._auto_snapshot("delete", rel)
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
            self._auto_snapshot("delete-dir", rel)
            return trash_rel

    # ------------------------------------------------------------------ trash
    # 回收站读写统一走「root / rel 直拼绝对路径 + os.replace」，与 delete_note 一致：
    # validate_relative 会拒绝含 .trash 片段的 API 路径（QA B2 修复），故此处
    # 自行逐段安全校验，绝不复用 validate_relative。
    def _trash_abs(self, trash_path: str) -> tuple[str, Path]:
        """校验 .trash/ 内路径 → (rel, abs_path)；越界/非法一律 400。"""
        rel = normalize_rel(trash_path)
        if not rel.startswith(TRASH_ROOT + "/") or rel == TRASH_ROOT:
            raise HTTPException(status_code=400, detail="路径必须位于 .trash/ 内")
        for part in rel.split("/"):
            if part in ("", ".", ".."):
                raise HTTPException(status_code=400, detail=f"非法路径片段: {part!r}")
            if part in (".kwey", ".git"):
                raise HTTPException(status_code=400, detail=f"禁止穿越到保留目录: {part!r}")
            if ":" in part or any(c in ILLEGAL_FILENAME_CHARS for c in part):
                raise HTTPException(status_code=400, detail=f"路径含非法字符: {part!r}")
            if re.search(r"[\x00-\x1f]", part):
                raise HTTPException(status_code=400, detail="路径含控制字符")
        root = self.paths.ensure_root()
        abs_path = (root / rel).resolve()
        trash_root = (root / TRASH_ROOT).resolve()
        try:
            abs_path.relative_to(trash_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="路径越出回收站") from exc
        return rel, abs_path

    def _dest_abs(self, dest_rel: str) -> Path:
        """恢复目标：root / rel 直拼 + 越界校验（不套 IGNORED_DIRS 限制）。"""
        rel = normalize_rel(dest_rel)
        for part in rel.split("/"):
            if part in ("", ".", ".."):
                raise HTTPException(status_code=400, detail=f"非法恢复目标片段: {part!r}")
            if ":" in part or any(c in ILLEGAL_FILENAME_CHARS for c in part):
                raise HTTPException(status_code=400, detail=f"恢复目标含非法字符: {part!r}")
            if re.search(r"[\x00-\x1f]", part):
                raise HTTPException(status_code=400, detail="恢复目标含控制字符")
        root = self.paths.ensure_root()
        abs_path = (root / rel).resolve()
        try:
            abs_path.relative_to(root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="恢复目标越出 vault") from exc
        return abs_path

    def _prune_empty_trash_dirs(self, start: Path) -> None:
        """恢复/永久删除后自底向上清理 .trash/ 内空目录（到 .trash 根为止）。"""
        root = self.paths.ensure_root()
        trash_root = (root / TRASH_ROOT).resolve()
        if start.is_dir():
            for d in sorted(start.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()  # 仅空目录可删
                    except OSError:
                        pass
        cur = start.resolve()
        while cur != trash_root:
            try:
                cur.rmdir()
            except OSError:
                break
            try:
                cur.parent.relative_to(trash_root)
            except ValueError:
                break
            cur = cur.parent

    def list_trash(self) -> List[Dict[str, Any]]:
        """列出 .trash/ 顶层条目（文件/目录），按 mtime 倒序。

        original_path = 去掉 .trash/ 前缀（保留子目录结构）。目录条目可整体
        恢复/永久删除；恢复目录时按其内部文件逐一映射回原子路径重索引。
        """
        root = self.paths.ensure_root()
        trash_root = root / TRASH_ROOT
        items: List[Dict[str, Any]] = []
        if not trash_root.is_dir():
            return items
        for p in trash_root.iterdir():
            if p.name.endswith(".kwey-tmp"):
                continue
            try:
                rel = self.paths.to_rel(p)
            except Exception:  # noqa: BLE001
                continue
            original = (
                rel[len(TRASH_ROOT) + 1:]
                if rel.startswith(TRASH_ROOT + "/") else p.name
            )
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            items.append({
                "trash_path": rel,
                "original_path": original,
                "name": p.name,
                "is_dir": p.is_dir(),
                "mtime": mtime,
            })
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def _restore_one(self, src_abs: Path, dest_rel: str) -> int:
        """移动单个文件回 vault → 增量索引 → 复活链接 → 广播 CREATED。"""
        dest_abs = self._dest_abs(dest_rel)
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_abs, dest_abs)
        self._mark_self_write(dest_rel)
        self.indexer.reindex_note(dest_rel)
        self._auto_snapshot("restore", dest_rel)
        revived = self.indexer.revive_for(dest_rel)
        self.eventbus.publish(VaultEvent(EventType.CREATED, path=dest_rel))
        if revived:
            self.eventbus.publish(VaultEvent(EventType.LINKS_CHANGED, path=dest_rel))
        return revived

    def restore_from_trash(self, trash_path: str, target_path: Optional[str] = None) -> Dict[str, Any]:
        """从 .trash/ 恢复：目标已存在自动去重、不覆盖；目录恢复逐文件重索引。"""
        with self.lock:
            trash_rel, src_abs = self._trash_abs(trash_path)
            if not src_abs.exists():
                raise HTTPException(status_code=404, detail=f"回收站条目不存在: {trash_rel}")
            original = trash_rel[len(TRASH_ROOT) + 1:]
            base = normalize_rel(target_path) if target_path else original
            if base.split("/")[0] in (".trash", ".kwey", ".git"):
                raise HTTPException(status_code=400, detail="恢复目标不可位于系统保留目录")
            root = self.paths.ensure_root()
            revived_total = 0

            if src_abs.is_dir():
                # 逐文件恢复到各自原路径：目标目录已存在时只给冲突文件去重，
                # 不能把整个目录改名（否则删除子目录内单篇笔记后恢复不到原位置）。
                children = [
                    c for c in sorted(src_abs.rglob("*"))
                    if c.is_file() and not c.name.endswith(".kwey-tmp")
                ]
                for child in children:
                    sub = child.relative_to(src_abs).as_posix()
                    dest_rel = AppPaths.unique_path(
                        f"{base}/{sub}", exists_fn=lambda p: (root / p).exists()
                    )
                    revived_total += self._restore_one(child, dest_rel)
                self._prune_empty_trash_dirs(src_abs)
                return_path = base
            else:
                dest_rel = AppPaths.unique_path(base, exists_fn=lambda p: (root / p).exists())
                revived_total += self._restore_one(src_abs, dest_rel)
                self._prune_empty_trash_dirs(src_abs.parent)
                return_path = dest_rel

            return {"path": return_path, "rewritten_links": revived_total}

    def purge_trash_path(self, trash_path: str) -> int:
        """永久删除回收站单条（真实 unlink/rmtree）。返回删除条目数。"""
        with self.lock:
            trash_rel, abs_path = self._trash_abs(trash_path)
            if not abs_path.exists():
                raise HTTPException(status_code=404, detail=f"回收站条目不存在: {trash_rel}")
            if abs_path.is_dir():
                shutil.rmtree(abs_path)
            else:
                abs_path.unlink()
            self._prune_empty_trash_dirs(abs_path.parent)
            return 1

    def purge_trash_all(self) -> int:
        """清空 .trash/ 全部条目（真实删除）。返回删除的顶层条目数。"""
        with self.lock:
            root = self.paths.ensure_root()
            trash_root = root / TRASH_ROOT
            if not trash_root.is_dir():
                return 0
            count = 0
            for p in list(trash_root.iterdir()):
                if p.name.endswith(".kwey-tmp"):
                    continue
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                count += 1
            return count

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
