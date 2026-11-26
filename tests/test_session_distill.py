"""P2-C：跨 Agent 会话蒸馏最小链（标准输入 → 六类槽位 → 待审草案）。

覆盖：schema/硬限制校验、服务端 hash 与幂等键、六类槽位与来源锚点、
「环境限制不算证伪」、重复提交幂等、过期清理、工具契约与 CLI 离线预览。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

import kwey_cli
from app.agent import registry
from app.agent.registry import ContractError
from app.agent.session_distill import (
    MAX_EVENT_CHARS,
    MAX_EVENTS,
    create_draft,
    extract,
    preview,
    validate_payload,
)
from app.api import deps


def _payload(**overrides):
    base = {
        "schema_version": 1,
        "source": {
            "agent": "claude-code",
            "session_id": "session-1",
            "export_hash": "sha256:client-declared",
        },
        "goal": "完成 C 第一批",
        "retention": "selected_evidence",
        "events": [
            {"id": "e1", "kind": "user", "text": "只改一个文件，不要推送。", "ts": "2026-11-24T10:00:00+08:00"},
            {"id": "e2", "kind": "assistant", "text": "决定采用标准 JSON 输入。", "ts": "2026-11-24T10:01:00+08:00"},
            {"id": "e3", "kind": "tool_result", "text": "测试失败：模块导入报错。", "ts": "2026-11-24T10:02:00+08:00"},
            {"id": "e4", "kind": "assistant", "text": "TODO 下一步补 hook 样本。", "ts": "2026-11-24T10:03:00+08:00"},
            {"id": "e5", "kind": "user", "text": "发现重名笔记必须写完整路径。", "ts": "2026-11-24T10:04:00+08:00"},
            {"id": "e6", "kind": "assistant", "text": "达到轮次上限，环境不可用，无法继续。", "ts": "2026-11-24T10:05:00+08:00"},
            {"id": "e7", "kind": "assistant", "text": "必须不要修改外部配置。", "ts": "2026-11-24T10:06:00+08:00"},
            {"id": "e8", "kind": "user", "text": "记住我偏好本地优先。", "ts": "2026-11-24T10:07:00+08:00"},
            {"id": "e9", "kind": "assistant", "text": "", "ts": "2026-11-24T10:08:00+08:00"},
        ],
    }
    base.update(overrides)
    return base


def _md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md"))


# ---------------------------------------------------------------- 标准输入校验
class TestValidation:
    def test_non_object(self):
        with pytest.raises(ValueError):
            validate_payload(["not", "object"])

    def test_schema_version_must_be_1(self):
        with pytest.raises(ValueError, match="schema_version"):
            validate_payload(_payload(schema_version=2))
        with pytest.raises(ValueError, match="schema_version"):
            validate_payload(_payload(schema_version=True))

    def test_source_required_fields(self):
        with pytest.raises(ValueError, match="source"):
            validate_payload(_payload(source=None))
        with pytest.raises(ValueError, match="source.agent"):
            validate_payload(_payload(source={"agent": "", "session_id": "s"}))
        with pytest.raises(ValueError, match="source.session_id"):
            validate_payload(_payload(source={"agent": "a", "session_id": " "}))

    def test_events_required_non_empty(self):
        with pytest.raises(ValueError, match="events"):
            validate_payload(_payload(events=[]))
        with pytest.raises(ValueError, match="events"):
            validate_payload(_payload(events=None))

    def test_event_id_unique(self):
        dup = [
            {"id": "x", "kind": "user", "text": "第一条"},
            {"id": "x", "kind": "user", "text": "第二条"},
        ]
        with pytest.raises(ValueError, match="重复"):
            validate_payload(_payload(events=dup))

    def test_event_kind_enum(self):
        with pytest.raises(ValueError, match="kind"):
            validate_payload(
                _payload(events=[{"id": "e1", "kind": "prompt", "text": "x"}])
            )

    def test_event_text_limit(self):
        with pytest.raises(ValueError, match="超长"):
            validate_payload(
                _payload(events=[{"id": "e1", "kind": "user", "text": "x" * (MAX_EVENT_CHARS + 1)}])
            )

    def test_events_count_limit(self):
        events = [{"id": f"e{i}", "kind": "user", "text": "x"} for i in range(MAX_EVENTS + 1)]
        with pytest.raises(ValueError, match="events 超限"):
            validate_payload(_payload(events=events))

    def test_retention_only_selected_evidence(self):
        with pytest.raises(ValueError, match="retention"):
            validate_payload(_payload(retention="full_log"))


# ---------------------------------------------------------------- hash / 幂等键
class TestHashAndKey:
    def test_server_computes_hash_not_client(self):
        doc = validate_payload(_payload())
        assert doc["export_hash"].startswith("sha256:")
        assert doc["export_hash"] != "sha256:client-declared"
        assert doc["client_export_hash"] == "sha256:client-declared"

    def test_same_content_same_draft_even_if_client_hash_changes(self, vault):
        a = create_draft(vault.paths.root, _payload(source={"agent": "claude-code", "session_id": "s", "export_hash": "sha256:a"}))
        b = create_draft(vault.paths.root, _payload(source={"agent": "claude-code", "session_id": "s", "export_hash": "sha256:b"}))
        assert a["draft_id"] == b["draft_id"]
        assert b["duplicate"] is True

    def test_changed_content_new_draft(self, vault):
        a = create_draft(vault.paths.root, _payload())
        changed = _payload()
        changed["events"][0]["text"] = "改主意了：允许推送。"
        b = create_draft(vault.paths.root, changed)
        assert a["draft_id"] != b["draft_id"]
        assert b["duplicate"] is False


# ---------------------------------------------------------------- 六类槽位与来源
class TestExtraction:
    @pytest.fixture
    def result(self):
        return preview(_payload())

    def test_all_six_slots_present(self, result):
        assert set(result["candidates"]) == {
            "decisions", "constraints", "refutations",
            "open_items", "knowledge_candidates",
        }
        assert "omissions" in result

    def test_every_candidate_has_source_anchor(self, result):
        event_ids = {e["id"] for e in _payload()["events"]}
        total = 0
        for items in result["candidates"].values():
            for item in items:
                total += 1
                ids = item["source_event_ids"]
                assert ids and set(ids) <= event_ids
                assert item["text"].strip() and item["quote"].strip()
        assert total > 0

    def test_constraints_only_from_user_events(self, result):
        kinds = {e["id"]: e["kind"] for e in _payload()["events"]}
        constraints = result["candidates"]["constraints"]
        assert constraints, "用户约束应被提取"
        for item in constraints:
            assert all(kinds[eid] == "user" for eid in item["source_event_ids"])
            assert item["scope"] == "user_explicit"

    def test_environment_limit_is_not_refutation(self, result):
        refutation_ids = {
            eid
            for item in result["candidates"]["refutations"]
            for eid in item["source_event_ids"]
        }
        assert "e3" in refutation_ids  # 真实失败有工具证据
        assert "e6" not in refutation_ids  # 轮次上限/环境不可用不算证伪
        reasons = {o["reason"] for o in result["omissions"]}
        assert "environment_or_round_limit_not_refutation" in reasons

    def test_open_item_and_knowledge_metadata(self, result):
        opens = result["candidates"]["open_items"]
        assert opens and any("TODO" in c["text"] for c in opens)
        knowledge = result["candidates"]["knowledge_candidates"]
        pref = [c for c in knowledge if "偏好" in c["text"]]
        assert pref and pref[0]["requires_user_confirmation"] is True
        assert all(c["confidence_type"] == "rule_candidate" for c in knowledge)

    def test_empty_text_event_recorded_as_omission(self, result):
        assert any(
            o["event_id"] == "e9" and o["reason"] == "empty_text"
            for o in result["omissions"]
        )

    def test_sources_only_referenced_events(self, result):
        assert result["sources"], "至少有一条事实的来源"
        assert set(result["sources"]) <= {e["id"] for e in _payload()["events"]}
        assert all(s["quote"].strip() for s in result["sources"].values())


class TestSentenceSplitting:
    def test_dot_inside_path_is_not_a_sentence_boundary(self):
        from app.agent.session_distill import _sentences

        parts = _sentences("不要动 .kwey 目录，也不要改写完历史。")
        assert parts == ["不要动 .kwey 目录，也不要改写完历史。"]

    def test_ascii_break_requires_trailing_space(self):
        from app.agent.session_distill import _sentences

        assert _sentences("Use v1.2. Then push.") == ["Use v1.2.", "Then push."]

    def test_cjk_terminators_split_without_space(self):
        from app.agent.session_distill import _sentences

        assert _sentences("先定方案。再补测试。") == ["先定方案。", "再补测试。"]

    def test_no_space_english_sentences_are_split(self):
        from app.agent.session_distill import _sentences

        assert _sentences("First.Second") == ["First.", "Second"]

    def test_version_and_decimal_are_not_split(self):
        from app.agent.session_distill import _sentences

        assert _sentences("升级到 v1.2 后正常") == ["升级到 v1.2 后正常"]
        assert _sentences("大概 3.14 左右") == ["大概 3.14 左右"]

    def test_paths_files_and_domains_are_not_split(self):
        from app.agent.session_distill import _sentences

        assert _sentences("不要动 .kwey 目录") == ["不要动 .kwey 目录"]
        assert _sentences("改 config.yml 就行") == ["改 config.yml 就行"]
        assert _sentences("访问 a.com 首页") == ["访问 a.com 首页"]

    def test_abbreviations_do_not_end_sentence(self):
        from app.agent.session_distill import _sentences

        assert _sentences("Use e.g. this style.") == ["Use e.g. this style."]
        assert _sentences("比如 i.e. 换个说法") == ["比如 i.e. 换个说法"]

    def test_ellipsis_and_combined_terminators(self):
        from app.agent.session_distill import _sentences

        assert _sentences("Done... Next.") == ["Done...", "Next."]
        assert _sentences("真的吗？！继续处理。") == ["真的吗？！", "继续处理。"]

    def test_no_evidence_candidates_are_dropped(self):
        doc = validate_payload(_payload())
        extracted = extract(doc)
        assert extracted["stats"]["dropped_no_evidence"] == 0
        for items in extracted["candidates"].values():
            for item in items:
                assert item["source_event_ids"]
                assert item["quote"].strip()


# ---------------------------------------------------------------- 持久草案
class TestDraft:
    def test_creates_pending_draft_without_notes(self, vault):
        before = _md_files(vault.paths.root)
        res = create_draft(vault.paths.root, _payload())
        assert res["status"] == "pending_review"
        assert res["duplicate"] is False
        assert res["draft_id"].startswith("draft_")
        assert res["draft_path"].startswith(".kwey/session/drafts/")
        draft_file = vault.paths.root / res["draft_path"]
        assert draft_file.is_file()
        saved = json.loads(draft_file.read_text(encoding="utf-8"))
        assert saved["status"] == "pending_review"
        assert saved["idempotency_key"] == res["idempotency_key"]
        # 只写控制文件：普通笔记数量不变
        assert _md_files(vault.paths.root) == before

    def test_duplicate_submission_returns_existing(self, vault):
        first = create_draft(vault.paths.root, _payload())
        second = create_draft(vault.paths.root, _payload())
        assert second["draft_id"] == first["draft_id"]
        assert second["duplicate"] is True
        drafts = list((vault.paths.root / ".kwey" / "session" / "drafts").glob("*.json"))
        assert len(drafts) == 1

    def test_expired_draft_is_cleaned_not_faked(self, vault):
        first = create_draft(vault.paths.root, _payload())
        draft_file = vault.paths.root / first["draft_path"]
        saved = json.loads(draft_file.read_text(encoding="utf-8"))
        saved["created_at"] = "2020-01-01T00:00:00+08:00"
        draft_file.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

        again = create_draft(vault.paths.root, _payload())
        assert again["status"] == "expired"
        assert again["draft_id"] == first["draft_id"]
        assert all(not items for items in again["candidates"].values())
        assert again["sources"] == {}
        assert again["stats"]["purged"] is True


# ---------------------------------------------------------------- 契约注册
class TestRegistryContract:
    def test_tool_metadata(self):
        tool = registry.get_tool("kwey.session.distill")
        assert tool["permission"] == "write"
        assert tool["side_effect"] == "reversible"
        assert tool["journal_action"] == "create"
        assert tool["input_schema"]["required"] == ["payload"]

    def test_invoke_creates_draft_and_is_idempotent(self, vault):
        before = _md_files(vault.paths.root)
        args = {"payload": _payload()}
        first = registry.invoke("kwey.session.distill", args, vault=vault)
        second = registry.invoke("kwey.session.distill", args, vault=vault)
        assert first["draft_id"] == second["draft_id"]
        assert second["duplicate"] is True
        assert _md_files(vault.paths.root) == before

    def test_invalid_payload_raises_400(self, vault):
        with pytest.raises(ContractError) as ei:
            registry.invoke(
                "kwey.session.distill",
                {"payload": {"schema_version": 99, "source": {}, "events": []}},
                vault=vault,
            )
        assert ei.value.code == 400
        assert "校验失败" in ei.value.message


# ---------------------------------------------------------------- CLI 离线预览
class TestCliPreview:
    @pytest.fixture
    def cli_vault(self, vault_dir):
        yield vault_dir
        deps.reset_services()

    def _run(self, cli_vault, monkeypatch, capsys, argv, stdin_text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        code = kwey_cli.main(["--vault", str(cli_vault), *argv])
        out = capsys.readouterr()
        return code, out

    def test_preview_json_no_persistence(self, cli_vault, monkeypatch, capsys):
        code, out = self._run(
            cli_vault, monkeypatch, capsys,
            ["session", "distill", "--stdin", "--preview", "--json"],
            json.dumps(_payload(), ensure_ascii=False),
        )
        assert code == 0
        envelope = json.loads(out.out)
        data = envelope["data"]
        assert envelope["code"] == 0
        assert data["persisted"] is False
        assert "draft_id" not in data
        assert data["preview_hash"].startswith("sha256:")
        assert data["candidates"] and data["stats"]["events"] == 9
        # 离线预览不建索引、不写控制文件、不落笔记
        assert not (cli_vault / ".kwey").exists()
        assert len(_md_files(cli_vault)) == 3

    def test_preview_requires_flag(self, cli_vault, monkeypatch, capsys):
        code, out = self._run(
            cli_vault, monkeypatch, capsys,
            ["session", "distill", "--stdin", "--json"],
            json.dumps(_payload(), ensure_ascii=False),
        )
        assert code == 2
        assert "--preview" in out.err

    def test_submit_mode_not_available(self, cli_vault, monkeypatch, capsys):
        code, out = self._run(
            cli_vault, monkeypatch, capsys,
            ["session", "distill", "--stdin", "--preview", "--submit", "--json"],
            json.dumps(_payload(), ensure_ascii=False),
        )
        assert code == 2
        assert "暂未提供" in out.err

    def test_invalid_json_exit_2(self, cli_vault, monkeypatch, capsys):
        code, out = self._run(
            cli_vault, monkeypatch, capsys,
            ["session", "distill", "--stdin", "--preview", "--json"],
            "{not json",
        )
        assert code == 2
        assert "JSON" in out.err

    def test_invalid_schema_exit_1(self, cli_vault, monkeypatch, capsys):
        bad = {"schema_version": 1, "source": {"agent": "a", "session_id": "s"}, "events": []}
        code, out = self._run(
            cli_vault, monkeypatch, capsys,
            ["session", "distill", "--stdin", "--preview", "--json"],
            json.dumps(bad),
        )
        assert code == 1
        assert "校验失败" in out.err
