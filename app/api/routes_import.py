"""库导入路由（P0）：扫描预览 / 执行导入。

- ``POST /api/import/scan``  只读预览：文件数、体积、冲突、断链、重名。
- ``POST /api/import/apply`` 落盘：笔记走 VaultService 统一写路径，附件直接复制。
- 路由用同步 ``def``（FastAPI 会放进线程池），避免大库导入阻塞事件循环。
- 源目录非法 / 策略错误 → 400；单文件失败不中断整批，收在 data.failed 里。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.vault.importer import VaultImporter

router = APIRouter(prefix="/api/import", tags=["import"])


class ScanRequest(BaseModel):
    source: str
    include_attachments: bool = True
    conflict: str = "rename"


class ApplyRequest(BaseModel):
    source: str
    include_attachments: bool = True
    conflict: str = "rename"


@router.post("/scan")
def import_scan(req: ScanRequest):
    vault = deps.require_vault()
    try:
        data = VaultImporter(vault).scan(
            req.source,
            include_attachments=req.include_attachments,
            conflict=req.conflict,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/apply")
def import_apply(req: ApplyRequest):
    vault = deps.require_vault()
    try:
        data = VaultImporter(vault).apply(
            req.source,
            include_attachments=req.include_attachments,
            conflict=req.conflict,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}
