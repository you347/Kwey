"""sqlite-vec 后端 + ONNX 本地模型：可插拔向量层的验收。

分两层：
- 不依赖真实模型的部分用 FakeEmbedder（确定性），任何机器都能跑；
- 真实 ONNX 推理用例在模型文件缺失时 skip（CI 不联网也能绿）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.api import deps
from app.index import models as model_store
from app.index import sqlite_vec
from app.index.onnx_embedder import BertTokenizer
from app.index.vector import HashingEmbedder, build_vector_index

VEC_OK = sqlite_vec.available()
MODEL_OK = model_store.is_ready()

needs_vec = pytest.mark.skipif(not VEC_OK, reason="sqlite-vec 未安装")
needs_model = pytest.mark.skipif(not MODEL_OK, reason="本地 ONNX 模型未下载")


class FakeEmbedder:
    """4 维语义空间：番茄/西红柿 → 第 0 维，会议/碰头 → 第 1 维。"""

    name = "fake"
    dim = 4

    GROUPS = [("番茄", "西红柿"), ("会议", "碰头"), ("向量", "embedding")]

    def embed(self, texts):
        out = []
        for text in texts:
            vec = [float(sum(text.count(w) for w in group)) for group in self.GROUPS] + [0.0]
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm else vec)
        return out

    def embed_query(self, text):
        return self.embed([text])[0]


class FakeEmbedder2(FakeEmbedder):
    name = "fake2"
    dim = 8

    def embed(self, texts):
        base = super().embed(texts)
        return [v + [0.0] * 4 for v in base]


@needs_vec
class TestSqliteVecBackend:
    def test_upsert_query_remove_reset(self, tmp_path: Path):
        from app.index.sqlite_vec import SqliteVecBackend

        backend = SqliteVecBackend(tmp_path, FakeEmbedder(), model_key="fake")
        assert backend.available and backend.count() == 0
        backend.upsert("a.md", "番茄工作法")
        backend.upsert("b.md", "会议记录")
        backend.upsert("c.md", "完全无关")            # 零向量：不入索引
        assert backend.count() == 2

        hits = backend.query("西红柿", 3)
        assert hits[0][0] == "a.md"
        assert hits[0][1] > hits[-1][1]
        assert all(-1.0 <= score <= 1.0 for _p, score in hits)

        backend.upsert("a.md", "会议记录")          # 覆盖同一 path
        assert backend.count() == 2
        assert {p for p, _s in backend.query("碰头", 2)} == {"a.md", "b.md"}

        backend.remove("b.md")
        assert backend.count() == 1
        backend.reset()
        assert backend.count() == 0

    def test_upsert_many(self, tmp_path: Path):
        from app.index.sqlite_vec import SqliteVecBackend

        backend = SqliteVecBackend(tmp_path, FakeEmbedder(), model_key="fake")
        backend.upsert_many([("a.md", "番茄"), ("b.md", "会议"), ("c.md", "向量")])
        assert backend.count() == 3

    def test_model_change_drops_vectors(self, tmp_path: Path):
        from app.index.sqlite_vec import SqliteVecBackend

        first = SqliteVecBackend(tmp_path, FakeEmbedder(), model_key="fake")
        first.upsert("a.md", "番茄")
        assert first.count() == 1
        first.close()

        second = SqliteVecBackend(tmp_path, FakeEmbedder2(), model_key="fake2")
        assert second.count() == 0          # 换模型 → 旧向量必须丢弃（维度也不同）
        second.close()

    def test_query_empty_text(self, tmp_path: Path):
        from app.index.sqlite_vec import SqliteVecBackend

        backend = SqliteVecBackend(tmp_path, FakeEmbedder(), model_key="fake")
        backend.upsert("a.md", "番茄")
        assert backend.query("", 5) == []
        assert backend.query("番茄", 0) == []


class TestFactory:
    def test_auto_picks_sqlite_vec(self, tmp_path: Path):
        if not VEC_OK:
            pytest.skip("sqlite-vec 未安装")
        backend = build_vector_index(tmp_path, {"enabled": True, "embedder": "hashing"})
        assert backend.name == "sqlite-vec"
        assert backend.available

    def test_backend_null_explicit(self, tmp_path: Path):
        backend = build_vector_index(tmp_path, {"enabled": True, "backend": "null", "embedder": "hashing"})
        assert backend.name == "null"

    def test_disabled_is_null(self, tmp_path: Path):
        assert build_vector_index(tmp_path, {"enabled": False}).name == "null"


class TestTokenizer:
    @needs_model
    def test_chinese_char_split_and_specials(self):
        tokenizer = BertTokenizer(model_store.model_dir() / "vocab.txt")
        ids, mask, types = tokenizer.encode("我爱笔记", max_len=16)
        assert ids[0] == tokenizer.cls_id
        assert ids[1] == tokenizer.vocab["我"]
        assert tokenizer.sep_id in ids
        assert len(ids) == 16
        assert mask[:5] == [1, 1, 1, 1, 1]
        assert mask[-1] == 0                     # padding
        assert len(ids) == len(mask) == len(types)

    @needs_model
    def test_truncation(self):
        tokenizer = BertTokenizer(model_store.model_dir() / "vocab.txt")
        ids, mask, _types = tokenizer.encode("笔记" * 400, max_len=32)
        assert len(ids) == 32 and mask[-1] == 1
        assert ids[0] == tokenizer.cls_id and ids[-1] == tokenizer.sep_id

    @needs_model
    def test_unknown_and_punctuation(self):
        tokenizer = BertTokenizer(model_store.model_dir() / "vocab.txt")
        ids, _mask, _types = tokenizer.encode("hello, world!", max_len=24)
        assert len(ids) == 24
        assert tokenizer.unk_id not in ids[:6]   # 常见英文词在词表里


@needs_model
class TestOnnxEmbedder:
    def test_semantic_similarity(self):
        from app.index.onnx_embedder import build_onnx_embedder

        embedder = build_onnx_embedder()
        assert embedder is not None and embedder.dim == 512
        vectors = embedder.embed(["如何实现向量检索", "向量搜索用 sqlite-vec 存", "今天中午吃什么"])
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))
        assert dot(vectors[0], vectors[1]) > dot(vectors[0], vectors[2]) + 0.1
        assert abs(math.sqrt(sum(v * v for v in vectors[0])) - 1.0) < 1e-3

    def test_query_prefix_changes_vector(self):
        from app.index.onnx_embedder import build_onnx_embedder

        embedder = build_onnx_embedder()
        doc = embedder.embed(["检索语义"])[0]
        query = embedder.embed_query("检索语义")
        assert len(query) == 512
        assert query != doc                     # 查询前缀确实生效

    def test_batch(self):
        from app.index.onnx_embedder import build_onnx_embedder

        embedder = build_onnx_embedder()
        vectors = embedder.embed([f"笔记 {i}" for i in range(20)])
        assert len(vectors) == 20 and all(len(v) == 512 for v in vectors)


class TestModelStore:
    def test_status_shape(self):
        status = model_store.status()
        assert status["model"] == model_store.DEFAULT_MODEL
        assert status["dim"] == 512
        assert "ready" in status and "progress" in status


# --------------------------------------------------------------------- API
class TestVectorApi:
    def test_status(self, client):
        data = client.get("/api/vector/status").json()["data"]
        assert "enabled" in data and "model" in data and "task" in data
        assert data["model"]["dim"] == 512

    def test_reindex_requires_enabled(self, client):
        res = client.post("/api/vector/reindex", json={"wait": True})
        assert res.status_code == 400
        assert "启用" in res.json()["message"]

    def test_download_rejects_hashing(self, client, services):
        cfg = deps.require_config()
        cfg.set({"vector": {**(cfg.get("vector") or {}), "model": "hashing"}})
        res = client.post("/api/vector/model/download", json={})
        assert res.status_code == 400
        assert "哈希" in res.json()["message"]

    def test_reindex_and_search(self, client, services, monkeypatch):
        """用哈希 embedder 走完整链路：启用 → 重建（同步）→ 语义试查。"""
        cfg = deps.require_config()
        cfg.set({"vector": {
            "enabled": True, "backend": "auto", "embedder": "hashing",
            "model": "hashing", "weight": 1.0, "graph_weight": 0.5, "rrf_k": 60,
        }})
        monkeypatch.setattr("app.index.vector.build_embedder", lambda *a, **kw: HashingEmbedder())

        res = client.post("/api/vector/reindex", json={"wait": True})
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        if data["backend"] == "null":
            pytest.skip("当前环境无可用向量后端")
        assert data["count"] >= 1

        hits = client.get("/api/vector/search", params={"q": "会议", "limit": 3}).json()["data"]
        assert hits["items"]
        assert hits["items"][0]["title"]

    def test_search_without_backend(self, client):
        res = client.get("/api/vector/search", params={"q": "会议"})
        assert res.status_code == 400
