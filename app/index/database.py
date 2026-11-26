"""SQLite 连接管理、建表 DDL、FTS5 虚表、检索查询。

文件位置：``<vault>/.kwey/index.db``。全部表可整库重建（删除文件即可，G4）。

FTS5 中文策略（风险点④）：unicode61 tokenizer 不切分 CJK，因此索引与查询前
统一按字切分（``cjk_space``），查询短语用双引号保证相邻；snippet 输出后用
``cjk_compact`` 去掉字符间空格，中文截断显示正常。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")


def cjk_space(text: str) -> str:
    """CJK 相邻字符之间插入空格（按字切分），供 FTS5 索引/查询。"""
    return CJK_RE.sub(r"\1 ", text)


def cjk_compact(text: str) -> str:
    """去掉 CJK 字符之间的空格（snippet 后处理，中文截断还原）。"""
    return re.sub(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]) (?=[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])", r"\1", text)


DDL = [
    """
    CREATE TABLE IF NOT EXISTS notes (
        path     TEXT PRIMARY KEY,
        title    TEXT NOT NULL,
        mtime    REAL NOT NULL,
        size     INTEGER NOT NULL,
        created  TEXT,
        updated  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        note_path TEXT NOT NULL,
        tag       TEXT NOT NULL,
        source    TEXT NOT NULL,
        PRIMARY KEY (note_path, tag, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS links (
        source_path TEXT NOT NULL,
        target      TEXT NOT NULL,
        target_path TEXT,
        kind        TEXT NOT NULL,
        line        INTEGER,
        PRIMARY KEY (source_path, target, kind, line)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (
        path UNINDEXED,
        title,
        body,
        tags,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    # 附件全文索引：与笔记库完全隔离（不含标签/双链语义、不参与图扩展）
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5 (
        path UNINDEXED,
        title,
        body,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    # 附件索引状态：让「超限/截断/缺转换器」等未完整索引原因在界面上可见
    """
    CREATE TABLE IF NOT EXISTS attachment_status (
        path    TEXT PRIMARY KEY,
        state   TEXT NOT NULL,
        detail  TEXT,
        mtime   REAL,
        size    INTEGER,
        updated REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
    """,
    # 结构化视图（Bases）：frontmatter 原样 JSON 入索引，避免每次查询读盘解析。
    # 属于可重建索引，永不作为事实源。
    """
    CREATE TABLE IF NOT EXISTS props (
        note_path TEXT PRIMARY KEY,
        data      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_path)",
    "CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)",
]


class IndexDatabase:
    """SQLite 索引封装。线程安全：单连接 + 锁（写频率低，足够）。"""

    def __init__(self, vault_root: Path) -> None:
        self._lock = threading.RLock()
        self._root = Path(vault_root)
        self._conn: Optional[sqlite3.Connection] = None
        self.connect()

    # ------------------------------------------------------------------ setup
    def connect(self) -> None:
        db_path = self._root / ".kwey" / "index.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for stmt in DDL:
            self._conn.execute(stmt)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库增列（CREATE TABLE IF NOT EXISTS 不会补列）。幂等，失败不阻断启动。"""
        try:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(notes)")}
            if "excerpt" not in cols:
                self._conn.execute("ALTER TABLE notes ADD COLUMN excerpt TEXT")
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn

    def reset(self) -> None:
        """整库重建入口：清空全部索引数据（保留连接）。"""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM notes")
            c.execute("DELETE FROM tags")
            c.execute("DELETE FROM links")
            c.execute("DELETE FROM notes_fts")
            c.execute("DELETE FROM attachments_fts")
            c.execute("DELETE FROM attachment_status")
            c.execute("DELETE FROM props")
            c.execute("DELETE FROM meta")
            c.commit()

    # ------------------------------------------------------------------ notes
    def upsert_note(
        self,
        path: str,
        title: str,
        mtime: float,
        size: int,
        created: Optional[str] = None,
        updated: Optional[str] = None,
        excerpt: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO notes (path, title, mtime, size, created, updated, excerpt)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    title=excluded.title, mtime=excluded.mtime, size=excluded.size,
                    created=excluded.created, updated=excluded.updated,
                    excerpt=excluded.excerpt
                """,
                (path, title, mtime, size, created, updated, excerpt),
            )
            self.conn.commit()

    def remove_note(self, path: str) -> None:
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM notes WHERE path = ?", (path,))
            c.execute("DELETE FROM tags WHERE note_path = ?", (path,))
            c.execute("DELETE FROM links WHERE source_path = ?", (path,))
            c.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            c.execute("DELETE FROM props WHERE note_path = ?", (path,))
            c.commit()

    def get_note(self, path: str) -> Optional[Dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT path, title, mtime, size, created, updated, excerpt"
                " FROM notes WHERE path = ?",
                (path,),
            ).fetchone()
        if row is None:
            return None
        keys = ("path", "title", "mtime", "size", "created", "updated", "excerpt")
        return dict(zip(keys, row))

    def all_notes(self) -> List[Dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, title, mtime, size, created, updated, excerpt FROM notes"
            ).fetchall()
        keys = ("path", "title", "mtime", "size", "created", "updated", "excerpt")
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------------------ props
    def upsert_props(self, path: str, data: Dict) -> None:
        """写入 frontmatter 快照（JSON）。空 dict 也写，代表"有笔记但无属性"。"""
        import json as _json

        with self._lock:
            self.conn.execute(
                "INSERT INTO props (note_path, data) VALUES (?, ?)"
                " ON CONFLICT(note_path) DO UPDATE SET data=excluded.data",
                (path, _json.dumps(data or {}, ensure_ascii=False, default=str)),
            )
            self.conn.commit()

    def get_props(self, path: str) -> Dict:
        import json as _json

        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM props WHERE note_path = ?", (path,)
            ).fetchone()
        if row is None:
            return {}
        try:
            return _json.loads(row[0])
        except (ValueError, TypeError):
            return {}

    def all_props(self) -> Dict[str, Dict]:
        """全部 frontmatter 快照 → {path: dict}。结构化视图一次查询用。"""
        import json as _json

        with self._lock:
            rows = self.conn.execute("SELECT note_path, data FROM props").fetchall()
        out: Dict[str, Dict] = {}
        for path, raw in rows:
            try:
                out[path] = _json.loads(raw)
            except (ValueError, TypeError):
                out[path] = {}
        return out

    def props_keys(self) -> List[str]:
        """全部出现过的 frontmatter 键（去重）。"""
        import json as _json

        with self._lock:
            rows = self.conn.execute("SELECT data FROM props").fetchall()
        keys = set()
        for (raw,) in rows:
            try:
                data = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                keys.update(str(k) for k in data)
        return sorted(keys)

    def count_notes(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])

    # ------------------------------------------------------------------ fts
    def upsert_fts(self, path: str, title: str, body: str, tags_joined: str) -> None:
        """写入 FTS 行（先删后插）；正文/标题/标签均按字切分。"""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            c.execute(
                "INSERT INTO notes_fts (path, title, body, tags) VALUES (?, ?, ?, ?)",
                (path, cjk_space(title), cjk_space(body), cjk_space(tags_joined)),
            )
            c.commit()

    # ------------------------------------------------------------------ attachments
    def upsert_attachment_fts(self, path: str, title: str, body: str) -> None:
        """写入附件全文（先删后插）；与 notes_fts 完全隔离。"""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM attachments_fts WHERE path = ?", (path,))
            c.execute(
                "INSERT INTO attachments_fts (path, title, body) VALUES (?, ?, ?)",
                (path, cjk_space(title), cjk_space(body)),
            )
            c.commit()

    def remove_attachment_fts(self, path: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM attachments_fts WHERE path = ?", (path,))
            self.conn.commit()

    def count_attachments(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM attachments_fts").fetchone()[0])

    def set_attachment_status(
        self,
        path: str,
        state: str,
        detail: str = "",
        mtime: Optional[float] = None,
        size: Optional[int] = None,
    ) -> None:
        """记录附件索引状态（indexed / too_large / truncated / empty / ...）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO attachment_status (path, state, detail, mtime, size, updated)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " state=excluded.state, detail=excluded.detail, mtime=excluded.mtime,"
                " size=excluded.size, updated=excluded.updated",
                (path, state, detail or "", mtime, size, time.time()),
            )
            self.conn.commit()

    def attachment_status(self, path: str) -> Optional[Dict[str, object]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT path, state, detail, mtime, size, updated"
                " FROM attachment_status WHERE path = ?",
                (path,),
            ).fetchone()
        if row is None:
            return None
        return {
            "path": row[0],
            "state": row[1],
            "detail": row[2] or "",
            "mtime": row[3],
            "size": row[4],
            "updated": row[5],
        }

    def attachment_statuses(self) -> Dict[str, Dict[str, str]]:
        """全部附件状态（文件树批量标注用）→ {path: {state, detail}}。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, state, detail FROM attachment_status"
            ).fetchall()
        return {r[0]: {"state": r[1], "detail": r[2] or ""} for r in rows}

    def remove_attachment_status(self, path: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM attachment_status WHERE path = ?", (path,))
            self.conn.commit()

    def search_attachments(
        self,
        terms: List[str],
        path_prefix: Optional[str] = None,
        file_pattern: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[Dict], int]:
        """附件全文检索。返回 (hits, total)，hit 带 ``kind='attachment'``。

        附件没有标签/双链，只支持 path:/file: 过滤（tag: 不适用于附件）。
        """
        if not terms:
            return [], 0
        where = ["attachments_fts MATCH :match"]
        params: Dict[str, object] = {"match": " ".join(terms), "limit": limit}
        if path_prefix:
            where.append("path LIKE :pp")
            params["pp"] = path_prefix.rstrip("/") + "/%"
        if file_pattern:
            where.append("(title LIKE :fp OR path LIKE :fp2)")
            params["fp"] = f"%{file_pattern}%"
            params["fp2"] = f"%{file_pattern}%"
        where_sql = " WHERE " + " AND ".join(where)

        with self._lock:
            c = self.conn
            total = int(
                c.execute(f"SELECT COUNT(*) FROM attachments_fts{where_sql}", params).fetchone()[0]
            )
            rows = c.execute(
                "SELECT path, title, -bm25(attachments_fts) AS score,"
                " snippet(attachments_fts, 2, '<mark>', '</mark>', '…', 24) AS snip"
                f" FROM attachments_fts{where_sql}"
                " ORDER BY score DESC, path ASC LIMIT :limit",
                params,
            ).fetchall()

        hits = [
            {
                "path": r[0],
                # 标题取路径末段原文：FTS 里按字切分会带空格，不能直接展示
                "title": str(r[0]).rsplit("/", 1)[-1],
                "score": round(float(r[2]), 6),
                "snippet": cjk_compact(r[3] or ""),
                "tags": [],
                "kind": "attachment",
                "format": str(r[0]).rsplit(".", 1)[-1].lower() if "." in str(r[0]) else "",
            }
            for r in rows
        ]
        return hits, total

    # ------------------------------------------------------------------ links
    def replace_links(self, path: str, rows: Sequence[Tuple[str, Optional[str], str, int]]) -> None:
        """覆盖某笔记全部出链。rows: (target, target_path|None, kind, line)。"""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM links WHERE source_path = ?", (path,))
            c.executemany(
                "INSERT OR IGNORE INTO links (source_path, target, target_path, kind, line)"
                " VALUES (?, ?, ?, ?, ?)",
                [(path, t, tp, k, ln) for (t, tp, k, ln) in rows],
            )
            c.commit()

    def links_for(self, path: str) -> List[Dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT source_path, target, target_path, kind, line"
                " FROM links WHERE source_path = ?",
                (path,),
            ).fetchall()
        return [
            {"source_path": r[0], "target": r[1], "target_path": r[2], "kind": r[3], "line": r[4]}
            for r in rows
        ]

    def link_sources(self, target_path: str) -> List[str]:
        """引用某路径的全部源笔记（去重），供改名重写。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT source_path FROM links WHERE target_path = ?",
                (target_path,),
            ).fetchall()
        return [r[0] for r in rows]

    def break_links_to(self, target_path: str) -> None:
        """目标被删除后，把指向它的链接标记为断链（target_path = NULL）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE links SET target_path = NULL WHERE target_path = ?",
                (target_path,),
            )
            self.conn.commit()

    def all_links(self) -> List[Dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT source_path, target, target_path, kind, line FROM links"
            ).fetchall()
        return [
            {"source_path": r[0], "target": r[1], "target_path": r[2], "kind": r[3], "line": r[4]}
            for r in rows
        ]

    def unresolved_links(self) -> List[Dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT source_path, target, target_path, kind, line"
                " FROM links WHERE target_path IS NULL"
            ).fetchall()
        return [
            {"source_path": r[0], "target": r[1], "target_path": r[2], "kind": r[3], "line": r[4]}
            for r in rows
        ]

    def update_link_resolution(self, source_path: str, target: str, target_path: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE links SET target_path = ? WHERE source_path = ? AND target = ?",
                (target_path, source_path, target),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ tags
    def replace_tags(self, path: str, rows: Sequence[Tuple[str, str]]) -> None:
        """覆盖某笔记全部标签。rows: (tag, source)。"""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM tags WHERE note_path = ?", (path,))
            c.executemany(
                "INSERT OR IGNORE INTO tags (note_path, tag, source) VALUES (?, ?, ?)",
                [(path, t, s) for (t, s) in rows],
            )
            c.commit()

    def tags_of_note(self, path: str) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT tag FROM tags WHERE note_path = ? ORDER BY tag", (path,)
            ).fetchall()
        return [r[0] for r in rows]

    def tag_counts(self) -> List[Tuple[str, int]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT tag, COUNT(DISTINCT note_path) AS n FROM tags GROUP BY tag ORDER BY tag"
            ).fetchall()
        return [(r[0], int(r[1])) for r in rows]

    def notes_with_tag(self, tag: str) -> List[Tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT t.note_path, n.title FROM tags t"
                " LEFT JOIN notes n ON n.path = t.note_path"
                " WHERE t.tag = ? ORDER BY t.note_path",
                (tag,),
            ).fetchall()
        return [(r[0], r[1] or r[0]) for r in rows]

    def all_tag_note_pairs(self) -> List[Tuple[str, str]]:
        """全部 (note_path, tag) 去重对，供标签树按前缀聚合。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT note_path, tag FROM tags ORDER BY tag, note_path"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------ search
    def search(
        self,
        terms: List[str],
        path_prefix: Optional[str] = None,
        tag: Optional[str] = None,
        file_pattern: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[Dict], int]:
        """FTS5 BM25 检索 + 结构化过滤。返回 (hits, total)。

        - terms 非空：MATCH（每个 term 已由调用方构造为合法 MATCH 片段）。
        - terms 为空：纯操作符过滤查询（score=0），供切换器/标签浏览复用。
        """
        where = []
        params: Dict[str, object] = {"limit": limit}

        if terms:
            where.append("notes_fts MATCH :match")
            params["match"] = " ".join(terms)

        if path_prefix:
            where.append("n.path LIKE :pp")
            params["pp"] = path_prefix.rstrip("/") + "/%"

        if tag:
            where.append(
                "n.path IN (SELECT note_path FROM tags WHERE tag = :tag)"
            )
            params["tag"] = tag

        if file_pattern:
            where.append("(n.title LIKE :fp OR n.path LIKE :fp2)")
            params["fp"] = f"%{file_pattern}%"
            params["fp2"] = f"%{file_pattern}%"

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        base_from = "notes n"
        fts_join = ""
        if terms:
            fts_join = " JOIN notes_fts ON notes_fts.path = n.path"

        with self._lock:
            c = self.conn
            count_sql = f"SELECT COUNT(*) FROM {base_from}{fts_join} {where_sql}"
            total = int(c.execute(count_sql, params).fetchone()[0])

            if terms:
                select_sql = (
                    "SELECT n.path, n.title,"
                    " -bm25(notes_fts) AS score,"
                    " snippet(notes_fts, 2, '<mark>', '</mark>', '…', 24) AS snip"
                    f" FROM notes_fts JOIN notes n ON n.path = notes_fts.path"
                    f" {where_sql}"
                    " ORDER BY score DESC, n.path ASC LIMIT :limit"
                )
            else:
                select_sql = (
                    "SELECT n.path, n.title, 0.0 AS score, '' AS snip"
                    f" FROM {base_from} {where_sql}"
                    " ORDER BY n.path ASC LIMIT :limit"
                )
            rows = c.execute(select_sql, params).fetchall()

        hits = []
        for r in rows:
            path, title, score, snip = r[0], r[1], float(r[2]), cjk_compact(r[3] or "")
            hits.append(
                {
                    "path": path,
                    "title": title,
                    "score": score,
                    "snippet": snip,
                    "tags": self.tags_of_note(path),
                }
            )
        return hits, total

    def body_mentions(self, title: str, exclude_paths: Iterable[str]) -> List[Dict]:
        """在 body 中按标题做短语匹配，返回提及笔记（剔除已链接者与自身）。"""
        phrase = '"' + cjk_space(title).strip() + '"'
        exclude = set(exclude_paths)
        with self._lock:
            rows = self.conn.execute(
                "SELECT f.path, n.title,"
                " snippet(notes_fts, 2, '<mark>', '</mark>', '…', 24) AS snip"
                " FROM notes_fts f JOIN notes n ON n.path = f.path"
                " WHERE notes_fts MATCH :m"
                " ORDER BY bm25(notes_fts) LIMIT 50",
                {"m": f"body:{phrase}"},
            ).fetchall()
        result = []
        for r in rows:
            if r[0] in exclude:
                continue
            result.append(
                {
                    "path": r[0],
                    "title": r[1],
                    "snippet": cjk_compact(r[2] or ""),
                    "line": 0,
                }
            )
        return result

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def stamp_index_ready(self) -> None:
        self.set_meta("index_ready_at", str(time.time()))
