"""B0 接线验证：service/registry/kernel/ops/watcher/路由 的 actor 与溯源贯通。

全部使用 conftest 临时 vault，不接触运行实例、不启动真实 watcher 线程。
"""

from __future__ import annotations

from watchfiles import Change

from app.agent import kernel, registry
from app.agent.ops import apply_ops
from app.agent.provider import AgentProvider
from app.vault.provenance import (
    FIELD_REVIEWED_HASH,
    LOCAL_UI_ACTOR,
    ActorContext,
    get_current_actor,
    payload_hash,
    use_actor,
)


def _agent(name: str = "mcp-stdio", **kw) -> ActorContext:
    return ActorContext(kind="agent", name=name, origin="test", **kw)


class TestServiceWrites:
    def test_ui_write_marks_local_ui_and_records_baseline(self, client, vault):
        resp = client.put(
            "/api/note",
            json={"path": "欢迎.md", "content": "# 欢迎\n\nUI 保存\n"},
        )
        assert resp.status_code == 200
        doc = vault.read_note("欢迎.md")
        assert doc["frontmatter"]["kwey_writer_kind"] == "human"
        assert doc["frontmatter"]["kwey_last_writer"] == "local-ui"
        assert doc["frontmatter"]["kwey_hash_version"] == 1

        info = vault.provenance_info("欢迎.md")
        assert info["baseline"]["writer_kind"] == "human"
        assert info["baseline"]["last_writer"] == "local-ui"
        assert info["state"] == "human_or_system_current"

    def test_generated_stays_sticky_after_human_edit(self, vault):
        vault.create_note("笔记/a.md", "Agent 正文\n", actor=_agent())
        assert vault.provenance_info("笔记/a.md")["state"] == "agent_unreviewed"

        vault.write_note("笔记/a.md", "人工小改\n", actor=LOCAL_UI_ACTOR)
        info = vault.provenance_info("笔记/a.md")
        assert info["baseline"]["writer_kind"] == "human"
        assert info["baseline"]["generated"] is True
        assert info["state"] == "agent_unreviewed"  # 未洗成 human/已审


class TestRegistryContext:
    def test_invoke_binds_agent_actor_and_restores(self, vault):
        registry.invoke(
            "kwey.create",
            {"path": "agent.md", "content": "agent"},
            vault=vault,
            actor={"role": "write", "name": "verbot", "run_id": "r-1"},
        )
        assert get_current_actor() is None  # 调用后恢复
        info = vault.provenance_info("agent.md")
        assert info["baseline"]["writer_kind"] == "agent"
        assert info["baseline"]["last_writer"] == "verbot"
        assert info["baseline"]["run_id"] == "r-1"
        assert info["declared"]["run_id"] == "r-1"

    def test_exception_path_still_restores_outer_actor(self, vault):
        outer = ActorContext(kind="system", name="outer")
        with use_actor(outer):
            try:
                registry.invoke(
                    "kwey.read",
                    {"path": "不存在.md"},
                    vault=vault,
                    actor={"role": "read", "name": "reader"},
                )
            except Exception:  # noqa: BLE001
                pass
            assert get_current_actor() is outer
        assert get_current_actor() is None

    def test_client_claimed_human_is_not_trusted(self, vault):
        registry.invoke(
            "kwey.create",
            {"path": "spoof.md", "content": "x"},
            vault=vault,
            actor={"role": "write", "name": "spoof", "kind": "human"},
        )
        info = vault.provenance_info("spoof.md")
        assert info["baseline"]["writer_kind"] == "agent"


