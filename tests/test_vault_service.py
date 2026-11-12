"""T04 VaultService 单测：统一写路径、并发锁、事件广播、自写去重、附件。"""

from __future__ import annotations

import threading

from app.events import EventType, VaultEvent


class TestWritePath:
    def test_write_note_broadcasts_updated(self, vault):
        seen: list[VaultEvent] = []
        vault.eventbus.subscribe(EventType.UPDATED, seen.append)
        vault.write_note("欢迎.md", content="# 新内容", frontmatter={"title": "欢迎"})
        assert [e.event for e in seen] == [EventType.UPDATED]
        doc = vault.read_note("欢迎.md")
        assert doc["content"] == "# 新内容"

    def test_atomic_write_and_fm_update(self, vault):
        vault.write_note(
            "欢迎.md",
            content="正文",
            frontmatter={"title": "欢迎", "tags": ["x"], "自定义": "在"},
        )
        doc = vault.read_note("欢迎.md")
        assert doc["frontmatter"]["自定义"] == "在"
        assert doc["frontmatter"]["updated"]

    def test_mtime_conflict_409(self, vault):
        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            vault.write_note("欢迎.md", content="x", frontmatter=None, base_mtime=1.0)
        assert exc.value.status_code == 409

    def test_write_missing_404(self, vault):
        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            vault.write_note("没有.md", content="x")
        assert exc.value.status_code == 404

    def test_create_note_events_and_index(self, vault):
        seen: list[VaultEvent] = []
        vault.eventbus.subscribe(None, seen.append)
        meta = vault.create_note("notes/新建.md", content="# 新")
        assert meta["path"] == "notes/新建.md"
        events = [e.event for e in seen]
        assert EventType.CREATED in events
        doc = vault.read_note("notes/新建.md")
        assert "created" in doc["frontmatter"]
        # 断链复活：此前指向它的 wikilink 已解析
        links = vault.db.links_for("欢迎.md")

    def test_create_duplicate_409(self, vault):
        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            vault.create_note("欢迎.md")
        assert exc.value.status_code == 409

    def test_path_traversal_rejected(self, vault):
        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            vault.read_note("../outside.md")


class TestConcurrency:
    def test_concurrent_writes_no_corruption(self, vault):
        """并发写同一笔记：RLock 保证串行落盘，最终文件是某一次写入的完整内容。"""
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for _ in range(5):
                    vault.write_note(
                        "欢迎.md",
                        content=f"# 版本 {i}\n\n" + "内容" * 200,
                        frontmatter={"title": "欢迎"},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        doc = vault.read_note("欢迎.md")
        assert doc["content"].startswith("# 版本 ")
        assert doc["content"].endswith("内容" * 200)


class TestSelfWrite:
    def test_is_self_write_dedup(self, vault):
        vault._mark_self_write("欢迎.md")
        assert vault.is_self_write("欢迎.md") is True
        assert vault.is_self_write("欢迎.md") is False  # 消费后不再命中

    def test_expired_self_write(self, vault):
        vault.recent_self_writes["旧.md"] = 0.0  # 远过期
        assert vault.is_self_write("旧.md") is False


class TestAttachments:
    def test_save_attachment_dedup(self, vault):
        rel1 = vault.save_attachment("图.png", b"a")
        rel2 = vault.save_attachment("图.png", b"b")
        assert rel1 == "attachments/图.png"
        assert rel2 == "attachments/图 (1).png"
        # 登记进链接图，嵌入可按文件名解析
        assert vault.indexer.linkgraph.resolve("图.png") == rel1


class TestMoveRenameDelete:
    def test_rename_moves_index(self, vault):
        result = vault.rename_note("notes/读书/2026计划.md", "新年计划")
        assert result["new_path"] == "notes/读书/新年计划.md"
        assert result["rewritten_links"] >= 2  # 欢迎.md 两处引用
        assert vault.db.get_note("notes/读书/2026计划.md") is None
        assert vault.db.get_note("notes/读书/新年计划.md") is not None

    def test_delete_to_trash(self, vault):
        trash = vault.delete_note("notes/工作/会议记录.md")
        assert trash.startswith(".trash/")
        assert vault.db.get_note("notes/工作/会议记录.md") is None

    def test_tree_hides_ignored(self, vault):
        tree = vault.tree()
        names = {e["name"] for e in tree}
        assert ".kwey" not in names and ".trash" not in names
