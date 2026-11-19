"""A8 上下文组装路由（可选，集成卡注册后可用）。

端点：
- GET /api/context?query=&limit=            → 轻量候选（select_files）
- GET /api/context/build?query=&max_chars=&focus_path=&depth=  → {text, sources}

统一响应 {code,data,message}；只读，不改索引/文件。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.api import deps
from app.index.context import build_context, select_files

router = APIRouter(prefix="/api/context", tags=["context"])


@router.get("")
async def context_candidates(
    query: str = Query(default="", description="检索词，支持 path:/tag:/file: 操作符"),
    limit: int = Query(default=30, ge=1, le=100),
):
    vault = deps.require_vault()
    return {
        "code": 0,
        "data": {"files": select_files(vault, query, limit)},
        "message": "ok",
    }


@router.get("/build")
async def context_build(
    query: str = Query(default="", description="检索词"),
    max_chars: int = Query(default=12000, ge=500, le=100000),
    focus_path: Optional[str] = Query(default=None, description="聚焦笔记，优先纳入并作种子"),
    depth: int = Query(default=1, ge=1, le=4),
):
    vault = deps.require_vault()
    data = build_context(vault, query, max_chars, focus_path, depth)
    return {"code": 0, "data": data, "message": "ok"}
