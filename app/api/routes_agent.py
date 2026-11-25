"""Agent Link REST / SSE 接口（M2 / T11，卡A W1 改薄）。

- 所有端点从 ``X-Kwey-Token`` 或 ``Authorization: Bearer <token>`` 取 token，
  按 read / write / admin 三级校验（见 app/agent/auth.py）。
- 统一响应 ``{"code":0,"data":...,"message":"ok"}``；错误抛 HTTPException 由 server.py 包装。
- 能力语义一律经 ``app.agent.registry``（契约 v1）：旧端点 = 鉴权 + 请求体映射 +
  响应 framing；journal 留痕收口在 registry.invoke(actor=token 记录)（W2）。
  ``POST /tool`` 为通用工具入口，``GET /contract``
  暴露契约快照。内置智能体（卡B）与外部 Agent（Verbot）共享同一 registry。
- 所有笔记写操作一律经 deps.require_vault()（VaultService 统一写路径）。
- 路径一律 vault 相对正斜杠；请求模型在本模块内定义，不改 app/models.py。
- SSE 订阅 EventBus：asyncio.Queue + loop.call_soon_threadsafe 跨线程投递（参考 app/api/ws.py）。
- router 为模块级对象，由编排者在 server.py include_router。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent.auth import ROLE_LEVEL, TokenStore
from app.agent import registry
from app.agent.registry import ContractError
from app.api import deps
from app.events import VaultEvent
from app.paths import normalize_rel

router = APIRouter(prefix="/api/agent", tags=["agent"])

SSE_HEARTBEAT_SECONDS = 25.0


# ---------------------------------------------------------------- 请求模型（本地定义）
class NoteCreateBody(BaseModel):
    path: str
    content: str = ""
    frontmatter: Optional[Dict[str, Any]] = None


class NoteWriteBody(BaseModel):
    path: str
    content: str = ""
    frontmatter: Optional[Dict[str, Any]] = None
    base_mtime: Optional[float] = None


class LinkBody(BaseModel):
    # "from" 是 Python 关键字：字段名用 from_，请求体走 alias "from"
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(..., alias="from")
    to: str


class TagsBody(BaseModel):
    path: str
    add: Optional[List[str]] = None
    remove: Optional[List[str]] = None


class TokenBody(BaseModel):
    action: str  # issue | revoke | list
    role: Optional[str] = None
    name: Optional[str] = None
    token: Optional[str] = None


class ToolCallBody(BaseModel):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- 公共 helper
def _ok(data: Any) -> Dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def _vault_root() -> Path:
    """当前 vault 绝对路径。未打开 vault 时由 deps.require_vault() 抛 400。"""
    return deps.require_vault().paths.ensure_root()


def _store() -> TokenStore:
    # 每次构造轻量实例（无内存态），与「外部引导签发」共享同一份磁盘数据 + 全局锁
    return TokenStore(_vault_root())


def _extract_token(request: Request) -> str:
    """优先级：X-Kwey-Token > Authorization: Bearer <token>。"""
    token = (request.headers.get("X-Kwey-Token") or "").strip()
    if token:
        return token
    auth = (request.headers.get("Authorization") or "").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _auth(request: Request, required_role: str) -> Dict[str, Any]:
    """按端点所需等级校验，返回命中的 token 记录（含 role）。"""
    return _store().verify(_extract_token(request), required_role)


def _invoke(
    name: str, arguments: Dict[str, Any], actor: Optional[Dict[str, Any]] = None
) -> Any:
    """契约统一入口：ContractError → HTTPException（400 校验 / 404 未知工具）。

    actor 传 token 记录，journal 留痕收口在 registry.invoke（W2）。
    """
    try:
        return registry.invoke(name, arguments, actor=actor)
    except ContractError as exc:
        raise HTTPException(status_code=exc.code, detail=exc.message) from exc


# ---------------------------------------------------------------- read 端点
@router.get("/health")
async def agent_health(request: Request):
    record = _auth(request, "read")
    vault = deps.require_vault()
    info = vault.info()
    return _ok({
        "vault": info["path"],
        "note_count": info["note_count"],
        "role": record["role"],
    })


@router.get("/search")
async def agent_search(
    request: Request,
    q: str = Query(default="", description="支持 path:/tag:/file: 操作符"),
    limit: int = Query(default=50, ge=1, le=200),
    mode: str = Query(default="fts", description="fts（默认 BM25）；hybrid = FTS+向量+链接图 RRF 融合，向量不可用时降级纯 FTS"),
    as_of: Optional[str] = Query(default=None, description="按该时点评估有效期；缺省为当前"),
):
    _auth(request, "read")
    started = time.perf_counter()
    args: Dict[str, Any] = {"query": q, "limit": limit, "mode": mode}
    if as_of is not None:
        args["as_of"] = as_of
    try:
        data = _invoke("kwey.search", args)
    except HTTPException:
        raise
    except Exception as exc:  # 兜底：漏网的 FTS5 语法错误不应 500
        raise HTTPException(status_code=400, detail=f"查询语法不受支持: {exc}") from exc
    data["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return _ok(data)


@router.get("/note")
async def agent_get_note(
    request: Request,
    path: str = Query(..., description="vault 相对正斜杠路径"),
    as_of: Optional[str] = Query(default=None, description="按该时点评估有效期；缺省为当前"),
):
    _auth(request, "read")
    args: Dict[str, Any] = {"path": path}
    if as_of is not None:
        args["as_of"] = as_of
    return _ok(_invoke("kwey.read", args))


# ---------------------------------------------------------------- write 端点
@router.post("/note")
async def agent_create_note(request: Request, body: NoteCreateBody):
    rec = _auth(request, "write")
    meta = _invoke("kwey.create", {
        "path": body.path, "content": body.content, "frontmatter": body.frontmatter,
    }, actor=rec)
    return _ok(meta)


@router.put("/note")
async def agent_update_note(request: Request, body: NoteWriteBody):
    rec = _auth(request, "write")
    result = _invoke("kwey.write", {
        "path": body.path,
        "content": body.content,
        "frontmatter": body.frontmatter,
        "base_mtime": body.base_mtime,
    }, actor=rec)
    return _ok(result)


@router.delete("/note")
async def agent_delete_note(
    request: Request,
    path: str = Query(..., description="vault 相对正斜杠路径"),
):
    rec = _auth(request, "write")
    data = _invoke("kwey.delete", {"path": path}, actor=rec)
    return _ok(data)


@router.post("/link")
async def agent_add_link(request: Request, body: LinkBody):
    """在源笔记正文末尾追加 ``[[to]]``，语义经 registry（kwey.link）。"""
    rec = _auth(request, "write")
    target = normalize_rel(body.to)  # 统一正斜杠
    data = _invoke("kwey.link", {"from": body.from_, "to": target}, actor=rec)
    # linked_to 为 M2 旧响应键，保留兼容；link 为契约 canonical 键
    return _ok({**data, "linked_to": data["link"]})


@router.post("/tags")
async def agent_edit_tags(request: Request, body: TagsBody):
    """改 frontmatter.tags（add / remove），语义经 registry（kwey.tags）。"""
    rec = _auth(request, "write")
    data = _invoke(
        "kwey.tags",
        {"path": body.path, "add": body.add, "remove": body.remove},
        actor=rec,
    )
    return _ok(data)


# ---------------------------------------------------------------- 契约端点（卡A W1）
@router.get("/contract")
async def agent_contract(request: Request):
    """契约快照：version + 全量工具元数据（read 级即可读，工具清单非机密）。"""
    _auth(request, "read")
    return _ok(registry.contract())


@router.post("/tool")
async def agent_tool(request: Request, body: ToolCallBody):
    """通用工具入口：与内置智能体共享 registry.invoke，按工具 permission 鉴权。

    先验 read 级 token（未知工具对无 token 者不暴露），再按工具所需等级二次校验；
    写工具成功后按 journal_action 留痕（W2 收口进 registry.invoke + actor）。
    """
    rec = _auth(request, "read")
    try:
        tool = registry.get_tool(body.name)  # 未知工具 → 404
        if ROLE_LEVEL[rec["role"]] < ROLE_LEVEL[tool["permission"]]:
            raise HTTPException(
                status_code=403,
                detail=f"工具 {body.name} 需要 {tool['permission']} 权限",
            )
        data = registry.invoke(body.name, body.arguments, actor=rec)
    except ContractError as exc:
        raise HTTPException(status_code=exc.code, detail=exc.message) from exc
    return _ok(data)


# ---------------------------------------------------------------- graph 端点
@router.get("/graph")
async def agent_graph(
    request: Request,
    path: str = Query(..., description="中心笔记路径"),
    depth: int = Query(default=1, ge=1, le=3, description="双向扩展跳数 1..3"),
):
    """以 path 为中心、双向（入链 + 出链）扩展 depth 跳邻域，返回 {nodes, edges}。"""
    _auth(request, "read")
    linkgraph = deps.require_linkgraph()
    db = deps.require_db()
    center = normalize_rel(path)
    visited = {center}
    frontier = {center}
    for _ in range(depth):
        following: set = set()
        for node in frontier:
            following |= linkgraph.out_links_of(node)
            following |= linkgraph.backlinks(node)
        following -= visited
        visited |= following
        frontier = following
    # 只用 LinkGraph / IndexDatabase 公开方法组装邻域（nodes 仅 md 笔记，
    # edges 仅邻域内已解析链接，按 source/target/kind 去重），不依赖其它路由的私有函数
    node_map = {n["path"]: n["title"] for n in db.all_notes()}
    allowed = visited & set(node_map)
    nodes = [{"path": p, "title": node_map[p]} for p in sorted(allowed)]
    edges: List[Dict[str, Any]] = []
    seen: set = set()
    for link in db.all_links():
        src, tgt, kind = link["source_path"], link["target_path"], link["kind"]
        if not tgt or src not in allowed or tgt not in allowed:
            continue
        key = (src, tgt, kind)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": src, "target": tgt, "kind": kind})
    return _ok({"nodes": nodes, "edges": edges})


@router.get("/audit")
async def agent_audit(
    request: Request,
    actor: str = Query(default="", description="调用方名字（token 的 name，如 verbot）；空为全部"),
    role: str = Query(default="", description="角色过滤：read / write / admin"),
    action: str = Query(default="", description="动作过滤，如 create / write / delete / move"),
    path: str = Query(default="", description="路径子串过滤，如 projects/"),
    since: float = Query(default=0.0, description="起始 Unix 时间戳（秒）；0 为不限"),
    limit: int = Query(default=50, ge=1, le=500),
):
    """写入归因：谁在什么时候改了哪个笔记（外部 agent 的 token name 即可定位）。"""
    _auth(request, "read")
    arguments: Dict[str, Any] = {"limit": limit}
    if actor:
        arguments["actor"] = actor
    if role:
        arguments["role"] = role
    if action:
        arguments["action"] = action
    if path:
        arguments["path"] = path
    if since:
        arguments["since"] = since
    return _ok(_invoke("kwey.audit", arguments))


# ---------------------------------------------------------------- SSE 端点
def _sse_frame(data: Dict[str, Any]) -> str:
    """紧凑 JSON + SSE data 帧（首帧严格为 data: {\"event\":\"connected\"}\\n\\n）。"""
    return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def sse_event_stream() -> AsyncGenerator[str, None]:
    """EventBus → SSE 帧生成器。

    模块级暴露，便于单测直接取首个 yield，不依赖会阻塞的流式 TestClient。
    EventBus 的 handler 可能在 watcher / VaultService 写线程被同步调用，
    因此必须用 loop.call_soon_threadsafe 把事件投递回 SSE 所在的事件循环线程
    （与 app/api/ws.py 的 WsHub._on_event 同构）。
    """
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
    bus = deps.require_eventbus()

    def _on_event(event: VaultEvent) -> None:
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event.to_dict())
        except RuntimeError:  # loop 已关闭
            pass

    bus.subscribe(None, _on_event)
    try:
        yield _sse_frame({"event": "connected"})
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse_frame(data)
    finally:
        # 客户端断开 / 生成器 aclose 时触发，避免订阅泄漏
        bus.unsubscribe(None, _on_event)


@router.get("/events")
async def agent_events(request: Request):
    """SSE 事件流。鉴权在响应开始前完成，失败返回普通 JSON 错误而非流。"""
    _auth(request, "read")
    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- token 管理端点
@router.post("/tokens")
async def agent_manage_tokens(request: Request, body: TokenBody):
    """admin 专用：issue / revoke / list。"""
    _auth(request, "admin")
    store = _store()
    if body.action == "issue":
        role = body.role or "read"
        if role not in ROLE_LEVEL:
            raise HTTPException(
                status_code=400,
                detail=f"非法角色: {role}（仅 read/write/admin）",
            )
        return _ok(store.issue(role, body.name or ""))
    if body.action == "revoke":
        if not body.token:
            raise HTTPException(status_code=400, detail="revoke 需要 token 字段")
        return _ok({"token": body.token, "revoked": store.revoke(body.token)})
    if body.action == "list":
        return _ok({"tokens": store.list()})
    raise HTTPException(status_code=400, detail="action 仅支持 issue / revoke / list")
