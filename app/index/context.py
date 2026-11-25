"""大 vault 上下文工程：分层索引 + 图扩展的选择性加载（A8）。

目的：解决 150–200 篇笔记后 Agent 无法把整库塞进上下文的"上下文墙"。
策略：
1. **master index**：全库 path / title / 标签 / 出入度摘要（轻量，按预算截断），
   Agent 先看目录再决定加载哪些笔记；
2. **召回**：FTS5（复用 routes_search 的查询解析与消毒）+ 可选向量后端（A2 hybrid 已解锁时自动用）；
3. **图扩展**：以召回分数最高者（及 focus_path）为种子，BFS 出链/反链 depth 跳；
4. **精选正文片段**：按 query 词命中度取最佳段落，严格控制在 max_chars 内；
5. **sources 可溯源**：每个进入正文的片段都记录 path / score / reason。

零新依赖：只读现有 SQLite 索引、内存链接图与磁盘 Markdown（经 VaultService），
不复制索引数据、不改索引 schema。A6 编译层 / A7 记忆注入可直接复用 build_context()。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.api.routes_search import build_match_terms, parse_query
from app.freshness import downweight_enabled, rerank
from app.index.database import IndexDatabase

_CJK_WORD_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")
_INDEX_RATIO = 0.30          # master index 最多占预算的 30%
_SNIPPET_LINE_LIMIT = 800    # 单篇正文片段上限（字符）


def _extract_terms(query: str) -> Tuple[Dict[str, Any], List[str]]:
    """从 query 提取结构化操作符与检索词（空操作符查询也能拿到词）。"""
    parsed = parse_query(query)
    terms = [t for t in parsed.get("terms") or [] if t]
    if not terms:
        terms = _CJK_WORD_RE.findall(query or "")
    return parsed, terms


def _graph_of(vault: Any):
    """从 VaultService 取内存链接图（Indexer.linkgraph），取不到返回 None。"""
    graph = getattr(vault, "linkgraph", None)
    if graph is None:
        graph = getattr(getattr(vault, "indexer", None), "linkgraph", None)
    return graph


def _vector_of(vault: Any):
    """取可选向量后端（A2 hybrid 装配后可用），未装配返回 None。"""
    return getattr(getattr(vault, "indexer", None), "vector", None)


def _fts_hits(
    db: IndexDatabase, parsed: Dict[str, Any], terms: Sequence[str], limit: int
) -> List[str]:
    """FTS5 召回；语法异常时降级为空（不把捕获交给上层 500）。"""
    match_terms = build_match_terms(list(terms))
    if not match_terms:
        return []
    try:
        hits, _total = db.search(
            terms=match_terms,
            path_prefix=parsed.get("path"),
            tag=parsed.get("tag"),
            file_pattern=parsed.get("file"),
            limit=limit,
        )
    except Exception:  # noqa: BLE001 —— FTS 语法兜底，与搜索路由一致
        return []
    return [str(h["path"]) for h in hits]


def _vector_hits(vault: Any, query: str, limit: int) -> List[str]:
    """向量召回；后端不可用或异常时降级为空。"""
    vector = _vector_of(vault)
    if vector is None or not getattr(vector, "available", False):
        return []
    try:
        pairs = vector.query(query, limit)
    except Exception:  # noqa: BLE001 —— 运行期向量失败本轮降级
        return []
    return [str(path) for path, _score in pairs]


def _graph_expand(
    graph: Any, seeds: Sequence[str], depth: int, exists
) -> Dict[str, float]:
    """从种子双向 BFS depth 跳；越近权重越高。返回 path -> graph_score。"""
    if graph is None or not seeds:
        return {}
    graph_score: Dict[str, float] = {}
    visited = {p for p in seeds if exists(p)}
    frontier = set(visited)
    for d in range(1, max(1, int(depth)) + 1):
        nxt: set = set()
        for seed in frontier:
            try:
                neighbors = set(graph.out_links_of(seed)) | set(graph.backlinks(seed))
            except Exception:  # noqa: BLE001 —— 图数据异常不阻断上下文组装
                continue
            for nb in neighbors:
                if nb in visited or not exists(nb):
                    continue
                graph_score[nb] = graph_score.get(nb, 0.0) + 1.0 / d
                nxt.add(nb)
        visited |= nxt
        frontier = nxt
        if not frontier:
            break
    return graph_score


def _rank(
    db: IndexDatabase,
    vault: Any,
    query: str,
    focus_path: Optional[str],
    depth: int,
    limit: int,
    *,
    as_of: Any = None,
    downweight: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """融合 FTS / 向量 / 图扩展 / focus 四路信号，返回有序候选与新鲜度概览。"""
    parsed, terms = _extract_terms(query)
    cand_n = min(max(limit * 3, 60), 200) if (downweight or as_of is not None) else limit
    fts = _fts_hits(db, parsed, terms, cand_n)
    vec = _vector_hits(vault, query, cand_n) if terms else []

    seeds: List[str] = []
    if focus_path and db.get_note(focus_path) is not None:
        seeds.append(focus_path)
    seeds.extend(fts[:10])
    seeds.extend(vec[:10])

    graph = _graph_of(vault)
    graph_score = _graph_expand(graph, seeds, depth, lambda p: db.get_note(p) is not None)

    scores: Dict[str, float] = {}
    reasons: Dict[str, set] = {}
    fts_set, vec_set = set(fts), set(vec)
    for i, path in enumerate(fts):
        scores[path] = scores.get(path, 0.0) + 2.0 / (i + 1)
        reasons.setdefault(path, set()).add("fts")
    for i, path in enumerate(vec):
        scores[path] = scores.get(path, 0.0) + 1.5 / (i + 1)
        reasons.setdefault(path, set()).add("vector")
    for path, score in graph_score.items():
        scores[path] = scores.get(path, 0.0) + score
        reasons.setdefault(path, set()).add("graph")
    if focus_path and focus_path in scores:
        scores[focus_path] += 3.0
        reasons[focus_path].add("focus")

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[Dict[str, Any]] = [
        {
            "path": path,
            "score": round(score, 6),
            "reason": "|".join(sorted(reasons.get(path, set()))) or "graph",
        }
        for path, score in ranked
    ]
    freshness: Dict[str, Any] = {}
    if out and (downweight or as_of is not None):
        # 与 kwey.search / REST 搜索同一口径：融合后、截断前重排。
        out, freshness = rerank(
            out,
            db,
            as_of=as_of,
            downweight=downweight,
            historical=as_of is not None,
        )
    return out[:limit], freshness


def select_files(vault: Any, query: str, limit: int = 30) -> List[Dict[str, Any]]:
    """轻量候选：只做召回与图扩展评分，不读全文，供 A6/A7 复用。"""
    db = getattr(vault, "db", None)
    if db is None:
        return []
    ranked, _freshness = _rank(
        db, vault, query, None, depth=2, limit=max(1, int(limit))
    )
    return ranked


def _master_index(
    db: IndexDatabase, graph: Any, budget: int, focus_path: Optional[str]
) -> str:
    """全库轻量目录：path | 标题 | 标签 | 出/入度；focus 笔记置顶。"""
    lines = ["# 主索引（path | 标题 | 标签 | 出/入度）"]
    notes = db.all_notes()
    if focus_path:
        notes.sort(key=lambda n: (n["path"] != focus_path, n["path"]))
    for note in notes:
        path = note["path"]
        tags = db.tags_of_note(path)
        try:
            out_deg = len(graph.out_links_of(path)) if graph else 0
            in_deg = len(graph.backlinks(path)) if graph else 0
        except Exception:  # noqa: BLE001
            out_deg = in_deg = 0
        tag_text = ",".join(tags[:3])
        lines.append(f"- {path} | {note['title']} | {tag_text} | {out_deg}/{in_deg}")
        if sum(len(s) + 1 for s in lines) > budget:
            lines.pop()
            omitted = len(notes) - (len(lines) - 1)
            lines.append(f"…（索引已按预算截断，另有 {omitted} 篇未列出）")
            break
    return "\n".join(lines)


def _best_snippet(content: str, terms: Sequence[str], max_chars: int) -> str:
    """按 query 词命中度挑段落；无命中取开头；严格不超过 max_chars。"""
    if max_chars <= 0:
        return ""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", content or "") if b.strip()]
    if not blocks:
        return ""
    if terms:
        def score_block(block: str) -> int:
            low = block.lower()
            return sum(low.count(t.lower()) for t in terms if t)
        blocks.sort(key=score_block, reverse=True)
    picked: List[str] = []
    used = 0
    for block in blocks:
        room = max_chars - used
        if room <= 0:
            break
        piece = block[:room]
        picked.append(piece)
        used += len(piece)
    return "\n\n".join(picked)[:max_chars]


def build_context(
    vault: Any,
    query: str,
    max_chars: int = 12000,
    focus_path: Optional[str] = None,
    depth: int = 1,
    *,
    as_of: Any = None,
    downweight: Optional[bool] = None,
) -> Dict[str, Any]:
    """组装可塞进 Agent 上下文的文本；返回 {text, sources, freshness}。

    text 严格 <= max_chars；sources 中每个 path 都在 text 中以「[来源: path]」标注，
    便于 A6 编译层 / A7 记忆层引用与回溯。``as_of`` 缺省为当前；历史期间查询
    按该时点评估有效期，不按今天压低当时正确的证据。
    """
    budget = max(0, int(max_chars))
    db = getattr(vault, "db", None)
    if db is None:
        return {"text": "", "sources": []}
    if downweight is None:
        downweight = downweight_enabled(getattr(vault, "config", None))
    graph = _graph_of(vault)
    _, terms = _extract_terms(query)

    index_budget = max(240, int(budget * _INDEX_RATIO))
    index_text = _master_index(db, graph, min(index_budget, budget), focus_path)[:budget]
    parts = [index_text]
    used = len(index_text)

    sources: List[Dict[str, Any]] = []
    candidates, freshness = _rank(
        db,
        vault,
        query,
        focus_path,
        depth,
        limit=max(20, budget // 300),
        as_of=as_of,
        downweight=downweight,
    )
    for cand in candidates:
        if used >= budget:
            break
        path = cand["path"]
        try:
            doc = vault.read_note(path)
        except Exception:  # noqa: BLE001 —— 单篇读取失败跳过，不阻断组装
            continue
        body = str(doc.get("content") or "")
        title = str(doc.get("title") or path)
        freshness_info = cand.get("freshness")
        snippet_limit = min(_SNIPPET_LINE_LIMIT, budget - used)
        snippet = _best_snippet(body, terms, snippet_limit)
        if not snippet:
            continue
        state_note = ""
        if freshness_info:
            state_note = f"\n[新鲜度: {freshness_info.get('state')}"
            target = freshness_info.get("superseded_by")
            if target:
                state_note += f"；替代: {target}"
            state_note += "]"
        block = (
            f"\n\n## {title} ({path})\n{snippet}{state_note}\n[来源: {path}]"
        )
        room = budget - used
        if len(block) > room:
            block = block[:room]
        parts.append(block)
        used += len(block)
        source = {
            "path": path,
            "score": cand["score"],
            "reason": cand["reason"],
        }
        if freshness_info:
            source["freshness"] = freshness_info
        if cand.get("replacement_for"):
            source["replacement_for"] = cand["replacement_for"]
        sources.append(source)

    text = "".join(parts)[:budget]
    result: Dict[str, Any] = {"text": text, "sources": sources}
    if freshness:
        result["freshness"] = freshness  # 可选；旧调用方忽略新增字段
    return result
