"""导出路由：把笔记导出为原始 Markdown、独立 HTML、打印 HTML 或服务端 PDF。"""

from __future__ import annotations

import base64
import importlib.util
import io
import mimetypes
import re
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.parser.renderer import render_markdown
from app.paths import normalize_rel

router = APIRouter(prefix="/api", tags=["export"])

# 独立 HTML 的基础排版样式（不依赖任何外部 CSS）
_BASE_CSS = (
    "body{max-width:820px;margin:40px auto;padding:0 20px;"
    "font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
    "line-height:1.7;color:#222;}"
    "h1,h2,h3,h4,h5,h6{line-height:1.3;border-bottom:1px solid #eee;padding-bottom:.3em;}"
    "pre{background:#f6f8fa;padding:12px;overflow:auto;}"
    "code{background:#f6f8fa;padding:2px 4px;border-radius:3px;}"
    "pre code{background:none;padding:0;}"
    "img{max-width:100%;}"
    "blockquote{border-left:4px solid #ddd;margin:0;padding-left:1em;color:#666;}"
    "table{border-collapse:collapse;}td,th{border:1px solid #ddd;padding:6px 12px;}"
    "a{color:#0969da;}"
)

_IMG_SRC_RE = re.compile(r'<img\s+([^>]*?)src="([^"]+)"', re.IGNORECASE)

# 打印/PDF 追加样式：A4 分页 + 避免标题/代码/图片被跨页截断
_PRINT_CSS = (
    "@page{size:A4;margin:18mm 16mm;}"
    "@media print{"
    "body{max-width:none;margin:0;padding:0;font-size:11pt;}"
    "h1,h2,h3,h4,h5,h6{page-break-after:avoid;}"
    "pre,blockquote,table,img{page-break-inside:avoid;}"
    "pre{white-space:pre-wrap;word-break:break-word;}"
    "a{color:#222;text-decoration:none;}"
    "}"
)

_FORMATS = ("md", "html", "print", "pdf")

_PDF_HINT = "请改用 format=print 在浏览器中打印/另存为 PDF"


def _inline_images(html: str, root) -> str:
    """把 render_markdown 产出的 /file/<rel> 图片内联为 base64 data URI。

    设计取舍：导出的是「完整独立 HTML 文档」，若保留 /file/ 相对路由，脱离 Kwey
    服务后图片便无法解析；改为 base64 内联后离线可解析、零外部依赖，最可靠。
    re.sub 仅替换匹配到的 <img ...src="..."> 前缀，标签尾部属性（alt 等）原样保留。
    已知限制：wikilink 嵌入 ![[图]] 按既有 renderer 渲染为占位 <span>，不在内联范围。
    """

    def _sub(m: re.Match) -> str:
        attrs, src = m.group(1), m.group(2)
        if not src.startswith("/file/"):
            return m.group(0)
        rel = unquote(unescape(src[len("/file/"):]))
        try:
            abs_path = (root / rel).resolve()
            abs_path.relative_to(root.resolve())
        except (ValueError, OSError):
            return m.group(0)
        if not abs_path.is_file():
            return m.group(0)
        mime = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
        try:
            data = base64.b64encode(abs_path.read_bytes()).decode("ascii")
        except OSError:
            return m.group(0)
        return f'<img {attrs}src="data:{mime};base64,{data}"'

    return _IMG_SRC_RE.sub(_sub, html)


def _document(title: str, body_html: str, extra_css: str = "") -> str:
    """完整独立 HTML 文档（html/print/pdf 共用同一渲染产物，保证与预览一致）。"""
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escape(title)}</title>\n"
        f"<style>{_BASE_CSS}{extra_css}</style>\n</head>\n<body>\n"
        f"<h1>{escape(title)}</h1>\n"
        f"{body_html}\n</body>\n</html>\n"
    )


def _payload(filename: str, content: str, mime: str, encoding: str | None = None) -> dict:
    data = {"filename": filename, "content": content, "mime": mime}
    if encoding:
        data["encoding"] = encoding
    return {"code": 0, "data": data, "message": "ok"}


def _pdf_engine() -> str | None:
    """可用 PDF 引擎：weasyprint 优先（HTML 保真），reportlab 降级；都无则 None。"""
    if importlib.util.find_spec("weasyprint") is not None:
        return "weasyprint"
    if importlib.util.find_spec("reportlab") is not None:
        return "reportlab"
    return None


class _BlockCollector(HTMLParser):
    """把渲染 HTML 拆为 (kind, text) 块序列，供 reportlab 降级引擎排版。

    仅保留块级结构（标题/段落/代码/列表/引用/图片/分隔线），行内标签剥掉取文本；
    表格按行压平为段落。保真度低于 weasyprint，故仅作无 weasyprint 时的兜底。
    """

    _TEXT_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._kind: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._TEXT_BLOCKS:
            self._flush()
            self._kind = tag
        elif tag == "img":
            src = dict(attrs).get("src", "")
            if src.startswith("data:"):
                self.blocks.append(("img", src))
        elif tag == "hr":
            self.blocks.append(("hr", ""))
        elif tag == "br":
            self._buf.append("\n")
        elif tag == "tr":
            self._flush()
            self._kind = "p"
        elif tag in ("td", "th") and self._buf:
            self._buf.append(" | ")

    def handle_endtag(self, tag):
        if tag == self._kind or (tag == "tr" and self._kind == "p"):
            self._flush()

    def handle_data(self, data):
        if self._kind is not None:
            self._buf.append(data)

    def _flush(self):
        if self._kind is not None:
            text = "".join(self._buf).strip()
            if text:
                self.blocks.append((self._kind, text))
        self._kind = None
        self._buf = []

    def close(self):
        super().close()
        self._flush()


