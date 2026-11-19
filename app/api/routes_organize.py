"""Agent 整理路由：diff 预演 / 确认应用。

- ``POST /api/agent/organize/preview``：ops 直传或经 Provider 生成后预演（不写盘）。
- ``POST /api/agent/organize/apply``：confirm=true 后应用（走 VaultService）。

A4 权限治理：两端点均需 **write** 级 token（guard.require_token）；
无 / 无效 token → 401，权限不足 → 403，鉴权在业务逻辑前完成。

统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛 ``HTTPException``，
由 ``server.py`` 的异常处理器统一包装为 ``{code,data,message}``。
所有写操作经 ``deps.require_vault()`` 取得的 VaultService。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agent.ops import apply_ops, preview_ops, validate_ops
from app.agent.provider import AgentProvider, ProviderError
from app.api import deps
from app.api.guard import require_token

router = APIRouter(prefix="/api/agent", tags=["organize"])


class PreviewRequest(BaseModel):
    """预演请求：给了 ops 直接预演；否则经 Provider 生成 ops。"""

    instruction: Optional[str] = None
    paths: Optional[List[str]] = None
    ops: Optional[List[Dict[str, Any]]] = None
    use_provider: bool = True


class ApplyRequest(BaseModel):
    """应用请求：confirm 必须显式为 true。"""

    ops: List[Dict[str, Any]]
    confirm: bool = False


@router.post("/organize/preview")
async def organize_preview(request: Request, req: PreviewRequest):
    """预演（write 级鉴权）：给了 ops 直接算 diff；否则经 Provider 生成 ops 再算 diff。"""
    require_token(request, "write")
    vault = deps.require_vault()

    if req.ops is not None:
        ops = req.ops
    else:
        if not req.use_provider:
            raise HTTPException(
                status_code=400,
                detail=(
                    "未提供 ops 且 use_provider=false，无法生成操作；"
                    "请传入 ops 或启用 provider"
                ),
            )
        root = vault.paths.ensure_root()
        provider = AgentProvider(root)
        try:
            ops = provider.run(req.instruction or "", req.paths)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = preview_ops(ops, vault)
    # 回传 ops：UI 展示 diff 并让用户确认后，可原样送到 /organize/apply
    data["ops"] = ops
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/organize/apply")
async def organize_apply(request: Request, req: ApplyRequest):
    """应用（write 级鉴权）：confirm 必须为 true；validate → apply → 返回结果。"""
    require_token(request, "write")
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "需要用户确认：请先将预演结果展示给用户，"
                "并把 confirm 设为 true 后再应用"
            ),
        )
    vault = deps.require_vault()
    validate_ops(req.ops)
    data = apply_ops(req.ops, vault)
    return {"code": 0, "data": data, "message": "ok"}
