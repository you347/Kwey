"""文件管理路由：新建笔记/目录、改名/移动（自动全库链接重写）、删除进 .trash/。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api import deps
from app.models import CreateDirRequest, CreateNoteRequest, MoveRequest, RenameRequest
from app.paths import normalize_rel, sanitize_filename

router = APIRouter(prefix="/api/files", tags=["files"])

MARKDOWN_EXTS = (".md", ".markdown")


def _join(dir_rel: str, name: str) -> str:
    dir_rel = normalize_rel(dir_rel)
    return f"{dir_rel}/{name}" if dir_rel else name


@router.post("/create-note")
async def create_note(req: CreateNoteRequest):
    vault = deps.require_vault()
    name = sanitize_filename(req.name)
    if not name.lower().endswith(MARKDOWN_EXTS):
        name += ".md"
    rel = _join(req.dir, name)
    meta = vault.create_note(rel, dedup=True)
    return {"code": 0, "data": meta, "message": "ok"}


@router.post("/create-dir")
async def create_dir(req: CreateDirRequest):
    vault = deps.require_vault()
    name = sanitize_filename(req.name)
    rel = vault.create_dir(_join(req.dir, name))
    return {"code": 0, "data": {"path": rel}, "message": "ok"}


@router.post("/rename")
async def rename(req: RenameRequest):
    vault = deps.require_vault()
    result = vault.rename_note(req.path, req.new_name)
    return {"code": 0, "data": result, "message": "ok"}


@router.post("/move")
async def move(req: MoveRequest):
    vault = deps.require_vault()
    result = vault.move_note(req.path, req.new_dir)
    return {"code": 0, "data": result, "message": "ok"}


@router.delete("/dir")
async def delete_dir(path: str, confirm: int = Query(default=0)):
    vault = deps.require_vault()
    trash_path = vault.delete_dir(path, confirm=bool(confirm))
    return {"code": 0, "data": {"trash_path": trash_path}, "message": "ok"}
