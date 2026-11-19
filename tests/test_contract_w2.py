"""卡A W2：新增 12 工具矩阵 + journal actor 收口 + confirm 闸门 + 权限分级测试。

全部走 conftest 临时 vault（vault / vault_dir / client fixtures），不污染项目根。
git 快照用例显式开启 snapshots.enabled（接线默认关闭，不擅自 git init）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent import journal, registry
from app.agent.auth import TokenStore
from app.agent.mcp_server import KweyMCPServer
from app.agent.registry import ContractError
from app.api import deps

AGENT = "/api/agent"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def agent_client(client):
    from app.api.routes_agent import router

    client.app.include_router(router)
    return client


@pytest.fixture
def admin_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("admin", "w2-admin")["token"]


@pytest.fixture
def write_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("write", "w2-write")["token"]


@pytest.fixture
def read_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("read", "w2-read")["token"]


def _h(token: str) -> dict:
    return {"X-Kwey-Token": token}


def _call(vault, name, args, actor=None):
    return registry.invoke(name, args, vault=vault, actor=actor)


# ---------------------------------------------------------------- tree / trash
def test_tree_shape(vault):
    tree = _call(vault, "kwey.tree", {})["tree"]
    assert isinstance(tree, list) and tree
    names = {e["name"] for e in tree}
    assert "notes" in names and "欢迎.md" in names
    assert not any(n.startswith(".") for n in names)  # .kwey/.trash/.git 隐藏
    dirs = [e for e in tree if e["type"] == "dir"]
    assert tree[: len(dirs)] == dirs  # 目录在前
    notes_dir = next(e for e in tree if e["name"] == "notes")
    assert any(c["name"] == "读书" for c in notes_dir["children"])


def test_trash_list_restore_purge_flow(vault):
    _call(vault, "kwey.create", {"path": "回收流程.md", "content": "回收站流程内容"})
    deleted = _call(vault, "kwey.delete", {"path": "回收流程.md"})
    trash_path = deleted["trash_path"]

    items = _call(vault, "kwey.trash.list", {})["items"]
    entry = next(i for i in items if i["trash_path"] == trash_path)
    assert entry["original_path"] == "回收流程.md"
    assert entry["is_dir"] is False

    restored = _call(vault, "kwey.trash.restore", {"trash_path": trash_path})
    assert restored["path"] == "回收流程.md"
    doc = _call(vault, "kwey.read", {"path": "回收流程.md"})
    assert "回收站流程内容" in doc["content"]

    # 再删 → purge 必须 confirm
    deleted2 = _call(vault, "kwey.delete", {"path": "回收流程.md"})
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.trash.purge", {"trash_path": deleted2["trash_path"]})
    assert ei.value.code == 400
    purged = _call(
        vault, "kwey.trash.purge",
        {"trash_path": deleted2["trash_path"], "confirm": True},
    )
    assert purged["purged"] == 1
    assert _call(vault, "kwey.trash.list", {})["items"] == []


def test_trash_restore_dedup(vault):
    _call(vault, "kwey.create", {"path": "去重.md", "content": "旧内容"})
    deleted = _call(vault, "kwey.delete", {"path": "去重.md"})
    _call(vault, "kwey.create", {"path": "去重.md", "content": "新内容"})
    res = _call(vault, "kwey.trash.restore", {"trash_path": deleted["trash_path"]})
    assert res["path"] != "去重.md"  # 自动去重，不覆盖现有
    doc = _call(vault, "kwey.read", {"path": "去重.md"})
    assert "新内容" in doc["content"]


def test_purge_all_confirm(vault):
    _call(vault, "kwey.create", {"path": "清一.md", "content": "a"})
    _call(vault, "kwey.create", {"path": "清二.md", "content": "b"})
    _call(vault, "kwey.delete", {"path": "清一.md"})
    _call(vault, "kwey.delete", {"path": "清二.md"})
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.trash.purge", {})
    assert ei.value.code == 400
    res = _call(vault, "kwey.trash.purge", {"confirm": True})
    assert res["purged"] == 2


# ---------------------------------------------------------------- append / move / rename
def test_append_preserves_content_and_frontmatter(vault):
    _call(vault, "kwey.create", {
        "path": "追加.md", "content": "原正文", "frontmatter": {"tags": ["甲"]},
    })
    res = _call(vault, "kwey.append", {"path": "追加.md", "text": "追加的一行"})
    assert res["updated"] is True
    doc = _call(vault, "kwey.read", {"path": "追加.md"})
    assert "原正文" in doc["content"]
    assert "追加的一行" in doc["content"]
    assert doc["frontmatter"]["tags"] == ["甲"]


def test_append_missing_note_404(vault):
    with pytest.raises(HTTPException) as ei:
        _call(vault, "kwey.append", {"path": "不存在.md", "text": "x"})
    assert ei.value.status_code == 404


def test_move_to_dir_and_root(vault):
    res = _call(vault, "kwey.move", {"path": "notes/读书/2026计划.md", "target": "归档"})
    assert res["new_path"] == "归档/2026计划.md"
    assert vault.db.get_note("归档/2026计划.md") is not None
    assert vault.db.get_note("notes/读书/2026计划.md") is None
    back = _call(vault, "kwey.move", {"path": "归档/2026计划.md", "target": ""})
    assert back["new_path"] == "2026计划.md"


def test_move_requires_target(vault):
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.move", {"path": "欢迎.md"})
    assert ei.value.code == 400


def test_rename_rewrites_links(vault):
    res = _call(vault, "kwey.rename", {"path": "notes/读书/2026计划.md", "name": "新年计划"})
    assert res["new_path"] == "notes/读书/新年计划.md"
    assert res["rewritten_links"] >= 1
    welcome = _call(vault, "kwey.read", {"path": "欢迎.md"})
    assert "[[新年计划" in welcome["content"]
    assert "[[2026计划" not in welcome["content"]


# ---------------------------------------------------------------- dir
def test_dir_create_conflict_and_delete(vault):
    assert _call(vault, "kwey.dir.create", {"path": "临时目录"})["path"] == "临时目录"
    with pytest.raises(HTTPException) as ei:
        _call(vault, "kwey.dir.create", {"path": "临时目录"})
    assert ei.value.status_code == 409
    # 空目录可直接删（无需 confirm）
    res = _call(vault, "kwey.dir.delete", {"path": "临时目录"})
    assert res["trash_path"].startswith(".trash/")

    # 非空目录必须 confirm，否则 409
    _call(vault, "kwey.dir.create", {"path": "临时目录2"})
    _call(vault, "kwey.create", {"path": "临时目录2/内含.md", "content": "x"})
    with pytest.raises(HTTPException) as ei:
        _call(vault, "kwey.dir.delete", {"path": "临时目录2"})
    assert ei.value.status_code == 409
    res2 = _call(vault, "kwey.dir.delete", {"path": "临时目录2", "confirm": True})
    items = _call(vault, "kwey.trash.list", {})["items"]
    entry = next(i for i in items if i["trash_path"] == res2["trash_path"])
    assert entry["is_dir"] is True


# ---------------------------------------------------------------- history（git 快照）
def test_history_log_and_rollback(vault):
    vault.config.set({"snapshots": {"enabled": True, "auto_commit": False}})
    snap = deps.require_snapshots()
    rel = "快照笔记.md"
    _call(vault, "kwey.create", {"path": rel, "content": "第一版"})
    h1 = snap.commit("v1")
    _call(vault, "kwey.write", {"path": rel, "content": "第二版"})
    snap.commit("v2")
    assert h1

    entries = _call(vault, "kwey.history.log", {"path": rel})["entries"]
    assert len(entries) >= 2

    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.history.rollback", {"hash": h1, "path": rel})
    assert ei.value.code == 400

    actor = {"role": "write", "name": "w2-tester"}
    res = _call(
        vault, "kwey.history.rollback",
        {"hash": h1, "path": rel, "confirm": True}, actor=actor,
    )
    assert res["saved_commit"]
    doc = _call(vault, "kwey.read", {"path": rel})
    assert "第一版" in doc["content"]
    assert "第二版" not in doc["content"]
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert any(
        e["action"] == "rollback" and e["name"] == "w2-tester" and e["path"] == rel
        for e in recent
    )


def test_history_log_limit_bounds(vault):
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.history.log", {"limit": 0})
    assert ei.value.code == 400
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.history.log", {"limit": 201})
    assert ei.value.code == 400


# ---------------------------------------------------------------- organize.apply
def test_organize_apply_gate_and_journal(vault):
    ops = [
        {"type": "create", "path": "整理/新篇.md", "content": "整理新篇"},
        {"type": "append", "path": "欢迎.md", "text": "整理追加"},
    ]
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.organize.apply", {"ops": ops})
    assert ei.value.code == 400

    actor = {"role": "write", "name": "w2-organizer"}
    res = _call(vault, "kwey.organize.apply", {"ops": ops, "confirm": True}, actor=actor)
    assert res["applied"] == 2
    assert res["paths"] == ["整理/新篇.md", "欢迎.md"]
    recent = journal.recent(vault.paths.ensure_root(), 5)
    organize_entries = [e for e in recent if e["action"] == "organize"]
    assert {e["path"] for e in organize_entries} == {"整理/新篇.md", "欢迎.md"}
    assert all(e["name"] == "w2-organizer" for e in organize_entries)


def test_organize_apply_invalid_ops(vault):
    with pytest.raises(ContractError) as ei:
        _call(vault, "kwey.organize.apply", {"ops": "not-array", "confirm": True})
    assert ei.value.code == 400
    with pytest.raises(HTTPException) as ei:
        _call(vault, "kwey.organize.apply", {
            "ops": [{"type": "nope", "path": "x.md"}], "confirm": True,
        })
    assert ei.value.status_code == 400


# ---------------------------------------------------------------- journal actor 收口
def test_invoke_actor_none_no_journal(vault):
    _call(vault, "kwey.create", {"path": "无痕.md", "content": "x"})
    assert journal.recent(vault.paths.ensure_root(), 5) == []


def test_invoke_actor_journal(vault):
    _call(vault, "kwey.create", {"path": "留痕.md", "content": "x"})
    _call(vault, "kwey.append", {"path": "留痕.md", "text": "y"},
          actor={"role": "write", "name": "w2"})
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert recent[0]["action"] == "append"
    assert recent[0]["path"] == "留痕.md"
    assert recent[0]["role"] == "write"
    assert recent[0]["name"] == "w2"


def test_mcp_stdio_actor_journal(vault):
    server = KweyMCPServer(vault)
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "kwey.create", "arguments": {"path": "MCP留痕.md", "content": "x"}},
    })
    assert resp["result"]["isError"] is False
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert any(
        e["name"] == "mcp-stdio" and e["role"] == "admin" and e["action"] == "create"
        for e in recent
    )


# ---------------------------------------------------------------- REST /tool 权限 + actor
def test_tool_permission_matrix(agent_client, read_token, write_token, admin_token):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(read_token), json={
        "name": "kwey.append", "arguments": {"path": "欢迎.md", "text": "x"},
    })
    assert r.status_code == 403
    r = agent_client.post(f"{AGENT}/tool", headers=_h(write_token), json={
        "name": "kwey.trash.purge", "arguments": {"confirm": True},
    })
    assert r.status_code == 403
    r = agent_client.post(f"{AGENT}/tool", headers=_h(admin_token), json={
        "name": "kwey.trash.purge", "arguments": {"confirm": True},
    })
    assert r.status_code == 200
    assert r.json()["data"]["purged"] == 0
    r = agent_client.post(f"{AGENT}/tool", headers=_h(write_token), json={
        "name": "kwey.append", "arguments": {"path": "欢迎.md", "text": "权限追加"},
    })
    assert r.status_code == 200


def test_tool_journal_actor(agent_client, write_token, vault):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(write_token), json={
        "name": "kwey.create", "arguments": {"path": "REST留痕.md", "content": "x"},
    })
    assert r.status_code == 200
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert recent[0]["name"] == "w2-write"
    assert recent[0]["action"] == "create"


def test_legacy_note_journal_actor(agent_client, write_token, vault):
    r = agent_client.post(f"{AGENT}/note", headers=_h(write_token),
                          json={"path": "旧端点留痕.md", "content": "x"})
    assert r.status_code == 200
    recent = journal.recent(vault.paths.ensure_root(), 5)
    assert recent[0]["action"] == "create"
    assert recent[0]["name"] == "w2-write"


def test_tool_confirm_gate_400(agent_client, write_token):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(write_token), json={
        "name": "kwey.history.rollback",
        "arguments": {"hash": "abc1234", "path": "欢迎.md"},
    })
    assert r.status_code == 400