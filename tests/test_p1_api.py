"""P1 后端 API 集成测试：回收站 / 图谱 / 模板 / 导出 / 大纲 / settings 扩展。

使用 conftest 提供的 client / vault / vault_dir / services fixtures；
回收站条目如需预置，直接写盘模拟（不走删除 API）。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api import deps


def _body(resp):
    """断言统一 {code,data,message} 包装且 code=0，返回完整 payload。"""
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload.keys()) >= {"code", "data", "message"}
    assert payload["code"] == 0, payload
    return payload


def _write_trash(vault_dir: Path, rel: str, text: str = "# 废弃\n") -> str:
    """直接写盘模拟回收站条目（rel 相对 .trash/），返回 trash_path。"""
    p = vault_dir / ".trash" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return f".trash/{rel}"


# ---------------------------------------------------------------- 回收站
class TestTrash:
    def test_empty_trash(self, client):
        assert _body(client.get("/api/trash"))["data"]["items"] == []

    def test_delete_then_list(self, client):
        _body(client.delete("/api/note", params={"path": "欢迎.md"}))
        items = _body(client.get("/api/trash"))["data"]["items"]
        hit = [i for i in items if i["original_path"] == "欢迎.md"]
        assert hit, items
        item = hit[0]
        assert item["trash_path"] == ".trash/欢迎.md"
        assert item["name"] == "欢迎.md"
        assert item["is_dir"] is False
        assert isinstance(item["mtime"], float)
        # 按 mtime 倒序
        assert [i["mtime"] for i in items] == sorted((i["mtime"] for i in items), reverse=True)

    def test_restore_reindex_and_readable(self, client):
        _body(client.delete("/api/note", params={"path": "欢迎.md"}))
        db = deps.require_db()
        assert db.get_note("欢迎.md") is None
        data = _body(client.post("/api/trash/restore", json={
            "trash_path": ".trash/欢迎.md",
        }))["data"]
        assert data["path"] == "欢迎.md"
        assert "rewritten_links" in data and isinstance(data["rewritten_links"], int)
        doc = _body(client.get("/api/note", params={"path": "欢迎.md"}))["data"]
        assert doc["title"] == "欢迎"
        assert db.get_note("欢迎.md") is not None  # 索引已恢复
        assert _body(client.get("/api/trash"))["data"]["items"] == []

    def test_restore_dedup_when_target_exists(self, client, vault_dir):
        # 欢迎.md 本就存在：恢复到同名目标须自动去重、不覆盖
        trash = _write_trash(vault_dir, "废弃.md", "# 废弃内容\n")
        data = _body(client.post("/api/trash/restore", json={
            "trash_path": trash, "target_path": "欢迎.md",
        }))["data"]
        assert data["path"] != "欢迎.md"
        assert "欢迎" in data["path"] and "(1)" in data["path"]
        assert _body(client.get("/api/note", params={"path": "欢迎.md"}))["data"]["title"] == "欢迎"
        new_doc = _body(client.get("/api/note", params={"path": data["path"]}))["data"]
        assert "废弃内容" in new_doc["content"]

    def test_restore_directory_reindexes_each_file(self, client, vault_dir):
        # 模拟 delete_dir 产生的 .trash 目录结构（顶层目录条目 is_dir=True）
        (vault_dir / ".trash" / "归档" / "子").mkdir(parents=True)
        (vault_dir / ".trash" / "归档" / "二.md").write_text("# 二\n", encoding="utf-8")
        (vault_dir / ".trash" / "归档" / "子" / "一.md").write_text("# 一\n", encoding="utf-8")
        items = _body(client.get("/api/trash"))["data"]["items"]
        dir_item = [i for i in items if i["trash_path"] == ".trash/归档"]
        assert dir_item and dir_item[0]["is_dir"] is True
        assert dir_item[0]["original_path"] == "归档"

        data = _body(client.post("/api/trash/restore", json={
            "trash_path": ".trash/归档",
        }))["data"]
        assert data["path"] == "归档"
        db = deps.require_db()
        assert db.get_note("归档/二.md") is not None
        assert db.get_note("归档/子/一.md") is not None
        assert _body(client.get("/api/note", params={"path": "归档/子/一.md"}))["data"]["title"] == "一"

    def test_purge_single_and_clear_all(self, client, vault_dir):
        trash1 = _write_trash(vault_dir, "a.md")
        trash2 = _write_trash(vault_dir, "b.md")
        d = _body(client.delete("/api/trash", params={"path": trash1}))["data"]
        assert d["deleted"] == 1
        assert not (vault_dir / trash1).exists()
        # 清空全部必须 confirm=1
        assert client.delete("/api/trash").status_code == 400
        assert client.delete("/api/trash", params={"confirm": 0}).status_code == 400
        d2 = _body(client.delete("/api/trash", params={"confirm": 1}))["data"]
        assert d2["deleted"] >= 1
        assert not (vault_dir / trash2).exists()
        assert _body(client.get("/api/trash"))["data"]["items"] == []


# ---------------------------------------------------------------- 图谱
class TestGraph:
    def test_full_graph_nodes_edges(self, client):
        g = _body(client.get("/api/graph/full"))["data"]
        paths = {n["path"] for n in g["nodes"]}
        assert {"欢迎.md", "notes/读书/2026计划.md", "notes/工作/会议记录.md"} <= paths
        assert all(n["path"].endswith(".md") for n in g["nodes"])  # 仅 md 笔记
        assert all(set(n.keys()) == {"path", "title"} for n in g["nodes"])
        seen = set()
        for e in g["edges"]:
            assert e["source"] and e["target"] and e["kind"]  # 只含已解析链接
            key = (e["source"], e["target"], e["kind"])
            assert key not in seen  # 去重
            seen.add(key)
        assert any(
            e["source"] == "欢迎.md" and e["target"] == "notes/读书/2026计划.md"
            for e in g["edges"]
        )
        for u in g["unresolved"]:
            assert set(u.keys()) == {"source", "target"}

    def test_local_graph_only_neighborhood(self, client):
        g = _body(client.get("/api/graph/local", params={"path": "欢迎.md", "depth": 1}))["data"]
        paths = {n["path"] for n in g["nodes"]}
        assert "欢迎.md" in paths
        assert "notes/读书/2026计划.md" in paths
        # 会议记录与当前笔记无链接关系，不在 1 跳邻域
        assert "notes/工作/会议记录.md" not in paths
        for e in g["edges"]:
            assert e["source"] in paths and e["target"] in paths

    def test_local_depth_out_of_range_400(self, client):
        assert client.get("/api/graph/local", params={"path": "欢迎.md", "depth": 0}).status_code == 400
        assert client.get("/api/graph/local", params={"path": "欢迎.md", "depth": 4}).status_code == 400


# ---------------------------------------------------------------- 模板
class TestTemplates:
    def test_template_dir_missing_returns_empty(self, client):
        assert _body(client.get("/api/templates"))["data"] == []

    def test_template_not_indexed_nor_in_tree(self, client, vault_dir):
        tp = vault_dir / "templates"
        tp.mkdir()
        (tp / "模板笔记.md").write_text("# 模板\n", encoding="utf-8")
        deps.require_indexer().full_scan()  # 即使全量重建也不应收录
        assert deps.require_db().get_note("templates/模板笔记.md") is None
        names = {e["name"] for e in _body(client.get("/api/tree"))["data"]}
        assert "templates" not in names

    def test_list_content_and_apply_placeholders(self, client, vault_dir):
        tp = vault_dir / "templates"
        tp.mkdir()
        (tp / "日记.md").write_text(
            # 占位符作为 YAML 值须加引号，否则 {{title}} 会被当作非法 flow mapping
            "---\ntitle: \"{{title}}\"\nfoo: 保留\n---\n\n# {{title}}\n\n"
            "日期 {{date}} 时间 {{time}} 完整 {{datetime}}\n",
            encoding="utf-8",
        )
        items = _body(client.get("/api/templates"))["data"]
        assert any(i["name"] == "日记" and i["path"] == "templates/日记.md" for i in items)

        c = _body(client.get("/api/templates/content", params={"path": "templates/日记.md"}))["data"]
        assert "{{date}}" in c["content"]
        assert c["frontmatter"]["title"] == "{{title}}"

        data = _body(client.post("/api/templates/apply", json={
            "template_path": "templates/日记.md",
        }))["data"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}-日记\.md$", data["path"])
        assert "title" in data
        doc = _body(client.get("/api/note", params={"path": data["path"]}))["data"]
        assert "{{" not in doc["content"]
        assert "{{" not in str(doc["frontmatter"])  # frontmatter 内占位符同样替换
        assert re.search(r"\d{4}-\d{2}-\d{2}", doc["content"])
        assert "日期" in doc["content"] and "时间" in doc["content"]


# ---------------------------------------------------------------- 导出
class TestExport:
    def test_export_md_raw_with_frontmatter(self, client):
        d = _body(client.get("/api/export", params={"path": "欢迎.md", "format": "md"}))["data"]
        assert d["filename"] == "欢迎.md"
        assert d["content"].startswith("---")
        assert "title: 欢迎" in d["content"]
        assert d["mime"].startswith("text/markdown")

    def test_export_html_standalone(self, client):
        d = _body(client.get("/api/export", params={"path": "欢迎.md", "format": "html"}))["data"]
        assert d["filename"] == "欢迎.html"
        assert "<!DOCTYPE html>" in d["content"]
        assert '<meta charset="utf-8">' in d["content"]
        assert "<style>" in d["content"]
        assert "<h1>" in d["content"]
        assert "欢迎" in d["content"]
        assert "<p>" in d["content"]  # render_markdown 产物
        assert d["mime"].startswith("text/html")

    def test_export_html_image_inlined(self, client, vault_dir):
        (vault_dir / "带图.md").write_text(
            "# 带图\n\n![示意图](attachments/pic.png)\n", encoding="utf-8"
        )
        d = _body(client.get("/api/export", params={"path": "带图.md", "format": "html"}))["data"]
        assert "data:image/png;base64," in d["content"]


# ---------------------------------------------------------------- 大纲
class TestOutline:
    def test_headings_levels_and_skip_code(self, client, vault_dir):
        content = (
            "# 一级\n\n正文\n\n## 二级\n\n"
            "```\n# 假标题\n```\n\n"
            "行内代码 `# 不是标题`\n\n"
            "### 三级\n"
        )
        (vault_dir / "大纲.md").write_text(content, encoding="utf-8")
        h = _body(client.get("/api/note/outline", params={"path": "大纲.md"}))["data"]["headings"]
        assert [x["level"] for x in h] == [1, 2, 3]
        assert [x["text"] for x in h] == ["一级", "二级", "三级"]
        assert all(isinstance(x["line"], int) and x["line"] >= 1 for x in h)
        assert not any("假标题" in x["text"] or "不是标题" in x["text"] for x in h)


# ---------------------------------------------------------------- settings
class TestSettings:
    def test_new_fields_put_get_roundtrip(self, client):
        payload = {
            "template_dir": "我的模板",
            "daily_dir": "每日",
            "bookmarks": ["欢迎.md", "notes/读书/2026计划.md"],
            "recent": ["欢迎.md"],
            "hotkeys": {"open": "Ctrl+O", "palette": "Ctrl+P"},
            "sidebar_width": 320,
            "rightbar_width": 300,
            "left_panel": "search",
            "right_panel": "outline",
        }
        data = _body(client.put("/api/settings", json=payload))["data"]
        for k, v in payload.items():
            assert data[k] == v, (k, data.get(k))
        data2 = _body(client.get("/api/settings"))["data"]
        for k, v in payload.items():
            assert data2[k] == v, (k, data2.get(k))

    def test_agent_and_vector_writable(self, client):
        # M2 开放 agent/snapshots；A2 开放 vector（子键合并写入）
        data = _body(client.put("/api/settings", json={
            "agent": {"enabled": True},
            "vector": {"enabled": True, "weight": 2.0},
            "theme": "dark",
        }))["data"]
        assert data["agent"]["enabled"] is True
        assert data["vector"]["enabled"] is True
        assert data["vector"]["weight"] == 2.0
        # 未提交的子键保留默认（合并而非整体覆盖）
        assert data["vector"]["model"] == "bge-small-zh-v1.5"
        assert data["theme"] == "dark"


# ---------------------------------------------------------------- 路径/参数校验
class TestValidation:
    def test_invalid_export_format_400(self, client):
        # pdf 现为合法格式（PDF 依赖已落地）；真正非法的格式才 400
        assert client.get("/api/export", params={"path": "欢迎.md", "format": "docx"}).status_code == 400
        assert client.get("/api/export", params={"path": "欢迎.md", "format": ""}).status_code == 400

    def test_traversal_paths_400(self, client):
        assert client.get("/api/export", params={"path": "../secret.md", "format": "md"}).status_code == 400
        assert client.get("/api/note/outline", params={"path": "../secret.md"}).status_code == 400

    def test_template_content_outside_dir_400(self, client):
        assert client.get("/api/templates/content", params={"path": "../.kwey/settings.json"}).status_code == 400
        assert client.get("/api/templates/content", params={"path": "notes/读书/2026计划.md"}).status_code == 400

    def test_trash_paths_outside_trash_400(self, client):
        # 恢复/永久删除只允许 .trash/ 内部路径
        assert client.post("/api/trash/restore", json={"trash_path": "notes/读书/2026计划.md"}).status_code == 400
        assert client.post("/api/trash/restore", json={"trash_path": ".trash/../欢迎.md"}).status_code == 400
        assert client.delete("/api/trash", params={"path": "../欢迎.md"}).status_code == 400
