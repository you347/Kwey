"""P1-23 向量混合检索测试：同义召回、缺 chromadb 降级、索引同步/重建、RRF 权重、性能。

不下载任何模型：同义召回注入 FakeEmbedder；chromadb 缺失用 monkeypatch 模拟；
chromadb 实装用例在本机未安装时自动 skip。
"""

from __future__ import annotations

import json
import math
import time

import pytest

from app.api import deps
from app.api.routes_search import _rrf_fuse
from app.index import vector as vec_mod
from app.index.vector import (
    ChromaBackend,
    HashingEmbedder,
    InMemoryBackend,
    NullBackend,
    SentenceTransformerEmbedder,
    build_embedder,
    build_vector_index,
)

requires_chroma = pytest.mark.skipif(
    not vec_mod._chroma_installed(), reason="chromadb 未安装"
)

POMODORO = (
    "---\ntags: [效率]\n---\n# 番茄工作法\n\n"
    "番茄工作法是一种时间管理方法，把任务切成二十五分钟一段。\n见 [[关联笔记]]。\n"
)
NEIGHBOR = "# 关联笔记\n\n完全无关的内容，甲乙丙丁戊己庚辛壬癸。\n"


class FakeEmbedder:
    """同义词组映射到同一维度：验证混合召回管线，不依赖真实模型。"""

    GROUPS = [
        ("番茄", "西红柿"),
        ("时间管理", "光阴管理"),
        ("会议", "碰头会"),
    ]

    def embed(self, texts):
        out = []
        for text in texts:
            vec = [float(sum(text.count(w) for w in group)) for group in self.GROUPS]
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm else vec)
        return out


@pytest.fixture
def vec_client(client):
    """注入 InMemoryBackend + FakeEmbedder，补同义/图扩展样例并整库重建。"""
    root = deps.require_vault().paths.root
    (root / "notes" / "方法").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "方法" / "番茄工作法.md").write_text(POMODORO, encoding="utf-8")
    (root / "notes" / "方法" / "关联笔记.md").write_text(NEIGHBOR, encoding="utf-8")
    indexer = deps.require_indexer()
    indexer.vector = InMemoryBackend(FakeEmbedder())
    indexer.full_scan()
    yield client


def _search(client, q, mode):
    resp = client.get("/api/search", params={"q": q, "mode": mode})
    assert resp.status_code == 200
    return resp.json()["data"]


# --------------------------------------------------------------------- 降级
class TestDegradation:
    def test_default_disabled_is_null(self, client):
        vector = deps.require_indexer().vector
        assert isinstance(vector, NullBackend)
        assert not vector.available
        assert vector.name == "null"

    def test_hybrid_equals_fts_when_null(self, client):
        """缺向量后端：hybrid 与 fts 逐字段等价（hits/total），且 200。"""
        for q in ("会议", "path:notes 计划", "tag:入门/指南", "file:会议", "西红柿工作法", ""):
            fts = _search(client, q, "fts")
            hybrid = _search(client, q, "hybrid")
            assert hybrid["mode"] == "hybrid"
            assert hybrid["hits"] == fts["hits"], q
            assert hybrid["total"] == fts["total"], q
            assert hybrid["vector"] == {"available": False, "backend": "null"}

    def test_missing_chromadb_factory_null(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vec_mod, "_chroma_installed", lambda: False)
        backend = build_vector_index(tmp_path, {"enabled": True})
        assert isinstance(backend, NullBackend)
        assert not backend.available

    def test_missing_chromadb_api_equals_fts(self, client, monkeypatch):
        monkeypatch.setattr(vec_mod, "_chroma_installed", lambda: False)
        root = deps.require_vault().paths.root
        deps.require_indexer().vector = build_vector_index(root, {"enabled": True})
        fts = _search(client, "会议", "fts")
        hybrid = _search(client, "会议", "hybrid")
        assert hybrid["hits"] == fts["hits"]
        assert hybrid["vector"]["available"] is False

    def test_init_failure_degrades_null(self, tmp_path, monkeypatch):
        class Boom:
            def __init__(self, *a, **kw):
                raise RuntimeError("chroma 初始化失败")

        monkeypatch.setattr(vec_mod, "ChromaBackend", Boom)
        backend = build_vector_index(tmp_path, {"enabled": True})
        assert isinstance(backend, NullBackend)


