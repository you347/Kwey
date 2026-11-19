"""回收站恢复回归：目录条目恢复必须回到原路径，且冲突文件不覆盖。"""

from __future__ import annotations


def test_restore_note_in_existing_dir_returns_to_original_path(client, vault_dir):
    """删除子目录内单篇笔记后，.trash 顶层是目录条目；恢复不得把整个目录改名。"""
    rel = "notes/读书/2026计划.md"
    original = (vault_dir / rel).read_text(encoding="utf-8")
    assert client.delete("/api/note", params={"path": rel}).status_code == 200
    assert not (vault_dir / rel).exists()

    items = client.get("/api/trash").json()["data"]["items"]
    assert items, "回收站应有条目"
    resp = client.post("/api/trash/restore", json={"trash_path": items[0]["trash_path"]})
    assert resp.status_code == 200
    assert (vault_dir / rel).read_text(encoding="utf-8") == original
    assert not (vault_dir / "notes (1)").exists()
    assert client.get("/api/note", params={"path": rel}).status_code == 200


def test_restore_conflict_keeps_existing_file(client, vault_dir):
    """同名文件已存在时恢复应去重，不覆盖现有内容。"""
    rel = "欢迎.md"
    client.delete("/api/note", params={"path": rel})
    (vault_dir / rel).write_text("# 新欢迎\n", encoding="utf-8")
    items = client.get("/api/trash").json()["data"]["items"]
    entry = next(i for i in items if i["original_path"] == rel)
    resp = client.post("/api/trash/restore", json={"trash_path": entry["trash_path"]})
    assert resp.status_code == 200
    assert (vault_dir / rel).read_text(encoding="utf-8") == "# 新欢迎\n"
    assert resp.json()["data"]["path"] != rel
    assert (vault_dir / resp.json()["data"]["path"]).exists()
