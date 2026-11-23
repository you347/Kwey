"""视图查询引擎：定义（YAML dict）→ 行数据。

纯内存计算，输入是索引快照（notes/props/tags），不碰磁盘。
条件、排序、列、聚合都是白名单操作，不接受任何可执行代码（不学 Dataview 的 JS）。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.views.schema import infer_type

VIEW_DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "name": "未命名视图",
    "icon": "table",
    "mode": "table",          # table | cards | list
    "folder": "",             # 目录前缀（"" = 全库）
    "tags": [],               # 全部命中
    "any_tags": [],           # 任一命中
    "where": [],              # [{key, op, value}] 或字符串
    "columns": [],            # [] = 自动（标题 + 覆盖最高的若干属性）
    "sort": [],               # [{key, dir}]
    "group_by": "",
    "search": "",
    "limit": 200,
    "card_title": "",
    "card_body": "",
    "summaries": {},          # {key: count|sum|avg|min|max|unique}
}

MAX_LIMIT = 2000
_OPS = {
    "=", "==", "!=", ">", ">=", "<", "<=",
    "contains", "not_contains", "in", "not_in",
    "exists", "empty", "before", "after",
}
_ALIAS = {"==": "=", "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_COND_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*"
    r"(==|!=|>=|<=|=|>|<|\bcontains\b|\bnot_contains\b|\bin\b|\bnot_in\b|"
    r"\bexists\b|\bempty\b|\bbefore\b|\bafter\b)\s*(.*)$",
    re.IGNORECASE,
)
_QUOTES = ("'", '"', "`")


# --------------------------------------------------------------------- snapshot
class _SnapshotCache:
    """索引快照缓存：同一秒内的多次查询（排序/翻页）不重复拼装。

    key 用 (笔记数, max(mtime)) 粗判索引是否变化——写路径都会更新 mtime，
    比维护版本号简单且不会漏。
    """

    def __init__(self, ttl: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._ttl = ttl
        self._stamp: Optional[Tuple] = None
        self._at = 0.0
        self._data: Optional[Dict[str, Any]] = None

    def invalidate(self) -> None:
        with self._lock:
            self._data = None
            self._stamp = None

    def get(self, db) -> Dict[str, Any]:
        with self._lock:
            try:
                row = db.conn.execute("SELECT COUNT(*), MAX(mtime) FROM notes").fetchone()
                stamp = (int(row[0] or 0), float(row[1] or 0.0))
            except Exception:  # noqa: BLE001 —— 索引不可用时不缓存
                stamp = None
            now = time.monotonic()
            if stamp is not None and self._data is not None and self._stamp == stamp and now - self._at < self._ttl:
                return self._data
            data = build_snapshot(db)
            self._stamp, self._at, self._data = stamp, now, data
            return data


_CACHE = _SnapshotCache()


def cached_snapshot(db) -> Dict[str, Any]:
    return _CACHE.get(db)


def invalidate_cache() -> None:
    """写路径后显式失效（TTL 只有 1s，这里让"改完立刻看到"无延迟）。"""
    _CACHE.invalidate()


def build_snapshot(db) -> Dict[str, Any]:
    """从索引构建 {notes, props, tags}。"""
    notes = {n["path"]: n for n in db.all_notes()}
    props = db.all_props()
    tags: Dict[str, List[str]] = {}
    try:
        for path, tag in db.all_tag_note_pairs():
            tags.setdefault(path, []).append(tag)
    except Exception:  # noqa: BLE001
        tags = {}
    for bucket in tags.values():
        bucket.sort()
    return {"notes": notes, "props": props, "tags": tags}


# --------------------------------------------------------------------- helpers
def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _as_ts(value: Any) -> Optional[float]:
    """字符串/数字 → 时间戳（秒）；解析不了返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        # 毫秒时间戳归一化
        return num / 1000.0 if num > 1e11 else num
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).timestamp()
        except ValueError:
            continue
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1", "是"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "0", "否"}:
        return False
    return None


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        return text[1:-1]
    return text


