"""笔记路由：读取/创建/保存/删除、wikilink 解析、渲染、嵌入。"""

from __future__ import annotations

import mimetypes
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.models import NoteCreateRequest, NoteUpdateRequest, RenderRequest
from app.parser.outline import extract_outline
from app.parser.renderer import render_markdown
from app.paths import normalize_rel

router = APIRouter(prefix="/api", tags=["notes"])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}


@router.get("/note")
async def get_note(path: str):
    vault = deps.require_vault()
    return {"code": 0, "data": vault.read_note(path), "message": "ok"}


@router.post("/note")
async def create_note(req: NoteCreateRequest):
    vault = deps.require_vault()
    meta = vault.create_note(req.path, content=req.content, frontmatter=req.frontmatter)
    return {"code": 0, "data": meta, "message": "ok"}


@router.put("/note")
async def update_note(req: NoteUpdateRequest):
    vault = deps.require_vault()
    result = vault.write_note(
        req.path, content=req.content, frontmatter=req.frontmatter, base_mtime=req.base_mtime
    )
    return {"code": 0, "data": result, "message": "ok"}


@router.delete("/note")
async def delete_note(path: str):
    vault = deps.require_vault()
    trash_path = vault.delete_note(path)
    return {"code": 0, "data": {"trash_path": trash_path}, "message": "ok"}


@router.get("/note/resolve")
async def resolve_note(target: str):
    """wikilink 目标解析；不存在时 exists=false（前端提示创建）。"""
    linkgraph = deps.require_linkgraph()
    db = deps.require_db()
    resolved = linkgraph.resolve(target)
    if resolved is None:
        return {"code": 0, "data": {"exists": False, "path": None, "title": target}, "message": "ok"}
    note = db.get_note(resolved)
    title = note["title"] if note else resolved.rsplit("/", 1)[-1]
    return {
        "code": 0,
        "data": {"exists": True, "path": resolved, "title": title},
        "message": "ok",
    }


@router.get("/note/render")
async def render_note(path: str):
    """服务端 markdown-it 渲染（预览兜底）：wikilink/嵌入/标签/图片路径已处理。"""
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    doc = vault.read_note(path)
    resolver = lambda target, source=None: linkgraph.resolve(target)  # noqa: E731
    html = render_markdown(doc["content"], resolve=resolver)
    return {"code": 0, "data": {"html": html, "path": doc["path"], "title": doc["title"]}, "message": "ok"}


@router.post("/note/render")
async def render_note_content(req: RenderRequest):
    """实时预览：渲染编辑器当前正文（未落盘），不改动磁盘内容。"""
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    doc = vault.read_note(req.path)
    content = doc["content"] if req.content is None else req.content
    resolver = lambda target, source=None: linkgraph.resolve(target)  # noqa: E731
    html = render_markdown(content, resolve=resolver)
    return {"code": 0, "data": {"html": html, "path": doc["path"], "title": doc["title"]}, "message": "ok"}


@router.get("/note/embed")
async def embed_note(path: str):
    """![[嵌入]] 内容块：md 渲染正文；图片直出 <img>；其他文件给链接。"""
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    rel = normalize_rel(path)
    abs_path = vault.paths.to_abs(rel)
    if not abs_path.is_file():
        html = f'<div class="kwey-embed kwey-embed--missing">未找到嵌入目标: {rel}</div>'
        return {"code": 0, "data": {"html": html, "exists": False}, "message": "ok"}

    suffix = abs_path.suffix.lower()
    if suffix in IMAGE_EXTS:
        html = f'<img class="kwey-embed-image" src="/file/{rel}" alt="{rel}"/>'
    elif suffix in {".md", ".markdown"}:
        doc = vault.read_note(rel)
        resolver = lambda target, source=None: linkgraph.resolve(target)  # noqa: E731
        html = render_markdown(doc["content"], resolve=resolver)
    else:
        media = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
        html = (
            f'<a class="kwey-embed-file" href="/file/{rel}" data-type="{media}">{rel}</a>'
        )
    return {"code": 0, "data": {"html": html, "exists": True}, "message": "ok"}


@router.get("/note/outline")
async def note_outline(path: str):
    """大纲：ATX 标题层级/文本/行号（跳过围栏代码块与行内代码）。"""
    vault = deps.require_vault()
    doc = vault.read_note(path)
    return {
        "code": 0,
        "data": {"headings": extract_outline(doc["content"])},
        "message": "ok",
    }
