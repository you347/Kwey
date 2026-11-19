"""向量索引（P1-23）：VectorIndex 抽象 + ChromaBackend + NullBackend + 可插拔 Embedder。

降级链：settings.vector.enabled=False / 缺 chromadb / 初始化失败 → NullBackend，
hybrid 检索自动等价纯 FTS，不抛错。embedding 默认 bge-small-zh-v1.5
（sentence-transformers 可选），模型库缺失时降级 HashingEmbedder（确定性哈希
词向量，不联网、不下载模型）。

存储位于 <vault>/.kwey/vector/，Indexer.full_scan 时 reset 重建（索引可重建语义）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import math
import re
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger("kwey.vector")

DEFAULT_MODEL = "bge-small-zh-v1.5"
VECTOR_DIRNAME = "vector"  # <vault>/.kwey/vector/
COLLECTION_NAME = "kwey_notes"
# bge-small 512 token 上限的保守截断（中文约 1 字/token）
MAX_EMBED_CHARS = 2000

_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


class Embedder(Protocol):
    """文本 → 向量。实现方负责 L2 归一化（余弦相似度 = 点积）。"""

    def embed(self, texts: Sequence[str]) -> List[List[float]]: ...


class HashingEmbedder:
    """确定性哈希词向量（降级/测试用）：ASCII 词 + CJK 单字/双字 → 固定维计数向量。

    用 hashlib 而非内建 hash：跨进程一致，Chroma 持久化向量才能与后续查询同分布。
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _tokens(self, text: str) -> List[str]:
        text = text.lower()
        tokens = _WORD_RE.findall(text)
        cjk = _CJK_RE.findall(text)
        tokens.extend(cjk)
        tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))
        return tokens

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in self._tokens(text or ""):
                digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
                vec[int.from_bytes(digest, "big") % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm > 0 else vec)
        return out


class SentenceTransformerEmbedder:
    """sentence-transformers 模型（默认 BAAI/bge-small-zh-v1.5），首次 embed 才懒加载。"""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name if "/" in model_name else f"BAAI/{model_name}"
        self._model = None
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name)
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in v] for v in vectors]


def _chroma_installed() -> bool:
    return importlib.util.find_spec("chromadb") is not None


def _sentence_transformers_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def build_embedder(model: str = DEFAULT_MODEL) -> Embedder:
    """配置口：model=hashing 显式用哈希词向量；其余走 sentence-transformers，缺库降级。"""
    name = (model or DEFAULT_MODEL).strip()
    if name.lower() in {"hashing", "hash", "bow"}:
        return HashingEmbedder()
    if not _sentence_transformers_installed():
        logger.warning(
            "未安装 sentence-transformers，embedding 降级为哈希词向量（配置 model=%s）", name
        )
        return HashingEmbedder()
    return SentenceTransformerEmbedder(name)


class VectorIndex(ABC):
    """向量索引抽象：path 为主键，text 为标题+正文拼接。"""

    name: str = "abstract"

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def upsert(self, path: str, text: str) -> None: ...

    @abstractmethod
    def remove(self, path: str) -> None: ...

    @abstractmethod
    def reset(self) -> None:
        """清空全部向量（整库重建入口）。"""

    @abstractmethod
    def query(self, text: str, limit: int) -> List[Tuple[str, float]]:
        """返回 [(path, similarity)]，按相似度降序；空文本/空索引返回 []。"""

    @abstractmethod
    def count(self) -> int: ...


class NullBackend(VectorIndex):
    """降级后端：全部空操作，hybrid 等价纯 FTS。"""

    name = "null"

    @property
    def available(self) -> bool:
        return False

    def upsert(self, path: str, text: str) -> None:
        pass

    def remove(self, path: str) -> None:
        pass

    def reset(self) -> None:
        pass

    def query(self, text: str, limit: int) -> List[Tuple[str, float]]:
        return []

    def count(self) -> int:
        return 0