def _split_list(text: str) -> List[str]:
    """解析 ``[a, b]`` / ``a,b`` → ['a','b']（支持中英文逗号）。"""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = re.split(r"[,，]", text)
    return [_unquote(p).strip() for p in parts if _unquote(p).strip()]


def parse_where(raw: Any) -> List[Dict[str, Any]]:
    """把 where 的各种写法统一成条件列表。"""
    if not raw:
        return []
    items: List[Any] = []
    if isinstance(raw, str):
        # 支持 "a = 1 and b != 2"；OR 不支持（避免优先级歧义），用 any_tags 表达
        chunks = re.split(r"\s+(?:and|AND|&&)\s+", raw)
        items = chunks
    elif isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []

    out: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("field") or "").strip()
            op = str(item.get("op") or "=").strip().lower()
            if not key:
                continue
            out.append({"key": key, "op": _ALIAS.get(op, op), "value": item.get("value")})
            continue
        if not isinstance(item, str):
            continue
        match = _COND_RE.match(item)
        if not match:
            continue
        key, op, rest = match.group(1), match.group(2).lower(), match.group(3)
        op = _ALIAS.get(op, op)
        if op in {"exists", "empty"}:
            value: Any = None
        elif op in {"in", "not_in"}:
            value = _split_list(rest)
        else:
            text = _unquote(rest)
            num = _as_number(text)
            value = num if (num is not None and text != "") else text
        out.append({"key": key, "op": op, "value": value})
    return out


def resolve_value(snapshot: Dict[str, Any], path: str, key: str) -> Any:
    """按键名取值；支持 file.name / file.path / file.folder / file.tags / file.mtime。"""
    note = snapshot["notes"].get(path) or {}
    props = snapshot["props"].get(path) or {}
    norm = (key or "").strip()
    alias = {
        "name": "title",
        "file": "path",
        "file.name": "title",
        "file.path": "path",
        "file.folder": "folder",
        "file.tags": "tags",
        "file.mtime": "mtime",
        "file.size": "size",
        "file.created": "created",
        "file.updated": "updated",
        "body": "excerpt",
    }.get(norm, norm)

    if alias == "title":
        return note.get("title") or props.get("title") or path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if alias in {"path", "folder", "mtime", "size", "excerpt"}:
        if alias == "path":
            return path
        if alias == "folder":
            return _dirname(path)
        return note.get(alias)
    if alias == "tags":
        return snapshot["tags"].get(path, [])
    if alias in {"created", "updated"}:
        return props.get(alias) or note.get(alias)

    if "." in norm:
        current: Any = props
        for part in norm.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return props.get(norm)
        return current
    return props.get(norm)


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, (list, tuple)):
        return any(_equal(item, b) for item in a)
    if isinstance(b, (list, tuple)):
        return any(_equal(a, item) for item in b)
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    ba, bb = _as_bool(a), _as_bool(b)
    if ba is not None and bb is not None:
        return ba is bb
    return str(a).strip().lower() == str(b).strip().lower()


def match_condition(value: Any, op: str, target: Any) -> bool:
    op = _ALIAS.get((op or "=").lower(), (op or "=").lower())
    if op == "exists":
        return value not in (None, "", [], {})
    if op == "empty":
        return value in (None, "", [], {})
    if op == "contains":
        if isinstance(value, (list, tuple)):
            return any(_equal(item, target) for item in value)
        return str(target).lower() in str(value).lower()
    if op == "not_contains":
        return not match_condition(value, "contains", target)
    if op in {"in", "not_in"}:
        targets = target if isinstance(target, (list, tuple)) else [target]
        hit = any(_equal(value, item) for item in targets)
        return hit if op == "in" else not hit
    if op in {"before", "after"}:
        va, vb = _as_ts(value), _as_ts(target)
        if va is None or vb is None:
            return False
        return va < vb if op == "before" else va > vb
    if op == "=":
        return _equal(value, target)
    if op == "!=":
        return not _equal(value, target)

    if isinstance(value, (list, tuple)):
        return False
    na, nb = _as_number(value), _as_number(target)
    if na is not None and nb is not None:
        left, right = na, nb
    else:
        ta, tb = _as_ts(value), _as_ts(target)
        if ta is not None and tb is not None:
            left, right = ta, tb
        else:
            left, right = str(value).lower(), str(target).lower()
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    return False


