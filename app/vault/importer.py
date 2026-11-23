"""Markdown / Obsidian 库导入（P0）：扫描预览 → 落盘，全程只读源库。

设计取舍
--------
1. **保留原目录结构**。附件（``![[img.png]]`` 与 ``![](./x/img.png)``）都不改路径，
   因此不需要重写任何链接——这是导入大库最稳的策略；集中到 ``attachments/``
   会让同名附件与全库唯一名引用产生歧义。
2. **扫描与落盘分离**：``scan()`` 只读，返回统计/冲突/断链样例；``apply()`` 才写。
   前端先展示预览，用户确认后再调 apply。
3. **写盘一律走 VaultService**（统一写路径 → 原子写 → 索引 → 事件）。
   批量期间用 ``vault.bulk_write()`` 抑制逐篇 git 快照，收尾只提交一条。
4. **冲突策略**：``rename``（默认，自动加序号）/ ``skip`` / ``overwrite``。
5. **安全**：源目录必须是绝对路径、存在、是目录，且不能是当前 vault 或位于其内部
   （防自导入递归）；目标路径全部经 ``AppPaths.to_abs`` 校验，浏览器传什么都不会越界。
"""

from __future__ import annotations

import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from app.parser.frontmatter import parse_frontmatter
from app.parser.wikilink import extract_wikilinks
from app.paths import AppPaths

MARKDOWN_EXTS = {".md", ".markdown"}
ATTACHMENT_EXTS = {
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif",
    # 文档
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".canvas", ".excalidraw",
    # 音视频
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".webm", ".mov",
    # 压缩包
    ".zip", ".7z", ".tar", ".gz",
}
SKIP_DIRS = {
    ".obsidian", ".trash", ".git", ".kwey", ".idea", ".vscode",
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "release",
}
CONFLICT_POLICIES = ("rename", "skip", "overwrite")
LINK_SCAN_LIMIT = 3000     # 断链检测最多读这么多篇，避免超大库卡住扫描
MAX_FILES = 50000          # 单次导入文件数上限（防止误选整个磁盘）
MAX_SAMPLES = 30


