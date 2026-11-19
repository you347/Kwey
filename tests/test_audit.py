"""写入归因（audit）：journal.query 过滤 + GET /api/agent/audit。

外部 agent（如 verbot）用自己的 token 写入后，必须能按 token name 精确定位
它改过哪些笔记——这是「掌控」的可核查前提，也是 MCP 之外最便宜的归因手段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import journal
from app.agent.auth import TokenStore
from app.api.routes_agent import router

AGENT = "/api/agent"


@pytest.fixture
def agent_client(client):
    client.app.include_router(router)
    return client


def _h(token: str) -> dict:
    return {"X-Kwey-Token": token}


def _issue(vault_dir: Path, role: str, name: str) -> str:
    return TokenStore(vault_dir).issue(role, name)["token"]


# ---------------------------------------------------------------- journal.query
def test_query_returns_newest_first(tmp_path):
    for i in range(3):
        journal.append(tmp_path, {"name": "a", "role": "write", "action": "create", "path": f"n{i}.md"})
    rows = journal.query(tmp_path, limit=10)
    assert [r["path"] for r in rows] == ["n2.md", "n1.md", "n0.md"]


def test_query_filters_by_actor(tmp_path):
    journal.append(tmp_path, {"name": "verbot", "role": "write", "action": "create", "path": "v.md"})
    journal.append(tmp_path, {"name": "kwey-ui", "role": "write", "action": "create", "path": "u.md"})
    rows = journal.query(tmp_path, actor="verbot")
    assert [r["path"] for r in rows] == ["v.md"]


def test_query_filters_by_path_substring(tmp_path):
    journal.append(tmp_path, {"name": "a", "role": "write", "action": "write", "path": "projects/x.md"})
    journal.append(tmp_path, {"name": "a", "role": "write", "action": "write", "path": "daily/y.md"})
    rows = journal.query(tmp_path, path="projects/")
    assert [r["path"] for r in rows] == ["projects/x.md"]


def test_query_filters_by_since(tmp_path):
    import time

    journal.append(tmp_path, {"name": "a", "role": "write", "action": "write", "path": "old.md"})
    assert len(journal.query(tmp_path, since=1)) == 1  # ts 为 time.time()，必然 > 1
    assert journal.query(tmp_path, since=time.time() + 3600) == []


def test_query_respects_limit(tmp_path):
    for i in range(20):
        journal.append(tmp_path, {"name": "a", "role": "write", "action": "create", "path": f"n{i}.md"})
    assert len(journal.query(tmp_path, limit=5)) == 5


def test_query_missing_file_is_empty(tmp_path):
    assert journal.query(tmp_path) == []


# ---------------------------------------------------------------- REST 端点
def test_audit_attributes_by_token_name(agent_client, vault_dir: Path):
    write = _issue(vault_dir, "write", "verbot")
    created = agent_client.post(
        f"{AGENT}/note", json={"path": "from-verbot.md", "content": "# hi"}, headers=_h(write)
    )
    assert created.status_code == 200, created.text

    res = agent_client.get(f"{AGENT}/audit", params={"actor": "verbot"}, headers=_h(write))
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["count"] >= 1
    assert any(e["path"] == "from-verbot.md" for e in data["entries"])
    assert {e["name"] for e in data["entries"]} == {"verbot"}


def test_audit_does_not_mix_actors(agent_client, vault_dir: Path):
    verbot = _issue(vault_dir, "write", "verbot")
    other = _issue(vault_dir, "write", "other-agent")
    agent_client.post(f"{AGENT}/note", json={"path": "a.md", "content": "a"}, headers=_h(verbot))
    agent_client.post(f"{AGENT}/note", json={"path": "b.md", "content": "b"}, headers=_h(other))

    rows = agent_client.get(f"{AGENT}/audit", params={"actor": "verbot"}, headers=_h(verbot)).json()["data"]
    assert [e["path"] for e in rows["entries"]] == ["a.md"]


def test_audit_requires_token(agent_client):
    assert agent_client.get(f"{AGENT}/audit").status_code == 401


def test_audit_rejects_bad_role(agent_client, vault_dir: Path):
    read = _issue(vault_dir, "read", "reader")
    res = agent_client.get(f"{AGENT}/audit", params={"role": "root"}, headers=_h(read))
    assert res.status_code == 400


def test_audit_empty_vault_returns_zero(agent_client, vault_dir: Path):
    read = _issue(vault_dir, "read", "reader")
    res = agent_client.get(f"{AGENT}/audit", headers=_h(read))
    assert res.status_code == 200
    assert res.json()["data"]["count"] == 0
