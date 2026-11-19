"""回收站路由：列出 / 恢复 / 永久删除（清空全部需 confirm=1 确认）。

回收站内的路径均为 .trash/ 前缀，validate_relative 会拒绝该片段（QA B2 修复），
因此所有读写由 VaultService 以「root / rel 直拼 + os.replace」完成，路由层只做转发。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api import deps
from app.models import TrashRestoreRequest

router = APIRouter(prefix="/api/trash", tags=["trash"])


@router.get("")
async def list_trash():
    vault = deps.require_vault()
    return {"code": 0, "data": {"items": vault.list_trash()}, "message": "ok"}


@router.post("/restore")
async def restore_trash(req: TrashRestoreRequest):
    vault = deps.require_vault()
    data = vault.restore_from_trash(req.trash_path, req.target_path)
    return {"code": 0, "data": data, "message": "ok"}


@router.delete("")
async def delete_trash(path: Optional[str] = Query(default=None), confirm: int = Query(default=0)):
    """path 缺省表示清空全部（高危，必须 confirm=1）；否则永久删除该单条。"""
    vault = deps.require_vault()
    if path:
        deleted = vault.purge_trash_path(path)
        return {"code": 0, "data": {"deleted": deleted}, "message": "ok"}
    if confirm != 1:
        raise HTTPException(status_code=400, detail="清空回收站需要 confirm=1 确认")
    deleted = vault.purge_trash_all()
    return {"code": 0, "data": {"deleted": deleted}, "message": "ok"}
