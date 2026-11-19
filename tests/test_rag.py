"""C5 RAG 问答测试：带引用答案 / 幻觉引用剥离 / 无 Provider 摘录降级 / 参数校验。"""

from __future__ import annotations

import json

import pytest

from app.agent import provider as provider_mod
from app.api import routes_rag


def _body(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.fixture
def client(services):
    """本卡专用 client：在标准 app 上临时挂载 rag router（router 注册属集成卡职责）。"""
    from fastapi.testclient import TestClient
    from app.server import create_app

    app = create_app()
    app.include_router(routes_rag.router)
    with TestClient(app) as c:
        yield c


def _make_provider(content, configured=True, protocol="openai"):
    """构造 AgentProvider 替身：configured 可读写、chat 返回给定文本。"""
    p = provider_mod.AgentProvider.__new__(provider_mod.AgentProvider)
    p.config = {"endpoint": "https://example.test/v1" if configured else "", "protocol": protocol}
    p._fake_content = content

    def _chat(messages, tools=None):
        # system 提示必须强调引用约束与"无依据说不知道"
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        assert "来源" in system and "编造" in system
        return {"content": p._fake_content, "tool_calls": []}

    p.chat = _chat
    return p


class TestNoProviderFallback:
    def test_excerpt_answer_when_not_configured(self, client, monkeypatch):
        """未配 Provider → used_provider=False，答案明确是摘录，且含来源引用。"""
        monkeypatch.setattr(
            routes_rag, "_provider_answer",
            lambda q, c, v: (_ for _ in ()).throw(provider_mod.ProviderError("未配置")),
        )
        data = _body(client.post("/api/rag/ask", json={"question": "欢迎 计划"}))
        assert data["used_provider"] is False
        assert "检索摘录" in data["answer"]
        assert "未配置 Agent Provider" in data["answer"]
        assert len(data["sources"]) > 0
        assert all("path" in s and "title" in s for s in data["sources"])

    def test_provider_http_protocol_falls_back(self, client, monkeypatch):
        """http 协议 Provider 不支持 chat → 降级摘录（报告里说明的取舍）。"""
        def _raise(q, c, v):
            raise provider_mod.ProviderError("RAG 需要 openai 协议")
        monkeypatch.setattr(routes_rag, "_provider_answer", _raise)
        data = _body(client.post("/api/rag/ask", json={"question": "会议"}))
        assert data["used_provider"] is False
        assert "检索摘录" in data["answer"]

    def test_empty_vault_answer(self, client, monkeypatch):
        """无召回来源 → 明确说没找到，不编造。"""
        monkeypatch.setattr(routes_rag, "build_context", lambda *a, **k: {"text": "", "sources": []})
        data = _body(client.post("/api/rag/ask", json={"question": "完全不存在的东西xyz"}))
        assert data["used_provider"] is False
        assert data["sources"] == []
        assert "没有找到" in data["answer"]


class TestProviderAnswer:
    def test_valid_citations_kept(self, client, monkeypatch, vault):
        """合法引用（存在于 sources）原样保留，used_provider=True。"""
        ctx = {"text": "参考资料", "sources": [{"path": "欢迎.md", "score": 1.0, "reason": "fts"}]}
        monkeypatch.setattr(routes_rag, "build_context", lambda *a, **k: ctx)
        answer = "欢迎笔记介绍了双链。参见 [[欢迎.md]]。"
        monkeypatch.setattr(routes_rag, "_provider_answer", lambda q, c, v: answer)

        data = _body(client.post("/api/rag/ask", json={"question": "双链"}))
        assert data["used_provider"] is True
        assert "[[欢迎.md]]" in data["answer"]
        assert data["hallucinated"] == []
        assert data["sources"][0]["title"] == "欢迎"

    def test_wikilink_with_display_name(self, client, monkeypatch):
        """[[path|显示名]] 形式，path 合法则保留整串。"""
        ctx = {"text": "x", "sources": [{"path": "notes/读书/2026计划.md", "score": 1.0, "reason": "fts"}]}
        monkeypatch.setattr(routes_rag, "build_context", lambda *a, **k: ctx)
        monkeypatch.setattr(
            routes_rag, "_provider_answer",
            lambda q, c, v: "见 [[notes/读书/2026计划.md|新年计划]]。",
        )
        data = _body(client.post("/api/rag/ask", json={"question": "计划"}))
        assert "[[notes/读书/2026计划.md|新年计划]]" in data["answer"]
        assert data["hallucinated"] == []

    def test_hallucinated_citation_stripped(self, client, monkeypatch):
        """幻觉引用（不在 sources）被剥离并在 hallucinated 标注。"""
        ctx = {"text": "x", "sources": [{"path": "欢迎.md", "score": 1.0, "reason": "fts"}]}
        monkeypatch.setattr(routes_rag, "build_context", lambda *a, **k: ctx)
        monkeypatch.setattr(
            routes_rag, "_provider_answer",
            lambda q, c, v: "正确 [[欢迎.md]]，但也可能在 [[不存在的笔记.md]] 里。",
        )
        data = _body(client.post("/api/rag/ask", json={"question": "双链"}))
        assert "[[欢迎.md]]" in data["answer"]
        assert "[[不存在的笔记.md]]" not in data["answer"]
        assert "不存在的笔记.md" in data["hallucinated"]

    def test_all_hallucinated_stripped(self, client, monkeypatch):
        """全部引用都是幻觉 → 全剥离，answer 仍非空。"""
        ctx = {"text": "x", "sources": [{"path": "欢迎.md", "score": 1.0, "reason": "fts"}]}
        monkeypatch.setattr(routes_rag, "build_context", lambda *a, **k: ctx)
        monkeypatch.setattr(
            routes_rag, "_provider_answer",
            lambda q, c, v: "见 [[幻觉A]] 与 [[幻觉B]]。",
        )
        data = _body(client.post("/api/rag/ask", json={"question": "x"}))
        assert "[[" not in data["answer"]
        assert set(data["hallucinated"]) == {"幻觉A", "幻觉B"}
        assert data["answer"].strip()

    def test_provider_answer_via_chat(self, client, monkeypatch, vault):
        """真走 _provider_answer → AgentProvider.chat，验证 system/user 消息构造。"""
        captured = {}

        class _FakeProvider:
            configured = True
            config = {"endpoint": "https://x/v1", "protocol": "openai"}

            def __init__(self, root):
                pass

            def chat(self, messages, tools=None):
                captured["messages"] = messages
                return {"content": "答案 [[欢迎.md]]", "tool_calls": []}

        monkeypatch.setattr(routes_rag, "AgentProvider", _FakeProvider)
        data = _body(client.post("/api/rag/ask", json={"question": "测试"}))
        assert data["used_provider"] is True
        msgs = captured["messages"]
        assert len(msgs) == 2 and msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
        assert "测试" in msgs[1]["content"]


class TestValidation:
    def test_empty_question_400(self, client):
        r = client.post("/api/rag/ask", json={"question": "   "})
        assert r.status_code == 400

    def test_max_chars_out_of_range_400(self, client):
        r = client.post("/api/rag/ask", json={"question": "x", "max_chars": 10})
        assert r.status_code == 400
        r = client.post("/api/rag/ask", json={"question": "x", "max_chars": 999999})
        assert r.status_code == 400

    def test_question_too_long_400(self, client):
        r = client.post("/api/rag/ask", json={"question": "x" * 2100})
        assert r.status_code == 400

    def test_response_envelope(self, client):
        data = _body(client.post("/api/rag/ask", json={"question": "双链"}))
        assert set(data.keys()) >= {"answer", "sources", "used_provider", "hallucinated", "question"}
        assert data["question"] == "双链"


class TestCitationParser:
    def test_regex_variants(self):
        from app.api.routes_rag import _WIKILINK_RE
        text = "[[a]] 与 [[dir/b.md|显示]] 与 [[带 空格]]"
        assert [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)] == [
            "a", "dir/b.md", "带 空格",
        ]
