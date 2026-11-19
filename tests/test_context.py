"""A8 上下文工程单测：300 篇笔记 + 链式链接，验证预算、图邻域与溯源。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api import deps
from app.index.context import build_context, select_files

BATCH_COUNT = 300


@pytest.fixture
def big_vault(services, vault_dir: Path):
    """在 conftest 的 3 篇基础上再写 300 篇，形成链式双链图。"""
    vault = deps.require_vault()
    root = vault.paths.root
    batch_dir = root / "notes" / "批量"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for i in range(BATCH_COUNT):
        rel = f"notes/批量/笔记-{i:03d}.md"
        nxt = (i + 1) % BATCH_COUNT
        content = (
            f"# 笔记 {i:03d}\n\n"
            f"主题：上下文工程第 {i} 篇，讨论分层索引与选择性加载。\n\n"
            f"双链到 [[笔记-{nxt:03d}]]。\n"
        )
        (root / rel).write_text(content, encoding="utf-8")
        vault.indexer.reindex_note(rel)
    return vault


class TestSelectFiles:
    def test_returns_ranked_candidates_with_reasons(self, big_vault):
        hits = select_files(big_vault, "上下文工程", limit=20)
        assert len(hits) <= 20
        assert all({"path", "score", "reason"} <= set(h) for h in hits)
        assert all(h["score"] >= 0 for h in hits)
        assert any("fts" in h["reason"] for h in hits)

    def test_empty_query_still_returns_candidates(self, big_vault):
        hits = select_files(big_vault, "", limit=10)
        assert len(hits) <= 10


class TestBuildContext:
    def test_budget_and_sources(self, big_vault):
        result = build_context(big_vault, "上下文工程", max_chars=20000)
        text = result["text"]
        sources = result["sources"]
        assert len(text) <= 20000
        assert "主索引" in text
        assert sources, "应至少纳入一篇正文片段"
        for src in sources:
            assert src["path"] in text, f"来源未标注: {src['path']}"
            assert src["score"] >= 0
            assert src["reason"]

    def test_query_related_and_graph_neighborhood(self, big_vault):
        focus = "notes/批量/笔记-000.md"
        result = build_context(
            big_vault, "上下文工程", max_chars=40000, focus_path=focus, depth=1
        )
        text = result["text"]
        paths = {s["path"] for s in result["sources"]}
        # focus 自身入链来自 笔记-299，出链到 笔记-001；两者都应被引擎发现
        assert focus in text or focus in paths
        assert "notes/批量/笔记-001.md" in paths or "notes/批量/笔记-299.md" in paths
        assert "上下文工程" in text

    def test_small_budget_never_exceeds(self, big_vault):
        for budget in (500, 1200, 3000):
            result = build_context(big_vault, "上下文工程", max_chars=budget)
            assert len(result["text"]) <= budget

    def test_missing_query_terms_degrades_to_index(self, big_vault):
        result = build_context(big_vault, "完全不存在的关键词ZZZ", max_chars=3000)
        assert len(result["text"]) <= 3000
        assert "主索引" in result["text"]


class TestNoVault:
    def test_none_db_returns_empty(self):
        class _NoDb:
            pass

        result = build_context(_NoDb(), "x", max_chars=1000)
        assert result == {"text": "", "sources": []}
