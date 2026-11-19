"""批次 0 回归：实时预览渲染接口 + watcher 目录事件（QA 遗留 B6）。"""

from __future__ import annotations

from watchfiles import Change

from app.events import EventType


class TestRealtimePreview:
    def test_post_renders_unsaved_content_without_touching_disk(self, client, vault_dir):
        path = "欢迎.md"
        original = (vault_dir / path).read_text(encoding="utf-8")
        resp = client.post(
            "/api/note/render",
            json={"path": path, "content": "# 未落盘标题\n\n见 [[欢迎]] 与 #临时/标签"},
        )
        assert resp.status_code == 200
        html = resp.json()["data"]["html"]
        assert "未落盘标题" in html
        assert 'class="kwey-wikilink' in html
        assert 'class="kwey-tag" data-tag="临时/标签"' in html
        assert (vault_dir / path).read_text(encoding="utf-8") == original

    def test_post_without_content_falls_back_to_disk(self, client):
        resp = client.post("/api/note/render", json={"path": "欢迎.md"})
        assert resp.status_code == 200
        assert "这是" in resp.json()["data"]["html"]

    def test_get_render_still_works(self, client):
        resp = client.get("/api/note/render", params={"path": "欢迎.md"})
        assert resp.status_code == 200
        assert "欢迎" in resp.json()["data"]["html"]


class TestWatcherDirectoryEvents:
    def test_directory_added_not_registered_as_file(self, vault, vault_dir):
        """B6：目录级事件不得进入链接图 files 表，也不得广播 created。"""
        seen: list = []
        vault.eventbus.subscribe(None, seen.append)
        new_dir = vault_dir / "新建目录"
        new_dir.mkdir()
        vault._watcher._process_changes({new_dir: Change.added})
        assert "新建目录" not in vault.indexer.linkgraph.files
        assert vault.indexer.linkgraph.resolve("新建目录") is None
        assert all(e.event != EventType.CREATED for e in seen)

    def test_attachment_still_registered(self, vault, vault_dir):
        """回归：真实文件仍按原逻辑登记，B6 过滤不误伤。"""
        img = vault_dir / "attachments" / "新图.png"
        img.write_bytes(b"\x89PNG")
        vault._watcher._process_changes({img: Change.added})
        assert vault.indexer.linkgraph.resolve("新图.png") == "attachments/新图.png"
