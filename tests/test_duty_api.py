"""A0 值班集成测试：API 形状、grant 审批边界、apply 预检/逐项状态、事件与恢复。

全部使用临时 vault 与内存 TestClient，不联网、不碰真实库。
"""

from __future__ import annotations

import hashlib
import time

import pytest

from app.api import deps
from app.duty.approval import ApprovalGrants
from app.duty.links import IndexDutyLinks
from app.duty.policy import (
    compute_proposal_hash,
    last_due_slot,
    normalize_definition,
    slot_key,
)
from app.duty.reader import VaultDutyReader
from app.duty.runtime import DutyRuntime
from app.duty.store import DutyStore
from app.events import EventType, VaultEvent


def _headers(client) -> dict:
    data = client.post("/api/agent/bootstrap", json={}).json()["data"]
    return {"X-Kwey-Token": data["token"]}


def _entry(reader, path: str, role: str) -> dict:
    raw = reader.read_raw(path)
    return {
        "path": path,
        "exists": raw is not None,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None,
        "mtime": time.time(),
        "role": role,
    }


def _make_proposal(*, ops, manifest, sources=None, reason="测试提案"):
    store = deps.require_duty_store()
    runtime = deps.require_duty_runtime()
    run = store.create_run(
        duty_id="inbox_duty",
        vault_generation=runtime.generation,
        config_version=1,
        slot="test-slot",
    )["run"]
    proposal_hash = compute_proposal_hash(
        ops=ops, manifest=manifest, sources=sources or [], reason=reason
    )
    return store.create_proposal(
        proposal_id=f"prop_{int(time.time() * 1000)}_{len(store.list_proposals(limit=500))}",
        run_id=run["run_id"],
        ops=ops,
        diffs=[
            {
                "op": "tag",
                "action": "modify",
                "path": ops[0]["path"] if ops else "",
                "before": None,
                "after": None,
            }
        ],
        manifest=manifest,
        sources=sources or [],
        reason=reason,
        proposal_hash=proposal_hash,
    )


# ---------------------------------------------------------------------- API 形状
def test_overview_seeds_three_disabled_duties(client):
    headers = _headers(client)
    response = client.get("/api/duty", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["definitions"]) == 3
    assert {item["recipe"] for item in data["definitions"]} == {
        "inbox_duty",
        "daily_brief",
        "weekly_health",
    }
    assert all(item["enabled"] is False for item in data["definitions"])
    assert data["runtime_state"]["mode"] == "server"
    assert data["runtime_state"]["apply_enabled"] is False
    assert data["pending_count"] == 0
    assert client.get("/api/duty").status_code == 401


