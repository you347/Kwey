"""职责只读访问层：把索引 / VaultService 的读能力收敛成注入接口。

runtime 只依赖本模块的 :class:`DutyReader` 协议，**不读全局 deps、不写盘**。
生产接线由后续集成批次把 ``deps.require_vault()`` 与 ``deps.require_db()``
传入 :class:`VaultDutyReader`；测试可直接用 conftest 的临时 vault。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.diagnose import scan as diagnose_scan
from app.paths import IGNORED_DIRS, PathError, normalize_rel, validate_relative


@runtime_checkable
class DutyReader(Protocol):
    """职责所需的**只读**能力；任何实现都不得写盘。"""

    root: Path

    def list_notes(self, prefix: str = "") -> List[Dict[str, Any]]: ...

    def read_raw(self, rel: str) -> Optional[str]: ...

    def read_note(self, rel: str) -> Optional[Dict[str, Any]]: ...

    def tags_of(self, rel: str) -> List[str]: ...

    def list_dirs(self, limit: int = 80) -> List[str]: ...

    def health_snapshot(
        self, config: Optional[Dict[str, Any]] = None, *, deep: bool = False
    ) -> Dict[str, Any]: ...


class VaultDutyReader:
    """用已打开的 VaultService + IndexDatabase 实现只读协议（不启动 watcher）。"""

    def __init__(self, vault: Any, db: Any) -> None:
        root = getattr(getattr(vault, "paths", None), "root", None)
        if root is None:
            raise ValueError("VaultDutyReader 需要已打开且带 paths.root 的 vault")
        self.root = Path(root)
        self._vault = vault
        self._db = db

    # ------------------------------------------------------------------ notes
    def list_notes(self, prefix: str = "") -> List[Dict[str, Any]]:
        clean = normalize_rel(prefix)
        notes = list(self._db.all_notes())
        if clean:
            notes = [
                note
                for note in notes
                if note["path"] == clean or note["path"].startswith(clean + "/")
            ]
        notes.sort(key=lambda note: (float(note.get("mtime") or 0), note["path"]), reverse=True)
        return notes

    def read_raw(self, rel: str) -> Optional[str]:
        try:
            clean = validate_relative(rel)
        except PathError:
            return None
        target = self.root.joinpath(*clean.split("/"))
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None

    def read_note(self, rel: str) -> Optional[Dict[str, Any]]:
        try:
            return self._vault.read_note(rel)
        except Exception:  # noqa: BLE001 —— HTTPException(404) / 路径非法一律视为不可读
            return None

    def tags_of(self, rel: str) -> List[str]:
        try:
            return list(self._db.tags_of_note(rel))
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ dirs
    def list_dirs(self, limit: int = 80) -> List[str]:
        """列出 vault 内 1~2 层现有目录，供 move 目标白名单。

        跳过隐藏目录、保留目录与附件目录；不跟随符号链接。
        """
        result: List[str] = []
        root = self.root
        if not root.is_dir():
            return result
        for current, dirs, _files in os.walk(root, followlinks=False):
            rel = Path(current).relative_to(root).as_posix()
            rel = "" if rel == "." else rel
            depth = rel.count("/") + 1 if rel else 0
            if depth >= 2:
                dirs[:] = []
                continue
            keep: List[str] = []
            for name in dirs:
                if name.startswith(".") or name in IGNORED_DIRS or name == "attachments":
                    continue
                keep.append(name)
                child = normalize_rel(f"{rel}/{name}" if rel else name)
                if child and child not in result:
                    result.append(child)
                if len(result) >= limit:
                    break
            dirs[:] = keep
            if len(result) >= limit:
                break
        result.sort()
        return result

    # ------------------------------------------------------------------ health
    def health_snapshot(
        self, config: Optional[Dict[str, Any]] = None, *, deep: bool = False
    ) -> Dict[str, Any]:
        return diagnose_scan(self.root, self._db, config, deep=deep)
