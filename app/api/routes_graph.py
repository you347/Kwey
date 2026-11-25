"""图/标签路由：反向链接、未链接提及、一键转双链、标签层级树、标签笔记列表。"""

from __future__ import annotations

from html import escape
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.models import LinkifyRequest
from app.parser.wikilink import extract_wikilinks, split_anchor
from app.paths import normalize_rel
from app.vault.provenance import LOCAL_UI_ACTOR

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

    vault.write_note(
        req.path,
        content="".join(lines),
        frontmatter=doc["frontmatter"],
        actor=LOCAL_UI_ACTOR,
    )
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


# ---------------------------------------------------------------- 图谱数据
def _collect_graph(db, allowed: Optional[set]) -> dict:
    """组装图谱数据。allowed=None 表示全库；否则只保留该邻域内的笔记节点。

    nodes 只含 md 笔记；edges 只含已解析链接（按 source/target/kind 去重）；
    unresolved 为断链（source_path + 原始 target 文本，按二元组去重）。
    """
    notes = db.all_notes()
    node_map = {n["path"]: n["title"] for n in notes}
    node_paths = set(node_map) if allowed is None else (set(node_map) & set(allowed))
    nodes = [{"path": p, "title": node_map[p]} for p in sorted(node_paths)]

    edges: List[dict] = []
    seen_edges = set()
    for link in db.all_links():
        target = link["target_path"]
        if not target:  # 未解析链接单列 unresolved
            continue
        source = link["source_path"]
        if allowed is not None and (source not in node_paths or target not in node_paths):
            continue
        key = (source, target, link["kind"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({"source": source, "target": target, "kind": link["kind"]})

    unresolved: List[dict] = []
    seen_unres = set()
    for link in db.unresolved_links():
        source = link["source_path"]
        if allowed is not None and source not in node_paths:
            continue
        key = (source, link["target"])
        if key in seen_unres:
            continue
        seen_unres.add(key)
        unresolved.append({"source": source, "target": link["target"]})
    return {"nodes": nodes, "edges": edges, "unresolved": unresolved}


@router.get("/full")
async def graph_full():
    """全库图谱：nodes 仅 md 笔记；edges 仅已解析链接（去重）；unresolved 为断链。"""
    db = deps.require_db()
    return {"code": 0, "data": _collect_graph(db, None), "message": "ok"}


@router.get("/local")
async def graph_local(path: str, depth: int = 1):
    """局部图谱：以 path 为中心，双向（入链+出链）扩展 depth 跳邻域。"""
    if depth < 1 or depth > 3:
        raise HTTPException(status_code=400, detail="depth 仅支持 1..3")
    linkgraph = deps.require_linkgraph()
    db = deps.require_db()
    center = normalize_rel(path)
    visited = {center}
    frontier = {center}
    for _ in range(depth):
        following = set()
        for node in frontier:
            following |= linkgraph.out_links_of(node)
            following |= linkgraph.backlinks(node)
        following -= visited
        visited |= following
        frontier = following
    return {"code": 0, "data": _collect_graph(db, visited), "message": "ok"}
