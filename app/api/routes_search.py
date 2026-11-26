"""搜索路由：FTS5 BM25 + path:/tag:/file: 操作符；hybrid = FTS5 + 向量 + 链接图 RRF 融合。

hybrid 降级语义（P1-23）：向量后端不可用（未启用 / 缺 chromadb / 初始化失败）
或无检索词时，结果与纯 FTS 完全一致且仍返回 200，data.vector 标注后端状态。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

from app.api import deps
from app.freshness import (
    downweight_enabled,
    parse_as_of_param,
    rerank,
)
from app.index.database import IndexDatabase, cjk_space
from app.index.vector import build_vector_index
from app.paths import normalize_rel

logger = logging.getLogger("kwey.search")

router = APIRouter(prefix="/api", tags=["search"])

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
# ASCII 词按「非字母数字」切分，与 unicode61 分词规则对齐：连字符 / 下划线 / 点号
# 在索引侧都是分隔符，查询侧若把它们删掉粘合，就会得到永不命中的 token
# （PET-KWEY-SMOKE 被索引成 pet/kwey/smoke，粘合成 PETKWEYSMOKE* 则 0 结果）。
_ASCII_SPLIT_RE = re.compile(r"[\W_]+")
# FTS5 保留关键字：作 bareword 会引发语法错误，需转引号短语（B1 修复）
FTS_RESERVED = {"and", "or", "not", "near"}
# CJK/普通词的合法字符（含空格，供按字切分）
_CJK_TERM_CLEAN_RE = re.compile(r"[^\w\s\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+")

# 链接图扩展：取 FTS+向量融合后的前 N 名作种子，展开其出链/反链邻居
GRAPH_SEEDS = 10
DEFAULT_RRF_K = 60
DEFAULT_VECTOR_WEIGHT = 1.0
DEFAULT_GRAPH_WEIGHT = 0.5


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def build_match_terms(terms: List[str]) -> List[str]:
    """用户检索词 → FTS5 MATCH 片段（B1 修复：消毒后保证任意输入不产生语法错误）。

    - 含 CJK：剥离非法字符 → 按字切分为引号短语（引号内为纯 token，杜绝注入）。
    - ASCII：按非字母数字切分为多个词（与 unicode61 分词对齐，连字符/下划线不粘合）；
      保留字（AND/OR/NOT/NEAR）转引号短语；其余词用 `term*` 前缀匹配。
    - 清洗后为空的词直接丢弃。
    """
    match_terms: List[str] = []
    for term in terms:
        term = (term or "").strip()
        if not term:
            continue
        if _has_cjk(term):
            clean = _CJK_TERM_CLEAN_RE.sub(" ", term)
            phrase = cjk_space(clean).strip()
            if phrase:
                match_terms.append(f'"{phrase}"')
        else:
            words = [w for w in _ASCII_SPLIT_RE.split(term) if w]
            for word in words:
                if word.lower() in FTS_RESERVED:
                    match_terms.append(f'"{word}"')  # 引号短语 → 不再是关键字
                else:
                    match_terms.append(f"{word}*")   # 安全 bareword 前缀匹配
    return match_terms


def parse_query(q: str) -> Dict[str, Optional[object]]:
    """提取 path:/tag:/file: 操作符与剩余检索词 → 结构化过滤。"""
    ops: Dict[str, Optional[str]] = {"path": None, "tag": None, "file": None}
    terms: List[str] = []
    for token in (q or "").split():
        if token.startswith("path:") and len(token) > 5:
            ops["path"] = normalize_rel(token[5:])
        elif token.startswith("tag:") and len(token) > 4:
            ops["tag"] = token[4:].strip().lstrip("#").strip("/")
        elif token.startswith("file:") and len(token) > 5:
            ops["file"] = token[5:]
        else:
            terms.append(token)
    return {"path": ops["path"], "tag": ops["tag"], "file": ops["file"], "terms": terms}


def _fts_search(
    db: IndexDatabase, parsed: Dict, match_terms: List[str], limit: int
) -> Tuple[List[Dict], int]:
    try:
        return db.search(
            terms=match_terms,
            path_prefix=parsed["path"],
            tag=parsed["tag"],
            file_pattern=parsed["file"],
            limit=limit,
        )
    except Exception as exc:
        # 兜底：任何漏网的 FTS5 语法错误不应 500（B1 双保险）
        raise HTTPException(status_code=400, detail=f"查询语法不受支持: {exc}") from exc


def _attachment_search(
    db: IndexDatabase, parsed: Dict, match_terms: List[str], limit: int
) -> Tuple[List[Dict], int]:
    """附件全文检索（独立索引）：无检索词或 tag: 过滤时不参与。"""
    if not match_terms or parsed.get("tag"):
        return [], 0
    try:
        return db.search_attachments(
            terms=match_terms,
            path_prefix=parsed["path"],
            file_pattern=parsed["file"],
            limit=limit,
        )
    except Exception:  # noqa: BLE001 —— 附件索引异常不拖垮笔记检索
        logger.exception("附件检索失败（已跳过）")
        return [], 0


def _num(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _passes_filters(db: IndexDatabase, path: str, parsed: Dict) -> bool:
    """向量/图候选套用与 FTS 相同的 path:/tag:/file: 过滤（含笔记存在性校验）。"""
    pp = parsed.get("path")
    if pp and not path.startswith(str(pp).rstrip("/") + "/"):
        return False
    note = db.get_note(path)
    if note is None:  # 向量库残留的已删除笔记
        return False
    tag = parsed.get("tag")
    if tag and tag not in db.tags_of_note(path):
        return False
    fp = parsed.get("file")
    if fp:
        fpl = str(fp).lower()
        if fpl not in note["title"].lower() and fpl not in path.lower():
            return False
    return True


def _rrf_fuse(
    ranked: Dict[str, Tuple[Dict[str, int], float]], k: float
) -> Dict[str, float]:
    """Reciprocal Rank Fusion：score = Σ weight / (k + rank)，rank 从 1 起。"""
    scores: Dict[str, float] = {}
    for ranks, weight in ranked.values():
        if weight <= 0:
            continue
        for path, rank in ranks.items():
            scores[path] = scores.get(path, 0.0) + weight / (k + rank)
    return scores


def _hybrid_search(
    db: IndexDatabase, parsed: Dict, match_terms: List[str], limit: int
) -> Tuple[List[Dict], int, Dict]:
    indexer = deps.get_indexer()
    vector = getattr(indexer, "vector", None)
    info = {
        "available": bool(vector is not None and vector.available),
        "backend": getattr(vector, "name", "null"),
    }
    # 降级：向量不可用或无检索词（纯操作符查询）→ 与纯 FTS 完全一致
    if vector is None or not vector.available or not match_terms:
        hits, total = _fts_search(db, parsed, match_terms, limit)
        return hits, total, info

    vcfg = {}
    cfg = deps.get_config()
    if cfg is not None:
        vcfg = cfg.get("vector") or {}
    weight_vector = _num(vcfg.get("weight", DEFAULT_VECTOR_WEIGHT), DEFAULT_VECTOR_WEIGHT)
    weight_graph = _num(vcfg.get("graph_weight", DEFAULT_GRAPH_WEIGHT), DEFAULT_GRAPH_WEIGHT)
    rrf_k = _num(vcfg.get("rrf_k", DEFAULT_RRF_K), DEFAULT_RRF_K)
    cand = min(max(limit * 3, 60), 200)

    fts_hits, _fts_total = _fts_search(db, parsed, match_terms, cand)
    fts_rank = {h["path"]: i + 1 for i, h in enumerate(fts_hits)}

    try:
        vec_pairs = vector.query(" ".join(parsed["terms"]), cand)
    except Exception:  # noqa: BLE001 —— 运行期向量失败本轮降级，不 500
        logger.exception("向量检索失败，本轮降级为 FTS+链接图")
        vec_pairs = []
    vec_rank: Dict[str, int] = {}
    for path, _score in vec_pairs:
        if path in vec_rank or not _passes_filters(db, path, parsed):
            continue
        vec_rank[path] = len(vec_rank) + 1

    graph_rank: Dict[str, int] = {}
    graph = deps.get_linkgraph()
    if graph is not None and weight_graph > 0:
        base = _rrf_fuse(
            {"fts": (fts_rank, 1.0), "vector": (vec_rank, weight_vector)}, rrf_k
        )
        seeds = sorted(base, key=lambda p: (-base[p], p))[:GRAPH_SEEDS]
        for seed in seeds:
            for nb in sorted(graph.out_links_of(seed) | graph.backlinks(seed)):
                if nb in graph_rank or not _passes_filters(db, nb, parsed):
                    continue
                graph_rank[nb] = len(graph_rank) + 1
                if len(graph_rank) >= cand:
                    break
            if len(graph_rank) >= cand:
                break

    lists = {
        "fts": (fts_rank, 1.0),
        "vector": (vec_rank, weight_vector),
        "graph": (graph_rank, weight_graph),
    }
    scores = _rrf_fuse(lists, rrf_k)
    order = sorted(scores, key=lambda p: (-scores[p], p))[:limit]

    fts_by_path = {h["path"]: h for h in fts_hits}
    hits: List[Dict] = []
    for path in order:
        base_hit = fts_by_path.get(path)
        if base_hit is not None:
            hit = dict(base_hit)
        else:
            note = db.get_note(path) or {"title": path}
            hit = {
                "path": path,
                "title": note["title"],
                "snippet": "",
                "tags": db.tags_of_note(path),
            }
        hit["score"] = round(scores[path], 6)
        hit["sources"] = [s for s, (ranks, w) in lists.items() if w > 0 and path in ranks]
        hits.append(hit)
    return hits, len(scores), info


@router.get("/search")
async def search(
    q: str = Query(default="", description="支持 path:/tag:/file: 操作符"),
    limit: int = Query(default=50, ge=1, le=200),
    mode: str = Query(default="fts", description="fts（默认）；hybrid = FTS5+向量+链接图，向量不可用时自动降级"),
    as_of: Optional[str] = Query(
        default=None,
        description="按该时点评估有效期（ISO 日期/时间）；缺省为当前。历史期间查询不会被今天的状态压低",
    ),
):
    if mode not in ("fts", "hybrid"):
        raise HTTPException(status_code=400, detail="mode 仅支持 fts / hybrid")
    db = deps.require_db()
    parsed = parse_query(q)
    match_terms = build_match_terms(parsed["terms"])  # type: ignore[arg-type]
    try:
        as_of_dt = parse_as_of_param(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # P2-D：默认当前事实查询降权；关闭降权后仍按 as_of 返回状态元数据。
    downweight = downweight_enabled(deps.get_config())
    need_freshness = downweight or as_of_dt is not None
    cand_limit = min(max(limit * 3, 60), 200) if need_freshness else limit

    started = time.perf_counter()
    vector_info: Optional[Dict] = None
    if mode == "hybrid":
        hits, total, vector_info = _hybrid_search(db, parsed, match_terms, cand_limit)
    else:
        hits, total = _fts_search(db, parsed, match_terms, cand_limit)
    freshness_info: Optional[Dict] = None
    if need_freshness and hits:
        # 候选融合之后、最终截断之前重排；替代证据可能在截断前补入。
        hits, freshness_info = rerank(
            hits,
            db,
            as_of=as_of_dt,
            downweight=downweight,
            historical=as_of_dt is not None,
            make_entry=lambda path, score: {
                "path": path,
                "title": (db.get_note(path) or {}).get("title") or path,
                "score": score,
                "snippet": "",
                "tags": db.tags_of_note(path),
            },
        )
        hits = hits[:limit]
    for hit in hits:
        hit["kind"] = "note"
    # 附件命中排在笔记之后（独立索引，不参与标签/图/向量融合）
    att_hits, att_total = _attachment_search(db, parsed, match_terms, limit)
    if att_hits:
        total += att_total
        hits.extend(att_hits[: max(0, limit - len(hits))])
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    data: Dict = {
        "hits": hits,
        "total": total,
        "elapsed_ms": elapsed_ms,
        "mode": mode,
    }
    if vector_info is not None:
        data["vector"] = vector_info
    if freshness_info is not None:
        data["freshness"] = freshness_info
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/search/vector/status")
async def vector_status():
    """A2：向量后端状态（前端设置页展示，不触发重建）。"""
    indexer = deps.get_indexer()
    vector = getattr(indexer, "vector", None)
    cfg = deps.get_config()
    vcfg = (cfg.get("vector") or {}) if cfg is not None else {}
    return {
        "code": 0,
        "data": {
            "enabled": bool(vcfg.get("enabled", False)),
            "available": bool(vector is not None and vector.available),
            "backend": getattr(vector, "name", "null"),
            "model": str(vcfg.get("model") or ""),
            "count": int(vector.count()) if vector is not None else 0,
        },
        "message": "ok",
    }


@router.post("/search/vector/rebuild")
async def vector_rebuild():
    """A2：按当前 settings.vector 重建向量后端并全量灌入（索引可重建语义）。

    缺 chromadb / 未启用时 build_vector_index 返回 NullBackend → count=0、hybrid 降级 FTS，
    不报错。重建过程只读磁盘笔记，不改任何用户文件。
    """
    indexer = deps.require_indexer()
    cfg = deps.require_config()
    vcfg = cfg.get("vector") or {}
    root = indexer.paths.root
    new_backend = build_vector_index(root, vcfg)
    indexer.vector = new_backend
    count = 0
    if new_backend.available:
        try:
            new_backend.reset()
            for note in indexer.db.all_notes():
                path = note["path"]
                abs_path = root / path.replace("/", "\\")
                try:
                    body = abs_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # 读取失败跳过，不阻断整体重建
                title = str(note.get("title") or path)
                new_backend.upsert(path, f"{title}\n{body}")
                count += 1
        except Exception:  # noqa: BLE001 —— 向量是增强能力，重建失败不 500
            logger.exception("向量索引重建失败，hybrid 将降级为 FTS")
    return {
        "code": 0,
        "data": {
            "enabled": bool(vcfg.get("enabled", False)),
            "available": bool(new_backend.available),
            "backend": new_backend.name,
            "count": int(new_backend.count()),
        },
        "message": "ok",
    }
