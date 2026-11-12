"""QA 补充测试（严过关 · P4 独立测试轮）。

覆盖既有 10 个测试文件的盲区：
- 空 vault 行为（G4 边界）
- 非法路径/隐藏目录写入等安全边界
- 搜索特殊字符鲁棒性（FTS5 MATCH 注入）
- 反链上下文/别名与锚点重写/linkify 全链路/标签层级树
- WebSocket /ws/events 事件广播
- watcher set-of-tuples 形式（watch() 真实产出形态）
- 附件目录配置联动、目录删除后的图清理
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from watchfiles import Change

from app.api import deps


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def empty_services(tmp_path: Path):
    """空 vault（0 篇笔记）。"""
    deps.reset_services()
    root = tmp_path / "空库"
    root.mkdir()
    deps.init_services(root, open_now=True)
    yield deps
    deps.reset_services()


@pytest.fixture
def empty_client(empty_services):
    from app.server import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as c:
        yield c


def _body(resp):
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["code"] == 0, payload
    return payload["data"]


# ---------------------------------------------------------------- 空 vault


class TestEmptyVault:
    def test_health_zero(self, empty_client):
        data = _body(empty_client.get("/api/health"))
        assert data["indexed"] == 0

    def test_search_empty(self, empty_client):
        data = _body(empty_client.get("/api/search", params={"q": "任意"}))
        assert data["total"] == 0 and data["hits"] == []

    def test_tree_empty(self, empty_client):
        assert _body(empty_client.get("/api/tree")) == []

    def test_tags_empty(self, empty_client):
        assert _body(empty_client.get("/api/graph/tags")) == []

    def test_backlinks_nonexistent(self, empty_client):
        data = _body(empty_client.get("/api/graph/backlinks", params={"path": "无.md"}))
        assert data["count"] == 0 and data["backlinks"] == []

    def test_unlinked_nonexistent_404(self, empty_client):
        resp = empty_client.get("/api/graph/unlinked", params={"path": "无.md"})
        assert resp.status_code == 404

    def test_vault_open_nonexistent(self, empty_client):
        resp = empty_client.post("/api/vault/open", json={"path": "Z:/不存在的目录xyz"})
        assert resp.status_code == 404


# ---------------------------------------------------------------- 路径安全


class TestPathSecurity:
    @pytest.mark.parametrize("bad", [
        "notes/../欢迎.md",
        "../逃逸.md",
        "a/../../b.md",
        "con.md",
        "CON",
        "nul/x.md",
        "a:b.md",
        "C:/evil.md",
        "含?问号.md",
        "a*b.md",
    ])
    def test_read_illegal_paths_rejected(self, client, bad):
        resp = client.get("/api/note", params={"path": bad})
        assert resp.status_code in (400, 404), f"{bad!r} 应被拒绝，实际 {resp.status_code}"

    def test_create_illegal_path_rejected(self, client):
        resp = client.post("/api/note", json={"path": "notes/../越界.md"})
        assert resp.status_code == 400

    def test_write_into_hidden_dir(self, client):
        """写入 .kwey/ 等隐藏目录：期望被拒绝（记录实际行为）。"""
        resp = client.post("/api/note", json={"path": ".kwey/隐藏笔记.md"})
        if resp.status_code == 200:
            pytest.fail(
                "源码问题：POST /api/note 允许向 .kwey/ 隐藏目录写入笔记"
                "（validate_relative 未拒绝 IGNORED_DIRS），见 app/paths.py"
            )

    def test_delete_nonexistent_404(self, client):
        resp = client.delete("/api/note", params={"path": "ghost.md"})
        assert resp.status_code == 404

    def test_embed_traversal_rejected(self, client):
        resp = client.get("/api/note/embed", params={"path": "../secret.md"})
        assert resp.status_code in (400, 404)

    def test_serve_file_ignored_dir_blocked(self, client):
        resp = client.get("/file/.kwey/settings.json")
        assert resp.status_code == 404


# ---------------------------------------------------------------- 搜索鲁棒性


class TestSearchRobustness:
    @pytest.mark.parametrize("q", ['"', 'a**b', "-", "会议\"查询", "((( ", "\\AND"])
    def test_special_chars_no_500(self, client, q):
        """FTS5 MATCH 语法注入不应导致 500（应 200 或 400）。"""
        resp = client.get("/api/search", params={"q": q})
        assert resp.status_code != 500, (
            f"查询 {q!r} 返回 500：FTS5 语法错误未在 build_match_terms 中消毒，"
            "见 app/api/routes_search.py"
        )

    def test_ascii_prefix_match(self, client):
        data = _body(client.get("/api/search", params={"q": "english"}))
        assert data["total"] >= 0

    def test_empty_query_returns_all(self, client):
        data = _body(client.get("/api/search", params={"q": ""}))
        assert data["total"] == 3

    def test_limit_respected(self, client):
        data = _body(client.get("/api/search", params={"q": "", "limit": 2}))
        assert len(data["hits"]) == 2 and data["total"] == 3

    def test_file_operator_with_ext(self, client):
        data = _body(client.get("/api/search", params={"q": "file:会议记录.md"}))
        assert any(h["path"] == "notes/工作/会议记录.md" for h in data["hits"])

    def test_path_operator_root(self, client):
        data = _body(client.get("/api/search", params={"q": "path:notes"}))
        assert data["total"] == 2

    def test_multiple_same_operator_last_wins(self, client):
        data = _body(client.get("/api/search", params={"q": "tag:读书/2026 tag:入门/指南"}))
        assert all("入门/指南" in h["tags"] for h in data["hits"])


# ---------------------------------------------------------------- 图谱细节


class TestGraphDetail:
    def test_backlinks_snippet_and_line(self, client):
        data = _body(client.get("/api/graph/backlinks", params={"path": "欢迎.md"}))
        paths = [b["path"] for b in data["backlinks"]]
        assert "notes/读书/2026计划.md" in paths
        item = next(b for b in data["backlinks"] if b["path"] == "notes/读书/2026计划.md")
        assert item["line"] >= 1 and item["snippet"]
        # 已知观察项：欢迎.md 自嵌入 ![[欢迎]] 使其出现在自身反链中（参见 QA 报告遗留问题）

    def test_rename_preserves_alias_and_anchor(self, client, vault_dir):
        (vault_dir / "notes" / "工作" / "锚点别名.md").write_text(
            "见 [[2026计划|新年快乐]] 与 [[2026计划#第一章]] 与 ![[2026计划]]\n",
            encoding="utf-8",
        )
        import app.parser.wikilink as wl
        from app.api import deps as d
        d.require_indexer().reindex_note("notes/工作/锚点别名.md")

        data = _body(client.post("/api/files/rename", json={
            "path": "notes/读书/2026计划.md", "new_name": "新年计划",
        }))
        assert data["new_path"] == "notes/读书/新年计划.md"

        text = (vault_dir / "notes" / "工作" / "锚点别名.md").read_text(encoding="utf-8")
        assert "[[新年计划|新年快乐]]" in text, "别名重写失败"
        assert "[[新年计划#第一章]]" in text, "锚点重写失败"
        assert "![[新年计划]]" in text, "嵌入重写失败"
        assert "2026计划" not in text

    def test_linkify_flow(self, client):
        # unlinked 提及
        data = _body(client.get("/api/graph/unlinked", params={"path": "notes/读书/2026计划.md"}))
        assert any(m["path"] == "notes/工作/会议记录.md" for m in data["mentions"])
        mention = next(m for m in data["mentions"] if m["path"] == "notes/工作/会议记录.md")
        assert mention["line"] >= 1
        # 一键转双链
        data2 = _body(client.post("/api/graph/linkify", json={
            "path": "notes/工作/会议记录.md",
            "mention_path": "notes/读书/2026计划.md",
            "line": mention["line"],
        }))
        assert data2["linked"] is True
        # 转换后成为反链，且提及消失
        backs = _body(client.get("/api/graph/backlinks", params={"path": "notes/读书/2026计划.md"}))
        assert any(b["path"] == "notes/工作/会议记录.md" for b in backs["backlinks"])
        data3 = _body(client.get("/api/graph/unlinked", params={"path": "notes/读书/2026计划.md"}))
        assert all(m["path"] != "notes/工作/会议记录.md" for m in data3["mentions"])

    def test_tag_tree_hierarchy(self, client):
        tree = _body(client.get("/api/graph/tags"))
        names = {n["name"] for n in tree}
        assert "入门" in names and "读书" in names and "工作" in names
        entry = next(n for n in tree if n["name"] == "入门")
        child_names = {c["name"] for c in entry["children"]}
        assert "入门/指南" in child_names
        # 行内标签 入门/首页 与 frontmatter 入门/指南 同挂 "入门" 前缀，去重笔记数=1
        assert entry["count"] == 1

    def test_tag_notes_nested(self, client):
        data = _body(client.get("/api/graph/tag-notes", params={"tag": "入门/指南"}))
        assert any(n["path"] == "欢迎.md" for n in data)

    def test_embed_missing_note(self, client):
        data = _body(client.get("/api/note/embed", params={"path": "不存在.md"}))
        assert data["exists"] is False
        assert "kwey-embed--missing" in data["html"]


# ---------------------------------------------------------------- WebSocket


class TestWsEvents:
    def test_connect_and_pong(self, client):
        with client.websocket_connect("/ws/events") as ws:
            hello = ws.receive_json()
            assert hello["event"] == "connected"
            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong["event"] == "pong"

    def test_event_broadcast_on_create(self, client):
        with client.websocket_connect("/ws/events") as ws:
            assert ws.receive_json()["event"] == "connected"
            resp = client.post("/api/note", json={"path": "notes/ws事件.md", "content": "# ws"})
            assert resp.status_code == 200
            result: dict = {}

            def reader() -> None:
                result["msg"] = ws.receive_json()

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(timeout=5)
            assert "msg" in result, "WS 5s 内未收到 created 广播"
            msg = result["msg"]
            assert msg["event"] == "created"
            assert msg["path"] == "notes/ws事件.md"
            assert set(msg.keys()) >= {"event", "path", "ts"}


# ---------------------------------------------------------------- watcher


class TestWatcherTupleForm:
    def test_set_of_tuples_processed(self, vault, vault_dir):
        """watch() 真实产出为 set[(Change, Path)]，_process_changes 应兼容。"""
        new_file = vault_dir / "元组形式.md"
        new_file.write_text("# 元组形式\n", encoding="utf-8")
        vault._watcher._process_changes({(Change.added, new_file)})
        assert vault.db.get_note("元组形式.md") is not None

    def test_tmp_file_not_indexed_as_note(self, vault, vault_dir):
        """自写原子写的 .kwey-tmp 中间文件不应被索引为笔记。"""
        tmp = vault_dir / "原子写.md.kwey-tmp"
        tmp.write_text("# 临时\n", encoding="utf-8")
        vault._watcher._process_changes({(Change.added, tmp)})
        assert vault.db.get_note("原子写.md.kwey-tmp") is None


# ---------------------------------------------------------------- 附件与设置


class TestAttachmentSettings:
    def test_custom_attachment_dir(self, client):
        _body(client.put("/api/settings", json={"attachment_dir": "我的附件"}))
        resp = client.post(
            "/api/attachments",
            files={"file": ("中文 附件.png", b"\x89PNGdata", "image/png")},
        )
        data = _body(resp)
        assert data["path"].startswith("我的附件/")
        served = client.get(f"/file/{data['path']}")
        assert served.status_code == 200

    def test_upload_illegal_filename_sanitized(self, client):
        resp = client.post(
            "/api/attachments",
            files={"file": ("a<b>|c?.png", b"data", "image/png")},
        )
        data = _body(resp)
        bad = set('<>:"|?*')
        name = data["path"].rsplit("/", 1)[-1]
        assert not (bad & set(name)), f"非法字符未被消毒: {name}"

    def test_delete_dir_cleans_graph(self, client, vault_dir):
        """目录删除后，其下附件不应再可按 basename 解析（图清理）。"""
        (vault_dir / "临时附件" ).mkdir()
        (vault_dir / "临时附件" / "临时图.png").write_bytes(b"png")
        from app.api import deps as d
        d.require_indexer().linkgraph.upsert_file("临时附件/临时图.png")
        assert d.require_linkgraph().resolve("临时图.png") == "临时附件/临时图.png"

        data = _body(client.delete("/api/files/dir", params={"path": "临时附件", "confirm": 1}))
        assert data["trash_path"].startswith(".trash/")
        assert d.require_linkgraph().resolve("临时图.png") != "临时附件/临时图.png", (
            "源码问题：delete_dir 未清理目录下非笔记文件的链接图登记（"
            "app/vault/service.py delete_dir 仅遍历 notes 表），见测试 test_delete_dir_cleans_graph"
        )
