"""模板路由：列表 / 读取内容 / 套用占位符生成笔记。

模板目录（settings.template_dir，默认 templates/）已在 IGNORED_DIRS 内，
validate_relative 会拒绝该片段，故一律以 root / rel 直读，并自行做逐段安全校验。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.models import TemplateApplyRequest
from app.parser.frontmatter import parse_frontmatter
from app.paths import ILLEGAL_FILENAME_CHARS, AppPaths, normalize_rel
from app.vault.provenance import LOCAL_UI_ACTOR

router = APIRouter(prefix="/api/templates", tags=["templates"])

TEMPLATE_EXTS = (".md", ".markdown")


def _check_rel(rel: str) -> None:
    """逐段安全校验（不复用 validate_relative：它拒绝 templates 片段）。"""
    if not rel:
        raise HTTPException(status_code=400, detail="模板路径为空")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise HTTPException(status_code=400, detail=f"非法路径片段: {part!r}")
        if ":" in part or any(c in ILLEGAL_FILENAME_CHARS for c in part):
            raise HTTPException(status_code=400, detail=f"路径含非法字符: {part!r}")
        if re.search(r"[\x00-\x1f]", part):
            raise HTTPException(status_code=400, detail="路径含控制字符")


def _template_dir() -> str:
    """从 settings 读模板目录（vault 相对，规范化 + 片段安全校验）。"""
    config = deps.require_config()
    rel = normalize_rel(str(config.get("template_dir") or "templates"))
    _check_rel(rel)
    return rel


def _template_abs(rel: str) -> Path:
    """模板相对路径 → 绝对路径，且必须落在模板目录内（防越界读任意文件）。"""
    _check_rel(rel)
    root = deps.require_vault().paths.ensure_root()
    base = _template_dir()
    if not (rel == base or rel.startswith(base + "/")):
        raise HTTPException(status_code=400, detail="路径不在模板目录内")
    abs_path = (root / rel).resolve()
    base_abs = (root / base).resolve()
    try:
        abs_path.relative_to(base_abs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="路径越出模板目录") from exc
    return abs_path


def _read_template(rel: str) -> Tuple[Dict[str, Any], str]:
    """读取模板 → (frontmatter, 正文)。"""
    abs_path = _template_abs(rel)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"模板不存在: {rel}")
    raw = abs_path.read_text(encoding="utf-8-sig")
    frontmatter, body = parse_frontmatter(raw)
    return frontmatter, body


@router.get("")
async def list_templates():
    """列出模板目录下 .md/.markdown；目录不存在返回空数组，不报错。"""
    vault = deps.require_vault()
    root = vault.paths.ensure_root()
    base = _template_dir()
    abs_dir = root / base
    items: List[dict] = []
    if abs_dir.is_dir():
        for p in sorted(abs_dir.rglob("*"), key=lambda x: x.as_posix().lower()):
            if not p.is_file() or p.suffix.lower() not in TEMPLATE_EXTS:
                continue
            rel = p.relative_to(root).as_posix()
            items.append({"name": p.stem, "path": rel})
    return {"code": 0, "data": items, "message": "ok"}


@router.get("/content")
async def template_content(path: str):
    frontmatter, body = _read_template(normalize_rel(path))
    return {
        "code": 0,
        "data": {"content": body, "frontmatter": frontmatter},
        "message": "ok",
    }


def _replace_placeholders(text: str, title: str) -> str:
    """替换 {{date}}/{{time}}/{{title}}/{{datetime}} 占位符。"""
    now = datetime.now()
    mapping = {
        "{{date}}": now.strftime("%Y-%m-%d"),
        "{{time}}": now.strftime("%H:%M"),
        "{{title}}": title,
        "{{datetime}}": now.astimezone().isoformat(timespec="seconds"),
    }
    for key, value in mapping.items():
        text = text.replace(key, value)
    return text


def _replace_in_obj(value: Any, title: str) -> Any:
    """递归替换 frontmatter 内字符串值中的占位符。"""
    if isinstance(value, str):
        return _replace_placeholders(value, title)
    if isinstance(value, list):
        return [_replace_in_obj(v, title) for v in value]
    if isinstance(value, dict):
        return {k: _replace_in_obj(v, title) for k, v in value.items()}
    return value


@router.post("/apply")
async def apply_template(req: TemplateApplyRequest):
    """套用模板：替换占位符后走 VaultService.create_note() 统一写路径。"""
    vault = deps.require_vault()
    template_rel = normalize_rel(req.template_path)
    frontmatter, body = _read_template(template_rel)
    template_stem = template_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    # 目标路径：缺省按 YYYY-MM-DD-模板名.md 放 vault 根并去重
    if req.target_path:
        rel = normalize_rel(req.target_path)
        _check_rel(rel)
    else:
        rel = f"{datetime.now().strftime('%Y-%m-%d')}-{template_stem}.md"
    if not rel.lower().endswith(TEMPLATE_EXTS):
        rel += ".md"
    root = vault.paths.ensure_root()
    rel = AppPaths.unique_path(rel, exists_fn=lambda p: (root / p).exists())
    title = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    content = _replace_placeholders(body, title)
    fm = _replace_in_obj(frontmatter, title)
    meta = vault.create_note(
        rel, content=content, frontmatter=fm, dedup=False, actor=LOCAL_UI_ACTOR
    )
    return {
        "code": 0,
        "data": {"path": meta["path"], "title": meta.get("title") or title},
        "message": "ok",
    }
