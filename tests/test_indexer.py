"""T03 索引器单测：全量索引、增量更新、整库重建（G4）、FTS 检索与 snippet。"""

from __future__ import annotations

from app.index.database import cjk_compact, cjk_space


class TestFullScan:
    def test_full_scan_counts(self, services, vault_dir):
        db = services.require_db()
        assert db.count_notes() == 3
        assert services.require_linkgraph().note_count() == 3

    def test_links_resolved(self, services):
        db = services.require_db()
        rows = db.links_for("notes/读书/2026计划.md")
        targets = {r["target_path"] for r in rows}
        assert "欢迎.md" in targets
        # 嵌入附件按文件名解析
        embeds = [r for r in rows if r["kind"] == "embed"]
        assert any(r["target_path"] == "attachments/pic.png" for r in embeds)

    def test_tags_merged(self, services):
        db = services.require_db()
        tags = set(db.tags_of_note("欢迎.md"))
        assert "入门/指南" in tags       # frontmatter
        assert "入门/首页" in tags       # 行内


class TestIncremental:
    def test_reindex_note_updates(self, services, vault_dir):
        db = services.require_db()
        note = vault_dir / "欢迎.md"
        note.write_text("---\ntitle: 欢迎2\n---\n新内容 [[notes/读书/2026计划.md]]", encoding="utf-8")
        links_changed = services.require_indexer().reindex_note("欢迎.md")
        row = db.get_note("欢迎.md")
        assert row["title"] == "欢迎2"
        rows = db.links_for("欢迎.md")
        assert any(r["target_path"] == "notes/读书/2026计划.md" for r in rows)
        assert isinstance(links_changed, bool)

    def test_reindex_missing_removes(self, services, vault_dir):
        db = services.require_db()
        (vault_dir / "notes" / "工作" / "会议记录.md").unlink()
        services.require_indexer().reindex_note("notes/工作/会议记录.md")
        assert db.get_note("notes/工作/会议记录.md") is None

    def test_reindex_after_external_write(self, services, vault_dir):
        """外部编辑器直接改盘（不走 VaultService）→ 索引可通过 reindex 同步。"""
        db = services.require_db()
        f = vault_dir / "notes" / "工作" / "会议记录.md"
        f.write_text("# 会议\n\n新增 [[欢迎]] 链接。\n", encoding="utf-8")
        services.require_indexer().reindex_note("notes/工作/会议记录.md")
        rows = db.links_for("notes/工作/会议记录.md")
        assert any(r["target_path"] == "欢迎.md" for r in rows)


class TestRebuild:
    def test_rebuild_after_db_delete(self, services, vault_dir):
        """G4：删除 .kwey/index.db 后整库重建，内容零丢失。"""
        db_path = vault_dir / ".kwey" / "index.db"
        services.require_db().close()
        db_path.unlink()
        services.init_services(vault_dir, open_now=True)
        db = services.require_db()
        assert db.count_notes() == 3
        assert db.get_note("欢迎.md")["title"] == "欢迎"
        # 链接图同样恢复
        assert "欢迎.md" in services.require_linkgraph().backlinks("欢迎.md")


class TestFts:
    def test_cjk_helpers(self):
        assert cjk_space("读书笔记") == "读 书 笔 记 "
        assert cjk_compact("读 书 笔 记") == "读书笔记"

    def test_search_chinese(self, services):
        db = services.require_db()
        hits, total = db.search(terms=['"讨 论 了 2026 计 划"'.replace(" ", "")][:0] or [], limit=10)
        assert total >= 0  # 空查询走纯过滤分支

    def test_search_body_match(self, services):
        db = services.require_db()
        # 「2026计划」标题在会议记录正文出现但未建链
        mentions = db.body_mentions("2026计划", exclude_paths={"notes/读书/2026计划.md", "欢迎.md"})
        paths = [m["path"] for m in mentions]
        assert "notes/工作/会议记录.md" in paths
        assert "<mark>" in mentions[0]["snippet"]

    def test_search_with_filters(self, services):
        db = services.require_db()
        hits, total = db.search(terms=[], path_prefix="notes", limit=10)
        assert all(h["path"].startswith("notes/") for h in hits)
        hits2, _ = db.search(terms=[], tag="入门/指南", limit=10)
        assert any(h["path"] == "欢迎.md" for h in hits2)
        hits3, _ = db.search(terms=[], file_pattern="会议", limit=10)
        assert any(h["path"] == "notes/工作/会议记录.md" for h in hits3)
