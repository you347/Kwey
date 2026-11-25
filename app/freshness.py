"""知识保鲜与失效语义（INNOVATION-PLAN §7 / P2-D）——纯规则，不含路由与写盘。

设计边界
--------
- 只解释用户**明确声明**的生命周期字段，不猜事实真伪、不联网核验、不判历史记录对错。
- 有效区间统一 ``[valid_from, valid_until)``；缺失端点视为无界。
- 日期（无时间）按调用时区规范到**当日本地日结束后**的排他边界（次日 00:00）；
  ``valid_from`` 的日期则取当日本地 00:00。
- 状态：``current_declared`` / ``not_yet_effective_declared`` / ``expired_declared`` /
  ``superseded_declared`` / ``unknown`` / ``invalid_metadata``。
- ``unknown`` 表示没有声明，不等同于 expired；``invalid_metadata`` 明确报错，
  不悄悄降级为正常。
- 替代关系只做单跳生效判断；为检测自指/环会沿链行走但最多 ``MAX_SUPERSEDE_HOPS`` 跳，
  不无限追链，也不据此自动删除/改写旧事实。

本模块不导入 FastAPI / deps，便于单测与在 CLI/MCP/RAG 间复用。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.paths import IGNORED_DIRS, normalize_rel

# ---------------------------------------------------------------- 字段 / 状态常量
FIELD_VALID_FROM = "valid_from"
FIELD_VALID_UNTIL = "valid_until"
FIELD_SUPERSEDED_BY = "superseded_by"
FIELD_FACT_SCOPE = "fact_scope"
FIELD_VERIFIED_AT = "verified_at"

LIFECYCLE_FIELDS: Tuple[str, ...] = (
    FIELD_VALID_FROM,
    FIELD_VALID_UNTIL,
    FIELD_SUPERSEDED_BY,
    FIELD_FACT_SCOPE,
    FIELD_VERIFIED_AT,
)

STATE_CURRENT = "current_declared"
STATE_NOT_YET = "not_yet_effective_declared"
STATE_EXPIRED = "expired_declared"
STATE_SUPERSEDED = "superseded_declared"
STATE_UNKNOWN = "unknown"
STATE_INVALID = "invalid_metadata"

DECLARED_STATES = (
    STATE_CURRENT,
    STATE_NOT_YET,
    STATE_EXPIRED,
    STATE_SUPERSEDED,
)
RISK_STATES = (STATE_NOT_YET, STATE_EXPIRED, STATE_SUPERSEDED, STATE_INVALID)

# 降权系数：只压低排序，不删除候选；值越小越靠后。
DOWNWEIGHT_FACTORS: Dict[str, float] = {
    STATE_NOT_YET: 0.35,
    STATE_EXPIRED: 0.25,
    STATE_SUPERSEDED: 0.20,
}
DEFAULT_DOWNWEIGHT = True
MAX_SUPERSEDE_HOPS = 16
DUTY_EVIDENCE_CAP = 5
REPLACEMENT_CAP = 20

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- 时间解析
def local_tz() -> tzinfo:
    """当前本地时区（带固定偏移；中国无夏令时，足够规范本地日边界）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _coerce_tz(tz: Optional[tzinfo]) -> tzinfo:
    return tz if tz is not None else local_tz()


def _local_iso(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).isoformat(timespec="seconds")


