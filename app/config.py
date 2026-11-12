"""应用配置：读写 vault 内 .kwey/settings.json，提供带默认值的设置项。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

SETTINGS_DIRNAME = ".kwey"
SETTINGS_FILENAME = "settings.json"


class AppSettings:
    """vault 级设置。缺失/损坏的 settings.json 自动回退默认值并重建。"""

    DEFAULTS: Dict[str, Any] = {
        "theme": "light",              # light | dark（P0-13）
        "attachment_dir": "attachments",  # 附件目录（vault 相对，P0-12）
        "editor_mode": "split",        # source | preview | split（P0-2）
        "watch_debounce_ms": 300,      # watchfiles 防抖（风险点①）
        "watch_step_ms": 400,
        "autosave_delay_ms": 800,      # 前端防抖自动保存
        # ---- M2/M3 预留（本期灰置，不实现） ----
        "agent": {"enabled": False, "provider": None},   # T11 占位
        "vector": {"enabled": False, "model": "bge-small-zh-v1.5"},  # T12 占位
    }

    def __init__(self, vault_root: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._root: Optional[Path] = Path(vault_root) if vault_root else None
        self._data: Dict[str, Any] = dict(self.DEFAULTS)

    # ------------------------------------------------------------------ path
    @property
    def settings_path(self) -> Optional[Path]:
        if self._root is None:
            return None
        return self._root / SETTINGS_DIRNAME / SETTINGS_FILENAME

    def set_root(self, vault_root: Path) -> None:
        with self._lock:
            self._root = Path(vault_root)

    # ------------------------------------------------------------------ io
    def load(self) -> "AppSettings":
        """读取 settings.json 并合并到默认值之上；文件缺失/损坏时用默认值。"""
        with self._lock:
            self._data = dict(self.DEFAULTS)
            path = self.settings_path
            if path is not None and path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for key, value in raw.items():
                            # 预留区块整体覆盖，其余标量覆盖默认
                            self._data[key] = value
                except (json.JSONDecodeError, OSError):
                    # 损坏则保留默认值，下次 save 时重建
                    pass
            return self

    def save(self) -> None:
        with self._lock:
            path = self.settings_path
            if path is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)  # 原子替换

    # ------------------------------------------------------------------ access
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """合并更新并持久化；返回更新后的完整设置。"""
        with self._lock:
            for key, value in values.items():
                if key in ("agent", "vector"):
                    continue  # M2/M3 预留区块本期不可改
                self._data[key] = value
            self.save()
            return dict(self._data)

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