# ----------------------------------------------------------------- 同义召回
class TestSynonymRecall:
    def test_synonym_beats_fts(self, vec_client):
        """「西红柿工作法」FTS 零命中，hybrid 经向量召回「番茄工作法」。"""
        fts = _search(vec_client, "西红柿工作法", "fts")
        assert fts["total"] == 0
        hybrid = _search(vec_client, "西红柿工作法", "hybrid")
        assert hybrid["vector"] == {"available": True, "backend": "memory"}
        hit = next(h for h in hybrid["hits"] if h["path"] == "notes/方法/番茄工作法.md")
        assert "vector" in hit["sources"]
        assert hybrid["total"] >= 1

    def test_synonym_meeting(self, vec_client):
        fts = _search(vec_client, "碰头会", "fts")
        assert fts["total"] == 0
        hybrid = _search(vec_client, "碰头会", "hybrid")
        assert any(h["path"] == "notes/工作/会议记录.md" for h in hybrid["hits"])

    def test_exact_query_keeps_fts_source(self, vec_client):
        hybrid = _search(vec_client, "番茄工作法", "hybrid")
        hit = next(h for h in hybrid["hits"] if h["path"] == "notes/方法/番茄工作法.md")
        assert "fts" in hit["sources"] and "vector" in hit["sources"]
        assert "<mark>" in hit["snippet"]  # FTS 命中保留高亮

    def test_graph_expansion(self, vec_client):
        """链接图扩展：种子笔记的邻居（零文本相关）也进 hybrid，标 graph 来源。"""
        fts = _search(vec_client, "西红柿工作法", "fts")
        assert all(h["path"] != "notes/方法/关联笔记.md" for h in fts["hits"])
        hybrid = _search(vec_client, "西红柿工作法", "hybrid")
        hit = next(h for h in hybrid["hits"] if h["path"] == "notes/方法/关联笔记.md")
        assert hit["sources"] == ["graph"]

    def test_path_operator_filters_vector(self, vec_client):
        hybrid = _search(vec_client, "西红柿 path:notes/方法", "hybrid")
        assert any(h["path"] == "notes/方法/番茄工作法.md" for h in hybrid["hits"])
        assert all(h["path"].startswith("notes/方法/") for h in hybrid["hits"])
        empty = _search(vec_client, "西红柿 path:notes/工作", "hybrid")
        assert empty["total"] == 0

    def test_tag_operator_filters_vector(self, vec_client):
        hybrid = _search(vec_client, "西红柿 tag:效率", "hybrid")
        assert any(h["path"] == "notes/方法/番茄工作法.md" for h in hybrid["hits"])
        empty = _search(vec_client, "西红柿 tag:读书/2026", "hybrid")
        assert empty["total"] == 0

    def test_file_operator_filters_vector(self, vec_client):
        hybrid = _search(vec_client, "西红柿 file:番茄", "hybrid")
        assert any(h["path"] == "notes/方法/番茄工作法.md" for h in hybrid["hits"])
        empty = _search(vec_client, "西红柿 file:关联", "hybrid")
        assert empty["total"] == 0

    def test_operator_only_query_equals_fts(self, vec_client):
        """纯操作符（无检索词）：向量不参与，hybrid 等价 fts。"""
        fts = _search(vec_client, "path:notes/方法", "fts")
        hybrid = _search(vec_client, "path:notes/方法", "hybrid")
        assert hybrid["hits"] == fts["hits"]
        assert hybrid["vector"]["available"] is True

    def test_vector_weight_zero(self, vec_client):
        """可配置权重：weight=0 关闭向量贡献，同义召回消失。"""
        vcfg = deps.require_config().get("vector")
        vcfg["weight"] = 0.0
        try:
            hybrid = _search(vec_client, "西红柿工作法", "hybrid")
            assert hybrid["total"] == 0
        finally:
            vcfg["weight"] = 1.0
        assert _search(vec_client, "西红柿工作法", "hybrid")["total"] >= 1

    def test_graph_weight_zero(self, vec_client):
        vcfg = deps.require_config().get("vector")
        vcfg["graph_weight"] = 0.0
        try:
            hybrid = _search(vec_client, "西红柿工作法", "hybrid")
            assert all(h["path"] != "notes/方法/关联笔记.md" for h in hybrid["hits"])
        finally:
            vcfg["graph_weight"] = 0.5


