"""系统路由：health、vault 打开/信息、文件树、设置、附件上传、静态直出。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api import deps
from app.api.ws import hub
from app.models import SettingsUpdateRequest, VaultOpenRequest
from app.paths import is_ignored, normalize_rel, validate_relative

router = APIRouter(prefix="/api", tags=["system"])

THEMES = {"light", "dark"}
EDITOR_MODES = {"source", "preview", "split"}


@router.get("/health")
async def health():
    vault = deps.get_vault()
    return {
        "code": 0,
        "data": {
            "status": "ok",
            "vault": str(vault.paths.root) if vault else None,
            "indexed": vault.db.count_notes() if vault else 0,
        },
        "message": "ok",
    }


@router.post("/vault/open")
async def vault_open(req: VaultOpenRequest):
    from pathlib import Path as _Path

    root = _Path(req.path).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {req.path}")
    service = deps.init_services(root, open_now=True)
    hub.bind_eventbus()  # EventBus 随 vault 重建，WS 出口重新订阅
    service.start_watcher()
    deps.start_duty()    # A0 值班：新库 generation、Timer/worker 重新订阅
    # F2：切换 vault 后启动自动推送（补推 + 定时器）；内部 daemon 线程非阻塞
    from app.api.routes_autopush import start_autopush
    start_autopush()
    return {"code": 0, "data": service.info(), "message": "ok"}


@router.get("/vault/info")
async def vault_info():
    vault = deps.require_vault()
    return {"code": 0, "data": vault.info(), "message": "ok"}


@router.get("/tree")
async def tree():
    vault = deps.require_vault()
    return {"code": 0, "data": vault.tree(), "message": "ok"}


@router.get("/settings")
async def get_settings():
    config = deps.require_config()
    return {"code": 0, "data": config.all(), "message": "ok"}


@router.put("/settings")
async def put_settings(req: SettingsUpdateRequest):
    config = deps.require_config()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "theme" in updates and updates["theme"] not in THEMES:
        raise HTTPException(status_code=400, detail="theme 仅支持 light/dark")
    if "editor_mode" in updates and updates["editor_mode"] not in EDITOR_MODES:
        raise HTTPException(status_code=400, detail="editor_mode 仅支持 source/preview/split")
    if "attachment_dir" in updates:
        updates["attachment_dir"] = normalize_rel(str(updates["attachment_dir"])) or "attachments"
    data = config.set(updates)
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    vault = deps.require_vault()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    rel = vault.save_attachment(file.filename or "upload.bin", data)
    return {"code": 0, "data": {"path": rel}, "message": "ok"}


# ---------------------------------------------------------------- 静态直出
# 注意：静态直出必须挂根路径（/file/...、/attachments/...），不能带 /api 前缀
static_router = APIRouter(tags=["static"])


# 静态直出上限：超大文件不允许整包塞进浏览器内存，也不允许拖垮 WebView
MAX_SERVE_BYTES = 200 * 1024 * 1024
# 这些类型直接内联会在应用 origin 里执行脚本（SVG/HTML），强制下载
_FORCE_DOWNLOAD_EXTS = {".svg", ".html", ".htm", ".xhtml", ".xml"}


def _serve_vault_file(raw_rel: str) -> FileResponse:
    vault = deps.require_vault()
    rel_norm = normalize_rel(raw_rel)
    # 保留目录/临时文件先拦（404），再做完整合法性校验
    if is_ignored(rel_norm) or rel_norm.endswith(".kwey-tmp"):
        raise HTTPException(status_code=404, detail="文件不存在")
    rel = validate_relative(raw_rel)
    abs_path = vault.paths.to_abs(rel)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {rel}")
    size = abs_path.stat().st_size
    if size > MAX_SERVE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_SERVE_BYTES // (1024 * 1024)}MB，请用本地应用打开",
        )
    media_type = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if abs_path.suffix.lower() in _FORCE_DOWNLOAD_EXTS:
        headers["Content-Disposition"] = f'attachment; filename="{abs_path.name}"'
    return FileResponse(str(abs_path), media_type=media_type, headers=headers)


@static_router.get("/file/{file_path:path}", include_in_schema=False)
async def serve_file(file_path: str):
    """任意 vault 内文件直出（预览图片/嵌入附件用）。"""
    return _serve_vault_file(file_path)


@static_router.get("/attachments/{file_path:path}", include_in_schema=False)
async def serve_attachment(file_path: str):
    """附件目录直出（等价 /file/<attachment_dir>/...）。"""
    config = deps.require_config()
    attach_dir = normalize_rel(str(config.get("attachment_dir") or "attachments"))
    return _serve_vault_file(f"{attach_dir}/{file_path}")
