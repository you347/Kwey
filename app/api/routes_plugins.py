"""插件系统 API（C3 / P2）：列表、启用切换、入口代码下发。

- ``GET  /api/plugins/list``：全部合法插件（含 enabled 标记）。
- ``POST /api/plugins/toggle``：``{id, enabled}`` 启用/禁用，状态持久化到
  ``<vault>/.kwey/plugins.json``；禁用后前端撤销命令注册、后端拒绝下发入口（403）。
- ``GET  /api/plugins/entry?id=``：返回已启用插件的 main.js 代码文本，
  供前端 Blob URL 动态 import；未启用 403、不存在 404。

统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛 ``HTTPException``，
由 server.py 异常处理器包装为同形状。路由由编排者在 server.py include_router。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.api import deps
from app.plugins.builtin import catalog as builtin_catalog, get_builtin
from app.plugins.registry import PluginRegistry, PLUGINS_DIRNAME

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class ToggleBody(BaseModel):
    id: str
    enabled: bool


class InstallBody(BaseModel):
    id: str


def _registry() -> PluginRegistry:
    vault = deps.require_vault()  # vault 未打开 → 400
    return PluginRegistry(Path(vault.paths.root))


def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/list")
async def list_plugins():
    """列出全部合法插件（损坏/非法清单已在注册表侧跳过）。"""
    return _ok({"plugins": _registry().list()})


@router.post("/toggle")
async def toggle_plugin(body: ToggleBody):
    """启用 / 禁用插件；返回更新后的清单条目（含 enabled 终值）。"""
    registry = _registry()
    record = registry.enable(body.id) if body.enabled else registry.disable(body.id)
    return _ok({
        "id": record["id"],
        "name": record["name"],
        "enabled": record["enabled"],
    })


@router.get("/entry")
async def get_plugin_entry(id: str = Query(..., description="插件 id")):
    """下发已启用插件的入口代码文本；未启用 403，不存在 404。"""
    return _ok(_registry().get_entry(id))


# ------------------------------------------------------------------ 插件市场（内置）
@router.get("/catalog")
async def plugin_catalog():
    """内置插件市场清单（含是否已安装标记）；不下发源码。"""
    installed = {p["id"] for p in _registry().list()}
    items = []
    for item in builtin_catalog():
        items.append({**item, "installed": item["id"] in installed})
    return _ok({"plugins": items})


@router.post("/install")
async def install_plugin(body: InstallBody):
    """安装内置插件到 <vault>/.kwey/plugins/<id>/（plugin.json + main.js 原子写）。"""
    builtin = get_builtin(body.id)
    if builtin is None:
        raise HTTPException(status_code=404, detail=f"内置插件不存在: {body.id}")
    registry = _registry()
    plugin_dir = registry.plugins_dir / body.id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": builtin["id"],
        "name": builtin["name"],
        "version": builtin["version"],
        "description": builtin["description"],
        "permissions": builtin["permissions"],
        "entry": "main.js",
    }
    import json
    import os

    for filename, content in (
        ("plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"),
        ("main.js", builtin["code"] + "\n"),
    ):
        target = plugin_dir / filename
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    record = registry._find(body.id)
    return _ok({"id": body.id, "name": builtin["name"], "installed": True,
                "enabled": bool(record and record.get("enabled"))})


@router.post("/uninstall")
async def uninstall_plugin(body: InstallBody):
    """卸载插件：从启用集合移除并删除插件目录（仅限 .kwey/plugins/ 内）。"""
    import shutil

    registry = _registry()
    target = (registry.plugins_dir / body.id).resolve()
    try:
        target.relative_to(registry.plugins_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="非法插件 id（越界）") from None
    enabled = registry._load_enabled()
    enabled.discard(body.id)
    registry._save_enabled(enabled)
    if target.is_dir():
        shutil.rmtree(target)
    return _ok({"id": body.id, "installed": False, "enabled": False})