# ----------------------------------------------------------------- 索引同步
class TestIndexSync:
    def test_full_scan_populates(self, vec_client):
        indexer = deps.require_indexer()
        assert indexer.vector.count() == indexer.db.count_notes() == 5

    def test_reindex_add_and_update(self, vec_client):
        indexer = deps.require_indexer()
        root = deps.require_vault().paths.root
        rel = "notes/方法/临时笔记.md"
        (root / rel).write_text("# 临时笔记\n\n番茄工作法临时内容。\n", encoding="utf-8")
        indexer.reindex_note(rel)
        assert indexer.vector.count() == 6
        assert any(h["path"] == rel for h in _search(vec_client, "西红柿", "hybrid")["hits"])
        # 内容更新后向量同步覆盖（标题不含同义词 → 不再召回）
        (root / rel).write_text("# 临时笔记\n\n无关内容甲乙丙丁。\n", encoding="utf-8")
        indexer.reindex_note(rel)
        assert indexer.vector.count() == 6
        assert all(h["path"] != rel for h in _search(vec_client, "西红柿", "hybrid")["hits"])

    def test_remove_note_syncs(self, vec_client):
        indexer = deps.require_indexer()
        indexer.remove_note("notes/方法/番茄工作法.md")
        assert indexer.vector.count() == 4
        assert _search(vec_client, "西红柿工作法", "hybrid")["total"] == 0

    def test_full_scan_rebuild_clears_stale(self, vec_client):
        """索引可重建：残留幽灵向量在 full_scan 后被清除。"""
        indexer = deps.require_indexer()
        indexer.vector.upsert("幽灵笔记.md", "番茄")
        assert indexer.vector.count() == 6
        indexer.full_scan()
        assert indexer.vector.count() == 5
        assert all(h["path"] != "幽灵笔记.md" for h in _search(vec_client, "西红柿", "hybrid")["hits"])

    def test_vector_failure_does_not_break_indexing(self, client):
        class BrokenBackend(InMemoryBackend):
            def upsert(self, path, text):
                raise RuntimeError("embedding 失败")

            def remove(self, path):
                raise RuntimeError("remove 失败")

            def reset(self):
                raise RuntimeError("reset 失败")

        indexer = deps.require_indexer()
        indexer.vector = BrokenBackend(FakeEmbedder())
        assert indexer.full_scan() == 3  # reset 失败不阻断整库重建
        indexer.reindex_note("欢迎.md")  # upsert 失败不抛错
        assert indexer.db.get_note("欢迎.md") is not None
        indexer.remove_note("欢迎.md")  # remove 失败不抛错
        assert indexer.db.get_note("欢迎.md") is None


# ----------------------------------------------------------------- Embedder
class TestEmbedder:
    def test_hashing_deterministic_and_normalized(self):
        a = HashingEmbedder().embed(["番茄工作法 time management"])[0]
        b = HashingEmbedder().embed(["番茄工作法 time management"])[0]
        assert a == b  # 跨实例一致（hashlib，非内建 hash）
        assert math.sqrt(sum(v * v for v in a)) == pytest.approx(1.0)
        c = HashingEmbedder().embed(["完全不同的文本"])[0]
        assert a != c

    def test_build_embedder_hashing_explicit(self):
        assert isinstance(build_embedder("hashing"), HashingEmbedder)

    def test_build_embedder_fallback_no_download(self, monkeypatch):
        monkeypatch.setattr(vec_mod, "_sentence_transformers_installed", lambda: False)
        emb = build_embedder("bge-small-zh-v1.5")
        assert isinstance(emb, HashingEmbedder)  # 缺模型库 → 词向量降级

    def test_build_embedder_st_lazy(self, monkeypatch):
        monkeypatch.setattr(vec_mod, "_sentence_transformers_installed", lambda: True)
        emb = build_embedder("bge-small-zh-v1.5")
        assert isinstance(emb, SentenceTransformerEmbedder)
        assert emb.model_name == "BAAI/bge-small-zh-v1.5"
        assert emb._model is None  # 构造不加载/不下载模型


# ----------------------------------------------------------------- RRF 融合
class TestRRF:
    def test_rrf_math(self):
        scores = _rrf_fuse(
            {"fts": ({"a": 1, "b": 2}, 1.0), "vector": ({"b": 1}, 1.0)}, 60
        )
        assert scores["a"] == pytest.approx(1 / 61)
        assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
        assert scores["b"] > scores["a"]  # 双来源命中排序靠前

    def test_rrf_zero_weight_skipped(self):
        scores = _rrf_fuse({"fts": ({"a": 1}, 1.0), "vector": ({"z": 1}, 0.0)}, 60)
        assert "z" not in scores


