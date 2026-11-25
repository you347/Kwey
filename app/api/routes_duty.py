"""值班 Agent API（INNOVATION-PLAN §4.9）。

统一 ``{code,data,message}`` 信封；全部端点经 ``guard.require_token``：
读取 read，写配置/试跑/决策 write。批准额外要求**桌面原生桥**签发的一次性
approval_grant —— HTTP 端点不提供签发入口，纯 ``--server`` 模式因此默认不可 apply。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.api import deps
from app.api.guard import require_token
from app.duty.policy import DutyPolicyError
from app.duty.recipes import DutyRecipeError
from app.duty.store import DutyConflictError, DutyStateError

router = APIRouter(prefix="/api/duty", tags=["duty"])


class DutyUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    scope: Optional[Union[str, List[str]]] = None
    schedule: Optional[Dict[str, Any]] = None
    limits: Optional[Dict[str, int]] = None
    config_version: Optional[int] = None


class RunRequest(BaseModel):
    config_version: Optional[int] = None


class DecisionRequest(BaseModel):
    version: int
    proposal_hash: str = ""
    decision: str
    reason: str = ""
    approval_grant: Optional[str] = None


class PauseRequest(BaseModel):
    all: bool = False
    duty_id: Optional[str] = None


def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def _supervisor():
    return deps.require_duty_supervisor()


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"不存在: {exc.args[0] if exc.args else exc}")
    if isinstance(exc, (DutyConflictError, DutyStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (DutyPolicyError, DutyRecipeError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"值班服务错误: {exc}")


# ---------------------------------------------------------------------- 概览
@router.get("")
async def duty_overview(request: Request):
    require_token(request, "read")
    try:
        return _ok(_supervisor().overview())
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.put("/{duty_id}")
async def duty_update(duty_id: str, req: DutyUpdateRequest, request: Request):
    require_token(request, "write")
    patch = {key: value for key, value in req.model_dump().items() if value is not None}
    try:
        definition = _supervisor().update_definition(duty_id, patch)
        return _ok(definition)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/{duty_id}/run")
async def duty_run(duty_id: str, req: RunRequest, request: Request):
    require_token(request, "write")
    supervisor = _supervisor()
    try:
        supervisor.ensure_seeded()
        definition = supervisor.store.get_definition(duty_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"职责不存在: {duty_id}")
        if req.config_version is not None and int(req.config_version) != int(
            definition["config_version"]
        ):
            raise HTTPException(status_code=409, detail="配置版本已变化，请刷新后重试")
        result = supervisor.runtime.enqueue(duty_id, trigger="manual")
        if not result.get("queued"):
            reason = str(result.get("reason") or "")
            code = 429 if reason in ("queue_full", "rate_limited") else 409
            raise HTTPException(status_code=code, detail=f"未入队: {reason}")
        return _ok(
            {
                "run_id": str(result["run"]["run_id"]),
                "status": str(result["run"]["status"]),
                "queued": True,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ---------------------------------------------------------------------- 运行记录
@router.get("/runs")
async def duty_runs(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    cursor: Optional[int] = Query(default=None, ge=0),
):
    require_token(request, "read")
    try:
        return _ok(_supervisor().list_runs(limit=limit, cursor=int(cursor or 0)))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ---------------------------------------------------------------------- 提案
@router.get("/proposals")
async def duty_proposals(
    request: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[int] = Query(default=None, ge=0),
):
    require_token(request, "read")
    try:
        return _ok(
            _supervisor().list_proposals(
                status=status, limit=limit, cursor=int(cursor or 0)
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.get("/proposals/{proposal_id}")
async def duty_proposal_detail(proposal_id: str, request: Request):
    require_token(request, "read")
    try:
        return _ok(_supervisor().proposal_detail(proposal_id))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


@router.post("/proposals/{proposal_id}/decision")
async def duty_decision(proposal_id: str, req: DecisionRequest, request: Request):
    require_token(request, "write")
    supervisor = _supervisor()
    decision = str(req.decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision 仅支持 approve / reject")

    if decision == "reject":
        try:
            return _ok(
                supervisor.applier.reject(
                    proposal_id,
                    version=int(req.version),
                    reason=req.reason,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc

    # approve：必须持有桌面原生桥签发的一次性 grant。
    if not req.approval_grant:
        raise HTTPException(
            status_code=403,
            detail="批准需要桌面端一次性审批凭据；纯 --server 模式仅支持预览",
        )
    try:
        result = supervisor.applier.apply(
            proposal_id,
            version=int(req.version),
            proposal_hash=req.proposal_hash,
            grant=req.approval_grant,
        )
        if result.get("status") in ("applied", "partial"):
            try:
                detail = supervisor.store.get_proposal(proposal_id) or {}
                paths = [
                    str(op.get("path") or "")
                    for op in (detail.get("ops") or [])
                ]
                supervisor.mark_applied_paths(paths)
            except Exception:  # noqa: BLE001
                pass
        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# ---------------------------------------------------------------------- 暂停
@router.post("/pause")
async def duty_pause(req: PauseRequest, request: Request):
    require_token(request, "write")
    try:
        if not req.all and not req.duty_id:
            raise HTTPException(status_code=400, detail="必须指定 all 或 duty_id")
        return _ok(_supervisor().pause(all=req.all, duty_id=req.duty_id))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