def _day_start(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def _parse_moment(
    value: Any,
    *,
    tz: tzinfo,
    end_of_day: bool,
) -> Tuple[Optional[datetime], Optional[str], bool]:
    """解析 ISO 时间/日期 → (aware datetime, 错误信息, 是否仅日期)。

    - 日期（date 或 ``YYYY-MM-DD`` 字符串）：``end_of_day=True`` 时取次日 00:00
      作为排他边界，否则取当日 00:00。
    - 无时区的 datetime 按 ``tz`` 本地化；带时区按原偏移换算。
    """
    if value is None or value == "":
        return None, None, False

    date_only = False
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        date_only = True
        base = _day_start(value, tz)
        return (base + timedelta(days=1) if end_of_day else base), None, True
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None, False
        if _DATE_ONLY_RE.match(text):
            try:
                day = date.fromisoformat(text)
            except ValueError:
                return None, f"日期非法: {text}", True
            date_only = True
            base = _day_start(day, tz)
            return (base + timedelta(days=1) if end_of_day else base), None, True
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, f"时间格式非法: {text}", False
    else:
        return None, f"时间类型不支持: {type(value).__name__}", False

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment, None, date_only


def coerce_as_of(value: Any = None, *, tz: Optional[tzinfo] = None) -> datetime:
    """把查询时点规范为 aware datetime；日期（无时间）取该本地日结束前的排他边界。

    ``None`` → 现在。非法输入抛 ``ValueError``（REST/contract 层转 400）。
    """
    zone = _coerce_tz(tz)
    if value is None:
        return datetime.now(zone)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=zone)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=zone)  # epoch 秒（值班时钟）
    moment, error, _ = _parse_moment(value, tz=zone, end_of_day=True)
    if error or moment is None:
        raise ValueError(error or f"as_of 非法: {value!r}")
    return moment