def test_put_definition_bumps_config_version_and_rejects_stale_version(client):
    headers = _headers(client)
    client.get("/api/duty", headers=headers)
    current = deps.require_duty_supervisor().store.get_definition("inbox_duty")
    response = client.put(
        "/api/duty/inbox_duty",
        headers=headers,
        json={
            "enabled": True,
            "scope": "inbox",
            "schedule": current["schedule"],
            "limits": current["limits"],
            "config_version": current["config_version"],
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["enabled"] is True
    assert updated["config_version"] == current["config_version"] + 1

    stale = client.put(
        "/api/duty/inbox_duty",
        headers=headers,
        json={"enabled": False, "config_version": current["config_version"]},
    )
    assert stale.status_code == 409


def test_runs_and_proposals_lists_use_envelopes(client):
    headers = _headers(client)
    runs = client.get("/api/duty/runs?limit=10", headers=headers)
    assert runs.status_code == 200
    assert isinstance(runs.json()["data"]["runs"], list)
    proposals = client.get("/api/duty/proposals?status=pending", headers=headers)
    assert proposals.status_code == 200
    assert isinstance(proposals.json()["data"]["proposals"], list)
    assert proposals.json()["data"]["next_cursor"] is None


# ---------------------------------------------------------------------- 审批边界
def test_approve_without_grant_is_forbidden_but_reject_is_allowed(client, vault):
    headers = _headers(client)
    deps.get_duty_supervisor().ensure_seeded()
    vault.create_note("inbox/想法.md", content="随手记")
    reader = deps.require_duty_reader()
    proposal = _make_proposal(
        ops=[{"type": "tag", "path": "inbox/想法.md", "tag": "待办"}],
        manifest=[_entry(reader, "inbox/想法.md", "write_target")],
    )
    denied = client.post(
        f"/api/duty/proposals/{proposal['proposal_id']}/decision",
        headers=headers,
        json={
            "version": proposal["version"],
            "proposal_hash": proposal["proposal_hash"],
            "decision": "approve",
        },
    )
    assert denied.status_code == 403

    rejected = client.post(
        f"/api/duty/proposals/{proposal['proposal_id']}/decision",
        headers=headers,
        json={
            "version": proposal["version"],
            "proposal_hash": proposal["proposal_hash"],
            "decision": "reject",
            "reason": "不需要",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["status"] == "rejected"
    assert not (vault.paths.root / "inbox" / "想法.md").read_text(
        encoding="utf-8"
    ).startswith("---\ntags:")


def test_grant_is_single_use_and_version_bound(client, vault):
    headers = _headers(client)
    vault.create_note("inbox/想法.md", content="需要补标签")
    reader = deps.require_duty_reader()
    ops = [{"type": "tag", "path": "inbox/想法.md", "tag": "待办"}]
    proposal = _make_proposal(
        ops=ops, manifest=[_entry(reader, "inbox/想法.md", "write_target")]
    )
    supervisor = deps.require_duty_supervisor()
    grant = supervisor.issue_grant(proposal["proposal_id"])
    assert grant

    applied = client.post(
        f"/api/duty/proposals/{proposal['proposal_id']}/decision",
        headers=headers,
        json={
            "version": proposal["version"],
            "proposal_hash": proposal["proposal_hash"],
            "decision": "approve",
            "approval_grant": grant,
        },
    )
    assert applied.status_code == 200, applied.text
    data = applied.json()["data"]
    assert data["status"] == "applied"
    assert data["applied"] == 1
    assert "待办" in (vault.read_note("inbox/想法.md")["frontmatter"].get("tags") or [])

    # 重复批准：返回既有状态，不再次执行。
    again = client.post(
        f"/api/duty/proposals/{proposal['proposal_id']}/decision",
        headers=headers,
        json={
            "version": proposal["version"],
            "proposal_hash": proposal["proposal_hash"],
            "decision": "approve",
            "approval_grant": grant,
        },
    )
    assert again.status_code == 200
    assert again.json()["data"]["status"] == "applied"


def test_apply_marks_stale_when_file_changed_after_preview(client, vault):
    headers = _headers(client)
    vault.create_note("inbox/想法.md", content="原始内容")
    reader = deps.require_duty_reader()
    proposal = _make_proposal(
        ops=[{"type": "tag", "path": "inbox/想法.md", "tag": "待办"}],
        manifest=[_entry(reader, "inbox/想法.md", "write_target")],
    )
    vault.write_note("inbox/想法.md", content="用户刚改过")

    grant = deps.require_duty_supervisor().issue_grant(proposal["proposal_id"])
    response = client.post(
        f"/api/duty/proposals/{proposal['proposal_id']}/decision",
        headers=headers,
        json={
            "version": proposal["version"],
            "proposal_hash": proposal["proposal_hash"],
            "decision": "approve",
            "approval_grant": grant,
        },
    )
    assert response.status_code == 409
    assert deps.require_duty_store().get_proposal(proposal["proposal_id"])["status"] == "stale"


# ---------------------------------------------------------------------- 链接影响（复用索引）
def test_runtime_manifest_includes_link_impact(services, vault, fake_provider, fake_clock, tmp_path):
    vault.create_note("inbox/想法.md", content="随手记一条")
    vault.create_note("notes/工作/引用.md", content="见 [[想法]]")
    provider = fake_provider(
        {"items": [{"path": "inbox/想法.md", "tags": [], "folder": "notes/工作"}]}
    )
    store = DutyStore(tmp_path / "duty-links.db", clock=fake_clock(time.time()))
    try:
        reader = VaultDutyReader(deps.require_vault(), deps.require_db())
        runtime = DutyRuntime(
            store=store,
            reader=reader,
            clock=fake_clock(time.time()),
            provider=provider,
            links=IndexDutyLinks(deps.require_vault().rewriter, deps.require_linkgraph()),
        )
        store.save_definition(normalize_definition("inbox_duty", enabled=True, scope="inbox"))
        result = runtime.run_once("inbox_duty")
        proposal = result["proposal"]
        assert proposal is not None
        roles = {(entry["path"], entry["role"]) for entry in proposal["manifest"]}
        assert ("notes/工作/引用.md", "link_impact") in roles
        move_diff = next(diff for diff in proposal["diffs"] if diff["op"] == "move")
        assert move_diff["links_affected"] >= 1
    finally:
        store.close()


# ---------------------------------------------------------------------- 生命周期
def test_restart_marks_applying_interrupted(client, vault):
    headers = _headers(client)
    vault.create_note("inbox/想法.md", content="中断恢复")
    reader = deps.require_duty_reader()
    proposal = _make_proposal(
        ops=[{"type": "tag", "path": "inbox/想法.md", "tag": "待办"}],
        manifest=[_entry(reader, "inbox/想法.md", "write_target")],
    )
    store = deps.require_duty_store()
    store.record_decision(
        proposal["proposal_id"], version=1, decision="approve", actor="tester"
    )
    store.record_apply_item(proposal["proposal_id"], 0, status="intent")

    supervisor = deps.require_duty_supervisor()
    supervisor.prepare()

    assert store.get_proposal(proposal["proposal_id"])["status"] == "interrupted"
    item = store.get_apply_item(proposal["proposal_id"], 0)
    assert item["status"] == "uncertain"
    supervisor.stop()


def test_event_callback_only_enqueues_inbox_and_debounces(client, vault):
    headers = _headers(client)  # noqa: F841 —— 触发 bootstrap，保证 vault 已打开
    supervisor = deps.require_duty_supervisor()
    supervisor.ensure_seeded()
    definition = supervisor.store.get_definition("inbox_duty")
    supervisor.update_definition(
        "inbox_duty",
        {
            "enabled": True,
            "scope": "inbox",
            "schedule": definition["schedule"],
            "limits": definition["limits"],
            "config_version": definition["config_version"],
        },
    )
    supervisor.prepare()
    store = deps.require_duty_store()
    runtime = deps.require_duty_runtime()
    now = time.time()
    # 预先占位最近一个已到期定时槽，隔离 scheduler 补跑，只观察事件入队。
    due_ts = last_due_slot(definition["schedule"], now) or now
    store.create_run(
        duty_id="inbox_duty",
        vault_generation=runtime.generation,
        config_version=2,
        slot=slot_key("inbox_duty", due_ts),
        status="skipped",
    )

    bus = deps.require_eventbus()
    bus.publish(VaultEvent(EventType.CREATED, path="inbox/新想法.md"))
    bus.publish(VaultEvent(EventType.CREATED, path="daily/2026-11-25-知识简报.md"))
    supervisor.tick(now + 31)

    queued = store.list_runs(status="queued", limit=50)
    assert len(queued) == 1
    assert queued[0]["duty_id"] == "inbox_duty"

    # 输出目录事件不产生新任务。
    bus.publish(VaultEvent(EventType.CREATED, path="daily/2026-11-26-知识简报.md"))
    supervisor.tick(now + 62)
    assert len(store.list_runs(status="queued", limit=50)) == 1
    supervisor.stop()
