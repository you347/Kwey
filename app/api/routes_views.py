"""结构化视图（Bases）路由：定义 CRUD / 查询 / 单元格写回 / 自然语言建视图。

设计要点：
- 视图定义是**用户资产**，落在 vault 内 ``views/*.base``，随 git 同步；
- 查询只读索引快照（``props`` 表），不逐篇读盘，20k 笔记也是内存过滤；
- 单元格写回必须走 ``VaultService.write_note``（统一锁 / 原子写 / 快照 / 事件 / 增量索引），
  绝不直接改文件——否则编辑器里的内容会被覆盖，索引也会失联；
- AI 建视图只产出定义，**不自动落盘**：前端确认后再 POST /api/views。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.views import ViewStore, build_schema, normalize_definition, run_query
from app.views.query import cached_snapshot
from app.views.schema import schema_for_prompt

router = APIRouter(prefix="/api/views", tags=["views"])

AI_TIMEOUT = 90.0


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _store() -> ViewStore:
    vault = deps.require_vault()
    cfg = deps.require_config()
    data = cfg.load().get("views") or {}
    dirname = str(data.get("dir") or "views")
    return ViewStore(vault.paths.root, dirname)


def _snapshot():
    return cached_snapshot(deps.require_vault().db)


def _schema() -> Dict[str, Any]:
    snapshot = _snapshot()
    return build_schema(snapshot["props"], snapshot["notes"])


# --------------------------------------------------------------------- models
class QueryRequest(BaseModel):
    definition: Optional[Dict[str, Any]] = None
    slug: Optional[str] = None
    offset: int = 0
    limit: Optional[int] = None
    include_schema: bool = False


class SaveRequest(BaseModel):
    slug: Optional[str] = None
    definition: Dict[str, Any]


class CellRequest(BaseModel):
    path: str
    key: str
    value: Any = None


class AiRequest(BaseModel):
    prompt: str
    save: bool = False


# --------------------------------------------------------------------- read
@router.get("")
async def list_views():
    store = _store()
    items = []
    for item in store.list():
        items.append({
            "slug": item["slug"],
            "path": item["path"],
            "name": item["definition"].get("name"),
            "mode": item["definition"].get("mode"),
            "folder": item["definition"].get("folder"),
            "tags": item["definition"].get("tags"),
            "definition": item["definition"],
        })
    return {"code": 0, "data": {"items": items, "total": len(items)}, "message": "ok"}


@router.get("/schema")
async def views_schema():
    return {"code": 0, "data": _schema(), "message": "ok"}


@router.post("/query")
async def query_view(req: QueryRequest):
    """执行视图：传 slug 或 definition。结果按 limit 分页，total 是命中总数。"""
    definition: Optional[Dict[str, Any]] = None
    if req.slug:
        definition = _store().read(req.slug)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"视图不存在: {req.slug}")
    if req.definition:
        definition = normalize_definition(req.definition)
    if definition is None:
        raise _bad("缺少 definition 或 slug")

    result = run_query(
        _snapshot(), definition, offset=req.offset, limit_override=req.limit
    )
    result["definition"] = definition
    if req.include_schema:
        result["schema"] = _schema()
    return {"code": 0, "data": result, "message": "ok"}


# --------------------------------------------------------------------- write
@router.post("")
async def create_view(req: SaveRequest):
    store = _store()
    if not (req.definition.get("name") or "").strip():
        raise _bad("视图需要名字")
    try:
        info = store.save(req.definition, slug=req.slug)
    except ValueError as exc:
        raise _bad(str(exc)) from None
    return {"code": 0, "data": info, "message": "ok"}


@router.delete("/{slug}")
async def delete_view(slug: str):
    if not _store().delete(slug):
        raise HTTPException(status_code=404, detail=f"视图不存在: {slug}")
    return {"code": 0, "data": {"deleted": True}, "message": "ok"}


@router.get("/{slug}")
async def get_view(slug: str):
    definition = _store().read(slug)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"视图不存在: {slug}")
    return {"code": 0, "data": {"slug": slug, "definition": definition}, "message": "ok"}


def _coerce(current: Any, value: Any) -> Any:
    """按当前值类型收敛输入（前端单元格一律传字符串/布尔）。"""
    if value is None:
        return None
    if isinstance(current, bool) or isinstance(value, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "是", "✓"}:
            return True
        if text in {"false", "0", "no", "否", ""}:
            return False
        return bool(value)
    if isinstance(current, (list, tuple)):
        if isinstance(value, (list, tuple)):
            return list(value)
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        try:
            num = float(str(value).strip())
        except (TypeError, ValueError):
            raise _bad(f"该列是数字，无法写入: {value!r}") from None
        return int(num) if num.is_integer() and isinstance(current, int) else num
    if value == "":
        return None
    return str(value)


@router.put("/cell")
async def update_cell(req: CellRequest):
    """单元格写回：只改一个 frontmatter 字段，正文原样保留。"""
    vault = deps.require_vault()
    key = (req.key or "").strip()
    if not key or key in {"path", "title", "folder", "excerpt", "mtime", "size"}:
        raise _bad(f"该列不可写: {key or '(空)'}")
    if key in {"file.name", "file.path", "file.folder", "file.mtime", "file.size"}:
        raise _bad(f"该列不可写: {key}")

    note = vault.read_note(req.path)
    frontmatter = dict(note.get("frontmatter") or {})
    current = frontmatter.get(key)
    value = _coerce(current, req.value)
    if value is None:
        frontmatter.pop(key, None)
    else:
        frontmatter[key] = value

    result = vault.write_note(req.path, content=note["content"], frontmatter=frontmatter)
    from app.views.query import invalidate_cache

    invalidate_cache()
    return {
        "code": 0,
        "data": {"path": req.path, "key": key, "value": value, **result},
        "message": "ok",
    }


# --------------------------------------------------------------------- AI
_AI_SYSTEM = """你是「Kwey 结构化视图」的定义生成器。用户用自然语言描述想看的笔记集合，\
你把它翻译成一份 JSON 视图定义。

