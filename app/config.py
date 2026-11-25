"""应用配置：读写 vault 内 .kwey/settings.json，提供带默认值的设置项。"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

SETTINGS_DIRNAME = ".kwey"
SETTINGS_FILENAME = "settings.json"


class AppSettings:
    """vault 级设置。缺失/损坏的 settings.json 自动回退默认值并重建。"""

    DEFAULTS: Dict[str, Any] = {
        "theme": "dark",               # light | dark（P0-13；默认深色仪器面板）
        "attachment_dir": "attachments",  # 附件目录（vault 相对，P0-12）
        "editor_mode": "split",        # source | preview | split（P0-2）
        "watch_debounce_ms": 300,      # watchfiles 防抖（风险点①）
        "watch_step_ms": 400,
        "autosave_delay_ms": 800,      # 前端防抖自动保存
        # 字号：按区域独立调节（px）。ui=全局，sidebar=左栏，editor=编辑+预览，chat=智能体聊天
        "fonts": {"ui": 13, "sidebar": 13, "editor": 15, "chat": 13},
        # ---- M2 Agent Link（已实现）/ M3 预留 ----
        "agent": {"enabled": True, "provider": None},    # M2：MCP / REST / SSE / diff
        "snapshots": {"enabled": False, "auto_commit": True},  # M2：git 快照（默认关闭）
        # A2：向量检索可配置。enabled 默认关闭（零依赖开箱即用）；缺 chromadb 时自动降级纯 FTS
        "vector": {
            "enabled": False,
            "backend": "auto",        # auto | sqlite-vec | chroma | null
            "embedder": "auto",       # auto | local(ONNX) | api | hashing
            "model": "bge-small-zh-v1.5",   # 本地模型（models/ 下）
            "api_model": "",          # OpenAI 兼容 embedding 模型名（复用 provider.json）
            "weight": 1.0,       # RRF 中向量通道权重
            "graph_weight": 0.5, # 链接图扩展权重
            "rrf_k": 60,         # RRF 平滑常数
        },
        # ---- P1 前端功能配置 ----
        "template_dir": "templates",      # 模板目录（vault 相对，P1 模板）
        "daily_dir": "daily",             # 每日笔记目录（P1）
        "bookmarks": [],                  # 收藏笔记路径列表
        "recent": [],                    # 最近打开笔记路径列表
        "hotkeys": {},                   # 自定义快捷键映射
        "sidebar_width": 240,            # 左栏宽度（px）
        "rightbar_width": 260,           # 右栏宽度（px）
        "left_panel": "files",           # 左栏默认面板
        "right_panel": "backlinks",      # 右栏默认面板
        # ---- Wave A 第二批（C1-C6）----
        "periodic_dir": "templates",     # 周期笔记模板目录（C2；与 daily 共用）
        "clip_dir": "clips",             # 网页剪藏落盘目录（C6）
        "custom_css_enabled": False,     # 自定义 CSS 实时预览开关（C4；CSS 内容存独立文件）
        # ---- Bases 式结构化视图 ----
        "views": {"dir": "views"},      # 视图定义（*.base YAML）所在目录，随 vault 同步
        # ---- 自动更新检查（只告知，不静默替换）----
        "update": {"check_on_start": True, "platform": "gitee", "repo": "ye-jun-hao55/kwey"},
        # ---- 知识体检 ----
        "diagnose": {"stale_days": 90, "duplicate_ratio": 0.92, "exclude": []},
        # ---- P2-D 知识保鲜：当前事实查询降权开关（可一键关闭） ----
        "freshness": {"downweight": True},
        # ---- 快速捕获 / AI 收件箱 ----
        "inbox": {"dir": "inbox"},
    }

    def __init__(self, vault_root: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._root: Optional[Path] = Path(vault_root) if vault_root else None
        self._data: Dict[str, Any] = copy.deepcopy(self.DEFAULTS)

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
            self._data = copy.deepcopy(self.DEFAULTS)
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
                # A2：vector 已开放；合并子键而非整体覆盖，避免只改 enabled 时丢失 model/weight
                if key == "vector" and isinstance(value, dict) and isinstance(self._data.get("vector"), dict):
                    merged = dict(self._data["vector"])
                    merged.update(value)
                    self._data["vector"] = merged
                elif key == "freshness" and isinstance(value, dict) and isinstance(self._data.get("freshness"), dict):
                    merged = dict(self._data["freshness"])
                    merged.update(value)
                    self._data["freshness"] = merged
                elif key == "fonts" and isinstance(value, dict) and isinstance(self._data.get("fonts"), dict):
                    merged = dict(self._data["fonts"])
                    merged.update(value)
                    self._data["fonts"] = merged
                else:
                    self._data[key] = value
            self.save()
            return dict(self._data)

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
