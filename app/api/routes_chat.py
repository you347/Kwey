"""内置智能体聊天路由（卡B）：UI-facing SSE 步骤流。

- ``POST /api/agent/chat``：携带对话历史 messages（+ 可选 resume）驱动内核，
  以 SSE 返回步骤事件（tool_call / tool_result / confirm_required / message / error / done）。
- ``POST /api/agent/render``：把模型回复文本渲染成安全 HTML（只读，无落盘，read 级即可）。
  前端用它把助手气泡从纯文本升级为 Markdown；渲染失败时前端保留纯文本兜底。
- A4 权限治理：需 **write** 级 token（guard.require_token），鉴权在流开始前完成，
  失败返回普通 JSON 错误而非流；UI 调用方需携带签发的 token（集成卡适配）。
- 内核为同步生成器（urllib 阻塞调用），StreamingResponse 由 Starlette 在线程池迭代，
  不阻塞事件循环。危险操作经 confirm_required 暂停，客户端批准后带 resume 续跑。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import feedback, kernel
from app.api import deps
from app.api.guard import require_token
from app.parser.renderer import render_chat_markdown

router = APIRouter(prefix="/api/agent", tags=["chat"])


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    resume: Optional[Dict[str, Any]] = None
    # 前端上报的当前工作区：{"path": "项目/计划.md", "kind": "note"|"dir"}，
    # 只用于 system prompt 的指代解析，路径由内核再次校验（越界/不存在则忽略）。
    context: Optional[Dict[str, Any]] = None


class RenderRequest(BaseModel):
    """聊天回复渲染请求：正文上限 200k（模型单条回复远低于此）。"""

    content: str = Field(min_length=1, max_length=200_000)


class FeedbackRequest(BaseModel):
    """用户对某条消息的评价：up / down / clear（清除）。"""

    id: str = Field(min_length=1, max_length=80)
    rating: str
    content: str = ""
    role: str = "assistant"


def _frame(data: Dict[str, Any]) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


@router.post("/feedback")
async def agent_feedback(request: Request, req: FeedbackRequest):
    """记录点赞 / 点踩（write 级）；用于系统提示里的轻量调教。"""
    require_token(request, "write")
    vault = deps.require_vault()
    if req.rating not in ("up", "down", "clear"):
        raise HTTPException(status_code=400, detail="rating 仅支持 up / down / clear")
    if req.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role 仅支持 user / assistant")
    data = feedback.upsert(
        vault.paths.root,
        fid=req.id,
        rating=req.rating,
        content=req.content,
        role=req.role,
    )
    return {"code": 0, "data": data, "message": "ok"}


@router.post("/render")
async def agent_render(request: Request, req: RenderRequest):
    """聊天回复 → 安全 HTML（纯渲染，不碰 vault，read 级 token 即可）。"""
    require_token(request, "read")
    return {"code": 0, "data": {"html": render_chat_markdown(req.content)}, "message": "ok"}


@router.post("/chat")
async def agent_chat(request: Request, req: ChatRequest):
    """驱动内置智能体（write 级鉴权）；vault 未打开时由 deps.require_vault() 抛 400（流开始前）。"""
    require_token(request, "write")
    vault = deps.require_vault()

    def gen():
        try:
            for event in kernel.run_chat(
                req.messages, vault=vault, resume=req.resume, context=req.context
            ):
                yield _frame(event)
        except Exception as exc:  # noqa: BLE001 兜底：任何漏网异常都成帧，不抛穿流
            yield _frame({"type": "error", "message": str(exc)})
            yield _frame({"type": "done", "messages": req.messages, "pending": None})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )