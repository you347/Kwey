"""内置智能体：消息反馈（点赞/点踩）与「打开」界面意图。

覆盖：
1. POST /api/agent/feedback 需要 write token；up/down 覆盖同一 id，clear 删除。
2. 反馈摘要注入 system prompt（点赞保持、点踩避免），旧的客户端 system 被丢弃。
3. kwey.open 只校验路径并返回界面意图，不回传正文；目录与笔记 kind 区分。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import feedback, kernel, registry
from app.agent.auth import TokenStore
from app.agent.kernel import (
    SYSTEM_PROMPT,
    _context_hint,
    _ensure_system,
    _system_prompt,
    _ui_intent,
)


def _tk(vault) -> str:
    return TokenStore(vault.paths.root).issue("admin", "test")["token"]


def _post(client, tk, **body):
    return client.post("/api/agent/feedback", headers={"X-Kwey-Token": tk}, json=body)


# ---------------------------------------------------------------- 鉴权与落盘
def test_feedback_requires_token(client):
    r = client.post("/api/agent/feedback", json={"id": "m1", "rating": "up"})
    assert r.status_code == 401


def test_feedback_upsert_and_clear(client, vault):
    tk = _tk(vault)
    r = _post(client, tk, id="m1", rating="up", content="  先给结论，再展开  ")
    assert r.status_code == 200
    assert r.json()["data"]["up"] == 1
    path = Path(vault.paths.root) / ".kwey" / "feedback.json"
    assert path.is_file()

    # 同 id 改「没用」：覆盖而不是追加
    _post(client, tk, id="m1", rating="down", content="太啰嗦")
    items = feedback.load(vault.paths.root)
    assert len(items) == 1
    assert items[0]["rating"] == "down"
    assert feedback.counts(vault.paths.root) == {"up": 0, "down": 1, "total": 1}

    # clear 删除
    _post(client, tk, id="m1", rating="clear")
    assert feedback.load(vault.paths.root) == []


def test_feedback_rejects_bad_rating(client, vault):
    tk = _tk(vault)
    assert _post(client, tk, id="x", rating="love").status_code == 400
    assert _post(client, tk, id="x", rating="up", role="system").status_code == 400


# ---------------------------------------------------------------- 提示词调教
def test_feedback_injected_into_system_prompt(client, vault):
    tk = _tk(vault)
    _post(client, tk, id="a", rating="up", content="先给结论再展开")
    _post(client, tk, id="b", rating="down", content="不要贴大段原文")

    hint = feedback.prompt_hint(vault.paths.root)
    assert "先给结论再展开" in hint and "不要贴大段原文" in hint

    msgs = _ensure_system(
        [{"role": "system", "content": "旧约定"}, {"role": "user", "content": "hi"}],
        vault,
    )
    assert msgs[0]["role"] == "system"
    assert SYSTEM_PROMPT in msgs[0]["content"]
    assert "先给结论再展开" in msgs[0]["content"]
    assert all(m.get("role") != "system" for m in msgs[1:])  # 旧 system 不再透传


def test_feedback_hint_truncates_and_caps(vault):
    root = vault.paths.root
    for i in range(6):
        feedback.upsert(root, fid=f"u{i}", rating="up", content=f"喜欢{i}" + "x" * 300)
    hint = feedback.prompt_hint(root, limit=2)
    assert hint.count("·") == 2  # 只取最近 2 条
    assert "x" * 300 not in hint  # 单条已截断


# ---------------------------------------------------------------- 界面意图
def test_kwey_open_returns_intent_without_content(vault):
    data = registry.invoke("kwey.open", {"path": "欢迎.md"}, vault=vault)
    assert data == {"path": "欢迎.md", "kind": "note"}
    assert "content" not in data  # 正文不回流给智能体

    folder = registry.invoke("kwey.open", {"path": "notes"}, vault=vault)
    assert folder["kind"] == "dir"


def test_kwey_open_missing_path_and_escape(vault):
    with pytest.raises(registry.ContractError) as ei:
        registry.invoke("kwey.open", {"path": "没有这篇.md"}, vault=vault)
    assert ei.value.code == 404

    with pytest.raises(registry.ContractError) as ei2:
        registry.invoke("kwey.open", {"path": "../outside.md"}, vault=vault)
    assert ei2.value.code == 400


def test_ui_intent_mapping():
    assert _ui_intent("kwey.open", {}, {"path": "a.md", "kind": "note"}) == {
        "action": "open", "path": "a.md", "kind": "note",
    }
    assert _ui_intent("kwey.create", {}, {"path": "new.md"}) == {
        "action": "open", "path": "new.md", "kind": "note",
    }
    assert _ui_intent("kwey.read", {}, {"path": "a.md"}) is None
    assert _ui_intent("kwey.open", {}, None) is None


# ---------------------------------------------------------------- 端到端：注入 + UI 事件
def test_kernel_injects_feedback_and_emits_ui(vault, monkeypatch):
    from app.agent import kernel
    from app.agent.provider import AgentProvider

    feedback.upsert(vault.paths.root, fid="f1", rating="down", content="不要复述全文")
    captured = {}

    def fake_chat(self, messages, tools):
        captured.setdefault("system", messages[0]["content"])
        if "ui_sent" in captured:
            return {"content": "好的", "tool_calls": []}
        captured["ui_sent"] = True
        return {
            "content": "",
            "tool_calls": [{"id": "o1", "name": "kwey_open", "arguments": {"path": "欢迎.md"}}],
        }

    monkeypatch.setattr(AgentProvider, "chat", fake_chat)
    events = list(kernel.run_chat([{"role": "user", "content": "打开欢迎"}], vault=vault))

    assert "不要复述全文" in captured["system"]        # 反馈进了 system
    ui = [e for e in events if e["type"] == "ui"]
    assert ui and ui[0]["action"] == "open" and ui[0]["path"] == "欢迎.md"
    # 打开类工具不把正文回灌给模型
    result = next(e for e in events if e["type"] == "tool_result")
    assert "content" not in (result["data"] or {})


# ---------------------------------------------------------------- 端到端：注入 + UI 事件
def test_kernel_injects_feedback_and_emits_ui(vault, monkeypatch):
    from app.agent import kernel
    from app.agent.provider import AgentProvider

    feedback.upsert(vault.paths.root, fid="f1", rating="down", content="不要复述全文")
    captured = {}

    def fake_chat(self, messages, tools):
        captured.setdefault("system", messages[0]["content"])
        if "ui_sent" in captured:
            return {"content": "好的", "tool_calls": []}
        captured["ui_sent"] = True
        return {
            "content": "",
            "tool_calls": [{"id": "o1", "name": "kwey_open", "arguments": {"path": "欢迎.md"}}],
        }

    monkeypatch.setattr(AgentProvider, "chat", fake_chat)
    events = list(kernel.run_chat([{"role": "user", "content": "打开欢迎"}], vault=vault))

    assert "不要复述全文" in captured["system"]        # 反馈进了 system
    ui = [e for e in events if e["type"] == "ui"]
    assert ui and ui[0]["action"] == "open" and ui[0]["path"] == "欢迎.md"
    # 打开类工具不把正文回灌给模型
    result = next(e for e in events if e["type"] == "tool_result")
    assert "content" not in (result["data"] or {})


# ---------------------------------------------------------------- 工作区上下文
def test_context_hint_note_and_dir(vault):
    """笔记：带上路径与所在目录；目录：提示用 path: 限定检索范围。"""
    note = _context_hint(vault, {"path": "notes/读书/2026计划.md", "kind": "note"})
    assert "notes/读书/2026计划.md" in note
    assert "所在目录：notes/读书/" in note
    assert "kwey.read" in note

    folder = _context_hint(vault, {"path": "notes/读书", "kind": "dir"})
    assert "文件夹「notes/读书/」" in folder
    assert "path:notes/读书" in folder


def test_context_hint_ignores_invalid(vault):
    """空 / 越界 / 保留目录 / 不存在 / 无 vault 一律静默忽略，不污染提示词。"""
    assert _context_hint(vault, None) == ""
    assert _context_hint(vault, {"path": ""}) == ""
    assert _context_hint(vault, {"path": "../outside.md"}) == ""
    assert _context_hint(vault, {"path": ".kwey/settings.json"}) == ""
    assert _context_hint(vault, {"path": "notes/不存在.md"}) == ""
    assert _context_hint(None, {"path": "欢迎.md"}) == ""


def test_context_reaches_system_prompt(vault):
    text = _system_prompt(vault, {"path": "欢迎.md", "kind": "note"})
    assert "当前工作区" in text and "欢迎.md" in text

    msgs = _ensure_system(
        [{"role": "system", "content": "旧约定"}, {"role": "user", "content": "hi"}],
        vault,
        {"path": "notes/读书/2026计划.md", "kind": "note"},
    )
    assert msgs[0]["role"] == "system"
    assert "2026计划.md" in msgs[0]["content"]
    assert all(m.get("role") != "system" for m in msgs[1:])


def test_kernel_sends_context_to_provider(vault, monkeypatch):
    """run_chat 把上下文拼进 system（模型据此解析「这篇 / 这个」）。"""
    from app.agent.provider import AgentProvider

    captured = {}

    def fake_chat(self, messages, tools):
        captured["system"] = messages[0]["content"]
        return {"content": "好的", "tool_calls": []}

    monkeypatch.setattr(AgentProvider, "chat", fake_chat)
    events = list(kernel.run_chat(
        [{"role": "user", "content": "总结这篇"}],
        vault=vault,
        context={"path": "notes/读书/2026计划.md", "kind": "note"},
    ))
    assert "notes/读书/2026计划.md" in captured["system"]
    assert events[-1]["type"] == "done"


def test_chat_route_forwards_context(client, vault, monkeypatch):
    """SSE 路由把 context 原样交给内核（内核再做校验）。"""
    tk = _tk(vault)
    seen = {}

    def fake_run(messages, vault=None, resume=None, context=None):
        seen["context"] = context
        yield {"type": "message", "content": "ok"}
        yield {"type": "done", "messages": messages, "pending": None}

    monkeypatch.setattr(kernel, "run_chat", fake_run)
    resp = client.post(
        "/api/agent/chat",
        headers={"X-Kwey-Token": tk},
        json={"messages": [{"role": "user", "content": "hi"}],
              "context": {"path": "欢迎.md", "kind": "note"}},
    )
    assert resp.status_code == 200
    assert seen["context"] == {"path": "欢迎.md", "kind": "note"}


# ---------------------------------------------------------------- 工作区上下文
def test_context_hint_note_and_dir(vault):
    """笔记：带上路径与所在目录；目录：提示用 path: 限定检索范围。"""
    note = _context_hint(vault, {"path": "notes/读书/2026计划.md", "kind": "note"})
    assert "notes/读书/2026计划.md" in note
    assert "所在目录：notes/读书/" in note
    assert "kwey.read" in note

    folder = _context_hint(vault, {"path": "notes/读书", "kind": "dir"})
    assert "文件夹「notes/读书/」" in folder
    assert "path:notes/读书" in folder


def test_context_hint_ignores_invalid(vault):
    """空 / 越界 / 保留目录 / 不存在 / 无 vault 一律静默忽略，不污染提示词。"""
    assert _context_hint(vault, None) == ""
    assert _context_hint(vault, {"path": ""}) == ""
    assert _context_hint(vault, {"path": "../outside.md"}) == ""
    assert _context_hint(vault, {"path": ".kwey/settings.json"}) == ""
    assert _context_hint(vault, {"path": "notes/不存在.md"}) == ""
    assert _context_hint(None, {"path": "欢迎.md"}) == ""


def test_context_reaches_system_prompt(vault):
    text = _system_prompt(vault, {"path": "欢迎.md", "kind": "note"})
    assert "当前工作区" in text and "欢迎.md" in text

    msgs = _ensure_system(
        [{"role": "system", "content": "旧约定"}, {"role": "user", "content": "hi"}],
        vault,
        {"path": "notes/读书/2026计划.md", "kind": "note"},
    )
    assert msgs[0]["role"] == "system"
    assert "2026计划.md" in msgs[0]["content"]
    assert all(m.get("role") != "system" for m in msgs[1:])


def test_kernel_sends_context_to_provider(vault, monkeypatch):
    """run_chat 把上下文拼进 system（模型据此解析「这篇 / 这个」）。"""
    from app.agent.provider import AgentProvider

    captured = {}

    def fake_chat(self, messages, tools):
        captured["system"] = messages[0]["content"]
        return {"content": "好的", "tool_calls": []}

    monkeypatch.setattr(AgentProvider, "chat", fake_chat)
    events = list(kernel.run_chat(
        [{"role": "user", "content": "总结这篇"}],
        vault=vault,
        context={"path": "notes/读书/2026计划.md", "kind": "note"},
    ))
    assert "notes/读书/2026计划.md" in captured["system"]
    assert events[-1]["type"] == "done"


def test_chat_route_forwards_context(client, vault, monkeypatch):
    """SSE 路由把 context 原样交给内核（内核再做校验）。"""
    tk = _tk(vault)
    seen = {}

    def fake_run(messages, vault=None, resume=None, context=None):
        seen["context"] = context
        yield {"type": "message", "content": "ok"}
        yield {"type": "done", "messages": messages, "pending": None}

    monkeypatch.setattr(kernel, "run_chat", fake_run)
    resp = client.post(
        "/api/agent/chat",
        headers={"X-Kwey-Token": tk},
        json={"messages": [{"role": "user", "content": "hi"}],
              "context": {"path": "欢迎.md", "kind": "note"}},
    )
    assert resp.status_code == 200
    assert seen["context"] == {"path": "欢迎.md", "kind": "note"}
