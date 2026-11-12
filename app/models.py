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


class SettingsUpdateRequest(BaseModel):
    theme: Optional[str] = None
    attachment_dir: Optional[str] = None
    editor_mode: Optional[str] = None
    autosave_delay_ms: Optional[int] = None