def parse_as_of_param(value: Optional[str], *, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """HTTP 查询参数 ``as_of`` → datetime；空串/None 返回 None（= 当前）。"""
    if value is None or not str(value).strip():
        return None
    return coerce_as_of(str(value).strip(), tz=tz)


# ---------------------------------------------------------------- 字段解析
def _present(fm: Mapping[str, Any], key: str) -> bool:
    if key not in fm:
        return False
    value = fm.get(key)
    return value is not None and value != "" and value != []


def _path_key(rel: str) -> str:
    text = normalize_rel(rel)
    if text.lower().endswith(".md"):
        text = text[:-3]
    return text.casefold()


def _clean_target(raw: Any) -> str:
    """``[[目标|别名]]`` / ``目标#锚点`` → vault 相对路径。"""
    text = str(raw or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0]
    text = text.split("#", 1)[0]
    return normalize_rel(text)


def _is_reserved(rel: str) -> bool:
    return any(part in IGNORED_DIRS for part in rel.split("/") if part)


def _fact_scope(value: Any) -> Tuple[List[str], Optional[str]]:
    if value is None or value == "":
        return [], None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        parts = [p.strip() for p in re.split(r"[,，、]", text) if p.strip()]
        return parts, None
    if isinstance(value, (list, tuple)):
        items: List[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                return items, "fact_scope 必须是字符串或字符串列表"
            text = str(item).strip().lstrip("#")
            if text:
                items.append(text)
        return items, None
    return [], "fact_scope 必须是字符串或字符串列表"


def _fact_state(fm: Mapping[str, Any], moment: datetime, tz: tzinfo) -> Tuple[str, List[str]]:
    """只按有效区间判定（不追替代链）；用于替代目标自身状态提示。"""
    problems: List[str] = []
    start, err, _ = _parse_moment(fm.get(FIELD_VALID_FROM), tz=tz, end_of_day=False) if _present(fm, FIELD_VALID_FROM) else (None, None, False)
    if err:
        problems.append(f"valid_from {err}")
    end, err, _ = _parse_moment(fm.get(FIELD_VALID_UNTIL), tz=tz, end_of_day=True) if _present(fm, FIELD_VALID_UNTIL) else (None, None, False)
    if err:
        problems.append(f"valid_until {err}")
    if problems:
        return STATE_INVALID, problems
    if start is not None and end is not None and start >= end:
        return STATE_INVALID, ["valid_from 晚于或等于 valid_until"]
    if start is not None and moment < start:
        return STATE_NOT_YET, []
    if end is not None and moment >= end:
        return STATE_EXPIRED, []
    return STATE_CURRENT, []


# ---------------------------------------------------------------- 单篇评估
def evaluate(
    path: str,
    frontmatter: Optional[Mapping[str, Any]],
    *,
    as_of: Any = None,
    tz: Optional[tzinfo] = None,
    lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
    exists: Optional[Callable[[str], bool]] = None,
    historical: bool = False,
) -> Dict[str, Any]:
    """评估单篇笔记在 ``as_of`` 时点的声明状态。

    ``lookup`` / ``exists`` 用于解析替代目标（缺省视为目标不存在，返回 invalid_metadata）。
    ``historical=True`` 表示用户显式查询历史期间：替代目标缺少 ``valid_from`` 时
    只提示关系，不按今天的替代状态压低历史证据。
    """
    zone = _coerce_tz(tz)
    moment = coerce_as_of(as_of, tz=zone)
    fm: Mapping[str, Any] = frontmatter or {}
    rel = normalize_rel(path)

    meta: Dict[str, Any] = {
        "state": STATE_UNKNOWN,
        "as_of": _local_iso(moment, zone),
        FIELD_VALID_FROM: None,
        FIELD_VALID_UNTIL: None,
        FIELD_SUPERSEDED_BY: None,
        FIELD_FACT_SCOPE: [],
        FIELD_VERIFIED_AT: None,
        "verified": False,
        "supersede_effective": None,
        "replacement_state": None,
        "problems": [],
    }

    if not any(_present(fm, key) for key in LIFECYCLE_FIELDS):
        return meta

    problems: List[str] = []

    # ---- 有效区间
    start: Optional[datetime] = None
    if _present(fm, FIELD_VALID_FROM):
        raw = fm.get(FIELD_VALID_FROM)
        start, err, _ = _parse_moment(raw, tz=zone, end_of_day=False)
        if err:
            problems.append(f"valid_from {err}")
            meta[FIELD_VALID_FROM] = raw
        else:
            meta[FIELD_VALID_FROM] = _local_iso(start, zone) if start else None

    end: Optional[datetime] = None
    if _present(fm, FIELD_VALID_UNTIL):
        raw = fm.get(FIELD_VALID_UNTIL)
        end, err, _ = _parse_moment(raw, tz=zone, end_of_day=True)
        if err:
            problems.append(f"valid_until {err}")
            meta[FIELD_VALID_UNTIL] = raw
        else:
            meta[FIELD_VALID_UNTIL] = _local_iso(end, zone) if end else None

    if start is not None and end is not None and start >= end:
        problems.append("valid_from 晚于或等于 valid_until")

    # ---- fact_scope / verified_at
    if _present(fm, FIELD_FACT_SCOPE):
        scope, err = _fact_scope(fm.get(FIELD_FACT_SCOPE))
        meta[FIELD_FACT_SCOPE] = scope
        if err:
            problems.append(err)
    if _present(fm, FIELD_VERIFIED_AT):
        raw = fm.get(FIELD_VERIFIED_AT)
        verified_at, err, _ = _parse_moment(raw, tz=zone, end_of_day=False)
        if err:
            problems.append(f"verified_at {err}")
            meta[FIELD_VERIFIED_AT] = raw
        else:
            meta[FIELD_VERIFIED_AT] = _local_iso(verified_at, zone) if verified_at else None
            meta["verified"] = True

    # ---- 替代关系（单跳生效 + 自指/环检测）
    superseded_effective = False
    if _present(fm, FIELD_SUPERSEDED_BY):
        raw_target = fm.get(FIELD_SUPERSEDED_BY)
        target = _clean_target(raw_target)
        meta[FIELD_SUPERSEDED_BY] = target or str(raw_target)
        if not target:
            problems.append("superseded_by 为空或路径非法")
        elif _path_key(target) == _path_key(rel):
            problems.append("superseded_by 指向自身")
        elif _is_reserved(target):
            problems.append(f"superseded_by 指向保留目录: {target}")
        else:
            direct = _walk_supersede_chain(
                rel, target, lookup=lookup, exists=exists, problems=problems
            )
            if direct is not None and not problems:
                target_fm = direct[1]
                target_state, target_problems = _fact_state(target_fm, moment, zone)
                if _present(target_fm, FIELD_SUPERSEDED_BY):
                    # 目标自身也被替代：不追链，只提示关系；替代证据不再推荐。
                    target_state = STATE_SUPERSEDED
                meta["replacement_state"] = target_state
                if target_problems:
                    problems.append("替代目标 " + "；".join(target_problems))
                else:
                    target_start: Optional[datetime] = None
                    if _present(target_fm, FIELD_VALID_FROM):
                        target_start, err, _ = _parse_moment(
                            target_fm.get(FIELD_VALID_FROM), tz=zone, end_of_day=False
                        )
                        if err:
                            problems.append(f"替代目标 valid_from {err}")
                    if not problems:
                        if target_start is None and historical:
                            # 历史模式：替代生效时间缺失，只提示关系，不按今天降权历史证据。
                            superseded_effective = False
                        else:
                            superseded_effective = (
                                target_start is None or moment >= target_start
                            )
                        meta["supersede_effective"] = superseded_effective

    if problems:
        meta["state"] = STATE_INVALID
        meta["problems"] = problems
        return meta

    if _present(fm, FIELD_SUPERSEDED_BY) and superseded_effective:
        meta["state"] = STATE_SUPERSEDED
        return meta

    if start is not None and moment < start:
        meta["state"] = STATE_NOT_YET
    elif end is not None and moment >= end:
        meta["state"] = STATE_EXPIRED
    else:
        meta["state"] = STATE_CURRENT
    return meta


def _walk_supersede_chain(
    start_path: str,
    target: str,
    *,
    lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]],
    exists: Optional[Callable[[str], bool]],
    problems: List[str],
) -> Optional[Tuple[str, Mapping[str, Any]]]:
    """沿替代链行走检测自指/环；返回直接目标 (path, frontmatter)，异常写入 problems。"""
    if exists is None or lookup is None:
        problems.append(f"替代目标无法校验: {target}")
        return None

    seen = {_path_key(start_path)}
    cur = target
    direct: Optional[Tuple[str, Mapping[str, Any]]] = None
    for hop in range(MAX_SUPERSEDE_HOPS):
        key = _path_key(cur)
        if key in seen:
            problems.append("替代关系成环")
            return None
        seen.add(key)
        resolved = _resolve_target(cur, exists)
        if not exists(resolved):
            problems.append(f"替代目标不存在: {cur}")
            return None
        target_fm = lookup(resolved)
        if target_fm is None:
            problems.append(f"无法读取替代目标: {cur}")
            return None
        if hop == 0:
            direct = (resolved, target_fm)
        if not _present(target_fm, FIELD_SUPERSEDED_BY):
            return direct
        cur = _clean_target(target_fm.get(FIELD_SUPERSEDED_BY))
        if not cur:
            return direct
    problems.append("替代链过长，未能确认终点")
    return None


