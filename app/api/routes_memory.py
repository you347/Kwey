"""F3 个人信息库路由：三分区读写 / 50/30/20 配额注入 / 闭环更新。

端点（全部 UI-facing，与 ``/api/note``、``/api/trash`` 同级，不要求 agent token；
Agent 侧访问记忆走 MCP resources / memory tools，由 A2 收口）：

- ``GET  /api/memory``              三分区记忆 + 配额约定
- ``GET  /api/memory/{section}``    单分区（projects / profile / lessons）
- ``PUT  /api/memory/{section}``    整分区保存（用户手动编辑 / 面板保存）
- ``POST /api/memory/inject``       配额注入预览（按 max_chars 拼上下文）
- ``POST /api/memory/update``       diff 确认后的闭环更新（A6/organize 调用）

约定：统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛
``HTTPException(status_code, detail)``，由 ``server.py`` 统一包装。
所有读写都落在 ``<vault>/.kwey/memory/``，**不读写任何用户笔记正文**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import deps
from app.memory.store import (
    DEFAULT_BUDGET,
    MAX_BUDGET,
    MIN_BUDGET,
    SECTIONS,
    MemoryStore,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])

MAX_PAYLOAD_ENTRIES = 200  # 单次保存的条目数上限（防止误传整库）


class SectionWriteRequest(BaseModel):
    """整分区保存请求：entries 既接受字符串，也接受 {content, weight, ...} 对象。"""

    entries: List[Any] = Field(default_factory=list)
    source: str = "manual"


class InjectRequest(BaseModel):
    """注入预览请求：默认只读；touch=true 时记录「被使用」并轻微升权。"""

    max_chars: int = DEFAULT_BUDGET
    redistribute: bool = False
    touch: bool = False


class UpdateRequest(BaseModel):
    """闭环更新请求：ops 来自已确认的 diff，diff_result 为 apply_ops 的返回值。"""

    ops: List[Any] = Field(default_factory=list)
    diff_result: Optional[Dict[str, Any]] = None


def _store() -> MemoryStore:
    """当前 vault 的记忆库（vault 未打开 → deps 抛 400）。"""
    return MemoryStore(deps.require_vault().paths.ensure_root())


def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def _section_or_404(section: str) -> str:
    if section not in SECTIONS:
        raise HTTPException(
            status_code=404,
            detail=f"未知记忆分区: {section}（可选 {' / '.join(SECTIONS)}）",
        )
    return section


def _validate_entries(items: List[Any]) -> None:
    """保存前逐条校验：空内容 / 超长 / 非法权重都明确报 400（不做静默丢弃）。"""
    for index, item in enumerate(items, 1):
        if isinstance(item, dict):
            content = item.get("content")
            weight = item.get("weight")
            if weight is not None:
                try:
                    float(weight)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"第 {index} 条记忆的 weight 不是数字"
                    ) from exc
        elif isinstance(item, str):
            content = item
        else:
            raise HTTPException(
                status_code=400,
                detail=f"第 {index} 条记忆格式非法（需字符串或 {{content: ...}} 对象）",
            )
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail=f"第 {index} 条记忆内容为空")
        if len(content.strip()) > MemoryStore.MAX_CONTENT:
            raise HTTPException(
                status_code=400,
                detail=f"第 {index} 条记忆过长（上限 {MemoryStore.MAX_CONTENT} 字）",
            )


@router.get("")
async def get_memory() -> Dict[str, Any]:
    """三分区记忆全量（含配额比例、文件路径、按分数排序的条目）。"""
    return _ok(_store().snapshot())


@router.post("/inject")
async def inject_memory(req: InjectRequest) -> Dict[str, Any]:
    """按 50/30/20 配额拼装注入上下文（默认只读；touch=true 才记录使用）。"""
    if req.max_chars < MIN_BUDGET or req.max_chars > MAX_BUDGET:
        raise HTTPException(
            status_code=400,
            detail=f"max_chars 超出范围（{MIN_BUDGET}~{MAX_BUDGET}）",
        )
    try:
        data = _store().build_injection(
            int(req.max_chars),
            redistribute=bool(req.redistribute),
            touch=bool(req.touch),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok(data)


@router.post("/update")
async def update_memory(req: UpdateRequest) -> Dict[str, Any]:
    """diff 确认落盘后的闭环更新：提取新记忆 → 去重合并 → 衰减过期条目。

    空 ``ops`` 也可调用，等价于「立即执行一次过期衰减」（手动触发入口）。
    """
    if len(req.ops) > MAX_PAYLOAD_ENTRIES:
        raise HTTPException(
            status_code=400, detail=f"单次更新 ops 过多（上限 {MAX_PAYLOAD_ENTRIES}）"
        )
    try:
        data = _store().update_from_interaction(req.ops, req.diff_result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok(data)


@router.get("/{section}")
async def get_memory_section(section: str) -> Dict[str, Any]:
    """单分区负载（面板切换分区时懒加载）。"""
    section = _section_or_404(section)
    return _ok(_store().section_payload(section))


@router.put("/{section}")
async def put_memory_section(section: str, req: SectionWriteRequest) -> Dict[str, Any]:
    """整分区保存：逐条规范化 → 去重合并 → 原子写回 Markdown。"""
    section = _section_or_404(section)
    if len(req.entries) > MAX_PAYLOAD_ENTRIES:
        raise HTTPException(
            status_code=400,
            detail=f"单分区条目过多（上限 {MAX_PAYLOAD_ENTRIES}）",
        )
    _validate_entries(req.entries)
    try:
        data = _store().replace(section, req.entries, source=req.source or "manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok(data)
