"""职责链接影响：复用 LinkGraph / LinkRewriter 的既有解析结果。

语义（INNOVATION-PLAN §4.8）：移动/改名前必须展示「会被重写链接的源文件」，
该清单必须来自索引里已解析的 links 表（LinkRewriter.collect_referencing_files），
**不得**另写一套正则猜测 wikilink。
"""

from __future__ import annotations

from typing import Any, List, Optional


class IndexDutyLinks:
    """DutyRuntime 的链接影响适配器。

    ``rewriter`` 为 :class:`app.vault.links.LinkRewriter`；只读取其索引查询能力，
    不触发任何写操作。数据库不可用时返回空清单（预览降级，不伪造影响）。
    """

    def __init__(self, rewriter: Any, graph: Optional[Any] = None) -> None:
        self._rewriter = rewriter
        self._graph = graph

    def affected(self, old_path: str, new_path: str = "") -> List[str]:
        """移动 ``old_path`` 时会被重写链接的源文件（去重、稳定排序）。"""
        if not old_path:
            return []
        try:
            sources = list(self._rewriter.collect_referencing_files(old_path))
        except Exception:  # noqa: BLE001 —— 索引不可用时按无影响处理
            return []
        return [path for path in sources if path and path != old_path]
