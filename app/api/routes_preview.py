"""只读预览路由：把非 Markdown 文件转换成消毒后的 HTML，供文件树打开。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api import deps
from app.paths import PathError, is_ignored, normalize_rel, validate_relative
from app.viewers import PreviewError, preview_cache_dir, preview_file
from app.viewers.images import (
    ImageConversionError,
    image_display_path,
    thumbnail_path,
)

router = APIRouter(prefix="/api", tags=["preview"])


def _image_file(path: str):
    """图片路由共用的路径校验（返回 vault / rel / abs）。"""
    vault = deps.require_vault()
    rel = normalize_rel(path)
    if not rel:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    if is_ignored(rel):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        validate_relative(rel)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    abs_path = vault.paths.to_abs(rel)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {rel}")
    return vault, rel, abs_path


@router.get("/preview/image")
async def preview_image(path: str):
    """浏览器可显示的图片（SVG 已消毒，tiff/heic 已转 PNG）。"""
    vault, _rel, abs_path = _image_file(path)
    try:
        file_path, media_type = image_display_path(abs_path, preview_cache_dir(vault.paths))
    except ImageConversionError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=60"}
    if media_type == "image/svg+xml":
        # 双保险：即使消毒遗漏，直接打开该 URL 也不能执行脚本
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return FileResponse(str(file_path), media_type=media_type, headers=headers)


@router.get("/preview/thumb")
async def preview_thumb(path: str):
    """文件树缩略图（位图压到 72px；SVG 返回消毒版）。"""
    vault, _rel, abs_path = _image_file(path)
    try:
        file_path, media_type = thumbnail_path(abs_path, preview_cache_dir(vault.paths))
    except ImageConversionError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"}
    if media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return FileResponse(str(file_path), media_type=media_type, headers=headers)


@router.get("/preview")
async def preview(path: str, refresh: int = 0):
    """返回 {kind: html|image|pdf, html?, url, format, title, size, cached?}。"""
    vault = deps.require_vault()
    rel = normalize_rel(path)
    if not rel:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    try:
        validate_relative(rel)
        data = preview_file(vault.paths, rel, refresh=bool(refresh))
    except PathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    # 附件索引状态（超限/截断/缺转换器）随预览一起返回，供界面提示「未完整索引」；
    # 图片的 not_applicable 由 preview_file 提供，不应被空状态覆盖。
    status = deps.require_db().attachment_status(rel)
    if status is not None:
        data["index_status"] = status
    elif "index_status" not in data:
        data["index_status"] = None
    if data.get("kind") == "image":
        data["url"] = "/api/preview/image?path=" + quote(rel)
    else:
        data["url"] = "/file/" + quote(rel)
    return {"code": 0, "data": data, "message": "ok"}
