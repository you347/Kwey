"""内存链接图：邻接表、反向链接、wikilink 解析、basename 索引。

解析优先级（ARCHITECTURE.md §3.2）：
1. 精确相对路径（target 原样 / 补 .md，相对 vault 根）
2. 相对源笔记所在目录（补 .md）
3. basename 全局匹配（大小写不敏感，Windows 语义；优先 md 笔记，其次附件等文件）
4. 未解析（None）
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, Optional, Set

from app.paths import normalize_rel


class LinkGraph:
    """进程内单例，由 Indexer 在全量/增量索引时同步维护。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # path → basename（不含 .md）；notes 与其他文件（附件）分表，解析时优先笔记
        self.notes: Dict[str, str] = {}
        self.files: Dict[str, str] = {}
        self.out_links: Dict[str, Set[str]] = {}
        self.in_links: Dict[str, Set[str]] = {}
        self.by_basename: Dict[str, Set[str]] = {}       # lower(basename) → md 路径集合
        self.file_by_basename: Dict[str, Set[str]] = {}  # lower(basename) → 非笔记文件路径集合

    # ------------------------------------------------------------------ registry
    def upsert_note(self, path: str) -> None:
        path = normalize_rel(path)
        basename = path.rsplit("/", 1)[-1]
        if basename.lower().endswith(".md"):
            basename = basename[: -len(".md")]
        with self._lock:
            self._remove_from_basename(self.by_basename, path)
            self.notes[path] = basename
            self.by_basename.setdefault(basename.lower(), set()).add(path)
            self.files.pop(path, None)
            self._remove_from_basename(self.file_by_basename, path)

    def upsert_file(self, path: str) -> None:
        """登记非笔记文件（附件等），供嵌入 ![[图.png]] 按文件名全局解析。"""
        path = normalize_rel(path)
        basename = path.rsplit("/", 1)[-1]
        with self._lock:
            self._remove_from_basename(self.file_by_basename, path)
            self.files[path] = basename
            self.file_by_basename.setdefault(basename.lower(), set()).add(path)
            self.notes.pop(path, None)
            self._remove_from_basename(self.by_basename, path)

    def remove_entry(self, path: str) -> None:
        """从 notes/files 登记表移除（不影响链接图，链接走 remove）。"""
        path = normalize_rel(path)
        with self._lock:
            self.notes.pop(path, None)
            self.files.pop(path, None)
            self._remove_from_basename(self.by_basename, path)
            self._remove_from_basename(self.file_by_basename, path)

    @staticmethod
    def _remove_from_basename(table: Dict[str, Set[str]], path: str) -> None:
        for key in list(table.keys()):
            if path in table[key]:
                table[key].discard(path)
                if not table[key]:
                    del table[key]

    # ------------------------------------------------------------------ resolve
    @staticmethod
    def _candidates(target: str) -> list:
        t = normalize_rel(target).lstrip("./")
        if not t:
            return []
        if t.lower().endswith(".md"):
            return [t, t[:-3]]
        return [t, t + ".md"]

    def resolve(self, target: str, source_path: Optional[str] = None) -> Optional[str]:
        """wikilink 目标 → vault 相对路径；未解析返回 None。"""
        cands = self._candidates(target)
        if not cands:
            return None
        with self._lock:
            # 1) 精确相对路径（vault 根）
            for c in cands:
                if c in self.notes or c in self.files:
                    return c
            # 2) 相对源笔记所在目录
            if source_path and "/" in source_path:
                src_dir = source_path.rsplit("/", 1)[0]
                for c in cands:
                    joined = f"{src_dir}/{c}"
                    if joined in self.notes or joined in self.files:
                        return joined
            # 3) basename 全局匹配（大小写不敏感），优先笔记
            base = cands[0].rsplit("/", 1)[-1].lower()
            if base.lower().endswith(".md"):
                base = base[:-3]
            note_hits = self.by_basename.get(base)
            if note_hits:
                return sorted(note_hits)[0]
            file_hits = self.file_by_basename.get(base)
            if file_hits:
                return sorted(file_hits)[0]
        return None

    # ------------------------------------------------------------------ graph
    def apply(self, path: str, rows: Iterable[dict]) -> None:
        """同步某笔记的出链（rows: links 表行 dict，target_path 可为 None）。"""
        path = normalize_rel(path)
        new_out: Set[str] = set()
        for row in rows:
            tp = row.get("target_path")
            if tp:
                new_out.add(normalize_rel(tp))
        with self._lock:
            old_out = self.out_links.pop(path, set())
            for target in old_out:
                targets = self.in_links.get(target)
                if targets is not None:
                    targets.discard(path)
                    if not targets:
                        del self.in_links[target]
            self.out_links[path] = new_out
            for target in new_out:
                self.in_links.setdefault(target, set()).add(path)

    def remove(self, path: str) -> None:
        """笔记删除/改名时移除其全部链接关系。"""
        path = normalize_rel(path)
        with self._lock:
            old_out = self.out_links.pop(path, set())
            for target in old_out:
                targets = self.in_links.get(target)
                if targets is not None:
                    targets.discard(path)
                    if not targets:
                        del self.in_links[target]
            # 作为目标被引用的关系也清掉
            for sources in self.in_links.values():
                sources.discard(path)
            self.in_links.pop(path, None)

    def rebuild(self, rows: Iterable[dict]) -> None:
        """整库重建：按 links 表全量灌入。"""
        with self._lock:
            self.out_links.clear()
            self.in_links.clear()
        for row in rows:
            self.apply(row["source_path"], [row])

    # ------------------------------------------------------------------ query
    def backlinks(self, path: str) -> Set[str]:
        path = normalize_rel(path)
        with self._lock:
            return set(self.in_links.get(path, set()))

    def out_links_of(self, path: str) -> Set[str]:
        path = normalize_rel(path)
        with self._lock:
            return set(self.out_links.get(path, set()))

    def note_count(self) -> int:
        with self._lock:
            return len(self.notes)
