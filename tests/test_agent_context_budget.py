"""卡 C：上下文预算（kwey.read 截断续读 + 历史编译）。

覆盖：read 默认截断 / offset 续读 / 精确窗口 / 参数校验 / 短笔记不变；
_compile_history 的预算边界、user 切分、tool 调用对完整、账本不可变；
run_chat 发给 provider 的是派生视图，done 仍回完整账本，且 context_trim 只发一次。
"""

from __future__ import annotations

import pytest

from app.agent import kernel, registry
from app.agent.provider import AgentProvider

LONG = "甲" * 12000


# ---------------------------------------------------------------- kwey.read
def _read(vault, **args):
    return registry.invoke("kwey.read", args, vault=vault)


def test_read_truncates_by_default(vault):
    vault.create_note("长文.md", content=LONG)
    doc = _read(vault, path="长文.md")
    assert len(doc["content"]) == 8000
    assert doc["offset"] == 0
    assert doc["total_chars"] == 12000
    assert doc["truncated"] is True
    assert doc["title"] == "长文"


def test_read_offset_continues(vault):
    vault.create_note("长文.md", content=LONG)
    first = _read(vault, path="长文.md")
    second = _read(vault, path="长文.md", offset=8000)
    assert len(second["content"]) == 4000
    assert second["truncated"] is False
    assert second["offset"] == 8000
    assert first["content"] + second["content"] == LONG


def test_read_exact_window(vault):
    vault.create_note("长文.md", content=LONG)
    doc = _read(vault, path="长文.md", offset=100, max_chars=50)
    assert doc["content"] == LONG[100:150]
    assert doc["offset"] == 100
    assert doc["truncated"] is True


def test_read_offset_beyond_end(vault):
    vault.create_note("长文.md", content=LONG)
    doc = _read(vault, path="长文.md", offset=999999, max_chars=100)
    assert doc["content"] == ""
    assert doc["offset"] == 12000       # 收敛到总长，不越界
    assert doc["truncated"] is False


@pytest.mark.parametrize("args", [
    {"offset": -1},
    {"offset": True},
    {"offset": "10"},
    {"max_chars": 0},
    {"max_chars": 40001},
    {"max_chars": True},
])
def test_read_rejects_bad_window_args(vault, args):
    vault.create_note("长文.md", content=LONG)
    with pytest.raises(registry.ContractError) as ei:
        _read(vault, path="长文.md", **args)
    assert ei.value.code == 400


def test_read_short_note_unchanged(vault):
    vault.create_note("短文.md", content="hello")
    doc = _read(vault, path="短文.md")
    assert doc["content"] == "hello"
    assert doc["total_chars"] == 5
    assert doc["truncated"] is False


# ---------------------------------------------------------------- 历史编译
def _turn(i: int, size: int = 2000):
    return [
        {"role": "user", "content": f"问题{i}" + "甲" * size},
        {"role": "assistant", "content": f"回答{i}" + "乙" * size},
    ]


def test_compile_keeps_short_history_same_object():
    msgs = [{"role": "system", "content": "S"}] + _turn(0, 10)
    assert kernel._compile_history(msgs) is msgs


def test_compile_trims_at_user_boundary():
    msgs = [{"role": "system", "content": "S"}]
    for i in range(30):
        msgs.extend(_turn(i))

    wire = kernel._compile_history(msgs)
    assert wire[0]["role"] == "system"
    assert wire[1]["role"] == "user"
    assert wire[-1] is msgs[-1]
    assert len(wire) < len(msgs)
    assert sum(kernel._msg_chars(m) for m in wire) <= kernel.HISTORY_CHAR_BUDGET + len(kernel.OMISSION_NOTE) + 8
    assert "较早的" in wire[0]["content"]


def test_compile_never_splits_tool_pairs():
    msgs = [{"role": "system", "content": "S"}]
    for i in range(12):
        msgs.append({"role": "user", "content": f"任务{i}" + "甲" * 1200})
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{i}", "name": "kwey_read", "arguments": {"path": "x.md"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "乙" * 2000})
        msgs.append({"role": "assistant", "content": "完成" + "丙" * 1200})

    wire = kernel._compile_history(msgs)
    assert wire[1]["role"] == "user"
    assert all(m.get("role") != "tool" for m in wire[:2])
    for i, m in enumerate(wire):
        if m.get("role") != "tool":
            continue
        prev = wire[i - 1]
        assert prev.get("role") == "assistant", "tool 结果前必须是发起调用的 assistant"
        assert m["tool_call_id"] in {c["id"] for c in prev.get("tool_calls") or []}


def test_compile_does_not_mutate_ledger():
    msgs = [{"role": "system", "content": "S"}]
    for i in range(20):
        msgs.extend(_turn(i))
    before = [dict(m) for m in msgs]
    kernel._compile_history(msgs)
    assert msgs == before
    assert "较早的" not in msgs[0]["content"]


# ---------------------------------------------------------------- run_chat
def _long_history():
    msgs = [{"role": "user", "content": "最早的问题" + "甲" * 300}]
    for i in range(20):
        msgs.extend(_turn(i))
    return msgs


def test_run_chat_uses_compiled_view_but_returns_full_ledger(vault, monkeypatch):
    captured = {}

    def fake_chat(self, messages, tools):
        captured["messages"] = messages
        return {"content": "收尾", "tool_calls": []}

    monkeypatch.setattr(AgentProvider, "chat", fake_chat)
    msgs = _long_history()
    events = list(kernel.run_chat(msgs, vault=vault))

    done = events[-1]
    assert done["type"] == "done"
    # 账本：system + 全部原始消息 + 最终 assistant
    assert len(done["messages"]) == len(msgs) + 2
    assert any("最早的问题" in str(m.get("content")) for m in done["messages"])
    # provider 只看到裁剪后的派生视图
    assert len(captured["messages"]) < len(done["messages"])

    trims = [e for e in events if e["type"] == "context_trim"]
    assert len(trims) == 1
    assert trims[0]["dropped_messages"] > 0
    assert trims[0]["budget"] == kernel.HISTORY_CHAR_BUDGET
    assert events.index(trims[0]) < events.index(next(e for e in events if e["type"] == "message"))


def test_run_chat_no_trim_event_for_short_history(vault, monkeypatch):
    monkeypatch.setattr(
        AgentProvider,
        "chat",
        lambda self, messages, tools: {"content": "好", "tool_calls": []},
    )
    events = list(kernel.run_chat([{"role": "user", "content": "你好"}], vault=vault))
    assert [e for e in events if e["type"] == "context_trim"] == []
    assert events[-1]["type"] == "done"
