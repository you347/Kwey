"""卡A W4：视图 / 体检 / 快速捕获三组新工具的行为测试。

不复述引擎细节（见 tests/test_bases.py / test_workspace.py），只钉住
「智能体从契约进去能不能真的拿到 / 写对」这条链路。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import journal, registry
from app.agent.registry import ContractError

VIEW_DEF = {
    "name": "读书清单",
    "folder": "notes",
    "tags": ["读书/2026"],
    "columns": ["title", "tags"],
}


# ---------------------------------------------------------------- views
def test_views_list_empty(vault):
    assert registry.invoke("kwey.views.list", {}, vault=vault)["total"] == 0


def test_views_save_then_list_and_read(vault):
    saved = registry.invoke("kwey.views.save", {"definition": VIEW_DEF}, vault=vault)
    assert saved["slug"]
    assert (vault.paths.root / saved["path"]).is_file()

    items = registry.invoke("kwey.views.list", {}, vault=vault)["items"]
    assert [i["slug"] for i in items] == [saved["slug"]]
    assert items[0]["definition"]["folder"] == "notes"
    assert items[0]["name"] == "读书清单"


def test_views_save_requires_name(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.views.save", {"definition": {"folder": "notes"}}, vault=vault)
    assert ei.value.code == 400


def test_views_save_unknown_slug_404(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke(
            "kwey.views.save", {"definition": VIEW_DEF, "slug": "nope"}, vault=vault
        )
    assert ei.value.code == 404


def test_views_query_inline_definition(vault):
    res = registry.invoke("kwey.views.query", {"definition": VIEW_DEF}, vault=vault)
    paths = [row["path"] for row in res["rows"]]
    assert "notes/读书/2026计划.md" in paths
    assert "欢迎.md" not in paths  # folder=notes 把根目录排掉
    assert res["total"] == len(paths)
    assert res["definition"]["name"] == "读书清单"


def test_views_query_by_slug(vault):
    slug = registry.invoke("kwey.views.save", {"definition": VIEW_DEF}, vault=vault)["slug"]
    res = registry.invoke("kwey.views.query", {"slug": slug}, vault=vault)
    assert res["total"] >= 1
    assert "columns" in res


def test_views_query_needs_slug_or_definition(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.views.query", {}, vault=vault)
    assert ei.value.code == 400


def test_views_query_unknown_slug_404(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.views.query", {"slug": "ghost"}, vault=vault)
    assert ei.value.code == 404


# ---------------------------------------------------------------- diagnose
def test_diagnose_shape(vault):
    res = registry.invoke("kwey.diagnose", {"deep": False}, vault=vault)
    assert 0 <= res["score"] <= 100
    assert res["grade"]
    assert isinstance(res["issues"], list)
    keys = {issue["key"] for issue in res["issues"]}
    assert "broken" in keys  # 临时库里有指向 pic.png 之外的嵌入？至少有断链统计位
    for issue in res["issues"]:
        assert set(issue) >= {"key", "label", "severity", "count", "items"}


def test_diagnose_deep_finds_duplicates(vault, vault_dir: Path):
    body = "这是一段足够长的正文，用来触发近重复检测，内容要超过三十个字才能比较相似度。" * 2
    (vault_dir / "dup-a.md").write_text(f"# A\n\n{body}\n", encoding="utf-8")
    (vault_dir / "dup-b.md").write_text(f"# B\n\n{body}\n", encoding="utf-8")
    registry.invoke("kwey.write", {"path": "dup-a.md", "content": f"# A\n\n{body}\n"}, vault=vault)
    registry.invoke("kwey.write", {"path": "dup-b.md", "content": f"# B\n\n{body}\n"}, vault=vault)

    deep = registry.invoke("kwey.diagnose", {"deep": True}, vault=vault)
    dups = next(i for i in deep["issues"] if i["key"] == "duplicates")
    assert dups["count"] >= 1

    shallow = registry.invoke("kwey.diagnose", {"deep": False}, vault=vault)
    assert next(i for i in shallow["issues"] if i["key"] == "duplicates")["count"] == 0


# ---------------------------------------------------------------- capture
def test_capture_writes_inbox_note(vault, vault_dir: Path):
    res = registry.invoke(
        "kwey.capture", {"text": "周四和张三聊定价\n记得带上报价单"}, vault=vault
    )
    assert res["path"].startswith("inbox/")
    assert res["path"].endswith(".md")
    assert "周四和张三聊定价" in res["path"]  # 文件名带时间戳前缀，title 取 stem
    text = (vault_dir / res["path"].replace("/", "\\")).read_text(encoding="utf-8")
    assert "captured: true" in text
    assert "source: agent" in text
    assert "报价单" in text


def test_capture_custom_title_and_tags(vault):
    res = registry.invoke(
        "kwey.capture",
        {"text": "随手记一句话", "title": "临时想法", "tags": ["#想法", "试验"]},
        vault=vault,
    )
    assert "临时想法" in res["path"]
    note = vault.read_note(res["path"])
    assert note["frontmatter"]["tags"] == ["想法", "试验"]


def test_capture_empty_text_400(vault):
    with pytest.raises(ContractError) as ei:
        registry.invoke("kwey.capture", {"text": "   "}, vault=vault)
    assert ei.value.code == 400


def test_capture_journal_path_is_generated(vault):
    res = registry.invoke(
        "kwey.capture",
        {"text": "记一笔"},
        vault=vault,
        actor={"role": "write", "name": "w4-agent"},
    )
    entries = journal.recent(vault.paths.ensure_root(), 5)
    assert entries and entries[0]["action"] == "create"
    assert entries[0]["path"] == res["path"]
    assert entries[0]["name"] == "w4-agent"


# ---------------------------------------------------------------- 权限分级
def test_new_tools_permission_tiers():
    read = {"kwey.views.list", "kwey.views.query", "kwey.diagnose"}
    write = {"kwey.views.save", "kwey.capture"}
    for name in read:
        assert registry.get_tool(name)["permission"] == "read"
    for name in write:
        tool = registry.get_tool(name)
        assert tool["permission"] == "write"
        assert tool["side_effect"] == "reversible"
        assert tool["journal_action"] == "create"
