"""视图定义持久化：``<vault>/<views_dir>/<slug>.base``（YAML）。

为什么存 vault 内而不是 ``.kwey/``：
视图是用户的资产（和笔记一样需要被 git 同步、被别的机器看到），
``.kwey/`` 只放索引/凭据这类本机私有物。

文件名用「名字派生 slug」而不是随机 id，用户能在文件管理器里看懂；
改名时保留原文件名（不破坏外部引用），显式重命名才换文件。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.views.query import normalize_definition

DEFAULT_VIEWS_DIR = "views"
_BAD_SLUG = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
MANIFEST_KEY = "kwey"


class ViewStore:
    def __init__(self, root: Path, dirname: str = DEFAULT_VIEWS_DIR) -> None:
        self.root = Path(root)
        self.dirname = (dirname or DEFAULT_VIEWS_DIR).strip().strip("/") or DEFAULT_VIEWS_DIR

    # ------------------------------------------------------------------ paths
    @property
    def views_dir(self) -> Path:
        return self.root / self.dirname

    def path_for(self, slug: str) -> Path:
        slug = _safe_slug(slug)
        return self.views_dir / f"{slug}.base"

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ------------------------------------------------------------------ read
    def list(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.views_dir.is_dir():
            return out
        for path in sorted(self.views_dir.glob("*.base")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(raw, dict):
                continue
            try:
                defn = _from_file(raw)
            except Exception:  # noqa: BLE001 —— 单个坏文件不影响其它视图
                continue
            out.append({
                "slug": path.stem,
                "path": self._rel(path),
                "definition": defn,
            })
        out.sort(key=lambda item: item["definition"].get("name") or item["slug"])
        return out

    def read(self, slug: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(slug)
        if not path.is_file():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return None
        return _from_file(raw)

    def exists(self, slug: str) -> bool:
        return self.path_for(slug).is_file()

    # ------------------------------------------------------------------ write
    def save(self, definition: Dict[str, Any], slug: Optional[str] = None) -> Dict[str, Any]:
        """创建或更新。slug 为空时按名字生成（重名追加序号）。"""
        defn = normalize_definition(definition)
        if slug:
            slug = _safe_slug(slug)
            if not slug:
                raise ValueError("非法的视图标识")
        else:
            slug = self._unique_slug(_slugify(defn["name"]))
        path = self.path_for(slug)
        self.views_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(defn)
        payload[MANIFEST_KEY] = True
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=10 ** 6)
        tmp = path.with_suffix(".base.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return {"slug": slug, "path": self._rel(path), "definition": defn}

    def delete(self, slug: str) -> bool:
        path = self.path_for(slug)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def rename(self, slug: str, new_slug: str) -> Dict[str, Any]:
        old = self.path_for(slug)
        if not old.is_file():
            raise FileNotFoundError(slug)
        target = self.path_for(new_slug)
        if target.exists():
            raise FileExistsError(new_slug)
        old.replace(target)
        return {"slug": target.stem, "path": self._rel(target)}

    def _unique_slug(self, base: str) -> str:
        base = base or "view"
        candidate, index = base, 2
        while self.path_for(candidate).exists():
            candidate = f"{base}-{index}"
            index += 1
        return candidate


def _safe_slug(slug: str) -> str:
    text = _BAD_SLUG.sub("", str(slug or "").strip())
    return text.replace("..", "").strip(" .-")[:80]


def _slugify(name: str) -> str:
    text = _BAD_SLUG.sub("", str(name or "").strip())
    text = re.sub(r"\s+", "-", text)
    return _safe_slug(text) or "view"


def _from_file(raw: Dict[str, Any]) -> Dict[str, Any]:
    """读文件 → 定义。非本产品写出的文件先按 Obsidian Bases 尽力转换。"""
    if raw.get(MANIFEST_KEY) is True:
        return normalize_definition(raw)
    converted = obsidian_base_to_definition(raw)
    if converted is not None:
        return normalize_definition(converted)
    return normalize_definition(raw)


# ---------------------------------------------------------------- obsidian compat
_HAS_TAG_RE = re.compile(r"file\.hasTag\(\s*[\"']([^\"']+)[\"']\s*\)")
_IN_FOLDER_RE = re.compile(r"file\.inFolder\(\s*[\"']([^\"']+)[\"']\s*\)")
_CMP_RE = re.compile(
    r"^\(?\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\s*(==|!=|>=|<=|>|<)\s*"
    r"([\"'][^\"']*[\"']|true|false|-?\d+(?:\.\d+)?)\s*\)?$"
)


def obsidian_base_to_definition(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Obsidian Bases（2025+）→ Kwey 定义。认不出来返回 None（交给通用解析）。

    仅覆盖社区最常用的子集：filters(and) + views[0] 的 type/name/order/sort/limit。
    表达式里的函数只认 hasTag / inFolder，其余条件忽略而不是报错——
    宁可少筛一点，也不要打不开别人的视图。
    """
    if not isinstance(raw, dict):
        return None
    views = raw.get("views")
    if not isinstance(views, list) or not views:
        return None
    first = views[0] if isinstance(views[0], dict) else {}
    out: Dict[str, Any] = {
        "name": first.get("name") or raw.get("name") or "导入的视图",
        "mode": {"table": "table", "cards": "cards", "list": "list"}.get(
            str(first.get("type") or "table").lower(), "table"
        ),
        "columns": first.get("order") or [],
        "limit": first.get("limit") or 200,
    }

    raw_sort = first.get("sort")
    sort: List[Dict[str, str]] = []
    if isinstance(raw_sort, list):
        for item in raw_sort:
            if isinstance(item, dict):
                key = item.get("property") or item.get("key") or ""
                direction = str(item.get("direction") or "ASC").lower()
                if key:
                    sort.append({"key": key, "dir": "desc" if direction.startswith("desc") else "asc"})
            elif isinstance(item, str):
                sort.append({"key": item, "dir": "asc"})
    elif isinstance(raw_sort, dict):
        key = raw_sort.get("property") or raw_sort.get("key") or ""
        if key:
            sort.append({"key": key, "dir": "asc"})
    out["sort"] = sort

    filters = raw.get("filters")
    exprs: List[str] = []
    if isinstance(filters, str):
        exprs = [filters]
    elif isinstance(filters, list):
        exprs = [e for e in filters if isinstance(e, str)]
    elif isinstance(filters, dict):
        for branch in ("and", "or"):
            values = filters.get(branch)
            if isinstance(values, list):
                exprs.extend(e for e in values if isinstance(e, str))
                break
        if not exprs and isinstance(filters.get("and"), str):
            exprs = [filters["and"]]

    tags: List[str] = []
    any_tags: List[str] = []
    folder = ""
    conditions: List[Dict[str, Any]] = []
    for expr in exprs:
        for tag in _HAS_TAG_RE.findall(expr):
            tags.append(tag.lstrip("#"))
        for name in _IN_FOLDER_RE.findall(expr):
            folder = name.strip("/")
        match = _CMP_RE.match(expr.strip())
        if match:
            key, op, value = match.groups()
            if value[0] in "\"'":
                value = value[1:-1]
            conditions.append({"key": key, "op": op, "value": value})
    if tags:
        out["tags"] = tags
    if any_tags:
        out["any_tags"] = any_tags
    if folder:
        out["folder"] = folder
    if conditions:
        out["where"] = conditions
    return out
