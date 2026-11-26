"""附件全文索引：独立 FTS、格式抽取、检索标记、写回增量更新。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.api.routes_search import build_match_terms


def _terms(*words: str):
    return build_match_terms(list(words))


def _search_attachments(vault, *words: str):
    return vault.db.search_attachments(_terms(*words))


def _find_tree_node(nodes, path: str):
    for node in nodes or []:
        if node.get("path") == path:
            return node
        if node.get("type") == "dir":
            found = _find_tree_node(node.get("children") or [], path)
            if found:
                return found
    return None


def _make_docx(path: Path, text: str) -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)


class TestAttachmentIndex:
    def test_txt_indexed_and_searchable(self, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "说明.txt").write_bytes("季度预算审批流程\n".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/说明.txt")

        hits, total = _search_attachments(vault, "预算")
        assert total == 1
        assert hits[0]["path"] == "attachments/说明.txt"
        assert hits[0]["kind"] == "attachment"
        assert hits[0]["format"] == "txt"
        assert "预算" in hits[0]["snippet"]

    def test_csv_indexed(self, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "预算表.csv").write_bytes("项目,金额\n服务器,1200\n".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/预算表.csv")

        hits, total = _search_attachments(vault, "服务器")
        assert total == 1
        assert hits[0]["path"] == "attachments/预算表.csv"

    def test_docx_indexed(self, vault):
        root = vault.paths.ensure_root()
        _make_docx(root / "attachments" / "报告.docx", "年度复盘正文")
        vault.indexer.reindex_attachment("attachments/报告.docx")

        hits, total = _search_attachments(vault, "复盘")
        assert total == 1
        assert hits[0]["path"] == "attachments/报告.docx"

    def test_xlsx_indexed(self, vault):
        from openpyxl import Workbook

        root = vault.paths.ensure_root()
        wb = Workbook()
        ws = wb.active
        ws.append(["项目", "金额"])
        ws.append(["云服务器", 900])
        wb.save(root / "attachments" / "预算.xlsx")
        vault.indexer.reindex_attachment("attachments/预算.xlsx")

        hits, total = _search_attachments(vault, "云服务器")
        assert total == 1

    def test_xls_indexed(self, vault):
        xlwt = pytest.importorskip("xlwt")
        root = vault.paths.ensure_root()
        book = xlwt.Workbook()
        sheet = book.add_sheet("旧表")
        sheet.write(0, 0, "项目")
        sheet.write(0, 1, "金额")
        sheet.write(1, 0, "老式服务器")
        sheet.write(1, 1, 1200)
        book.save(str(root / "attachments" / "旧预算.xls"))

        vault.indexer.reindex_attachment("attachments/旧预算.xls")
        hits, total = _search_attachments(vault, "老式服务器")
        assert total == 1
        assert hits[0]["path"] == "attachments/旧预算.xls"
        assert hits[0]["format"] == "xls"
        assert vault.db.attachment_status("attachments/旧预算.xls")["state"] == "indexed"

    def test_bad_xls_does_not_break_index(self, vault):
        root = vault.paths.ensure_root()
        bad = root / "attachments" / "坏表.xls"
        bad.write_bytes(b"not-an-excel-file")
        before = vault.db.count_attachments()
        vault.indexer.reindex_attachment("attachments/坏表.xls")
        assert vault.db.count_attachments() == before

    def test_doc_indexed_via_conversion(self, vault, monkeypatch):
        root = vault.paths.ensure_root()
        converted = root / "attachments" / "_旧文档.docx"
        _make_docx(converted, "旧格式索引正文")
        (root / "attachments" / "旧文档.doc").write_bytes(b"fake-doc")

        import app.viewers.render as render
        monkeypatch.setattr(render, "find_soffice", lambda: "soffice")
        monkeypatch.setattr(
            render, "convert_doc_to_docx", lambda path, cache_dir=None: converted
        )
        vault.indexer.reindex_attachment("attachments/旧文档.doc")

        hits, total = _search_attachments(vault, "索引正文")
        assert total == 1
        assert hits[0]["path"] == "attachments/旧文档.doc"

    def test_doc_without_libreoffice_skips(self, vault, monkeypatch):
        root = vault.paths.ensure_root()
        (root / "attachments" / "无转换.doc").write_bytes(b"fake-doc")

        import app.viewers.render as render
        monkeypatch.setattr(render, "find_soffice", lambda: None)
        before = vault.db.count_attachments()
        vault.indexer.reindex_attachment("attachments/无转换.doc")
        assert vault.db.count_attachments() == before
        status = vault.db.attachment_status("attachments/无转换.doc")
        assert status is not None and status["state"] == "missing_converter"

    def test_pdf_indexed(self, vault):
        reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
        root = vault.paths.ensure_root()
        pdf_path = root / "attachments" / "报告.pdf"
        canvas = reportlab.Canvas(str(pdf_path))
        canvas.drawString(72, 720, "QuarterlyRevenueReview")
        canvas.showPage()
        canvas.save()
        vault.indexer.reindex_attachment("attachments/报告.pdf")

        hits, total = _search_attachments(vault, "QuarterlyRevenueReview")
        assert total == 1
        assert hits[0]["format"] == "pdf"

    def test_notes_not_in_attachment_index(self, vault):
        vault.indexer.full_scan()
        hits, total = _search_attachments(vault, "欢迎")
        assert total == 0 and hits == []

    def test_binary_and_unsupported_skipped(self, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "图片.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        before = vault.db.count_attachments()
        vault.indexer.reindex_attachment("attachments/图片.png")
        assert vault.db.count_attachments() == before

    def test_remove_file_entry_drops_index(self, vault):
        root = vault.paths.ensure_root()
        path = root / "attachments" / "临时.txt"
        path.write_bytes("待删除内容".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/临时.txt")
        assert _search_attachments(vault, "待删除")[1] == 1
        assert vault.db.attachment_status("attachments/临时.txt")["state"] == "indexed"

        vault.indexer.remove_file_entry("attachments/临时.txt")
        assert _search_attachments(vault, "待删除")[1] == 0
        assert vault.db.attachment_status("attachments/临时.txt") is None

    def test_write_plain_text_updates_index(self, vault):
        root = vault.paths.ensure_root()
        path = root / "attachments" / "日志.log"
        path.write_bytes("旧日志\n".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/日志.log")

        data = vault.read_plain_text("attachments/日志.log")
        vault.write_plain_text("attachments/日志.log", "新关键字甲\n", base_mtime=data["mtime"])

        assert _search_attachments(vault, "关键字甲")[1] == 1
        assert _search_attachments(vault, "旧日志")[1] == 0

    def test_full_scan_indexes_attachments(self, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "扫描.txt").write_bytes("全量扫描附件".encode("utf-8"))
        vault.indexer.full_scan()
        assert _search_attachments(vault, "全量扫描")[1] == 1


class TestAttachmentSearchApi:
    def test_kinds_and_note_priority(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "notes" / "预算笔记.md").write_text(
            "---\ntitle: 预算笔记\n---\n预算说明\n", encoding="utf-8"
        )
        (root / "attachments" / "预算附件.txt").write_bytes("预算附件正文".encode("utf-8"))
        vault.indexer.full_scan()

        data = client.get("/api/search", params={"q": "预算"}).json()["data"]
        kinds = [h["kind"] for h in data["hits"]]
        assert "note" in kinds and "attachment" in kinds
        assert kinds.index("note") < kinds.index("attachment")
        assert data["total"] >= 2

    def test_tag_filter_excludes_attachments(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "唯一词组.txt").write_bytes("独有词汇".encode("utf-8"))
        vault.indexer.full_scan()

        data = client.get("/api/search", params={"q": "独有词汇 tag:不存在"}).json()["data"]
        assert all(h["kind"] != "attachment" for h in data["hits"])

    def test_path_filter_applies_to_attachments(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "路径过滤.txt").write_bytes("路径专用词".encode("utf-8"))
        vault.indexer.full_scan()

        inside = client.get(
            "/api/search", params={"q": "路径专用词 path:attachments"}
        ).json()["data"]
        outside = client.get(
            "/api/search", params={"q": "路径专用词 path:notes"}
        ).json()["data"]
        assert any(h["kind"] == "attachment" for h in inside["hits"])
        assert all(h["kind"] != "attachment" for h in outside["hits"])

    def test_plain_put_refreshes_search(self, client, vault):
        root = vault.paths.ensure_root()
        path = root / "attachments" / "可写.txt"
        path.write_bytes("初始内容\n".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/可写.txt")

        doc = client.get("/api/plain-file", params={"path": "attachments/可写.txt"}).json()["data"]
        client.put(
            "/api/plain-file",
            json={"path": "attachments/可写.txt", "content": "改后词组乙\n", "base_mtime": doc["mtime"]},
        )
        data = client.get("/api/search", params={"q": "词组乙"}).json()["data"]
        assert any(h["path"] == "attachments/可写.txt" and h["kind"] == "attachment" for h in data["hits"])


class TestAttachmentStatus:
    def test_oversize_not_indexed_and_visible(self, client, vault, monkeypatch):
        import app.index.indexer as indexer

        monkeypatch.setattr(indexer, "MAX_ATTACHMENT_BYTES", 8)
        root = vault.paths.ensure_root()
        (root / "attachments" / "超大.txt").write_bytes("超限内容".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/超大.txt")

        status = vault.db.attachment_status("attachments/超大.txt")
        assert status is not None and status["state"] == "too_large"
        assert _search_attachments(vault, "超限内容")[1] == 0

        data = client.get("/api/preview", params={"path": "attachments/超大.txt"}).json()["data"]
        assert data["index_status"]["state"] == "too_large"

        node = _find_tree_node(client.get("/api/tree").json()["data"], "attachments/超大.txt")
        assert node is not None
        assert node["index_state"] == "too_large"
        assert node["index_detail"]

    def test_truncated_indexes_prefix_only(self, vault, monkeypatch):
        import app.index.indexer as indexer

        monkeypatch.setattr(indexer, "MAX_ATTACHMENT_INDEX_CHARS", 5)
        root = vault.paths.ensure_root()
        (root / "attachments" / "长文.txt").write_bytes("前缀关键字甲乙丙丁".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/长文.txt")

        status = vault.db.attachment_status("attachments/长文.txt")
        assert status is not None and status["state"] == "truncated"
        assert "仅索引前 5 字" in status["detail"]
        assert _search_attachments(vault, "前缀关键")[1] == 1
        assert _search_attachments(vault, "甲乙丙丁")[1] == 0

        node = _find_tree_node(vault.tree(), "attachments/长文.txt")
        assert node is not None and node["index_state"] == "truncated"

    def test_empty_text_status(self, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "空文件.txt").write_bytes(b"")
        vault.indexer.reindex_attachment("attachments/空文件.txt")
        status = vault.db.attachment_status("attachments/空文件.txt")
        assert status is not None and status["state"] == "empty"

    def test_indexed_status_on_preview(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "正常.txt").write_bytes("正常内容".encode("utf-8"))
        vault.indexer.reindex_attachment("attachments/正常.txt")
        data = client.get("/api/preview", params={"path": "attachments/正常.txt"}).json()["data"]
        assert data["index_status"]["state"] == "indexed"
