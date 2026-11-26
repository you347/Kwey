"""纯文本 / CSV 读写路由：就地编辑不注入 frontmatter，保留原编码与换行。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import deps
from app.models import PlainFileUpdateRequest
from app.vault.provenance import LOCAL_UI_ACTOR

router = APIRouter(prefix="/api", tags=["plain"])


@router.get("/plain-file")
async def read_plain_file(path: str):
    vault = deps.require_vault()
    return {"code": 0, "data": vault.read_plain_text(path), "message": "ok"}


@router.put("/plain-file")
async def write_plain_file(req: PlainFileUpdateRequest):
    vault = deps.require_vault()
    data = vault.write_plain_text(
        req.path,
        req.content,
        base_mtime=req.base_mtime,
        actor=LOCAL_UI_ACTOR,
    )
    return {"code": 0, "data": data, "message": "ok"}
