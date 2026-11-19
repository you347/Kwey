"""F2 每日自动推送 Gitee / GitHub（卡片 F2）。

零新第三方依赖：标准库 ``subprocess`` 调系统 git + ``threading.Timer`` 定时
（契约明确不用 GitPython / APScheduler）。所有 git 调用 ``cwd=vault root``，
只操作 vault 内仓库，绝不触碰项目代码仓库。

配置存 ``.kwey/autopush.json``（该目录已被 .gitignore 排除，不随推送外泄）。
凭据策略：配置只存 remote 名（如 origin），不存任何 token / 带凭据 URL；
认证完全交给系统 git credential helper（Windows 凭据管理器 / macOS keychain /
libsecret），明文 token 永不落盘到 Kwey 的任何文件。

调度语义：
- 每天本地时间 ``time``（HH:MM）触发一次；应用未运行时错过的计划，下次启动
  对比 ``last_push`` 与最近一个已过去的计划时刻补推。
- push 前 ``git pull --rebase --autostash``；冲突则 ``rebase --abort`` 还原
  工作区，状态置 paused 并取消定时器，提示手动处理；手动「立即推送」成功后
  恢复调度。手动 run 不受 enabled 限制（用户显式触发），开关只控制定时器。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.paths import normalize_rel

logger = logging.getLogger("kwey.autopush")

CONFIG_DIRNAME = ".kwey"
CONFIG_FILENAME = "autopush.json"

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "remote": "origin",
    "branch": "main",
    "time": "22:00",
    "commit_template": "vault backup {date}",
    "include_dirs": [],          # 空 = 整个 vault
    "last_push": 0,              # epoch 秒；0 = 从未推送（服务维护，拒绝外部写入）
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# remote / branch 白名单：首字符必须字母数字（防 '-' 开头被当成 git 选项）
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# git status --porcelain 的未合并（冲突）状态码
_UNMERGED = ("UU", "AA", "DD", "AU", "UA", "DU", "UD")


def _validate(updates: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化配置增量；非法抛 ValueError（路由层转 400）。"""
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in DEFAULTS or key == "last_push":
            continue
        if key == "enabled":
            clean[key] = bool(value)
        elif key in ("remote", "branch"):
            v = str(value).strip()
            pattern = _REMOTE_RE if key == "remote" else _BRANCH_RE
            if not pattern.fullmatch(v) or ".." in v:
                raise ValueError(f"非法 {key}: {value!r}")
            clean[key] = v
        elif key == "time":
            v = str(value).strip()
            if not _TIME_RE.fullmatch(v):
                raise ValueError(f"time 需为 HH:MM（24 小时制）: {value!r}")
            clean[key] = v
        elif key == "commit_template":
            v = str(value).strip() or DEFAULTS["commit_template"]
            if len(v) > 200:
                raise ValueError("commit_template 过长（最多 200 字符）")
            clean[key] = v
        elif key == "include_dirs":
            if not isinstance(value, (list, tuple)):
                raise ValueError("include_dirs 需为字符串列表")
            dirs: List[str] = []
            for item in value:
                rel = normalize_rel(str(item))
                if not rel:
                    continue
                parts = rel.split("/")
                if (
                    any(p in ("", ".", "..") for p in parts)
                    or rel.startswith("-")
                    or ":" in rel
                ):
                    raise ValueError(f"非法包含目录: {item!r}")
                if rel not in dirs:
                    dirs.append(rel)
            clean[key] = dirs
    return clean


def _iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


