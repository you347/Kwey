"""链接重写：改名/移动时全库 0 断链（G2 核心）。

依据 links 表中「target 原始文本 → target_path」的既有解析结果精确定位
引用方（不依赖重解析——改名后旧文本已无法解析），逐文件正则重写：
    [[旧名]] / [[旧名|别名]] / ![[旧图.png]] 与附件路径同步更新。
重写后的文件经 VaultService.write_raw 落盘（统一写路径）。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set, Tuple

from app.index.database import IndexDatabase
from app.index.linkgraph import LinkGraph
from app.parser.wikilink import WIKILINK_RE, parse_wikilink_text, split_anchor
from app.paths import AppPaths


class LinkRewriter:
    """改名/移动时的全库链接重写器。"""

    def __init__(self, paths: AppPaths, db: IndexDatabase, linkgraph: LinkGraph) -> None:
        self.paths = paths
        self.db = db
        self.linkgraph = linkgraph

    # ------------------------------------------------------------------ query
    def collect_referencing_files(self, target_path: str) -> List[str]:
        """引用了 target_path 的全部源笔记（去重、稳定排序）。"""
        return sorted(set(self.db.link_sources(target_path)))

    # ------------------------------------------------------------------ rewrite
    def rewrite_on_rename(self, old_path: str, new_path: str, writer) -> int:
        """将所有指向 old_path 的 wikilink/嵌入重写为 new_path。

        Args:
            old_path / new_path: vault 相对路径（.md）。
            writer: VaultService（提供统一写路径 write_raw）。

        Returns:
            重写的链接总数（不含未变化的引用方文件数）。
        """
        count = 0
        for src in self.collect_referencing_files(old_path):
            try:
                abs_path = self.paths.to_abs(src)
            except Exception:  # noqa: BLE001
                continue
            if not abs_path.is_file():
                continue
            raw = abs_path.read_text(encoding="utf-8-sig")
            targets = {
                r["target"]
                for r in self.db.links_for(src)
                if r["target_path"] == old_path
            }
            if not targets:
                continue
            new_raw, n = self.rewrite_text(raw, targets, old_path, new_path, src)
            if n > 0:
                if writer.write_raw(src, new_raw):
                    count += n
        return count

    def rewrite_text(
        self,
        text: str,
        old_targets: Set[str],
        old_path: str,
        new_path: str,
        source_rel: str,
    ) -> Tuple[str, int]:
        """重写文本中解析到 old_path 的 wikilink。返回 (新文本, 重写数)。"""
        state = {"count": 0}
        new_basename = new_path.rsplit("/", 1)[-1]
        if new_basename.lower().endswith(".md"):
            new_basename = new_basename[:-3]
        new_path_no_ext = new_path[:-3] if new_path.lower().endswith(".md") else new_path

        def preferred_target(original: str) -> str:
            """保持用户书写风格：原来是 basename 式就用新 basename；路径式就用新路径。"""
            if "/" in original or original.lower().endswith(".md"):
                return new_path_no_ext
            return new_basename

        def _sub(m: re.Match) -> str:
            bang, inner = m.group(1), m.group(2)
            target_raw, alias = parse_wikilink_text(inner)
            path_part, anchor = split_anchor(target_raw)
            if path_part in old_targets or target_raw in old_targets:
                new_target = preferred_target(path_part or target_raw)
                rebuilt = new_target
                if anchor:
                    rebuilt += f"#{anchor}"
                if alias:
                    rebuilt += f"|{alias}"
                state["count"] += 1
                return f"{bang}[[{rebuilt}]]"
            return m.group(0)

        return WIKILINK_RE.sub(_sub, text), state["count"]
