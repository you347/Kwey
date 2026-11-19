"""本地 UI token 自愈：本地回环可重复 bootstrap，远程仍 409。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent.auth import TokenStore


def _body(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_loopback_reissue_when_store_nonempty(client, vault_dir):
    """库非空 + 本地回环 → 再签发一个 admin token（浏览器清 localStorage 后自愈）。"""
    first = _body(client.post("/api/agent/bootstrap"))["token"]
    assert (vault_dir / ".kwey" / "tokens.json").exists()
    second = _body(client.post("/api/agent/bootstrap"))["token"]
    assert first and second and first != second
    # 重签吊销旧 local-ui，新 token 可用（列表始终只有一条 local-ui）
    assert client.get("/api/agent/health", headers={"X-Kwey-Token": first}).status_code == 401
    assert client.get("/api/agent/health", headers={"X-Kwey-Token": second}).status_code == 200
    assert sum(1 for t in TokenStore(vault_dir).list() if t["name"] == "local-ui") == 1


def test_remote_bootstrap_rejected_when_nonempty(client, vault_dir):
    """库非空 + 远程来源 → 409（防越权重置）。"""
    from app.server import create_app

    _body(client.post("/api/agent/bootstrap"))  # 先建库
    # TestClient 默认 host=testclient 视为本地；显式注入远程 client host
    with TestClient(create_app(), client=("203.0.113.9", 12345)) as remote:
        # vault 未随 app 创建（lifespan 无 initial_vault），需先打开
        remote.post("/api/vault/open", json={"path": str(vault_dir)})
        r = remote.post("/api/agent/bootstrap")
        assert r.status_code == 409
        assert "已有访问 token" in r.json()["message"]


def test_api_injects_token_for_all_requests(client, vault_dir):
    """回归：所有经 api.js 的请求统一带 X-Kwey-Token（本测试验证服务端接受）。"""
    tk = _body(client.post("/api/agent/bootstrap"))["token"]
    # pulse 是之前漏网的端点：带 token 应 200
    assert client.get("/api/pulse", headers={"X-Kwey-Token": tk}).status_code == 200
    assert client.get("/api/pulse").status_code == 401
