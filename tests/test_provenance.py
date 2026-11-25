"""B0 写入溯源规则与存储单测（仅用临时 vault，不接触运行实例）。"""

from __future__ import annotations

import json
import re

import pytest

from app.vault.provenance import (
    ActorContext,
    PAYLOAD_SERIALIZATION_VERSION,
    ProvenanceStore,
    STATE_AGENT_REVIEWED_CURRENT,
    STATE_AGENT_UNREVIEWED,
    STATE_EXTERNAL_CHANGED,
    STATE_HUMAN_OR_SYSTEM_CURRENT,
    STATE_UNKNOWN,
    actor_fields,
    canonical_payload,
    derive_review_state,
    describe_note,
    payload_hash,
    raw_hash,
)

RAW = """---
title: 计划
tags: [读书]
created: 2026-11-12T10:00:00+08:00
---

# 计划

内容
"""


def _baseline(raw: str, **overrides):
    entry = {
        "raw_hash": raw_hash(raw),
        "payload_hash": payload_hash(raw),
        "payload_version": PAYLOAD_SERIALIZATION_VERSION,
        "writer_kind": "agent",
        "last_writer": "mcp-stdio",
        "generated": True,
        "reviewed_hash": None,
        "reviewed_version": None,
    }
    entry.update(overrides)
    return entry


class TestActor:
    def test_invalid_kind_falls_back_to_unknown(self):
        actor = ActorContext(kind="admin", name="  ", role="admin")
        assert actor.kind == "unknown"
        assert actor.role == "admin"  # 权限角色与 writer_kind 分离
        assert actor.is_agent is False

    def test_actor_fields_are_namespaced_and_generated_by_kind(self):
        fields = actor_fields(
            ActorContext(kind="agent", name="mcp-stdio", run_id=" duty:inbox "),
            generated=False,
        )
        assert fields["kwey_writer_kind"] == "agent"
        assert fields["kwey_last_writer"] == "mcp-stdio"
        assert fields["kwey_run_id"] == "duty:inbox"
        assert fields["kwey_generated"] is True
        assert fields["kwey_hash_version"] == PAYLOAD_SERIALIZATION_VERSION

    def test_unknown_actor_does_not_claim_human(self):
        fields = actor_fields(None, generated=False)
        assert fields["kwey_writer_kind"] == "unknown"
        assert fields["kwey_generated"] is False
        assert "kwey_run_id" not in fields


class TestPayloadHash:
    def test_stable_across_line_endings_order_and_managed_fields(self):
        other = (
            "---\n"
            "tags: [读书]\n"
            "updated: 2026-11-25T09:00:00+08:00\n"
            "kwey_writer_kind: human\n"
            "kwey_generated: false\n"
            "title: 计划\n"
            "created: 2020-01-01T00:00:00+08:00\n"
            "---\n\n"
            "# 计划\r\n\r\n内容\r\n\r\n"
        )
        assert payload_hash(RAW) == payload_hash(other)

    def test_unicode_nfc_and_nfd_are_equivalent(self):
        nfc = "---\ntitle: café\n---\n\ncafé\n"
        nfd = "---\ntitle: cafe\u0301\n---\n\ncafe\u0301\n"
        assert payload_hash(nfc) == payload_hash(nfd)

    def test_business_field_or_body_change_changes_hash(self):
        base = payload_hash(RAW)
        assert payload_hash(RAW.replace("title: 计划", "title: 计划2")) != base
        assert payload_hash(RAW.replace("[读书]", "[读书, 2026]")) != base
        assert payload_hash(RAW.replace("内容", "内容改")) != base

    def test_created_updated_and_kwey_fields_are_excluded(self):
        base = payload_hash(RAW)
        changed = RAW.replace(
            "created: 2026-11-12T10:00:00+08:00",
            "created: 2026-11-13T10:00:00+08:00\nupdated: 2026-11-25T00:00:00+08:00",
        ).replace("---\n\n#", "kwey_reviewed_hash: deadbeef\n---\n\n#")
        assert payload_hash(changed) == base

    def test_yaml_date_not_colliding_with_string(self):
        date_raw = "---\ndue: 2026-01-01\n---\n正文\n"
        str_raw = '---\ndue: "2026-01-01"\n---\n正文\n'
        assert payload_hash(date_raw) != payload_hash(str_raw)
        assert payload_hash(date_raw) == payload_hash(date_raw)

    def test_hash_is_sha256_hex(self):
        assert re.fullmatch(r"[0-9a-f]{64}", payload_hash(RAW))

    def test_unsupported_serialization_version_is_rejected(self):
        with pytest.raises(ValueError):
            payload_hash(RAW, version=0)
        with pytest.raises(ValueError):
            canonical_payload(RAW, version=PAYLOAD_SERIALIZATION_VERSION + 1)


