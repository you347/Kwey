"""M2-3 卡片测试：ops 校验 / diff 预演 / 确认应用 / Provider 缺配置错误。

使用 conftest 的 vault / vault_dir / client fixtures。``client`` 对应的
``create_app()`` 由编排者在 server.py 里接线 include_organize_router；测试期
通过 ``organize_client`` 做运行时挂载（带去重），保证本卡片测试自包含。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.agent.auth import TokenStore
from app.agent.ops import OP_TYPES, apply_ops, preview_ops, validate_ops
from app.api import deps
from app.api.routes_organize import router as organize_router


@pytest.fixture
def organize_client(client, vault_dir):
    """复用 conftest 的 client（create_app 已含统一 {code,data,message} 异常处理器）。

    server.py 的 include_router 由编排者接线；测试期若尚未接线则运行时挂载，
    保证本卡片测试自包含。编排者接线后由去重判断跳过，不产生重复路由。
    """
    paths = {getattr(r, "path", None) for r in client.app.routes}
    if "/api/agent/organize/preview" not in paths:
        client.app.include_router(organize_router)
    # A4：organize 端点需 write token，统一注入供本卡既有断言使用
    client.headers.update({'X-Kwey-Token': TokenStore(vault_dir).issue('write', 'ops-test')['token']})
    return client


class TestValidateOps:
    def test_op_types(self):
        assert OP_TYPES == {"create", "append", "tag", "link", "move", "delete"}

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../secret.md",
            ".kwey/provider.json",
            ".trash/x.md",
            "templates/t.md",
            "notes/../x.md",
        ],
    )
    def test_reject_illegal_path(self, bad_path):
        with pytest.raises(HTTPException) as ei:
            validate_ops([{"type": "append", "path": bad_path, "text": "x"}])
        assert ei.value.status_code == 400
        assert "路径" in str(ei.value.detail)

    def test_reject_unknown_type(self):
        with pytest.raises(HTTPException) as ei:
            validate_ops([{"type": "nuke", "path": "a.md"}])
        assert ei.value.status_code == 400

    def test_reject_missing_required_field(self):
        with pytest.raises(HTTPException) as ei:
            validate_ops([{"type": "append", "path": "a.md"}])  # 缺 text
        assert ei.value.status_code == 400

    def test_accept_legal_and_normalize(self):
        out = validate_ops([{"type": "delete", "path": "notes/读书/a.md"}])
        assert out[0]["path"] == "notes/读书/a.md"


class TestPreview:
    def test_preview_does_not_touch_disk_and_after_correct(self, vault, vault_dir):
        rel = "欢迎.md"
        before_bytes = (vault_dir / rel).read_bytes()
        data = preview_ops(
            [{"type": "append", "path": rel, "text": "新的追加内容"}], vault
        )
        assert data["valid"] is True and data["count"] == 1
        d = data["diffs"][0]
        assert d["op"] == "append" and d["action"] == "modify" and d["path"] == rel
        assert "欢迎" in d["before"]
        assert "新的追加内容" in d["after"] and "新的追加内容" not in d["before"]
        # 磁盘一字未改
        assert (vault_dir / rel).read_bytes() == before_bytes

    def test_preview_all_op_types(self, vault, vault_dir):
        ops = [
            {"type": "create", "path": "新笔记.md", "content": "# 新笔记\n正文"},
            {"type": "append", "path": "欢迎.md", "text": "追加A"},
            {"type": "tag", "path": "欢迎.md", "tag": "整理/已处理"},
            {"type": "link", "path": "欢迎.md", "target": "会议记录"},
            {"type": "move", "path": "欢迎.md", "target": "notes/归档"},
            {"type": "delete", "path": "欢迎.md"},
        ]
        snap = {p: p.read_bytes() for p in vault_dir.rglob("*.md")}
        data = preview_ops(ops, vault)
        assert data["count"] == 6
        by = {d["op"]: d for d in data["diffs"]}
        assert by["create"]["action"] == "create" and by["create"]["before"] is None
        assert "新笔记" in by["create"]["after"]
        assert "追加A" in by["append"]["after"]
        assert "整理/已处理" in by["tag"]["after"]
        assert by["tag"]["after"].startswith("---")  # YAML + 正文
        assert "[[会议记录]]" in by["link"]["after"]
        assert by["move"]["action"] == "move"
        assert by["move"]["target"] == "notes/归档/欢迎.md"
        assert by["delete"]["action"] == "delete"
        assert by["delete"]["target"].startswith(".trash/")
        # 所有 md 文件完全未变
        for p, b in snap.items():
            assert p.read_bytes() == b


class TestApply:
    def test_apply_create_append_tag_link_and_index(self, vault, vault_dir):
        ops = [
            {
                "type": "create",
                "path": "整理/新笔记.md",
                "content": "# 新笔记\n\n整理生成内容",
            },
            {"type": "append", "path": "欢迎.md", "text": "由 Agent 追加"},
            {"type": "tag", "path": "欢迎.md", "tag": "整理/已处理"},
            {"type": "link", "path": "欢迎.md", "target": "会议记录"},
        ]
        result = apply_ops(ops, vault)
        assert result["applied"] == 4
        assert "整理/新笔记.md" in result["paths"]
        assert result["rewritten_links"] == 0
        db = deps.require_db()
        # create 生效 + 索引可查
        assert db.get_note("整理/新笔记.md") is not None
        assert "整理生成内容" in (
            vault_dir / "整理" / "新笔记.md"
        ).read_text(encoding="utf-8")
        # append / tag / link 生效
        doc = vault.read_note("欢迎.md")
        assert "由 Agent 追加" in doc["content"]
        assert "[[会议记录]]" in doc["content"]
        assert "整理/已处理" in doc["frontmatter"]["tags"]
        # 索引可查：tags 表 / links 表
        assert "整理/已处理" in db.tags_of_note("欢迎.md")
        assert "会议记录" in [l["target"] for l in db.links_for("欢迎.md")]

    def test_apply_reports_failing_index(self, vault):
        with pytest.raises(HTTPException) as ei:
            apply_ops(
                [
                    {"type": "append", "path": "欢迎.md", "text": "A"},
                    {"type": "append", "path": "不存在.md", "text": "B"},
                ],
                vault,
            )
        assert "第 2 项" in str(ei.value.detail)

    def test_apply_via_api_requires_confirm(self, organize_client, vault_dir):
        before = (vault_dir / "欢迎.md").read_bytes()
        resp = organize_client.post(
            "/api/agent/organize/apply",
            json={"ops": [{"type": "append", "path": "欢迎.md", "text": "x"}]},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 400
        assert "确认" in body["message"] or "confirm" in body["message"]
        assert (vault_dir / "欢迎.md").read_bytes() == before  # 未写盘

    def test_apply_via_api_with_confirm(self, organize_client, vault):
        resp = organize_client.post(
            "/api/agent/organize/apply",
            json={
                "ops": [
                    {"type": "create", "path": "API新建.md", "content": "# 新建"}
                ],
                "confirm": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0 and body["message"] == "ok"
        assert body["data"]["applied"] == 1
        assert deps.require_db().get_note("API新建.md") is not None


class TestPreviewApi:
    def test_preview_via_api_with_ops(self, organize_client):
        resp = organize_client.post(
            "/api/agent/organize/preview",
            json={"ops": [{"type": "append", "path": "欢迎.md", "text": "预览追加"}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0 and body["message"] == "ok"
        assert body["data"]["count"] == 1
        assert "预览追加" in body["data"]["diffs"][0]["after"]

    def test_preview_without_provider_config_reports_clear_error(
        self, organize_client, vault_dir
    ):
        assert not (vault_dir / ".kwey" / "provider.json").exists()
        resp = organize_client.post(
            "/api/agent/organize/preview",
            json={"instruction": "整理一下笔记"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 400
        assert "provider" in body["message"].lower() or "配置" in body["message"]
