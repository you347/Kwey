"""跨 Agent 会话蒸馏草案：预览、提交、人工确认落地（C §6.2 / §6.6 D7–D8）。

- 预览 / 提交复用 ``kwey.session.distill`` 的校验与提取，不新增 contract 工具。
- 确认入口复用 A 的人工审批边界：批准必须持有桌面原生桥签发的一次性
  ``ApprovalGrants`` 凭据（与值班提案同一授权表），HTTP 端点不能自行签发。
- 批准后由 ``approve_draft`` 经 VaultService 统一写路径创建最小证据笔记与
  蒸馏笔记；状态机 pending_review → approved / rejected / expired 幂等。
- 过期草案在读取时即清理引用段，之后不可恢复、不可批准。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.agent import session_distill as sd
from app.api import deps
from app.api.guard import require_token
from app.duty.approval import GrantError

router = APIRouter(prefix="/api/session", tags=["session"])


class DraftSubmitRequest(BaseModel):
    payload: Dict[str, Any]


class DraftDecisionRequest(BaseModel):
    decision: str
    version: Optional[int] = None
    build_hash: str = ""
    reason: str = ""
    approval_grant: Optional[str] = None


def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def _root():
    return deps.require_vault().paths.root


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, sd.DraftNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sd.DraftStateError):
        return HTTPException(status_code=exc.code, detail=str(exc))
    if isinstance(exc, sd.DistillError):
        return HTTPException(status_code=400, detail=f"会话输入校验失败: {exc}")
    if isinstance(exc, GrantError):
        return HTTPException(status_code=exc.code, detail=str(exc))
    return HTTPException(status_code=500, detail=f"会话蒸馏错误: {exc}")


# ---------------------------------------------------------------------- 预览 / 草案
@router.post("/drafts/preview")
async def preview_draft(req: DraftSubmitRequest, request: Request):
    """纯计算预览：返回候选 / 来源 / 遗漏 / build_hash，不落盘、不产生 draft_id。"""
    require_token(request, "read")
    try:
        return _ok(sd.preview(req.payload))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/drafts")
async def submit_draft(req: DraftSubmitRequest, request: Request):
    """持久提交：写 .kwey 控制文件并返回待审 draft_id（幂等）。"""
    require_token(request, "write")
    try:
        return _ok(sd.create_draft(_root(), req.payload))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/drafts")
async def list_drafts(
    request: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    require_token(request, "read")
    try:
        rows = sd.list_drafts(_root(), status=status, limit=limit)
        return _ok({"drafts": rows, "total": len(rows)})
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/drafts/{draft_id}")
async def draft_detail(draft_id: str, request: Request):
    require_token(request, "read")
    try:
        draft = sd.load_draft(_root(), draft_id)
        data = sd.draft_summary(draft)
        data.update(
            {
                "schema_version": draft.get("schema_version"),
                "parser_version": draft.get("parser_version"),
                "retention": draft.get("retention"),
                "export_hash": draft.get("export_hash"),
                "client_export_hash": draft.get("client_export_hash"),
                "candidates": draft.get("candidates") or {},
                "sources": draft.get("sources") or {},
                "omissions": draft.get("omissions") or [],
                "stats": draft.get("stats") or {},
            }
        )
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ---------------------------------------------------------------------- 决策
@router.post("/drafts/{draft_id}/decision")
async def draft_decision(draft_id: str, req: DraftDecisionRequest, request: Request):
    """拒绝只改队列状态；批准必须持有 A 的一次性审批 grant 并经 VaultService 落笔记。"""
    record = require_token(request, "write")
    decision = str(req.decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision 仅支持 approve / reject")
    root = _root()

    if decision == "reject":
        try:
            current = sd.load_draft(root, draft_id)
            if req.version is not None and int(req.version) != int(
                current.get("version") or 1
            ):
                raise HTTPException(
                    status_code=409, detail="草案版本已变化，请重新载入"
                )
            return _ok(sd.reject_draft(root, draft_id, reason=req.reason))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc

    try:
        draft = sd.load_draft(root, draft_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc

    status = str(draft.get("status") or "")
    if status == sd.STATUS_APPROVED:
        # 终态幂等：重复确认不再要求 grant，也不会重复建笔记。
        return _ok(sd.approved_result(draft, duplicate=True))
    if status == sd.STATUS_REJECTED:
        raise HTTPException(status_code=409, detail="草案已被拒绝，不能批准")
    if status == sd.STATUS_EXPIRED:
        raise HTTPException(status_code=410, detail="草案已过期，不可恢复")

    if not req.approval_grant:
        raise HTTPException(
            status_code=403,
            detail="批准需要桌面端一次性审批凭据；纯 --server 模式仅支持预览",
        )

    version = int(draft.get("version") or 1)
    if req.version is not None and int(req.version) != version:
        raise HTTPException(status_code=409, detail="草案版本已变化，请重新载入")
    current = sd.draft_build_hash(draft)
    if req.build_hash and str(req.build_hash) != current:
        raise HTTPException(status_code=409, detail="构建 hash 已变化，请重新载入草案")

    supervisor = deps.require_duty_supervisor()
    try:
        supervisor.consume_session_grant(
            req.approval_grant,
            draft_id=draft_id,
            version=version,
            build_hash=current,
        )
    except GrantError as exc:
        raise _translate(exc) from exc

    try:
        result = sd.approve_draft(
            root,
            draft_id,
            vault=deps.require_vault(),
            expected_build_hash=current,
            approved_by=str(record.get("name") or "local-ui"),
        )
        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
