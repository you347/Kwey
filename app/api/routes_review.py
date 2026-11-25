"""F4 学习复习流水线：frontmatter 驱动的间隔复习（review_due / review_interval）。

数据事实源仍是磁盘 Markdown —— 复习状态不落独立数据库，全部写在笔记
frontmatter 里，索引删掉重建也不丢：

    ---
    review_due: 2026-11-20      # 下次复习日期（ISO 日期，纯日期不带时间）
    review_interval: 4          # 当前间隔（天），标记后按结果递增
    review_last: 2026-11-16     # 最近一次复习日期（供「今日已完成」统计）
    ---

接口（prefix=/api/review）：
- ``GET  /due``              列 review_due <= 今天 的笔记（逾期最久排最前）
- ``POST /mark``             {path, result: again|good|easy} 标记并推进间隔
- ``GET  /stats``            待复习数 / 今日已完成 / 已排期总数
- ``POST /schedule``         （扩展）把笔记纳入复习或改期，供面板「加入复习」按钮
- ``DELETE /schedule``       （扩展）移出复习（删除三个 review_* 字段）

间隔规则：again → 1 天；good → 当前间隔 ×2；easy → 当前间隔 ×3。
所有写操作经 ``VaultService.write_note`` 统一写路径（原子写 + 增量索引 + 事件广播），
正文与其它 frontmatter 字段原样保留。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import deps
from app.vault.provenance import LOCAL_UI_ACTOR
from app.vault.service import VaultService

router = APIRouter(prefix="/api/review", tags=["review"])

# 首次纳入复习时的默认间隔（天）与上限（避免 ×2/×3 无限膨胀）
DEFAULT_INTERVAL = 3
MAX_INTERVAL = 3650
MAX_DUE_LIMIT = 500

RESULT_AGAIN = "again"
RESULT_GOOD = "good"
RESULT_EASY = "easy"
VALID_RESULTS = (RESULT_AGAIN, RESULT_GOOD, RESULT_EASY)

# 扫描缓存：{(vault_root, path): (db_mtime, item|None)}。
# 复习面板每次刷新都要遍历全库 frontmatter，缓存命中时零磁盘读；
# 键含 vault 根，避免多 vault / 测试临时目录互相污染。
_SCAN_CACHE: Dict[Tuple[str, str], Tuple[float, Optional[Dict[str, Any]]]] = {}


# ------------------------------------------------------------------ 请求模型
class MarkRequest(BaseModel):
    """标记一条复习结果。"""

    path: str = Field(min_length=1, max_length=1024)
    result: str = Field(min_length=1, max_length=16)


class ScheduleRequest(BaseModel):
    """纳入复习 / 改期。due 优先于 interval。"""

    path: str = Field(min_length=1, max_length=1024)
    interval: Optional[int] = Field(default=None, ge=1, le=MAX_INTERVAL)
    due: Optional[str] = Field(default=None, max_length=32)


# ------------------------------------------------------------------ 日期工具
def _today() -> date:
    return date.today()


def _coerce_date(value: Any) -> Optional[date]:
    """frontmatter 值 → date。支持 date/datetime（YAML 原生解析）与字符串。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 兼容 2026-11-20 / 2026-11-20T09:00:00 / 2026-11-20 09:00
        head = text[:10]
        try:
            return date.fromisoformat(head)
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError:
                return None
    return None


