"""知识体检路由：扫描（只读）+ 安全修复（走 VaultService 统一写路径）。

修复动作只有三种，都刻意保守：
- ``trash``：空笔记移入回收站（可还原，不是真删）
- ``strip_broken``：把断链降级为纯文本，保留可读内容
- ``tag``：给一批笔记补同一个标签
批量改正文/批量移动这类「看起来聪明」的动作不在这里做——那属于 Agent 的 diff 确认流程。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.diagnose import scan, strip_broken_links

router = APIRouter(prefix="/api/diagnose", tags=["diagnose"])


def _config() -> Dict[str, Any]:
    cfg = deps.get_config()
    return dict((cfg.get("diagnose") if cfg is not None else {}) or {})


@router.get("")
async def run_scan(deep: bool = True):
    vault = deps.require_vault()
    db = deps.require_db()
    data = scan(Path(vault.paths.root), db, _config(), deep=deep)
    return {"code": 0, "data": data, "message": "ok"}


class FixRequest(BaseModel):
    action: str
    paths: List[str] = []
    tag: Optional[str] = None


@router.post("/fix")
async def fix(req: FixRequest):
    vault = deps.require_vault()
    db = deps.require_db()
    action = (req.action or "").strip()
    paths = [p for p in (req.paths or []) if p][:500]
    if not paths:
        raise HTTPException(status_code=400, detail="没有选中要修复的笔记")
    if action not in {"trash", "strip_broken", "tag"}:
        raise HTTPException(status_code=400, detail=f"不支持的修复动作: {action}")

    fixed: List[str] = []
    errors: List[Dict[str, str]] = []

    if action == "trash":
        for path in paths:
            try:
                vault.delete_note(path)
                fixed.append(path)
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": path, "error": str(exc)})

    elif action == "strip_broken":
        unresolved: Dict[str, List[str]] = {}
        for row in db.unresolved_links():
            unresolved.setdefault(row["source_path"], []).append(row["target"])
        for path in paths:
            targets = unresolved.get(path) or []
            if not targets:
                continue
            try:
                changed, _content = strip_broken_links(vault, path, targets)
                if changed:
                    fixed.append(path)
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": path, "error": str(exc)})

    else:  # tag
        tag = (req.tag or "").strip().lstrip("#")
        if not tag:
            raise HTTPException(status_code=400, detail="请填写要补的标签")
        for path in paths:
            try:
                note = vault.read_note(path)
                fm = dict(note.get("frontmatter") or {})
                tags = fm.get("tags")
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
                elif not isinstance(tags, list):
                    tags = []
                if tag in tags:
                    continue
                tags.append(tag)
                fm["tags"] = tags
                vault.write_note(path, content=note["content"], frontmatter=fm)
                fixed.append(path)
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": path, "error": str(exc)})

    return {
        "code": 0,
        "data": {"action": action, "fixed": len(fixed), "paths": fixed[:100], "errors": errors[:50]},
        "message": "ok",
    }