class TestOpsAndKernel:
    def test_apply_ops_passes_actor_and_run_id(self, vault):
        apply_ops(
            [{"type": "create", "path": "ops/batch.md", "content": "x"}],
            vault,
            actor=_agent("duty:inbox"),
            run_id="run-9",
        )
        info = vault.provenance_info("ops/batch.md")
        assert info["baseline"]["writer_kind"] == "agent"
        assert info["baseline"]["last_writer"] == "duty:inbox"
        assert info["baseline"]["run_id"] == "run-9"
        assert info["declared"]["run_id"] == "run-9"

    def test_kernel_passes_run_id_to_tool_writes(self, vault, monkeypatch):
        responses = iter([
            {
                "content": None,
                "tool_calls": [{
                    "id": "t1",
                    "name": "kwey_create",
                    "arguments": {"path": "kernel/batch.md", "content": "x"},
                }],
            },
            {"content": "done", "tool_calls": []},
        ])
        monkeypatch.setattr(
            AgentProvider, "chat", lambda self, messages, tools: next(responses)
        )
        list(
            kernel.run_chat(
                [{"role": "user", "content": "hi"}], vault=vault, run_id="duty:test"
            )
        )
        info = vault.provenance_info("kernel/batch.md")
        assert info["baseline"]["run_id"] == "duty:test"
        assert info["baseline"]["last_writer"] == "kwey-built-in"


class TestProvenanceRoutes:
    def test_read_then_review_then_stale_409(self, client, vault):
        registry.invoke(
            "kwey.create",
            {"path": "ai.md", "content": "# AI\n\n内容\n"},
            vault=vault,
            actor={"role": "write", "name": "mcp-stdio"},
        )
        data = client.get("/api/provenance/note", params={"path": "ai.md"}).json()["data"]
        assert data["state"] == "agent_unreviewed"
        expected = data["current_payload_hash"]

        resp = client.post(
            "/api/provenance/review",
            json={"path": "ai.md", "expected_payload_hash": expected},
        )
        assert resp.status_code == 200
        reviewed = resp.json()["data"]
        assert reviewed["state"] == "agent_reviewed_current"
        assert reviewed["baseline"]["reviewed_hash"] == expected

        doc = vault.read_note("ai.md")
        assert doc["frontmatter"][FIELD_REVIEWED_HASH] == expected

        # 人工改内容 → 旧审阅自动失效
        client.put(
            "/api/note",
            json={
                "path": "ai.md",
                "content": "# AI\n\n人工修改\n",
                "frontmatter": doc["frontmatter"],
            },
        )
        assert vault.provenance_info("ai.md")["state"] == "agent_unreviewed"

        stale = client.post(
            "/api/provenance/review",
            json={"path": "ai.md", "expected_payload_hash": expected},
        )
        assert stale.status_code == 409

    def test_review_without_baseline_is_409(self, client, vault):
        info = client.get(
            "/api/provenance/note", params={"path": "欢迎.md"}
        ).json()["data"]
        assert info["baseline"] is None
        resp = client.post(
            "/api/provenance/review",
            json={
                "path": "欢迎.md",
                "expected_payload_hash": info["current_payload_hash"],
            },
        )
        assert resp.status_code == 409

    def test_review_preserves_agent_source_fields(self, client, vault):
        registry.invoke(
            "kwey.create",
            {"path": "来源保持.md", "content": "# AI\n\n生成内容\n"},
            vault=vault,
            actor={"role": "write", "name": "mcp-stdio", "run_id": "run-7"},
        )
        before = vault.read_note("来源保持.md")["frontmatter"]
        assert before["kwey_writer_kind"] == "agent"
        assert before["kwey_last_writer"] == "mcp-stdio"
        assert before["kwey_run_id"] == "run-7"
        assert before["kwey_generated"] is True

        info = client.get(
            "/api/provenance/note", params={"path": "来源保持.md"}
        ).json()["data"]
        expected = info["current_payload_hash"]
        resp = client.post(
            "/api/provenance/review",
            json={"path": "来源保持.md", "expected_payload_hash": expected},
        )
        assert resp.status_code == 200
        reviewed = resp.json()["data"]
        assert reviewed["state"] == "agent_reviewed_current"
        assert reviewed["baseline"]["writer_kind"] == "agent"
        assert reviewed["baseline"]["last_writer"] == "mcp-stdio"
        assert reviewed["baseline"]["run_id"] == "run-7"
        assert reviewed["baseline"]["generated"] is True
        assert reviewed["baseline"]["reviewed_hash"] == expected

        after = vault.read_note("来源保持.md")["frontmatter"]
        assert after["kwey_writer_kind"] == "agent"
        assert after["kwey_last_writer"] == "mcp-stdio"
        assert after["kwey_run_id"] == "run-7"
        assert after["kwey_generated"] is True
        assert after[FIELD_REVIEWED_HASH] == expected

    def test_human_edit_after_review_updates_writer(self, client, vault):
        registry.invoke(
            "kwey.create",
            {"path": "人工接手.md", "content": "# AI\n\n生成内容\n"},
            vault=vault,
            actor={"role": "write", "name": "mcp-stdio", "run_id": "run-8"},
        )
        info = client.get(
            "/api/provenance/note", params={"path": "人工接手.md"}
        ).json()["data"]
        client.post(
            "/api/provenance/review",
            json={
                "path": "人工接手.md",
                "expected_payload_hash": info["current_payload_hash"],
            },
        )

        doc = vault.read_note("人工接手.md")
        resp = client.put(
            "/api/note",
            json={
                "path": "人工接手.md",
                "content": "# AI\n\n人工小改\n",
                "frontmatter": doc["frontmatter"],
            },
        )
        assert resp.status_code == 200

        after = vault.read_note("人工接手.md")["frontmatter"]
        assert after["kwey_writer_kind"] == "human"
        assert after["kwey_last_writer"] == "local-ui"
        assert "kwey_run_id" not in after
        assert after["kwey_generated"] is True  # Agent 派生标记保持粘性

        baseline = vault.provenance_info("人工接手.md")["baseline"]
        assert baseline["writer_kind"] == "human"
        assert baseline["last_writer"] == "local-ui"
        assert baseline["run_id"] is None
        assert baseline["generated"] is True
        assert vault.provenance_info("人工接手.md")["state"] == "agent_unreviewed"


