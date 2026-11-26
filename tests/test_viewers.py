"""非 Markdown 只读预览：转换、白名单消毒、缓存与 /api/preview、/file 出口。"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from app.paths import AppPaths
from app.viewers import PreviewError, preview_file

# ---------------------------------------------------------------- 样例构造


def _make_docx(path: Path, text: str) -> None:
    """最小 WordprocessingML 包（不依赖 python-docx）。"""
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


def _make_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "预算"
    ws.append(["项目", "金额"])
    ws.append(["服务器", 1200])
    ws.append(["域名", 80])
    ws2 = wb.create_sheet("备注")
    ws2.append(["说明", "第二张表"])
    wb.save(path)


def _make_xls(path: Path) -> None:
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("旧表")
    sheet.write(0, 0, "标题")
    sheet.write(1, 0, "旧格式单元格")
    book.save(str(path))


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "notes.txt").write_text("第一行\n<strong>不是标签</strong>\n", encoding="utf-8")
    (root / "表格.csv").write_text("名称,数量\n苹果,3\n香蕉,5\n", encoding="utf-8")
    _make_docx(root / "报告.docx", "季度报告正文")
    _make_xlsx(root / "预算.xlsx")
    _make_xls(root / "旧表.xls")
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (root / "说明.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "打包.zip").write_bytes(b"PK\x03\x04fake")
    return root


@pytest.fixture
def paths(sample_vault: Path) -> AppPaths:
    return AppPaths(sample_vault)


# ---------------------------------------------------------------- 转换


class TestPreviewFormats:
    def test_txt_is_escaped_and_cached(self, paths: AppPaths):
        first = preview_file(paths, "notes.txt")
        assert first["kind"] == "html"
        assert "第一行" in first["html"]
        assert "&lt;strong&gt;" in first["html"]
        assert first["cached"] is False

        second = preview_file(paths, "notes.txt")
        assert second["cached"] is True
        assert second["html"] == first["html"]

    def test_cache_invalidates_on_mtime_change(self, paths: AppPaths, sample_vault: Path):
        preview_file(paths, "notes.txt")
        target = sample_vault / "notes.txt"
        target.write_text("改过的内容", encoding="utf-8")
        stat = target.stat()
        target.touch()
        time.sleep(0.01)
        import os

        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert preview_file(paths, "notes.txt")["cached"] is False
        assert "改过的内容" in preview_file(paths, "notes.txt")["html"]

    def test_csv_becomes_table(self, paths: AppPaths):
        html = preview_file(paths, "表格.csv")["html"]
        assert "<table" in html
        assert "苹果" in html and "香蕉" in html

    def test_docx_converts_with_mammoth(self, paths: AppPaths):
        data = preview_file(paths, "报告.docx")
        assert data["kind"] == "html"
        assert "季度报告正文" in data["html"]

    def test_xlsx_renders_all_sheets(self, paths: AppPaths):
        html = preview_file(paths, "预算.xlsx")["html"]
        assert "预算" in html and "备注" in html
        assert "服务器" in html and "1200" in html

    def test_xls_renders_legacy_sheet(self, paths: AppPaths):
        html = preview_file(paths, "旧表.xls")["html"]
        assert "旧表" in html
        assert "旧格式单元格" in html

    def test_pdf_and_image_are_passthrough(self, paths: AppPaths):
        assert preview_file(paths, "说明.pdf")["kind"] == "pdf"
        assert preview_file(paths, "pic.png")["kind"] == "image"

    def test_unknown_format_is_415(self, paths: AppPaths):
        with pytest.raises(PreviewError) as exc:
            preview_file(paths, "打包.zip")
        assert exc.value.status_code == 415

    def test_size_limit(self, paths: AppPaths, monkeypatch):
        import app.viewers as viewers

        monkeypatch.setattr(viewers, "MAX_SOURCE_BYTES", 4)
        with pytest.raises(PreviewError) as exc:
            preview_file(paths, "打包.zip")
        assert exc.value.status_code == 413

    def test_missing_file_is_404(self, paths: AppPaths):
        with pytest.raises(PreviewError) as exc:
            preview_file(paths, "不存在.txt")
        assert exc.value.status_code == 404

    def test_doc_degrades_without_libreoffice(self, paths: AppPaths, sample_vault: Path, monkeypatch):
        (sample_vault / "老文档.doc").write_bytes(b"\xd0\xcf\x11\xe0fake")
        import app.viewers.render as render

        monkeypatch.setattr(render, "find_soffice", lambda: None)
        data = preview_file(paths, "老文档.doc")
        assert data["kind"] == "unsupported"
        assert data["degraded"] is True
        assert "LibreOffice" in data["reason"]


# ---------------------------------------------------------------- 消毒


class TestSanitize:
    def test_strips_scripts_events_and_styles(self):
        from app.viewers.render import sanitize_html

        raw = (
            '<p onclick="x()">ok</p>'
            "<script>alert(1)</script>"
            '<img src="javascript:alert(1)">'
            '<a href="javascript:alert(1)">bad</a>'
            '<span style="position:fixed">s</span>'
            '<a href="https://example.com">good</a>'
        )
        clean = sanitize_html(raw)
        assert "<script" not in clean
        assert "onclick" not in clean
        assert "javascript:" not in clean
        assert "style=" not in clean
        assert "https://example.com" in clean
        assert "ok" in clean

    def test_allows_data_image(self):
        from app.viewers.render import sanitize_html

        clean = sanitize_html('<img src="data:image/png;base64,AAAA" alt="x">')
        assert "data:image/png" in clean


# ---------------------------------------------------------------- HTTP 层


class TestPreviewApi:
    def test_preview_endpoint_returns_html(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "a.txt").write_text("接口文本", encoding="utf-8")
        resp = client.get("/api/preview", params={"path": "a.txt"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kind"] == "html" and "接口文本" in data["html"]
        assert data["url"] == "/file/a.txt"

    def test_preview_rejects_traversal(self, client):
        resp = client.get("/api/preview", params={"path": "../etc/passwd"})
        assert resp.status_code in (400, 404)

    def test_preview_cache_lives_under_kwey(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "b.txt").write_text("缓存", encoding="utf-8")
        client.get("/api/preview", params={"path": "b.txt"})
        assert (root / ".kwey" / "previews").is_dir()

    def test_file_endpoint_sets_type_and_blocks_svg_inline(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "c.svg").write_text("<svg></svg>", encoding="utf-8")
        resp = client.get("/file/c.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_file_endpoint_size_cap(self, client, vault, monkeypatch):
        from app.api import routes_system

        monkeypatch.setattr(routes_system, "MAX_SERVE_BYTES", 3)
        root = vault.paths.ensure_root()
        (root / "big.txt").write_text("超过上限", encoding="utf-8")
        assert client.get("/file/big.txt").status_code == 413
