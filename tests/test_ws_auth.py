"""WS 握手鉴权（A4 遗留落地）：渐进式——带 token 严格校验，无 token 本地回环放行。

不引入 pytest-asyncio：用 asyncio.run() 在同步测试里驱动协程；
用最小 WebSocket 替身验证握手分支，不进入 run_client 长循环。
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest
from fastapi import HTTPException

from app.agent.auth import TokenStore
from app.api import guard as guard_mod
from app.api import ws as ws_mod


class _FakeWebSocket:
    """最小 WebSocket 替身：记录 accept/send/close，不进入 run_client 长循环。"""

    def __init__(self, host: str = "testclient") -> None:
        self.client = type("C", (), {"host": host})()
        self.accepted = False
        self.closed_with = None
        self.sent: List[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def _run(host: str, token, verify=None, reach_client=None):
    """驱动一次握手，返回 (fake, reached_run_client)。"""
    fake = _FakeWebSocket(host)
    calls = {"run": False}

    async def _capture(ws):
        calls["run"] = True

    original_run = ws_mod.hub.run_client
    original_verify = ws_mod.verify_ws_token
    ws_mod.hub.run_client = _capture
    if verify is not None:
        ws_mod.verify_ws_token = verify
    try:
        asyncio.run(ws_mod.ws_events(fake, token=token))
    finally:
        ws_mod.hub.run_client = original_run
        ws_mod.verify_ws_token = original_verify
    return fake, calls["run"]


class TestLoopbackFallback:
    def test_no_token_loopback_allowed(self):
        """无 token + 本地回环 → 放行到 run_client。"""
        fake, reached = _run("127.0.0.1", None)
        assert fake.accepted and reached and fake.closed_with is None

    def test_no_token_ipv6_loopback_allowed(self):
        fake, reached = _run("::1", None)
        assert reached

    def test_no_token_remote_rejected(self):
        """无 token + 非回环 → 4401 + error 帧。"""
        fake, reached = _run("203.0.113.9", None)
        assert fake.closed_with == 4401 and not reached
        assert fake.sent and fake.sent[0]["code"] == 401

    def test_testclient_is_loopback(self):
        """TestClient 的 'testclient' 视作本地，保证既有 QA 测试不回归。"""
        fake, reached = _run("testclient", None)
        assert reached


class TestTokenHandshake:
    def test_valid_token_allowed(self, monkeypatch):
        """带有效 token → 校验通过 → 放行（即便非回环）。"""
        seen = {}

        def _verify(t, r="read"):
            seen["token"], seen["role"] = t, r
            return {"role": r}

        fake, reached = _run("203.0.113.9", "good-token", verify=_verify)
        assert reached and seen == {"token": "good-token", "role": "read"}

    def test_invalid_token_4401(self):
        """无效 token → 401 → close 4401（即便来自回环也拒绝）。"""
        def _raise(t, r="read"):
            raise HTTPException(status_code=401, detail="访问 token 无效")

        fake, reached = _run("127.0.0.1", "bad-token", verify=_raise)
        assert fake.closed_with == 4401 and not reached
        assert fake.sent[0]["code"] == 401

    def test_insufficient_role_4403(self):
        """权限不足 → 403 → close 4403。"""
        def _raise(t, r="read"):
            raise HTTPException(status_code=403, detail="权限不足")

        fake, reached = _run("127.0.0.1", "low-role", verify=_raise)
        assert fake.closed_with == 4403 and not reached
        assert fake.sent[0]["code"] == 403

    def test_real_token_via_guard(self, monkeypatch, vault_dir):
        """不 stub verify：真 token 经 guard._store → TokenStore 校验通过。"""
        tk = TokenStore(vault_dir).issue("write", "ws-real")["token"]
        monkeypatch.setattr(guard_mod, "_store", lambda: TokenStore(vault_dir))
        fake, reached = _run("127.0.0.1", tk)
        assert reached

    def test_real_invalid_token_via_guard(self, monkeypatch, vault_dir):
        """真 verify + 无效 token → 4401。"""
        monkeypatch.setattr(guard_mod, "_store", lambda: TokenStore(vault_dir))
        fake, reached = _run("127.0.0.1", "no-such-token")
        assert fake.closed_with == 4401 and not reached


class TestRealWebSocket:
    """通过 TestClient 的真实 websocket 握手（复用 conftest client/vault fixtures）。"""

    def test_real_ws_connects_and_receives(self, client):
        with client.websocket_connect("/ws/events") as ws:
            data = ws.receive_json()
        assert data["event"] == "connected"

    def test_real_ws_with_bad_token_closes(self, client, vault_dir):
        with client.websocket_connect("/ws/events?token=bad") as ws:
            data = ws.receive_json()
            assert data["event"] == "error" and data["code"] == 401
