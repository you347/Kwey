"""路径工具（Windows 中文路径/非法字符集中处理）。

约定（全项目最重要约定，见 ARCHITECTURE.md §8.2）：
- API 与模块间传递的 ``path`` 一律为 **vault 相对路径、正斜杠、不含 vault 根**，
  例如 ``notes/读书/2026计划.md``。
- 绝对路径只在 AppPaths 内部转换，任何模块不得手工拼 ``\\``。
- 全部文件 I/O 显式 UTF-8（读取容忍 BOM）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

# Windows 保留设备名与非法字符
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
ILLEGAL_FILENAME_CHARS = '<>:"|?*'
# templates/ 为 P1 模板目录：不作为普通笔记索引、不出现在文件树，
# 其内容经 /api/templates 专用 API 直读（validate_relative 会拒绝该片段）。
IGNORED_DIRS = {".kwey", ".trash", ".git", "templates"}


class PathError(ValueError):
    """路径非法（含 ..、盘符、保留名、控制字符、非法字符等）。"""


def normalize_rel(path: Optional[str]) -> str:
    """任意输入规范化为 vault 相对正斜杠路径（不做安全校验）。"""
    p = (path or "").strip().replace("\\", "/")
    p = re.sub(r"/+", "/", p)
    return p.strip("/")


def sanitize_filename(name: str) -> str:
    """过滤文件名非法字符（< > : " | ? *）、控制字符、结尾点/空格与保留名。"""
    name = (name or "").strip().strip("/")
    for ch in ILLEGAL_FILENAME_CHARS:
        name = name.replace(ch, "_")
    name = re.sub(r"[\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")  # Windows 不允许结尾点/空格
    if not name:
        name = "untitled"
    stem = name.rsplit(".", 1)[0]
    if stem.upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def validate_relative(path: Optional[str]) -> str:
    """校验并规范化 vault 相对路径；非法抛 PathError。

    B2 修复：任一路径片段命中系统保留目录（.kwey/.trash/.git）直接拒绝，
    杜绝经 API 向隐藏目录写笔记/读文件。内部对 .trash/ 的合法移动
    （delete_note/delete_dir）不经此函数，不受影响。
    """
    rel = normalize_rel(path)
    if not rel:
        raise PathError("路径为空")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise PathError(f"非法路径片段: {part!r}")
        if part in IGNORED_DIRS:
            raise PathError(f"系统保留目录不可读写: {part!r}")
        if part.endswith(".kwey-tmp"):
            raise PathError(f"临时文件路径不可读写: {part!r}")
        if ":" in part:  # 盘符 / 流分隔
            raise PathError(f"路径含非法字符: {part!r}")
        if any(c in ILLEGAL_FILENAME_CHARS for c in part):
            raise PathError(f"路径含非法字符: {part!r}")
        if re.search(r"[\x00-\x1f]", part):
            raise PathError("路径含控制字符")
        if part.rsplit(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise PathError(f"Windows 保留名: {part!r}")
    return rel


def is_temp_path(rel: Optional[str]) -> bool:
    """是否为原子写临时文件或系统保留目录路径（B5：事件广播层过滤杂音）。"""
    rel = normalize_rel(rel)
    return rel.endswith(".kwey-tmp") or is_ignored(rel)


def is_ignored(rel: Optional[str]) -> bool:
    """是否属于忽略目录（.kwey / .trash / .git）——索引与文件树均跳过。"""
    rel = normalize_rel(rel)
    return any(part in IGNORED_DIRS for part in rel.split("/"))


class AppPaths:
    """持有 vault 根目录，负责相对/绝对路径互转与唯一化命名。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root: Optional[Path] = Path(root) if root else None

    # ------------------------------------------------------------------ root
    @property
    def root(self) -> Optional[Path]:
        return self._root

    @property
    def name(self) -> str:
        return self._root.name if self._root else ""

    def set_root(self, root: Path) -> None:
        self._root = Path(root)

    def ensure_root(self) -> Path:
        if self._root is None:
            raise PathError("尚未打开 vault")
        return self._root

    # ------------------------------------------------------------------ convert
    def to_abs(self, rel: str) -> Path:
        """vault 相对路径 → 绝对 Path（先做安全校验，防目录穿越）。"""
        root = self.ensure_root()
        clean = validate_relative(rel)
        return root.joinpath(*clean.split("/"))

    def to_rel(self, abs_path: Path) -> str:
        """绝对 Path → vault 相对正斜杠路径；越界抛 PathError。"""
        root = self.ensure_root()
        try:
            resolved = Path(abs_path).resolve()
            rel = resolved.relative_to(root.resolve())
        except (ValueError, OSError) as exc:
            raise PathError(f"路径不在 vault 内: {abs_path}") from exc
        return normalize_rel(str(rel))

    # ------------------------------------------------------------------ naming
    @staticmethod
    def unique_path(rel: str, exists_fn=None) -> str:
        """重名自动追加序号：`笔记.md` → `笔记 (1).md`。"""
        rel = normalize_rel(rel)
        if exists_fn is None:
            exists_fn = lambda p: Path(p).exists()  # noqa: E731
        if not exists_fn(rel):
            return rel
        stem, dot, ext = rel.rpartition(".")
        if not dot:
            stem, ext = rel, ""
        base = stem
        counter = 1
        while True:
            candidate = f"{base} ({counter})" + (f".{ext}" if ext else "")
            if not exists_fn(candidate):
                return candidate
            counter += 1

    # ------------------------------------------------------------------ tree
    def list_dir(self, rel: str = "") -> List[Path]:
        """列出目录内容（不递归），供文件树构建。"""
        root = self.ensure_root()
        target = root.joinpath(*rel.split("/")) if rel else root
        if not target.is_dir():
            return []
        return sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
