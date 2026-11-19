"""C1 Canvas：.canvas CRUD、路径校验、结构校验、非笔记索引隔离与事件。

路由用「只挂 canvas router 的 FastAPI 应用」验证（server.py 属共享文件，
由集成卡统一注册）；服务族仍用 conftest 的 services fixture。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_canvas import MAX_NODES, MAX_NODE_TEXT, router as canvas_router


@pytest.fixture
def canvas_client(services) -> TestClient:
    app = FastAPI()
    app.include_router(canvas_router)
    with TestClient(app) as client:
        yield client


def _text_node(node_id: str = "n1", **overrides) -> dict:
    node = {
        "id": node_id,
        "type": "text",
        "text": "卡片内容",
        "x": 12,
        "y": 24,
        "width": 240,
        "height": 120,
    }
    node.update(overrides)
    return node


def _file_node(node_id: str = "f1", file: str = "欢迎.md") -> dict:
    return {
        "id": node_id,
        "type": "file",
        "file": file,
        "x": 400,
        "y": 24,
        "width": 240,
        "height": 120,
    }


def _edge(edge_id: str = "e1", source: str = "n1", target: str = "f1") -> dict:
    return {"id": edge_id, "fromNode": source, "toNode": target}


# ------------------------------------------------------------------ 列表
class TestList:
    def test_empty_vault_returns_empty_list(self, canvas_client: TestClient):
        resp = canvas_client.get("/api/canvas/list")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["code"] == 0
        assert payload["data"] == {"canvases": [], "count": 0}

    def test_lists_canvas_files_and_skips_reserved_dirs(
        self, canvas_client: TestClient, vault_dir: Path
    ):
        (vault_dir / ".kwey").mkdir(exist_ok=True)
        (vault_dir / ".kwey" / "隐藏.canvas").write_text("{}", encoding="utf-8")
        (vault_dir / ".trash" / "旧画布.canvas").parent.mkdir(exist_ok=True)
        (vault_dir / ".trash" / "旧画布.canvas").write_text("{}", encoding="utf-8")
        (vault_dir / "根画布.canvas").write_text(
            json.dumps({"nodes": [], "edges": []}), encoding="utf-8"
        )
        (vault_dir / "boards").mkdir()
        (vault_dir / "boards" / "项目.canvas").write_text(
            json.dumps({"nodes": [], "edges": []}), encoding="utf-8"
        )

        data = canvas_client.get("/api/canvas/list").json()["data"]
        paths = [item["path"] for item in data["canvases"]]
        assert "根画布.canvas" in paths
        assert "boards/项目.canvas" in paths
        assert all(not p.startswith(".kwey/") and not p.startswith(".trash/") for p in paths)
        item = next(i for i in data["canvases"] if i["path"] == "boards/项目.canvas")
        assert item["name"] == "项目"
        assert item["size"] > 0
        assert item["mtime"] > 0


# ------------------------------------------------------------------ 保存 / 读取
class TestSaveLoad:
    def test_create_then_load_roundtrip(self, canvas_client: TestClient, vault_dir: Path):
        data = {
            "nodes": [_text_node(), _file_node()],
            "edges": [_edge()],
        }
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "我的画布.canvas", "data": data}
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["data"]
        assert result["path"] == "我的画布.canvas"
        assert result["created"] is True
        assert result["nodes"] == 2 and result["edges"] == 1
        assert result["mtime"] > 0

        abs_path = vault_dir / "我的画布.canvas"
        assert abs_path.is_file()
        on_disk = json.loads(abs_path.read_text(encoding="utf-8"))
        assert on_disk["nodes"][0]["x"] == 12  # 整数坐标保持 int
        assert on_disk["nodes"][0]["type"] == "text"
        assert on_disk["edges"][0]["fromNode"] == "n1"

        loaded = canvas_client.get(
            "/api/canvas/load", params={"path": "我的画布.canvas"}
        )
        assert loaded.status_code == 200
        payload = loaded.json()["data"]
        assert payload["path"] == "我的画布.canvas"
        assert payload["data"] == on_disk
        assert payload["mtime"] == pytest.approx(result["mtime"])

    def test_empty_data_creates_blank_canvas(self, canvas_client: TestClient):
        resp = canvas_client.post("/api/canvas/save", json={"path": "空白.canvas"})
        assert resp.status_code == 200
        loaded = canvas_client.get("/api/canvas/load", params={"path": "空白.canvas"})
        assert loaded.json()["data"]["data"] == {"nodes": [], "edges": []}

    def test_preserves_unknown_obsidian_fields(self, canvas_client: TestClient):
        data = {
            "nodes": [
                _text_node(color="4"),
                _file_node(),
            ],
            "edges": [{**_edge(), "fromSide": "right", "toSide": "left", "label": "关联"}],
            "meta": {"zoom": 1.25, "custom": "保留"},
        }
        canvas_client.post("/api/canvas/save", json={"path": "兼容.canvas", "data": data})
        loaded = canvas_client.get("/api/canvas/load", params={"path": "兼容.canvas"}).json()["data"]
        assert loaded["data"]["nodes"][0]["color"] == "4"
        assert loaded["data"]["edges"][0]["fromSide"] == "right"
        assert loaded["data"]["meta"] == {"zoom": 1.25, "custom": "保留"}

    def test_second_save_updates_in_place(self, canvas_client: TestClient, vault_dir: Path):
        canvas_client.post(
            "/api/canvas/save", json={"path": "更新.canvas", "data": {"nodes": [_text_node()]}}
        )
        first = (vault_dir / "更新.canvas").read_text(encoding="utf-8")
        resp = canvas_client.post(
            "/api/canvas/save",
            json={
                "path": "更新.canvas",
                "data": {"nodes": [_text_node(text="改过的内容")], "edges": []},
            },
        )
        assert resp.json()["data"]["created"] is False
        second = (vault_dir / "更新.canvas").read_text(encoding="utf-8")
        assert second != first
        assert "改过的内容" in second

    def test_nested_path_creates_directories(self, canvas_client: TestClient, vault_dir: Path):
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "boards/子目录/架构.canvas", "data": {}}
        )
        assert resp.status_code == 200
        assert (vault_dir / "boards" / "子目录" / "架构.canvas").is_file()

    def test_canvas_is_plain_file_not_note(self, canvas_client: TestClient, services):
        canvas_client.post(
            "/api/canvas/save", json={"path": "普通文件.canvas", "data": {"nodes": [_text_node()]}}
        )
        services.require_indexer().full_scan()

        db = services.require_db()
        assert db.get_note("普通文件.canvas") is None
        assert len(db.all_notes()) == 3  # conftest 的三篇笔记

        paths = []

        def walk(nodes):
            for node in nodes:
                paths.append((node["path"], node["type"]))
                walk(node.get("children") or [])

        walk(services.require_vault().tree())
        assert ("普通文件.canvas", "file") in paths

    def test_save_publishes_created_then_updated(self, canvas_client: TestClient, services):
        events = []
        services.require_eventbus().subscribe(
            None, lambda e: events.append((e.event.value, e.path))
        )
        canvas_client.post("/api/canvas/save", json={"path": "事件.canvas", "data": {}})
        canvas_client.post(
            "/api/canvas/save",
            json={"path": "事件.canvas", "data": {"nodes": [_text_node()]}},
        )
        assert ("created", "事件.canvas") in events
        assert ("updated", "事件.canvas") in events

    def test_prefers_service_write_file_when_integrated(
        self, canvas_client: TestClient, services, monkeypatch
    ):
        """集成卡给 VaultService 补上 write_file 后，画布保存自动走统一写路径。"""
        vault = services.require_vault()
        calls = []

        def fake_write_file(rel, text):
            calls.append((rel, text))
            abs_path = vault.paths.to_abs(rel)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(text, encoding="utf-8")
            return {"path": rel, "created": True, "mtime": 123.0}

        monkeypatch.setattr(vault, "write_file", fake_write_file, raising=False)
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "统一写.canvas", "data": {"nodes": [_text_node()]}}
        )
        assert resp.status_code == 200
        assert calls and calls[0][0] == "统一写.canvas"
        assert resp.json()["data"]["created"] is True

    def test_list_skips_extension_only_filename(
        self, canvas_client: TestClient, vault_dir: Path
    ):
        (vault_dir / ".canvas").write_text("{}", encoding="utf-8")
        data = canvas_client.get("/api/canvas/list").json()["data"]
        assert data["canvases"] == []


# ------------------------------------------------------------------ 路径校验
class TestPathValidation:
    @pytest.mark.parametrize(
        "path",
        [
            "",
            "../越界.canvas",
            ".kwey/隐藏.canvas",
            ".trash/旧.canvas",
            "notes/不是画布.txt",
            "/绝对.canvas",
            "C:/temp/绝对.canvas",
            ".canvas",
            "目录/.canvas",
        ],
    )
    def test_save_rejects_bad_paths(self, canvas_client: TestClient, path: str):
        resp = canvas_client.post("/api/canvas/save", json={"path": path, "data": {}})
        assert resp.status_code == 400, (path, resp.text)
        assert resp.json()["detail"]

    def test_load_rejects_bad_paths(self, canvas_client: TestClient):
        resp = canvas_client.get("/api/canvas/load", params={"path": "notes/欢迎.md"})
        assert resp.status_code == 400
        resp = canvas_client.get("/api/canvas/load", params={"path": "../越界.canvas"})
        assert resp.status_code == 400

    def test_missing_canvas_returns_404(self, canvas_client: TestClient):
        resp = canvas_client.get("/api/canvas/load", params={"path": "不存在.canvas"})
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_traversal_does_not_write_outside_vault(
        self, canvas_client: TestClient, vault_dir: Path
    ):
        outside = vault_dir.parent / "越界.canvas"
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "../越界.canvas", "data": {}}
        )
        assert resp.status_code == 400
        assert not outside.exists()


# ------------------------------------------------------------------ 结构校验
class TestDataValidation:
    @pytest.mark.parametrize(
        "data,keyword",
        [
            ({"nodes": "不是数组"}, "nodes"),
            ({"edges": {}}, "edges"),
            ({"nodes": ["字符串"]}, "对象"),
            ({"nodes": [{"type": "text"}]}, "id"),
            ({"nodes": [_text_node(), _text_node()]}, "重复"),
            ({"nodes": [_text_node(type="unknown")]}, "type"),
            ({"nodes": [_text_node(x="左")]}, "数字"),
            ({"nodes": [_text_node(width=0)]}, "正数"),
            ({"nodes": [{"id": "f", "type": "file"}]}, "file"),
            ({"nodes": [{"id": "f", "type": "file", "file": "../逃逸.md"}]}, "file"),
            ({"nodes": [{"id": "l", "type": "link"}]}, "url"),
            ({"nodes": [_text_node()], "edges": [{"id": "e1", "fromNode": "n1", "toNode": "缺失"}]}, "不存在"),
            ({"nodes": [_text_node()], "edges": [_edge("e1", "n1", "n1")]}, "自身"),
        ],
    )
    def test_invalid_data_returns_400(self, canvas_client: TestClient, data, keyword):
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "非法.canvas", "data": data}
        )
        assert resp.status_code == 400, resp.text
        assert keyword in resp.json()["detail"]

    def test_duplicate_edge_pair_rejected(self, canvas_client: TestClient):
        data = {
            "nodes": [_text_node(), _file_node()],
            "edges": [_edge("e1"), _edge("e2")],
        }
        resp = canvas_client.post("/api/canvas/save", json={"path": "重复线.canvas", "data": data})
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_too_many_nodes_rejected(self, canvas_client: TestClient):
        nodes = [{"id": f"n{i}", "type": "text", "text": "x"} for i in range(MAX_NODES + 1)]
        resp = canvas_client.post(
            "/api/canvas/save", json={"path": "过多.canvas", "data": {"nodes": nodes}}
        )
        assert resp.status_code == 400
        assert "节点过多" in resp.json()["detail"]

    def test_text_too_long_rejected(self, canvas_client: TestClient):
        resp = canvas_client.post(
            "/api/canvas/save",
            json={"path": "长文.canvas", "data": {"nodes": [_text_node(text="字" * (MAX_NODE_TEXT + 1))]}},
        )
        assert resp.status_code == 400
        assert "过长" in resp.json()["detail"]

    def test_invalid_save_does_not_touch_existing_file(
        self, canvas_client: TestClient, vault_dir: Path
    ):
        canvas_client.post(
            "/api/canvas/save", json={"path": "稳定.canvas", "data": {"nodes": [_text_node()]}}
        )
        before = (vault_dir / "稳定.canvas").read_bytes()
        resp = canvas_client.post(
            "/api/canvas/save",
            json={"path": "稳定.canvas", "data": {"nodes": [], "edges": [_edge("e1", "a", "b")]}},
        )
        assert resp.status_code == 400
        assert (vault_dir / "稳定.canvas").read_bytes() == before


# ------------------------------------------------------------------ 读取异常
class TestLoadErrors:
    def test_invalid_json_returns_400(self, canvas_client: TestClient, vault_dir: Path):
        (vault_dir / "坏掉.canvas").write_text("{ 不是 JSON", encoding="utf-8")
        resp = canvas_client.get("/api/canvas/load", params={"path": "坏掉.canvas"})
        assert resp.status_code == 400
        assert "JSON" in resp.json()["detail"]

    def test_invalid_structure_returns_400(self, canvas_client: TestClient, vault_dir: Path):
        (vault_dir / "结构坏.canvas").write_text(
            json.dumps({"nodes": [{"id": "a"}, {"id": "a"}]}), encoding="utf-8"
        )
        resp = canvas_client.get("/api/canvas/load", params={"path": "结构坏.canvas"})
        assert resp.status_code == 400
        assert "重复" in resp.json()["detail"]

    def test_non_utf8_returns_400(self, canvas_client: TestClient, vault_dir: Path):
        (vault_dir / "乱码.canvas").write_bytes(b"\xff\xfe\x00\x01")
        resp = canvas_client.get("/api/canvas/load", params={"path": "乱码.canvas"})
        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["detail"]
