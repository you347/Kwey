"""搜索路由：FTS5 BM25 + path:/tag:/file: 操作符（mode=hybrid 参数位 M3 预留）。"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.api import deps
from app.index.database import cjk_space
from app.paths import normalize_rel

router = APIRouter(prefix="/api", tags=["search"])

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_ASCII_TERM_RE = re.compile(r"[^\w*]+")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def build_match_terms(terms: List[str]) -> List[str]:
    """用户检索词 → FTS5 MATCH 片段。

    - 含 CJK：按字切分为短语（引号包裹保证相邻，风险点④）。
    - ASCII：清洗后前缀匹配（term*）。
    """
    match_terms: List[str] = []
    for term in terms:
        term = term.strip()
        if not term:
            continue
        if _has_cjk(term):
            phrase = cjk_space(term).strip()
            match_terms.append(f'"{phrase}"')
        else:
            clean = _ASCII_TERM_RE.sub("", term).strip("*")
            if clean:
                match_terms.append(f"{clean}*")
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


@router.get("/search")
async def search(
    q: str = Query(default="", description="支持 path:/tag:/file: 操作符"),
    limit: int = Query(default=50, ge=1, le=200),
    mode: str = Query(default="fts", description="fts（默认）；hybrid 为 M3 预留参数位"),
):
    if mode not in ("fts", "hybrid"):
        raise HTTPException(status_code=400, detail="mode 仅支持 fts / hybrid（hybrid M3 提供）")
    db = deps.require_db()
    parsed = parse_query(q)
    match_terms = build_match_terms(parsed["terms"])  # type: ignore[arg-type]

    started = time.perf_counter()
    hits, total = db.search(
        terms=match_terms,
        path_prefix=parsed["path"],  # type: ignore[arg-type]
        tag=parsed["tag"],  # type: ignore[arg-type]
        file_pattern=parsed["file"],  # type: ignore[arg-type]
        limit=limit,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "code": 0,
        "data": {
            "hits": hits,
            "total": total,
            "elapsed_ms": elapsed_ms,
            "mode": "hybrid" if mode == "hybrid" else "fts",
        },
        "message": "ok",
    }
