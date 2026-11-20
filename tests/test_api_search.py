"""T05/T10 搜索 API 测试：操作符、中文高亮、1000 笔记性能基准（≤200ms）。"""

from __future__ import annotations

import time

import pytest


class TestSearchOperators:
    def test_plain_search(self, client):
        data = client.get("/api/search", params={"q": "会议"}).json()["data"]
        assert data["total"] >= 1
        assert any(h["path"] == "notes/工作/会议记录.md" for h in data["hits"])
        assert "elapsed_ms" in data

    def test_path_operator(self, client):
        data = client.get("/api/search", params={"q": "path:notes/读书 计划"}).json()["data"]
        assert all(h["path"].startswith("notes/读书/") for h in data["hits"])

    def test_tag_operator(self, client):
        data = client.get("/api/search", params={"q": "tag:入门/指南"}).json()["data"]
        assert any(h["path"] == "欢迎.md" for h in data["hits"])
        assert all("入门/指南" in h["tags"] for h in data["hits"])

    def test_file_operator(self, client):
        data = client.get("/api/search", params={"q": "file:会议"}).json()["data"]
        assert any(h["path"] == "notes/工作/会议记录.md" for h in data["hits"])

    def test_combined_operators(self, client):
        data = client.get("/api/search", params={"q": "path:notes tag:读书"}).json()["data"]
        assert all(h["path"].startswith("notes/") for h in data["hits"])

    def test_highlight_mark(self, client):
        data = client.get("/api/search", params={"q": "会议记录"}).json()["data"]
        assert any("<mark>" in h["snippet"] for h in data["hits"])

    def test_bad_mode(self, client):
        resp = client.get("/api/search", params={"q": "x", "mode": "bad"})
        assert resp.status_code == 400

    def test_hybrid_reserved(self, client):
        """mode=hybrid 为 M3 预留参数位：可传入，行为等同 fts。"""
        resp = client.get("/api/search", params={"q": "会议", "mode": "hybrid"})
        assert resp.status_code == 200
        assert resp.json()["data"]["mode"] == "hybrid"


@pytest.mark.perf
class TestPerformance:
    @pytest.fixture()
    def big_client(self, client):
        """在既有 client 上批量补 1000 篇笔记（直写磁盘+增量索引）。"""
        services = client.app.state  # noqa: F841 —— 通过 deps 访问
        from app.api import deps as d

        vault = d.require_vault()
        root = vault.paths.root
        for i in range(1000):
            p = root / "notes" / "bulk" / f"批量笔记{i:04d}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"---\ntags: [bulk/b{i % 10}]\n---\n# 批量笔记{i:04d}\n\n"
                + ("这是用于性能基准测试的正文内容，包含一些中文词汇和 english words。关键词：项目评审。" * 30),
                encoding="utf-8",
            )
            vault.indexer.reindex_note(f"notes/bulk/批量笔记{i:04d}.md")
        return client

    def test_search_under_200ms(self, big_client):
        """1000 笔记稳态查询 ≤200ms（G3）。"""
        for q in ("项目评审", "path:notes/bulk 关键词", "tag:bulk/b3", "file:批量笔记0100"):
            started = time.perf_counter()
            resp = big_client.get("/api/search", params={"q": q, "limit": 50})
            elapsed = (time.perf_counter() - started) * 1000
            assert resp.status_code == 200
            assert elapsed <= 200, f"查询 {q!r} 耗时 {elapsed:.1f}ms 超标"

    def test_quick_switcher_under_300ms(self, big_client):
        """Ctrl+O 切换器查询（file: 操作符）≤300ms（P0-8）。"""
        started = time.perf_counter()
        resp = big_client.get("/api/search", params={"q": "file:批量笔记01", "limit": 20})
        elapsed = (time.perf_counter() - started) * 1000
        assert resp.status_code == 200
        assert elapsed <= 300, f"切换器耗时 {elapsed:.1f}ms 超标"


class TestHyphenatedTokens:
    """连字符/下划线/点在 unicode61 里是分隔符（Verbot 联调发现）。

    旧实现把 ASCII 词的非 \\w 字符删掉再粘合：PET-KWEY-SMOKE → PETKWEYSMOKE*，
    而索引里是 pet/kwey/smoke 三个 token，永远 0 命中——语法合法、静默失效。
    """

    def test_match_terms_split_on_hyphen(self):
        from app.api.routes_search import build_match_terms

        assert build_match_terms(["PET-KWEY-SMOKE"]) == ["PET*", "KWEY*", "SMOKE*"]

    def test_match_terms_split_on_underscore_and_dot(self):
        from app.api.routes_search import build_match_terms

        assert build_match_terms(["foo_bar"]) == ["foo*", "bar*"]
        assert build_match_terms(["v1.2.3"]) == ["v1*", "2*", "3*"]

    def test_pure_punctuation_yields_no_term(self):
        from app.api.routes_search import build_match_terms

        assert build_match_terms(["+++"]) == []
        assert build_match_terms(["AND"]) == ['"AND"']

    def test_hyphenated_identifier_is_searchable(self, client, vault):
        from app.agent import registry

        registry.invoke(
            "kwey.create",
            {"path": "probe-hyphen.md", "content": "标识符 PET-KWEY-SMOKE 用于回归探针。"},
            vault=vault,
        )
        data = client.get(
            "/api/search", params={"q": "PET-KWEY-SMOKE", "mode": "fts"}
        ).json()["data"]
        assert any(h["path"] == "probe-hyphen.md" for h in data["hits"])
