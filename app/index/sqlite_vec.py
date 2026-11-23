"""sqlite-vec 向量后端：向量与索引同库同机，零外部服务、零模型依赖。

为什么换掉 Chroma：
- chromadb 依赖链重（pydantic 版本冲突、onnxruntime 自带下载），打包 100MB+；
- sqlite-vec 是单文件扩展（~1MB），和 FTS 一样是 SQLite，备份/迁移/删库都简单；
- 500 文档实测查询 1.46ms 量级，个人知识库规模完全够用。

存储：``<vault>/.kwey/vector.db``（独立于 index.db，避免给主索引连接加载扩展）。
可重建：``Indexer.full_scan`` 会 reset 后重灌，向量永远不是事实源。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("kwey.sqlitevec")

VECTOR_DB = "vector.db"


def _load_extension(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def available() -> bool:
    """sqlite-vec 是否可用（装了包且能加载扩展）。"""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    try:
        conn = sqlite3.connect(":memory:")
        _load_extension(conn)
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


class SqliteVecBackend:
    """VectorIndex 的 sqlite-vec 实现（鸭子类型，避免循环 import）。"""

    name = "sqlite-vec"

    def __init__(self, root: Path, embedder, model_key: str = "") -> None:
        self._embedder = embedder
        self._model_key = model_key or getattr(embedder, "model", "") or getattr(embedder, "name", "unknown")
        self.dim = int(getattr(embedder, "dim", 0) or 0)
        if self.dim <= 0:
            # API embedding 的维度要探测一次才知道；本地模型自带 dim
            probe = embedder.embed(["维度探测"])[0]
            self.dim = len(probe)
            try:
                setattr(embedder, "dim", self.dim)
            except Exception:  # noqa: BLE001
                pass
        if self.dim <= 0:
            raise ValueError("embedder 缺少 dim，无法建向量表")
        self._lock = threading.RLock()
        path = Path(root) / ".kwey" / VECTOR_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        _load_extension(self._conn)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_table()

    # ---------------------------------------------------------------- schema
    def _ensure_table(self) -> None:
        with self._lock:
            c = self._conn
            c.execute("CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)")
            row = c.execute("SELECT value FROM vec_meta WHERE key='model'").fetchone()
            current = row[0] if row else None
            if current is not None and current != self._model_key:
                # 换模型 = 向量空间变了，旧向量必须整体丢弃
                c.execute("DROP TABLE IF EXISTS vec_items")
                logger.info("向量模型变更（%s → %s），重建向量表", current, self._model_key)
            c.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
                f"path TEXT PRIMARY KEY, embedding float[{self.dim}] distance_metric=cosine)"
            )
            c.execute(
                "INSERT INTO vec_meta(key, value) VALUES ('model', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._model_key,),
            )
            c.commit()

    # ---------------------------------------------------------------- write
    def _embed_docs(self, texts: Sequence[str]) -> List[List[float]]:
        return self._embedder.embed(list(texts))

    @staticmethod
    def _is_zero(vector: Sequence[float]) -> bool:
        return not any(abs(float(v)) > 1e-9 for v in vector)

    def upsert(self, path: str, text: str) -> None:
        text = (text or "")[:2000]
        vector = self._embed_docs([text])[0]
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM vec_items WHERE path = ?", (path,))
            if self._is_zero(vector):
                # 零向量在余弦距离下是未定义（distance=NULL），宁可不入索引
                c.commit()
                return
            payload = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
            c.execute("INSERT INTO vec_items(path, embedding) VALUES (?, ?)", (path, payload))
            c.commit()

    def upsert_many(self, items: Sequence[Tuple[str, str]]) -> None:
        """批量写入（重建索引时减少 commit 次数）。"""
        if not items:
            return
        vectors = self._embed_docs([text[:2000] for _p, text in items])
        rows = [
            (path, "[" + ",".join(f"{v:.6f}" for v in vec) + "]")
            for (path, _text), vec in zip(items, vectors)
            if not self._is_zero(vec)
        ]
        if not rows:
            return
        with self._lock:
            c = self._conn
            c.executemany("DELETE FROM vec_items WHERE path = ?", [(p,) for p, _v in rows])
            c.executemany("INSERT INTO vec_items(path, embedding) VALUES (?, ?)", rows)
            c.commit()

    def remove(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM vec_items WHERE path = ?", (path,))
            self._conn.commit()

    def reset(self) -> None:
        with self._lock:
            c = self._conn
            c.execute("DROP TABLE IF EXISTS vec_items")
            c.commit()
            self._ensure_table()

    # ---------------------------------------------------------------- read
    def query(self, text: str, limit: int) -> List[Tuple[str, float]]:
        text = (text or "").strip()
        if limit <= 0 or not text:
            return []
        embed_query = getattr(self._embedder, "embed_query", None)
        vector = embed_query(text) if callable(embed_query) else self._embed_docs([text])[0]
        payload = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT path, distance FROM vec_items"
                    " WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                    (payload, int(limit)),
                ).fetchall()
            except sqlite3.Error:
                logger.exception("sqlite-vec 查询失败")
                return []
        # cosine distance → similarity；NULL（零向量）直接丢弃
        return [
            (path, round(1.0 - float(distance), 6))
            for path, distance in rows
            if distance is not None
        ]

    def count(self) -> int:
        with self._lock:
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0])
            except sqlite3.Error:
                return 0

    @property
    def available(self) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------------- test helper
    @staticmethod
    def cosine(a: Sequence[float], b: Sequence[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0
