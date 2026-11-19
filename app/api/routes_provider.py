"""编排者接线：Agent Link 首次引导 token、Provider 配置读写。

- ``POST /api/agent/bootstrap``：令牌库为空时签发首个 admin token（本地 UI 冷启动）。
  保持无 token 可用，但仅空库生效（已有 token → 409，防越权重置）。
- ``GET/PUT /api/agent/provider-config``：读写 ``<vault>/.kwey/provider.json``。
  A4 权限治理：均需 **write** 级 token（guard.require_token）。
  GET 时 api_key 打码，PUT 遇到掩码值保留原密钥，避免前端回写抹掉密钥。

零新依赖；配置写入使用临时文件 + os.replace 原子替换。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agent.auth import TokenStore
from app.agent.provider import DEFAULTS
from app.api import deps
from app.api.guard import require_token

router = APIRouter(prefix="/api/agent", tags=["agent-setup"])

MASK = "********"


class ProviderConfigBody(BaseModel):
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    protocol: Optional[str] = None
    timeout: Optional[float] = None
    max_context_chars: Optional[int] = None


def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def _config_path() -> Path:
    vault = deps.require_vault()
    return Path(vault.paths.root) / ".kwey" / "provider.json"


def _read() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    path = _config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS and v is not None})
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # 损坏配置回退默认值，不把文件内容写进异常
    return cfg


def _write(cfg: Dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@router.get("/provider-config")
async def get_provider_config(request: Request):
    require_token(request, "write")  # 配置含端点/密钥信息，读也按 write 级收敛
    cfg = _read()
    masked = dict(cfg)
    if masked.get("api_key"):
        masked["api_key"] = MASK
    masked["configured"] = bool(str(cfg.get("endpoint") or "").strip())
    return _ok(masked)


@router.put("/provider-config")
async def put_provider_config(request: Request, body: ProviderConfigBody):
    require_token(request, "write")
    cfg = _read()
    for key in DEFAULTS:
        value = getattr(body, key, None)
        if value is None:
            continue
        if key == "api_key" and value == MASK:
            continue  # 掩码回写视为「不修改」
        if key == "protocol" and value not in ("openai", "http"):
            raise HTTPException(status_code=400, detail="protocol 仅支持 openai / http")
        cfg[key] = value
    _write(cfg)
    return _ok({"configured": bool(str(cfg.get("endpoint") or "").strip())})


@router.post("/bootstrap")
async def bootstrap_token(request: Request):
    """引导签发本地 UI 的 admin token。

    - 令牌库为空：任何来源都可签发首个 token（本地应用冷启动）；
    - 库非空：**仅本地回环**（浏览器 UI）可再签发一个新 admin token——解决
      用户清了 localStorage / 换浏览器后 token 丢失、但磁盘仍有 token 导致
      本地 UI 全部 401 的死锁；远程来源仍 409，防止越权重置。

    签发的新 token 与旧 token 并存（不吊销），调用方负责保存。
    """
    vault = deps.require_vault()
    store = TokenStore(Path(vault.paths.root))
    host = (request.client.host if request.client else "") or ""
    loopback = host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if store.list() and not loopback:
        raise HTTPException(
            status_code=409, detail="已有访问 token，请通过 /api/agent/tokens 管理"
        )
    # 本地重签：先清掉旧的 local-ui token，避免反复打开/清缓存导致 token 列表无限增长。
    # 只吊销 name=local-ui 的（Agent 自己用 /tokens 签的具名 token 不受影响）。
    if loopback:
        store.revoke_by_name("local-ui")
    return _ok(store.issue("admin", "local-ui"))
