"""F2 自动推送路由（卡片 F2）：配置读写 / 立即推送 / 状态查询 / 远端绑定。

- 懒加载 ``AutoPushService``，按 vault root 字符串缓存（不改 deps.py）；
  vault 切换时停掉旧 root 的定时器，避免向旧仓库推送。
- 统一响应 ``{"code":0,"data":...,"message":"ok"}``；校验错误抛 400。
  推送冲突 / 失败是合法业务结果（code=0，data.status=paused/error），不是 HTTP 错误。
- 远端绑定（``POST /bind``）把地址写进 ``.git/config``，Kwey 自己的 json 只存
  remote 名；地址内嵌口令时返回 ``warning`` 但不阻断。
- 接线（由整合者完成，见卡片报告）：server.py 注册本 router；lifespan 打开
  vault 后调 ``start_autopush()``、退出前调 ``stop_autopush()``；
  POST /api/vault/open 成功后同样调 ``start_autopush()``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.vault.autopush import AutoPushService

router = APIRouter(prefix="/api/autopush", tags=["autopush"])

# root 字符串 → AutoPushService（懒加载缓存；root 变化即停旧定时器）
_AUTOPUSH: Dict[str, AutoPushService] = {}


class AutopushConfigRequest(BaseModel):
    """PUT /api/autopush/config 请求体（部分更新；last_push 由服务维护不可写）。"""

    enabled: Optional[bool] = None
    remote: Optional[str] = None
    branch: Optional[str] = None
    time: Optional[str] = None
    commit_template: Optional[str] = None
    include_dirs: Optional[List[str]] = None


class BindRequest(BaseModel):
    """POST /api/autopush/bind 请求体（地址 + 远端名 + 是否允许改地址）。"""

    url: str
    name: str = "origin"
    replace: bool = False


def _get_autopush(vault) -> AutoPushService:
    root = str(vault.paths.root)
    for old_root, svc in list(_AUTOPUSH.items()):
        if old_root != root:
            svc.stop()
            del _AUTOPUSH[old_root]
    svc = _AUTOPUSH.get(root)
    if svc is None:
        svc = AutoPushService(Path(root))
        _AUTOPUSH[root] = svc
    return svc


def start_autopush() -> None:
    """启动补推 + 定时器（lifespan / vault open 后调用；vault 未打开则忽略）。"""
    vault = deps.get_vault()
    if vault is not None:
        _get_autopush(vault).start()


def stop_autopush() -> None:
    """停止全部定时器并清缓存（lifespan 退出 / 测试清理用）。"""
    for svc in _AUTOPUSH.values():
        svc.stop()
    _AUTOPUSH.clear()


@router.get("/config")
async def autopush_config():
    vault = deps.require_vault()
    return {"code": 0, "data": _get_autopush(vault).get_config(), "message": "ok"}


@router.put("/config")
async def autopush_update_config(req: AutopushConfigRequest):
    vault = deps.require_vault()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        data = _get_autopush(vault).update_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/run")
async def autopush_run():
    """立即推送（手动触发不受 enabled 限制）；冲突返回 data.status=paused。"""
    vault = deps.require_vault()
    data = _get_autopush(vault).run(reason="manual")
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/status")
async def autopush_status():
    vault = deps.require_vault()
    return {"code": 0, "data": _get_autopush(vault).status(), "message": "ok"}


@router.get("/remotes")
async def autopush_remotes():
    """列已绑定远端 + 仓库当前分支（设置页绑定 UI 的数据源）。"""
    vault = deps.require_vault()
    svc = _get_autopush(vault)
    return {
        "code": 0,
        "data": {
            "remotes": svc.list_remotes(),
            "current_branch": svc.current_branch(),
            "repo": svc.repo_exists,
            "git_available": svc._git_available(),
        },
        "message": "ok",
    }


@router.post("/bind")
async def autopush_bind(req: BindRequest):
    """绑定或替换远端地址（写入 .git/config；不保存到 Kwey 配置）。"""
    vault = deps.require_vault()
    try:
        data = _get_autopush(vault).bind_remote(
            req.url, name=req.name, replace=req.replace
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}