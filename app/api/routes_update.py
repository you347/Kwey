"""版本检查路由：GET /api/update/check（只读，不下载、不替换文件）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app import __version__
from app.api import deps
from app.update import check, current_version

router = APIRouter(prefix="/api/update", tags=["update"])


def _config() -> dict:
    cfg = deps.get_config()
    if cfg is None:
        return {}
    return dict(cfg.get("update") or {})


@router.get("/check")
async def update_check(force: bool = Query(False)):
    # 网络检查最长 6 秒；失败返回 ok=false，不抛 500
    data = check(_config(), force=force)
    data["app_version"] = current_version()
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/version")
async def version():
    return {"code": 0, "data": {"version": __version__}, "message": "ok"}
