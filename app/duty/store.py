"""值班控制存储：``<vault>/.kwey/duty.db``（独立 SQLite，带 schema_version）。

- 与可删除重建的 ``index.db`` 分离：审批、run、故障状态是控制数据，
  不能靠重建索引恢复（INNOVATION-PLAN §4.5）。
- 所有写入经本类；调用方注入时钟，测试用假时钟即可确定性推进。
- 正文敏感信息不进普通日志；本模块不打印 ops / diff 内容。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.duty.recipes import PROPOSAL_TTL_SECONDS, DutyDefinition

logger = logging.getLogger("kwey.duty")

SCHEMA_VERSION = 1

RUN_STATUSES = {
    "queued",
    "running",
    "no_change",
    "awaiting_review",
    "failed",
    "budget_exceeded",
    "skipped",
}

# queued → running → 终态；终态不再迁移（重启不把 applying 自动恢复成 pending）。
RUN_TRANSITIONS: Dict[str, set] = {
    "queued": {"running", "skipped", "failed", "budget_exceeded", "no_change"},
    "running": {"no_change", "awaiting_review", "failed", "budget_exceeded", "skipped"},
    "no_change": set(),
    "awaiting_review": set(),
    "failed": set(),
    "budget_exceeded": set(),
    "skipped": set(),
}

PROPOSAL_STATUSES = {
    "pending",
    "rejected",
    "expired",
    "stale",
    "applying",
    "applied",
    "partial",
    "uncertain",
    "interrupted",
}

PROPOSAL_TRANSITIONS: Dict[str, set] = {
    "pending": {"rejected", "expired", "stale", "applying"},
    "applying": {"applied", "partial", "uncertain", "interrupted"},
    "partial": {"applied", "uncertain", "interrupted"},
    "uncertain": {"applied", "partial", "interrupted"},
    "interrupted": {"applied", "partial", "uncertain"},
    "rejected": set(),
    "expired": set(),
    "stale": set(),
    "applied": set(),
}

APPLY_ITEM_STATUSES = {"intent", "applied", "failed", "uncertain"}

DECISIONS = {"approve", "reject"}

_DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS duties (
        duty_id        TEXT PRIMARY KEY,
        recipe         TEXT NOT NULL,
        enabled        INTEGER NOT NULL,
        scope          TEXT NOT NULL,
        schedule       TEXT NOT NULL,
        limits         TEXT NOT NULL,
        config_version INTEGER NOT NULL,
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id           TEXT PRIMARY KEY,
        duty_id          TEXT NOT NULL,
        vault_generation INTEGER NOT NULL,
        config_version   INTEGER NOT NULL,
        input_hash       TEXT,
        slot             TEXT,
        status           TEXT NOT NULL,
        queued_at        REAL NOT NULL,
        started_at       REAL,
        ended_at         REAL,
        model_usage      TEXT NOT NULL DEFAULT '{}',
        error_code       TEXT,
        summary          TEXT NOT NULL DEFAULT '{}',
        idempotency_key  TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        proposal_id   TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL,
        version       INTEGER NOT NULL,
        ops           TEXT NOT NULL,
        diffs         TEXT NOT NULL,
        manifest      TEXT NOT NULL,
        sources       TEXT NOT NULL,
        reason        TEXT NOT NULL DEFAULT '',
        proposal_hash TEXT NOT NULL,
        status        TEXT NOT NULL,
        created_at    REAL NOT NULL,
        expires_at    REAL NOT NULL,
        updated_at    REAL NOT NULL,
        UNIQUE (run_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manifest_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        path        TEXT NOT NULL,
        exists_flag INTEGER NOT NULL,
        raw_sha256  TEXT,
        mtime       REAL,
        role        TEXT NOT NULL,
        UNIQUE (proposal_id, path, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS apply_items (
        proposal_id  TEXT NOT NULL,
        op_index     INTEGER NOT NULL,
        intent_at    REAL,
        result_at    REAL,
        status       TEXT NOT NULL,
        result_paths TEXT NOT NULL DEFAULT '[]',
        error        TEXT,
        PRIMARY KEY (proposal_id, op_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        proposal_id TEXT PRIMARY KEY,
        version     INTEGER NOT NULL,
        decision    TEXT NOT NULL,
        actor       TEXT NOT NULL,
        ts          REAL NOT NULL,
        reason      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_duty_status ON runs(duty_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_runs_input ON runs(duty_id, config_version, input_hash)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_run ON proposals(run_id)",
]


class DutyStoreError(Exception):
    """控制存储领域错误基类。"""


class DutySchemaVersionError(DutyStoreError):
    """磁盘 schema 版本高于当前代码，拒绝打开（避免写坏控制数据）。"""


class DutyStateError(DutyStoreError):
    """状态机非法迁移。"""


class DutyConflictError(DutyStoreError):
    """唯一键 / 乐观版本冲突。"""


class DutyDecisionError(DutyStoreError):
    """decision 枚举非法。"""


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_load(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class DutyStore:
    """线程安全控制存储：单连接 + RLock（与 IndexDatabase 同风格）。"""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: Path, *, clock: Optional[Any] = None) -> None:
        self._path = Path(db_path)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    @classmethod
    def for_vault(cls, vault_root: Path, *, clock: Optional[Any] = None) -> "DutyStore":
        return cls(Path(vault_root) / ".kwey" / "duty.db", clock=clock)

    # ------------------------------------------------------------------ setup
    def _connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in _DDL:
                conn.execute(statement)
            self._check_schema()
            conn.commit()
        except Exception:
            conn.close()
            self._conn = None
            raise

    def _check_schema(self) -> None:
        conn = self._c()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        try:
            found = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise DutySchemaVersionError(
                f"duty.db schema_version 非法: {row['value']!r}"
            ) from exc
        if found > SCHEMA_VERSION:
            raise DutySchemaVersionError(
                f"duty.db schema_version={found} 高于当前代码 {SCHEMA_VERSION}，"
                "请升级 Kwey 或备份后手动处理"
            )
        if found < SCHEMA_VERSION:
            raise DutySchemaVersionError(
                f"duty.db schema_version={found} 低于当前代码 {SCHEMA_VERSION}，"
                "当前版本没有可用的迁移路径"
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DutyStoreError("DutyStore 已关闭")
        return self._conn

    def _now(self, now: Optional[float]) -> float:
        return float(now) if now is not None else float(self._clock())

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._c().execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row["value"]) if row else 0

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ duties
    def save_definition(self, definition: DutyDefinition | Mapping[str, Any]) -> Dict[str, Any]:
        data = definition.to_dict() if hasattr(definition, "to_dict") else dict(definition)
        now = self._now(None)
        with self._lock, self._c():
            self._c().execute(
                """
                INSERT INTO duties(
                    duty_id, recipe, enabled, scope, schedule, limits,
                    config_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(duty_id) DO UPDATE SET
                    recipe=excluded.recipe,
                    enabled=excluded.enabled,
                    scope=excluded.scope,
                    schedule=excluded.schedule,
                    limits=excluded.limits,
                    config_version=excluded.config_version,
                    updated_at=excluded.updated_at
                """,
                (
                    str(data["duty_id"]),
                    str(data["recipe"]),
                    1 if data.get("enabled") else 0,
                    _json_dump(list(data.get("scope") or [])),
                    _json_dump(dict(data.get("schedule") or {})),
                    _json_dump(dict(data.get("limits") or {})),
                    int(data.get("config_version", 1)),
                    now,
                    now,
                ),
            )
            self._c().commit()
        return self.get_definition(str(data["duty_id"]))  # type: ignore[return-value]

    def get_definition(self, duty_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM duties WHERE duty_id = ?", (str(duty_id),)
            ).fetchone()
        return self._definition_row(row) if row else None

    def list_definitions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._c().execute(
                "SELECT * FROM duties ORDER BY duty_id"
            ).fetchall()
        return [self._definition_row(row) for row in rows]

    @staticmethod
    def _definition_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "duty_id": row["duty_id"],
            "recipe": row["recipe"],
            "enabled": bool(row["enabled"]),
            "scope": list(_json_load(row["scope"], [])),
            "schedule": dict(_json_load(row["schedule"], {})),
            "limits": {k: int(v) for k, v in dict(_json_load(row["limits"], {})).items()},
            "config_version": int(row["config_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        duty_id: str,
        vault_generation: int,
        slot: Optional[str],
        config_version: int = 1,
        run_id: Optional[str] = None,
        status: str = "queued",
        idempotency_key: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """插入 run；``idempotency_key`` 冲突时返回既有 run（不重复入队）。"""
        if status not in RUN_STATUSES:
            raise DutyStateError(f"未知 run 状态: {status}")
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        moment = self._now(now)
        with self._lock, self._c():
            try:
                self._c().execute(
                    """
                    INSERT INTO runs(
                        run_id, duty_id, vault_generation, config_version,
                        slot, status, queued_at, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(duty_id),
                        int(vault_generation),
                        int(config_version),
                        slot,
                        status,
                        moment,
                        idempotency_key,
                    ),
                )
                self._c().commit()
            except sqlite3.IntegrityError:
                if idempotency_key:
                    row = self._c().execute(
                        "SELECT * FROM runs WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if row:
                        return {"created": False, "run": self._run_row(row)}
                raise DutyConflictError("run 唯一键冲突") from None
        return {"created": True, "run": self.get_run(run_id)}  # type: ignore[dict-item]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        duty_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM runs"
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if duty_id:
            clauses.append("duty_id = ?")
            params.append(duty_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY queued_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._c().execute(sql, params).fetchall()
        return [self._run_row(row) for row in rows]

    def count_active_runs(self) -> int:
        with self._lock:
            row = self._c().execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row["n"])

    def started_run_timestamps(self, duty_id: str, since: float) -> List[float]:
        with self._lock:
            rows = self._c().execute(
                "SELECT started_at FROM runs"
                " WHERE duty_id = ? AND started_at IS NOT NULL AND started_at >= ?",
                (str(duty_id), float(since)),
            ).fetchall()
        return [float(row["started_at"]) for row in rows]

    def claim_next_queued(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """原子领取最早 queued run 并置 running；单 worker 下的有限队列。"""
        moment = self._now(now)
        with self._lock:
            for _ in range(20):
                row = self._c().execute(
                    "SELECT run_id FROM runs WHERE status = 'queued'"
                    " ORDER BY queued_at ASC, rowid ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                run_id = str(row["run_id"])
                cursor = self._c().execute(
                    "UPDATE runs SET status = 'running', started_at = ?"
                    " WHERE run_id = ? AND status = 'queued'",
                    (moment, run_id),
                )
                self._c().commit()
                if cursor.rowcount:
                    return self.get_run(run_id)
            return None

    def set_run_input(
        self,
        run_id: str,
        input_hash: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """写输入哈希 / 幂等键；键冲突返回既有 run，不覆盖。"""
        with self._lock, self._c():
            try:
                cursor = self._c().execute(
                    "UPDATE runs SET input_hash = ?, idempotency_key = ?"
                    " WHERE run_id = ? AND status IN ('queued', 'running')",
                    (str(input_hash), idempotency_key, str(run_id)),
                )
                self._c().commit()
            except sqlite3.IntegrityError:
                row = self._c().execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return {
                    "ok": False,
                    "conflict": self._run_row(row) if row else None,
                    "run": self.get_run(run_id),
                }
            if cursor.rowcount == 0:
                raise DutyConflictError(f"run 不存在或状态不可更新: {run_id}")
        return {"ok": True, "conflict": None, "run": self.get_run(run_id)}

    def find_prior_runs(
        self,
        *,
        duty_id: str,
        config_version: int,
        input_hash: str,
        slot: Optional[str],
        exclude_run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT * FROM runs WHERE duty_id = ? AND config_version = ?"
            " AND input_hash = ?"
        )
        params: List[Any] = [str(duty_id), int(config_version), str(input_hash)]
        if slot is not None:
            sql += " AND slot = ?"
            params.append(slot)
        if exclude_run_id:
            sql += " AND run_id <> ?"
            params.append(str(exclude_run_id))
        sql += " ORDER BY queued_at DESC, rowid DESC"
        with self._lock:
            rows = self._c().execute(sql, params).fetchall()
        return [self._run_row(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        expected: Optional[str] = None,
        error_code: Optional[str] = None,
        summary: Optional[Mapping[str, Any]] = None,
        model_usage: Optional[Mapping[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if status not in RUN_STATUSES:
            raise DutyStateError(f"未知 run 状态: {status}")
        moment = self._now(now)
        with self._lock, self._c():
            row = self._c().execute(
                "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise DutyConflictError(f"run 不存在: {run_id}")
            current = str(row["status"])
            if expected is not None and current != expected:
                raise DutyStateError(
                    f"run {run_id} 期望状态 {expected}，实际 {current}"
                )
            if status not in RUN_TRANSITIONS.get(current, set()):
                raise DutyStateError(f"run 状态非法迁移: {current} → {status}")

            ended = moment if status not in {"queued", "running"} else row["ended_at"]
            self._c().execute(
                """
                UPDATE runs SET status = ?, ended_at = ?, error_code = ?,
                    summary = ?, model_usage = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    status,
                    ended,
                    error_code if error_code is not None else row["error_code"],
                    _json_dump(dict(summary))
                    if summary is not None
                    else row["summary"],
                    _json_dump(dict(model_usage))
                    if model_usage is not None
                    else row["model_usage"],
                    str(run_id),
                    current,
                ),
            )
            self._c().commit()
        return self.get_run(run_id)  # type: ignore[return-value]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "duty_id": row["duty_id"],
            "vault_generation": int(row["vault_generation"]),
            "config_version": int(row["config_version"]),
            "input_hash": row["input_hash"],
            "slot": row["slot"],
            "status": row["status"],
            "queued_at": row["queued_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "model_usage": dict(_json_load(row["model_usage"], {})),
            "error_code": row["error_code"],
            "summary": dict(_json_load(row["summary"], {})),
            "idempotency_key": row["idempotency_key"],
        }

    # ------------------------------------------------------------------ proposals
    def create_proposal(
        self,
        *,
        proposal_id: str,
        run_id: str,
        ops: Sequence[Mapping[str, Any]],
        diffs: Sequence[Mapping[str, Any]],
        manifest: Sequence[Mapping[str, Any]],
        sources: Sequence[str],
        reason: str,
        proposal_hash: str,
        version: int = 1,
        status: str = "pending",
        expires_at: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if status not in PROPOSAL_STATUSES:
            raise DutyStateError(f"未知提案状态: {status}")
        moment = self._now(now)
        expires = float(expires_at) if expires_at is not None else moment + PROPOSAL_TTL_SECONDS
        with self._lock, self._c():
            try:
                self._c().execute(
                    """
                    INSERT INTO proposals(
                        proposal_id, run_id, version, ops, diffs, manifest,
                        sources, reason, proposal_hash, status,
                        created_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(proposal_id),
                        str(run_id),
                        int(version),
                        _json_dump([dict(op) for op in ops]),
                        _json_dump([dict(diff) for diff in diffs]),
                        _json_dump([dict(entry) for entry in manifest]),
                        _json_dump(list(sources)),
                        str(reason or ""),
                        str(proposal_hash),
                        status,
                        moment,
                        expires,
                        moment,
                    ),
                )
                for entry in manifest:
                    self._c().execute(
                        """
                        INSERT OR REPLACE INTO manifest_entries(
                            proposal_id, path, exists_flag, raw_sha256, mtime, role
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(proposal_id),
                            str(entry.get("path") or ""),
                            1 if entry.get("exists") else 0,
                            entry.get("raw_sha256"),
                            entry.get("mtime"),
                            str(entry.get("role") or "input"),
                        ),
                    )
                self._c().commit()
            except sqlite3.IntegrityError as exc:
                raise DutyConflictError("提案唯一键冲突（同一 run 版本重复）") from exc
        return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (str(proposal_id),)
            ).fetchone()
            if row is None:
                return None
            manifest_rows = self._c().execute(
                "SELECT * FROM manifest_entries WHERE proposal_id = ? ORDER BY id",
                (str(proposal_id),),
            ).fetchall()
        data = self._proposal_row(row)
        data["manifest"] = [
            {
                "path": item["path"],
                "exists": bool(item["exists_flag"]),
                "raw_sha256": item["raw_sha256"],
                "mtime": item["mtime"],
                "role": item["role"],
            }
            for item in manifest_rows
        ]
        return data

    def list_proposals(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT p.*, (SELECT COUNT(*) FROM manifest_entries m"
            " WHERE m.proposal_id = p.proposal_id) AS manifest_count FROM proposals p"
        )
        params: List[Any] = []
        if status:
            sql += " WHERE p.status = ?"
            params.append(status)
        sql += " ORDER BY p.created_at DESC, p.rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._c().execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = self._proposal_row(row)
            item["manifest_count"] = int(row["manifest_count"])
            result.append(item)
        return result

    def latest_proposal_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM proposals WHERE run_id = ?"
                " ORDER BY version DESC, created_at DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        return self._proposal_row(row) if row else None

    def count_unresolved_proposals(self) -> int:
        with self._lock:
            row = self._c().execute(
                "SELECT COUNT(*) AS n FROM proposals"
                " WHERE status IN ('pending', 'applying', 'partial', 'uncertain')"
            ).fetchone()
        return int(row["n"])

    def transition_proposal(
        self,
        proposal_id: str,
        status: str,
        *,
        expected: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if status not in PROPOSAL_STATUSES:
            raise DutyStateError(f"未知提案状态: {status}")
        moment = self._now(now)
        with self._lock, self._c():
            row = self._c().execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (str(proposal_id),)
            ).fetchone()
            if row is None:
                raise DutyConflictError(f"提案不存在: {proposal_id}")
            current = str(row["status"])
            if expected is not None and current != expected:
                raise DutyStateError(
                    f"提案 {proposal_id} 期望状态 {expected}，实际 {current}"
                )
            if status not in PROPOSAL_TRANSITIONS.get(current, set()):
                raise DutyStateError(f"提案状态非法迁移: {current} → {status}")
            self._c().execute(
                "UPDATE proposals SET status = ?, updated_at = ?"
                " WHERE proposal_id = ? AND status = ?",
                (status, moment, str(proposal_id), current),
            )
            self._c().commit()
        return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def expire_pending(self, *, now: Optional[float] = None) -> int:
        moment = self._now(now)
        with self._lock, self._c():
            cursor = self._c().execute(
                "UPDATE proposals SET status = 'expired', updated_at = ?"
                " WHERE status = 'pending' AND expires_at <= ?",
                (moment, moment),
            )
            self._c().commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _proposal_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "run_id": row["run_id"],
            "version": int(row["version"]),
            "ops": list(_json_load(row["ops"], [])),
            "diffs": list(_json_load(row["diffs"], [])),
            "manifest": list(_json_load(row["manifest"], [])),
            "sources": list(_json_load(row["sources"], [])),
            "reason": row["reason"],
            "proposal_hash": row["proposal_hash"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------ decisions
    def record_decision(
        self,
        proposal_id: str,
        *,
        version: int,
        decision: str,
        actor: str,
        reason: str = "",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """原子记录批准/拒绝；重复批准返回既有 decision，不再次执行。"""
        if decision not in DECISIONS:
            raise DutyDecisionError(f"未知 decision: {decision}")
        moment = self._now(now)
        with self._lock, self._c():
            proposal = self._c().execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (str(proposal_id),)
            ).fetchone()
            if proposal is None:
                raise DutyConflictError(f"提案不存在: {proposal_id}")
            if int(proposal["version"]) != int(version):
                raise DutyConflictError(
                    f"提案版本不匹配：期望 {proposal['version']}，收到 {version}"
                )
            existing = self._c().execute(
                "SELECT * FROM decisions WHERE proposal_id = ?", (str(proposal_id),)
            ).fetchone()
            if existing:
                return {
                    "created": False,
                    "decision": dict(existing),
                    "proposal_status": proposal["status"],
                }
            if str(proposal["status"]) != "pending":
                raise DutyStateError(
                    f"提案当前状态 {proposal['status']} 不可决策（仅 pending）"
                )
            target = "rejected" if decision == "reject" else "applying"
            self._c().execute(
                """
                INSERT INTO decisions(
                    proposal_id, version, decision, actor, ts, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(proposal_id),
                    int(version),
                    decision,
                    str(actor or ""),
                    moment,
                    str(reason or ""),
                ),
            )
            self._c().execute(
                "UPDATE proposals SET status = ?, updated_at = ?"
                " WHERE proposal_id = ? AND status = 'pending'",
                (target, moment, str(proposal_id)),
            )
            self._c().commit()
        return {
            "created": True,
            "decision": {
                "proposal_id": str(proposal_id),
                "version": int(version),
                "decision": decision,
                "actor": str(actor or ""),
                "ts": moment,
                "reason": str(reason or ""),
            },
            "proposal_status": target,
        }

    def get_decision(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM decisions WHERE proposal_id = ?", (str(proposal_id),)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ apply items
    def record_apply_item(
        self,
        proposal_id: str,
        op_index: int,
        *,
        status: str,
        result_paths: Optional[Sequence[str]] = None,
        error: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if status not in APPLY_ITEM_STATUSES:
            raise DutyStateError(f"未知 apply 项状态: {status}")
        moment = self._now(now)
        with self._lock, self._c():
            existing = self._c().execute(
                "SELECT * FROM apply_items WHERE proposal_id = ? AND op_index = ?",
                (str(proposal_id), int(op_index)),
            ).fetchone()
            if existing and str(existing["status"]) == "applied" and status != "applied":
                raise DutyConflictError("已应用项不可重跑或改判")
            paths_json = _json_dump(list(result_paths or []))
            if existing:
                self._c().execute(
                    """
                    UPDATE apply_items SET status = ?, result_paths = ?, error = ?,
                        intent_at = ?, result_at = ?
                    WHERE proposal_id = ? AND op_index = ?
                    """,
                    (
                        status,
                        paths_json,
                        error,
                        existing["intent_at"] if existing["intent_at"] else (
                            moment if status == "intent" else None
                        ),
                        moment if status != "intent" else existing["result_at"],
                        str(proposal_id),
                        int(op_index),
                    ),
                )
            else:
                self._c().execute(
                    """
                    INSERT INTO apply_items(
                        proposal_id, op_index, intent_at, result_at,
                        status, result_paths, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(proposal_id),
                        int(op_index),
                        moment if status == "intent" else None,
                        moment if status != "intent" else None,
                        status,
                        paths_json,
                        error,
                    ),
                )
            self._c().commit()
        return self.get_apply_item(proposal_id, op_index)  # type: ignore[return-value]

    def get_apply_item(self, proposal_id: str, op_index: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._c().execute(
                "SELECT * FROM apply_items WHERE proposal_id = ? AND op_index = ?",
                (str(proposal_id), int(op_index)),
            ).fetchone()
        return self._apply_item_row(row) if row else None

    def list_apply_items(self, proposal_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._c().execute(
                "SELECT * FROM apply_items WHERE proposal_id = ? ORDER BY op_index",
                (str(proposal_id),),
            ).fetchall()
        return [self._apply_item_row(row) for row in rows]

    @staticmethod
    def _apply_item_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "op_index": int(row["op_index"]),
            "intent_at": row["intent_at"],
            "result_at": row["result_at"],
            "status": row["status"],
            "result_paths": list(_json_load(row["result_paths"], [])),
            "error": row["error"],
        }
