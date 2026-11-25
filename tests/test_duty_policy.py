"""A0 值班策略单测：范围、预算、ops 白名单、输入哈希与调度判定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.duty.candidates import Candidate
from app.duty.policy import (
    BudgetExceeded,
    DutyPolicyError,
    RunBudget,
    ScopeError,
    compute_input_hash,
    dedup_should_skip,
    idempotency_key,
    last_due_slot,
    normalize_definition,
    normalize_limits,
    normalize_scope,
    rate_limit_exceeded,
    slot_key,
    validate_ops_for_recipe,
)

TZ = timezone(timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


# --------------------------------------------------------------------- 范围
class TestScope:
    def test_normalize_valid(self):
        assert normalize_scope("inbox, notes/读书") == ("inbox", "notes/读书")
        assert normalize_scope(["notes\\读书", "notes/读书"]) == ("notes/读书",)
        assert normalize_scope("") == ()

    @pytest.mark.parametrize(
        "value",
        ["../escape", "notes/../x", "/abs/path", "C:/win", ".kwey", ".trash/x", "templates"],
    )
    def test_normalize_rejects_dangerous(self, value):
        with pytest.raises(ScopeError):
            normalize_scope(value)

    def test_empty_scope_requires_when_needed(self):
        with pytest.raises(ScopeError):
            normalize_scope("", allow_empty=False)

    def test_inbox_scope_must_stay_inside_inbox(self):
        definition = normalize_definition("inbox_duty", scope="inbox")
        assert definition.scope == ("inbox",)
        with pytest.raises(ScopeError):
            normalize_definition("inbox_duty", scope="notes")
        # 子目录允许
        assert normalize_definition("inbox_duty", scope="inbox/待整理").scope == (
            "inbox/待整理",
        )

    def test_custom_inbox_dir_used_as_default(self):
        definition = normalize_definition("inbox_duty", inbox_dir="收集箱")
        assert definition.scope == ("收集箱",)


# --------------------------------------------------------------------- 定义
class TestDefinition:
    def test_defaults_and_override(self):
        definition = normalize_definition("daily_brief", enabled=True, config_version=3)
        assert definition.recipe == "daily_brief"
        assert definition.enabled is True
        assert definition.config_version == 3
        assert definition.schedule == {"kind": "daily", "time": "18:00"}
        assert definition.limits["max_ops"] == 10

    def test_limits_can_shrink_but_not_exceed(self):
        limits = normalize_limits("inbox_duty", {"max_ops": 3, "max_model_calls": 1})
        assert limits["max_ops"] == 3 and limits["max_model_calls"] == 1
        with pytest.raises(DutyPolicyError):
            normalize_limits("inbox_duty", {"max_ops": 11})
        with pytest.raises(DutyPolicyError):
            normalize_limits("inbox_duty", {"max_ops": 0})
        with pytest.raises(DutyPolicyError):
            normalize_limits("inbox_duty", {"unknown": 1})

    def test_schedule_fixed_kind_and_shape(self):
        with pytest.raises(DutyPolicyError):
            normalize_definition("daily_brief", schedule={"kind": "weekly"})
        with pytest.raises(DutyPolicyError):
            normalize_definition("daily_brief", schedule={"time": "25:00"})
        with pytest.raises(DutyPolicyError):
            normalize_definition("daily_brief", schedule={"weekday": 1})
        weekly = normalize_definition("weekly_health", schedule={"time": "20:30", "weekday": 0})
        assert weekly.schedule == {"kind": "weekly", "weekday": 0, "time": "20:30"}

    def test_invalid_duty_id(self):
        with pytest.raises(DutyPolicyError):
            normalize_definition("daily_brief", duty_id="bad id!")


# --------------------------------------------------------------------- 调度
class TestSchedule:
    def test_daily_last_due_slot_within_24h(self):
        schedule = normalize_definition("daily_brief").schedule
        now = _ts(2026, 11, 26, 10, 0)  # 前一天 18:00 过去 16h
        assert last_due_slot(schedule, now, tz=TZ) == _ts(2026, 11, 25, 18, 0)
        now = _ts(2026, 11, 25, 19, 0)
        assert last_due_slot(schedule, now, tz=TZ) == _ts(2026, 11, 25, 18, 0)

    def test_weekly_last_due_slot(self):
        schedule = normalize_definition("weekly_health").schedule  # 周日 18:00
        monday = _ts(2026, 11, 30, 10, 0)
        assert last_due_slot(schedule, monday, tz=TZ) == _ts(2026, 11, 29, 18, 0)
        wednesday = _ts(2026, 12, 2, 10, 0)
        assert last_due_slot(schedule, wednesday, tz=TZ) is None

    def test_slot_key_buckets(self):
        assert slot_key("daily_brief", _ts(2026, 11, 25, 18, 0), TZ) == "2026-11-25"
        assert slot_key("weekly_health", _ts(2026, 11, 29, 18, 0), TZ) == "2026-W48"


# --------------------------------------------------------------------- ops
class TestOpsPolicy:
    def test_inbox_tag_and_move_allowed(self):
        ops = [
            {"type": "tag", "path": "inbox/a.md", "tag": "#待办"},
            {"type": "move", "path": "inbox/a.md", "target": "notes/工作"},
        ]
        result = validate_ops_for_recipe(
            ops,
            recipe="inbox_duty",
            scope=("inbox",),
            allowed_move_dirs=("notes/工作", "notes/读书"),
        )
        assert result[0]["tag"] == "待办"
        assert result[1]["target"] == "notes/工作"

    def test_inbox_rejects_delete_and_out_of_scope(self):
        with pytest.raises(DutyPolicyError):
            validate_ops_for_recipe(
                [{"type": "delete", "path": "inbox/a.md"}],
                recipe="inbox_duty",
                scope=("inbox",),
            )
        with pytest.raises(ScopeError):
            validate_ops_for_recipe(
                [{"type": "tag", "path": "notes/a.md", "tag": "x"}],
                recipe="inbox_duty",
                scope=("inbox",),
            )

    def test_move_target_must_be_existing_allowed_dir(self):
        with pytest.raises(ScopeError):
            validate_ops_for_recipe(
                [{"type": "move", "path": "inbox/a.md", "target": "notes/新目录"}],
                recipe="inbox_duty",
                scope=("inbox",),
                allowed_move_dirs=("notes/工作",),
            )

    def test_daily_create_only_in_output_dir(self):
        valid = [{"type": "create", "path": "daily/2026-11-25-知识简报.md", "content": "x"}]
        assert validate_ops_for_recipe(
            valid, recipe="daily_brief", scope=(), output_dirs=("daily",)
        )
        with pytest.raises(ScopeError):
            validate_ops_for_recipe(
                [{"type": "create", "path": "notes/x.md", "content": "x"}],
                recipe="daily_brief",
                scope=(),
                output_dirs=("daily",),
            )
        with pytest.raises(DutyPolicyError):
            validate_ops_for_recipe(
                [{"type": "tag", "path": "notes/x.md", "tag": "x"}],
                recipe="daily_brief",
                scope=(),
                output_dirs=("daily",),
            )

    def test_ops_count_cap(self):
        ops = [
            {"type": "create", "path": f"daily/{i}.md", "content": "x"}
            for i in range(11)
        ]
        with pytest.raises(BudgetExceeded):
            validate_ops_for_recipe(
                ops, recipe="daily_brief", scope=(), output_dirs=("daily",)
            )


# --------------------------------------------------------------------- 哈希
class TestHashAndDedup:
    def test_input_hash_stable_and_sensitive(self):
        a = Candidate(path="inbox/a.md", title="A", mtime=1.0, size=3, digest="aaa")
        b = Candidate(path="notes/b.md", title="B", mtime=2.0, size=4, digest="bbb")
        h1 = compute_input_hash(recipe="inbox_duty", config_version=1, candidates=[a, b])
        h2 = compute_input_hash(recipe="inbox_duty", config_version=1, candidates=[b, a])
        assert h1 == h2  # 输入顺序不影响
        changed = Candidate(path="inbox/a.md", title="A", mtime=1.0, size=3, digest="zzz")
        assert compute_input_hash(
            recipe="inbox_duty", config_version=1, candidates=[changed, b]
        ) != h1
        assert compute_input_hash(
            recipe="inbox_duty", config_version=2, candidates=[a, b]
        ) != h1

    def test_idempotency_key(self):
        base = idempotency_key("inbox_duty", 1, "hash", "2026-11-25")
        assert base == idempotency_key("inbox_duty", 1, "hash", "2026-11-25")
        assert base != idempotency_key("inbox_duty", 1, "hash", "2026-11-26")

    def test_dedup_rules(self):
        assert dedup_should_skip({"status": "awaiting_review"})
        assert dedup_should_skip({"status": "running"})
        assert not dedup_should_skip({"status": "failed"})
        assert dedup_should_skip({"status": "failed"}, {"status": "pending"})
        assert dedup_should_skip(
            {"status": "failed"}, {"status": "rejected"}, {"decision": "reject"}
        )
        assert not dedup_should_skip({"status": "skipped"})
        assert not dedup_should_skip(None)


# --------------------------------------------------------------------- 预算
class TestBudget:
    def test_ops_and_files(self):
        budget = RunBudget(
            max_ops=1, max_files=2, max_note_chars=10,
            max_model_calls=1, max_model_input_chars=10,
        )
        budget.charge_ops()
        with pytest.raises(BudgetExceeded):
            budget.charge_ops()
        budget.charge_files(2)
        with pytest.raises(BudgetExceeded):
            budget.charge_files(3)
        assert budget.check_note_chars(10)
        assert not budget.check_note_chars(11)

    def test_model_calls_and_input_chars(self):
        budget = RunBudget(
            max_ops=10, max_files=20, max_note_chars=50_000,
            max_model_calls=1, max_model_input_chars=10,
        )
        budget.charge_model(5)
        with pytest.raises(BudgetExceeded):
            budget.charge_model(1)
        fresh = RunBudget(
            max_ops=10, max_files=20, max_note_chars=50_000,
            max_model_calls=3, max_model_input_chars=10,
        )
        with pytest.raises(BudgetExceeded):
            fresh.charge_model(11)

    def test_rate_limit(self):
        assert rate_limit_exceeded([999.0, 1000.0], 1000.0)
        assert not rate_limit_exceeded([999.0], 1000.0)
        assert not rate_limit_exceeded([1000.0 - 4000], 1000.0)