def _sort_value(value: Any) -> Tuple[int, Any]:
    """排序键：空值永远排最后；数字/时间按数值，其余按小写字符串。"""
    if value in (None, "", [], {}):
        return (1, "")
    if isinstance(value, (list, tuple)):
        return (0, " ".join(str(v) for v in value).lower())
    num = _as_number(value)
    if num is not None:
        return (0, num)
    ts = _as_ts(value)
    if ts is not None and isinstance(value, str):
        return (0, ts)
    return (0, str(value).lower())


# --------------------------------------------------------------------- definition
def normalize_definition(raw: Optional[Dict[str, Any]], *, keep_unknown: bool = False) -> Dict[str, Any]:
    """把外部输入（AI / 用户 YAML / 老 .base）规范成可执行定义。"""
    raw = dict(raw or {})
    out = dict(VIEW_DEFAULTS)

    out["name"] = str(raw.get("name") or raw.get("title") or out["name"]).strip()[:120]
    mode = str(raw.get("mode") or raw.get("type") or "table").strip().lower()
    out["mode"] = mode if mode in {"table", "cards", "list"} else "table"
    out["icon"] = str(raw.get("icon") or "table")[:32]

    folder = raw.get("folder") or raw.get("dir") or ""
    if isinstance(folder, str):
        folder = folder.strip().strip("/")
        out["folder"] = "" if folder in {"", ".", "/"} else folder

    def _tags(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [t.strip().lstrip("#") for t in re.split(r"[,，\s]+", value) if t.strip()]
        if isinstance(value, (list, tuple)):
            return [str(t).strip().lstrip("#") for t in value if str(t).strip()]
        return []

    out["tags"] = _tags(raw.get("tags"))[:20]
    out["any_tags"] = _tags(raw.get("any_tags") or raw.get("tags_any"))[:20]
    out["where"] = parse_where(raw.get("where") or raw.get("filter"))[:20]
    out["search"] = str(raw.get("search") or "")[:200]
    out["group_by"] = str(raw.get("group_by") or "").strip()[:80]
    out["card_title"] = str(raw.get("card_title") or "").strip()[:80]
    out["card_body"] = str(raw.get("card_body") or "").strip()[:80]

    columns: List[Dict[str, Any]] = []
    for item in raw.get("columns") or raw.get("order") or []:
        if isinstance(item, str):
            if item.strip():
                columns.append({"key": item.strip(), "label": item.strip()})
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("field") or "").strip()
            if key:
                columns.append({
                    "key": key,
                    "label": str(item.get("label") or key),
                    "width": item.get("width"),
                    "format": item.get("format"),
                })
    out["columns"] = columns[:24]

    sort: List[Dict[str, str]] = []
    raw_sort = raw.get("sort")
    if isinstance(raw_sort, str):
        raw_sort = [{"key": raw_sort, "dir": "asc"}]
    if isinstance(raw_sort, dict):
        raw_sort = [raw_sort]
    for item in raw_sort or []:
        if isinstance(item, str) and item.strip():
            sort.append({"key": item.strip(), "dir": "asc"})
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("field") or "").strip()
            direction = str(item.get("dir") or item.get("direction") or "asc").lower()
            if key:
                sort.append({"key": key, "dir": "desc" if direction.startswith("desc") else "asc"})
    out["sort"] = sort[:4]

    try:
        limit = int(raw.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    out["limit"] = max(1, min(limit, MAX_LIMIT))

    summaries: Dict[str, str] = {}
    raw_sum = raw.get("summaries") or raw.get("summary") or {}
    if isinstance(raw_sum, dict):
        for key, kind in list(raw_sum.items())[:12]:
            kind = str(kind).lower()
            if kind in {"count", "sum", "avg", "min", "max", "unique"}:
                summaries[str(key)] = kind
    out["summaries"] = summaries

    if keep_unknown:
        for key, value in raw.items():
            out.setdefault(key, value)
    return out


# --------------------------------------------------------------------- run
def _auto_columns(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """没指定列时：标题 + 覆盖率高、取值重复度低的属性（最多 6 列）。"""
    counts: Dict[str, int] = {}
    for fm in snapshot["props"].values():
        if isinstance(fm, dict):
            for key in fm:
                if key in {"created", "updated"}:
                    continue
                counts[str(key)] = counts.get(str(key), 0) + 1
    total = max(len(snapshot["props"]), 1)
    ranked = sorted(
        ((k, c) for k, c in counts.items() if c / total >= 0.08),
        key=lambda kv: (-kv[1], kv[0]),
    )
    columns = [{"key": "title", "label": "标题"}]
    for key, _count in ranked[:6]:
        columns.append({"key": key, "label": key})
    return columns


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def run_query(
    snapshot: Dict[str, Any],
    definition: Dict[str, Any],
    *,
    offset: int = 0,
    limit_override: Optional[int] = None,
) -> Dict[str, Any]:
    """执行视图查询。definition 需先经过 normalize_definition。"""
    defn = definition
    notes: Dict[str, Dict] = snapshot["notes"]
    props: Dict[str, Dict] = snapshot["props"]
    tags: Dict[str, List[str]] = snapshot["tags"]

    folder = defn.get("folder") or ""
    need_tags = list(dict.fromkeys((defn.get("tags") or []) + (defn.get("any_tags") or [])))
    require_all = set(defn.get("tags") or [])
    require_any = set(defn.get("any_tags") or [])
    conditions = defn.get("where") or []
    needle = (defn.get("search") or "").strip().lower()

    matched: List[Dict[str, Any]] = []
    for path, note in notes.items():
        if folder and not (path == folder or path.startswith(folder + "/")):
            continue
        note_tags = set(tags.get(path) or [])
        if require_all and not require_all.issubset(note_tags):
            continue
        if require_any and not (require_any & note_tags):
            continue
        fm = props.get(path) or {}
        if conditions:
            ok = True
            for cond in conditions:
                value = resolve_value(snapshot, path, cond["key"])
                if not match_condition(value, cond["op"], cond.get("value")):
                    ok = False
                    break
            if not ok:
                continue
        if needle:
            haystack = " ".join([
                _stringify(note.get("title")),
                _stringify(note.get("excerpt")),
                _stringify(fm),
                _stringify(tags.get(path)),
            ]).lower()
            if needle not in haystack:
                continue
        matched.append({"path": path, "note": note, "fm": fm, "tags": sorted(note_tags)})

    # 排序（多键，稳定）
    sort_rules = defn.get("sort") or []
    if sort_rules:
        for rule in reversed(sort_rules):
            key, desc = rule["key"], rule.get("dir") == "desc"
            matched.sort(
                key=lambda row: _sort_value(resolve_value(snapshot, row["path"], key)),
                reverse=desc,
            )
    else:
        matched.sort(key=lambda row: -(row["note"].get("mtime") or 0))

    total = len(matched)
    columns = defn.get("columns") or _auto_columns(snapshot)
    # 补类型（前端据此渲染勾选框/数字/日期）
    type_map = {c["key"]: c.get("type") for c in _column_types(snapshot, [c["key"] for c in columns])}
    for col in columns:
        col.setdefault("label", col["key"])
        if not col.get("type"):
            col["type"] = type_map.get(col["key"]) or "text"

    limit = int(limit_override or defn.get("limit") or 200)
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, int(offset or 0))
    page = matched[offset: offset + limit]

    rows: List[Dict[str, Any]] = []
    group_key = defn.get("group_by") or ""
    for item in page:
        values: Dict[str, Any] = {}
        for col in columns:
            values[col["key"]] = _clean(resolve_value(snapshot, item["path"], col["key"]))
        if group_key:
            values.setdefault(group_key, _clean(resolve_value(snapshot, item["path"], group_key)))
        rows.append({
            "path": item["path"],
            "title": item["note"].get("title") or "",
            "excerpt": item["note"].get("excerpt") or "",
            "folder": _dirname(item["path"]),
            "mtime": item["note"].get("mtime"),
            "tags": item["tags"],
            "values": values,
        })

    result: Dict[str, Any] = {
        "name": defn.get("name"),
        "mode": defn.get("mode"),
        "columns": columns,
        "rows": rows,
        "total": total,
        "returned": len(rows),
        "offset": offset,
        "limit": limit,
        "group_by": group_key,
        "groups": [],
        "aggregates": {},
    }
    if group_key:
        buckets: Dict[str, int] = {}
        for item in matched:
            raw = resolve_value(snapshot, item["path"], group_key)
            label = _stringify(raw) or "（空）"
            buckets[label] = buckets.get(label, 0) + 1
        result["groups"] = [
            {"value": key, "label": key, "count": count}
            for key, count in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    result["aggregates"] = _aggregates(snapshot, matched, columns, defn.get("summaries") or {})
    return result


def _clean(value: Any) -> Any:
    """JSON 友好化：datetime → ISO，tuple → list，其它原样。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    return str(value)


def _column_types(snapshot: Dict[str, Any], keys: List[str]) -> List[Dict[str, str]]:
    """为每列推断类型（基于前 200 篇的取值采样）。"""
    sample_paths = list(snapshot["notes"].keys())[:200]
    out: List[Dict[str, str]] = []
    for key in keys:
        if key in {"title", "path", "folder", "excerpt", "tags"}:
            out.append({"key": key, "type": "list" if key == "tags" else "text"})
            continue
        if key in {"mtime", "size"}:
            out.append({"key": key, "type": "date" if key == "mtime" else "number"})
            continue
        values = [resolve_value(snapshot, p, key) for p in sample_paths]
        out.append({"key": key, "type": infer_type(values)})
    return out


def _aggregates(
    snapshot: Dict[str, Any],
    matched: List[Dict[str, Any]],
    columns: List[Dict[str, Any]],
    summaries: Dict[str, str],
) -> Dict[str, Any]:
    """列聚合。未指定 summaries 时给数值列补 count/sum，前端可直接显示页脚。"""
    wanted: Dict[str, set] = {}
    for key, kind in summaries.items():
        wanted.setdefault(key, set()).add(kind)

    numeric_keys = [c["key"] for c in columns if c.get("type") == "number"]
    for key in numeric_keys:
        wanted.setdefault(key, set()).update({"sum", "avg"})
    for col in columns:
        wanted.setdefault(col["key"], set()).add("count")

    out: Dict[str, Any] = {}
    for key, kinds in wanted.items():
        values = [_clean(resolve_value(snapshot, item["path"], key)) for item in matched]
        filled = [v for v in values if v not in (None, "", [], {})]
        stats: Dict[str, Any] = {"count": len(filled)}
        numbers = [n for n in (_as_number(v) for v in filled) if n is not None]
        if "sum" in kinds and numbers:
            stats["sum"] = round(sum(numbers), 6)
        if "avg" in kinds and numbers:
            stats["avg"] = round(sum(numbers) / len(numbers), 4)
        if "min" in kinds and numbers:
            stats["min"] = min(numbers)
        if "max" in kinds and numbers:
            stats["max"] = max(numbers)
        if "unique" in kinds:
            stats["unique"] = len({_stringify(v).lower() for v in filled})
        out[key] = stats
    return out
