"""快速捕获 + AI 收件箱。

设计意图：知识系统的入口必须比「新建笔记 → 想标题 → 选目录」更轻，
所以捕获只问一句话；整理则延后——收件箱里的东西由 AI 给建议，人只做勾选。

落盘路径：``<vault>/<inbox_dir>/*.md``（默认 ``inbox/``），普通 Markdown，
没有隐藏数据库；不整理也不会丢。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.provider import AgentProvider, ProviderError, chat_json
from app.api import deps
from app.paths import normalize_rel

router = APIRouter(prefix="/api", tags=["inbox"])

_TITLE_BAD = re.compile(r"[\\/:*?\"<>|\r\n\t]+")
MAX_SUGGEST = 40
AI_TIMEOUT = 90.0


def _inbox_dir() -> str:
    cfg = deps.get_config()
    data = (cfg.get("inbox") if cfg is not None else None) or {}
    return str(data.get("dir") or "inbox").strip("/") or "inbox"


def _clean_title(text: str, limit: int = 40) -> str:
    text = _TITLE_BAD.sub(" ", str(text or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip(" .-") or "未命名"


def _first_line_title(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return _clean_title(cleaned)
    return "未命名"


# --------------------------------------------------------------------- 捕获
class CaptureRequest(BaseModel):
    text: str
    title: Optional[str] = None
    tags: List[str] = []


@router.post("/capture")
async def capture(req: CaptureRequest):
    vault = deps.require_vault()
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容为空，没什么可存的")
    if len(text) > 50000:
        raise HTTPException(status_code=400, detail="内容过长（上限 50000 字）")

    title = _clean_title(req.title) if (req.title or "").strip() else _first_line_title(text)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    folder = _inbox_dir()
    rel = f"{folder}/{stamp} {title}.md"

    tags = [str(t).strip().lstrip("#") for t in (req.tags or []) if str(t).strip()][:10]
    frontmatter: Dict[str, Any] = {
        "captured": True,
        "source": "quick-capture",
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if tags:
        frontmatter["tags"] = tags

    meta = vault.create_note(rel, content=text + ("\n" if not text.endswith("\n") else ""),
                             frontmatter=frontmatter, dedup=True)
    return {"code": 0, "data": {"path": meta["path"], "title": meta.get("title") or title}, "message": "ok"}


# --------------------------------------------------------------------- 收件箱
@router.get("/inbox")
async def list_inbox():
    vault = deps.require_vault()
    db = deps.require_db()
    folder = _inbox_dir()
    prefix = folder + "/"
    notes = [
        note for note in db.all_notes()
        if note["path"].startswith(prefix)
    ]
    notes.sort(key=lambda n: -(n.get("mtime") or 0))
    items = []
    for note in notes:
        path = note["path"]
        items.append({
            "path": path,
            "title": note.get("title") or path.rsplit("/", 1)[-1],
            "excerpt": note.get("excerpt") or "",
            "mtime": note.get("mtime"),
            "tags": db.tags_of_note(path),
        })
    return {"code": 0, "data": {"dir": folder, "items": items, "total": len(items)}, "message": "ok"}


# --------------------------------------------------------------------- 建议
class SuggestRequest(BaseModel):
    paths: Optional[List[str]] = None


_SUGGEST_SYSTEM = """你是「Kwey 收件箱整理助手」。用户把随手记的东西扔进收件箱，\
你要为每一条建议：放进哪个目录、标题改成什么、打什么标签。

只输出 JSON，不要解释、不要 Markdown 围栏：
{"items":[{"path":"原路径（必须原样返回）","title":"更清楚的标题（不超过 30 字）",
"folder":"目标目录（必须是已有目录之一，或空字符串表示根目录）","tags":["标签"],
"summary":"一句话说明这条笔记讲什么","reason":"为什么这样归置"}]}

