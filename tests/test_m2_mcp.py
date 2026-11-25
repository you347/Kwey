"""M2-1 测试：Kwey MCP Server（JSON-RPC 2.0 / stdio）。

约定：直接实例化 KweyMCPServer(vault) 调 handle()，不起子进程（主测试），
避免 stdio 场景把 pytest 挂住；最后一个用例单独用 subprocess 验证 stdio 入口，
输入是有限的几行并以 EOF 结束，且显式设置 timeout，绝不启动 watcher 线程。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.mcp_server import KweyMCPServer  # noqa: E402

EXPECTED_TOOLS = {
    "kwey.search",
    "kwey.read",
    "kwey.open",
    "kwey.create",
    "kwey.write",
    "kwey.delete",
    "kwey.link",
    "kwey.tags",
    "kwey.graph.query",
    "kwey.organize",
    # W2/W4：补齐工具矩阵（现 29 个，含 open / audit / views / diagnose / capture / session.distill）
    "kwey.audit",
    "kwey.tree",
    "kwey.trash.list",
    "kwey.history.log",
    "kwey.history.rollback",
    "kwey.append",
    "kwey.move",
    "kwey.rename",
    "kwey.dir.create",
    "kwey.dir.delete",
    "kwey.trash.restore",
    "kwey.trash.purge",
    "kwey.organize.apply",
    "kwey.views.list",
    "kwey.views.query",
    "kwey.views.save",
    "kwey.diagnose",
    "kwey.capture",
    "kwey.session.distill",
}


# ---------------------------------------------------------------------- helpers
def rpc(server: KweyMCPServer, method: str, params=None, rid=1) -> dict:
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(server: KweyMCPServer, name: str, args: dict, rid=1):
    """执行 tools/call，返回 (CallToolResult, 内部响应 payload)。"""
    resp = rpc(server, "tools/call", {"name": name, "arguments": args}, rid)
    assert "error" not in resp, f"tools/call 不应产生 JSON-RPC error: {resp}"
    tool_res = resp["result"]
    assert tool_res["content"][0]["type"] == "text"
    payload = json.loads(tool_res["content"][0]["text"])
    return tool_res, payload


# ---------------------------------------------------------------------- 协议层
def test_initialize(vault):
    server = KweyMCPServer(vault)
    resp = rpc(server, "initialize", {})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "error" not in resp
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}, "resources": {}}
    assert result["serverInfo"] == {"name": "kwey", "version": "0.2.0"}


def test_ping(vault):
    assert rpc(KweyMCPServer(vault), "ping", {})["result"] == {}


def test_tools_list(vault):
    resp = rpc(KweyMCPServer(vault), "tools/list", {})
    tools = resp["result"]["tools"]
    assert len(tools) == 29
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert isinstance(t["description"], str) and t["description"]
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema.get("required", []), list)


def test_resources_list_and_read(vault):
    server = KweyMCPServer(vault)
    resp = rpc(server, "resources/list", {})
    resources = resp["result"]["resources"]
    notes = vault.db.all_notes()
    assert len(resources) == len(notes) >= 3
    for r in resources:
        assert r["uri"].startswith("kwey://note/")
        assert r["mimeType"] == "text/markdown"
        assert r["name"]

    uri = "kwey://note/欢迎.md"
    read = rpc(server, "resources/read", {"uri": uri})
    assert "error" not in read
    contents = read["result"]["contents"]
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "text/markdown"
    assert "欢迎" in contents[0]["text"]
    assert "title:" in contents[0]["text"]  # 原文含 frontmatter


# ---------------------------------------------------------------------- 工具往返
def test_search_read_create_write_roundtrip(vault):
    server = KweyMCPServer(vault)

    _, created = call(server, "kwey.create", {
        "path": "mcp/测试.md",
        "content": "你好 MCP 初始内容",
        "frontmatter": {"tags": ["mcp"], "自定义字段": "保留"},
    })
    assert created["code"] == 0
    assert created["data"]["path"] == "mcp/测试.md"

    _, read1 = call(server, "kwey.read", {"path": "mcp/测试.md"})
    assert read1["code"] == 0
    assert "你好 MCP 初始内容" in read1["data"]["content"]
    assert read1["data"]["frontmatter"]["自定义字段"] == "保留"

    _, wrote = call(server, "kwey.write", {
        "path": "mcp/测试.md",
        "content": "改写内容 独一无二",
    })
    assert wrote["code"] == 0
    assert wrote["data"]["updated"] is True

    _, read2 = call(server, "kwey.read", {"path": "mcp/测试.md"})
    assert "改写内容 独一无二" in read2["data"]["content"]
    # write 未传 frontmatter 时保留现有自定义字段
    assert read2["data"]["frontmatter"]["自定义字段"] == "保留"

    _, found = call(server, "kwey.search", {"query": "改写内容", "limit": 10})
    assert found["code"] == 0
    paths = [h["path"] for h in found["data"]["hits"]]
    assert "mcp/测试.md" in paths
    assert found["data"]["total"] >= 1


def test_other_tools_are_real(vault):
    """delete / link / tags / graph.query / organize 全部真实可用，非占位。"""
    server = KweyMCPServer(vault)

    # organize：ops 模块不存在 → provider_not_configured
    _, org = call(server, "kwey.organize", {"instruction": "整理所有笔记"})
    assert org["code"] == 0
    assert org["data"]["status"] == "provider_not_configured"
    assert org["data"]["hint"] == "配置 .kwey/provider.json 后可用"

    # link：末尾追加 [[to]]，走 write_note
    _, linked = call(server, "kwey.link", {"from": "欢迎.md", "to": "测试目标"})
    assert linked["code"] == 0
    _, after_link = call(server, "kwey.read", {"path": "欢迎.md"})
    assert "[[测试目标]]" in after_link["data"]["content"]

    # tags：改 frontmatter tags，走 write_note
    _, tagged = call(server, "kwey.tags", {
        "path": "notes/读书/2026计划.md",
        "add": ["mcp/新标签"],
        "remove": ["读书/2026"],
    })
    assert tagged["code"] == 0
    _, after_tags = call(server, "kwey.read", {"path": "notes/读书/2026计划.md"})
    tags = after_tags["data"]["frontmatter"]["tags"]
    assert "mcp/新标签" in tags
    assert "读书/2026" not in tags

    # graph.query：用 linkgraph 的 in/out 邻域
    _, g = call(server, "kwey.graph.query", {"path": "欢迎.md", "depth": 1})
    assert g["code"] == 0
    assert "欢迎.md" in g["data"]["nodes"]
    assert "欢迎.md" in g["data"]["reachable"]
    assert isinstance(g["data"]["nodes"]["欢迎.md"]["in"], list)
    assert isinstance(g["data"]["nodes"]["欢迎.md"]["out"], list)

    # delete：移入 .trash/，之后读取应报错（工具异常 isError=true，不抛穿）
    call(server, "kwey.create", {"path": "待删除.md", "content": "临时"})
    _, deleted = call(server, "kwey.delete", {"path": "待删除.md"})
    assert deleted["code"] == 0
    assert deleted["data"]["trash_path"].startswith(".trash/")
    tool_res, err_payload = call(server, "kwey.read", {"path": "待删除.md"})
    assert tool_res["isError"] is True
    assert err_payload["code"] == 404


# ---------------------------------------------------------------------- 错误路径
def test_unknown_method(vault):
    resp = rpc(KweyMCPServer(vault), "does/not/exist", {}, rid=7)
    assert resp["id"] == 7
    assert resp["error"]["code"] == -32601
    assert "does/not/exist" in resp["error"]["message"]


def test_unknown_tool(vault):
    tool_res, payload = call(KweyMCPServer(vault), "kwey.nope", {})
    assert tool_res["isError"] is True
    assert payload["code"] != 0
    assert "未知工具" in payload["message"]


def test_invalid_params(vault):
    server = KweyMCPServer(vault)
    # tools/call 缺 name → -32602
    resp = rpc(server, "tools/call", {"arguments": {}}, rid=9)
    assert resp["error"]["code"] == -32602
    # params 非 dict → -32602
    assert server.handle({"jsonrpc": "2.0", "id": 10, "method": "ping", "params": [1, 2]})["error"]["code"] == -32602
    # 工具缺必填参数 → -32602
    resp = rpc(server, "tools/call", {"name": "kwey.read", "arguments": {}}, rid=11)
    assert resp["error"]["code"] == -32602
    # resources/read uri 不合格式 → -32602
    resp = rpc(server, "resources/read", {"uri": "http://x"}, rid=12)
    assert resp["error"]["code"] == -32602


def test_server_defaults_to_deps_singleton(services):
    """vault=None 时用 deps.require_vault()。"""
    from app.api import deps

    server = KweyMCPServer()
    assert server.vault is deps.require_vault()
    assert server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"] == {}


def test_handle_never_raises(vault):
    """畸形输入也不抛穿，返回合法 JSON-RPC 响应。"""
    server = KweyMCPServer(vault)
    for bad in (None, 123, "x", {}, {"method": 5}):
        resp = server.handle(bad)  # type: ignore[arg-type]
        assert resp["jsonrpc"] == "2.0"
        assert "result" in resp or "error" in resp


# ---------------------------------------------------------------------- stdio 入口（独立子进程）
def test_stdio_entry(tmp_path):
    """独立临时 vault，避免与 pytest 进程内的 SQLite 连接冲突；EOF 优雅退出。"""
    root = tmp_path / "独立库"
    root.mkdir()
    (root / "hello.md").write_text("# 你好\n\nstdio 入口测试\n", encoding="utf-8")

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}),
        # 无 id 的通知：入口应静默，不产生 stdout 行
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]
    proc = subprocess.run(
        [sys.executable, "kwey_mcp.py", "--vault", str(root)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PROJECT_ROOT),
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(out_lines) == 3, f"stdout={proc.stdout!r}"
    assert json.loads(out_lines[0])["result"]["serverInfo"]["name"] == "kwey"
    assert json.loads(out_lines[1])["result"] == {}
    assert len(json.loads(out_lines[2])["result"]["tools"]) == 29


def test_stdio_help(tmp_path):
    proc = subprocess.run(
        [sys.executable, "kwey_mcp.py", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PROJECT_ROOT),
        timeout=20,
    )
    assert proc.returncode == 0
    assert "--vault" in proc.stdout
