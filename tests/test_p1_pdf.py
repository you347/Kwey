"""P1-21 PDF/打印导出测试：print HTML 契约、pdf 引擎分支、md/html 零回归。

本环境未装 weasyprint/reportlab，pdf 直出分支用 monkeypatch 模拟引擎验证路由契约；
真实引擎渲染另在隔离 venv 冒烟（不污染主环境，避免影响 legacy pdf→400 用例）。
"""

from __future__ import annotations

import base64
import sys
import types

import pytest

from app.api import routes_export


def _body(resp):
    """断言统一 {code,data,message} 包装且 code=0，返回完整 payload。"""
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload.keys()) >= {"code", "data", "message"}
    assert payload["code"] == 0, payload
    return payload


def _export(client, fmt, path="欢迎.md"):
    return _body(client.get("/api/export", params={"path": path, "format": fmt}))["data"]


# ---------------------------------------------------------------- print
class TestPrintExport:
    def test_print_html_standalone_with_title_body(self, client):
        d = _export(client, "print")
        assert d["filename"] == "欢迎.html"
        assert d["mime"].startswith("text/html")
        c = d["content"]
        assert "<!DOCTYPE html>" in c
        assert '<meta charset="utf-8">' in c
        assert "<title>欢迎</title>" in c
        assert "<h1>欢迎</h1>" in c
        assert "这是" in c  # 正文渲染产物
        assert "<p>" in c

    def test_print_pagination_css(self, client):
        c = _export(client, "print")["content"]
        assert "@page" in c
        assert "size:A4" in c
        assert "@media print" in c
        assert "page-break-after:avoid" in c
        assert "page-break-inside:avoid" in c

    def test_print_image_inlined_offline(self, client, vault_dir):
        (vault_dir / "带图.md").write_text(
            "# 带图\n\n![示意图](attachments/pic.png)\n", encoding="utf-8"
        )
        c = _export(client, "print", path="带图.md")["content"]
        assert "data:image/png;base64," in c
        assert "/file/" not in c  # 离线可打开：不依赖 Kwey 服务路由

    def test_print_body_identical_to_html(self, client):
        # 与预览渲染一致：print 仅比 html 多一段分页 CSS，其余逐字节相同
        p = _export(client, "print")["content"]
        h = _export(client, "html")["content"]
        assert p.replace(routes_export._PRINT_CSS, "") == h


# ---------------------------------------------------------------- pdf
class TestPdfExport:
    def test_pdf_without_engine_400_hint(self, client):
        if routes_export._pdf_engine() is not None:
            pytest.skip("环境已装 PDF 引擎，走直出分支")
        resp = client.get("/api/export", params={"path": "欢迎.md", "format": "pdf"})
        assert resp.status_code == 400
        detail = resp.json()["message"]  # 全局异常包装：detail → message
        assert "print" in detail
        assert "weasyprint" in detail or "reportlab" in detail

    def test_pdf_with_engine_base64_download(self, client, monkeypatch):
        fake_pdf = b"%PDF-1.4\nfake engine output"
        monkeypatch.setattr(routes_export, "_pdf_engine", lambda: "weasyprint")
        monkeypatch.setattr(routes_export, "_render_pdf", lambda html: fake_pdf)
        d = _export(client, "pdf")
        assert d["filename"] == "欢迎.pdf"
        assert d["mime"] == "application/pdf"
        assert d["encoding"] == "base64"
        assert base64.b64decode(d["content"]) == fake_pdf

    def test_pdf_engine_broken_400_hint(self, client, monkeypatch):
        # weasyprint 装了但缺 GTK 依赖：导入失败同样引导改用 print
        monkeypatch.setattr(routes_export, "_pdf_engine", lambda: "weasyprint")

        def boom(html):
            raise ImportError("cannot load library 'gobject-2.0-0'")

        monkeypatch.setattr(routes_export, "_render_pdf", boom)
        resp = client.get("/api/export", params={"path": "欢迎.md", "format": "pdf"})
        assert resp.status_code == 400
        assert "print" in resp.json()["message"]

    def test_render_pdf_dispatch_weasyprint(self, monkeypatch):
        fake = types.ModuleType("weasyprint")

        class FakeHTML:
            def __init__(self, string):
                self.string = string

            def write_pdf(self):
                assert "<html>" in self.string
                return b"%PDF weasy"

        fake.HTML = FakeHTML
        monkeypatch.setitem(sys.modules, "weasyprint", fake)
        monkeypatch.setattr(routes_export, "_pdf_engine", lambda: "weasyprint")
        assert routes_export._render_pdf("<html></html>") == b"%PDF weasy"

    def test_pdf_engine_detection_priority(self, monkeypatch):
        util = routes_export.importlib.util
        monkeypatch.setattr(util, "find_spec", lambda name: object() if name == "reportlab" else None)
        assert routes_export._pdf_engine() == "reportlab"
        monkeypatch.setattr(util, "find_spec", lambda name: object())
        assert routes_export._pdf_engine() == "weasyprint"  # weasyprint 优先
        monkeypatch.setattr(util, "find_spec", lambda name: None)
        assert routes_export._pdf_engine() is None