规则：
1. path 必须来自输入，不能编造；
2. folder 只能从「现有目录」里选，不要发明新目录；拿不准就留空字符串；
3. 标题要具体（「周五和张三聊定价」优于「笔记1」）；
4. 标签 1~3 个，优先复用已有标签；
5. 输入里每一条都必须有一条输出。"""


def _existing_dirs(root: Path, limit: int = 30) -> List[str]:
    dirs: List[str] = []
    try:
        for entry in sorted(Path(root).iterdir()):
            name = entry.name
            if not entry.is_dir() or name.startswith(".") or name in {"attachments", "__pycache__"}:
                continue
            dirs.append(name)
            if len(dirs) >= limit:
                break
    except OSError:
        pass
    return dirs


def _top_tags(limit: int = 30) -> List[str]:
    try:
        return [tag for tag, _count in deps.require_db().tag_counts()[:limit]]
    except Exception:  # noqa: BLE001
        return []


@router.post("/inbox/suggest")
async def suggest(req: SuggestRequest):
    vault = deps.require_vault()
    db = deps.require_db()
    folder = _inbox_dir()
    prefix = folder + "/"
    notes = [n for n in db.all_notes() if n["path"].startswith(prefix)]
    notes.sort(key=lambda n: -(n.get("mtime") or 0))
    if req.paths:
        wanted = set(req.paths)
        notes = [n for n in notes if n["path"] in wanted]
    notes = notes[:MAX_SUGGEST]
    if not notes:
        return {"code": 0, "data": {"items": [], "ai": False, "message": "收件箱是空的"}, "message": "ok"}

    base = [
        {
            "path": n["path"],
            "filename": n["path"].rsplit("/", 1)[-1],
            "excerpt": (n.get("excerpt") or "")[:300],
            "tags": db.tags_of_note(n["path"]),
        }
        for n in notes
    ]

    provider = AgentProvider(vault.paths.root)
    ai_used = False
    suggestions: List[Dict[str, Any]] = []
    if provider.configured and str(provider.config.get("protocol") or "openai") == "openai":
        import json as _json

        prompt = (
            f"【现有目录】{_existing_dirs(Path(vault.paths.root)) or ['(无子目录)']}\n"
            f"【已有标签】{_top_tags() or ['(无)']}\n"
            f"【收件箱条目】\n{_json.dumps(base, ensure_ascii=False)}"
        )
        try:
            data = chat_json(
                provider,
                [{"role": "system", "content": _SUGGEST_SYSTEM}, {"role": "user", "content": prompt}],
                timeout=AI_TIMEOUT,
            )
            raw_items = data.get("items") or []
            by_path = {n["path"]: n for n in notes}
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if path not in by_path:
                    continue
                suggestions.append({
                    "path": path,
                    "current_title": by_path[path].get("title") or path,
                    "suggestion": {
                        "title": _clean_title(item.get("title") or by_path[path].get("title")),
                        "folder": str(item.get("folder") or "").strip("/"),
                        "tags": [str(t).strip().lstrip("#") for t in (item.get("tags") or []) if str(t).strip()][:5],
                    },
                    "summary": str(item.get("summary") or "")[:200],
                    "reason": str(item.get("reason") or "")[:200],
                })
            ai_used = bool(suggestions)
        except ProviderError as exc:
            suggestions = []
            ai_used = False
            error = str(exc)
        else:
            error = ""
    else:
        error = ""

    if not suggestions:
        # 规则兜底：保留标题、沿用标签、留在收件箱
        suggestions = [
            {
                "path": n["path"],
                "current_title": n.get("title") or n["path"],
                "suggestion": {
                    "title": _clean_title(n.get("title") or n["path"].rsplit("/", 1)[-1]),
                    "folder": "",
                    "tags": db.tags_of_note(n["path"])[:3],
                },
                "summary": (n.get("excerpt") or "")[:120],
                "reason": "未配置 AI，先按原样保留；可手动改标题/目录/标签",
            }
            for n in notes
        ]

    return {
        "code": 0,
        "data": {
            "items": suggestions,
            "ai": ai_used,
            "dirs": _existing_dirs(Path(vault.paths.root)),
            "tags": _top_tags(),
            "message": "" if ai_used else (error or "AI 未配置，已给出规则建议"),
        },
        "message": "ok",
    }


# --------------------------------------------------------------------- 应用
class ApplyItem(BaseModel):
    path: str
    title: Optional[str] = None
    folder: Optional[str] = None
    tags: Optional[List[str]] = None


class ApplyRequest(BaseModel):
    items: List[ApplyItem]


@router.post("/inbox/apply")
async def apply(req: ApplyRequest):
    vault = deps.require_vault()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for item in (req.items or [])[:MAX_SUGGEST]:
        try:
            current = item.path
            if not (Path(vault.paths.root) / current.replace("/", "\\")).is_file():
                raise ValueError("笔记已不存在")

            # 1) 标签写回（路径未变，最安全）
            if item.tags is not None:
                note = vault.read_note(current)
                fm = dict(note.get("frontmatter") or {})
                tags = [str(t).strip().lstrip("#") for t in item.tags if str(t).strip()][:10]
                if tags:
                    fm["tags"] = tags
                else:
                    fm.pop("tags", None)
                vault.write_note(current, content=note["content"], frontmatter=fm)

            # 2) 移动目录（同时重写指向它的链接）
            if item.folder is not None:
                target_dir = normalize_rel(str(item.folder).strip("/")) if str(item.folder).strip("/") else ""
                if target_dir and target_dir != _inbox_dir():
                    moved = vault.move_note(current, target_dir)
                    current = moved["new_path"]

            # 3) 改标题（= 改文件名；从收件箱出来时通常都要）
            if item.title:
                desired = _clean_title(item.title, limit=60)
                existing = current.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if desired and desired != existing:
                    renamed = vault.rename_note(current, desired)
                    current = renamed["new_path"]

            results.append({"from": item.path, "to": current})
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": item.path, "error": str(exc)})

    return {
        "code": 0,
        "data": {"applied": len(results), "results": results, "errors": errors[:20]},
        "message": "ok",
    }
