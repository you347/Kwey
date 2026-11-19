"""git 快照时间线服务（卡片 M2-4）。

零新第三方依赖：通过标准库 ``subprocess`` 调用系统 git（不使用 GitPython）。
所有 git 操作都限定在 ``root``（vault 根目录）下新建的 ``.git`` 仓库内，
绝不触碰项目代码仓库；``.git`` 已由 ``app.paths.IGNORED_DIRS`` 排除在索引/文件树之外。

设计取舍：
- ``commit`` 服务于「写路径自动挂钩」：无改动时返回 ``None``，避免每次写盘
  都产生空提交噪音；git 不可用 / 未初始化时静默降级，绝不阻断主写路径。
- ``rollback`` 独立保证「回滚前必留一条 before rollback 里程碑」：即使工作区
  干净也用 ``--allow-empty`` 落一条提交，确保当前版本不丢失、时间线可追溯。
- ``.gitignore`` 只以「纯追加」方式补缺失条目，绝不覆盖用户既有字节。
- 回滚后通过注入的 VaultService 增量重索引并广播 UPDATED 事件，让内存索引 /
  前端与磁盘保持一致；``git checkout <hash> -- <rel>`` 只作用于指定文件，
  不破坏其它文件。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.events import EventType, VaultEvent
from app.paths import PathError, normalize_rel, validate_relative

# 需要写入 .gitignore 的保留目录 / 临时文件（追加，不覆盖）
GITIGNORE_ENTRIES: Tuple[str, ...] = (".kwey/", ".trash/", "*.kwey-tmp")

# 允许的 git revision 字符：十六进制 hash / HEAD / branch / tag / HEAD~1 等。
# 禁止以 '-' 开头（防被当成命令选项）、禁止 ':' '空格' '..'（防路径穿越 / 注入）。
_REV_RE = re.compile(r"^[A-Za-z0-9._^~@][A-Za-z0-9._^~@/\-]*$")


class SnapshotService:
    """基于系统 git 的快照 / 回滚服务。

    Args:
        root: vault 绝对根目录（``.git`` 建在此目录下）。
        vault: 可选 VaultService，用于回滚后重索引 + 事件广播；也可后续 ``bind_vault`` 注入。
        enabled: 总开关（可由 settings.snapshots.enabled 控制）；关闭后写类操作静默降级。
    """

    def __init__(self, root: Path, vault=None, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self._vault = vault

    # ------------------------------------------------------------------ 底层
    def _run_ok(
        self, args: List[str], ok_codes: Tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess:
        """统一 git 调用；返回码不在 ``ok_codes`` 内则抛 RuntimeError（带 stderr，不吞错）。"""
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode not in ok_codes:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"git {' '.join(args)} 失败 (code={result.returncode}): {stderr}"
            )
        return result

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        """严格模式：任何非 0 返回码都视为错误。"""
        return self._run_ok(args, ok_codes=(0,))

    def _has_head(self) -> bool:
        """是否已有至少一个 commit（空仓库 ``rev-parse --verify HEAD`` 返回 128，属正常）。"""
        if not (self.root / ".git").exists():
            return False
        res = self._run_ok(["rev-parse", "--verify", "HEAD"], ok_codes=(0, 128))
        return res.returncode == 0

    def _check_rev(self, rev: str) -> str:
        """校验 revision，防命令选项注入 / 路径穿越；非法抛 400。"""
        value = (rev or "").strip()
        if (
            not value
            or not _REV_RE.fullmatch(value)
            or ".." in value
            or ":" in value
            or " " in value
        ):
            raise HTTPException(status_code=400, detail=f"非法快照 revision: {rev!r}")
        return value

    @staticmethod
    def _path_arg(rel: str) -> str:
        """宽松路径规范化（``commit`` / ``log`` 的 ``--`` 之后参数）；非法片段抛 400。"""
        value = normalize_rel(rel)
        parts = value.split("/") if value else [""]
        if any(p in ("", ".", "..") for p in parts):
            raise HTTPException(status_code=400, detail=f"非法路径: {rel!r}")
        return value

    def _validate_rel(self, rel: str) -> str:
        """show / rollback 的相对路径：复用项目统一校验，PathError 转 400。"""
        try:
            return validate_relative(rel)
        except PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------ 环境
    def is_available(self) -> bool:
        """系统 git 是否可用（``git --version`` 返回码为 0）。"""
        try:
            result = subprocess.run(
                ["git", "--version"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:  # git 未安装 / root 不存在
            return False
        return result.returncode == 0

    def ensure_repo(self) -> None:
        """初始化本仓库 git + 本地 user 配置 + 补齐 .gitignore（幂等）。"""
        if not self.enabled or not self.is_available():
            return
        if not (self.root / ".git").exists():
            self._run(["init", "-q"])
        # 仅本仓库生效（不加 --global）
        self._run(["config", "user.name", "Kwey"])
        self._run(["config", "user.email", "kwey@local"])
        self._ensure_gitignore()

    def _ensure_gitignore(self) -> None:
        """仅追加缺失条目，纯追加写，绝不改动用户已有字节。"""
        gi = self.root / ".gitignore"
        existing_lines: List[str] = []
        if gi.is_file():
            existing_lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        have = {line.strip() for line in existing_lines}
        missing = [e for e in GITIGNORE_ENTRIES if e not in have]
        if not missing:
            return
        # 原文件非空且不以换行结尾时，先补一个换行再追加，避免两行粘连
        need_sep = False
        if gi.is_file() and gi.stat().st_size > 0:
            with gi.open("rb") as fh:
                fh.seek(-1, 2)
                need_sep = fh.read(1) != b"\n"
        with gi.open("a", encoding="utf-8", newline="\n") as fh:
            if need_sep:
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")

    # ------------------------------------------------------------------ 写
    def commit(self, message: str, rel: Optional[str] = None) -> Optional[str]:
        """``git add -A``（或指定 rel）→ 有改动则提交并返回短 hash；无改动返回 ``None``。

        不加 ``--allow-empty``：写路径每次触发也不产生空提交噪音。
        """
        if not self.enabled or not self.is_available():
            return None
        if not (self.root / ".git").exists():
            return None  # 未初始化：静默降级，绝不阻断主写路径
        if rel:
            self._run(["add", "-A", "--", self._path_arg(rel)])
        else:
            self._run(["add", "-A"])
        diff = self._run_ok(["diff", "--cached", "--quiet"], ok_codes=(0, 1))
        if diff.returncode == 0:
            return None  # 无 staged 改动
        self._run(["commit", "-m", message])
        return self._run(["rev-parse", "--short", "HEAD"]).stdout.strip()

    def _commit_before_rollback(self, message: str) -> str:
        """回滚前留存：无论工作区是否干净都落一条里程碑提交（``--allow-empty``）。"""
        self._run(["add", "-A"])
        self._run(["commit", "--allow-empty", "-m", message])
        return self._run(["rev-parse", "--short", "HEAD"]).stdout.strip()

    # ------------------------------------------------------------------ 读
    def log(self, rel: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """时间线（最新在前）；每项 ``{hash, short, ts, message, author}``。"""
        if not self.enabled or not self.is_available() or not self._has_head():
            return []
        if limit is None or limit < 1:
            limit = 50
        args = [
            "log",
            "--pretty=format:%H%x09%h%x09%at%x09%an%x09%s",
            "-n",
            str(limit),
        ]
        if rel:
            args += ["--", self._path_arg(rel)]
        out = self._run(args).stdout
        items: List[Dict[str, Any]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            full, short, ts, author, message = parts
            try:
                ts_int = int(ts)
            except ValueError:
                ts_int = 0
            items.append({
                "hash": full,
                "short": short,
                "ts": ts_int,
                "message": message,
                "author": author,
            })
        return items

    def show(self, hash: str, rel: str) -> str:
        """返回 ``<hash>:<rel>`` 的文本内容；revision / path 或对象不存在抛 404。"""
        if not self.enabled or not self.is_available():
            raise HTTPException(status_code=503, detail="git 不可用或快照功能未启用")
        rev = self._check_rev(hash)
        path = self._validate_rel(rel)
        if not self._has_head():
            raise HTTPException(status_code=404, detail=f"快照不存在: {rev}:{path}")
        # 非 0 返回码统一映射为 404（资源不存在），而非 RuntimeError
        res = self._run_ok(["show", f"{rev}:{path}"], ok_codes=(0, 1, 128))
        if res.returncode != 0:
            raise HTTPException(status_code=404, detail=f"快照中不存在: {rev}:{path}")
        return res.stdout

    # ------------------------------------------------------------------ 回滚
    def rollback(self, hash: str, rel: str, vault=None) -> Dict[str, Any]:
        """一键回滚单文件到指定快照。

        步骤：校验 → 验证目标存在 → 自动 commit 当前版本（before rollback 里程碑）
        → ``git checkout <hash> -- <rel>`` → 重索引 + 广播 UPDATED。
        只作用于 ``rel``，不破坏其它文件。
        """
        if not self.enabled or not self.is_available():
            raise HTTPException(status_code=503, detail="git 不可用或快照功能未启用")
        rev = self._check_rev(hash)
        path = self._validate_rel(rel)
        if not self._has_head():
            raise HTTPException(status_code=404, detail="仓库暂无任何提交，无法回滚")
        self.show(rev, path)  # 目标不存在 → 404，且不产生留存提交

        saved = self._commit_before_rollback(f"before rollback {rev}")
        self._run(["checkout", rev, "--", path])

        v = vault if vault is not None else self._vault
        if v is not None:
            v.indexer.reindex_note(path)
            v.eventbus.publish(VaultEvent(EventType.UPDATED, path=path))
        return {"path": path, "hash": rev, "saved_commit": saved}

    # ------------------------------------------------------------------ 状态 / 装配
    def status(self) -> Dict[str, Any]:
        """``{available, repo, head, count}``；git 不可用 / 空仓库也能安全返回。"""
        available = self.is_available()
        repo = (self.root / ".git").exists()
        head: Optional[str] = None
        count = 0
        if available and repo and self._has_head():
            head = self._run(["rev-parse", "--short", "HEAD"]).stdout.strip() or None
            raw = self._run(["rev-list", "--count", "HEAD"]).stdout.strip()
            try:
                count = int(raw)
            except ValueError:
                count = 0
        return {"available": available, "repo": repo, "head": head, "count": count}

    def bind_vault(self, vault) -> None:
        """注入 / 刷新 VaultService（供编排者在装配或 vault 切换时调用）。"""
        self._vault = vault