def _resolve_target(rel: str, exists: Callable[[str], bool]) -> str:
    if exists(rel):
        return rel
    if not rel.lower().endswith(".md") and exists(rel + ".md"):
        return rel + ".md"
    return rel


# ---------------------------------------------------------------- 索引化评估（懒加载）
class FreshnessIndex:
    """面向索引库的懒评估器：只为实际访问的路径读 props，不扫全库。"""

    def __init__(
        self,
        db: Any,
        *,
        as_of: Any = None,
        tz: Optional[tzinfo] = None,
        downweight: bool = DEFAULT_DOWNWEIGHT,
        historical: bool = False,
    ) -> None:
        self.db = db
        self.tz = _coerce_tz(tz)
        self.as_of = coerce_as_of(as_of, tz=self.tz)
        self.downweight = bool(downweight)
        self.historical = bool(historical)
        self._props: Dict[str, Optional[Mapping[str, Any]]] = {}
        self._exists: Dict[str, bool] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}

    # ---- 基础查询
    def exists(self, rel: str) -> bool:
        if rel in self._exists:
            return self._exists[rel]
        found = False
        if self.db is not None:
            try:
                found = self.db.get_note(rel) is not None
            except Exception:  # noqa: BLE001 —— 索引异常不阻断检索
                found = False
        self._exists[rel] = found
        return found

    def props(self, rel: str) -> Optional[Mapping[str, Any]]:
        if rel in self._props:
            return self._props[rel]
        data: Optional[Mapping[str, Any]] = None
        if self.db is not None and self.exists(rel):
            try:
                data = self.db.get_props(rel) or {}
            except Exception:  # noqa: BLE001
                data = {}
        self._props[rel] = data
        return data

    # ---- 评估
    def meta(self, path: str) -> Dict[str, Any]:
        rel = normalize_rel(path)
        if rel in self._meta:
            return self._meta[rel]
        info = evaluate(
            rel,
            self.props(rel) or {},
            as_of=self.as_of,
            tz=self.tz,
            lookup=self.props,
            exists=self.exists,
            historical=self.historical,
        )
        self._meta[rel] = info
        return info

    def factor(self, path: str) -> float:
        if not self.downweight:
            return 1.0
        return DOWNWEIGHT_FACTORS.get(self.meta(path)["state"], 1.0)

    def replacement(self, path: str) -> Optional[str]:
        """被替代且替代证据在该时点有效时，返回直接替代路径。"""
        if not self.downweight:
            return None
        info = self.meta(path)
        if info["state"] != STATE_SUPERSEDED or not info.get(FIELD_SUPERSEDED_BY):
            return None
        if info.get("replacement_state") not in (STATE_CURRENT, STATE_UNKNOWN):
            return None
        target = str(info[FIELD_SUPERSEDED_BY])
        resolved = _resolve_target(target, self.exists)
        return resolved if self.exists(resolved) else None


