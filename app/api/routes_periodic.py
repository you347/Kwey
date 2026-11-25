"""周期笔记路由（C2 / P2）：ISO 周笔记（周一起）与月度笔记。

约定：
- 笔记存放 ``periodic/`` 目录（vault 相对；不在 IGNORED_DIRS，可走 VaultService 统一写路径）：
  周笔记 ``periodic/YYYY-Www.md``（ISO 周名，YYYY 为 ISO 年份，跨年归属正确），
  月笔记 ``periodic/YYYY-MM.md``（自然月）。
- 模板目录取 settings.periodic_dir（默认 templates，与模板/每日笔记共用目录约定），
  模板文件 ``week.md`` / ``month.md``；缺失时用内置骨架。
- 模板目录可能在 IGNORED_DIRS 内（validate_relative 拒绝该片段），故模板一律 root/rel
  直读并逐段安全校验（与 routes_templates 同一套做法）。
- GET /week、GET /month 均为「确保存在」语义：不存在则经 VaultService.create_note 创建
  （幂等；并发下 409 视为已存在），返回 {path, exists, template, prev_path, next_path, notes}。
- prev_path/next_path 指向相邻周期笔记（只算路径，不预创建）。
- notes：本周期内 created/updated 任一命中的笔记（db.all_notes() 过滤，按最新命中时间倒序，
  最多 50 条，不含周期笔记自身；frontmatter 无 created/updated 的笔记不计入）。

占位符（正文与 frontmatter 字符串值均替换）：
- {{date}}  周期起点日期（周笔记=周一；月笔记=当月 1 日），YYYY-MM-DD
- {{week}}  ISO 周号（不带前导零的自然数，如 46）
- {{month}} 两位月份（如 11）
- {{year}}  周笔记=ISO 年份；月笔记=自然年
- {{prev}}/{{next}} 相邻周期笔记路径（**不含 .md**，便于直接写 [[{{prev}}]] 双链；
  API 响应里的 prev_path/next_path 则含 .md，与 path 保持一致）
"""

from __future__ import annotations

import re
from datetime import date as dt_date, datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.parser.frontmatter import parse_frontmatter
from app.paths import ILLEGAL_FILENAME_CHARS
from app.vault.provenance import LOCAL_UI_ACTOR

router = APIRouter(prefix="/api/periodic", tags=["periodic"])

NOTE_DIR = "periodic"            # 周期笔记存放目录（vault 相对，固定）
NOTE_EXTS = (".md", ".markdown")
NOTES_LIMIT = 50                 # 周期内笔记汇总上限

# 内置骨架（模板文件缺失时兜底），占位符与自定义模板完全一致
_DEFAULT_TEMPLATES: Dict[str, str] = {
    "week": "# {{year}} 年第 {{week}} 周\n\n上周：[[{{prev}}]] · 下周：[[{{next}}]]\n\n## 本周笔记\n\n",
    "month": "# {{year}} 年 {{month}} 月\n\n上月：[[{{prev}}]] · 下月：[[{{next}}]]\n\n## 本月笔记\n\n",
}

KINDS = ("week", "month")


# --------------------------------------------------------------------- helpers
def _check_rel(rel: str) -> None:
    """逐段安全校验（不复用 validate_relative：模板目录可能在 IGNORED_DIRS 内）。"""
    if not rel:
        raise HTTPException(status_code=400, detail="路径为空")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise HTTPException(status_code=400, detail=f"非法路径片段: {part!r}")
        if part in (".kwey", ".trash", ".git"):
            raise HTTPException(status_code=400, detail=f"系统保留目录不可读写: {part!r}")
        if ":" in part or any(c in ILLEGAL_FILENAME_CHARS for c in part):
            raise HTTPException(status_code=400, detail=f"路径含非法字符: {part!r}")
        if re.search(r"[\x00-\x1f]", part):
            raise HTTPException(status_code=400, detail="路径含控制字符")


