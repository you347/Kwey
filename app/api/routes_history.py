"""历史快照路由（卡片 M2-4，卡A W2 收口）：git 时间线查询与一键回滚。

- 快照服务统一取 ``deps.require_snapshots()``（单例 + 运行时刷新），
  与 VaultService 自动快照共享同一实例；原按 root 懒加载缓存已移除。
- 统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛 ``HTTPException``。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps

router = APIRouter(prefix="/api/history", tags=["history"])


class RollbackRequest(BaseModel):
    """POST /api/history/rollback 请求体。"""

    hash: str
    path: str
    confirm: bool = False


@router.get("/status")
async def history_status():
    """git 可用 / 仓库 / HEAD / 提交数。"""
    return {"code": 0, "data": deps.require_snapshots().status(), "message": "ok"}


@router.get("/log")
async def history_log(path: Optional[str] = None, limit: int = 50):
    """时间线；``path`` 缺省为全库。"""
    data = deps.require_snapshots().log(rel=path or None, limit=limit)
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/show")
async def history_show(hash: str, path: str):
    """返回指定快照的文件文本（data 为字符串）。"""
    data = deps.require_snapshots().show(hash, path)
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/rollback")
async def history_rollback(req: RollbackRequest):
    """一键回滚；必须 ``confirm=true``，并把当前 vault 传入以触发重索引。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="回滚需要 confirm=true 显式确认")
    vault = deps.require_vault()
    data = deps.require_snapshots().rollback(req.hash, req.path, vault=vault)
    return {"code": 0, "data": data, "message": "ok"}