只输出 JSON，不要解释、不要 Markdown 围栏。字段：
{
  "name": "视图名（8 字以内）",
  "mode": "table" | "cards" | "list",
  "folder": "目录前缀，没有就空字符串",
  "tags": ["必须全部包含的标签"],
  "where": [{"key": "字段名", "op": "= | != | > | >= | < | <= | contains | in | exists | empty | before | after", "value": 值}],
  "columns": ["列字段名"],
  "sort": [{"key": "字段名", "dir": "asc" | "desc"}],
  "group_by": "分组字段或空字符串",
  "limit": 200,
  "explain": "一句话说明这份视图筛了什么（给用户看）"
}

硬性要求：
1. 只能用「可用字段」里出现过的字段名，绝不能编造；
2. tags 里的标签必须来自「可用标签」；
3. 结论要宽松：用户说"最近"就用 mtime after 具体日期；说"重要"优先用 priority/rating；
4. 若用户只是要"看看某目录的笔记"，folder 填目录，columns 留空让系统自动选。"""


def _thinking_off(provider) -> Dict[str, Any]:
    host = (urlparse(str(provider.config.get("endpoint") or "")).hostname or "").lower()
    return {"enable_thinking": False} if host.endswith("aliyuncs.com") else {}


@router.post("/ai")
async def ai_create_view(req: AiRequest):
    """自然语言 → 视图定义（默认只返回，save=true 才落盘）。"""
    from app.agent.provider import AgentProvider, ProviderError

    prompt = (req.prompt or "").strip()
    if not prompt:
        raise _bad("请描述想看的笔记，例如『这周要交的项目，按截止日期排』")
    if len(prompt) > 1500:
        raise _bad("描述过长（上限 1500 字）")

    vault = deps.require_vault()
    provider = AgentProvider(vault.paths.root)
    if not provider.configured:
        raise _bad("未配置 Agent Provider：请先在设置页填写 endpoint 与 API Key")
    if str(provider.config.get("protocol") or "openai") != "openai":
        raise _bad("AI 建视图需要 openai 协议的 Provider（当前为 http）")

    schema = _schema()
    tags = _top_tags()
    user_content = (
        f"【可用字段】\n{schema_for_prompt(schema)}\n\n"
        f"【可用标签】{tags or '(无)'}\n\n"
        f"【用户需求】\n{prompt}"
    )
    messages = [
        {"role": "system", "content": _AI_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    extra = _thinking_off(provider)
    try:
        result = provider.chat(messages, extra_body=extra, timeout=AI_TIMEOUT)
    except ProviderError as exc:
        if extra and ("HTTP 400" in str(exc) or "HTTP 422" in str(exc)):
            try:
                result = provider.chat(messages, timeout=AI_TIMEOUT)
            except ProviderError as retry_exc:
                raise _bad(f"AI 生成视图失败：{retry_exc}") from None
        else:
            raise _bad(f"AI 生成视图失败：{exc}") from None

    raw = str(result.get("content") or "").strip()
    data = _extract_json(raw)
    if data is None:
        raise _bad("AI 返回的不是合法 JSON，请换个说法再试一次")

    explain = str(data.pop("explain", "") or "")
    definition = normalize_definition(data)
    if not definition["name"] or definition["name"] == "未命名视图":
        definition["name"] = prompt[:12]

    payload: Dict[str, Any] = {"definition": definition, "explain": explain}
    try:
        payload["preview"] = run_query(_snapshot(), definition, limit_override=50)
    except Exception as exc:  # noqa: BLE001 —— 定义能生成但查询失败时也要把定义交回用户
        payload["preview"] = None
        payload["warning"] = f"定义已生成，但查询失败：{exc}"

    if req.save:
        payload["saved"] = _store().save(definition)
    return {"code": 0, "data": payload, "message": "ok"}


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    import json

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start: end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


def _top_tags(limit: int = 40) -> List[str]:
    try:
        pairs = deps.require_vault().db.tag_counts()
    except Exception:  # noqa: BLE001
        return []
    return [tag for tag, _count in pairs[:limit]]


# 注意：`/{slug}` 必须在所有固定路径（/cell、/ai、/query …）之后注册，
# 否则 PUT /api/views/cell 会被当成 slug=cell 命中。
@router.put("/{slug}")
async def update_view(slug: str, req: SaveRequest):
    store = _store()
    if not store.exists(slug):
        raise HTTPException(status_code=404, detail=f"视图不存在: {slug}")
    info = store.save(req.definition, slug=slug)
    return {"code": 0, "data": info, "message": "ok"}
