"""知识保鲜只读路由（P2-D）：单篇状态、替代路径与降权开关状态。

不提供写入口；字段本身仍由 VaultService 统一写路径修改。前端的
「一键关闭降权」走 ``PUT /api/settings`` 的 ``freshness.downweight``。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api import deps
from app.freshness import (
    STATE_UNKNOWN,
    FreshnessIndex,
    annotate,
    downweight_enabled,
    parse_as_of_param,
)
from app.paths import normalize_rel

router = APIRouter(prefix="/api/freshness", tags=["freshness"])


@router.get("/note")
async def note_freshness(
    path: str = Query(..., description="vault 相对路径"),
    as_of: Optional[str] = Query(default=None, description="按该时点评估；缺省为当前"),
):
    vault = deps.require_vault()
    rel = normalize_rel(path)
    if not rel:
        raise HTTPException(status_code=400, detail="path 不能为空")
    db = deps.require_db()
    if db.get_note(rel) is None:
        raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
    try:
        as_of_dt = parse_as_of_param(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    index = FreshnessIndex(db, as_of=as_of_dt, historical=as_of_dt is not None)
    info = index.meta(rel)
    data = {
        "path": rel,
        "downweight": downweight_enabled(deps.get_config()),
        **annotate(info),
    }
    if info["state"] != STATE_UNKNOWN:
        data["replacement"] = index.replacement(rel)
    return {"code": 0, "data": data, "message": "ok"}