class AutoPushService:
    """vault 级每日自动推送：配置持久化 + threading 定时 + 补推 + 冲突暂停。

    Args:
        root: vault 绝对根目录（git 仓库与 .kwey/autopush.json 都在此目录下）。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()      # 保护配置与运行状态
        self._run_lock = threading.Lock()   # 同一时刻只允许一条推送流水线
        self._timer: Optional[threading.Timer] = None
        self._config: Dict[str, Any] = self._load()
        self._state = "idle"                # idle | running | paused | error
        self._last_result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 配置
    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_DIRNAME / CONFIG_FILENAME

    def _load(self) -> Dict[str, Any]:
        data = copy.deepcopy(DEFAULTS)
        path = self.config_path
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in DEFAULTS:
                        if key in raw:
                            data[key] = raw[key]
            except (json.JSONDecodeError, OSError):
                pass  # 损坏回退默认值，下次 save 重建
        return data

    def _save(self) -> None:
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)  # 原子替换

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """校验合并 → 持久化 → 重排定时器；返回更新后的完整配置。"""
        clean = _validate(updates)
        with self._lock:
            self._config.update(clean)
            self._save()
        self._schedule_next()
        return self.get_config()

    # ------------------------------------------------------------------ 调度
    def _slot_of(self, day: date) -> datetime:
        hh, mm = self._config["time"].split(":")
        return datetime.combine(day, dtime(int(hh), int(mm)))

    def next_run(self, now: Optional[float] = None) -> Optional[float]:
        """下一个计划时刻（epoch 秒）；未启用返回 None。"""
        with self._lock:
            if not self._config["enabled"]:
                return None
            now_dt = datetime.fromtimestamp(now if now is not None else time.time())
            slot = self._slot_of(now_dt.date())
            if slot <= now_dt:
                slot += timedelta(days=1)
            return slot.timestamp()

    def _latest_past_slot(self, now_dt: datetime) -> datetime:
        slot = self._slot_of(now_dt.date())
        if slot > now_dt:
            slot -= timedelta(days=1)
        return slot

    def needs_catchup(self, now: Optional[float] = None) -> bool:
        """补推判定：已启用且 last_push 早于最近一个已过去的计划时刻。"""
        with self._lock:
            if not self._config["enabled"]:
                return False
            now_dt = datetime.fromtimestamp(now if now is not None else time.time())
            last = self._config.get("last_push") or 0
            return last < self._latest_past_slot(now_dt).timestamp()

    def start(self) -> None:
        """启动入口（lifespan / vault 打开后调用）：补推 + 排定定时器，不阻塞。"""
        if self.needs_catchup():
            threading.Thread(
                target=self._catchup, name="kwey-autopush-catchup", daemon=True
            ).start()
        self._schedule_next()

    def _catchup(self) -> None:
        try:
            self.run(reason="catchup")
        except Exception:  # noqa: BLE001 —— 补推失败不影响应用启动
            logger.exception("启动补推失败")

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_next(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._config["enabled"] or self._state == "paused":
                return
            delay = max(0.0, (self.next_run() or 0) - time.time())
            self._timer = threading.Timer(delay, self._on_timer)
            self._timer.daemon = True
            self._timer.name = "kwey-autopush"
            self._timer.start()

    def _on_timer(self) -> None:
        try:
            self.run(reason="schedule")
        except Exception:  # noqa: BLE001 —— 定时器线程不允许裸崩
            logger.exception("定时推送异常")
            self._schedule_next()

    # ------------------------------------------------------------------ git
    def _git(
        self,
        args: List[str],
        ok_codes: Tuple[int, ...] = (0,),
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        """统一 git 调用（cwd=vault root）；非预期返回码抛 RuntimeError。"""
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode not in ok_codes:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"git {' '.join(args)} 失败 (code={result.returncode}): {stderr}"
            )
        return result

    def _git_available(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "--version"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _has_conflict(self, stderr: str) -> bool:
        if "conflict" in stderr.lower():
            return True
        git_dir = self.root / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            return True
        try:
            out = self._git(["status", "--porcelain"]).stdout
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return False
        return any(line[:2] in _UNMERGED for line in out.splitlines())

    @staticmethod
    def _render_message(template: str) -> str:
        now = datetime.now()
        return template.replace("{date}", now.strftime("%Y-%m-%d")).replace(
            "{datetime}", now.strftime("%Y-%m-%d %H:%M:%S")
        )

    # ------------------------------------------------------------------ 推送
    def run(self, reason: str = "manual") -> Dict[str, Any]:
        """执行一条完整推送流水线；并发触发时返回 skipped。

        返回 ``{status: pushed|paused|error|skipped, reason, message, ts,
        committed, commit}``。paused / error 都不会 push。
        """
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "skipped", "reason": reason,
                "message": "已有推送进行中，本次跳过", "ts": int(time.time()),
                "committed": False, "commit": None,
            }
        try:
            with self._lock:
                self._state = "running"
            result = self._pipeline(reason)
            with self._lock:
                self._state = {
                    "pushed": "idle", "paused": "paused",
                }.get(result["status"], "error")
                self._last_result = result
            return result
        finally:
            self._run_lock.release()
            self._schedule_next()

    def _pipeline(self, reason: str) -> Dict[str, Any]:
        ts = int(time.time())

        def fail(message: str, status: str = "error") -> Dict[str, Any]:
            return {
                "status": status, "reason": reason, "message": message,
                "ts": ts, "committed": False, "commit": None,
            }

        if not self._git_available():
            return fail("系统未安装 git，无法自动推送")
        if not (self.root / ".git").exists():
            return fail("vault 尚未初始化 git 仓库（请先在设置中开启 git 快照）")

        cfg = self.get_config()
        remote, branch = cfg["remote"], cfg["branch"]
        committed, commit_hash = False, None
        try:
            if remote not in self._git(["remote"]).stdout.split():
                return fail(
                    f"remote 不存在: {remote}"
                    f"（先在 vault 中执行 git remote add {remote} <url>）"
                )
            # 1) 暂存 + 提交（include_dirs 为空 = 全库）
            dirs = cfg["include_dirs"]
            self._git(["add", "-A", "--", *dirs] if dirs else ["add", "-A"])
            staged = self._git(["diff", "--cached", "--quiet"], ok_codes=(0, 1))
            if staged.returncode == 1:
                self._git(["commit", "-m", self._render_message(cfg["commit_template"])])
                commit_hash = self._git(["rev-parse", "--short", "HEAD"]).stdout.strip()
                committed = True
            # 2) push 前同步远端；autostash 兼容 include_dirs 之外的未暂存改动
            pull = self._git(
                ["pull", "--rebase", "--autostash", remote, branch],
                ok_codes=(0, 1, 128), timeout=180,
            )
            if pull.returncode != 0:
                stderr = (pull.stderr or "").strip()
                if self._has_conflict(stderr):
                    # 还原工作区，绝不把冲突现场留给用户的主写路径
                    self._git(["rebase", "--abort"], ok_codes=(0, 1, 128))
                    return fail(
                        "pull --rebase 冲突：已中止 rebase 并暂停自动推送，"
                        "请手动解决冲突后点击「立即推送」恢复",
                        status="paused",
                    )
                low = stderr.lower()
                if "couldn't find remote ref" not in low and "no such ref" not in low:
                    return fail(f"pull --rebase 失败（未推送）: {stderr or '未知错误'}")
                # 远端分支尚不存在：首次推送，直接 push 建分支
            # 3) 推送
            push = self._git(["push", remote, branch], ok_codes=(0, 1, 128), timeout=180)
            if push.returncode != 0:
                return fail(f"push 失败: {(push.stderr or '').strip() or '未知错误'}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            return fail(f"git 操作失败: {exc}")

        with self._lock:
            self._config["last_push"] = ts
            self._save()
        return {
            "status": "pushed", "reason": reason, "message": "推送成功",
            "ts": ts, "committed": committed, "commit": commit_hash,
        }

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        with self._lock:
            cfg = copy.deepcopy(self._config)
            state = self._state
            last_result = self._last_result
            scheduled = self._timer is not None
        nxt = self.next_run() if (cfg["enabled"] and state != "paused") else None
        return {
            "enabled": cfg["enabled"],
            "state": state,
            "remote": cfg["remote"],
            "branch": cfg["branch"],
            "time": cfg["time"],
            "next_run": nxt,
            "next_run_iso": _iso(nxt),
            "last_push": cfg["last_push"] or None,
            "last_push_iso": _iso(cfg["last_push"]),
            "last_result": last_result,
            "catchup_pending": self.needs_catchup(),
            "git_available": self._git_available(),
            "repo": (self.root / ".git").exists(),
            "scheduled": scheduled,
        }