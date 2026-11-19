"""大纲提取：ATX 标题（^#{1,6}\\s），跳过围栏代码块与行内代码。

复用 app/parser/wikilink.py 的 _iter_outside_code 跳过围栏代码块（``` / ~~~），
行内代码用等长空格屏蔽后再判定结构，避免把代码里的 `#` 误判为标题。
返回的 line 为 1-based，基于传入正文（body，不含 frontmatter，与编辑器一致）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.parser.wikilink import _CODE_SPAN_RE, _iter_outside_code

ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_TRAILING_HASH_RE = re.compile(r"[ \t]+#+[ \t]*$")


def extract_outline(body: str) -> List[Dict[str, Any]]:
    """提取标题列表 → [{"level":1-6, "text":..., "line":1-based}]。"""
    headings: List[Dict[str, Any]] = []
    for line_no, line in _iter_outside_code(body):
        # 行内代码用等长空格屏蔽后再判定结构（保持与原文列对齐）
        masked = _CODE_SPAN_RE.sub(lambda m: " " * (m.end() - m.start()), line)
        match = ATX_RE.match(masked)
        if not match:
            continue
        # text 从原始行按相同区间截取，去掉 ATX 尾部闭合 #
        text = _TRAILING_HASH_RE.sub("", line[match.start(2):match.end(2)]).strip()
        if not text:
            continue
        headings.append({"level": len(match.group(1)), "text": text, "line": line_no})
    return headings
