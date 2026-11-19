"""A6 / F1 知识编译层测试：proposal-first、raw 只读、wiki 落盘、体检。

本卡测试自包含：server.py 的路由注册由集成卡完成；测试期若尚未注册则运行时挂载。
Provider 用 fake 覆盖依赖注入，不访问网络，也不需要真实 Agent 配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.agent.compiler import compile_vault, health_check
from app.api.routes_compile import get_compile_provider, router as compile_router


class FakeProvider:
    """返回固定 ops 的 Provider；记录调用参数，便于断言上下文收敛到 raw/。"""

    def __init__(self, ops: List[Dict[str, Any]]) -> None:
        self.ops = ops
        self.calls: List[tuple] = []

    def run(self, instruction: str, paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        self.calls.append((instruction, list(paths or [])))
        return [dict(op) for op in self.ops]


def base_ops() -> List[Dict[str, Any]]:
    return [
        {
            "type": "create",
            "path": "wiki/entities/叶俊豪.md",
            "content": "# 叶俊豪\n\n实体页正文",
            "kind": "entity",
        },
        {
            "type": "create",
            "path": "wiki/concepts/知识编译.md",
            "content": "# 知识编译\n\n概念页正文",
            "kind": "concept",
        },
        # 指向同批次新建页的关系链接：应被内联进 create.content，而非单独 op
        {
            "type": "link",
            "path": "wiki/entities/叶俊豪.md",
            "target": "wiki/concepts/知识编译",
            "alias": "知识编译",
        },
    ]


@pytest.fixture
def compile_client(client):
    """复用统一 {code,data,message} 包装的 app，按需挂载路由并清理依赖覆盖。"""
    paths = {getattr(route, "path", None) for route in client.app.routes}
    if "/api/compile/preview" not in paths:
        client.app.include_router(compile_router)
    yield client
    client.app.dependency_overrides.pop(get_compile_provider, None)


def snapshot(root: Path) -> Dict[str, bytes]:
    return {p.as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestCompileVault:
    def test_preview_is_proposal_only_and_builds_moc(self, vault, vault_dir):
        (vault_dir / "raw").mkdir()
        (vault_dir / "raw" / "素材.md").write_text("# 原始素材\n不要改我", encoding="utf-8")
        before = snapshot(vault_dir)
        provider = FakeProvider(base_ops())

        result = compile_vault(vault, provider)

        assert snapshot(vault_dir) == before  # 默认 dry_run：磁盘零改动
        assert result["report"]["raw_notes"] == 1
        assert result["report"]["dry_run"] is True
        assert provider.calls[0][1] == ["raw/素材.md"]  # 上下文只含 raw/
        ops = result["ops"]
        assert len(result["diffs"]) == len(ops)
        assert all(diff["before"] is None for diff in result["diffs"])  # 全是新建页

        moc = [op for op in ops if op["path"] == "wiki/MOC.md"]
        assert len(moc) == 1 and moc[0]["type"] == "create"
        assert "[[wiki/entities/叶俊豪|叶俊豪]]" in moc[0]["content"]
        assert "[[wiki/concepts/知识编译|知识编译]]" in moc[0]["content"]
        assert result["report"]["entity_pages"] == ["叶俊豪"]
        assert result["report"]["concept_pages"] == ["知识编译"]

    def test_relations_to_new_pages_are_inlined(self, vault, vault_dir):
        result = compile_vault(vault, FakeProvider(base_ops()))
        entity = next(op for op in result["ops"] if op["path"] == "wiki/entities/叶俊豪.md")
        assert "## 关联" in entity["content"]
        assert "[[wiki/concepts/知识编译|知识编译]]" in entity["content"]
        assert not [op for op in result["ops"] if op["type"] == "link"]  # 已内联

    def test_apply_writes_only_wiki(self, vault, vault_dir):
        (vault_dir / "raw").mkdir()
        source = vault_dir / "raw" / "素材.md"
        source.write_text("# 原始素材", encoding="utf-8")
        raw_before = source.read_bytes()

        result = compile_vault(vault, FakeProvider(base_ops()), dry_run=False)

        assert result["report"]["applied"]["applied"] == 3
        assert (vault_dir / "wiki" / "entities" / "叶俊豪.md").is_file()
        assert (vault_dir / "wiki" / "concepts" / "知识编译.md").is_file()
        assert (vault_dir / "wiki" / "MOC.md").is_file()
        assert source.read_bytes() == raw_before
        assert not list((vault_dir / "raw").glob("wiki*"))  # 产物没有落到 raw/

        report = health_check(vault)
        assert not [x for x in report["missing_moc"] if x["reason"] == "未找到 MOC 索引页"]

    @pytest.mark.parametrize(
        "bad_path,keyword",
        [("raw/禁止写.md", "只读"), ("notes/普通笔记.md", "wiki")],
    )
    def test_reject_raw_write_and_outside_wiki(self, vault, bad_path, keyword, vault_dir):
        (vault_dir / "raw").mkdir()
        with pytest.raises(HTTPException) as exc:
            compile_vault(
                vault,
                FakeProvider([{"type": "create", "path": bad_path, "content": "# 坏 op"}]),
            )
        assert exc.value.status_code == 400
        assert keyword in str(exc.value.detail)


class TestHealthCheck:
    def test_detects_orphan_broken_link_schema_drift_and_missing_moc(self, vault, vault_dir):
        (vault_dir / "notes" / "工作" / "断链.md").write_text(
            "# 断链\n[[不存在的目标]]", encoding="utf-8"
        )
        (vault_dir / "notes" / "工作" / "孤立.md").write_text(
            "# 孤立\n没有任何双链", encoding="utf-8"
        )
        report = health_check(vault)

        broken = {(x["source"], x["target"]) for x in report["broken_links"]}
        assert ("notes/工作/断链.md", "不存在的目标") in broken
        orphans = {x["path"] for x in report["orphans"]}
        assert "notes/工作/孤立.md" in orphans
        assert "notes/工作/会议记录.md" in orphans  # conftest 中原本就无链
        assert "欢迎.md" not in orphans  # 与 2026计划 互链
        drift = {x["path"] for x in report["schema_drift"]}
        assert "notes/工作/会议记录.md" in drift
        assert any(x["reason"] == "未找到 MOC 索引页" for x in report["missing_moc"])


class TestCompileAPI:
    def test_preview_then_apply_and_health(self, compile_client, vault_dir):
        fake = FakeProvider(base_ops())
        compile_client.app.dependency_overrides[get_compile_provider] = lambda: fake
        before = snapshot(vault_dir)

        res = compile_client.post("/api/compile/preview", json={"instruction": "编译"})
        assert res.status_code == 200 and res.json()["code"] == 0
        data = res.json()["data"]
        assert len(data["ops"]) == len(data["diffs"]) == 3
        assert snapshot(vault_dir) == before  # preview 零落盘

        bad = compile_client.post("/api/compile/apply", json={"ops": data["ops"]})
        assert bad.status_code == 400 and "确认" in bad.json()["message"]

        applied = compile_client.post(
            "/api/compile/apply", json={"ops": data["ops"], "confirm": True}
        )
        assert applied.status_code == 200
        assert applied.json()["data"]["applied"] == 3
        assert (vault_dir / "wiki" / "MOC.md").is_file()

        health = compile_client.get("/api/compile/health")
        assert health.status_code == 200
        assert set(health.json()["data"].keys()) == {
            "orphans", "broken_links", "schema_drift", "missing_moc"
        }

    def test_provider_not_configured_returns_clear_400(self, compile_client):
        res = compile_client.post("/api/compile/preview", json={})
        assert res.status_code == 400
        assert "未配置 Agent Provider" in res.json()["message"]

    def test_apply_rejects_raw_write(self, compile_client):
        res = compile_client.post(
            "/api/compile/apply",
            json={
                "ops": [{"type": "create", "path": "raw/evil.md", "content": "# 坏"}],
                "confirm": True,
            },
        )
        assert res.status_code == 400 and "只读" in res.json()["message"]
