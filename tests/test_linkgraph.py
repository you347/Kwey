"""T03 链接图单测：反链、basename 解析优先级、未链接提及。"""

from __future__ import annotations

from app.index.linkgraph import LinkGraph


class TestResolve:
    def test_priority_exact_then_basename(self, services):
        lg = services.require_linkgraph()
        # 精确相对路径（不带 .md）
        assert lg.resolve("notes/读书/2026计划") == "notes/读书/2026计划.md"
        # 带 .md
        assert lg.resolve("notes/读书/2026计划.md") == "notes/读书/2026计划.md"
        # basename 全局匹配（大小写不敏感）
        assert lg.resolve("2026计划") == "notes/读书/2026计划.md"
        assert lg.resolve("2026JIHUA".lower()) is None  # 大小写仅对 ascii 生效

    def test_basename_attachment(self, services):
        lg = services.require_linkgraph()
        # 笔记优先；附件按文件名匹配
        assert lg.resolve("pic.png") == "attachments/pic.png"

    def test_unresolved(self, services):
        lg = services.require_linkgraph()
        assert lg.resolve("不存在的笔记") is None

    def test_linkgraph_standalone(self):
        lg = LinkGraph()
        lg.upsert_note("notes/a.md")
        lg.upsert_note("b.md")
        lg.apply("notes/a.md", [{"target_path": "b.md"}])
        assert lg.backlinks("b.md") == {"notes/a.md"}
        assert lg.resolve("a") == "notes/a.md"
        lg.remove("notes/a.md")
        assert lg.backlinks("b.md") == set()


class TestBacklinks:
    def test_backlinks(self, services):
        lg = services.require_linkgraph()
        backs = lg.backlinks("欢迎.md")
        assert "notes/读书/2026计划.md" in backs

    def test_backlinks_after_reindex(self, services, vault_dir):
        """断链复活：先建断链，目标创建后反链出现。"""
        lg = services.require_linkgraph()
        indexer = services.require_indexer()
        (vault_dir / "notes" / "工作" / "新笔记.md").write_text(
            "引用 [[幽灵笔记]]\n", encoding="utf-8"
        )
        indexer.reindex_note("notes/工作/新笔记.md")
        assert lg.backlinks("notes/工作/幽灵笔记.md") == set()

        (vault_dir / "notes" / "工作" / "幽灵笔记.md").write_text("# 幽灵\n", encoding="utf-8")
        indexer.reindex_note("notes/工作/幽灵笔记.md")
        revived = indexer.revive_for("notes/工作/幽灵笔记.md")
        assert revived >= 1
        assert "notes/工作/新笔记.md" in lg.backlinks("notes/工作/幽灵笔记.md")


class TestUnlinkedMentions:
    def test_unlinked_mention(self, services):
        db = services.require_db()
        # 会议记录正文含「2026计划」标题但未建链
        mentions = db.body_mentions("2026计划", exclude_paths={"notes/读书/2026计划.md", "欢迎.md"})
        assert "notes/工作/会议记录.md" in [m["path"] for m in mentions]

    def test_linked_note_excluded(self, services):
        """欢迎.md 已被 2026计划.md 链接；按路由同样的排除逻辑应被剔除。"""
        db = services.require_db()
        lg = services.require_linkgraph()
        exclude = lg.backlinks("欢迎.md") | {"欢迎.md"}
        mentions = db.body_mentions("欢迎", exclude)
        assert "notes/读书/2026计划.md" not in [m["path"] for m in mentions]
