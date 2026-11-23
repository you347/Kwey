"""P0 库导入测试：扫描只读、冲突策略、断链、跳过规则、批量快照抑制、API 契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.vault.importer import VaultImporter


def make_source(tmp_path: Path) -> Path:
    """模拟一个 Obsidian vault：隐藏目录 + 嵌套目录 + 附件 + 断链 + 非 md 文件。"""
    src = tmp_path / "obsidian-vault"
    (src / ".obsidian").mkdir(parents=True)
    (src / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (src / ".trash").mkdir()
    (src / ".trash" / "旧笔记.md").write_text("# 回收站里的\n", encoding="utf-8")
    (src / "项目").mkdir()
    (src / "附件").mkdir()
    (src / "首页.md").write_text(
        "---\ntitle: 首页\ntags: [moc]\n---\n\n# 首页\n\n见 [[计划]] 与 ![[图.png]]\n",
        encoding="utf-8",
    )
    (src / "项目" / "计划.md").write_text(
        "# 计划\n\n链接 [[不存在的笔记]]\n", encoding="utf-8"
    )
    (src / "附件" / "图.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (src / "笔记.txt").write_text("不是 markdown", encoding="utf-8")
    return src


# ------------------------------------------------------------------ 扫描
def test_scan_counts(vault, tmp_path):
    src = make_source(tmp_path)
    data = VaultImporter(vault).scan(str(src))
    assert data["notes"] == 2, [x["path"] for x in data["samples"]]
    assert data["attachments"] == 1
    assert data["skipped_files"] == 1
    assert data["conflicts"] == 0
    assert data["links_total"] == 3      # [[计划]] + ![[图.png]] + 断链
    assert data["links_broken"] == 1
    assert data["broken_samples"][0]["target"] == "不存在的笔记"


def test_scan_skips_hidden_and_reserved_dirs(vault, tmp_path):
    src = make_source(tmp_path)
    data = VaultImporter(vault).scan(str(src))
    paths = [item["path"] for item in data["samples"]]
    assert all(".obsidian" not in p and ".trash" not in p for p in paths)
    assert data["notes"] == 2      # .trash/旧笔记.md 不计入


def test_scan_is_read_only(vault, tmp_path):
    src = make_source(tmp_path)
    before = sorted(p.name for p in Path(vault.paths.root).rglob("*"))
    VaultImporter(vault).scan(str(src))
    after = sorted(p.name for p in Path(vault.paths.root).rglob("*"))
    assert before == after
    assert not (Path(vault.paths.root) / "首页.md").exists()


def test_scan_reports_conflicts(vault, tmp_path):
    src = make_source(tmp_path)
    importer = VaultImporter(vault)
    importer.apply(str(src))
    data = importer.scan(str(src))
    assert data["conflicts"] == 3          # 2 篇笔记 + 1 个附件


# ------------------------------------------------------------------ 落盘
def test_apply_creates_notes_and_attachments(vault, tmp_path):
    src = make_source(tmp_path)
    report = VaultImporter(vault).apply(str(src))
    assert report["notes_created"] == 2
    assert report["attachments_copied"] == 1
    root = Path(vault.paths.root)
    assert (root / "首页.md").is_file()
    assert (root / "项目" / "计划.md").is_file()      # 保留目录结构
    assert (root / "附件" / "图.png").is_file()
    assert not (root / "笔记.txt").exists()           # 非 md/附件不导入


def test_apply_preserves_frontmatter(vault, tmp_path):
    src = make_source(tmp_path)
    VaultImporter(vault).apply(str(src))
    note = vault.read_note("首页.md")
    assert note["frontmatter"]["title"] == "首页"
    assert note["frontmatter"]["tags"] == ["moc"]


def test_apply_skips_attachments_when_disabled(vault, tmp_path):
    src = make_source(tmp_path)
    report = VaultImporter(vault).apply(str(src), include_attachments=False)
    assert report["attachments_copied"] == 0
    assert not (Path(vault.paths.root) / "附件" / "图.png").exists()


def test_conflict_rename_keeps_both(vault, tmp_path):
    src = make_source(tmp_path)
    importer = VaultImporter(vault)
    importer.apply(str(src))
    report = importer.apply(str(src), conflict="rename")
    assert report["notes_renamed"] == 2
    assert (Path(vault.paths.root) / "首页 (1).md").is_file()


def test_conflict_skip_leaves_existing(vault, tmp_path):
    src = make_source(tmp_path)
    importer = VaultImporter(vault)
    importer.apply(str(src))
    report = importer.apply(str(src), conflict="skip")
    assert report["notes_skipped"] == 2
    assert report["notes_created"] == 0


def test_conflict_overwrite_updates_content(vault, tmp_path):
    src = make_source(tmp_path)
    importer = VaultImporter(vault)
    importer.apply(str(src))
    (src / "首页.md").write_text("# 首页（改过）\n", encoding="utf-8")
    report = importer.apply(str(src), conflict="overwrite")
    assert report["notes_overwritten"] == 2
    assert "改过" in vault.read_note("首页.md")["content"]


def test_apply_indexes_imported_notes(vault, tmp_path):
    src = make_source(tmp_path)
    VaultImporter(vault).apply(str(src))
    hits, _total = vault.db.search(terms=[], file_pattern="计划", limit=10)
    paths = {hit["path"] for hit in hits}
    assert "项目/计划.md" in paths


# ------------------------------------------------------------------ 源目录校验
@pytest.mark.parametrize("kind", ["missing", "file", "self", "inside"])
def test_invalid_source_rejected(vault, tmp_path, kind):
    importer = VaultImporter(vault)
    if kind == "missing":
        source = str(tmp_path / "nope")
    elif kind == "file":
        f = tmp_path / "a.md"
        f.write_text("x", encoding="utf-8")
        source = str(f)
    elif kind == "self":
        source = str(vault.paths.root)
    else:
        child = Path(vault.paths.root) / "子目录"
        child.mkdir()
        source = str(child)
    with pytest.raises(ValueError):
        importer.scan(source)


def test_invalid_conflict_policy(vault, tmp_path):
    src = make_source(tmp_path)
    with pytest.raises(ValueError, match="冲突策略"):
        VaultImporter(vault).scan(str(src), conflict="merge")


def test_blank_source_rejected(vault):
    with pytest.raises(ValueError):
        VaultImporter(vault).scan("   ")


# ------------------------------------------------------------------ 批量快照
def test_apply_suppresses_per_note_snapshots(vault, tmp_path):
    """批量导入只在收尾提交一次，不给每篇笔记生成 git 快照。"""
    messages = []

    class FakeSnapshots:
        root = Path(vault.paths.root)
        enabled = True

        def ensure_repo(self):
            (Path(vault.paths.root) / ".git").mkdir(exist_ok=True)

        def commit(self, message, rel=None):
            messages.append(message)
            return "abc1234"

    src = make_source(tmp_path)
    vault.bind_snapshots(FakeSnapshots())
    vault.config.set({"snapshots": {"enabled": True, "auto_commit": True}})
    try:
        VaultImporter(vault).apply(str(src))
    finally:
        vault.config.set({"snapshots": {"enabled": False}})
        vault.bind_snapshots(None)
    assert len(messages) == 1
    assert "import" in messages[0]


# ------------------------------------------------------------------ API
def test_api_scan_and_apply(client, tmp_path):
    src = make_source(tmp_path)
    res = client.post("/api/import/scan", json={"source": str(src)})
    assert res.status_code == 200
    assert res.json()["data"]["notes"] == 2

    res = client.post("/api/import/apply", json={"source": str(src), "conflict": "rename"})
    assert res.status_code == 200
    assert res.json()["data"]["notes_created"] == 2


def test_api_invalid_source_400(client, tmp_path):
    res = client.post("/api/import/scan", json={"source": str(tmp_path / "nope")})
    assert res.status_code == 400
    assert res.json()["code"] == 400


def test_api_invalid_policy_400(client, tmp_path):
    src = make_source(tmp_path)
    res = client.post("/api/import/scan", json={"source": str(src), "conflict": "merge"})
    assert res.status_code == 400
