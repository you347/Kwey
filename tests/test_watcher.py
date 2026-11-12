"""T04 watcher 单测：外部改动增量索引、自写去重（不启动真实监听线程）。"""

from __future__ import annotations

from pathlib import Path

from watchfiles import Change

from app.events import EventType


class TestExternalChanges:
    def test_external_modify_reindex(self, vault, vault_dir):
        watcher = vault._watcher
        target = vault_dir / "notes" / "工作" / "会议记录.md"
        target.write_text("# 会议\n\n外部修改后新增 [[欢迎]]。\n", encoding="utf-8")
        watcher._process_changes({target: Change.modified})
        rows = vault.db.links_for("notes/工作/会议记录.md")
        assert any(r["target_path"] == "欢迎.md" for r in rows)

    def test_external_create(self, vault, vault_dir):
        watcher = vault._watcher
        new_file = vault_dir / "外部新建.md"
        new_file.write_text("# 新\n", encoding="utf-8")
        watcher._process_changes({new_file: Change.added})
        assert vault.db.get_note("外部新建.md") is not None

    def test_external_delete(self, vault, vault_dir):
        watcher = vault._watcher
        target = vault_dir / "notes" / "工作" / "会议记录.md"
        target.unlink()
        watcher._process_changes({target: Change.deleted})
        assert vault.db.get_note("notes/工作/会议记录.md") is None

    def test_events_broadcast(self, vault, vault_dir):
        seen: list = []
        vault.eventbus.subscribe(None, seen.append)
        watcher = vault._watcher
        new_file = vault_dir / "事件.md"
        new_file.write_text("x", encoding="utf-8")
        watcher._process_changes({new_file: Change.added})
        events = [e.event for e in seen]
        assert EventType.CREATED in events


class TestSelfWriteLoop:
    def test_self_write_skipped(self, vault, vault_dir):
        """风险点①：VaultService 写盘后 watcher 命中自写记录 → 不重复索引、不发事件。"""
        seen: list = []
        vault.eventbus.subscribe(None, seen.append)
        watcher = vault._watcher
        vault._mark_self_write("自写.md")
        target = vault_dir / "自写.md"
        target.write_text("外部看似修改", encoding="utf-8")
        watcher._process_changes({target: Change.modified})
        assert [e.event for e in seen] == []

    def test_ignored_dirs_skipped(self, vault, vault_dir):
        seen: list = []
        vault.eventbus.subscribe(None, seen.append)
        watcher = vault._watcher
        target = vault_dir / ".kwey" / "settings.json"
        target.write_text("{}", encoding="utf-8")
        watcher._process_changes({target: Change.modified})
        assert [e.event for e in seen] == []

    def test_attachment_registered(self, vault, vault_dir):
        watcher = vault._watcher
        img = vault_dir / "attachments" / "外部图.png"
        img.write_bytes(b"\x89PNG", )
        watcher._process_changes({img: Change.added})
        assert vault.indexer.linkgraph.resolve("外部图.png") == "attachments/外部图.png"