class TestReviewStates:
    def test_no_baseline_is_unknown_even_with_frontmatter_claim(self):
        claimed = (
            "---\n"
            "kwey_writer_kind: human\n"
            "kwey_last_writer: me\n"
            "kwey_reviewed_hash: " + "a" * 64 + "\n"
            "---\n\n正文\n"
        )
        assert derive_review_state(claimed, None) == STATE_UNKNOWN

    def test_agent_unreviewed_and_reviewed_current(self):
        assert derive_review_state(RAW, _baseline(RAW)) == STATE_AGENT_UNREVIEWED
        reviewed = _baseline(
            RAW,
            reviewed_hash=payload_hash(RAW),
            reviewed_version=PAYLOAD_SERIALIZATION_VERSION,
        )
        assert derive_review_state(RAW, reviewed) == STATE_AGENT_REVIEWED_CURRENT

    def test_review_hash_from_other_version_is_not_current(self):
        stale = _baseline(
            RAW,
            reviewed_hash=payload_hash(RAW),
            reviewed_version=PAYLOAD_SERIALIZATION_VERSION + 1,
        )
        assert derive_review_state(RAW, stale) == STATE_AGENT_UNREVIEWED

    def test_unsupported_payload_version_degrades_to_unknown(self):
        stale = _baseline(
            RAW,
            payload_version=PAYLOAD_SERIALIZATION_VERSION + 1,
            reviewed_hash=payload_hash(RAW),
            reviewed_version=PAYLOAD_SERIALIZATION_VERSION + 1,
        )
        assert derive_review_state(RAW, stale) == STATE_UNKNOWN

    def test_unknown_writer_kind_with_baseline_is_unknown(self):
        entry = _baseline(RAW, writer_kind="unknown", generated=False)
        assert derive_review_state(RAW, entry) == STATE_UNKNOWN

    def test_human_and_system_current(self):
        human = _baseline(RAW, writer_kind="human", generated=False)
        system = _baseline(RAW, writer_kind="system", generated=False)
        assert derive_review_state(RAW, human) == STATE_HUMAN_OR_SYSTEM_CURRENT
        assert derive_review_state(RAW, system) == STATE_HUMAN_OR_SYSTEM_CURRENT

    def test_generated_stays_agent_unreviewed_after_human_kind(self):
        entry = _baseline(RAW, writer_kind="human", generated=True)
        assert derive_review_state(RAW, entry) == STATE_AGENT_UNREVIEWED

    def test_external_change_takes_precedence(self):
        entry = _baseline(RAW)
        assert derive_review_state(RAW + "\n外部追加\n", entry) == STATE_EXTERNAL_CHANGED

    def test_all_five_states_are_covered(self):
        reviewed = _baseline(
            RAW,
            reviewed_hash=payload_hash(RAW),
            reviewed_version=PAYLOAD_SERIALIZATION_VERSION,
        )
        states = {
            derive_review_state(RAW, None),
            derive_review_state(RAW, _baseline(RAW)),
            derive_review_state(RAW, reviewed),
            derive_review_state(RAW, _baseline(RAW, writer_kind="human", generated=False)),
            derive_review_state(RAW + "x", _baseline(RAW)),
        }
        assert states == {
            STATE_UNKNOWN,
            STATE_AGENT_UNREVIEWED,
            STATE_AGENT_REVIEWED_CURRENT,
            STATE_HUMAN_OR_SYSTEM_CURRENT,
            STATE_EXTERNAL_CHANGED,
        }


