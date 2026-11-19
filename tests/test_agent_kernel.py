"""卡B W1：内置智能体内核 + provider.chat 单测（mock Provider，无真实 LLM）。

全部走 conftest 临时 vault。provider.chat 以 monkeypatch 脚本化响应，
验证 tool-calling 循环、confirm 拦截 / resume、工具名净化、journal 归因、
错误回灌、步数上限、SSE 路由，以及 api_key 绝不回显。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent import journal, kernel, registry
from app.agent.provider import AgentProvider, ProviderError
from app.agent.auth import TokenStore


# ---------------------------------------------------------------- helpers
def _final(content):
    return {"content": content, "tool_calls": []}


def _call(name, args, cid="c1"):
    return {"content": None, "tool_calls": [{"id": cid, "name": name, "arguments": args}]}


def _script(monkeypatch, *responses):
    """按序返回脚本化 assistant 消息；耗尽后再调用返回最终答复（避免 StopIteration）。"""
    it = iter(responses)

    def fake(self, messages, tools):
        return next(it, _final("（脚本耗尽）"))

    monkeypatch.setattr(AgentProvider, "chat", fake)


def _configure(vault_dir: Path, **kw):
    cfg = {"endpoint": "http://stub.local/v1", "api_key": "SECRET-KEY", "model": "test-model", "protocol": "openai"}
    cfg.update(kw)
    p = vault_dir / ".kwey" / "provider.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")


def _types(events):
    return [e["type"] for e in events]


USER = [{"role": "user", "content": "帮我操作笔记"}]


# ---------------------------------------------------------------- 循环主流程
def test_final_answer_no_tools(vault, monkeypatch):
    _script(monkeypatch, _final("你好，我是 Kwey 助手"))
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["message", "done"]
    assert events[0]["content"] == "你好，我是 Kwey 助手"
    assert events[1]["pending"] is None
    assert events[1]["messages"][0]["role"] == "system"  # 自动补 system


def test_single_tool_creates_note_and_journals(vault, monkeypatch):
    _script(monkeypatch, _call("kwey_create", {"path": "内核新建.md", "content": "经内核"}, "t1"), _final("已创建"))
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["tool_call", "tool_result", "ui", "message", "done"]
    assert events[0]["name"] == "kwey.create"  # llm 名映射回真名
    assert events[2]["action"] == "open" and events[2]["path"] == "内核新建.md"
    assert events[1]["ok"] is True
    assert "经内核" in vault.read_note("内核新建.md")["content"]
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert any(
        e["name"] == "kwey-built-in" and e["role"] == "admin"
        and e["action"] == "create" and e["path"] == "内核新建.md"
        for e in recent
    )


def test_multi_step_create_then_read(vault, monkeypatch):
    _script(
        monkeypatch,
        _call("kwey_create", {"path": "多步.md", "content": "多步内容"}, "a"),
        _call("kwey_read", {"path": "多步.md"}, "b"),
        _final("读到了"),
    )
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["tool_call", "tool_result", "ui", "tool_call", "tool_result", "message", "done"]
    assert events[4]["ok"] is True
    assert "多步内容" in events[4]["data"]["content"]


def test_tool_error_feeds_back_and_continues(vault, monkeypatch):
    _script(monkeypatch, _call("kwey_read", {"path": "不存在.md"}, "e"), _final("没找到"))
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["tool_call", "tool_result", "message", "done"]
    assert events[1]["ok"] is False
    assert "笔记不存在" in events[1]["error"]


def test_max_steps_bound(vault, monkeypatch):
    n = [0]

    def fake(self, messages, tools):
        n[0] += 1
        return _call("kwey_tree", {}, f"s{n[0]}")

    monkeypatch.setattr(AgentProvider, "chat", fake)
    events = list(kernel.run_chat(USER, vault=vault))
    assert n[0] == kernel.MAX_STEPS
    assert any(e["type"] == "error" and "最大步数" in e["message"] for e in events)
    assert events[-1]["type"] == "done"


# ---------------------------------------------------------------- confirm 拦截 / resume
def test_confirm_gate_pauses_even_if_llm_self_confirms(vault, monkeypatch):
    _script(monkeypatch, _call("kwey_trash_purge", {"confirm": True}, "p"))
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["confirm_required", "done"]
    assert events[0]["name"] == "kwey.trash.purge"
    assert events[1]["pending"]["id"] == "p"
    # 未执行：无 purge 留痕
    assert not any(e["action"] == "purge" for e in journal.recent(vault.paths.ensure_root(), 10))


def test_confirm_resume_executes_then_continues(vault, monkeypatch):
    _script(monkeypatch, _call("kwey_trash_purge", {}, "p"))
    events = list(kernel.run_chat(USER, vault=vault))
    msgs = events[-1]["messages"]

    _script(monkeypatch, _final("已清空"))
    events2 = list(kernel.run_chat(msgs, vault=vault, resume={"id": "p"}))
    assert _types(events2) == ["tool_call", "tool_result", "message", "done"]
    assert events2[0]["arguments"]["confirm"] is True  # resume 强制 confirm
    assert events2[1]["ok"] is True
    assert any(
        e["action"] == "purge" and e["name"] == "kwey-built-in"
        for e in journal.recent(vault.paths.ensure_root(), 5)
    )


def test_dir_delete_is_confirm_gated(vault, monkeypatch):
    _script(monkeypatch, _call("kwey_dir_delete", {"path": "notes"}, "d"))
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["confirm_required", "done"]
    assert events[0]["name"] == "kwey.dir.delete"


# ---------------------------------------------------------------- 工具名净化
def test_tool_names_sanitized_and_mapped_back(vault, monkeypatch):
    captured = {}

    def fake(self, messages, tools):
        captured["tools"] = tools
        return _call("kwey_graph_query", {"path": "欢迎.md"}, "g")

    monkeypatch.setattr(AgentProvider, "chat", fake)
    events = list(kernel.run_chat(USER, vault=vault))
    tools = captured["tools"]
    assert len(tools) == 22  # 契约新增 kwey.open
    for t in tools:
        fn = t["function"]["name"]
        assert "." not in fn and fn.replace("_", "").isalnum()
    assert events[0]["type"] == "tool_call"
    assert events[0]["name"] == "kwey.graph.query"  # 净化名映射回真名


# ---------------------------------------------------------------- Provider 错误 / 安全
def test_provider_not_configured_yields_error(vault):
    # 不写 provider.json，真实 AgentProvider.configured=False
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["error", "done"]
    assert "未配置" in events[0]["message"]


def test_provider_error_event_no_api_key(vault, vault_dir, monkeypatch):
    _configure(vault_dir)

    def fake(self, messages, tools):
        raise ProviderError("Provider 返回 HTTP 401，请检查 endpoint / model 配置")

    monkeypatch.setattr(AgentProvider, "chat", fake)
    events = list(kernel.run_chat(USER, vault=vault))
    assert _types(events) == ["error", "done"]
    assert "SECRET-KEY" not in json.dumps(events, ensure_ascii=False)


# ---------------------------------------------------------------- provider.chat 单元
def test_parse_chat_tool_calls():
    raw = json.dumps({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "x", "type": "function", "function": {
            "name": "kwey_create", "arguments": '{"path":"a.md"}'}}],
    }}]})
    norm = AgentProvider._parse_chat(raw)
    assert norm["content"] is None
    assert norm["tool_calls"] == [{"id": "x", "name": "kwey_create", "arguments": {"path": "a.md"}}]


def test_parse_chat_final_content():
    raw = json.dumps({"choices": [{"message": {"content": "完成"}}]})
    norm = AgentProvider._parse_chat(raw)
    assert norm == {"content": "完成", "tool_calls": []}


def test_parse_chat_bad_arguments_raises():
    raw = json.dumps({"choices": [{"message": {"tool_calls": [
        {"id": "x", "function": {"name": "kwey_create", "arguments": "{bad json"}}]}}]})
    with pytest.raises(ProviderError):
        AgentProvider._parse_chat(raw)


def test_parse_chat_missing_message_raises():
    with pytest.raises(ProviderError):
        AgentProvider._parse_chat(json.dumps({"choices": []}))


def test_chat_requires_openai_protocol(vault_dir):
    _configure(vault_dir, protocol="http")
    with pytest.raises(ProviderError) as ei:
        AgentProvider(vault_dir).chat([{"role": "user", "content": "x"}], [])
    assert "openai" in str(ei.value)


def test_chat_not_configured_raises(vault_dir):
    with pytest.raises(ProviderError):
        AgentProvider(vault_dir).chat([], [])


def test_chat_payload_and_bearer(vault_dir, monkeypatch):
    _configure(vault_dir)
    captured = {}

    def fake_post(self, url, payload, use_bearer, timeout=None):
        captured.update(url=url, payload=payload, bearer=use_bearer)
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(AgentProvider, "_post", fake_post)
    tools = [{"type": "function", "function": {"name": "kwey_tree", "description": "d", "parameters": {"type": "object"}}}]
    norm = AgentProvider(vault_dir).chat([{"role": "user", "content": "x"}], tools)
    assert norm["content"] == "ok"
    assert captured["url"].endswith("/chat/completions")
    assert captured["bearer"] is True
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["tool_choice"] == "auto"
    assert captured["payload"]["tools"] == tools


def test_chat_http_error_no_key_leak(vault_dir, monkeypatch):
    _configure(vault_dir)
    import urllib.error
    import urllib.request

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ProviderError) as ei:
        AgentProvider(vault_dir).chat([{"role": "user", "content": "x"}], [])
    assert "SECRET-KEY" not in str(ei.value)
    assert "401" in str(ei.value)


# ---------------------------------------------------------------- REST /chat SSE
def test_rest_chat_sse(client, vault, monkeypatch):
    _script(monkeypatch, _call("kwey_create", {"path": "REST内核.md", "content": "x"}, "r"), _final("完成"))
    tk = TokenStore(vault.paths.root).issue("admin", "test")["token"]
    with client.stream("POST", "/api/agent/chat", headers={"X-Kwey-Token": tk},
                       json={"messages": USER}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    events = [json.loads(ln[len("data: "):]) for ln in lines]
    assert _types(events) == ["tool_call", "tool_result", "ui", "message", "done"]
    assert (vault.paths.ensure_root() / "REST内核.md").is_file()


def test_rest_chat_confirm_pause_and_resume(client, vault, monkeypatch):
    _script(monkeypatch, _call("kwey_dir_delete", {"path": "notes"}, "d"))
    tk = TokenStore(vault.paths.root).issue("admin", "test")["token"]
    with client.stream("POST", "/api/agent/chat", headers={"X-Kwey-Token": tk},
                       json={"messages": USER}) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    events = [json.loads(ln[len("data: "):]) for ln in lines]
    assert _types(events) == ["confirm_required", "done"]
    msgs = events[-1]["messages"]

    _script(monkeypatch, _final("已删除"))
    with client.stream("POST", "/api/agent/chat", headers={"X-Kwey-Token": tk},
                       json={"messages": msgs, "resume": {"id": "d"}}) as r:
        lines2 = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    events2 = [json.loads(ln[len("data: "):]) for ln in lines2]
    assert _types(events2) == ["tool_call", "tool_result", "message", "done"]
    assert events2[1]["ok"] is True
    assert any(i["is_dir"] for i in vault.list_trash())
