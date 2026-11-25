"""F1 知识编译 API：预演（零落盘）/ 确认应用 / 库体检。

- ``POST /api/compile/preview``：raw/ 素材 → Provider 结构化 ops → diff 预演。
- ``POST /api/compile/apply``：confirm=true 才经 VaultService 落盘。
- ``GET  /api/compile/health``：孤儿 / 断链 / 字段漂移 / MOC 缺失。

统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛 ``HTTPException``，由
``app.server`` 的异常处理器统一包装。Provider 未配置时返回清晰 400，不让 UI 静默失败。

Provider 通过 :func:`get_compile_provider` 依赖注入，便于测试用 fake provider 覆盖。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent.compiler import (
    DEFAULT_RAW_DIR,
    DEFAULT_WIKI_DIR,
    assert_compile_ops,
    compile_vault,
    health_check,
)
from app.agent.ops import apply_ops
from app.agent.provider import AgentProvider, ProviderError
from app.api import deps
from app.vault.provenance import ActorContext

router = APIRouter(prefix="/api/compile", tags=["compile"])

# AI 编译 ops + 人工确认：保留 Agent 派生身份。
COMPILE_ACTOR = ActorContext(kind="agent", name="kwey-compile", origin="local-ui-apply")


def get_compile_provider() -> AgentProvider:
    """构造当前 vault 的 Provider；未配置直接 400（消息明确可操作）。"""
    vault = deps.require_vault()
    provider = AgentProvider(vault.paths.ensure_root())
    if not provider.configured:
        raise HTTPException(
            status_code=400,
            detail=(
                "未配置 Agent Provider：请在 <vault>/.kwey/provider.json 中设置 endpoint "
                "（OpenAI 兼容或 http 协议）后重试知识编译"
            ),
        )
    return provider


class PreviewRequest(BaseModel):
    """预演请求；不传 instruction 时用默认 F1 编译指令。"""

    instruction: Optional[str] = None
    raw_dir: Optional[str] = None
    wiki_dir: Optional[str] = None
    paths: Optional[List[str]] = None


class ApplyRequest(BaseModel):
    """应用请求：confirm 必须显式为 true；ops 来自 preview 原样回传。"""

    ops: List[Dict[str, Any]]
    confirm: bool = False


@router.post("/preview")
async def compile_preview(req: PreviewRequest, provider: Any = Depends(get_compile_provider)):
    """预演：生成 ops + diff + 报告，不写盘。"""
    vault = deps.require_vault()
    try:
        data = compile_vault(
            vault,
            provider,
            raw_dir=req.raw_dir or DEFAULT_RAW_DIR,
            wiki_dir=req.wiki_dir or DEFAULT_WIKI_DIR,
            dry_run=True,
            instruction=req.instruction,
            paths=req.paths,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/apply")
async def compile_apply(req: ApplyRequest):
    """确认应用：guard raw 只读 + wiki 分层后复用 apply_ops。"""
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="需要用户确认：请先展示预演 diff 并逐项勾选，再把 confirm 设为 true 后应用",
        )
    vault = deps.require_vault()
    assert_compile_ops(req.ops, DEFAULT_RAW_DIR, DEFAULT_WIKI_DIR)
    data = apply_ops(req.ops, vault, actor=COMPILE_ACTOR)
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/health")
async def compile_health():
    """只读体检；返回孤儿 / 断链 / schema 漂移 / MOC 缺失及修复建议。"""
    vault = deps.require_vault()
    data = health_check(vault)
    return {"code": 0, "data": data, "message": "ok"}
