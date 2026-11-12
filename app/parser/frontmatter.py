"""YAML frontmatter 解析/写回。

写回策略：只替换被编辑的字段（dict 合并语义由调用方控制），
YAML 序列化 ``sort_keys=False`` 保持字段顺序，正文原样不动；
未知自定义字段不丢不排序。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

import yaml
from frontmatter import loads as fm_loads

FM_PATTERN = re.compile(r"\A---\s*\n(.*?)\n?---\s*(?:\n|$)", re.DOTALL)


def parse_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
    """解析文本 → (frontmatter dict, 正文)。无 frontmatter 时返回 ({}, 原文)。

    读取容忍 UTF-8 BOM（由文件读取层以 utf-8-sig 保证）。
    """
    if not raw:
        return {}, ""
    post = fm_loads(raw)
    metadata = dict(post.metadata) if post.metadata else {}
    return metadata, post.content


def dump_frontmatter(metadata: Dict[str, Any], body: str) -> str:
    """(frontmatter, 正文) → 完整文件文本。metadata 为空时直接返回正文。"""
    if not metadata:
        return body if body.endswith("\n") or body == "" else body + "\n"
    fm_text = yaml.safe_dump(
        metadata,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10 ** 6,
    )
    body = body if (body.endswith("\n") or body == "") else body + "\n"
    return f"---\n{fm_text}---\n{body}"


def update_fields(metadata: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """合并更新字段（None 值表示删除该字段），返回新 dict 不改原对象。"""
    result = dict(metadata)
    for key, value in updates.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def normalize_frontmatter_for_write(metadata: Dict[str, Any], is_new: bool) -> Dict[str, Any]:
    """写入前规范化：自动补/刷新 created / updated（ISO 8601 本地时间带偏移）。"""
    from datetime import datetime

    result = dict(metadata)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if is_new:
        result.setdefault("created", now)
    result["updated"] = now
    return result
