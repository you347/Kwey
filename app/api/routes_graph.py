"""图/标签路由：反向链接、未链接提及、一键转双链、标签层级树、标签笔记列表。"""

from __future__ import annotations

from html import escape
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.models import LinkifyRequest
from app.parser.wikilink import extract_wikilinks, split_anchor

router = APIRouter(prefix="/api/graph", tags=["graph"])


def _link_context(content: str, note_path: str, linkgraph) -> Optional[dict]:
    """在源笔记正文中找到指向 note_path 的第一处链接 → (上下文行, 行号)。"""
    for ref in extract_wikilinks(content):
        target_only, _anchor = split_anchor(ref.target)
        if linkgraph.resolve(target_only) == note_path:
            lines = content.splitlines()
            line_text = lines[ref.line - 1].strip() if 0 < ref.line <= len(lines) else ""
            return {"snippet": escape(line_text[:200]), "line": ref.line}
    return None


@router.get("/backlinks")
async def backlinks(path: str):
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    db = deps.require_db()
    # B4 修复：排除自身引用（自嵌入 ![[自身]] 不进反链面板，与 Obsidian 惯例一致）
    sources = sorted(linkgraph.backlinks(path) - {path})
    items: List[dict] = []
    for src in sources:
        note = db.get_note(src)
        title = note["title"] if note else src
        snippet, line = "", 0
        try:
            doc = vault.read_note(src)
            ctx = _link_context(doc["content"], path, linkgraph)
            if ctx:
                snippet, line = ctx["snippet"], ctx["line"]
        except HTTPException:
            pass
        items.append({"path": src, "title": title, "snippet": snippet, "line": line})
    return {"code": 0, "data": {"backlinks": items, "count": len(items)}, "message": "ok"}


@router.get("/unlinked")
async def unlinked(path: str):
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    db = deps.require_db()
    note = db.get_note(path)
    if note is None:
        raise HTTPException(status_code=404, detail=f"笔记不存在: {path}")
    exclude = linkgraph.backlinks(path) | {path}
    mentions = db.body_mentions(note["title"], exclude)
    # line 号定位：逐个在提及笔记正文里找标题首现行
    result: List[dict] = []
    for m in mentions:
        line = 0
        try:
            doc = vault.read_note(m["path"])
            for idx, text in enumerate(doc["content"].splitlines(), start=1):
                if note["title"] in text:
                    line = idx
                    m["snippet"] = escape(text.strip()[:200])
                    break
        except HTTPException:
            pass
        m["line"] = line
        result.append(m)
    return {"code": 0, "data": {"mentions": result}, "message": "ok"}


@router.post("/linkify")
async def linkify(req: LinkifyRequest):
    """在提及处创建 [[双链]]（走统一写路径）。"""
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    doc = vault.read_note(req.path)
    mention_path = req.mention_path

    # 链接文本：basename 能唯一解析就用 basename，否则用完整路径（不含 .md）
    basename = mention_path.rsplit("/", 1)[-1]
    if basename.lower().endswith(".md"):
        basename = basename[:-3]
    resolved = linkgraph.resolve(basename, req.path)
    link_text = basename if resolved == mention_path else mention_path[:-3] if mention_path.lower().endswith(".md") else mention_path

    content = doc["content"]
    lines = content.splitlines(keepends=True)
    target_line = req.line if req.line and 0 < req.line <= len(lines) else None
    replaced = False

    def _try_replace(text: str) -> str:
        nonlocal replaced
        if replaced or link_text not in text:
            return text
        new_text = text.replace(link_text, f"[[{link_text}]]", 1)
        if new_text != text:
            replaced = True
        return new_text

    if target_line is not None:
        lines[target_line - 1] = _try_replace(lines[target_line - 1])
    if not replaced:
        for idx, line in enumerate(lines):
            lines[idx] = _try_replace(line)
            if replaced:
                break
    if not replaced:
        raise HTTPException(status_code=404, detail="未找到可转换的提及文本")

    vault.write_note(req.path, content="".join(lines), frontmatter=doc["frontmatter"])
    return {
        "code": 0,
        "data": {"linked": True, "link_text": link_text, "path": req.path},
        "message": "ok",
    }


@router.get("/tags")
async def tags_tree():
    """标签层级树：name/count/children；count = 含该前缀标签的去重笔记数。"""
    db = deps.require_db()
    rows = db.all_tag_note_pairs()  # [(note_path, tag)]
    prefix_notes: Dict[str, set] = {}
    for note_path, tag in rows:
        parts = tag.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            prefix_notes.setdefault(prefix, set()).add(note_path)

    tree: List[dict] = []
    nodes: Dict[str, dict] = {}
    for tag in sorted(prefix_notes.keys()):
        parts = tag.split("/")
        parent_parts = parts[:-1]
        node = {"name": tag, "count": len(prefix_notes[tag]), "children": []}
        nodes[tag] = node
        if parent_parts:
            parent = nodes.get("/".join(parent_parts))
            if parent is not None:
                parent["children"].append(node)
            else:  # 父级缺失（理论上不会）：挂到根
                tree.append(node)
        else:
            tree.append(node)
    return {"code": 0, "data": tree, "message": "ok"}


@router.get("/tag-notes")
async def tag_notes(tag: str):
    db = deps.require_db()
    tag = tag.strip().lstrip("#")
    notes = [{"path": p, "title": t} for p, t in db.notes_with_tag(tag)]
    return {"code": 0, "data": notes, "message": "ok"}
