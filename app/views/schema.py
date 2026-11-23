"""属性 schema：把全库 frontmatter 汇总成「有哪些字段、什么类型、有哪些取值」。

用途有三：
1. 视图编辑器给出可选列；
2. AI 建视图时把真实字段喂给模型（避免它凭空编 ``status``/``priority``）；
3. 设置页/视图空状态提示"你的库里还没有属性"。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

# 常用键优先展示（其余按出现次数排序）
_PREFERRED = [
    "title", "tags", "status", "type", "priority", "due", "date", "author",
    "category", "project", "area", "rating", "created", "updated",
]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
_MAX_SAMPLES = 12


def _is_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return True
    except ValueError:
        # 只到日的 YYYY-MM-DD 也接受（fromisoformat 在 3.11+ 支持，这里兜底老格式）
        try:
            datetime.strptime(value.strip()[:10], "%Y-%m-%d")
            return True
        except ValueError:
            return False


def infer_type(values: Iterable[Any]) -> str:
    """按一列的取值推断类型：number / date / checkbox / list / text。"""
    seen = [v for v in values if v is not None and v != ""]
    if not seen:
        return "text"
    if all(isinstance(v, bool) for v in seen):
        return "checkbox"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return "number"
    if all(isinstance(v, str) and _is_date(v) for v in seen):
        return "date"
    if any(isinstance(v, (list, tuple)) for v in seen):
        return "list"
    return "text"


def _flatten(value: Any) -> List[str]:
    """把标量/列表统一成字符串列表（用于取值统计）。"""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    if isinstance(value, bool):
        return ["true" if value else "false"]
    return [str(value)]


def build_schema(
    props: Dict[str, Dict],
    notes: Optional[Dict[str, Dict]] = None,
    limit_values: int = _MAX_SAMPLES,
) -> Dict[str, Any]:
    """props = {path: frontmatter}；notes = {path: 元数据}（可选，补 title/tags）。

    返回 ``{"keys": [{key,label,type,count,coverage,values:[{value,count}]}], "total": n}``
    """
    notes = notes or {}
    counts: Dict[str, int] = {}
    values: Dict[str, List[Any]] = {}
    for path, fm in props.items():
        if not isinstance(fm, dict):
            continue
        for key, value in fm.items():
            key = str(key)
            counts[key] = counts.get(key, 0) + 1
            bucket = values.setdefault(key, [])
            if len(bucket) < 400:      # 类型推断/取值统计的采样上限，避免大库吃内存
                bucket.append(value)

    total = max(len(props), 1)
    keys: List[Dict[str, Any]] = []
    # 内置列（不在 frontmatter 里也永远可用）
    for builtin, label, typ in (
        ("title", "标题", "text"),
        ("path", "路径", "text"),
        ("folder", "目录", "text"),
        ("tags", "标签", "list"),
        ("mtime", "修改时间", "date"),
        ("size", "大小", "number"),
        ("excerpt", "摘要", "text"),
    ):
        keys.append({
            "key": builtin, "label": label, "type": typ,
            "count": len(notes) or len(props), "coverage": 1.0,
            "builtin": True, "values": [],
        })

    for key, count in counts.items():
        bucket = values.get(key, [])
        typ = infer_type(bucket)
        distinct: Dict[str, int] = {}
        for value in bucket:
            for text in _flatten(value):
                distinct[text] = distinct.get(text, 0) + 1
        top = sorted(distinct.items(), key=lambda kv: (-kv[1], kv[0]))[:limit_values]
        keys.append({
            "key": key,
            "label": key,
            "type": typ,
            "count": count,
            "coverage": round(count / total, 4),
            "builtin": False,
            "values": [{"value": v, "count": c} for v, c in top],
            "distinct": len(distinct),
        })

    def sort_key(item: Dict[str, Any]):
        key = item["key"]
        preferred = _PREFERRED.index(key) if key in _PREFERRED else len(_PREFERRED)
        return (0 if item.get("builtin") else 1, preferred, -item["count"], key)

    keys.sort(key=sort_key)
    return {"keys": keys, "total": len(props)}


def schema_for_prompt(schema: Dict[str, Any], max_keys: int = 40) -> str:
    """把 schema 压成给模型看的紧凑清单（字段 + 类型 + 样例值）。"""
    lines: List[str] = []
    for item in schema.get("keys", []):
        if item.get("builtin") and item["key"] not in ("tags",):
            continue
        samples = [v["value"] for v in item.get("values", [])][:5]
        suffix = f" 例:{samples}" if samples else ""
        lines.append(f"- {item['key']} ({item['type']}, {item['count']} 篇){suffix}")
        if len(lines) >= max_keys:
            break
    return "\n".join(lines) or "（该库还没有 frontmatter 属性，只能用 title/path/tags）"
