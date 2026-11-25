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


# ---------------------------------------------------------------- 假时钟 / 假 Provider
class FakeClock:
    """可推进的假时钟；实例本身可调用，直接作为 duty 的 ``clock`` 注入。"""

    def __init__(self, ts: float = 0.0) -> None:
        self.ts = float(ts)

    def __call__(self) -> float:
        return self.ts

    def set(self, ts: float) -> float:
        self.ts = float(ts)
        return self.ts

    def advance(self, seconds: float) -> float:
        self.ts += float(seconds)
        return self.ts


class FakeProvider:
    """OpenAI 协议 Provider 的脚本化替身：按序返回 payload，不联网、不花钱。"""

    def __init__(self, payloads=None, *, configured: bool = True) -> None:
        self.configured = configured
        self.config = {
            "endpoint": "http://stub.local/v1",
            "api_key": "SECRET-KEY",
            "model": "test-model",
            "protocol": "openai",
            "timeout": 30.0,
            "max_context_chars": 12000,
        }
        self.payloads = list(payloads or [])
        self.calls = []

    def chat(self, messages, tools=None, extra_body=None, timeout=None):
        import json as _json

        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "extra_body": extra_body,
                "timeout": timeout,
            }
        )
        payload = self.payloads.pop(0) if self.payloads else {}
        content = payload if isinstance(payload, str) else _json.dumps(payload, ensure_ascii=False)
        return {"content": content, "tool_calls": []}


@pytest.fixture
def fake_clock():
    """工厂：``clock = fake_clock(1764000000.0)``，可 ``clock.advance(60)``。"""

    def _make(ts: float = 0.0) -> FakeClock:
        return FakeClock(ts)

    return _make


@pytest.fixture
def fake_provider():
    """工厂：``provider = fake_provider({"items": [...]}, configured=True)``。"""

    def _make(*payloads, configured: bool = True) -> FakeProvider:
        return FakeProvider(payloads, configured=configured)

    return _make

