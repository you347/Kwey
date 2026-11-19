"""A2 解锁验证：settings.vector 可写、registry kwey.search 走 hybrid、向量状态/重建端点。"""

from __future__ import annotations

import importlib

import pytest

from app.agent import registry
from app.api import deps
from app.index import vector as vec_mod
from app.index.vector import HashingEmbedder, InMemoryBackend


def _body(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestSettingsUnlock:
    def test_vector_writable_and_merged(self, client):
        data = _body(client.put("/api/settings", json={"vector": {"enabled": True, "weight": 2.5}}))
        assert data["vector"]["enabled"] is True
        assert data["vector"]["weight"] == 2.5
        assert data["vector"]["model"] == "bge-small-zh-v1.5"  # 未提交子键保留

    def test_vector_persisted(self, client):
        client.put("/api/settings", json={"vector": {"enabled": True, "model": "hashing"}})
        data = _body(client.get("/api/settings"))
        assert data["vector"]["model"] == "hashing"


class TestRegistryHybrid:
    def test_schema_exposes_hybrid(self):
        tool = registry.get_tool("kwey.search")
        schema = tool["input_schema"]
        assert set(schema["properties"]["mode"]["enum"]) >= {"fts", "hybrid"}
        assert "hybrid" in tool["description"] or "融合" in tool["description"]

    def test_hybrid_call_returns_vector_info(self, client):
        """注入向量后端后，kwey.search(mode=hybrid) 的返回带 vector 状态。"""
        indexer = deps.require_indexer()
        old = indexer.vector
        indexer.vector = InMemoryBackend(HashingEmbedder())
        try:
            # 灌一篇含检索词的笔记
            from pathlib import Path
            root = deps.require_vault().paths.root
            (root / "hybrid-registry.md").write_text("# 番茄工作法\n\n专注 25 分钟。\n", encoding="utf-8")
            indexer.reindex_note("hybrid-registry.md", broadcast=False)

            result = registry.invoke("kwey.search", {"query": "番茄", "mode": "hybrid", "limit": 5}, vault=deps.require_vault())
            assert result["mode"] == "hybrid"
            assert "vector" in result and "backend" in result["vector"]
            assert result["vector"]["backend"] == "memory"
        finally:
            indexer.vector = old

    def test_fts_call_still_works(self, client):
        result = registry.invoke("kwey.search", {"query": "", "mode": "fts", "limit": 5}, vault=deps.require_vault())
        assert result["mode"] == "fts"
        assert isinstance(result["hits"], list)


class TestVectorEndpoints:
    def test_status_default_null(self, client):
        data = _body(client.get("/api/search/vector/status"))
        assert data["backend"] == "null"
        assert data["available"] is False
        assert "enabled" in data and "count" in data

    def test_rebuild_without_chroma_degrades(self, client, monkeypatch):
        monkeypatch.setattr(vec_mod, "_chroma_installed", lambda: False)
        client.put("/api/settings", json={"vector": {"enabled": True}})
        data = _body(client.post("/api/search/vector/rebuild", json={}))
        assert data["enabled"] is True
        assert data["backend"] == "null"
        assert data["available"] is False

    def test_rebuild_with_memory_backend(self, client):
        """注入可用后端后重建：count 为已索引笔记数，available True。"""
        indexer = deps.require_indexer()
        root = indexer.paths.root
        (root / "vector-rebuild-note.md").write_text("# 向量重建测试\n\n正文。\n", encoding="utf-8")
        indexer.reindex_note("vector-rebuild-note.md", broadcast=False)

        import app.api.routes_search as rs
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(rs, "build_vector_index", lambda r, c: InMemoryBackend(HashingEmbedder()))
            data = _body(client.post("/api/search/vector/rebuild", json={}))
            assert data["backend"] == "memory"
            assert data["available"] is True
            assert data["count"] >= 1
        finally:
            monkeypatch.undo()
