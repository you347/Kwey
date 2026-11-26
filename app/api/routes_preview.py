"""只读预览路由：把非 Markdown 文件转换成消毒后的 HTML，供文件树打开。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.paths import PathError, normalize_rel, validate_relative
from app.viewers import PreviewError, preview_file

router = APIRouter(prefix="/api", tags=["preview"])


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
    # 附件索引状态（超限/截断/缺转换器）随预览一起返回，供界面提示「未完整索引」
    data["index_status"] = deps.require_db().attachment_status(rel)
    data["url"] = "/file/" + quote(rel)
    return {"code": 0, "data": data, "message": "ok"}
