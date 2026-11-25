"""P2-C 第二批：待审草案 → 人工审批 → VaultService 落地链路。

覆盖：预览/提交/详情 API、一次性 grant 复用、状态机幂等、过期不可恢复、
证据段来自实际输入、frontmatter 来源字段、B 管理字段沿用统一写路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent import session_distill as sd
from app.api import deps


def _payload(**overrides):
    base = {
        "schema_version": 1,
        "source": {
            "agent": "claude-code",
            "session_id": "session-landing",
            "export_hash": "sha256:client-declared",
        },
        "goal": "把待审草案接到落地链路",
        "retention": "selected_evidence",
        "events": [
            {"id": "e1", "kind": "user", "text": "只改一个文件，不要推送。", "ts": "2026-11-25T10:00:00+08:00"},
            {"id": "e2", "kind": "assistant", "text": "决定采用标准 JSON 输入。", "ts": "2026-11-25T10:01:00+08:00"},
            {"id": "e3", "kind": "tool_result", "text": "测试失败：模块导入报错。", "ts": "2026-11-25T10:02:00+08:00"},
            {"id": "e4", "kind": "assistant", "text": "TODO 下一步补 hook 样本。", "ts": "2026-11-25T10:03:00+08:00"},
            {"id": "e5", "kind": "user", "text": "发现重名笔记必须写完整路径。", "ts": "2026-11-25T10:04:00+08:00"},
        ],
    }
    base.update(overrides)
    return base


def _headers(client) -> dict:
    data = client.post("/api/agent/bootstrap", json={}).json()["data"]
    return {"X-Kwey-Token": data["token"]}


def _md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md"))


def _event_texts(payload) -> dict:
    return {event["id"]: event["text"] for event in payload["events"]}


# ---------------------------------------------------------------- 直接落地
class TestApproveDraft:
    def test_approve_creates_evidence_and_distill_notes(self, vault):
        payload = _payload()
        before = _md_files(vault.paths.root)
        draft = sd.create_draft(vault.paths.root, payload)
        result = sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)

        assert result["status"] == "approved"
        assert result["duplicate"] is False
        notes = {item["role"]: item["path"] for item in result["notes"]}
        assert set(notes) == {"evidence", "distill"}
        assert notes["evidence"].startswith("notes/蒸馏/证据/")
        assert notes["distill"].startswith("notes/蒸馏/")
        assert len(_md_files(vault.paths.root)) == len(before) + 2

        distill = vault.read_note(notes["distill"])
        evidence = vault.read_note(notes["evidence"])
        fm = distill["frontmatter"]
        assert fm["session_agent"] == "claude-code"
        assert fm["session_id"] == "session-landing"
        assert fm["session_sources"] == [notes["evidence"]]
        assert fm["session_build_hash"] == result["build_hash"]
        assert fm["session_draft_id"] == draft["draft_id"]
        assert evidence["frontmatter"]["session_build_hash"] == result["build_hash"]

        # 来源只写 vault 相对路径，不暴露主目录绝对路径
        root_text = str(vault.paths.root)
        for rel in notes.values():
            assert not rel.startswith(("/", "\\"))
            assert ":" not in rel[:2]
            raw = (vault.paths.root / rel).read_text(encoding="utf-8")
            assert root_text not in raw

    def test_evidence_quotes_come_from_actual_input(self, vault):
        payload = _payload()
        draft = sd.create_draft(vault.paths.root, payload)
        result = sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        notes = {item["role"]: item["path"] for item in result["notes"]}
        evidence = vault.read_note(notes["evidence"])["content"]
        actual = _event_texts(payload)

        for eid, item in draft["sources"].items():
            quote = item["quote"]
            assert quote in evidence
            assert quote in actual[eid], "证据段必须是来源事件的实际切片"

    def test_b_writer_and_run_fields_follow_write_path(self, vault):
        draft = sd.create_draft(vault.paths.root, _payload())
        result = sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        for item in result["notes"]:
            fm = vault.read_note(item["path"])["frontmatter"]
            assert fm["kwey_writer_kind"] == "agent"
            assert fm["kwey_last_writer"] == "kwey-session-distill"
            assert fm["kwey_generated"] is True
            assert fm["kwey_run_id"] == draft["draft_id"]
            assert fm["kwey_hash_version"] == 1

    def test_repeated_approve_is_idempotent(self, vault):
        draft = sd.create_draft(vault.paths.root, _payload())
        first = sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        count = len(_md_files(vault.paths.root))
        second = sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        assert second["duplicate"] is True
        assert second["notes"] == first["notes"]
        assert len(_md_files(vault.paths.root)) == count

    def test_reject_is_idempotent_and_blocks_approve(self, vault):
        draft = sd.create_draft(vault.paths.root, _payload())
        before = _md_files(vault.paths.root)
        first = sd.reject_draft(vault.paths.root, draft["draft_id"], reason="不需要")
        again = sd.reject_draft(vault.paths.root, draft["draft_id"])
        assert first["status"] == "rejected"
        assert again["duplicate"] is True
        assert _md_files(vault.paths.root) == before
        with pytest.raises(sd.DraftStateError) as ei:
            sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        assert ei.value.code == 409

    def test_expired_draft_cannot_be_approved(self, vault):
        draft = sd.create_draft(vault.paths.root, _payload())
        path = vault.paths.root / draft["draft_path"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["created_at"] = "2020-01-01T00:00:00+08:00"
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

        before = _md_files(vault.paths.root)
        loaded = sd.load_draft(vault.paths.root, draft["draft_id"])
        assert loaded["status"] == "expired"
        assert loaded["sources"] == {}
        with pytest.raises(sd.DraftStateError) as ei:
            sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)
        assert ei.value.code == 410
        assert _md_files(vault.paths.root) == before

    def test_tampered_build_hash_is_rejected(self, vault):
        draft = sd.create_draft(vault.paths.root, _payload())
        path = vault.paths.root / draft["draft_path"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["candidates"]["decisions"][0]["text"] = "模型补写的新结论"
        saved["build_hash"] = draft["build_hash"]
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(sd.DraftStateError, match="hash"):
            sd.approve_draft(vault.paths.root, draft["draft_id"], vault=vault)


# ---------------------------------------------------------------- REST 入口
class TestSessionApi:
    def test_preview_does_not_persist(self, client, vault):
        headers = _headers(client)
        response = client.post(
            "/api/session/drafts/preview", json={"payload": _payload()}, headers=headers
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["persisted"] is False
        assert "draft_id" not in data
        assert data["build_hash"].startswith("sha256:")
        assert not (vault.paths.root / ".kwey" / "session" / "drafts").exists()

    def test_submit_detail_and_granted_approve(self, client, vault):
        headers = _headers(client)
        submitted = client.post(
            "/api/session/drafts", json={"payload": _payload()}, headers=headers
        )
        assert submitted.status_code == 200, submitted.text
        draft = submitted.json()["data"]
        draft_id = draft["draft_id"]

        detail = client.get(f"/api/session/drafts/{draft_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()["data"]
        assert body["status"] == "pending_review"
        assert body["candidates"]["constraints"]
        assert body["build_hash"].startswith("sha256:")

        # 没有桌面 grant → 403，且不落笔记
        denied = client.post(
            f"/api/session/drafts/{draft_id}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert denied.status_code == 403
        assert not (vault.paths.root / "notes" / "蒸馏").exists()

        grant = deps.require_duty_supervisor().issue_session_grant(draft_id)
        assert grant
        approved = client.post(
            f"/api/session/drafts/{draft_id}/decision",
            json={
                "decision": "approve",
                "version": body["version"],
                "build_hash": body["build_hash"],
                "approval_grant": grant,
            },
            headers=headers,
        )
        assert approved.status_code == 200, approved.text
        result = approved.json()["data"]
        assert result["status"] == "approved"
        assert len(result["notes"]) == 2

        # 重复确认：终态幂等，不再要求 grant、不重复建笔记
        count = len(_md_files(vault.paths.root))
        again = client.post(
            f"/api/session/drafts/{draft_id}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert again.status_code == 200, again.text
        assert again.json()["data"]["duplicate"] is True
        assert len(_md_files(vault.paths.root)) == count

    def test_wrong_build_hash_is_409(self, client):
        headers = _headers(client)
        draft = client.post(
            "/api/session/drafts", json={"payload": _payload()}, headers=headers
        ).json()["data"]
        grant = deps.require_duty_supervisor().issue_session_grant(draft["draft_id"])
        response = client.post(
            f"/api/session/drafts/{draft['draft_id']}/decision",
            json={
                "decision": "approve",
                "build_hash": "sha256:wrong",
                "approval_grant": grant,
            },
            headers=headers,
        )
        assert response.status_code == 409

    def test_invalid_grant_is_403(self, client):
        headers = _headers(client)
        draft = client.post(
            "/api/session/drafts", json={"payload": _payload()}, headers=headers
        ).json()["data"]
        response = client.post(
            f"/api/session/drafts/{draft['draft_id']}/decision",
            json={"decision": "approve", "approval_grant": "not-a-grant"},
            headers=headers,
        )
        assert response.status_code == 403

    def test_reject_via_api_creates_no_notes(self, client, vault):
        headers = _headers(client)
        draft = client.post(
            "/api/session/drafts", json={"payload": _payload()}, headers=headers
        ).json()["data"]
        before = _md_files(vault.paths.root)
        response = client.post(
            f"/api/session/drafts/{draft['draft_id']}/decision",
            json={"decision": "reject", "reason": "不需要"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "rejected"
        assert _md_files(vault.paths.root) == before
