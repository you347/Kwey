"""内置智能体反馈：点赞 / 点踩落盘，并注入系统提示做轻量调教。

存储位置：``<vault>/.kwey/feedback.json``（vault 级，随知识库走）。
每条记录：``{id, rating: up|down, role, content, ts}``；同 id 覆盖，``clear`` 删除。

注入策略：只取最近若干条带内容的评价，拼成一段「用户反馈」提示，
让模型保持被认可的表达方式、避开被否定的表达方式；安全约束不变。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

FILENAME = "feedback.json"
MAX_ITEMS = 200          # 文件保留上限（按时间序，超出丢最旧）
MAX_CONTENT = 600        # 单条正文截断（落盘）
MAX_SNIPPET = 140        # 提示词里每条摘要截断
PROMPT_ITEMS = 3         # 认可 / 不喜欢各取几条

_lock = threading.RLock()


def _path(root: Path) -> Path:
    return Path(root) / ".kwey" / FILENAME


def load(root: Optional[Path]) -> List[Dict[str, Any]]:
    """读取全部反馈；文件缺失或损坏时返回空列表（不抛错）。"""
    if root is None:
        return []
    path = _path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _save(root: Path, items: List[Dict[str, Any]]) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(items[-MAX_ITEMS:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)  # 原子替换，避免半截文件


def upsert(
    root: Path,
    *,
    fid: str,
    rating: str,
    content: str = "",
    role: str = "assistant",
) -> Dict[str, Any]:
    """写入 / 覆盖 / 删除一条反馈，返回最新统计。"""
    with _lock:
        items = [x for x in load(root) if x.get("id") != fid]
        if rating in ("up", "down"):
            items.append({
                "id": fid,
                "rating": rating,
                "role": role if role in ("user", "assistant") else "assistant",
                "content": str(content or "")[:MAX_CONTENT],
                "ts": int(time.time()),
            })
        _save(root, items)
        return {"id": fid, "rating": rating if rating in ("up", "down") else None, **counts(root)}


def counts(root: Optional[Path]) -> Dict[str, int]:
    items = load(root)
    up = sum(1 for x in items if x.get("rating") == "up")
    down = sum(1 for x in items if x.get("rating") == "down")
    return {"up": up, "down": down, "total": up + down}


def _snippet(text: str) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:MAX_SNIPPET] + ("…" if len(flat) > MAX_SNIPPET else "")


def prompt_hint(root: Optional[Path], limit: int = PROMPT_ITEMS) -> str:
    """生成注入系统提示的反馈摘要；无反馈时返回空串。"""
    items = [
        x for x in load(root)
        if x.get("rating") in ("up", "down") and str(x.get("content") or "").strip()
    ]
    if not items:
        return ""
    liked = [_snippet(x["content"]) for x in reversed(items) if x["rating"] == "up"][:limit]
    disliked = [_snippet(x["content"]) for x in reversed(items) if x["rating"] == "down"][:limit]
    if not liked and not disliked:
        return ""
    lines = ["", "用户反馈（用于校准你的回复风格与做事方式，务必参考；安全约束不变）："]
    if liked:
        lines.append("- 认可（保持这种做法）：")
        lines.extend(f"  · {x}" for x in liked)
    if disliked:
        lines.append("- 不喜欢（避免这种做法）：")
        lines.extend(f"  · {x}" for x in disliked)
    return "\n".join(lines)