def _coerce_interval(value: Any) -> int:
    """frontmatter 值 → 正整数间隔；非法值回退默认间隔。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL
    if number < 1:
        return DEFAULT_INTERVAL
    return min(number, MAX_INTERVAL)


# ------------------------------------------------------------------ 扫描
def _read_review_item(vault: VaultService, path: str) -> Optional[Dict[str, Any]]:
    """读单篇笔记的复习字段；无 review_due 或读取失败返回 None。"""
    try:
        note = vault.read_note(path)
    except HTTPException:
        return None  # 索引残留的已删笔记：跳过，不让整个面板 500
    frontmatter = note.get("frontmatter") or {}
    due = _coerce_date(frontmatter.get("review_due"))
    if due is None:
        return None
    last = _coerce_date(frontmatter.get("review_last"))
    return {
        "path": path,
        "title": note.get("title") or path.rsplit("/", 1)[-1],
        "due": due.isoformat(),
        "interval": _coerce_interval(frontmatter.get("review_interval")),
        "last": last.isoformat() if last else None,
    }


def _scan(vault: VaultService) -> List[Dict[str, Any]]:
    """遍历索引内全部笔记，返回带 review_due 的条目（带 mtime 缓存）。"""
    root = str(vault.paths.root or "")
    seen: set[Tuple[str, str]] = set()
    items: List[Dict[str, Any]] = []

    for meta in vault.db.all_notes():
        path = str(meta.get("path") or "")
        if not path:
            continue
        key = (root, path)
        seen.add(key)
        mtime = float(meta.get("mtime") or 0.0)
        cached = _SCAN_CACHE.get(key)
        if cached is not None and abs(cached[0] - mtime) < 1e-6:
            item = cached[1]
        else:
            item = _read_review_item(vault, path)
            _SCAN_CACHE[key] = (mtime, item)
        if item:
            items.append(dict(item, title=str(meta.get("title") or item["title"])))

    # 清理已被删除的笔记，避免缓存随长期运行无限增长
    for key in [k for k in _SCAN_CACHE if k[0] == root and k not in seen]:
        _SCAN_CACHE.pop(key, None)
    return items


def _sort_key(item: Dict[str, Any]) -> Tuple[str, str]:
    return (item["due"], item["path"])


def _overdue_days(due_text: str, today: date) -> int:
    due = _coerce_date(due_text)
    if due is None:
        return 0
    return max(0, (today - due).days)


# ------------------------------------------------------------------ 接口
@router.get("/due")
async def list_due(limit: int = 200) -> Dict[str, Any]:
    """到期（review_due <= 今天）的笔记列表，逾期最久排最前。"""
    vault = deps.require_vault()
    today = _today()
    limit = max(1, min(int(limit or 200), MAX_DUE_LIMIT))

    due_items = [n for n in _scan(vault) if _coerce_date(n["due"]) <= today]
    due_items.sort(key=_sort_key)
    payload = [
        {
            "path": n["path"],
            "title": n["title"],
            "due": n["due"],
            "interval": n["interval"],
            "last": n["last"],
            "overdue_days": _overdue_days(n["due"], today),
        }
        for n in due_items[:limit]
    ]
    return {
        "code": 0,
        "data": payload,
        "message": "ok",
    }


@router.post("/mark")
async def mark(req: MarkRequest) -> Dict[str, Any]:
    """标记复习结果并推进间隔（again→1、good→×2、easy→×3）。"""
    vault = deps.require_vault()
    result = req.result.strip().lower()
    if result not in VALID_RESULTS:
        raise HTTPException(
            status_code=400,
            detail=f"result 只能是 {'/'.join(VALID_RESULTS)}，收到: {req.result!r}",
        )

    note = vault.read_note(req.path)  # 路径校验 + 不存在 → 404
    frontmatter = dict(note.get("frontmatter") or {})
    previous = _coerce_interval(frontmatter.get("review_interval"))

    if result == RESULT_AGAIN:
        interval = 1
    elif result == RESULT_GOOD:
        interval = max(1, previous) * 2
    else:
        interval = max(1, previous) * 3
    interval = max(1, min(interval, MAX_INTERVAL))

    today = _today()
    due = today + timedelta(days=interval)
    frontmatter["review_due"] = due.isoformat()
    frontmatter["review_interval"] = interval
    frontmatter["review_last"] = today.isoformat()

    # 统一写路径：正文原样保留，其余自定义字段不丢
    vault.write_note(note["path"], note["content"], frontmatter, actor=LOCAL_UI_ACTOR)
    return {
        "code": 0,
        "data": {
            "path": note["path"],
            "result": result,
            "previous_interval": previous,
            "interval": interval,
            "due": due.isoformat(),
            "last": today.isoformat(),
        },
        "message": "ok",
    }


@router.get("/stats")
async def stats() -> Dict[str, Any]:
    """复习统计：待复习 / 今日已完成 / 已排期总数。"""
    vault = deps.require_vault()
    today = _today()
    items = _scan(vault)
    due_count = sum(1 for n in items if _coerce_date(n["due"]) <= today)
    done_today = sum(1 for n in items if n["last"] == today.isoformat())
    overdue = [
        _overdue_days(n["due"], today) for n in items if _coerce_date(n["due"]) <= today
    ]
    return {
        "code": 0,
        "data": {
            "today": today.isoformat(),
            "due": due_count,
            "done_today": done_today,
            "scheduled": len(items),
            "max_overdue_days": max(overdue) if overdue else 0,
        },
        "message": "ok",
    }


@router.post("/schedule")
async def schedule(req: ScheduleRequest) -> Dict[str, Any]:
    """把笔记纳入复习（或改期）；缺省按当前间隔 / 默认 3 天。"""
    vault = deps.require_vault()
    note = vault.read_note(req.path)
    frontmatter = dict(note.get("frontmatter") or {})
    today = _today()

    interval = req.interval
    if interval is None:
        interval = _coerce_interval(frontmatter.get("review_interval"))
    interval = max(1, min(int(interval), MAX_INTERVAL))

    due: Optional[date] = None
    if req.due:
        due = _coerce_date(req.due)
        if due is None:
            raise HTTPException(status_code=400, detail=f"due 不是合法日期: {req.due!r}")
    if due is None:
        due = today + timedelta(days=interval)

    frontmatter["review_due"] = due.isoformat()
    frontmatter["review_interval"] = interval
    vault.write_note(note["path"], note["content"], frontmatter, actor=LOCAL_UI_ACTOR)
    return {
        "code": 0,
        "data": {
            "path": note["path"],
            "due": due.isoformat(),
            "interval": interval,
            "title": note.get("title") or note["path"],
        },
        "message": "ok",
    }


@router.delete("/schedule")
async def unschedule(path: str) -> Dict[str, Any]:
    """移出复习：删除 review_due / review_interval / review_last 三个字段。"""
    vault = deps.require_vault()
    note = vault.read_note(path)
    frontmatter = dict(note.get("frontmatter") or {})
    removed = [k for k in ("review_due", "review_interval", "review_last") if k in frontmatter]
    for key in removed:
        frontmatter.pop(key, None)
    if removed:
        vault.write_note(
            note["path"], note["content"], frontmatter, actor=LOCAL_UI_ACTOR
        )
    return {
        "code": 0,
        "data": {"path": note["path"], "removed": removed},
        "message": "ok",
    }


def clear_cache() -> None:
    """清空扫描缓存（测试 / vault 切换时可调用）。"""
    _SCAN_CACHE.clear()
