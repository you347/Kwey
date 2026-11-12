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
# ASCII 词只保留字母/数字/下划线（B1 修复：彻底剥离 *、引号、括号等一切 FTS5 语法字符）
_ASCII_TERM_RE = re.compile(r"[^\w]+")
# FTS5 保留关键字：作 bareword 会引发语法错误，需转引号短语（B1 修复）
FTS_RESERVED = {"and", "or", "not", "near"}
# CJK/普通词的合法字符（含空格，供按字切分）
_CJK_TERM_CLEAN_RE = re.compile(r"[^\w\s\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def build_match_terms(terms: List[str]) -> List[str]:
    """用户检索词 → FTS5 MATCH 片段（B1 修复：消毒后保证任意输入不产生语法错误）。

    - 含 CJK：剥离非法字符 → 按字切分为引号短语（引号内为纯 token，杜绝注入）。
    - ASCII：只保留 \\w 字符；保留字（AND/OR/NOT/NEAR）转引号短语；
      其余安全词用 `term*` 前缀匹配。
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
            word = _ASCII_TERM_RE.sub("", term)
            if not word:
                continue
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
    try:
        hits, total = db.search(
            terms=match_terms,
            path_prefix=parsed["path"],  # type: ignore[arg-type]
            tag=parsed["tag"],  # type: ignore[arg-type]
            file_pattern=parsed["file"],  # type: ignore[arg-type]
            limit=limit,
        )
    except Exception as exc:
        # 兜底：任何漏网的 FTS5 语法错误不应 500（B1 双保险）
        raise HTTPException(status_code=400, detail=f"查询语法不受支持: {exc}") from exc
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
