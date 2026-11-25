"""A0 值班运行时单测：只读候选、去重、预算、状态机与 generation 丢弃。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.duty.candidates import date_label
from app.duty.policy import normalize_definition
from app.duty.reader import VaultDutyReader
from app.duty.runtime import DutyRuntime
from app.duty.store import DutyStore

TZ = timezone(timedelta(hours=8))

# 固定起点：远离午夜，连推两小时也不会跨日期槽（避免依赖真实时钟）。
FIXED_NOW = datetime(2026, 11, 25, 10, 0, tzinfo=TZ).timestamp()


@pytest.fixture
def duty_env(vault_dir: Path, services, fake_clock):
    """构造注入式 runtime；测试结束关闭所有 store（不启动 watcher/Timer）。"""
    created = []

    def _make(*, now=None, provider=None, generation=1, dirs=None):
        moment = time.time() if now is None else float(now)
        clock = fake_clock(moment)
        store = DutyStore(vault_dir / ".kwey" / "duty.db", clock=clock)
        reader = VaultDutyReader(services.require_vault(), services.require_db())
        runtime = DutyRuntime(
            store=store,
            reader=reader,
            clock=clock,
            tz=TZ,
            provider=provider,
            dirs=dirs,
            generation=generation,
        )
        created.append(store)
        return runtime, store, clock

    yield _make
    for store in created:
        store.close()


def _define(store: DutyStore, recipe: str, **kwargs):
    kwargs.setdefault("enabled", True)
    definition = normalize_definition(recipe, **kwargs)
    store.save_definition(definition)
    return definition


# --------------------------------------------------------------------- 收件箱
class TestInboxDuty:
    def test_no_provider_reports_backlog_without_model(self, duty_env, services, vault_dir):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="随手记一条")
        vault.create_note("notes/工作/范围外.md", content="不在收件箱")

        runtime, store, _clock = duty_env()
        _define(store, "inbox_duty", scope="inbox")
        result = runtime.run_once("inbox_duty")

        assert result["run"]["status"] == "no_change"
        assert result["run"]["summary"]["backlog"] == 1
        assert result["run"]["summary"]["ai"] is False
        assert result["run"]["summary"]["rule_only"] is True
        assert store.list_proposals() == []
        assert not (vault_dir / "inbox" / "想法.md").read_text(
            encoding="utf-8"
        ).startswith("---\ntags:")

    def test_provider_suggestion_creates_pending_proposal(
        self, duty_env, services, fake_provider
    ):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="周五和张三聊定价")
        provider = fake_provider(
            {
                "items": [
                    {
                        "path": "inbox/想法.md",
                        "tags": ["待办", "定价"],
                        "folder": "notes/工作",
                    }
                ]
            }
        )

        runtime, store, _clock = duty_env(provider=provider)
        _define(store, "inbox_duty", scope="inbox")
        result = runtime.run_once("inbox_duty")

        assert result["run"]["status"] == "awaiting_review"
        assert len(provider.calls) == 1
        proposal = result["proposal"]
        assert proposal is not None and proposal["status"] == "pending"
        assert [op["type"] for op in proposal["ops"]] == ["tag", "tag", "move"]
        assert proposal["ops"][-1]["target"] == "notes/工作"
        roles = {entry["role"] for entry in proposal["manifest"]}
        assert {"input", "write_target"} <= roles
        assert "inbox/想法.md" in proposal["sources"]

    def test_same_input_is_deduplicated(self, duty_env, services, fake_provider):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="重复输入")
        provider = fake_provider(
            {"items": [{"path": "inbox/想法.md", "tags": ["待办"], "folder": ""}]}
        )

        runtime, store, _clock = duty_env(provider=provider)
        _define(store, "inbox_duty", scope="inbox")

        first = runtime.run_once("inbox_duty")
        assert first["run"]["status"] == "awaiting_review"

        second = runtime.run_once("inbox_duty")
        assert second["run"]["status"] == "skipped"
        assert second["run"]["error_code"] == "duplicate_input"
        assert len(store.list_proposals()) == 1

    def test_third_run_still_sees_original_pending_proposal(
        self, duty_env, services, fake_provider
    ):
        """中间一条 skipped run 不应遮住更早的待审提案（多 prior 去重）。"""
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="三次输入相同")
        provider = fake_provider(
            {"items": [{"path": "inbox/想法.md", "tags": ["待办"], "folder": ""}]},
            {"items": [{"path": "inbox/想法.md", "tags": ["待办"], "folder": ""}]},
        )

        runtime, store, clock = duty_env(now=FIXED_NOW, provider=provider)
        _define(store, "inbox_duty", scope="inbox")

        assert runtime.run_once("inbox_duty")["run"]["status"] == "awaiting_review"
        clock.advance(3601)
        assert runtime.run_once("inbox_duty")["run"]["status"] == "skipped"
        clock.advance(3601)
        third = runtime.run_once("inbox_duty")
        assert third["run"]["status"] == "skipped"
        assert third["run"]["error_code"] == "duplicate_input"
        assert len(store.list_proposals()) == 1

    def test_rejected_input_is_not_re_reminded(self, duty_env, services, fake_provider):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="被拒绝的输入")
        provider = fake_provider(
            {"items": [{"path": "inbox/想法.md", "tags": ["待办"], "folder": ""}]}
        )

        runtime, store, clock = duty_env(provider=provider)
        _define(store, "inbox_duty", scope="inbox")
        first = runtime.run_once("inbox_duty")
        proposal = first["proposal"]
        store.record_decision(
            proposal["proposal_id"],
            version=1,
            decision="reject",
            actor="tester",
            reason="不需要",
            now=clock(),
        )

        second = runtime.run_once("inbox_duty")
        assert second["run"]["status"] == "skipped"
        assert second["run"]["error_code"] == "duplicate_input"

    def test_provider_error_marks_failed(self, duty_env, services, fake_provider):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="模型坏响应")
        runtime, store, _clock = duty_env(provider=fake_provider("not-json"))
        _define(store, "inbox_duty", scope="inbox")

        result = runtime.run_once("inbox_duty")
        assert result["run"]["status"] == "failed"
        assert result["run"]["error_code"] == "provider_error"


# --------------------------------------------------------------------- 每日简报
class TestDailyBrief:
    def test_rule_based_proposal(self, duty_env, services):
        vault = services.require_vault()
        now = time.time()
        vault.create_note(
            "notes/今日.md",
            content="# 今日\n\n- [ ] 给张三回邮件\n",
        )

        runtime, store, _clock = duty_env(now=now, dirs={"daily": "daily"})
        _define(store, "daily_brief", scope=("notes",))
        result = runtime.run_once("daily_brief")

        assert result["run"]["status"] == "awaiting_review"
        assert result["run"]["summary"]["rule_only"] is True
        proposal = result["proposal"]
        op = proposal["ops"][0]
        assert op["type"] == "create"
        assert op["path"].startswith("daily/")
        assert op["path"].endswith("-知识简报.md")
        assert "规则版" in op["content"]
        assert "给张三回邮件" in op["content"]

    def test_model_generated_brief(self, duty_env, services, fake_provider):
        vault = services.require_vault()
        now = time.time()
        vault.create_note("notes/今日.md", content="今天完成了定价讨论")
        provider = fake_provider({"body": "## 今日摘要\n\n- 完成定价讨论"})

        runtime, store, _clock = duty_env(
            now=now, provider=provider, dirs={"daily": "daily"}
        )
        _define(store, "daily_brief", scope=("notes",))
        result = runtime.run_once("daily_brief")

        assert result["run"]["status"] == "awaiting_review"
        assert result["run"]["summary"]["rule_only"] is False
        assert len(provider.calls) == 1
        assert "## 今日摘要" in result["proposal"]["ops"][0]["content"]

    def test_existing_output_is_not_overwritten(self, duty_env, services):
        vault = services.require_vault()
        now = time.time()
        vault.create_note("notes/今日.md", content="今天有新变化")
        output = f"daily/{date_label(now, TZ)}-知识简报.md"
        vault.create_note(output, content="旧简报")

        runtime, store, _clock = duty_env(now=now, dirs={"daily": "daily"})
        _define(store, "daily_brief", scope=("notes",))
        result = runtime.run_once("daily_brief")

        assert result["run"]["status"] == "no_change"
        assert result["run"]["summary"]["output_exists"] is True
        assert store.list_proposals() == []


# --------------------------------------------------------------------- 健康巡检
class TestWeeklyHealth:
    def test_health_run_records_issues_without_proposal(self, duty_env, services):
        runtime, store, _clock = duty_env()
        _define(store, "weekly_health", scope=())
        result = runtime.run_once("weekly_health")

        assert result["run"]["status"] == "no_change"
        assert isinstance(result["run"]["summary"].get("issues"), list)
        assert store.list_proposals() == []


# --------------------------------------------------------------------- 状态机 / 限额
class TestRuntimeGuards:
    def test_old_generation_result_is_discarded(self, duty_env, services):
        runtime, store, _clock = duty_env(generation=1)
        _define(store, "inbox_duty", scope="inbox")
        queued = runtime.enqueue("inbox_duty")
        assert queued["queued"] is True
        assert queued["run"]["vault_generation"] == 1

        runtime.bump_generation()
        result = runtime.process_next()
        assert result["run"]["status"] == "skipped"
        assert result["run"]["error_code"] == "stale_generation"
        assert store.list_proposals() == []

    def test_schedule_ignores_disabled_but_manual_trial_allowed(self, duty_env):
        runtime, store, _clock = duty_env()
        _define(store, "inbox_duty", enabled=False, scope="inbox")

        assert runtime.enqueue("inbox_duty", trigger="schedule") == {
            "queued": False,
            "reason": "disabled",
        }
        manual = runtime.enqueue("inbox_duty", trigger="manual")
        assert manual["queued"] is True

    def test_pause_blocks_enqueue_and_worker(self, duty_env):
        runtime, store, _clock = duty_env()
        _define(store, "inbox_duty", scope="inbox")
        runtime.pause()
        assert runtime.enqueue("inbox_duty") == {"queued": False, "reason": "paused"}
        assert runtime.process_next() is None

    def test_queue_cap(self, duty_env):
        runtime, store, clock = duty_env()
        _define(store, "inbox_duty", scope="inbox")
        for index in range(20):
            store.create_run(
                run_id=f"queued-{index}",
                duty_id="inbox_duty",
                vault_generation=1,
                config_version=1,
                slot="2026-11-25",
                now=clock(),
            )
        assert runtime.enqueue("inbox_duty") == {
            "queued": False,
            "reason": "queue_full",
        }

    def test_rate_limit_two_runs_per_hour(self, duty_env, services):
        vault = services.require_vault()
        vault.create_note("inbox/想法.md", content="限流测试")
        runtime, store, _clock = duty_env()
        _define(store, "inbox_duty", scope="inbox")

        first = runtime.run_once("inbox_duty")
        second = runtime.run_once("inbox_duty")
        assert first["run"]["status"] in {"no_change", "skipped"}
        assert second["run"]["status"] in {"no_change", "skipped"}

        third = runtime.enqueue("inbox_duty")
        assert third == {"queued": False, "reason": "rate_limited"}
