"""只读候选收集：三类固定职责的输入指纹与事实摘要。

候选只从注入的 :class:`app.duty.reader.DutyReader` 读取；本模块不写盘、
不调用模型。候选的 ``digest`` 是输入哈希的组成部分，用于跨轮去重。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.paths import normalize_rel

from app.duty.policy import in_scope
from app.duty.reader import DutyReader
from app.duty.recipes import MAX_NOTE_CHARS, recipe_for

_TODO_RE = re.compile(r"^\s*[-*+]\s*\[ \]\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Candidate:
    """单条候选：路径 + 内容摘要 + 可选事实字段。"""

    path: str
    title: str
    mtime: float
    size: int
    digest: str
    excerpt: str = ""
    oversized: bool = False
    facts: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.path or f"issue:{self.facts.get('issue', '')}"


@dataclass
class CandidateSet:
    """一轮只读收集的结果；``summary`` 可安全写入运行记录（不含正文）。"""

    recipe: str
    items: List[Candidate]
    summary: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    oversized: List[str] = field(default_factory=list)


def _digest(raw: str, max_chars: int = MAX_NOTE_CHARS) -> tuple:
    """计算原文 sha256；超长只哈希前半段 + 长度标记，并标记需人工处理。"""
    if len(raw) > max_chars:
        payload = raw[:max_chars] + f"\x00TRUNCATED:{len(raw)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest(), True
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), False


def _read(reader: DutyReader, path: str) -> Optional[str]:
    try:
        return reader.read_raw(path)
    except Exception:  # noqa: BLE001 —— 单篇不可读不应中断整轮
        return None


def _title(note: Mapping[str, Any]) -> str:
    return str(note.get("title") or note.get("path") or "")


def _excerpt(note: Mapping[str, Any], limit: int = 300) -> str:
    return str(note.get("excerpt") or "")[:limit]


# --------------------------------------------------------------------- 收件箱
def collect_inbox(
    reader: DutyReader,
    definition: Mapping[str, Any],
    *,
    now: float,
    dirs: Mapping[str, str],
) -> CandidateSet:
    limit = recipe_for("inbox_duty").candidate_limit
    scope = tuple(definition.get("scope") or ())
    inbox_dir = scope[0] if scope else normalize_rel(dirs.get("inbox", "inbox"))
    notes = reader.list_notes(inbox_dir)

    items: List[Candidate] = []
    oversized: List[str] = []
    for note in notes[:limit]:
        path = str(note.get("path") or "")
        raw = _read(reader, path)
        if raw is None:
            continue
        digest, over = _digest(raw, int((definition.get("limits") or {}).get("max_note_chars", MAX_NOTE_CHARS)))
        if over:
            oversized.append(path)
        items.append(
            Candidate(
                path=path,
                title=_title(note),
                mtime=float(note.get("mtime") or 0),
                size=len(raw),
                digest=digest,
                excerpt=_excerpt(note),
                oversized=over,
                facts={"tags": list(reader.tags_of(path))},
            )
        )

    return CandidateSet(
        recipe="inbox_duty",
        items=items,
        summary={
            "dir": inbox_dir,
            "backlog": len(notes),
            "collected": len(items),
            "oversized": oversized,
            "skipped_oversized": len(oversized),
            "truncated": len(notes) > limit,
        },
        truncated=len(notes) > limit,
        oversized=oversized,
    )


# --------------------------------------------------------------------- 每日简报
def collect_daily(
    reader: DutyReader,
    definition: Mapping[str, Any],
    *,
    now: float,
    dirs: Mapping[str, str],
    tz: Any = None,
) -> CandidateSet:
    limit = recipe_for("daily_brief").candidate_limit
    scope = tuple(definition.get("scope") or ())
    output_dir = normalize_rel(dirs.get("daily", "daily"))
    window_start = float(now) - 24 * 3600

    notes = []
    for note in reader.list_notes(""):
        path = str(note.get("path") or "")
        mtime = float(note.get("mtime") or 0)
        if mtime < window_start:
            continue
        if not in_scope(path, scope):
            continue
        if output_dir and (path == output_dir or path.startswith(output_dir + "/")):
            continue  # 排除职责自己的输出目录，防回环
        notes.append(note)
    notes.sort(key=lambda note: (float(note.get("mtime") or 0), note["path"]), reverse=True)

    items: List[Candidate] = []
    oversized: List[str] = []
    open_todos: List[str] = []
    for note in notes[:limit]:
        path = str(note.get("path") or "")
        raw = _read(reader, path)
        if raw is None:
            continue
        digest, over = _digest(raw, int((definition.get("limits") or {}).get("max_note_chars", MAX_NOTE_CHARS)))
        if over:
            oversized.append(path)
        todos = [text.strip() for text in _TODO_RE.findall(raw)][:5]
        open_todos.extend(f"{_title(note)}: {text}" for text in todos)
        items.append(
            Candidate(
                path=path,
                title=_title(note),
                mtime=float(note.get("mtime") or 0),
                size=len(raw),
                digest=digest,
                excerpt=_excerpt(note),
                oversized=over,
                facts={"todos": todos, "links": raw.count("[[")},
            )
        )

    changed = [item.title or item.path for item in items]
    summary = {
        "window_start": window_start,
        "window_end": float(now),
        "note_count": len(notes),
        "collected": len(items),
        "changed": changed[:20],
        "open_todos": open_todos[:20],
        "oversized": oversized,
        "skipped_oversized": len(oversized),
        "truncated": len(notes) > limit,
        "rule_only": True,
    }
    return CandidateSet(
        recipe="daily_brief",
        items=items,
        summary=summary,
        truncated=len(notes) > limit,
        oversized=oversized,
    )


# --------------------------------------------------------------------- 健康巡检
def collect_weekly(
    reader: DutyReader,
    definition: Mapping[str, Any],
    *,
    now: float,
    dirs: Mapping[str, str],
) -> CandidateSet:
    limit = recipe_for("weekly_health").candidate_limit
    scope = tuple(definition.get("scope") or ())
    snapshot = reader.health_snapshot(deep=False)

    # 体检引擎按全库统计；职责范围只过滤样例，count 为范围内样例数（≤50 条样例上限）。
    issues = []
    for issue in snapshot.get("issues") or []:
        raw_count = int(issue.get("count") or 0)
        if raw_count <= 0:
            continue
        items = list(issue.get("items") or [])
        if scope:
            items = [item for item in items if in_scope(str(item.get("path") or ""), scope)]
            if not items:
                continue
            issue = {**issue, "count": len(items), "vault_count": raw_count}
        issues.append(issue)
    total_issues = len(issues)
    issues = issues[:limit]

    items: List[Candidate] = []
    for issue in issues:
        key = str(issue.get("key") or "")
        samples = [str(item.get("path") or "") for item in (issue.get("items") or [])][:3]
        payload = {
            "issue": key,
            "count": int(issue.get("count") or 0),
            "samples": sorted(path for path in samples if path),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        items.append(
            Candidate(
                path="",
                title=str(issue.get("label") or key),
                mtime=float(now),
                size=0,
                digest=digest,
                excerpt=f"{int(issue.get('count') or 0)} 项",
                facts={
                    "issue": key,
                    "label": issue.get("label"),
                    "severity": issue.get("severity"),
                    "count": int(issue.get("count") or 0),
                    "samples": samples,
                },
            )
        )

    return CandidateSet(
        recipe="weekly_health",
        items=items,
        summary={
            "score": snapshot.get("score"),
            "grade": snapshot.get("grade"),
            "deep": False,
            "total": snapshot.get("total"),
            "issues": [
                {
                    "key": item.facts.get("issue"),
                    "label": item.facts.get("label"),
                    "severity": item.facts.get("severity"),
                    "count": item.facts.get("count"),
                    "samples": item.facts.get("samples"),
                }
                for item in items
            ],
            "rule_only": True,
        },
        truncated=total_issues > limit,
    )


def collect_candidates(
    recipe: str,
    reader: DutyReader,
    definition: Mapping[str, Any],
    *,
    now: float,
    dirs: Optional[Mapping[str, str]] = None,
    tz: Any = None,
) -> CandidateSet:
    """按职责分发只读收集；未知 recipe 抛 ``DutyRecipeError``。"""
    spec = recipe_for(recipe)
    folders = dict(dirs or {})
    if spec.id == "inbox_duty":
        return collect_inbox(reader, definition, now=now, dirs=folders)
    if spec.id == "daily_brief":
        return collect_daily(reader, definition, now=now, dirs=folders, tz=tz)
    if spec.id == "weekly_health":
        return collect_weekly(reader, definition, now=now, dirs=folders)
    raise ValueError(f"未实现的职责收集器: {spec.id}")


def date_label(now: float, tz: Any = None) -> str:
    """本地日期（daily 输出文件名用）。"""
    return datetime.fromtimestamp(float(now), tz).strftime("%Y-%m-%d")
