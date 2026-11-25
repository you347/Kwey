"""A0 值班控制存储单测：schema 版本、状态机、唯一键、决策原子性。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.duty.policy import normalize_definition
from app.duty.store import (
    DutyConflictError,
    DutySchemaVersionError,
    DutyStateError,
    DutyStore,
)


@pytest.fixture
def store_env(vault_dir: Path, fake_clock):
    clock = fake_clock(1_800_000_000.0)
    store = DutyStore(vault_dir / ".kwey" / "duty.db", clock=clock)
    yield store, clock
    store.close()


def _run(store: DutyStore, clock, *, run_id="r1", duty_id="inbox_duty", slot="2026-11-25"):
    return store.create_run(
        run_id=run_id,
        duty_id=duty_id,
        vault_generation=1,
        config_version=1,
        slot=slot,
        now=clock(),
    )


def _proposal(store: DutyStore, clock, *, proposal_id="p1", run_id="r1", expires_at=None):
    return store.create_proposal(
        proposal_id=proposal_id,
        run_id=run_id,
        ops=[{"type": "tag", "path": "inbox/a.md", "tag": "待办"}],
        diffs=[{"op": "tag", "path": "inbox/a.md", "before": "x", "after": "y"}],
        manifest=[
            {
                "path": "inbox/a.md",
                "exists": True,
                "raw_sha256": "abc",
                "mtime": 1.0,
                "role": "input",
            },
            {
                "path": "daily/x.md",
                "exists": False,
                "raw_sha256": None,
                "mtime": None,
                "role": "write_target",
            },
        ],
        sources=["inbox/a.md"],
        reason="测试提案",
        proposal_hash="hash-1",
        expires_at=expires_at,
        now=clock(),
    )


# --------------------------------------------------------------------- schema
class TestSchema:
    def test_fresh_schema_version(self, store_env):
        store, _clock = store_env
        assert store.schema_version == 1
        assert store.path.name == "duty.db"
        assert store.path.parent.name == ".kwey"

    def test_newer_schema_refuses_open(self, store_env):
        store, _clock = store_env
        path = store.path
        store.close()
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                "UPDATE meta SET value = '99' WHERE key = 'schema_version'"
            )
            conn.commit()
        with pytest.raises(DutySchemaVersionError):
            DutyStore(path)

    def test_control_data_survives_reopen(self, store_env):
        store, clock = store_env
        store.save_definition(
            normalize_definition("inbox_duty", enabled=True, scope="inbox", config_version=2)
        )
        created = _run(store, clock)
        path = store.path
        store.close()

        reopened = DutyStore(path, clock=clock)
        try:
            definition = reopened.get_definition("inbox_duty")
            assert definition is not None
            assert definition["config_version"] == 2
            assert definition["enabled"] is True
            run = reopened.get_run(created["run"]["run_id"])
            assert run is not None and run["status"] == "queued"
            assert run["slot"] == "2026-11-25"
        finally:
            reopened.close()


# --------------------------------------------------------------------- 定义 / run
class TestDefinitionsAndRuns:
    def test_save_and_list_definitions(self, store_env):
        store, _clock = store_env
        store.save_definition(
            normalize_definition("inbox_duty", enabled=True, scope="inbox")
        )
        store.save_definition(normalize_definition("daily_brief", config_version=4))
        saved = store.get_definition("daily_brief")
        assert saved is not None
        assert saved["recipe"] == "daily_brief"
        assert saved["enabled"] is False
        assert saved["config_version"] == 4
        assert saved["scope"] == []
        assert [item["duty_id"] for item in store.list_definitions()] == [
            "daily_brief",
            "inbox_duty",
        ]
        assert store.get_definition("missing") is None

    def test_run_transitions(self, store_env):
        store, clock = store_env
        run = _run(store, clock)["run"]
        assert run["status"] == "queued"
        assert store.count_active_runs() == 1

        claimed = store.claim_next_queued(now=clock())
        assert claimed is not None and claimed["run_id"] == run["run_id"]
        assert claimed["status"] == "running"
        assert store.claim_next_queued(now=clock()) is None

        with pytest.raises(DutyStateError):
            store.transition_run(run["run_id"], "queued")

        updated = store.transition_run(
            run["run_id"], "awaiting_review", summary={"candidate_count": 1}
        )
        assert updated["status"] == "awaiting_review"
        assert updated["summary"]["candidate_count"] == 1
        with pytest.raises(DutyStateError):
            store.transition_run(run["run_id"], "running")
        with pytest.raises(DutyStateError):
            store.transition_run(run["run_id"], "failed")

    def test_idempotency_key_returns_existing(self, store_env):
        store, clock = store_env
        first = store.create_run(
            run_id="r1",
            duty_id="inbox_duty",
            vault_generation=1,
            slot="2026-11-25",
            idempotency_key="k1",
            now=clock(),
        )
        second = store.create_run(
            run_id="r2",
            duty_id="inbox_duty",
            vault_generation=1,
            slot="2026-11-25",
            idempotency_key="k1",
            now=clock(),
        )
        assert first["created"] is True
        assert second["created"] is False
        assert second["run"]["run_id"] == "r1"

    def test_set_run_input_conflict_keeps_original(self, store_env):
        store, clock = store_env
        first = _run(store, clock)["run"]
        second = _run(store, clock, run_id="r2")["run"]
        assert store.set_run_input(first["run_id"], "hash-1", "key-1")["ok"] is True
        conflict = store.set_run_input(second["run_id"], "hash-1", "key-1")
        assert conflict["ok"] is False
        assert conflict["conflict"]["run_id"] == first["run_id"]
        assert store.get_run(second["run_id"])["input_hash"] is None

    def test_find_prior_runs(self, store_env):
        store, clock = store_env
        first = _run(store, clock)["run"]
        second = _run(store, clock, run_id="r2")["run"]
        store.set_run_input(first["run_id"], "hash-1", "key-1")
        store.set_run_input(second["run_id"], "hash-1", "key-2")
        priors = store.find_prior_runs(
            duty_id="inbox_duty",
            config_version=1,
            input_hash="hash-1",
            slot="2026-11-25",
            exclude_run_id=second["run_id"],
        )
        assert [item["run_id"] for item in priors] == [first["run_id"]]
        assert (
            store.find_prior_runs(
                duty_id="inbox_duty",
                config_version=1,
                input_hash="hash-1",
                slot="2026-11-26",
            )
            == []
        )

    def test_rate_limit_timestamps(self, store_env):
        store, clock = store_env
        _run(store, clock)
        clock.advance(10)
        _run(store, clock, run_id="r2")
        store.claim_next_queued(now=clock())
        store.claim_next_queued(now=clock())
        stamps = store.started_run_timestamps("inbox_duty", clock() - 3600)
        assert len(stamps) == 2


# --------------------------------------------------------------------- 提案 / 决策
class TestProposalsAndDecisions:
    def test_create_and_get_proposal(self, store_env):
        store, clock = store_env
        _run(store, clock)
        proposal = _proposal(store, clock)
        assert proposal["status"] == "pending"
        assert len(proposal["manifest"]) == 2
        assert proposal["manifest"][1]["exists"] is False
        assert proposal["ops"][0]["tag"] == "待办"
        assert proposal["sources"] == ["inbox/a.md"]
        assert store.latest_proposal_for_run("r1")["proposal_id"] == "p1"
        assert [p["proposal_id"] for p in store.list_proposals(status="pending")] == ["p1"]
        assert store.count_unresolved_proposals() == 1

    def test_decision_is_atomic_and_idempotent(self, store_env):
        store, clock = store_env
        _run(store, clock)
        _proposal(store, clock)

        first = store.record_decision(
            "p1", version=1, decision="approve", actor="tester", now=clock()
        )
        assert first["created"] is True
        assert first["proposal_status"] == "applying"
        assert store.get_proposal("p1")["status"] == "applying"

        second = store.record_decision(
            "p1", version=1, decision="approve", actor="tester", now=clock()
        )
        assert second["created"] is False
        assert second["decision"]["ts"] == first["decision"]["ts"]

        with pytest.raises(DutyConflictError):
            store.record_decision(
                "p1", version=2, decision="approve", actor="tester", now=clock()
            )

    def test_reject_transitions(self, store_env):
        store, clock = store_env
        _run(store, clock)
        _proposal(store, clock)
        result = store.record_decision(
            "p1", version=1, decision="reject", actor="tester", reason="不采纳", now=clock()
        )
        assert result["proposal_status"] == "rejected"
        assert store.get_proposal("p1")["status"] == "rejected"
        assert store.count_unresolved_proposals() == 0
        with pytest.raises(DutyStateError):
            store.transition_proposal("p1", "applying")

    def test_apply_items(self, store_env):
        store, clock = store_env
        _run(store, clock)
        _proposal(store, clock)
        store.record_apply_item("p1", 0, status="intent", now=clock())
        applied = store.record_apply_item(
            "p1", 0, status="applied", result_paths=["inbox/a.md"], now=clock()
        )
        assert applied["status"] == "applied"
        assert applied["result_paths"] == ["inbox/a.md"]
        assert applied["intent_at"] is not None
        with pytest.raises(DutyConflictError):
            store.record_apply_item("p1", 0, status="uncertain", now=clock())
        assert len(store.list_apply_items("p1")) == 1

    def test_expire_pending(self, store_env):
        store, clock = store_env
        _run(store, clock)
        _proposal(store, clock, expires_at=clock() - 1)
        assert store.expire_pending(now=clock()) == 1
        assert store.get_proposal("p1")["status"] == "expired"

    def test_transition_proposal_guards(self, store_env):
        store, clock = store_env
        _run(store, clock)
        _proposal(store, clock)
        with pytest.raises(DutyStateError):
            store.transition_proposal("p1", "applied")  # pending 不能直接 applied
        store.transition_proposal("p1", "applying")
        assert store.transition_proposal("p1", "applied")["status"] == "applied"
        with pytest.raises(DutyStateError):
            store.transition_proposal("p1", "pending")
