"""F4 RAG 问答（C5）：build_context 组证据 → Provider 生成带引用的答案 → 引用校验防幻觉。

降级链（任何一环不可用都不静默失败，明确标注来源）：
- Provider 未配置 / 非 openai 协议 / 调用失败 → 摘录式回答（列最相关 3 篇标题+片段），
  ``used_provider=False``，answer 开头明确"未配置 Provider，这是检索摘录"；
- 上下文为空（空库 / 无召回）→ answer 给出"库中未找到相关内容"，sources=[]。

引用规范：答案中的 ``[[path]]`` / ``[[path|显示名]]`` 必须与 sources 中的 path 完全一致
（vault 相对正斜杠）；不存在的引用（幻觉）被剥离并在 ``hallucinated`` 中列出，供 UI 标注。

统一响应 ``{"code":0,"data":{answer,sources,used_provider,hallucinated,question},"message":"ok"}``。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.provider import AgentProvider, ProviderError
from app.api import deps
from app.freshness import parse_as_of_param
from app.index.context import build_context

router = APIRouter(prefix="/api/rag", tags=["rag"])

DEFAULT_MAX_CHARS = 12000
MIN_MAX_CHARS = 1000
MAX_MAX_CHARS = 40000
MAX_QUESTION_CHARS = 2000
EXCERPT_TOP_N = 3
EXCERPT_CHARS = 400

# 匹配 [[path]] 与 [[path|显示名]]；path 允许中文/斜杠/点/空格/连字符
_WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")

# system 提示：强约束只依据上下文回答、必须带合法引用、无依据就说不知道
_ANSWER_SYSTEM = (
    "你是个人知识库问答助手。只依据用户提供的【参考资料】回答问题，不要编造库中没有的内容。"
    "回答末尾必须列出你实际使用的笔记引用，格式为 [[笔记路径]]（路径必须与参考资料中"
    "「[来源: 路径]」标注完全一致）。如果参考资料不足以回答，直接说“知识库中没有相关内容”。"
    "回答用与问题相同的语言，简洁清晰。"
)


class RagAskRequest(BaseModel):
    question: str
    max_chars: Optional[int] = None
    # P2-D：缺省为当前事实；显式历史时点按 as_of 评估有效期，不按今天压低。
    as_of: Optional[str] = None


def _enrich_sources(vault, raw_sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """sources 补 title（build_context 只给 path/score/reason），保留可溯源字段。"""
    db = getattr(vault, "db", None)
    out: List[Dict[str, Any]] = []
    for s in raw_sources:
        path = str(s.get("path") or "")
        if not path:
            continue
        title = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if db is not None:
            try:
                note = db.get_note(path)
                if note is not None and note.get("title"):
                    title = str(note["title"])
            except Exception:  # noqa: BLE001 —— 标题取不到用文件名兜底
                pass
        item: Dict[str, Any] = {
            "path": path,
            "title": title,
            "reason": str(s.get("reason") or ""),
        }
        if s.get("freshness"):
            item["freshness"] = s["freshness"]
        if s.get("replacement_for"):
            item["replacement_for"] = str(s["replacement_for"])
        out.append(item)
    return out


def _excerpt_answer(question: str, sources: List[Dict[str, str]], vault) -> str:
    """无 Provider 时的摘录式回答：最相关 3 篇的标题 + 正文片段 + 来源引用。"""
    header = "未配置 Agent Provider，这是检索摘录（配置 Provider 后可生成完整答案）：\n\n"
    if not sources:
        return header + "知识库中没有找到与问题相关的内容。"

    lines: List[str] = []
    for s in sources[:EXCERPT_TOP_N]:
        path, title = s["path"], s["title"]
        snippet = ""
        try:
            doc = vault.read_note(path)  # 路径来自索引，已校验；失败则只列标题
            snippet = " ".join(str(doc.get("content") or "").split())[:EXCERPT_CHARS]
        except Exception:  # noqa: BLE001 —— 单篇读取失败不影响其余摘录
            pass
        lines.append(f"- **{title}** [[{path}]]\n  {snippet}" if snippet else f"- **{title}** [[{path}]]")
    return header + "\n".join(lines)


def _provider_answer(question: str, ctx_text: str, vault) -> str:
    """用 Provider 的 chat 能力生成文本答案（run() 只产出 ops，不能生成自由文本）。"""
    provider = AgentProvider(vault.paths.root)
    if not provider.configured:
        raise ProviderError("未配置 Agent Provider")
    if str(provider.config.get("protocol") or "openai") != "openai":
        raise ProviderError("RAG 问答需要 openai 协议的 Provider（当前为 http）")
    messages = [
        {"role": "system", "content": _ANSWER_SYSTEM},
        {
            "role": "user",
            "content": f"【参考资料】\n{ctx_text}\n\n【问题】\n{question}",
        },
    ]
    result = provider.chat(messages)
    content = result.get("content")
    if not content or not str(content).strip():
        raise ProviderError("Provider 未返回答案内容")
    return str(content).strip()


def _validate_citations(answer: str, sources: List[Dict[str, str]]) -> Tuple[str, List[str]]:
    """剥离幻觉引用；返回 (清洗后的答案, 被剥离的引用列表)。"""
    valid = {s["path"] for s in sources}
    hallucinated: List[str] = []

    def repl(match: re.Match) -> str:
        raw = match.group(1).strip()
        if raw in valid:
            return match.group(0)  # 合法引用原样保留（含显示名）
        hallucinated.append(raw)
        return ""

    cleaned = _WIKILINK_RE.sub(repl, answer)
    # 清理引用剥离后可能留下的多余空行/孤立标点
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, hallucinated


@router.post("/ask")
async def rag_ask(req: RagAskRequest):
    """知识库问答：带引用答案 / 无 Provider 摘录降级。"""
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(
            status_code=400, detail=f"question 过长（最多 {MAX_QUESTION_CHARS} 字符）"
        )
    max_chars = req.max_chars if req.max_chars is not None else DEFAULT_MAX_CHARS
    if max_chars < MIN_MAX_CHARS or max_chars > MAX_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"max_chars 取值范围 {MIN_MAX_CHARS}–{MAX_MAX_CHARS}",
        )

    vault = deps.require_vault()
    try:
        as_of = parse_as_of_param(req.as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx = build_context(vault, question, max_chars=max_chars, as_of=as_of)
    sources = _enrich_sources(vault, ctx.get("sources") or [])

    used_provider = False
    hallucinated: List[str] = []
    if not str(ctx.get("text") or "").strip() and not sources:
        answer = "知识库中没有找到与问题相关的内容。"
    else:
        try:
            answer = _provider_answer(question, str(ctx.get("text") or ""), vault)
            used_provider = True
        except ProviderError:
            answer = _excerpt_answer(question, sources, vault)

    # 摘录式回答自己生成的引用一定合法；Provider 答案需校验防幻觉
    if used_provider:
        answer, hallucinated = _validate_citations(answer, sources)

    return {
        "code": 0,
        "data": {
            "answer": answer,
            "sources": sources,
            "used_provider": used_provider,
            "hallucinated": hallucinated,
            "question": question,
            "freshness": ctx.get("freshness") or {},
        },
        "message": "ok",
    }
