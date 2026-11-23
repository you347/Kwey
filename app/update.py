"""版本检查（自动更新第一半：告知；第二半是安装器覆盖安装）。

刻意不做「静默下载替换 exe」：本地优先产品的更新应当由用户决定，
且 Windows 上替换运行中的 exe 需要额外的 helper 进程，收益不抵风险。

数据源：
- Gitee Releases：``/api/v5/repos/{owner}/{repo}/releases/latest``（国内快，主用）
- 没有 Release 时回落到 tags（`v0.2.0` 也能比较）
- GitHub 作为可选（海外用户）

失败一律静默降级为 ``ok=false``，绝不因为检查更新拖慢启动或弹错误。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("kwey.update")

DEFAULTS: Dict[str, Any] = {
    "platform": "gitee",          # gitee | github
    "repo": "ye-jun-hao55/kwey",
    "timeout": 6.0,
    "cache_seconds": 6 * 3600,
}

_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+]([\w.]+))?", re.IGNORECASE)
_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_UA = "Kwey-Updater/1.0"


def current_version() -> str:
    from app import __version__

    return str(__version__)


def parse_version(text: str) -> Tuple[int, int, int]:
    match = _SEMVER_RE.search(str(text or ""))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups()[:3])  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _get_json(url: str, timeout: float) -> Optional[Any]:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        logger.info("检查更新失败 %s: %s", url, exc)
        return None


def _fetch_gitee(repo: str, timeout: float) -> Optional[Dict[str, Any]]:
    base = f"https://gitee.com/api/v5/repos/{repo}"
    release = _get_json(f"{base}/releases/latest", timeout)
    if isinstance(release, dict) and release.get("tag_name"):
        assets = release.get("assets") or []
        url = ""
        for asset in assets:
            if isinstance(asset, dict) and str(asset.get("name", "")).endswith(".exe"):
                url = asset.get("browser_download_url") or ""
                break
        return {
            "latest": str(release.get("tag_name")),
            "url": url or release.get("html_url") or f"https://gitee.com/{repo}/releases",
            "notes": str(release.get("body") or "")[:4000],
            "published_at": release.get("created_at") or release.get("released_at"),
            "source": "gitee/release",
        }
    tags = _get_json(f"{base}/tags", timeout)
    if isinstance(tags, list) and tags:
        names = [str(t.get("name")) for t in tags if isinstance(t, dict) and t.get("name")]
        names.sort(key=parse_version, reverse=True)
        if names:
            return {
                "latest": names[0],
                "url": f"https://gitee.com/{repo}/releases",
                "notes": "仓库还没有 Release，按最新 tag 比较；安装包请到 Release 页下载。",
                "published_at": None,
                "source": "gitee/tags",
            }
    return None


def _fetch_github(repo: str, timeout: float) -> Optional[Dict[str, Any]]:
    data = _get_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout)
    if isinstance(data, dict) and data.get("tag_name"):
        return {
            "latest": str(data.get("tag_name")),
            "url": data.get("html_url") or f"https://github.com/{repo}/releases",
            "notes": str(data.get("body") or "")[:4000],
            "published_at": data.get("published_at"),
            "source": "github/release",
        }
    return None


def check(config: Optional[Dict[str, Any]] = None, *, force: bool = False) -> Dict[str, Any]:
    """检查更新。结果缓存 6 小时；网络失败不抛错。"""
    cfg = dict(DEFAULTS)
    for key, value in (config or {}).items():
        if value is not None:
            cfg[key] = value
    repo = str(cfg.get("repo") or DEFAULTS["repo"])
    platform = str(cfg.get("platform") or "gitee").lower()
    timeout = float(cfg.get("timeout") or DEFAULTS["timeout"])
    version = current_version()

    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["at"] < float(cfg["cache_seconds"]):
        cached = dict(_CACHE["data"])
        cached["cached"] = True
        return cached

    info = _fetch_gitee(repo, timeout) if platform != "github" else _fetch_github(repo, timeout)
    if info is None and platform != "github":
        info = _fetch_github(repo, timeout)     # Gitee 挂了再试 GitHub

    if info is None:
        result = {
            "ok": False, "current": version, "latest": None, "has_update": False,
            "url": f"https://gitee.com/{repo}/releases" if platform != "github" else f"https://github.com/{repo}/releases",
            "message": "检查更新失败（网络不可达或仓库无 Release）",
        }
        return result

    result = {
        "ok": True,
        "current": version,
        "latest": info["latest"],
        "latest_version": ".".join(str(p) for p in parse_version(info["latest"])),
        "has_update": is_newer(info["latest"], version),
        "url": info.get("url"),
        "notes": info.get("notes") or "",
        "published_at": info.get("published_at"),
        "source": info.get("source"),
        "message": "",
    }
    _CACHE["at"], _CACHE["data"] = now, result
    return result


def reset_cache() -> None:
    _CACHE["at"], _CACHE["data"] = 0.0, None
