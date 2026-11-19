"""M2-2：Agent Link REST/SSE 接口 + token 三级鉴权 集成测试。

复用 conftest 的 client / services / vault_dir fixtures。由于 server.py 的
include_router 由编排者接线，测试自行把本卡片 router 挂到 client.app 上，
从而仍能经过 server.py 的统一异常包装（401/403 的 {code,data,message}）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent.auth import ROLE_LEVEL, TokenStore
from app.api import deps
from app.api.routes_agent import router, sse_event_stream
from app.events import EventType, VaultEvent

AGENT = "/api/agent"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def agent_client(client):
    """在 conftest 的 client 上挂载本卡片 router。

    create_app() 已注册统一异常包装；app.include_router 在 TestClient 运行时
    追加同样生效（fastapi 0.162 验证通过）。若编排者已在 server.py 接线，
    重复 include 相同前缀的路由无害（请求匹配第一条）。
    """
    client.app.include_router(router)
    return client


@pytest.fixture
def admin_token(vault_dir: Path) -> str:
    """引导：模拟 CLI / 本地 UI 直接签发首个 admin token。"""
    return TokenStore(vault_dir).issue("admin", "test-admin")["token"]


@pytest.fixture
def read_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("read", "test-read")["token"]


def _h(token: str) -> dict:
    return {"X-Kwey-Token": token}


# ---------------------------------------------------------------- 401 / 403
def test_no_token_returns_401(agent_client):
    resp = agent_client.get(f"{AGENT}/health")
    assert resp.status_code == 401
    assert resp.json()["code"] == 401
    assert resp.json()["data"] is None


def test_invalid_token_returns_401(agent_client):
    resp = agent_client.get(f"{AGENT}/health", headers=_h("not-a-real-token"))
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


def test_read_token_cannot_write_returns_403(agent_client, read_token):
    resp = agent_client.post(
        f"{AGENT}/note",
        headers=_h(read_token),
        json={"path": "禁止写入.md", "content": "x"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 403


def test_tokens_endpoint_requires_admin(agent_client, read_token):
    resp = agent_client.post(
        f"{AGENT}/tokens", headers=_h(read_token), json={"action": "list"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- health / 取 token 方式
def test_health_returns_note_count(agent_client, admin_token, vault_dir):
    resp = agent_client.get(f"{AGENT}/health", headers=_h(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "ok"
    data = body["data"]
    # conftest vault 固定 3 篇 md：欢迎.md / notes/读书/2026计划.md / notes/工作/会议记录.md
    assert data["note_count"] == 3
    assert data["role"] == "admin"
    assert data["vault"] and vault_dir.name in data["vault"]


def test_bearer_header_and_read_endpoints(agent_client, read_token):
    """Authorization: Bearer 取 token；顺带覆盖 note / search / graph 读端点。"""
    h = {"Authorization": f"Bearer {read_token}"}

    health = agent_client.get(f"{AGENT}/health", headers=h)
    assert health.status_code == 200
    assert health.json()["data"]["role"] == "read"

    doc = agent_client.get(f"{AGENT}/note", params={"path": "欢迎.md"}, headers=h)
    assert doc.status_code == 200
    assert "# 欢迎" in doc.json()["data"]["content"]
    assert doc.json()["data"]["path"] == "欢迎.md"

    search = agent_client.get(
        f"{AGENT}/search", params={"q": "欢迎", "limit": 10}, headers=h
    )
    assert search.status_code == 200
    assert "hits" in search.json()["data"]

    graph = agent_client.get(
        f"{AGENT}/graph", params={"path": "欢迎.md", "depth": 1}, headers=h
    )
    assert graph.status_code == 200
    gdata = graph.json()["data"]
    assert "nodes" in gdata and "edges" in gdata


# ---------------------------------------------------------------- 核心链路
def test_admin_issue_write_token_creates_note_then_revoke(
    agent_client, admin_token, vault_dir
):
    """admin issue → write token 建笔记且磁盘存在 → revoke → 401。"""
    ah = _h(admin_token)

    issued = agent_client.post(
        f"{AGENT}/tokens",
        headers=ah,
        json={"action": "issue", "role": "write", "name": "agent"},
    )
    assert issued.status_code == 200
    write_token = issued.json()["data"]["token"]
    assert write_token and issued.json()["data"]["role"] == "write"

    listed = agent_client.post(f"{AGENT}/tokens", headers=ah, json={"action": "list"})
    assert listed.status_code == 200
    # A4：list 脱敏（id=hash 前 8），不再返回明文 token
    listed_items = listed.json()["data"]["tokens"]
    assert any(t.get("role") == "write" and t.get("name") == "agent" for t in listed_items)
    assert all("token" not in t for t in listed_items)

    # 持久化文件只存 hash + salt，明文 token 绝不可落盘
    tokens_file = vault_dir / ".kwey" / "tokens.json"
    assert tokens_file.is_file()
    raw = tokens_file.read_text(encoding="utf-8")
    assert write_token not in raw
    assert "token_hash" in raw and "salt" in raw

    wh = _h(write_token)
    created = agent_client.post(
        f"{AGENT}/note",
        headers=wh,
        json={"path": "agent/新笔记.md", "content": "# 由 Agent 创建\n"},
    )
    assert created.status_code == 200
    assert created.json()["code"] == 0
    assert (vault_dir / "agent" / "新笔记.md").is_file()

    read_back = agent_client.get(
        f"{AGENT}/note", params={"path": "agent/新笔记.md"}, headers=wh
    )
    assert read_back.status_code == 200
    assert "由 Agent 创建" in read_back.json()["data"]["content"]

    revoked = agent_client.post(
        f"{AGENT}/tokens", headers=ah, json={"action": "revoke", "token": write_token}
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["revoked"] is True

    after = agent_client.get(f"{AGENT}/health", headers=wh)
    assert after.status_code == 401
    assert after.json()["code"] == 401


def test_write_endpoints_put_link_tags_delete(agent_client, admin_token, vault_dir):
    """覆盖 PUT /note、/link、/tags、DELETE /note（admin 等级涵盖 write）。"""
    h = _h(admin_token)
    made = agent_client.post(
        f"{AGENT}/note",
        headers=h,
        json={
            "path": "ops.md",
            "content": "初始正文\n",
            "frontmatter": {"tags": ["原标签"]},
        },
    )
    assert made.status_code == 200

    put = agent_client.put(
        f"{AGENT}/note",
        headers=h,
        json={"path": "ops.md", "content": "改写正文\n"},
    )
    assert put.status_code == 200
    assert put.json()["data"]["updated"] is True

    tags = agent_client.post(
        f"{AGENT}/tags",
        headers=h,
        json={"path": "ops.md", "add": ["新标签"], "remove": ["原标签"]},
    )
    assert tags.status_code == 200
    assert tags.json()["data"]["tags"] == ["新标签"]

    link = agent_client.post(
        f"{AGENT}/link", headers=h, json={"from": "ops.md", "to": "欢迎"}
    )
    assert link.status_code == 200
    doc = agent_client.get(f"{AGENT}/note", params={"path": "ops.md"}, headers=h)
    assert "[[欢迎]]" in doc.json()["data"]["content"]

    deleted = agent_client.delete(f"{AGENT}/note", params={"path": "ops.md"}, headers=h)
    assert deleted.status_code == 200
    assert deleted.json()["data"]["trash_path"].startswith(".trash/")
    assert not (vault_dir / "ops.md").exists()


# ---------------------------------------------------------------- SSE（不依赖流式 TestClient）
def test_sse_stream_first_frame(services):
    """直接测生成器首个 yield：严格首帧 + 不挂测试。"""
    async def take():
        gen = sse_event_stream()
        first = await gen.__anext__()
        await gen.aclose()  # 触发 finally 的 unsubscribe，避免订阅残留
        return first

    frame = asyncio.run(take())
    assert frame == 'data: {"event":"connected"}\n\n'


def test_sse_stream_delivers_vault_event(services):
    """发布一条 VaultEvent，验证跨线程投递写法能把事件推成 SSE 帧。"""
    async def take():
        bus = deps.require_eventbus()
        gen = sse_event_stream()
        await gen.__anext__()  # connected
        bus.publish(VaultEvent(EventType.CREATED, path="agent/event.md"))
        second = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        await gen.aclose()
        return second

    frame = asyncio.run(take())
    assert frame.startswith("data: ")
    assert '"event":"created"' in frame
    assert '"path":"agent/event.md"' in frame
    assert frame.endswith("\n\n")


def test_events_requires_read_token(agent_client):
    """/events 是无限流，不用 client.stream；用 401 反证路由已挂载且鉴权生效。

    若 router 未挂载会返回 404，故 401 同时证明端点存在 + 需要 read 级 token。
    """
    no_token = agent_client.get(f"{AGENT}/events")
    assert no_token.status_code == 401
    assert no_token.json()["code"] == 401


# ---------------------------------------------------------------- TokenStore 单元边界
def test_token_store_missing_file_is_empty(tmp_path: Path):
    assert TokenStore(tmp_path).list() == []


def test_token_store_corrupt_file_is_empty(tmp_path: Path):
    p = tmp_path / ".kwey" / "tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken json", encoding="utf-8")
    assert TokenStore(tmp_path).list() == []
    with pytest.raises(HTTPException) as ei:
        TokenStore(tmp_path).verify("whatever", "read")
    assert ei.value.status_code == 401


def test_role_levels_and_issue_validation():
    assert ROLE_LEVEL == {"read": 1, "write": 2, "admin": 3}
    with pytest.raises(HTTPException) as ei:
        TokenStore(Path(".")).issue("root", "x")
    assert ei.value.status_code == 400
