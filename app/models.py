"""Pydantic 模型：API 请求体与核心数据结构。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- core data
class NoteDoc(BaseModel):
    """一篇笔记的完整读取结果（正文不含 frontmatter，二者分离）。"""

    path: str
    title: str
    content: str
    frontmatter: Dict[str, Any] = Field(default_factory=dict)
    mtime: float = 0.0


class SearchHit(BaseModel):
    """一条搜索命中。"""

    path: str
    title: str
    score: float = 0.0
    snippet: str = ""
    tags: List[str] = Field(default_factory=list)


class BacklinkItem(BaseModel):
    path: str
    title: str
    snippet: str = ""
    line: int = 0


class MentionItem(BaseModel):
    path: str
    title: str
    snippet: str = ""
    line: int = 0


# ---------------------------------------------------------------- requests
class VaultOpenRequest(BaseModel):
    path: str  # vault 绝对路径


class NoteCreateRequest(BaseModel):
    path: str
    content: str = ""
    frontmatter: Optional[Dict[str, Any]] = None


class NoteUpdateRequest(BaseModel):
    path: str
    content: str = ""
    frontmatter: Optional[Dict[str, Any]] = None
    base_mtime: Optional[float] = None  # 乐观锁：不符返回 409


class CreateNoteRequest(BaseModel):
    dir: str = ""
    name: str


class CreateDirRequest(BaseModel):
    dir: str = ""
    name: str


class RenameRequest(BaseModel):
    path: str
    new_name: str


class MoveRequest(BaseModel):
    path: str
    new_dir: str = ""


class LinkifyRequest(BaseModel):
    path: str          # 提及所在笔记
    mention_path: str  # 提及目标（当前笔记）路径
    line: Optional[int] = None  # 1-based 行号；缺省取第一处


class RenderRequest(BaseModel):
    """预览渲染请求：content 为编辑器未落盘的当前正文，缺省则回退读盘。"""

    path: str
    content: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    theme: Optional[str] = None
    attachment_dir: Optional[str] = None
    editor_mode: Optional[str] = None
    autosave_delay_ms: Optional[int] = None
    # ---- M2 Agent Link / A2 向量检索配置 ----
    agent: Optional[Dict[str, Any]] = None
    snapshots: Optional[Dict[str, Any]] = None
    vector: Optional[Dict[str, Any]] = None  # A2：enabled/model/weight/graph_weight/rrf_k
    freshness: Optional[Dict[str, Any]] = None  # P2-D：downweight 开关
    fonts: Optional[Dict[str, Any]] = None   # 字号：ui/sidebar/editor/chat（px，前端注入 CSS 变量）
    # ---- P1 前端功能配置 ----
    template_dir: Optional[str] = None
    daily_dir: Optional[str] = None
    bookmarks: Optional[List[str]] = None
    recent: Optional[List[str]] = None
    hotkeys: Optional[Dict[str, str]] = None
    sidebar_width: Optional[int] = None
    rightbar_width: Optional[int] = None
    left_panel: Optional[str] = None
    right_panel: Optional[str] = None
    # ---- Wave A 第二批（C1-C6）----
    periodic_dir: Optional[str] = None
    clip_dir: Optional[str] = None
    custom_css_enabled: Optional[bool] = None


class TrashRestoreRequest(BaseModel):
    """回收站恢复：trash_path 为 .trash/ 内路径，target_path 缺省按原路径恢复。"""

    trash_path: str
    target_path: Optional[str] = None


class TemplateApplyRequest(BaseModel):
    """模板套用：template_path 为模板相对路径，target_path 缺省按日期命名放 vault 根。"""

    template_path: str
    target_path: Optional[str] = None
