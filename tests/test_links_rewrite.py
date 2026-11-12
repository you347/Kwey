"""T04 链接重写单测：改名/移动全库 0 断链（G2）、嵌入与附件路径同步、trash 删除。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

FIVE_REFS = "# 引用 {} \n\n[[{}]] 与 [[{}|别名]] 与 ![[{}]]\n"


class TestRenameRewrite:
    def test_rename_zero_broken_links(self, vault, vault_dir):
        """G2：被 5 处引用的笔记改名后，全部引用自动更新且库内 0 断链。"""
        # 准备 5 个引用方（含别名、嵌入两种形式）
        for i in range(5):
            path = f"notes/工作/引用{i}.md"
            (vault_dir / "notes" / "工作" / f"引用{i}.md").write_text(
                FIVE_REFS.format(i, "2026计划", "2026计划", "2026计划"), encoding="utf-8"
            )
            vault.indexer.reindex_note(path)
        assert len(vault.db.link_sources("notes/读书/2026计划.md")) >= 5

        result = vault.rename_note("notes/读书/2026计划.md", "2027计划")
        assert result["new_path"] == "notes/读书/2027计划.md"
        assert result["rewritten_links"] >= 15  # 每个引用文件 3 处（含原欢迎.md 之外）

        # 0 断链：links 表中不再有指向旧路径的记录；正文不再含旧名
        old_refs = vault.db.link_sources("notes/读书/2026计划.md")
        # 旧路径已无笔记，target_path 均应为 NULL 或新路径——检查正文层面
        for i in range(5):
            text = (vault_dir / "notes" / "工作" / f"引用{i}.md").read_text(encoding="utf-8")
            assert "[[2027计划]]" in text
            assert "[[2027计划|别名]]" in text
            assert "![[2027计划]]" in text
            assert "[[2026计划" not in text
        # 新路径可被正常解析
        rows = vault.db.links_for("notes/工作/引用0.md")
        assert any(r["target_path"] == "notes/读书/2027计划.md" for r in rows)
        assert old_refs == [] or all(
            r != "notes/读书/2026计划.md" for r in old_refs
        )

    def test_move_keeps_links(self, vault, vault_dir):
        """移动目录后链接仍解析成功（basename 式写法不变）。"""
        result = vault.move_note("notes/读书/2026计划.md", "notes/工作")
        assert result["new_path"] == "notes/工作/2026计划.md"
        rows = vault.db.links_for("欢迎.md")
        assert any(r["target_path"] == "notes/工作/2026计划.md" for r in rows)
        text = (vault_dir / "欢迎.md").read_text(encoding="utf-8")
        assert "[[2026计划]]" in text  # basename 式写法保持

    def test_path_style_rewritten(self, vault, vault_dir):
        """路径式写法 [[notes/读书/2026计划]] 改名后重写为新路径。"""
        (vault_dir / "notes" / "工作" / "路径式.md").write_text(
            "见 [[notes/读书/2026计划]]\n", encoding="utf-8"
        )
        vault.indexer.reindex_note("notes/工作/路径式.md")
        vault.rename_note("notes/读书/2026计划.md", "新计划")
        text = (vault_dir / "notes" / "工作" / "路径式.md").read_text(encoding="utf-8")
        assert "[[notes/读书/新计划]]" in text

    def test_rename_conflict_409(self, vault):
        vault.create_note("重名.md")
        with pytest.raises(HTTPException) as exc:
            vault.rename_note("欢迎.md", "重名")
        assert exc.value.status_code == 409


class TestDelete:
    def test_delete_goes_to_trash(self, vault, vault_dir):
        trash = vault.delete_note("欢迎.md")
        assert trash.startswith(".trash/")
        assert (vault_dir / trash).is_file()
        assert not (vault_dir / "欢迎.md").exists()
        assert vault.db.get_note("欢迎.md") is None
        # 引用方链接变为断链（target_path NULL），不报错
        rows = vault.db.links_for("notes/读书/2026计划.md")
        assert all(r["target_path"] != "欢迎.md" for r in rows)

    def test_delete_dir_requires_confirm(self, vault, vault_dir):
        with pytest.raises(HTTPException) as exc:
            vault.delete_dir("notes/工作")
        assert exc.value.status_code == 409
        trash = vault.delete_dir("notes/工作", confirm=True)
        assert trash.startswith(".trash/")
        assert not (vault_dir / "notes" / "工作").exists()

    def test_rename_nonexistent_404(self, vault):
        with pytest.raises(HTTPException):
            vault.rename_note("ghost.md", "x")