class InMemoryBackend(VectorIndex):
    """进程内余弦检索：测试注入用，也可作无 chromadb 环境的轻量实现。"""

    name = "memory"

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._vectors: Dict[str, Tuple[List[float], float]] = {}
        self._lock = threading.RLock()

    def upsert(self, path: str, text: str) -> None:
        vec = self._embedder.embed([(text or "")[:MAX_EMBED_CHARS]])[0]
        norm = math.sqrt(sum(v * v for v in vec))
        with self._lock:
            self._vectors[path] = (vec, norm)

    def remove(self, path: str) -> None:
        with self._lock:
            self._vectors.pop(path, None)

    def reset(self) -> None:
        with self._lock:
            self._vectors.clear()

    def query(self, text: str, limit: int) -> List[Tuple[str, float]]:
        if limit <= 0 or not (text or "").strip():
            return []
        vec = self._embedder.embed([(text or "")[:MAX_EMBED_CHARS]])[0]
        qnorm = math.sqrt(sum(v * v for v in vec))
        if qnorm == 0:
            return []
        with self._lock:
            scored = [
                (p, sum(x * y for x, y in zip(vec, v)) / (qnorm * n))
                for p, (v, n) in self._vectors.items()
                if n > 0
            ]
        scored = [(p, s) for p, s in scored if s > 0.0]
        scored.sort(key=lambda ps: (-ps[1], ps[0]))
        return scored[:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._vectors)


class ChromaBackend(VectorIndex):
    """Chroma 持久化后端（chromadb 可选导入，工厂负责捕获 ImportError）。

    始终显式传 embeddings / query_embeddings 且 embedding_function=None，
    绝不触发 Chroma 默认 ONNX 模型下载。
    """

    name = "chroma"

    def __init__(
        self, root: Path, embedder: Embedder, collection: str = COLLECTION_NAME
    ) -> None:
        import chromadb
        from chromadb.config import Settings

        self._embedder = embedder
        self._lock = threading.RLock()
        self._collection_name = collection
        persist_dir = Path(root) / ".kwey" / VECTOR_DIRNAME
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._open_collection()

    def _open_collection(self):
        return self._client.get_or_create_collection(
            self._collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )

    def upsert(self, path: str, text: str) -> None:
        text = (text or "")[:MAX_EMBED_CHARS]
        vec = self._embedder.embed([text])[0]
        with self._lock:
            self._collection.upsert(ids=[path], embeddings=[vec], documents=[text])

    def remove(self, path: str) -> None:
        with self._lock:
            self._collection.delete(ids=[path])

    def reset(self) -> None:
        with self._lock:
            self._client.delete_collection(self._collection_name)
            self._collection = self._open_collection()

    def query(self, text: str, limit: int) -> List[Tuple[str, float]]:
        text = (text or "").strip()
        if limit <= 0 or not text:
            return []
        vec = self._embedder.embed([text[:MAX_EMBED_CHARS]])[0]
        with self._lock:
            n = self._collection.count()
            if n == 0:
                return []
            res = self._collection.query(
                query_embeddings=[vec], n_results=min(limit, n), include=["distances"]
            )
        ids = res["ids"][0] if res["ids"] else []
        dists = res["distances"][0] if res["distances"] else []
        # cosine distance → similarity
        return [(p, 1.0 - float(d)) for p, d in zip(ids, dists)]

    def count(self) -> int:
        with self._lock:
            return int(self._collection.count())

    def close(self) -> None:
        with self._lock:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass


def build_vector_index(
    root: Path,
    config: Optional[Dict] = None,
    embedder: Optional[Embedder] = None,
) -> VectorIndex:
    """按 settings.vector 构建后端；未启用/缺 chromadb/初始化失败一律 NullBackend。"""
    cfg = config or {}
    if not cfg.get("enabled", False):
        return NullBackend()
    if not _chroma_installed():
        logger.warning("未安装 chromadb，向量检索不可用（hybrid 降级为纯 FTS）")
        return NullBackend()
    try:
        emb = (
            embedder
            if embedder is not None
            else build_embedder(str(cfg.get("model") or DEFAULT_MODEL))
        )
        return ChromaBackend(root, emb)
    except Exception:  # noqa: BLE001 —— 向量是增强能力，初始化失败不阻断 vault
        logger.exception("向量索引初始化失败，降级为纯 FTS")
        return NullBackend()