def _collect_blocks(html: str) -> list[tuple[str, str]]:
    collector = _BlockCollector()
    collector.feed(html)
    collector.close()
    return collector.blocks


# 中文 TTF 候选：嵌入后 PDF 自包含、任意查看器可渲染；都无则退回内置 CID 字体
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _register_cjk_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _CJK_FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("KweyCJK", path, subfontIndex=0))
            return "KweyCJK"
        except Exception:  # noqa: BLE001 —— 字体损坏/格式不支持时尝试下一个
            continue
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _pdf_via_reportlab(html: str) -> bytes:
    """reportlab 兜底：块式排版，中文优先嵌入系统 TTF（无则内置 STSong CID）。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, Image, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
    )

    font = _register_cjk_font()
    base = dict(fontName=font)
    styles = {
        "h1": ParagraphStyle("h1", **base, fontSize=18, leading=24, spaceAfter=10),
        "h2": ParagraphStyle("h2", **base, fontSize=15, leading=20, spaceBefore=8, spaceAfter=6),
        "h3": ParagraphStyle("h3", **base, fontSize=13, leading=18, spaceBefore=6, spaceAfter=4),
        "p": ParagraphStyle("p", **base, fontSize=10.5, leading=16, spaceAfter=6),
        "li": ParagraphStyle("li", **base, fontSize=10.5, leading=16, leftIndent=12, spaceAfter=3),
        "blockquote": ParagraphStyle("q", **base, fontSize=10.5, leading=16, leftIndent=12,
                                     textColor=colors.grey, spaceAfter=6),
    }
    pre_style = ParagraphStyle("pre", fontName=font, fontSize=9, leading=13,
                               backColor=colors.whitesmoke, borderPadding=4, spaceAfter=6)

    body = html.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
    story = []
    max_w = A4[0] - 32 * mm
    for kind, text in _collect_blocks(body):
        if kind == "img":
            m = re.match(r"data:[^;]+;base64,(.+)", text, re.DOTALL)
            if not m:
                continue
            try:
                img = Image(io.BytesIO(base64.b64decode(m.group(1))))
                if img.drawWidth > max_w:
                    scale = max_w / img.drawWidth
                    img.drawWidth *= scale
                    img.drawHeight *= scale
                story.append(img)
                story.append(Spacer(1, 6))
            except Exception:  # noqa: BLE001 —— 兜底引擎：图片失败不阻断文本导出
                continue
        elif kind == "hr":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        elif kind == "pre":
            story.append(Preformatted(escape(text), pre_style))
        else:
            style = styles.get(kind, styles["p"])
            if kind in ("h4", "h5", "h6"):
                style = styles["h3"]
            story.append(Paragraph(escape(text).replace("\n", "<br/>"), style))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    ).build(story)
    return buf.getvalue()


def _render_pdf(html: str) -> bytes:
    """服务端 HTML→PDF。weasyprint 走打印 CSS 保真分页；reportlab 块式兜底。"""
    if _pdf_engine() == "weasyprint":
        from weasyprint import HTML

        return HTML(string=html).write_pdf()
    return _pdf_via_reportlab(html)


@router.get("/export")
async def export_note(path: str, format: str = "md"):
    """导出笔记。md=原始全文（含 frontmatter）；html=完整独立 HTML 文档；
    print=带分页 CSS 的打印 HTML；pdf=服务端直出（需 weasyprint/reportlab）。"""
    if format not in _FORMATS:
        raise HTTPException(status_code=400, detail="format 仅支持 md/html/print/pdf")
    vault = deps.require_vault()
    linkgraph = deps.require_linkgraph()
    doc = vault.read_note(path)  # 路径校验 + 404 + 取 title/body
    rel = normalize_rel(doc["path"])
    abs_path = vault.paths.root / rel
    basename = rel.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]

    if format == "md":
        content = abs_path.read_text(encoding="utf-8-sig")
        return _payload(basename, content, "text/markdown; charset=utf-8")

    resolver = lambda target, source=None: linkgraph.resolve(target)  # noqa: E731
    body_html = render_markdown(doc["content"], resolve=resolver)
    body_html = _inline_images(body_html, vault.paths.root)

    if format == "html":
        return _payload(f"{stem}.html", _document(doc["title"], body_html), "text/html; charset=utf-8")

    print_html = _document(doc["title"], body_html, _PRINT_CSS)
    if format == "print":
        return _payload(f"{stem}.html", print_html, "text/html; charset=utf-8")

    if _pdf_engine() is None:
        raise HTTPException(
            status_code=400,
            detail=f"当前环境未安装 weasyprint/reportlab，无法服务端生成 PDF；{_PDF_HINT}",
        )
    try:
        pdf_bytes = _render_pdf(print_html)
    except (ImportError, OSError) as exc:
        # weasyprint 在 Windows 缺 GTK 依赖时导入即失败，同样引导改用 print
        raise HTTPException(status_code=400, detail=f"PDF 引擎不可用（{exc}）；{_PDF_HINT}") from exc
    return _payload(
        f"{stem}.pdf",
        base64.b64encode(pdf_bytes).decode("ascii"),
        "application/pdf",
        encoding="base64",
    )
