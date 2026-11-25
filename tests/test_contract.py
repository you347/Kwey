"""卡A W1：能力契约 registry + 双传输改薄 parity 测试。

覆盖：契约形状 / schema 边界校验 / invoke 往返 / canonical 语义收敛 /
MCP 错误映射不变 / REST /contract /tool 鉴权与留痕 / 旧端点响应键兼容。
M2 既有测试（test_m2_*）保持原样绿，是 parity 的硬门禁；本文件只加新断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent import journal, registry
from app.agent.auth import TokenStore
from app.agent.mcp_server import KweyMCPServer
from app.agent.registry import ContractError

AGENT = "/api/agent"

READ_TOOLS = {
    "kwey.search", "kwey.read", "kwey.open", "kwey.graph.query", "kwey.organize",
    "kwey.tree", "kwey.trash.list", "kwey.history.log", "kwey.audit",
    # W4：结构化视图 / 知识体检
    "kwey.views.list", "kwey.views.query", "kwey.diagnose",
}
WRITE_TOOLS = {
    "kwey.create", "kwey.write", "kwey.delete", "kwey.link", "kwey.tags",
    "kwey.append", "kwey.move", "kwey.rename", "kwey.dir.create", "kwey.dir.delete",
    "kwey.trash.restore", "kwey.history.rollback", "kwey.organize.apply",
    # W4：视图落盘 / 快速捕获
    "kwey.views.save", "kwey.capture",
    # P2-C：会话蒸馏（只产出待审草案）
    "kwey.session.distill",
}
ADMIN_TOOLS = {"kwey.trash.purge"}
EXPECTED_TOOLS = READ_TOOLS | WRITE_TOOLS | ADMIN_TOOLS


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def agent_client(client):
    from app.api.routes_agent import router

    client.app.include_router(router)
    return client


@pytest.fixture
def admin_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("admin", "contract-admin")["token"]


@pytest.fixture
def read_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("read", "contract-read")["token"]


@pytest.fixture
def write_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("write", "contract-write")["token"]


def _h(token: str) -> dict:
    return {"X-Kwey-Token": token}


def _rpc(server, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def _call(server, name, args):
    resp = _rpc(server, "tools/call", {"name": name, "arguments": args})
    assert "error" not in resp, resp
    return resp["result"], json.loads(resp["result"]["content"][0]["text"])


# ---------------------------------------------------------------- 契约形状
def test_contract_shape():
    c = registry.contract()
    assert c["version"] == 1
    assert {t["name"] for t in c["tools"]} == EXPECTED_TOOLS
    for t in c["tools"]:
        assert t["description"]
        assert t["input_schema"]["type"] == "object"
        assert t["permission"] in ("read", "write", "admin")
        assert t["side_effect"] in ("read", "reversible", "irreversible")
        assert "handler" not in t  # 契约快照不泄漏可调用对象
        if t["name"] in WRITE_TOOLS:
            assert t["permission"] == "write"
            assert t["side_effect"] == "reversible"
            assert t["journal_action"]
        elif t["name"] in ADMIN_TOOLS:
            assert t["permission"] == "admin"
            assert t["side_effect"] == "irreversible"
            assert t["journal_action"]
        else:
            assert t["name"] in READ_TOOLS
            assert t["permission"] == "read"
            assert t["side_effect"] == "read"
            assert t["journal_action"] is None


def test_mcp_tools_list_derives_from_contract(vault):
    server = KweyMCPServer(vault)
    tools = _rpc(server, "tools/list")["result"]["tools"]
    by_name = {t["name"]: t for t in registry.contract()["tools"]}
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t["description"] == by_name[t["name"]]["description"]
        assert t["inputSchema"] == by_name[t["name"]]["input_schema"]


def test_get_tool_unknown_raises_404():
    with pytest.raises(ContractError) as ei:
        registry.get_tool("kwey.nope")
    assert ei.value.code == 404
    assert "未知工具" in ei.value.message


# ---------------------------------------------------------------- invoke 往返 + 校验
def test_invoke_roundtrip(vault):
    created = registry.invoke("kwey.create", {"path": "契约/往返.md", "content": "契约初始内容"}, vault=vault)
    assert created["path"] == "契约/往返.md"
    doc = registry.invoke("kwey.read", {"path": "契约/往返.md"}, vault=vault)
    assert "契约初始内容" in doc["content"]
    wrote = registry.invoke("kwey.write", {"path": "契约/往返.md", "content": "契约改写内容"}, vault=vault)
    assert wrote["updated"] is True
    found = registry.invoke("kwey.search", {"query": "契约改写"}, vault=vault)
    assert "契约/往返.md" in [h["path"] for h in found["hits"]]
    deleted = registry.invoke("kwey.delete", {"path": "契约/往返.md"}, vault=vault)
    assert deleted["trash_path"].startswith(".trash/")


def test_invoke_defaults_to_deps_singleton(services):
    from app.api import deps

    doc = registry.invoke("kwey.read", {"path": "欢迎.md"})
    assert doc["path"] == "欢迎.md"
    assert registry.invoke("kwey.read", {"path": "欢迎.md"}, vault=deps.require_vault()) == doc


@pytest.mark.parametrize("name,args", [
    ("kwey.read", {}),                          # 缺必填
    ("kwey.read", {"path": None}),              # 必填为 None
    ("kwey.read", {"path": 123}),               # 类型错误
    ("kwey.read", {"path": "  "}),              # 非空约束（handler 层）
    ("kwey.search", {"query": "x", "mode": "bogus"}),   # enum
    ("kwey.search", {"query": "x", "limit": 0}),        # minimum
    ("kwey.search", {"query": "x", "limit": 201}),      # maximum
    ("kwey.search", {"query": "x", "limit": "10"}),     # integer 严格
    ("kwey.graph.query", {"path": "欢迎.md", "depth": 6}),
    ("kwey.tags", {"path": "欢迎.md", "add": [1]}),     # 数组元素类型
    ("kwey.create", {"path": "x.md", "frontmatter": "str"}),
    ("kwey.write", {"path": "欢迎.md"}),        # 缺 content
])
def test_invoke_validation_errors(vault, name, args):
    with pytest.raises(ContractError) as ei:
        registry.invoke(name, args, vault=vault)
    assert ei.value.code == 400


def test_invoke_arguments_must_be_dict(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.read", [1, 2], vault=vault)  # type: ignore[arg-type]
    assert ei.value.code == 400


def test_invoke_unknown_tool(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.nope", {}, vault=vault)
    assert ei.value.code == 404


def test_optional_none_treated_as_absent(vault):
    created = registry.invoke(
        "kwey.create", {"path": "可选None.md", "content": "x", "frontmatter": None}, vault=vault
    )
    assert created["path"] == "可选None.md"
    wrote = registry.invoke(
        "kwey.write", {"path": "可选None.md", "content": "y", "base_mtime": None}, vault=vault
    )
    assert wrote["updated"] is True


# ---------------------------------------------------------------- canonical 语义收敛
def test_write_empty_content_clears_body(vault):
    registry.invoke("kwey.create", {"path": "清空.md", "content": "有内容"}, vault=vault)
    registry.invoke("kwey.write", {"path": "清空.md", "content": ""}, vault=vault)
    doc = registry.invoke("kwey.read", {"path": "清空.md"}, vault=vault)
    assert doc["content"].strip() == ""
    # MCP 同语义（W1 收敛：旧 MCP 拒绝空 content，现与 REST 一致放行）
    _, payload = _call(KweyMCPServer(vault), "kwey.write", {"path": "清空.md", "content": ""})
    assert payload["code"] == 0


def test_search_empty_query_browses(vault):
    data = registry.invoke("kwey.search", {"query": ""}, vault=vault)
    assert data["total"] >= 3  # conftest 固定 3 篇
    assert data["mode"] == "fts"


def test_tags_single_string_leniency(vault):
    data = registry.invoke("kwey.tags", {"path": "欢迎.md", "add": "solo标签"}, vault=vault)
    assert "solo标签" in data["tags"]
    assert "入门/指南" in data["tags"]  # 原有标签保留


def test_business_http_exception_propagates(vault):
    with pytest.raises(HTTPException) as ei:
        registry.invoke("kwey.read", {"path": "不存在.md"}, vault=vault)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------- MCP 错误映射不变
def test_mcp_contract_error_maps_to_32602(vault):
    server = KweyMCPServer(vault)
    resp = _rpc(server, "tools/call", {"name": "kwey.search", "arguments": {"query": "x", "mode": "bogus"}})
    assert resp["error"]["code"] == -32602
    resp = _rpc(server, "tools/call", {"name": "kwey.read", "arguments": {"path": 123}})
    assert resp["error"]["code"] == -32602


def test_mcp_business_error_maps_to_is_error(vault):
    tool_res, payload = _call(KweyMCPServer(vault), "kwey.read", {"path": "不存在.md"})
    assert tool_res["isError"] is True
    assert payload["code"] == 404


# ---------------------------------------------------------------- REST /contract
def test_contract_requires_token(agent_client):
    assert agent_client.get(f"{AGENT}/contract").status_code == 401


def test_contract_returns_snapshot(agent_client, read_token):
    resp = agent_client.get(f"{AGENT}/contract", headers=_h(read_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["version"] == 1
    assert {t["name"] for t in data["tools"]} == EXPECTED_TOOLS


# ---------------------------------------------------------------- REST /tool
def test_tool_requires_token(agent_client):
    resp = agent_client.post(f"{AGENT}/tool", json={"name": "kwey.read", "arguments": {"path": "欢迎.md"}})
    assert resp.status_code == 401


def test_tool_read_with_read_token(agent_client, read_token):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(read_token),
        json={"name": "kwey.read", "arguments": {"path": "欢迎.md"}},
    )
    assert resp.status_code == 200
    assert "# 欢迎" in resp.json()["data"]["content"]


def test_tool_write_with_read_token_403(agent_client, read_token):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(read_token),
        json={"name": "kwey.create", "arguments": {"path": "禁止.md"}},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 403


def test_tool_unknown_404(agent_client, write_token):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(write_token), json={"name": "kwey.nope", "arguments": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_tool_validation_400(agent_client, write_token):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(write_token), json={"name": "kwey.create", "arguments": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 400


def test_tool_create_journals(agent_client, write_token, vault_dir):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(write_token),
        json={"name": "kwey.create", "arguments": {"path": "tool/新建.md", "content": "经 /tool"}},
    )
    assert resp.status_code == 200
    assert (vault_dir / "tool" / "新建.md").is_file()
    entries = journal.recent(vault_dir, limit=1)
    assert entries[0]["action"] == "create"
    assert entries[0]["path"] == "tool/新建.md"
    assert entries[0]["name"] == "contract-write"
    assert entries[0]["role"] == "write"


def test_tool_link_journals_from_path(agent_client, write_token, vault_dir):
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(write_token),
        json={"name": "kwey.link", "arguments": {"from": "欢迎.md", "to": "tool目标"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["link"] == "tool目标"
    entries = journal.recent(vault_dir, limit=1)
    assert entries[0]["action"] == "link"
    assert entries[0]["path"] == "欢迎.md"


def test_tool_read_does_not_journal(agent_client, read_token, vault_dir):
    before = journal.recent(vault_dir, limit=500)
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(read_token),
        json={"name": "kwey.read", "arguments": {"path": "欢迎.md"}},
    )
    assert resp.status_code == 200
    assert journal.recent(vault_dir, limit=500) == before


def test_tool_delete_with_admin(agent_client, admin_token, vault_dir):
    agent_client.post(
        f"{AGENT}/tool", headers=_h(admin_token),
        json={"name": "kwey.create", "arguments": {"path": "待删.md"}},
    )
    resp = agent_client.post(
        f"{AGENT}/tool", headers=_h(admin_token),
        json={"name": "kwey.delete", "arguments": {"path": "待删.md"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["trash_path"].startswith(".trash/")
    assert not (vault_dir / "待删.md").exists()


# ---------------------------------------------------------------- 旧端点响应键兼容
def test_legacy_link_keeps_linked_to_and_link(agent_client, admin_token):
    resp = agent_client.post(
        f"{AGENT}/link", headers=_h(admin_token), json={"from": "欢迎.md", "to": "兼容目标"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["linked_to"] == "兼容目标"
    assert data["link"] == "兼容目标"
    assert data["path"] == "欢迎.md"


def test_legacy_delete_keeps_trash_path(agent_client, admin_token):
    agent_client.post(
        f"{AGENT}/note", headers=_h(admin_token), json={"path": "旧删.md", "content": "x"},
    )
    resp = agent_client.delete(f"{AGENT}/note", params={"path": "旧删.md"}, headers=_h(admin_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["trash_path"].startswith(".trash/")
    assert data["path"] == "旧删.md"  # additive：契约 canonical 键


def test_legacy_search_keeps_elapsed_ms(agent_client, read_token):
    resp = agent_client.get(f"{AGENT}/search", params={"q": "欢迎"}, headers=_h(read_token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "elapsed_ms" in data and "hits" in data and "total" in data


def test_legacy_search_bad_mode_400(agent_client, read_token):
    resp = agent_client.get(f"{AGENT}/search", params={"q": "x", "mode": "bogus"}, headers=_h(read_token))
    assert resp.status_code == 400