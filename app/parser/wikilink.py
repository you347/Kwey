"""wikilink / 嵌入语法提取与解析。

支持三种形式：
    [[目标]]        → kind='wikilink'
    [[目标|别名]]   → kind='wikilink'，alias='别名'
    ![[目标]]       → kind='embed'
提取时记录行号（1-based，跳转用），并跳过围栏代码块与行内代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")


@dataclass
class LinkRef:
    """一个 wikilink/嵌入引用。"""

    target: str       # [[ ]] 内别名分隔前的目标文本（原样，含 #标题锚）
    kind: str         # 'wikilink' | 'embed'
    line: int         # 1-based 行号
    alias: str = ""   # 别名（无则空）
    raw: str = ""     # 原始匹配文本


def parse_wikilink_text(text: str) -> tuple:
    """解析 [[ ]] 内部文本 → (target, alias)。target 不含别名部分。"""
    text = (text or "").strip()
    if "|" in text:
        target, alias = text.split("|", 1)
        return target.strip(), alias.strip()
    return text, ""


def split_anchor(target: str) -> tuple:
    """拆出 `目标#标题锚` → (目标路径部分, 锚部分)。锚不参与路径解析。"""
    if "#" in target:
        path_part, _, anchor = target.partition("#")
        return path_part.strip(), anchor.strip()
    return target.strip(), ""


def _iter_outside_code(body: str):
    """逐行迭代，跳过围栏代码块（``` / ~~~）。yield (line_no, line_text)。"""
    fence = None
    for idx, line in enumerate(body.splitlines(), start=1):
        stripped = line.lstrip()
        if fence is None:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                fence = m.group(1)[0]
                continue
            yield idx, line
        else:
            if stripped.startswith(fence * 3) or re.match(rf"^{re.escape(fence)}{{3,}}", stripped):
                fence = None  # 围栏结束

_CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _outside_inline_code(line: str) -> List[tuple]:
    """对单行做 wikilink 提取，跳过行内代码 span。返回 [(raw, embed, inner, span_start)]。"""
    results = []
    segments = []
    last = 0
    for m in _CODE_SPAN_RE.finditer(line):
        segments.append((last, m.start()))
        last = m.end()
    segments.append((last, len(line)))
    for start, end in segments:
        for m in WIKILINK_RE.finditer(line[start:end]):
            raw, embed_flag, inner = m.group(0), m.group(1), m.group(2)
            results.append((raw, embed_flag == "!", inner, start + m.start()))
    return results


def extract_wikilinks(body: str) -> List[LinkRef]:
    """提取全部 wikilink/嵌入（跳过围栏代码块与行内代码），含 1-based 行号。"""
    refs: List[LinkRef] = []
    for line_no, line in _iter_outside_code(body):
        for raw, embed, inner, _pos in _outside_inline_code(line):
            target, alias = parse_wikilink_text(inner)
            if not target:
                continue
            refs.append(
                LinkRef(
                    target=target,
                    kind="embed" if embed else "wikilink",
                    line=line_no,
                    alias=alias,
                    raw=raw,
                )
            )
    return refs
