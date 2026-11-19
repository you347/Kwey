"""卡A W3：22 工具矩阵一致性测试（元数据 / MCP parity / REST 权限 / schema 边界）。

与 test_contract.py（W1 行为）、test_contract_w2.py（W2 行为）互补：
本文件不重复业务往返，只钉住「全矩阵」的结构不变式与传输层一致性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import registry
from app.agent.auth import TokenStore
from app.agent.mcp_server import KweyMCPServer
from app.agent.registry import ContractError

AGENT = "/api/agent"

ALL_TOOLS = registry.contract()["tools"]
ALL_NAMES = [t["name"] for t in ALL_TOOLS]
READ_NAMES = [t["name"] for t in ALL_TOOLS if t["permission"] == "read"]
WRITE_NAMES = [t["name"] for t in ALL_TOOLS if t["permission"] == "write"]
ADMIN_NAMES = [t["name"] for t in ALL_TOOLS if t["permission"] == "admin"]
REQUIRED_CASES = [
    (t["name"], t["input_schema"]["required"])
    for t in ALL_TOOLS
    if t["input_schema"].get("required")
]

JOURNAL_ACTIONS = {
    "create", "update", "delete", "link", "tags", "append", "move", "rename",
    "mkdir", "rmdir", "restore", "rollback", "organize", "purge",
}


@pytest.fixture
def agent_client(client):
    from app.api.routes_agent import router

    client.app.include_router(router)
    return client


@pytest.fixture
def admin_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("admin", "matrix-admin")["token"]


@pytest.fixture
def read_token(vault_dir: Path) -> str:
    return TokenStore(vault_dir).issue("read", "matrix-read")["token"]


def _h(token: str) -> dict:
    return {"X-Kwey-Token": token}


# ---------------------------------------------------------------- 矩阵规模与元数据
def test_matrix_size_and_tiers():
    assert len(ALL_TOOLS) == 23
    assert len(READ_NAMES) == 9
    assert len(WRITE_NAMES) == 13
    assert len(ADMIN_NAMES) == 1
    assert ADMIN_NAMES == ["kwey.trash.purge"]
    assert len(set(ALL_NAMES)) == 23


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=ALL_NAMES)
def test_metadata_invariants(tool):
    assert tool["name"].startswith("kwey.")
    assert isinstance(tool["description"], str) and tool["description"]
    assert "handler" not in tool
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    required = schema.get("required", [])
    assert isinstance(required, list)
    assert set(required) <= set(schema["properties"])  # 必填字段必须在 properties 中
    perm, side, action = tool["permission"], tool["side_effect"], tool["journal_action"]
    if perm == "read":
        assert side == "read" and action is None
    elif perm == "write":
        assert side == "reversible" and action in JOURNAL_ACTIONS
    else:
        assert perm == "admin"
        assert side == "irreversible" and action in JOURNAL_ACTIONS


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=ALL_NAMES)
def test_get_tool_matches_contract(tool):
    single = registry.get_tool(tool["name"])
    assert single == tool


# ---------------------------------------------------------------- MCP parity
@pytest.mark.parametrize("tool", ALL_TOOLS, ids=ALL_NAMES)
def test_mcp_schema_parity(vault, tool):
    server = KweyMCPServer(vault)
    mcp_tools = {t["name"]: t for t in server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )["result"]["tools"]}
    assert tool["name"] in mcp_tools
    assert mcp_tools[tool["name"]]["inputSchema"] == tool["input_schema"]
    assert mcp_tools[tool["name"]]["description"] == tool["description"]


# ---------------------------------------------------------------- schema 边界
@pytest.mark.parametrize("name,required", REQUIRED_CASES, ids=[c[0] for c in REQUIRED_CASES])
def test_missing_required_raises_400(vault, name, required):
    with pytest.raises(ContractError) as ei:
        registry.invoke(name, {}, vault=vault)
    assert ei.value.code == 400
    assert any(field in ei.value.message for field in required)


@pytest.mark.parametrize("name", ALL_NAMES)
def test_non_object_arguments_raises_400(vault, name):
    with pytest.raises(ContractError) as ei:
        registry.invoke(name, ["not", "object"], vault=vault)
    assert ei.value.code == 400


# ---------------------------------------------------------------- REST 权限矩阵
@pytest.mark.parametrize("name", WRITE_NAMES + ADMIN_NAMES)
def test_read_token_blocked_on_write_tools(agent_client, read_token, name):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(read_token),
                          json={"name": name, "arguments": {}})
    assert r.status_code == 403


@pytest.mark.parametrize("name", READ_NAMES)
def test_read_token_allowed_on_read_tools(agent_client, read_token, name):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(read_token),
                          json={"name": name, "arguments": {}})
    assert r.status_code != 403  # 200（无参工具）或 400（缺必填），绝非权限拒绝


@pytest.mark.parametrize("name", ALL_NAMES)
def test_admin_token_never_403(agent_client, admin_token, name):
    r = agent_client.post(f"{AGENT}/tool", headers=_h(admin_token),
                          json={"name": name, "arguments": {}})
    assert r.status_code != 403


@pytest.mark.parametrize("name", ALL_NAMES)
def test_tool_requires_token(agent_client, name):
    r = agent_client.post(f"{AGENT}/tool", json={"name": name, "arguments": {}})
    assert r.status_code == 401