class TestWatcherProvenance:
    def test_external_change_marks_state_without_rewriting(self, vault, vault_dir):
        registry.invoke(
            "kwey.create",
            {"path": "外部.md", "content": "原文\n"},
            vault=vault,
            actor={"role": "write", "name": "mcp-stdio"},
        )
        before = vault.provenance.get_baseline("外部.md")
        target = vault_dir / "外部.md"
        external_text = "---\ntitle: 外部改\n---\n\n外部追加\n"
        target.write_text(external_text, encoding="utf-8")

        vault._watcher._process_changes({target: Change.modified})

        assert target.read_text(encoding="utf-8") == external_text  # 不重写外部文件
        info = vault.provenance_info("外部.md")
        assert info["state"] == "external_changed"
        assert info["baseline"]["raw_hash"] == before["raw_hash"]  # 基线不被覆盖
        assert vault.db.get_note("外部.md") is not None  # 索引仍更新

    def test_self_write_not_reported_as_external(self, vault, vault_dir):
        vault.create_note("自写.md", "内容\n", actor=LOCAL_UI_ACTOR)
        target = vault_dir / "自写.md"
        vault._watcher._process_changes({target: Change.modified})
        assert vault.provenance_info("自写.md")["state"] == "human_or_system_current"

    def test_link_rewrite_records_system_and_keeps_generated(self, vault):
        registry.invoke(
            "kwey.create",
            {"path": "links/a.md", "content": "# A\n"},
            vault=vault,
            actor={"role": "write", "name": "agent-a", "run_id": "run-a"},
        )
        registry.invoke(
            "kwey.create",
            {"path": "links/b.md", "content": "# B\n\n[[a]]\n"},
            vault=vault,
            actor={"role": "write", "name": "agent-a", "run_id": "run-a"},
        )
        registry.invoke(
            "kwey.move",
            {"path": "links/a.md", "target": "moved"},
            vault=vault,
            actor={"role": "write", "name": "agent-a", "run_id": "run-a"},
        )
        info = vault.provenance_info("links/b.md")
        assert info["baseline"]["writer_kind"] == "system"
        assert info["baseline"]["run_id"] == "run-a"
        assert info["baseline"]["generated"] is True
        assert info["state"] == "agent_unreviewed"  # 不是 external_changed


def test_payload_hash_ignores_managed_fields(vault):
    vault.create_note("hash.md", "正文\n", actor=_agent())
    raw = vault.paths.to_abs("hash.md").read_text(encoding="utf-8-sig")
    before = payload_hash(raw)
    vault.write_note("hash.md", "正文\n", actor=LOCAL_UI_ACTOR)
    after_raw = vault.paths.to_abs("hash.md").read_text(encoding="utf-8-sig")
    assert payload_hash(after_raw) == before