# ---------------------------------------------------------------- 检索重排
def _compact_meta(info: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "state": info.get("state"),
        FIELD_VALID_FROM: info.get(FIELD_VALID_FROM),
        FIELD_VALID_UNTIL: info.get(FIELD_VALID_UNTIL),
        FIELD_SUPERSEDED_BY: info.get(FIELD_SUPERSEDED_BY),
        FIELD_FACT_SCOPE: info.get(FIELD_FACT_SCOPE) or [],
        FIELD_VERIFIED_AT: info.get(FIELD_VERIFIED_AT),
        "replacement_state": info.get("replacement_state"),
        "problems": list(info.get("problems") or []),
    }


def annotate(info: Mapping[str, Any]) -> Dict[str, Any]:
    """单篇对外元数据：去掉内部 as_of 重复字段，保留可解释信息。"""
    data = _compact_meta(info)
    data["as_of"] = info.get("as_of")
    if info.get("verified"):
        data["verified"] = True
    if info.get("supersede_effective") is not None:
        data["supersede_effective"] = bool(info["supersede_effective"])
    return data


def rerank(
    entries: Sequence[Mapping[str, Any]],
    db: Any,
    *,
    as_of: Any = None,
    downweight: bool = DEFAULT_DOWNWEIGHT,
    tz: Optional[tzinfo] = None,
    cap: int = REPLACEMENT_CAP,
    make_entry: Optional[Callable[[str, float], Dict[str, Any]]] = None,
    historical: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """候选融合后、最终截断前：降权未生效/过期/被替代，并补入有效替代证据。

    只改排序与元数据，不删除候选；``unknown`` / ``invalid_metadata`` 不降权，
    但会带 ``problems`` 提示。``historical=True`` 时按 as_of 评估，不按今天压低。
    返回 (新候选列表, 概览)。
    """
    index = FreshnessIndex(
        db, as_of=as_of, tz=tz, downweight=downweight, historical=historical
    )
    items = [dict(entry) for entry in entries]
    paths = {str(item.get("path") or "") for item in items}
    replacements: List[Dict[str, Any]] = []
    downweighted = 0
    evaluated = 0
    truncated = False

    for item in items:
        path = str(item.get("path") or "")
        if not path:
            continue
        info = index.meta(path)
        if info["state"] == STATE_UNKNOWN:
            continue
        evaluated += 1
        item["freshness"] = annotate(info)
        factor = DOWNWEIGHT_FACTORS.get(info["state"], 1.0) if downweight else 1.0
        if factor < 1.0:
            downweighted += 1
            try:
                base = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                base = 0.0
            item["score"] = round(base * factor, 6)
            raw_score = base
        else:
            try:
                raw_score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                raw_score = 0.0

        replacement = index.replacement(path)
        if not replacement or replacement in paths:
            continue
        if len(replacements) >= cap:
            truncated = True
            continue
        if make_entry is not None:
            entry = make_entry(replacement, raw_score)
        else:
            entry = {"path": replacement, "score": raw_score}
        entry = dict(entry)
        entry["replacement_for"] = path
        rep_info = index.meta(replacement)
        entry["freshness"] = annotate(rep_info)
        replacements.append(entry)
        paths.add(replacement)

    merged = items + replacements
    merged.sort(key=lambda e: (-float(e.get("score") or 0.0), str(e.get("path") or "")))
    summary = {
        "enabled": bool(downweight),
        "as_of": _local_iso(index.as_of, index.tz),
        "evaluated": evaluated,
        "downweighted": downweighted,
        "replacements_added": len(replacements),
        "replacements_truncated": truncated,
    }
    return merged, summary


# ---------------------------------------------------------------- 提案证据检查（A）
def inspect_sources(
    paths: Sequence[str],
    *,
    db: Any = None,
    lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
    exists: Optional[Callable[[str], bool]] = None,
    as_of: Any = None,
    tz: Optional[tzinfo] = None,
    limit: int = DUTY_EVIDENCE_CAP,
    historical: bool = False,
) -> Dict[str, Any]:
    """最多检查 ``limit`` 份相关证据的有效期/替代关系；只提示，不阻断批准。

    ``db`` 与 ``lookup/exists`` 至少给一组；返回 risks 只含非 current/unknown 的项。
    """
    unique: List[str] = []
    for raw in paths:
        rel = normalize_rel(raw)
        if rel and rel not in unique:
            unique.append(rel)
    selected = unique[: max(0, int(limit))]

    if db is not None:
        index = FreshnessIndex(
            db, as_of=as_of, tz=tz, downweight=False, historical=historical
        )
        meta_of = index.meta
    else:
        zone = _coerce_tz(tz)
        moment = coerce_as_of(as_of, tz=zone)

        def meta_of(rel: str) -> Dict[str, Any]:
            fm = lookup(rel) if lookup is not None else None
            return evaluate(
                rel,
                fm or {},
                as_of=moment,
                tz=zone,
                lookup=lookup,
                exists=exists,
                historical=historical,
            )

    risks: List[Dict[str, Any]] = []
    for rel in selected:
        info = meta_of(rel)
        if info["state"] in (STATE_CURRENT, STATE_UNKNOWN):
            continue
        item = {"path": rel, **annotate(info)}
        risks.append(item)

    return {
        "checked": len(selected),
        "total": len(unique),
        "truncated": len(unique) > len(selected),
        "risks": risks,
        "message": (
            "发现有效期/替代关系风险，请人工确认；不会自动撤销旧事实。"
            if risks
            else "未发现明确的失效声明。"
        ),
    }


# ---------------------------------------------------------------- 配置
def downweight_enabled(config: Any) -> bool:
    """读取 vault 设置 ``freshness.downweight``；缺失时默认开启。"""
    raw = None
    try:
        raw = config.get("freshness") if config is not None else None
    except Exception:  # noqa: BLE001 —— 配置对象异常按默认
        raw = None
    if isinstance(raw, Mapping) and "downweight" in raw:
        return bool(raw.get("downweight"))
    return DEFAULT_DOWNWEIGHT
