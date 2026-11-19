"""C4 / P2 自定义主题测试：CRUD、安全过滤、路径安全、用户笔记零改动。

测试期按需挂载 ``routes_theme.router``；共享 server.py 的接线由集成卡完成。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from app.api import deps
from app.api.routes_theme import router as theme_router


@pytest.fixture
def theme_client(client):
    paths = {getattr(route, "path", None) for route in client.app.routes}
    if "/api/theme/custom" not in paths:
        client.app.include_router(theme_router)
    return client


def snapshot_notes(root: Path) -> Dict[str, bytes]:
    """只快照用户笔记（md / 附件），验证主题写盘不碰事实源。"""
    return {
        p.as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".kwey" not in p.parts
    }


class TestCustomThemeCRUD:
    def test_get_empty(self, theme_client, vault_dir):
        res = theme_client.get("/api/theme/custom")
        assert res.status_code == 200
        assert res.json() == {"code": 0, "data": {"css": "", "enabled": False}, "message": "ok"}
        assert not (vault_dir / ".kwey" / "custom.css").exists()

    def test_put_get_and_delete(self, theme_client, vault_dir):
        notes_before = snapshot_notes(vault_dir)
        css = "body { --accent: #f00; }\n:root { --bg: #123456; }\n"

        put = theme_client.put("/api/theme/custom", json={"css": css, "enabled": True})
        assert put.status_code == 200 and put.json()["code"] == 0
        assert put.json()["data"] == {"css": css, "enabled": True}

        css_file = vault_dir / ".kwey" / "custom.css"
        assert css_file.is_file()
        assert css_file.read_text(encoding="utf-8") == css
        assert not (vault_dir / ".kwey" / "custom.css.tmp").exists()  # 原子写无残留
        assert deps.require_config().get("custom_css_enabled") is True
        assert snapshot_notes(vault_dir) == notes_before  # 用户笔记零改动

        got = theme_client.get("/api/theme/custom")
        assert got.json()["data"] == {"css": css, "enabled": True}

        # enabled=false：CSS 保留但前端不注入
        off = theme_client.put("/api/theme/custom", json={"css": css, "enabled": False})
        assert off.json()["data"]["enabled"] is False
        assert css_file.read_text(encoding="utf-8") == css
        assert theme_client.get("/api/theme/custom").json()["data"]["enabled"] is False

        deleted = theme_client.delete("/api/theme/custom")
        assert deleted.status_code == 200
        assert deleted.json()["data"] == {"css": "", "enabled": False}
        assert not css_file.exists()
        assert deps.require_config().get("custom_css_enabled") is False
        assert theme_client.get("/api/theme/custom").json()["data"]["css"] == ""
        assert snapshot_notes(vault_dir) == notes_before

    def test_delete_is_idempotent(self, theme_client):
        for _ in range(2):
            res = theme_client.delete("/api/theme/custom")
            assert res.status_code == 200
            assert res.json()["data"] == {"css": "", "enabled": False}


class TestCssSafety:
    @pytest.mark.parametrize(
        "bad_css",
        [
            "@import url('https://evil.example/a.css');",
            "body { background: url(http://evil.example/a.png); }",
            'body { background: url("//evil.example/a.png"); }',
            "body { width: expression(alert(1)); }",
            "body { background: javascript:alert(1); }",
        ],
    )
    def test_forbidden_css_rejected_without_writing(self, theme_client, vault_dir, bad_css):
        css_file = vault_dir / ".kwey" / "custom.css"
        css_file.parent.mkdir(parents=True, exist_ok=True)
        css_file.write_text("/* 原有内容 */", encoding="utf-8")

        res = theme_client.put("/api/theme/custom", json={"css": bad_css, "enabled": True})
        assert res.status_code == 400
        assert "禁止" in res.json()["message"]
        assert css_file.read_text(encoding="utf-8") == "/* 原有内容 */"  # 失败不覆盖

    def test_path_field_is_ignored_and_write_stays_in_vault(self, theme_client, vault_dir, tmp_path):
        evil = tmp_path / "escaped.md"
        res = theme_client.put(
            "/api/theme/custom",
            json={
                "css": "body{--accent:#f00}",
                "enabled": True,
                "path": "../../escaped.md",  # 请求体里多塞路径也必须无效
            },
        )
        assert res.status_code == 200
        assert not evil.exists()
        target = vault_dir / ".kwey" / "custom.css"
        assert target.is_file() and target.parent == vault_dir / ".kwey"
        assert target.read_text(encoding="utf-8") == "body{--accent:#f00}"

    def test_oversized_css_rejected(self, theme_client):
        res = theme_client.put(
            "/api/theme/custom", json={"css": "x" * 200_001, "enabled": True}
        )
        assert res.status_code == 400 and "过长" in res.json()["message"]


class TestGeneratePrompt:
    """AI 生成提示词必须带真实选择器清单：否则模型臆造类名，CSS 合法但零命中。"""

    def test_prompt_lists_real_chat_selectors(self):
        from app.api.routes_theme import SELECTOR_MAP, _GENERATE_SYSTEM

        assert ".chat-msg--assistant .chat-msg__bubble" in _GENERATE_SYSTEM
        assert ".chat-msg__bubble" in SELECTOR_MAP
        assert ".cm-s-kwey" in SELECTOR_MAP
        assert "#panel-chat" in SELECTOR_MAP

    def test_prompt_forbids_inventing_class_names(self):
        from app.api.routes_theme import _GENERATE_SYSTEM

        assert "禁止臆造" in _GENERATE_SYSTEM
        # 反面样本要被显式点名，模型才不会再写
        assert ".agent-reply" in _GENERATE_SYSTEM
        assert "var(--x)" in _GENERATE_SYSTEM

    def test_prompt_has_shape_recipe(self):
        """形状需求最容易画错：提示词必须给出心形的正确旋转原点与成品示例。"""
        from app.api.routes_theme import SELECTOR_MAP, _GENERATE_SYSTEM

        assert "#btn-chat-send" in SELECTOR_MAP
        assert "transform-origin: 0 100%" in _GENERATE_SYSTEM
        assert "蝴蝶结" in _GENERATE_SYSTEM
        assert "#e5484d" in _GENERATE_SYSTEM

    def test_generate_requires_provider(self, theme_client):
        res = theme_client.post("/api/theme/generate", json={"request": "把聊天区文字改成蓝色"})
        assert res.status_code == 400
        assert "Provider" in res.json()["message"]

    def test_generate_rejects_empty_request(self, theme_client):
        res = theme_client.post("/api/theme/generate", json={"request": "   "})
        assert res.status_code == 400 and "描述" in res.json()["message"]


class TestKnownSelectors:
    """类名清单接口：给前端做「选择器不存在」体检，防止臆造类名静默失效。"""

    def _data(self, theme_client):
        res = theme_client.get("/api/theme/known-selectors")
        assert res.status_code == 200
        return res.json()["data"]

    def test_lists_real_chat_classes(self, theme_client):
        data = self._data(theme_client)
        assert "chat-msg--assistant" in data["classes"]
        assert "chat-msg__bubble" in data["classes"]
        assert "cm-s-kwey" in data["classes"]
        assert "panel-chat" in data["ids"]

    def test_invented_names_are_absent(self, theme_client):
        classes = set(self._data(theme_client)["classes"])
        # 模型常臆造的名字，必须不在真实清单里
        assert "agent-reply" not in classes
        assert "chat-reply" not in classes
        assert "message" not in classes


class TestKnownSelectors:
    """类名清单接口：给前端做「选择器不存在」体检，防止臆造类名静默失效。"""

    def _data(self, theme_client):
        res = theme_client.get("/api/theme/known-selectors")
        assert res.status_code == 200
        return res.json()["data"]

    def test_lists_real_chat_classes(self, theme_client):
        data = self._data(theme_client)
        assert "chat-msg--assistant" in data["classes"]
        assert "chat-msg__bubble" in data["classes"]
        assert "cm-s-kwey" in data["classes"]
        assert "panel-chat" in data["ids"]

    def test_invented_names_are_absent(self, theme_client):
        classes = set(self._data(theme_client)["classes"])
        # 模型常臆造的名字，必须不在真实清单里
        assert "agent-reply" not in classes
        assert "chat-reply" not in classes
        assert "message" not in classes
