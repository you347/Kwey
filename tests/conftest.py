"""pytest fixtures：临时 vault（中文文件名/嵌套目录/frontmatter 样例）+ TestClient。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api import deps  # noqa: E402
from app.server import create_app  # noqa: E402

WELCOME = """---
title: 欢迎
tags: [入门/指南]
created: 2026-11-12T10:00:00+08:00
自定义: 保留我
---

# 欢迎

这是 #入门/首页 笔记。
链接到 [[2026计划]]，也链接 [[2026计划|新年计划]]。
嵌入 ![[欢迎]] 自身用于测试。
"""

PLAN = """---
tags: [读书/2026]
---

# 2026 计划

见 [[欢迎]] 与 [[欢迎|入口]]。
嵌入 ![[欢迎]] 与图片 ![[pic.png]]。
"""

MEETING = """# 会议记录

今天讨论了 2026计划 但没有建链。
行内标签 #工作/周会
"""


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """结构化临时 vault：
    - 欢迎.md（frontmatter：title/tags/created/自定义字段；行内标签；双链；嵌入）
    - notes/读书/2026计划.md（嵌套目录；引用 欢迎；嵌入附件）
    - notes/工作/会议记录.md（未链接提及「2026计划」）
    - attachments/pic.png
    """
    root = tmp_path / "我的知识库"
    (root / "notes" / "读书").mkdir(parents=True)
    (root / "notes" / "工作").mkdir(parents=True)
    (root / "attachments").mkdir()
    (root / "欢迎.md").write_text(WELCOME, encoding="utf-8")
    (root / "notes" / "读书" / "2026计划.md").write_text(PLAN, encoding="utf-8")
    (root / "notes" / "工作" / "会议记录.md").write_text(MEETING, encoding="utf-8")
    (root / "attachments" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return root


@pytest.fixture
def services(vault_dir: Path):
    """初始化服务族单例（不做 watcher 线程），测试结束清理。"""
    deps.reset_services()
    deps.init_services(vault_dir, open_now=True)
    yield deps
    deps.reset_services()


@pytest.fixture
def client(services):
    """FastAPI TestClient（lifespan 已执行：WS hub 绑定 EventBus）。"""
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def vault(services):
    return deps.require_vault()
