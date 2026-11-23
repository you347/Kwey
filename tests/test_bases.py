"""Bases 式结构化视图：查询引擎 / 定义持久化 / 单元格写回 / Obsidian 兼容。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api import deps
from app.views import ViewStore, normalize_definition, run_query
from app.views.query import parse_where
from app.views.schema import build_schema, infer_type
from app.views.store import obsidian_base_to_definition

PROJECTS = {
    "projects/alpha.md": """---
title: Alpha 重构
status: doing
priority: 3
due: 2026-12-05
tags: [project, 后端]
---

# Alpha 重构

把检索层换成 sqlite-vec。
""",
    "projects/beta.md": """---
title: Beta 上线
status: done
priority: 1
due: 2026-11-20
tags: [project]
---

# Beta 上线

已经发版。
""",
    "projects/gamma.md": """---
title: Gamma 调研
status: todo
priority: 5
due: 2026-12-20
tags: [project, 调研]
---

# Gamma 调研

看看竞品怎么做视图。
""",
    "daily/2026-11-23.md": """---
title: 日记
mood: ok
---

今天写视图。
""",
}


@pytest.fixture
def bases_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for rel, text in PROJECTS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def bases_services(bases_vault: Path):
    deps.reset_services()
    deps.init_services(bases_vault, open_now=True)
    yield deps
    deps.reset_services()


@pytest.fixture
def bases_client(bases_services):
    from fastapi.testclient import TestClient

    from app.server import create_app

    with TestClient(create_app()) as client:
        yield client


# --------------------------------------------------------------------- 引擎
class TestQueryEngine:
    def test_folder_and_tags(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        result = run_query(snapshot, normalize_definition({"name": "x", "folder": "projects", "tags": ["project"]}))
        assert result["total"] == 3
        assert all(row["path"].startswith("projects/") for row in result["rows"])

    def test_where_and_sort(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        defn = normalize_definition({
            "name": "未完成",
            "folder": "projects",          # 全库时日记没有 status，!= done 也会命中（语义正确）
            "where": "status != done",
            "sort": [{"key": "priority", "dir": "desc"}],
        })
        result = run_query(snapshot, defn)
        assert [row["title"] for row in result["rows"]] == ["Gamma 调研", "Alpha 重构"]

    def test_before_operator_on_date(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        defn = normalize_definition({"name": "x", "where": [{"key": "due", "op": "before", "value": "2026-12-01"}]})
        result = run_query(snapshot, defn)
        assert {row["title"] for row in result["rows"]} == {"Beta 上线"}

    def test_in_and_empty(self):
        snapshot = {
            "notes": {
                "a.md": {"path": "a.md", "title": "A", "mtime": 1},
                "b.md": {"path": "b.md", "title": "B", "mtime": 2},
            },
            "props": {"a.md": {"status": "todo"}, "b.md": {}},
            "tags": {},
        }
        defn = normalize_definition({"name": "x", "where": [{"key": "status", "op": "in", "value": ["todo", "doing"]}]})
        assert [r["title"] for r in run_query(snapshot, defn)["rows"]] == ["A"]
        defn2 = normalize_definition({"name": "x", "where": [{"key": "status", "op": "empty"}]})
        assert [r["title"] for r in run_query(snapshot, defn2)["rows"]] == ["B"]

    def test_limit_and_total(self):
        snapshot = {
            "notes": {f"{i}.md": {"path": f"{i}.md", "title": f"N{i}", "mtime": i} for i in range(10)},
            "props": {},
            "tags": {},
        }
        defn = normalize_definition({"name": "x", "limit": 3})
        result = run_query(snapshot, defn)
        assert result["total"] == 10 and result["returned"] == 3

    def test_group_and_aggregates(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        defn = normalize_definition({
            "name": "x", "folder": "projects", "group_by": "status",
            "columns": ["title", "status", "priority"],
        })
        result = run_query(snapshot, defn)
        assert {g["label"] for g in result["groups"]} == {"doing", "done", "todo"}
        assert result["aggregates"]["priority"]["sum"] == 9.0

    def test_auto_columns_pick_props(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        result = run_query(snapshot, normalize_definition({"name": "x"}))
        keys = [col["key"] for col in result["columns"]]
        assert keys[0] == "title"
        assert "status" in keys or "priority" in keys

    def test_parse_where_string(self):
        conds = parse_where("status != done and priority >= 2 and tags contains 调研")
        assert [c["op"] for c in conds] == ["!=", ">=", "contains"]
        assert conds[1]["value"] == 2.0

    def test_unknown_key_does_not_crash(self, bases_services):
        from app.views.query import cached_snapshot

        snapshot = cached_snapshot(deps.require_vault().db)
        defn = normalize_definition({"name": "x", "where": [{"key": "不存在", "op": "exists"}]})
        assert run_query(snapshot, defn)["total"] == 0


class TestSchema:
    def test_types(self):
        assert infer_type([1, 2, 3]) == "number"
        assert infer_type([True, False]) == "checkbox"
        assert infer_type(["2026-11-01", "2026-12-01"]) == "date"
        assert infer_type([["a"], ["b"]]) == "list"
        assert infer_type(["x", "y"]) == "text"

    def test_build_schema_counts(self):
        schema = build_schema(
            {"a.md": {"status": "todo"}, "b.md": {"status": "done", "priority": 2}},
            {"a.md": {}, "b.md": {}},
        )
        by_key = {item["key"]: item for item in schema["keys"]}
        assert by_key["status"]["count"] == 2
        assert by_key["status"]["type"] == "text"
        assert by_key["priority"]["count"] == 1
        assert {v["value"] for v in by_key["status"]["values"]} == {"todo", "done"}


class TestStore:
    def test_save_list_read_delete(self, tmp_path: Path):
        store = ViewStore(tmp_path)
        info = store.save({"name": "项目进度", "folder": "projects"})
        assert info["slug"] == "项目进度"
        assert (tmp_path / "views" / "项目进度.base").is_file()
        assert store.read(info["slug"])["folder"] == "projects"
        assert [item["slug"] for item in store.list()] == ["项目进度"]
        assert store.delete(info["slug"]) is True
        assert store.list() == []

    def test_duplicate_name_gets_suffix(self, tmp_path: Path):
        store = ViewStore(tmp_path)
        first = store.save({"name": "重复"})
        second = store.save({"name": "重复"})
        assert first["slug"] != second["slug"]
        assert second["slug"] == "重复-2"

    def test_path_traversal_stays_inside_views_dir(self, tmp_path: Path):
        store = ViewStore(tmp_path)
        assert store.read("../../etc/passwd") is None
        # 斜杠/点被剥离，最终文件永远落在 views/ 内（不抛错，也不越界）
        path = store.path_for("../../etc/passwd")
        assert path.parent == tmp_path / "views"
        info = store.save({"name": "x"}, slug="../evil")
        assert (tmp_path / "views" / f"{info['slug']}.base").is_file()

    def test_unknown_file_is_readable(self, tmp_path: Path):
        views = tmp_path / "views"
        views.mkdir()
        (views / "plain.base").write_text("name: 手写\nmode: cards\n", encoding="utf-8")
        assert ViewStore(tmp_path).read("plain")["mode"] == "cards"


class TestObsidianCompat:
    def test_convert_common_subset(self):
        raw = {
            "filters": {"and": ['file.hasTag("book")', 'status != "done"', 'file.inFolder("reading")']},
            "views": [{
                "type": "cards",
                "name": "在读",
                "order": ["file.name", "status"],
                "sort": [{"property": "status", "direction": "DESC"}],
            }],
        }
        defn = obsidian_base_to_definition(raw)
        assert defn["name"] == "在读"
        assert defn["mode"] == "cards"
        assert defn["tags"] == ["book"]
        assert defn["folder"] == "reading"
        assert defn["where"][0]["key"] == "status"
        assert defn["sort"][0]["dir"] == "desc"

    def test_non_bases_file_returns_none(self):
        assert obsidian_base_to_definition({"name": "x"}) is None


# --------------------------------------------------------------------- API
class TestApi:
    def test_crud_and_query(self, bases_client):
        created = bases_client.post("/api/views", json={"definition": {"name": "项目", "folder": "projects"}})
        assert created.status_code == 200
        slug = created.json()["data"]["slug"]

        listing = bases_client.get("/api/views").json()["data"]
        assert listing["total"] == 1

        result = bases_client.post("/api/views/query", json={"slug": slug, "include_schema": True}).json()["data"]
        assert result["total"] == 3
        assert result["schema"]["keys"]

        assert bases_client.delete(f"/api/views/{slug}").status_code == 200
        assert bases_client.get("/api/views").json()["data"]["total"] == 0

    def test_cell_writeback_preserves_body(self, bases_client):
        # 先把 alpha 的 status 从 doing 改成 review
        res = bases_client.put("/api/views/cell", json={
            "path": "projects/alpha.md", "key": "status", "value": "review",
        })
        assert res.status_code == 200, res.text
        assert res.json()["data"]["value"] == "review"

        text = (Path(deps.require_vault().paths.root) / "projects/alpha.md").read_text(encoding="utf-8")
        assert "status: review" in text
        assert "把检索层换成 sqlite-vec。" in text     # 正文原样保留

        # 索引同步：再查能筛到
        result = bases_client.post("/api/views/query", json={
            "definition": {"name": "x", "where": [{"key": "status", "op": "=", "value": "review"}]},
        }).json()["data"]
        assert result["total"] == 1

    def test_cell_number_and_bool(self, bases_client):
        res = bases_client.put("/api/views/cell", json={
            "path": "projects/alpha.md", "key": "priority", "value": "4",
        })
        assert res.json()["data"]["value"] == 4
        res = bases_client.put("/api/views/cell", json={
            "path": "projects/alpha.md", "key": "pinned", "value": True,
        })
        assert res.json()["data"]["value"] is True

    def test_cell_rejects_readonly(self, bases_client):
        res = bases_client.put("/api/views/cell", json={"path": "projects/alpha.md", "key": "title", "value": "x"})
        assert res.status_code == 400

    def test_query_missing_definition(self, bases_client):
        assert bases_client.post("/api/views/query", json={}).status_code == 400

    def test_ai_without_provider(self, bases_client):
        res = bases_client.post("/api/views/ai", json={"prompt": "未完成的项目"})
        assert res.status_code == 400
        assert "Provider" in res.json()["message"]

    def test_missing_slug_404(self, bases_client):
        assert bases_client.get("/api/views/nope").status_code == 404
        assert bases_client.delete("/api/views/nope").status_code == 404
