"""向量索引：VectorIndex 抽象 + 多后端 + 可插拔 Embedder。

后端优先级（``vector.backend``，默认 auto）：
1. **sqlite-vec**：单文件扩展，和 FTS 同为 SQLite，发布版默认，零外部依赖；
2. Chroma：老配置兼容（装了 chromadb 才用）；
3. Null：不可用时空操作，hybrid 等价纯 FTS，绝不抛错。

Embedder 优先级（``vector.embedder``，默认 auto）：
1. **本地 ONNX 模型**（bge-small-zh-v1.5，24MB，首次启用时下载）——离线、快、中文效果好；
2. **API embedding**（复用已配置的 OpenAI 兼容 Provider，``vector.api_model`` 指定模型名）；
3. **Hashing**（确定性哈希词向量，零依赖兜底；质量弱，仅保证不崩）。

存储：``<vault>/.kwey/vector.db``（sqlite-vec）/ ``.kwey/vector/``（Chroma）
，Indexer.full_scan 时 reset 重建——向量永远是可重建索引，不是事实源。
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


class ApiEmbedder:
    """OpenAI 兼容 /embeddings（复用 provider.json 的 endpoint/api_key）。

    适用：用户已有云端 embedding 配额、或想要比 24MB 本地模型更强的模型。
    dim 未知时先探测一次（OpenAI 协议响应里有向量长度）。
    """

    name = "api"

    def __init__(self, endpoint: str, model: str, api_key: str = "", timeout: float = 60.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.url = self.endpoint if self.endpoint.endswith("/embeddings") else self.endpoint + "/embeddings"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.dim = 0

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        import json
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "input": [str(t or "")[:2000] for t in texts],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        items = data.get("data") or []
        items.sort(key=lambda item: item.get("index", 0))
        vectors = [[float(x) for x in item.get("embedding") or []] for item in items]
        if len(vectors) != len(texts) or any(not v for v in vectors):
            raise RuntimeError("embedding 接口返回的向量数量/维度不合法")
        if not self.dim:
            self.dim = len(vectors[0])
        # 归一化：后端用余弦，统一成单位向量避免 API 是否归一化的差异
        out: List[List[float]] = []
        for vec in vectors:
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm > 0 else vec)
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


def _provider_config(root: Path) -> Dict:
    """读 .kwey/provider.json（与 AgentProvider 同源，但不 import 它避免循环依赖）。"""
    import json

    path = Path(root) / ".kwey" / "provider.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_api_embedder(root: Path, config: Dict) -> Optional[Embedder]:
    """按 vector.api_model + provider.json 构建；缺配置返回 None。"""
    model = str((config or {}).get("api_model") or "").strip()
    if not model:
        return None
    provider = _provider_config(root)
    endpoint = str(provider.get("endpoint") or "").strip()
    if not endpoint:
        return None
    key = str(provider.get("api_key") or "")
    try:
        return ApiEmbedder(endpoint, model, key, timeout=float(provider.get("timeout") or 60))
    except Exception:  # noqa: BLE001
        logger.exception("API embedding 初始化失败")
        return None


def _chroma_installed() -> bool:
    return importlib.util.find_spec("chromadb") is not None


def _sentence_transformers_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def build_embedder(
    model: str = DEFAULT_MODEL,
    root: Optional[Path] = None,
    config: Optional[Dict] = None,
) -> Optional[Embedder]:
    """按 ``vector.embedder`` 构建：local(ONNX) → api → sentence-transformers → hashing。

    ``root`` 用于读 provider.json 走 API embedding；不给则不尝试 API。
    """
    cfg = config or {}
    name = (model or DEFAULT_MODEL).strip()
    kind = str(cfg.get("embedder") or "auto").strip().lower()

    if kind in {"hashing", "hash", "bow"} or name.lower() in {"hashing", "hash", "bow"}:
        return HashingEmbedder()

    if kind in {"auto", "local", "onnx"}:
        try:
            from app.index.onnx_embedder import build_onnx_embedder

            local = build_onnx_embedder(name)
        except Exception:  # noqa: BLE001
            logger.exception("本地 ONNX embedding 初始化异常")
            local = None
        if local is not None:
            return local
        if kind in {"local", "onnx"}:
            logger.warning("本地模型未就绪（model=%s），按配置不降级到其它 embedder", name)

    if kind in {"auto", "api"} and root is not None:
        api = build_api_embedder(Path(root), cfg)
        if api is not None:
            return api
        if kind == "api":
            logger.warning("vector.api_model 或 provider.json 未配置，API embedding 不可用")

    if kind == "auto" and _sentence_transformers_installed():
        try:
            return SentenceTransformerEmbedder(name)
        except Exception:  # noqa: BLE001
            logger.exception("sentence-transformers 初始化失败")

    if kind == "auto":
        logger.warning("没有可用的语义模型（model=%s），embedding 降级为哈希词向量", name)
        return HashingEmbedder()
    return None


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


def _sqlite_vec_available() -> bool:
    try:
        from app.index import sqlite_vec

        return sqlite_vec.available()
    except Exception:  # noqa: BLE001
        return False


def build_vector_index(
    root: Path,
    config: Optional[Dict] = None,
    embedder: Optional[Embedder] = None,
) -> VectorIndex:
    """按 settings.vector 构建；任何环节失败都回 NullBackend（hybrid 自动等价 FTS）。"""
    cfg = config or {}
    if not cfg.get("enabled", False):
        return NullBackend()

    backend = str(cfg.get("backend") or "auto").strip().lower()
    if backend in {"null", "off", "none"}:
        return NullBackend()

    emb = (
        embedder
        if embedder is not None
        else build_embedder(str(cfg.get("model") or DEFAULT_MODEL), Path(root), cfg)
    )
    if emb is None:
        logger.warning("没有可用 embedder，向量检索关闭（hybrid 降级为纯 FTS）")
        return NullBackend()

    want_sqlite = backend in {"auto", "sqlite-vec", "sqlite_vec", "sqlitevec"}
    if want_sqlite and _sqlite_vec_available():
        try:
            from app.index.sqlite_vec import SqliteVecBackend

            return SqliteVecBackend(
                Path(root), emb, model_key=getattr(emb, "model", "") or getattr(emb, "name", "")
            )
        except Exception:  # noqa: BLE001
            logger.exception("sqlite-vec 初始化失败")
            if backend != "auto":
                return NullBackend()

    want_chroma = backend in {"auto", "chroma"}
    if want_chroma and _chroma_installed():
        try:
            return ChromaBackend(Path(root), emb)
        except Exception:  # noqa: BLE001
            logger.exception("Chroma 初始化失败")
            if backend != "auto":
                return NullBackend()

    logger.warning("向量后端不可用（backend=%s），hybrid 降级为纯 FTS", backend)
    return NullBackend()