def _template_dir() -> str:
    """模板目录：settings.periodic_dir（缺省 templates，与 daily/模板共用目录约定）。"""
    config = deps.require_config()
    rel = str(config.get("periodic_dir") or "templates").strip() or "templates"
    rel = rel.replace("\\", "/").strip("/")
    _check_rel(rel)
    return rel


def _load_template(kind: str) -> Tuple[Dict[str, Any], str, Optional[str]]:
    """读模板 → (frontmatter, 正文, 模板名|None)。文件缺失返回内置骨架。"""
    name = f"{kind}.md"
    base = _template_dir()
    rel = f"{base}/{name}"
    _check_rel(rel)
    root = deps.require_vault().paths.ensure_root()
    abs_path = (root / rel).resolve()
    base_abs = (root / base).resolve()
    try:
        abs_path.relative_to(base_abs)  # 防越界读任意文件
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="路径越出模板目录") from exc
    if not abs_path.is_file():
        return {}, _DEFAULT_TEMPLATES[kind], None
    raw = abs_path.read_text(encoding="utf-8-sig")
    frontmatter, body = parse_frontmatter(raw)
    return frontmatter, body, name


def _replace(text: str, mapping: Dict[str, str]) -> str:
    """按占位符映射替换字符串（一次性遍历，避免前一个替换值再被后续命中）。"""
    result = text
    for key, value in mapping.items():
        result = result.replace(key, value)
    return result


def _replace_in_obj(value: Any, mapping: Dict[str, str]) -> Any:
    """递归替换 frontmatter 内字符串值中的占位符（与 routes_templates 同策略）。"""
    if isinstance(value, str):
        return _replace(value, mapping)
    if isinstance(value, list):
        return [_replace_in_obj(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace_in_obj(v, mapping) for k, v in value.items()}
    return value


def _parse_date(date: str) -> dt_date:
    """date 参数（YYYY-MM-DD）→ date；缺省今天；非法 400。"""
    raw = (date or "").strip()
    if not raw:
        return datetime.now().date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"date 需为 YYYY-MM-DD 格式: {raw!r}") from exc


def _parse_dt(value: str) -> Optional[datetime]:
    """frontmatter created/updated → aware datetime；naive 视为本地时区；非法返回 None。"""
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive 视为本地时区，保证可比较
    return parsed


# --------------------------------------------------------------------- period math
def _week_paths(d: dt_date) -> Dict[str, Any]:
    """ISO 周（周一起）：本/前/后周期笔记路径 + 占位符映射 + 汇总区间起点/终点（date）。"""
    iso_year, iso_week, _ = d.isocalendar()
    monday = d - timedelta(days=d.isoweekday() - 1)
    prev_monday = monday - timedelta(days=7)
    next_monday = monday + timedelta(days=7)
    py, pw, _ = prev_monday.isocalendar()
    ny, nw, _ = next_monday.isocalendar()
    return {
        "rel": f"{NOTE_DIR}/{iso_year}-W{iso_week:02d}.md",
        "prev_path": f"{NOTE_DIR}/{py}-W{pw:02d}.md",
        "next_path": f"{NOTE_DIR}/{ny}-W{nw:02d}.md",
        "start": monday,
        "end": next_monday,
        "mapping": {
            "{{date}}": monday.strftime("%Y-%m-%d"),
            "{{week}}": str(iso_week),
            "{{month}}": monday.strftime("%m"),
            "{{year}}": str(iso_year),
            "{{prev}}": f"{NOTE_DIR}/{py}-W{pw:02d}",
            "{{next}}": f"{NOTE_DIR}/{ny}-W{nw:02d}",
        },
    }