# ----------------------------------------------------------------- Chroma 实装
@requires_chroma
class TestChromaBackend:
    def test_crud_persistence_reset(self, tmp_path):
        backend = ChromaBackend(tmp_path, FakeEmbedder())
        try:
            backend.upsert("a.md", "番茄工作法")
            backend.upsert("b.md", "完全无关")
            assert backend.count() == 2
            hits = backend.query("西红柿", 5)
            assert hits[0][0] == "a.md" and hits[0][1] > 0.5
            backend.remove("a.md")
            assert backend.count() == 1
            assert backend.query("", 5) == []
        finally:
            backend.close()
        # 持久化：重开同目录仍在；reset 清空（可重建）
        reopened = ChromaBackend(tmp_path, FakeEmbedder())
        try:
            assert reopened.count() == 1
            reopened.reset()
            assert reopened.count() == 0
            assert reopened.query("西红柿", 5) == []
        finally:
            reopened.close()

    def test_factory_enabled_hashing_model(self, tmp_path):
        backend = build_vector_index(tmp_path, {"enabled": True, "model": "hashing"})
        try:
            assert isinstance(backend, ChromaBackend)
            assert backend.name == "chroma"
            backend.upsert("a.md", "番茄工作法是一种时间管理方法")
            assert backend.query("番茄", 5)[0][0] == "a.md"
        finally:
            backend.close()

    def test_api_hybrid_with_chroma(self, client):
        root = deps.require_vault().paths.root
        (root / "notes" / "方法").mkdir(parents=True, exist_ok=True)
        (root / "notes" / "方法" / "番茄工作法.md").write_text(POMODORO, encoding="utf-8")
        backend = ChromaBackend(root, FakeEmbedder())
        try:
            indexer = deps.require_indexer()
            indexer.vector = backend
            indexer.full_scan()
            assert backend.count() == indexer.db.count_notes()
            hybrid = _search(client, "西红柿工作法", "hybrid")
            assert hybrid["vector"] == {"available": True, "backend": "chroma"}
            assert any(h["path"] == "notes/方法/番茄工作法.md" for h in hybrid["hits"])
            assert _search(client, "西红柿工作法", "fts")["total"] == 0
        finally:
            backend.close()

    def test_settings_enabled_wiring(self, vault_dir):
        """settings.json 开启 vector（model=hashing）→ Indexer 自动装 ChromaBackend。"""
        from fastapi.testclient import TestClient

        from app.server import create_app

        (vault_dir / ".kwey").mkdir(parents=True, exist_ok=True)
        (vault_dir / ".kwey" / "settings.json").write_text(
            json.dumps({"vector": {"enabled": True, "model": "hashing"}}), encoding="utf-8"
        )
        deps.reset_services()
        try:
            deps.init_services(vault_dir)
            indexer = deps.require_indexer()
            assert isinstance(indexer.vector, ChromaBackend)
            assert indexer.vector.count() == 3
            with TestClient(create_app()) as c:
                data = _search(c, "会议", "hybrid")
                assert data["vector"]["backend"] == "chroma"
                assert any(h["path"] == "notes/工作/会议记录.md" for h in data["hits"])
        finally:
            deps.reset_services()


# ----------------------------------------------------------------- 性能
@pytest.mark.perf
class TestHybridPerformance:
    @pytest.fixture()
    def big_vec_client(self, client):
        """1000 笔记 + InMemoryBackend(HashingEmbedder)，逐篇增量索引同步向量。"""
        root = deps.require_vault().paths.root
        indexer = deps.require_indexer()
        indexer.vector = InMemoryBackend(HashingEmbedder())
        for i in range(1000):
            rel = f"notes/bulk/批量笔记{i:04d}.md"
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"---\ntags: [bulk/b{i % 10}]\n---\n# 批量笔记{i:04d}\n\n"
                + ("这是用于性能基准测试的正文内容，包含一些中文词汇和 english words。关键词：项目评审。" * 30),
                encoding="utf-8",
            )
            indexer.reindex_note(rel)
        return client

    def test_hybrid_under_200ms(self, big_vec_client):
        for q in ("项目评审", "path:notes/bulk 关键词", "西红柿工作法"):
            started = time.perf_counter()
            resp = big_vec_client.get("/api/search", params={"q": q, "mode": "hybrid", "limit": 50})
            elapsed = (time.perf_counter() - started) * 1000
            assert resp.status_code == 200
            assert elapsed <= 200, f"hybrid 查询 {q!r} 耗时 {elapsed:.1f}ms 超标"