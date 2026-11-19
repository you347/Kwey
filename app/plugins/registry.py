"""插件注册表（C3 / P2 插件系统，安全优先）。

目录布局（vault 内，``.kwey/`` 属 paths.IGNORED_DIRS，watcher 不索引）：

    <vault>/.kwey/plugins/<id>/plugin.json   # 清单
    <vault>/.kwey/plugins/<id>/main.js       # 入口（entry 可自定义）
    <vault>/.kwey/plugins.json               # 启用状态 {"enabled": ["<id>", ...]}

安全模型：**受信插件**，不做代码沙箱。本模块只做两道硬闸：

1. ``plugin.json`` 结构校验——id 格式与目录名一致、permissions 全部落在白名单、
   entry 解析后必须仍在插件目录内（防目录穿越）；任何一项不过，插件整体跳过
   （不出现在 list，也不报错），杜绝坏清单进入前端加载流程。
2. ``get_entry`` 只对**已启用**插件返回入口代码文本，且再次校验 entry 越界
   （双保险），前端拿到文本后经 Blob URL 动态 import。

启用状态：原子写（同目录临时文件 + os.replace）；状态文件缺失 / 损坏 / 结构异常
一律视为「全部禁用」，不阻断启动。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException

# 能力白名单：与 static/js/plugins.js 的能力注入一一对应
VALID_PERMISSIONS = ("read_notes", "write_notes", "read_graph", "ui_command")

# 插件 id：小写字母/数字开头，允许 - 与 _，总长 1..64
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

PLUGINS_DIRNAME = "plugins"       # 相对 .kwey/
STATE_FILENAME = "plugins.json"   # 相对 .kwey/
DEFAULT_ENTRY = "main.js"


class PluginRegistry:
    """扫描 ``<vault>/.kwey/plugins/*/plugin.json`` 并管理启用状态。"""

    def __init__(self, root: Path) -> None:
        self.root: Path = Path(root)
        self.kwey_dir: Path = self.root / ".kwey"
        self.plugins_dir: Path = self.kwey_dir / PLUGINS_DIRNAME
        self.state_path: Path = self.kwey_dir / STATE_FILENAME

    # ------------------------------------------------------------------ state
    def _load_enabled(self) -> Set[str]:
        """读取启用集合；缺失 / 损坏 / 结构异常一律空集（全部禁用）。"""
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return set()
        items = raw.get("enabled") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return set()
        return {i for i in items if isinstance(i, str) and i}

    def _save_enabled(self, ids: Set[str]) -> None:
        """原子写启用集合（同目录临时文件 + os.replace）。"""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(
            json.dumps({"enabled": sorted(ids)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ scan
    def _read_manifest(self, plugin_dir: Path) -> Optional[Dict[str, Any]]:
        """读取并校验单个插件清单；任何不合法返回 None（跳过，不报错）。"""
        manifest_path = plugin_dir / "plugin.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):  # 缺失 / 坏 JSON / 编码错误
            return None
        if not isinstance(raw, dict):
            return None

        # id：格式合法且必须与目录名一致（物理目录是隔离边界，id 错位即跳过）
        pid = raw.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid) or pid != plugin_dir.name:
            return None

        # permissions：必须是 list[str] 且全部在白名单内（缺失视为无权限）
        perms = raw.get("permissions", [])
        if not isinstance(perms, list) or any(
            not isinstance(p, str) or p not in VALID_PERMISSIONS for p in perms
        ):
            return None

        # entry：相对插件目录，解析后不得逃出（防 ../ 穿越）；缺省 main.js
        entry = raw.get("entry", DEFAULT_ENTRY)
        if not isinstance(entry, str) or not entry or entry.startswith("/"):
            return None
        entry_resolved = (plugin_dir / entry).resolve()
        try:
            entry_resolved.relative_to(plugin_dir.resolve())
        except ValueError:
            return None
        # entry 必须指向本插件目录内的普通文件
        if not entry_resolved.is_file():
            entry = DEFAULT_ENTRY if (plugin_dir / DEFAULT_ENTRY).is_file() else None
            if entry is None:
                return None  # 清单声明的入口不存在且默认入口也缺失

        name = raw.get("name")
        version = raw.get("version")
        description = raw.get("description")
        return {
            "id": pid,
            "name": name if isinstance(name, str) and name else pid,
            "version": version if isinstance(version, str) and version else "0.0.0",
            "description": description if isinstance(description, str) else "",
            "permissions": [p for p in perms if isinstance(p, str)],
            "entry": entry,
        }

    def list(self) -> List[Dict[str, Any]]:
        """列出全部合法插件（含 enabled 标记），按 id 排序；损坏的跳过不报错。"""
        enabled = self._load_enabled()
        result: List[Dict[str, Any]] = []
        if self.plugins_dir.is_dir():
            for plugin_dir in sorted(self.plugins_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                manifest = self._read_manifest(plugin_dir)
                if manifest is None:
                    continue
                result.append({**manifest, "enabled": manifest["id"] in enabled})
        return result

    def _find(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """按 id 取单个合法插件清单（含 enabled）；不合法 / 不存在 → None。"""
        if not isinstance(plugin_id, str) or not _ID_RE.match(plugin_id):
            return None
        plugin_dir = self.plugins_dir / plugin_id
        if not plugin_dir.is_dir():
            return None
        manifest = self._read_manifest(plugin_dir)
        if manifest is None:
            return None
        manifest["enabled"] = manifest["id"] in self._load_enabled()
        return manifest

    # ------------------------------------------------------------------ api
    def enable(self, plugin_id: str) -> Dict[str, Any]:
        """启用插件；不存在 / 不合法 → 404。返回更新后的清单条目。"""
        manifest = self._find(plugin_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"插件不存在或清单非法: {plugin_id}")
        enabled = self._load_enabled()
        enabled.add(plugin_id)
        self._save_enabled(enabled)
        return {**manifest, "enabled": True}

    def disable(self, plugin_id: str) -> Dict[str, Any]:
        """禁用插件（幂等）；不存在 / 不合法 → 404。返回更新后的清单条目。"""
        manifest = self._find(plugin_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"插件不存在或清单非法: {plugin_id}")
        enabled = self._load_enabled()
        enabled.discard(plugin_id)
        self._save_enabled(enabled)
        return {**manifest, "enabled": False}

    def get_entry(self, plugin_id: str) -> Dict[str, Any]:
        """返回已启用插件的入口代码文本（供前端 Blob URL 动态 import）。

        未启用 → 403（禁用即撤销加载能力）；不存在 → 404；
        entry 二次越界校验（双保险）→ 400。
        """
        manifest = self._find(plugin_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"插件不存在或清单非法: {plugin_id}")
        if not manifest["enabled"]:
            raise HTTPException(
                status_code=403,
                detail=f"插件未启用，禁止加载入口: {plugin_id}（先在设置页启用）",
            )
        plugin_dir = self.plugins_dir / plugin_id
        entry_resolved = (plugin_dir / manifest["entry"]).resolve()
        try:
            entry_resolved.relative_to(plugin_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"插件 entry 越界: {plugin_id}/{manifest['entry']}"
            ) from None
        try:
            code = entry_resolved.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise HTTPException(
                status_code=404, detail=f"插件入口文件缺失: {plugin_id}/{manifest['entry']}"
            ) from exc
        # 连同 name/permissions 一并下发：前端据此构造能力注入对象（白名单以磁盘清单为准）
        return {
            "id": plugin_id,
            "entry": manifest["entry"],
            "name": manifest["name"],
            "permissions": manifest["permissions"],
            "code": code,
        }
