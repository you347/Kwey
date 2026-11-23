"""版本检查：语义化比较、缓存、失败降级、API 契约。"""

from __future__ import annotations

import pytest

from app import update as upd


class TestVersionCompare:
    def test_parse(self):
        assert upd.parse_version("v0.2.0") == (0, 2, 0)
        assert upd.parse_version("1.10.3-beta.1") == (1, 10, 3)
        assert upd.parse_version("no-version") == (0, 0, 0)

    def test_newer(self):
        assert upd.is_newer("v0.3.0", "0.2.0")
        assert upd.is_newer("1.0.0", "0.99.99")
        assert not upd.is_newer("v0.2.0", "0.2.0")
        assert not upd.is_newer("v0.1.9", "0.2.0")

    def test_current_version_matches_package(self):
        from app import __version__

        assert upd.current_version() == __version__


class TestCheck:
    def setup_method(self):
        upd.reset_cache()

    def test_update_available(self, monkeypatch):
        monkeypatch.setattr(upd, "_fetch_gitee", lambda repo, timeout: {
            "latest": "v9.9.9", "url": "https://example.com/dl", "notes": "新功能", "source": "gitee/release",
        })
        data = upd.check({"repo": "x/y"}, force=True)
        assert data["ok"] and data["has_update"]
        assert data["latest_version"] == "9.9.9"
        assert data["url"] == "https://example.com/dl"

    def test_already_latest(self, monkeypatch):
        from app import __version__

        monkeypatch.setattr(upd, "_fetch_gitee", lambda repo, timeout: {
            "latest": f"v{__version__}", "url": "", "notes": "", "source": "gitee/tags",
        })
        data = upd.check({"repo": "x/y"}, force=True)
        assert data["ok"] and not data["has_update"]

    def test_cache_avoids_second_request(self, monkeypatch):
        calls = {"n": 0}

        def fake(repo, timeout):
            calls["n"] += 1
            return {"latest": "v9.0.0", "url": "u", "notes": "", "source": "gitee/release"}

        monkeypatch.setattr(upd, "_fetch_gitee", fake)
        upd.check({"repo": "x/y"}, force=True)
        upd.check({"repo": "x/y"})
        assert calls["n"] == 1
        assert upd.check({"repo": "x/y"})["cached"] is True

    def test_network_failure_is_soft(self, monkeypatch):
        monkeypatch.setattr(upd, "_fetch_gitee", lambda repo, timeout: None)
        monkeypatch.setattr(upd, "_fetch_github", lambda repo, timeout: None)
        data = upd.check({"repo": "x/y"}, force=True)
        assert data["ok"] is False and data["has_update"] is False
        assert "失败" in data["message"]


class TestApi:
    def test_version_endpoint(self, client):
        data = client.get("/api/update/version").json()["data"]
        assert data["version"]

    def test_check_endpoint(self, client, monkeypatch):
        monkeypatch.setattr("app.api.routes_update.check", lambda cfg, force=False: {
            "ok": True, "current": "0.2.0", "latest": "v0.3.0", "has_update": True,
            "url": "https://example.com", "message": "",
        })
        data = client.get("/api/update/check").json()["data"]
        assert data["has_update"] is True
        assert data["app_version"]