def _month_paths(d: dt_date) -> Dict[str, Any]:
    """自然月：本/前/后周期笔记路径 + 占位符映射 + 汇总区间起点/终点（date）。"""
    first = d.replace(day=1)
    if d.month == 12:
        next_first = d.replace(year=d.year + 1, month=1, day=1)
    else:
        next_first = d.replace(month=d.month + 1, day=1)
    prev_first = first - timedelta(days=1)  # 上月最后一天 → 取其年月
    return {
        "rel": f"{NOTE_DIR}/{d.year}-{d.month:02d}.md",
        "prev_path": f"{NOTE_DIR}/{prev_first.year}-{prev_first.month:02d}.md",
        "next_path": f"{NOTE_DIR}/{next_first.year}-{next_first.month:02d}.md",
        "start": first,
        "end": next_first,
        "mapping": {
            "{{date}}": first.strftime("%Y-%m-%d"),
            "{{week}}": str(first.isocalendar()[1]),
            "{{month}}": f"{d.month:02d}",
            "{{year}}": str(d.year),
            "{{prev}}": f"{NOTE_DIR}/{prev_first.year}-{prev_first.month:02d}",
            "{{next}}": f"{NOTE_DIR}/{next_first.year}-{next_first.month:02d}",
        },
    }


def _collect_notes(start: dt_date, end: dt_date, exclude: str) -> List[Dict[str, str]]:
    """本周期内 created/updated 任一命中的笔记（含端点 start、不含 end），最新在前，最多 50 条。

    周期笔记自身排除；frontmatter 无 created/updated 的笔记不计入（契约明确按字段过滤）。
    """
    start_dt = datetime.combine(start, dt_time.min).astimezone()  # naive → 本地时区 aware
    end_dt = datetime.combine(end, dt_time.min).astimezone()
    db = deps.require_db()
    hits: List[Tuple[datetime, str, str]] = []
    for note in db.all_notes():
        rel = str(note.get("path") or "")
        if not rel or rel == exclude:
            continue
        latest: Optional[datetime] = None
        for key in ("updated", "created"):
            raw = note.get(key)
            parsed = _parse_dt(raw) if raw else None
            if parsed is not None and start_dt <= parsed < end_dt:
                if latest is None or parsed > latest:
                    latest = parsed
        if latest is None:
            continue
        title = str(note.get("title") or rel)
        hits.append((latest, rel, title))
    hits.sort(key=lambda x: (-x[0].timestamp(), x[1]))
    return [{"path": rel, "title": title} for _ts, rel, title in hits[:NOTES_LIMIT]]


# --------------------------------------------------------------------- core
def _ensure_note(kind: str, d: dt_date) -> Dict[str, Any]:
    """确保周期笔记存在（幂等创建）→ 统一响应 data。"""
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"周期类型仅支持 week/month: {kind!r}")
    vault = deps.require_vault()
    info = _week_paths(d) if kind == "week" else _month_paths(d)
    rel = info["rel"]
    notes = _collect_notes(info["start"], info["end"], exclude=rel)  # 先汇总（创建前，不含自身）

    abs_path = vault.paths.to_abs(rel)  # periodic/ 不在保留目录，validate_relative 放行
    exists = abs_path.is_file()
    template_name: Optional[str] = None
    if not exists:
        frontmatter, body, template_name = _load_template(kind)
        content = _replace(body, info["mapping"])
        frontmatter = _replace_in_obj(frontmatter, info["mapping"])
        try:
            vault.create_note(
                rel, content=content, frontmatter=frontmatter, actor=LOCAL_UI_ACTOR
            )
        except HTTPException as exc:
            if exc.status_code != 409:  # 并发下已被创建：幂等返回
                raise
            exists = True

    return {
        "path": rel,
        "exists": exists,
        "template": template_name,       # 实际使用的模板文件名；None=内置骨架
        "prev_path": info["prev_path"],  # 相邻周期笔记路径（不预创建）
        "next_path": info["next_path"],
        "notes": notes,
    }


@router.get("/week")
async def week_note(date: str = ""):
    """本周（或 date 所在 ISO 周）笔记：不存在则创建并套模板，返回路径与周期内笔记汇总。"""
    return {"code": 0, "data": _ensure_note("week", _parse_date(date)), "message": "ok"}


@router.get("/month")
async def month_note(date: str = ""):
    """本月（或 date 所在自然月）笔记：不存在则创建并套模板，返回路径与周期内笔记汇总。"""
    return {"code": 0, "data": _ensure_note("month", _parse_date(date)), "message": "ok"}
