"""索引器：全量扫描 + 单文件增量索引（notes / links / tags / fts 同步更新 + 内存图同步）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from app.index.database import IndexDatabase
from app.index.linkgraph import LinkGraph
from app.parser import frontmatter as fm_mod
from app.parser.tags import extract_inline_tags, normalize_fm_tags
from app.parser.wikilink import extract_wikilinks, split_anchor
from app.paths import AppPaths, is_ignored, normalize_rel

logger = logging.getLogger("kwey.indexer")

MARKDOWN_EXTS = {".md", ".markdown"}
NOTE_EXTS = MARKDOWN_EXTS


class Indexer:
    """索引层门面。任何写盘后的索引更新都必须经这里（VaultService / Watcher 调用）。"""

    def __init__(self, paths: AppPaths, db: IndexDatabase, linkgraph: LinkGraph) -> None:
        self.paths = paths
        self.db = db
        self.linkgraph = linkgraph

    # ------------------------------------------------------------------ full
    def full_scan(self) -> int:
        """全量扫描建索引；返回笔记数。完成后调 stamp_index_ready。"""
        root = self.paths.ensure_root()
        self.db.reset()
        count = 0
        for abs_path in sorted(root.rglob("*")):
            if not abs_path.is_file():
                continue
            try:
                rel = self.paths.to_rel(abs_path)
            except Exception:  # noqa: BLE001
                continue
            if is_ignored(rel):
                continue
            if abs_path.suffix.lower() in NOTE_EXTS:
                self.reindex_note(rel, broadcast=False)
                count += 1
            else:
                # 附件等非笔记文件登记进图，供 ![[嵌入]] 按文件名解析
                self.linkgraph.upsert_file(rel)
        # 全部 basename 就绪后，复活未解析链接
        self.revive_all_unresolved()
        self.db.stamp_index_ready()
        return count

    # ------------------------------------------------------------------ single
    def reindex_note(self, rel: str, broadcast: bool = True) -> bool:
        """单文件增量索引。返回链接关系是否发生变化（links_changed 判定）。

        文件不存在时等价于 remove_note 并返回 False。
        """
        rel = normalize_rel(rel)
        if is_ignored(rel):
            return False
        abs_path = self.paths.to_abs(rel)
        if not abs_path.is_file():
            self.remove_note(rel)
            return False

        raw = abs_path.read_text(encoding="utf-8-sig")
        fm, body = fm_mod.parse_frontmatter(raw)
        stat = abs_path.stat()

        if abs_path.suffix.lower() in NOTE_EXTS:
            old_links = {
                (r["target"], r["target_path"], r["kind"]) for r in self.db.links_for(rel)
            }
            rows, _ = self._parse_links(rel, body)
            new_links = {(t, tp, k) for (t, tp, k, _ln) in rows}

            title = str(fm.get("title") or "").strip() or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            self.db.upsert_note(
                path=rel,
                title=title,
                mtime=stat.st_mtime,
                size=stat.st_size,
                created=fm.get("created") if isinstance(fm.get("created"), str) else None,
                updated=fm.get("updated") if isinstance(fm.get("updated"), str) else None,
            )
            self.db.replace_links(rel, rows)
            self.db.replace_tags(rel, self._parse_tags(fm, body))
            self.db.upsert_fts(rel, title, body, " ".join(self._all_tags(fm, body)))
            self.linkgraph.upsert_note(rel)
            self.linkgraph.apply(rel, self.db.links_for(rel))
            return old_links != new_links

        # 非笔记文件：仅登记进图（附件嵌入解析用）
        self.linkgraph.upsert_file(rel)
        return False

    def remove_note(self, rel: str, break_links: bool = False) -> None:
        """从索引与内存图移除（磁盘文件由调用方处理）。

        break_links=True 时把指向该笔记的链接置为断链（真实删除场景）；
        改名/移动场景传 False（重写器依赖既有 target_path 定位引用方）。
        """
        rel = normalize_rel(rel)
        self.db.remove_note(rel)
        if break_links:
            self.db.break_links_to(rel)
        self.linkgraph.remove(rel)
        self.linkgraph.remove_entry(rel)

    def remove_file_entry(self, rel: str) -> None:
        """非笔记文件移除（附件删除）。"""
        rel = normalize_rel(rel)
        self.linkgraph.remove_entry(rel)

    # ------------------------------------------------------------------ revive
    def revive_all_unresolved(self) -> int:
        """整库扫描后复活全部未解析链接（按 basename 二次解析）。"""
        revived = 0
        for row in self.db.unresolved_links():
            if self._try_revive(row):
                revived += 1
        return revived

    def revive_for(self, target_rel: str) -> int:
        """目标笔记创建/改名后，复活指向它的未解析链接（target_path IS NULL 且
        解析结果等于新路径），并同步内存图。返回复活条数。"""
        target_rel = normalize_rel(target_rel)
        revived = 0
        for row in self.db.unresolved_links():
            resolved = self.linkgraph.resolve(row["target"], row["source_path"])
            if resolved == target_rel:
                if self._try_revive(row):
                    revived += 1
        return revived

    def _try_revive(self, row: dict) -> bool:
        resolved = self.linkgraph.resolve(row["target"], row["source_path"])
        if resolved is None:
            return False
        self.db.update_link_resolution(row["source_path"], row["target"], resolved)
        # 同步内存图：重放该源笔记的出链
        self.linkgraph.apply(row["source_path"], self.db.links_for(row["source_path"]))
        return True

    # ------------------------------------------------------------------ parse
    def _parse_links(self, source_rel: str, body: str) -> Tuple[List[Tuple[str, Optional[str], str, int]], List[dict]]:
        """解析正文 wikilink → [(target, target_path|None, kind, line)]。"""
        rows: List[Tuple[str, Optional[str], str, int]] = []
        seen = set()
        for ref in extract_wikilinks(body):
            target_only, _anchor = split_anchor(ref.target)
            resolved = self.linkgraph.resolve(target_only, source_rel)
            key = (ref.target, resolved, ref.kind, ref.line)
            if key in seen:
                continue
            seen.add(key)
            rows.append((ref.target, resolved, ref.kind, ref.line))
        return rows, []

    def _parse_tags(self, fm: dict, body: str) -> List[Tuple[str, str]]:
        """frontmatter tags 与行内标签合并。"""
        rows: List[Tuple[str, str]] = []
        for tag in normalize_fm_tags(fm.get("tags")):
            rows.append((tag, "frontmatter"))
        for tag in extract_inline_tags(body):
            rows.append((tag, "inline"))
        # 去重（同名标签以 inline 优先级让位——同 note_path+tag+source 主键）
        seen = set()
        result = []
        for tag, source in rows:
            key = (tag, source)
            if key not in seen:
                seen.add(key)
                result.append((tag, source))
        return result

    def _all_tags(self, fm: dict, body: str) -> List[str]:
        tags = normalize_fm_tags(fm.get("tags")) + extract_inline_tags(body)
        return list(dict.fromkeys(tags))

    # ------------------------------------------------------------------ stats
    def file_mtime(self, rel: str) -> Optional[float]:
        abs_path = self.paths.to_abs(rel)
        if not abs_path.is_file():
            return None
        return abs_path.stat().st_mtime