def _iter_files(root: Path) -> Iterator[Tuple[Path, str]]:
    """遍历源库：剪枝隐藏/保留目录，返回 (绝对路径, 相对正斜杠路径)。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()
            count += 1
            if count >= MAX_FILES:
                return


class VaultImporter:
    """把一个外部 Markdown 库导入当前 vault。"""

    def __init__(self, vault) -> None:
        self.vault = vault

    @property
    def root(self) -> Path:
        return Path(self.vault.paths.root)

    # ------------------------------------------------------------------ 校验
    def _validate_source(self, source: str) -> Path:
        raw = (source or "").strip().strip('"')
        if not raw:
            raise ValueError("请填写要导入的库目录")
        path = Path(raw).expanduser()
        if not path.exists():
            raise ValueError(f"目录不存在：{raw}")
        if not path.is_dir():
            raise ValueError("请选择文件夹，而不是单个文件")
        path = path.resolve()
        root = self.root.resolve()
        if path == root:
            raise ValueError("源目录就是当前 vault，无需导入")
        if root in path.parents:
            raise ValueError("源目录位于当前 vault 内部，请选择其他目录")
        return path

    @staticmethod
    def _check_policy(conflict: str) -> str:
        policy = (conflict or "rename").strip().lower()
        if policy not in CONFLICT_POLICIES:
            raise ValueError(f"冲突策略仅支持 {'/'.join(CONFLICT_POLICIES)}")
        return policy

    def _target_exists(self, rel: str) -> bool:
        try:
            return self.vault.paths.to_abs(rel).exists()
        except Exception:  # noqa: BLE001 —— 非法路径按"不存在"处理，写入时自会报错
            return False

    def _resolve_target(self, rel: str, conflict: str) -> Tuple[Optional[str], str]:
        """→ (目标相对路径 or None, 动作 create/overwrite/rename/skip)。"""
        if not self._target_exists(rel):
            return rel, "create"
        if conflict == "skip":
            return None, "skip"
        if conflict == "overwrite":
            return rel, "overwrite"
        return (
            AppPaths.unique_path(rel, exists_fn=self._target_exists),
            "rename",
        )

    # ------------------------------------------------------------------ 扫描
    def scan(
        self,
        source: str,
        *,
        include_attachments: bool = True,
        conflict: str = "rename",
    ) -> Dict[str, Any]:
        """只读预览：文件数、体积、冲突、断链、重名，不写任何东西。"""
        root = self._validate_source(source)
        policy = self._check_policy(conflict)

        notes: List[Tuple[str, int]] = []
        atts: List[Tuple[str, int]] = []
        skipped_ext: Counter = Counter()
        total_bytes = 0

        for path, rel in _iter_files(root):
            ext = path.suffix.lower()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total_bytes += size
            if ext in MARKDOWN_EXTS:
                notes.append((rel, size))
            elif ext in ATTACHMENT_EXTS:
                if include_attachments:
                    atts.append((rel, size))
            else:
                skipped_ext[ext or "(无扩展名)"] += 1

        conflicts = [rel for rel, _ in notes + atts if self._target_exists(rel)]

        # 断链检测：源库内的 wikilink 目标是否存在（只看文件名级，够用且快）
        names = {Path(rel).stem.lower() for rel, _ in notes}
        names |= {Path(rel).name.lower() for rel, _ in atts}
        broken: List[Dict[str, str]] = []
        links_total = 0
        for rel, _ in notes[:LINK_SCAN_LIMIT]:
            try:
                raw = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _, body = parse_frontmatter(raw)
            for link in extract_wikilinks(body):
                links_total += 1
                target = (link.target or "").split("#", 1)[0].strip()
                if not target or "://" in target:
                    continue
                key = target.lower()
                if key in names or Path(target).stem.lower() in names:
                    continue
                if len(broken) < MAX_SAMPLES:
                    broken.append({"path": rel, "target": link.target})

        dup_counter = Counter(Path(rel).stem.strip().lower() for rel, _ in notes)
        duplicates = [name for name, n in dup_counter.items() if n > 1 and name]

        return {
            "source": str(root),
            "conflict": policy,
            "notes": len(notes),
            "attachments": len(atts),
            "total_bytes": total_bytes,
            "conflicts": len(conflicts),
            "conflict_samples": conflicts[:MAX_SAMPLES],
            "samples": [
                {"path": rel, "size": size, "kind": "note"}
                for rel, size in notes[:MAX_SAMPLES]
            ],
            "skipped_ext": dict(skipped_ext.most_common(10)),
            "skipped_files": sum(skipped_ext.values()),
            "links_total": links_total,
            "links_broken": len(broken),
            "broken_samples": broken,
            "duplicate_names": duplicates[:MAX_SAMPLES],
            "link_scan_limited": len(notes) > LINK_SCAN_LIMIT,
            "attachments_included": include_attachments,
        }

    # ------------------------------------------------------------------ 落盘
    def apply(
        self,
        source: str,
        *,
        include_attachments: bool = True,
        conflict: str = "rename",
    ) -> Dict[str, Any]:
        """执行导入；单篇失败记入 ``failed`` 并继续，不中断整批。"""
        root = self._validate_source(source)
        policy = self._check_policy(conflict)

        notes_created = notes_overwritten = notes_renamed = notes_skipped = 0
        atts_copied = atts_renamed = atts_skipped = 0
        failed: List[Dict[str, str]] = []

        with self.vault.bulk_write():
            for path, rel in _iter_files(root):
                ext = path.suffix.lower()
                is_note = ext in MARKDOWN_EXTS
                is_att = ext in ATTACHMENT_EXTS
                if not is_note and not (is_att and include_attachments):
                    continue
                try:
                    target, action = self._resolve_target(rel, policy)
                    if action == "skip":
                        if is_note:
                            notes_skipped += 1
                        else:
                            atts_skipped += 1
                        continue
                    if is_note:
                        raw = path.read_text(encoding="utf-8", errors="replace")
                        frontmatter, body = parse_frontmatter(raw)
                        if action == "overwrite":
                            self.vault.write_note(target, body, frontmatter)
                            notes_overwritten += 1
                        else:
                            self.vault.create_note(target, body, frontmatter)
                            if action == "rename":
                                notes_renamed += 1
                            else:
                                notes_created += 1
                    else:
                        dest = self.vault.paths.to_abs(target)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dest)
                        if action == "rename":
                            atts_renamed += 1
                        else:
                            atts_copied += 1
                except Exception as exc:  # noqa: BLE001 —— 单文件失败不中断整批
                    failed.append({"path": rel, "error": str(exc)})

        # 批量收尾：整批一条快照（bulk_write 已抑制逐篇提交）
        try:
            self.vault._auto_snapshot("import", "批量导入")
        except Exception:  # noqa: BLE001 —— 快照失败不影响导入结果
            pass

        return {
            "source": str(root),
            "conflict": policy,
            "notes_created": notes_created,
            "notes_overwritten": notes_overwritten,
            "notes_renamed": notes_renamed,
            "notes_skipped": notes_skipped,
            "attachments_copied": atts_copied,
            "attachments_renamed": atts_renamed,
            "attachments_skipped": atts_skipped,
            "failed": failed[:MAX_SAMPLES],
            "failed_total": len(failed),
        }
