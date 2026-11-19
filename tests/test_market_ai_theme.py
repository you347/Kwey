"""插件市场（catalog/install/uninstall）+ AI 主题生成 + Provider 配置保存回归。"""

from __future__ import annotations

import json

import pytest

from app.api import routes_plugins, routes_theme, deps
from app.agent import provider as provider_mod


def _body(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.fixture
def client(services):
    """本卡 router 已在 server.py 注册；用标准 client fixture 的 app。"""
    from fastapi.testclient import TestClient
    from app.server import create_app
    with TestClient(create_app()) as c:
        yield c


class TestPluginMarket:
    def test_catalog_lists_builtin(self, client):
        data = _body(client.get("/api/plugins/catalog"))
        ids = [p["id"] for p in data["plugins"]]
        assert "vault-health" in ids and "random-note" in ids
        assert all("code" not in p for p in data["plugins"])  # 列表不下发源码
        assert all(p["installed"] is False for p in data["plugins"])

    def test_install_then_list(self, client, vault_dir):
        r = _body(client.post("/api/plugins/install", json={"id": "vault-health"}))
        assert r["installed"] is True
        plugin_dir = vault_dir / ".kwey" / "plugins" / "vault-health"
        assert (plugin_dir / "plugin.json").is_file()
        assert (plugin_dir / "main.js").is_file()
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["id"] == "vault-health"
        assert "ui_command" in manifest["permissions"]
        listed = {p["id"]: p for p in _body(client.get("/api/plugins/list"))["plugins"]}
        assert "vault-health" in listed

    def test_install_unknown_404(self, client):
        assert client.post("/api/plugins/install", json={"id": "no-such"}).status_code == 404

    def test_install_toggle_uninstall(self, client, vault_dir):
        client.post("/api/plugins/install", json={"id": "random-note"})
        _body(client.post("/api/plugins/toggle", json={"id": "random-note", "enabled": True}))
        entry = _body(client.get("/api/plugins/entry", params={"id": "random-note"}))
        assert "setup" in entry["code"]
        _body(client.post("/api/plugins/uninstall", json={"id": "random-note"}))
        assert not (vault_dir / ".kwey" / "plugins" / "random-note").exists()
        assert client.get("/api/plugins/entry", params={"id": "random-note"}).status_code == 404

    def test_builtin_code_uses_only_injected_api(self):
        """内置插件源码不得直接碰 window/localStorage/fetch（受信最小权限）。"""
        from app.plugins.builtin import BUILTIN
        import re
        for item in BUILTIN:
            code = item["code"]
            assert not re.search(r"\b(window|localStorage|document|fetch)\b", code), item["id"]


class TestThemeGenerate:
    def test_generate_without_provider_400(self, client):
        r = client.post("/api/theme/generate", json={"request": "把强调色改红"})
        assert r.status_code == 400
        assert "Provider" in r.json()["message"]

    def test_generate_with_fake_provider(self, client, monkeypatch, vault_dir):
        captured = {}

        class _FakeProvider:
            configured = True
            config = {"endpoint": "https://x/v1", "protocol": "openai"}

            def __init__(self, root):
                pass

            def chat(self, messages, tools=None, **kw):
                captured["messages"] = messages
                captured["extra"] = kw.get("extra_body")
                captured["timeout"] = kw.get("timeout")
                return {"content": ":root { --accent: #f00; }", "tool_calls": []}

        # routes_theme.generate 在函数内 `from app.agent.provider import AgentProvider`，
        # 调用时取模块属性，因此 patch 源模块即可。
        monkeypatch.setattr(provider_mod, "AgentProvider", _FakeProvider)
        data = _body(client.post("/api/theme/generate", json={"request": "强调色改红"}))
        assert "--accent" in data["css"]
        assert data["request"] == "强调色改红"
        msgs = captured.get("messages") or []
        assert len(msgs) == 2 and "强调色改红" in msgs[1]["content"]

    def test_generate_rejects_forbidden_css(self, client, monkeypatch):
        """AI 输出含 @import 同样被过滤（不落盘）。"""
        class _BadProvider:
            configured = True
            config = {"endpoint": "https://x", "protocol": "openai"}
            def __init__(self, root): pass
            def chat(self, messages, tools=None, **kw):
                return {"content": "@import url('https://evil/x.css'); body{--accent:red}", "tool_calls": []}
        monkeypatch.setattr(provider_mod, "AgentProvider", _BadProvider)
        r = client.post("/api/theme/generate", json={"request": "随便"})
        assert r.status_code == 400
        assert "禁止" in r.json()["message"]

    def test_generate_empty_request_400(self, client):
        assert client.post("/api/theme/generate", json={"request": "  "}).status_code == 400


class TestProviderConfigSave:
    """回归：A4 后 provider-config 需 write token，本地 UI 必须先 bootstrap 再保存。"""

    def test_bootstrap_then_save_long_key(self, client, vault_dir):
        boot = _body(client.post("/api/agent/bootstrap"))
        tk = boot["token"]
        long_key = "sk-" + "A1b2C3d4" * 30
        r = client.put("/api/agent/provider-config",
                       headers={"X-Kwey-Token": tk},
                       json={"endpoint": "https://x/v1", "api_key": long_key,
                             "model": "gpt-4o-mini", "protocol": "openai"})
        assert r.status_code == 200
        cfg = json.loads((vault_dir / ".kwey" / "provider.json").read_text(encoding="utf-8"))
        assert cfg["api_key"] == long_key


class TestThemeGenerateLatency:
    """思考型模型超时回归：主题生成必须放宽超时，并在阿里云端点关掉思考。"""

    def test_dashscope_gets_thinking_off_and_long_timeout(self, client, monkeypatch, vault_dir):
        from app.api import routes_theme as rt

        captured = {}

        class _FakeProvider:
            configured = True
            config = {
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "protocol": "openai",
            }

            def __init__(self, root):
                pass

            def chat(self, messages, tools=None, **kw):
                captured.update(kw)
                return {"content": ":root{--accent:#f00}", "tool_calls": []}

        monkeypatch.setattr(provider_mod, "AgentProvider", _FakeProvider)
        _body(client.post("/api/theme/generate", json={"request": "强调色改红"}))
        assert captured.get("extra_body") == {"enable_thinking": False}
        assert captured.get("timeout") == rt.GENERATE_TIMEOUT
        assert rt.GENERATE_TIMEOUT >= 120

    def test_other_gateway_gets_no_extra_body(self, client, monkeypatch):
        captured = {}

        class _FakeProvider:
            configured = True
            config = {"endpoint": "https://api.openai.com/v1", "protocol": "openai"}

            def __init__(self, root):
                pass

            def chat(self, messages, tools=None, **kw):
                captured.update(kw)
                return {"content": ":root{--accent:#f00}", "tool_calls": []}

        monkeypatch.setattr(provider_mod, "AgentProvider", _FakeProvider)
        _body(client.post("/api/theme/generate", json={"request": "强调色改红"}))
        assert captured.get("extra_body") == {}

    def test_unknown_field_400_retries_without_extra(self, client, monkeypatch):
        """网关不认 enable_thinking 时自动去掉重试，用户不该看到 400。"""
        calls = []

        class _FakeProvider:
            configured = True
            config = {
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "protocol": "openai",
            }

            def __init__(self, root):
                pass

            def chat(self, messages, tools=None, **kw):
                calls.append(kw)
                if kw.get("extra_body"):
                    raise provider_mod.ProviderError(
                        "Provider 返回 HTTP 400：unknown field enable_thinking"
                    )
                return {"content": ":root{--accent:#f00}", "tool_calls": []}

        monkeypatch.setattr(provider_mod, "AgentProvider", _FakeProvider)
        data = _body(client.post("/api/theme/generate", json={"request": "强调色改红"}))
        assert "--accent" in data["css"]
        assert len(calls) == 2 and calls[1].get("extra_body") in (None, {})


class TestTimeoutMessage:
    """超时与断网要分开报：旧文案会把 30s 超时误报成「网络不通」。"""

    def test_timeout_reports_seconds(self, tmp_path, monkeypatch):
        from app.agent.provider import AgentProvider, ProviderError
        import urllib.error

        cfg = tmp_path / ".kwey"
        cfg.mkdir(parents=True)
        (cfg / "provider.json").write_text(
            '{"endpoint":"https://example.invalid/v1","api_key":"x","timeout":30}',
            encoding="utf-8",
        )
        p = AgentProvider(tmp_path)

        def boom(req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(ProviderError) as exc:
            p.chat([{"role": "user", "content": "hi"}], timeout=180)
        msg = str(exc.value)
        assert "超时" in msg and "180s" in msg
