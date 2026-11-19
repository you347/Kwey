"""知识脉冲首页：GET /api/pulse 聚合今日字数/连续天数/孤儿/断链/Agent 活动。

A4 权限治理：需 **read** 级 token（guard.require_token）。
"""
from datetime import date, datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Request

from app.agent import journal
from app.api import deps
from app.api.guard import require_token
from app.index.words import word_count

router = APIRouter(prefix="/api", tags=["pulse"])


@router.get("/pulse")
async def pulse(request: Request) -> Dict[str, Any]:
    require_token(request, "read")
    vault = deps.require_vault()
    db = deps.require_db()
    linkgraph = deps.require_linkgraph()
    notes = db.all_notes()
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time()).timestamp()

    today_notes = sorted(
        (n for n in notes if n["mtime"] >= today_start),
        key=lambda n: n["mtime"],
        reverse=True,
    )
    today_words = 0
    for n in today_notes:
        try:
            today_words += word_count(vault.read_note(n["path"])["content"])
        except Exception:
            continue  # 索引残留的已删笔记：跳过不计

    days = {datetime.fromtimestamp(n["mtime"]).date() for n in notes}
    streak = 0
    cursor = today if today in days else today - timedelta(days=1)
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)

    orphan_count = sum(
        1
        for n in notes
        if not linkgraph.out_links_of(n["path"]) and not linkgraph.backlinks(n["path"])
    )
    broken_count = len(
        {(r["source_path"], r["target"]) for r in db.unresolved_links()}
    )

    return {
        "code": 0,
        "data": {
            "today_words": today_words,
            "streak_days": streak,
            "orphan_count": orphan_count,
            "broken_count": broken_count,
            "note_count": len(notes),
            "today_notes": [n["path"] for n in today_notes[:5]],
            "agent_recent": journal.recent(vault.paths.ensure_root(), 5),
        },
        "message": "ok",
    }