# ---------------------------------------------------------------- reportlab 块解析
class TestBlockCollector:
    def test_blocks_inline_strip_and_list(self):
        html = (
            "<h1>标题</h1><p>你好 <b>世界</b> 啊</p><pre>code\nline2</pre>"
            "<ul><li>项一</li><li>项二</li></ul><blockquote>引用</blockquote><hr>"
        )
        blocks = routes_export._collect_blocks(html)
        assert ("h1", "标题") in blocks
        assert ("p", "你好 世界 啊") in blocks
        assert ("pre", "code\nline2") in blocks
        assert ("li", "项一") in blocks
        assert ("li", "项二") in blocks
        assert ("blockquote", "引用") in blocks
        assert ("hr", "") in blocks

    def test_img_data_uri_only_and_table_flatten(self):
        html = (
            '<p>前<img src="data:image/png;base64,AA==" alt="x">后</p>'
            '<img src="https://example.com/a.png">'
            "<table><tr><td>a</td><td>b</td></tr></table>"
        )
        blocks = routes_export._collect_blocks(html)
        assert ("img", "data:image/png;base64,AA==") in blocks
        assert ("p", "前后") in blocks  # 内联图片不丢前后文本
        assert ("p", "a | b") in blocks  # 表格按行压平
        assert not any(k == "img" and "https://" in v for k, v in blocks)

    def test_entities_decoded(self):
        blocks = routes_export._collect_blocks("<p>a &amp; b &lt;c&gt;</p>")
        assert ("p", "a & b <c>") in blocks


# ---------------------------------------------------------------- 校验与零回归
class TestValidationAndRegression:
    def test_invalid_formats_400(self, client):
        for fmt in ("", "exe", "PDF", "docx"):
            resp = client.get("/api/export", params={"path": "欢迎.md", "format": fmt})
            assert resp.status_code == 400, fmt

    def test_traversal_400_print_pdf(self, client):
        for fmt in ("print", "pdf"):
            resp = client.get("/api/export", params={"path": "../secret.md", "format": fmt})
            assert resp.status_code == 400, fmt

    def test_missing_note_404(self, client):
        resp = client.get("/api/export", params={"path": "不存在.md", "format": "print"})
        assert resp.status_code == 404

    def test_md_unchanged(self, client):
        d = _export(client, "md")
        assert d["filename"] == "欢迎.md"
        assert d["content"].startswith("---")
        assert "title: 欢迎" in d["content"]
        assert d["mime"].startswith("text/markdown")
        assert "encoding" not in d

    def test_html_unchanged_without_print_css(self, client):
        d = _export(client, "html")
        assert d["filename"] == "欢迎.html"
        assert "<!DOCTYPE html>" in d["content"]
        assert "@page" not in d["content"]
        assert "encoding" not in d

    def test_default_format_is_md(self, client):
        d = _body(client.get("/api/export", params={"path": "欢迎.md"}))["data"]
        assert d["filename"] == "欢迎.md"
        assert d["mime"].startswith("text/markdown")