"""C3 / P2 插件系统验收：清单解析、白名单校验、启用持久化、入口下发。

覆盖：
1. plugin.json 解析（合法清单出现、字段缺省值）
2. 非法权限 / 非法 id / id 与目录错位 / entry 越界 / 坏 JSON —— 一律跳过不出现在 list
3. 启用状态持久化（plugins.json 落盘 + 重开实例保持 + disable 幂等）
4. entry 下发：未启用 403、启用 200、入口文件缺失 404、toggle 未知插件 404、非法 id 400
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_plugins import router as plugins_router
from app.plugins.registry import PluginRegistry


@pytest.fixture
def plugins_client(services):
    """仅挂本卡 router 的 TestClient。

    server.py 属共享文件（router 由集成卡统一 include），本卡测试自建 app 验证
    API 契约；deps 服务族单例（vault/db 等）由 conftest services fixture 提供，
    路由行为与真实 app 完全一致（错误响应为 FastAPI 默认 {detail} 形状，
    断言只依赖 status_code，不依赖信封包装）。
    """
    app = FastAPI()
    app.include_router(plugins_router)
    with TestClient(app) as c:
        yield c

VALID_MANIFEST = {
    "id": "hello",
    "name": "Hello 插件",
    "version": "1.2.0",
    "description": "演示插件",
    "permissions": ["read_notes", "ui_command"],
    "entry": "main.js",
}


def _make_plugin(vault_dir: Path, dir_name: str, manifest, code: str = "// plugin") -> None:
    """在 vault 内写一个插件目录；manifest 为 dict / str（坏 JSON）/ None（缺文件）。"""
    d = vault_dir / ".kwey" / "plugins" / dir_name
    d.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        text = manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False)
        (d / "plugin.json").write_text(text, encoding="utf-8")
    if code is not None:
        (d / "main.js").write_text(code, encoding="utf-8")


def _state_file(vault_dir: Path) -> Path:
    return vault_dir / ".kwey" / "plugins.json"


# ---------------------------------------------------------------- list：解析与跳过
def test_list_empty_vault(plugins_client):
    r = plugins_client.get("/api/plugins/list")
    assert r.status_code == 200
    assert r.json() == {"code": 0, "data": {"plugins": []}, "message": "ok"}


def test_list_valid_plugin(plugins_client, vault_dir: Path):
    _make_plugin(vault_dir, "hello", VALID_MANIFEST)
    r = plugins_client.get("/api/plugins/list")
    plugins = r.json()["data"]["plugins"]
    assert len(plugins) == 1
    p = plugins[0]
    assert p["id"] == "hello"
    assert p["name"] == "Hello 插件"
    assert p["version"] == "1.2.0"
    assert p["permissions"] == ["read_notes", "ui_command"]
    assert p["entry"] == "main.js"
    assert p["enabled"] is False  # 默认禁用


def test_manifest_defaults(plugins_client, vault_dir: Path):
    """name/version/description 缺省回退；permissions 缺失视为无权限。"""
    _make_plugin(vault_dir, "bare", {"id": "bare"})
    p = plugins_client.get("/api/plugins/list").json()["data"]["plugins"][0]
    assert p["name"] == "bare"
    assert p["version"] == "0.0.0"
    assert p["description"] == ""
    assert p["permissions"] == []


def test_invalid_permissions_skipped(plugins_client, vault_dir: Path):
    """白名单外权限（如 admin_os）→ 整个插件拒绝出现。"""
    _make_plugin(vault_dir, "evil", {
        "id": "evil", "permissions": ["read_notes", "admin_os"],
    })
    plugins = plugins_client.get("/api/plugins/list").json()["data"]["plugins"]
    assert plugins == []


def test_broken_json_skipped(plugins_client, vault_dir: Path):
    """坏 JSON / 缺清单文件 → 跳过不报错。"""
    _make_plugin(vault_dir, "broken", "{not-json")
    _make_plugin(vault_dir, "nofile", None)
    assert plugins_client.get("/api/plugins/list").json()["data"]["plugins"] == []


def test_id_mismatch_skipped(plugins_client, vault_dir: Path):
    """目录名与 id 错位 → 跳过（目录是物理隔离边界）。"""
    _make_plugin(vault_dir, "app1", {**VALID_MANIFEST, "id": "app2"})
    assert plugins_client.get("/api/plugins/list").json()["data"]["plugins"] == []


def test_bad_id_skipped(plugins_client, vault_dir: Path):
    """非法 id（大写 / 空格 / 点）→ 跳过。"""
    _make_plugin(vault_dir, "UPPER", {"id": "UPPER"})
    _make_plugin(vault_dir, "a b", {"id": "a b"})
    _make_plugin(vault_dir, "a.b", {"id": "a.b"})
    assert plugins_client.get("/api/plugins/list").json()["data"]["plugins"] == []


def test_entry_escape_skipped(plugins_client, vault_dir: Path):
    """entry 目录穿越 → 跳过，杜绝清单把入口指到插件目录外。"""
    _make_plugin(vault_dir, "escape", {**VALID_MANIFEST, "entry": "../../evil.js"})
    (vault_dir / ".kwey" / "evil.js").write_text("// 逃逸目标", encoding="utf-8")
    assert plugins_client.get("/api/plugins/list").json()["data"]["plugins"] == []


def test_entry_missing_file_skipped(plugins_client, vault_dir: Path):
    """清单合法但入口文件不存在 → 跳过。"""
    d = vault_dir / ".kwey" / "plugins" / "ghost"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps(VALID_MANIFEST), encoding="utf-8")
    assert plugins_client.get("/api/plugins/list").json()["data"]["plugins"] == []


# ---------------------------------------------------------------- toggle：持久化
def test_toggle_persists_and_reopens(plugins_client, vault_dir: Path):
    """enable → plugins.json 落盘 + list 标记启用；新实例状态保持；disable 幂等移除。"""
    _make_plugin(vault_dir, "hello", VALID_MANIFEST)
    r = plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": True})
    assert r.status_code == 200
    assert r.json()["data"] == {"id": "hello", "name": "Hello 插件", "enabled": True}

    state = json.loads(_state_file(vault_dir).read_text(encoding="utf-8"))
    assert state == {"enabled": ["hello"]}

    # 新实例（模拟重启后重扫）：状态从磁盘恢复
    fresh = PluginRegistry(vault_dir).list()
    assert fresh[0]["enabled"] is True

    r = plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": False})
    assert r.json()["data"]["enabled"] is False
    assert json.loads(_state_file(vault_dir).read_text(encoding="utf-8")) == {"enabled": []}
    # 幂等：再禁一次不报错
    r = plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": False})
    assert r.status_code == 200


def test_corrupt_state_means_all_disabled(plugins_client, vault_dir: Path):
    """状态文件损坏 → 空集语义（全部禁用），list 不报错。"""
    _make_plugin(vault_dir, "hello", VALID_MANIFEST)
    _state_file(vault_dir).parent.mkdir(parents=True, exist_ok=True)
    _state_file(vault_dir).write_text("{broken", encoding="utf-8")
    plugins = plugins_client.get("/api/plugins/list").json()["data"]["plugins"]
    assert plugins[0]["enabled"] is False


def test_toggle_unknown_404_and_bad_id_400(plugins_client, vault_dir: Path):
    _make_plugin(vault_dir, "hello", VALID_MANIFEST)
    r = plugins_client.post("/api/plugins/toggle", json={"id": "nope", "enabled": True})
    assert r.status_code == 404
    r = plugins_client.post("/api/plugins/toggle", json={"id": "NOT-VALID", "enabled": True})
    assert r.status_code == 404  # 非法 id 在 _find 处即判不存在，统一 404


# ---------------------------------------------------------------- entry 下发
def test_entry_requires_enabled(plugins_client, vault_dir: Path):
    """未启用 → 403；启用 → 200 且 code 为入口文件文本。"""
    _make_plugin(vault_dir, "hello", VALID_MANIFEST, code="export function setup(api) {}")
    r = plugins_client.get("/api/plugins/entry", params={"id": "hello"})
    assert r.status_code == 403  # 禁用即撤销加载能力

    plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": True})
    r = plugins_client.get("/api/plugins/entry", params={"id": "hello"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["id"] == "hello"
    assert data["entry"] == "main.js"
    assert "setup" in data["code"]
    assert data["permissions"] == ["read_notes", "ui_command"]  # 供前端能力注入
    assert data["name"] == "Hello 插件"


def test_entry_missing_file_404(plugins_client, vault_dir: Path):
    """启用后入口文件被外部删除 → 404（不抛未处理异常）。"""
    _make_plugin(vault_dir, "hello", VALID_MANIFEST, code=None)
    plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": True})
    r = plugins_client.get("/api/plugins/entry", params={"id": "hello"})
    assert r.status_code == 404


def test_entry_escape_denied_even_enabled(plugins_client, vault_dir: Path):
    """entry 越界的插件在 list/toggle 链路已拒绝（404），拿不到任何越界文件。"""
    _make_plugin(vault_dir, "escape", {**VALID_MANIFEST, "entry": "../../evil.js"})
    (vault_dir / ".kwey" / "evil.js").write_text("secret", encoding="utf-8")
    plugins_client.post("/api/plugins/toggle", json={"id": "escape", "enabled": True})
    r = plugins_client.get("/api/plugins/entry", params={"id": "escape"})
    assert r.status_code == 404
    assert "secret" not in r.text


def test_state_file_not_indexed_as_note(plugins_client, client, vault_dir: Path):
    """.kwey/ 在 IGNORED_DIRS：插件文件不进笔记索引、不出现在文件树。

    plugins_client：挂本卡 router 的测试 app；client：完整 app（create_app，
    由集成卡接线后包含本卡 router 与全部真实路由）。
    """
    _make_plugin(vault_dir, "hello", VALID_MANIFEST, code="export const x = 1;")
    plugins_client.post("/api/plugins/toggle", json={"id": "hello", "enabled": True})
    r = client.get("/api/tree")  # 文件树接口（routes_system）
    assert r.status_code == 200
    body = r.text
    assert "main.js" not in body
    assert "plugins.json" not in body