class TestStore:
    def test_record_write_persists_and_reloads(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        actor = ActorContext(kind="agent", name="mcp-stdio", origin="mcp")
        entry = store.record_write("notes/a.md", RAW, actor)
        assert entry["writer_kind"] == "agent"
        assert entry["generated"] is True
        assert store.get_baseline("notes/a.md") is not None
        assert not list((tmp_path / ".kwey").glob("*.tmp"))

        reloaded = ProvenanceStore(tmp_path)
        assert reloaded.get_baseline("notes/a.md")["payload_hash"] == entry["payload_hash"]
        assert derive_review_state(RAW, reloaded.get_baseline("notes/a.md")) == (
            STATE_AGENT_UNREVIEWED
        )

    def test_store_sees_other_process_baseline_without_restart(self, tmp_path):
        """同库多进程：桌面 UI 已加载后，MCP 进程写入的 Agent 基线必须立即可见。"""
        app_store = ProvenanceStore(tmp_path)
        assert app_store.get_baseline("notes/a.md") is None  # 先加载（空）

        agent_store = ProvenanceStore(tmp_path)  # 模拟另一进程（MCP stdio）
        agent_store.record_write("notes/a.md", RAW, ActorContext(kind="agent", name="mcp-stdio"))

        entry = app_store.get_baseline("notes/a.md")
        assert entry is not None and entry["writer_kind"] == "agent"
        assert derive_review_state(RAW, entry) == STATE_AGENT_UNREVIEWED

        # 本进程随后写另一篇，不能抹掉另一进程刚写入的基线
        app_store.record_write("notes/b.md", RAW, ActorContext(kind="human", name="local-ui"))
        assert ProvenanceStore(tmp_path).get_baseline("notes/a.md") is not None

    def test_delete_baseline_downgrades_to_unknown(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        store.record_write("notes/a.md", RAW, ActorContext(kind="agent", name="mcp-stdio"))
        assert store.delete_baseline("notes/a.md") is True
        assert store.get_baseline("notes/a.md") is None
        assert derive_review_state(RAW, store.get_baseline("notes/a.md")) == STATE_UNKNOWN
        # 持久化后仍是 unknown，而不是回退成 human
        assert derive_review_state(RAW, ProvenanceStore(tmp_path).get_baseline("notes/a.md")) == (
            STATE_UNKNOWN
        )

    def test_review_then_human_edit_keeps_generated_and_invalidates_review(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        path = "notes/a.md"
        store.record_write(path, RAW, ActorContext(kind="agent", name="mcp-stdio"))
        assert store.record_review(path, reviewed_hash=payload_hash(RAW)) is True
        assert derive_review_state(RAW, store.get_baseline(path)) == (
            STATE_AGENT_REVIEWED_CURRENT
        )

        # 人工保存相同内容：generated 不被洗掉，已审仍对应当前版本
        store.record_write(path, RAW, ActorContext(kind="human", name="local-ui"))
        entry = store.get_baseline(path)
        assert entry["writer_kind"] == "human"
        assert entry["generated"] is True
        assert derive_review_state(RAW, entry) == STATE_AGENT_REVIEWED_CURRENT

        # 人工改内容：旧 review hash 失效，但 Agent 派生标记保留
        edited = RAW.replace("内容", "人工修改")
        store.record_write(path, edited, ActorContext(kind="human", name="local-ui"))
        assert derive_review_state(edited, store.get_baseline(path)) == (
            STATE_AGENT_UNREVIEWED
        )

    def test_review_without_baseline_returns_false(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        assert store.record_review("notes/none.md", reviewed_hash="a" * 64) is False

    def test_review_rejects_unsupported_version(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        store.record_write("notes/a.md", RAW, ActorContext(kind="agent"))
        with pytest.raises(ValueError):
            store.record_review(
                "notes/a.md",
                reviewed_hash=payload_hash(RAW),
                payload_version=PAYLOAD_SERIALIZATION_VERSION + 1,
            )

    def test_external_detection_does_not_overwrite_baseline(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        path = "notes/a.md"
        store.record_write(path, RAW, ActorContext(kind="human", name="local-ui"))
        before = store.get_baseline(path)

        changed = RAW + "\n外部工具追加\n"
        assert store.detect_external(path, changed) is True
        assert store.record_external(path, changed) is True
        after = store.get_baseline(path)
        assert after["raw_hash"] == before["raw_hash"]
        assert derive_review_state(changed, after) == STATE_EXTERNAL_CHANGED
        assert store.recent_events(1)[0]["action"] == "external_changed"

    def test_bounded_baselines_and_events(self, tmp_path):
        store = ProvenanceStore(tmp_path, max_baselines=2, max_events=3)
        for i in range(5):
            store.record_write(
                f"notes/{i}.md",
                RAW,
                ActorContext(kind="agent", name="mcp-stdio"),
            )
        assert store.baseline_count == 2
        assert store.get_baseline("notes/0.md") is None
        assert store.get_baseline("notes/1.md") is None
        assert store.get_baseline("notes/4.md") is not None
        events = store.recent_events(10)
        assert [e["path"] for e in events] == ["notes/2.md", "notes/3.md", "notes/4.md"]

    def test_store_rejects_paths_outside_notes(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        with pytest.raises(ValueError):
            store.record_write("../外面.md", RAW)
        with pytest.raises(ValueError):
            store.record_write(".kwey/provenance.json", RAW)

    def test_corrupt_store_loads_empty_without_crashing(self, tmp_path):
        target = tmp_path / ".kwey" / "provenance.json"
        target.parent.mkdir(parents=True)
        target.write_text("{ not json", encoding="utf-8")
        store = ProvenanceStore(tmp_path)
        assert store.baseline_count == 0
        assert store.get_baseline("notes/a.md") is None

    def test_store_file_shape_is_auditable(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        store.record_write("notes/a.md", RAW, ActorContext(kind="agent", name="mcp-stdio"))
        data = json.loads(store.path.read_text(encoding="utf-8"))
        assert data["version"] == ProvenanceStore.STORE_VERSION
        entry = data["baselines"]["notes/a.md"]
        assert set(entry) >= {
            "path", "raw_hash", "payload_hash", "payload_version",
            "writer_kind", "last_writer", "run_id", "generated",
            "recorded_at", "reviewed_at", "reviewed_hash", "reviewed_version",
        }
        assert "content" not in entry and "body" not in entry


class TestDescribe:
    def test_describe_marks_declaration_unverified_without_baseline(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        claimed = "---\nkwey_writer_kind: human\nkwey_last_writer: me\n---\n\n正文\n"
        info = describe_note(store, "notes/claim.md", claimed)
        assert info["state"] == STATE_UNKNOWN
        assert info["claim_unverified"] is True
        assert info["declared"]["writer_kind"] == "human"

    def test_describe_uses_baseline_over_declaration(self, tmp_path):
        store = ProvenanceStore(tmp_path)
        path = "notes/a.md"
        store.record_write(path, RAW, ActorContext(kind="agent", name="mcp-stdio"))
        info = describe_note(store, path, RAW)
        assert info["state"] == STATE_AGENT_UNREVIEWED
        assert info["claim_unverified"] is False
        assert info["baseline"]["writer_kind"] == "agent"
