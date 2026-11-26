"""把非 Markdown 文件转换成只读 HTML 片段（全部经白名单消毒后返回）。"""

from __future__ import annotations

import hashlib
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from app.viewers.extract import (
    MAX_SHEETS,
    MAX_TABLE_COLS,
    MAX_TABLE_ROWS,
    csv_rows,
    decode_text,
)

# ---------------------------------------------------------------- 白名单消毒
_ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "caption", "code", "col", "colgroup",
    "dd", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "s", "small",
    "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "u", "ul",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "table": {"border"},
}


def _attr_ok(tag: str, name: str, value: str) -> bool:
    if name not in _ALLOWED_ATTRS.get(tag, set()):
        return False
    if name == "href":
        return value.lower().startswith(("http://", "https://", "mailto:"))
    if name == "src":
        return value.lower().startswith(("http://", "https://", "data:image/"))
    if name in {"width", "height", "colspan", "rowspan", "border"}:
        return value.isdigit()
    return True


def sanitize_html(fragment: str) -> str:
    """按标签/属性/协议白名单清洗；注释、脚本、事件属性、内联样式全部剔除。"""
    import bleach

    return bleach.clean(
        fragment,
        tags=_ALLOWED_TAGS,
        attributes=_attr_ok,
        protocols=["http", "https", "mailto", "data"],
        strip=True,
        strip_comments=True,
    )


# ---------------------------------------------------------------- 纯文本 / CSV
def text_to_html(text: str, *, truncated: bool = False) -> str:
    body = f'<pre class="kwey-doc__plain">{html.escape(text)}</pre>'
    return f'<article class="kwey-doc">{body}{_note(truncated)}</article>'


def _note(truncated: bool) -> str:
    if not truncated:
        return ""
    return '<p class="kwey-doc__note">内容较大，已截断显示。</p>'


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return html.escape(str(value))


def _table_html(rows: Sequence[Sequence[Any]], *, header: bool = False) -> str:
    if not rows:
        return '<p class="kwey-doc__note">空表</p>'
    out: List[str] = ['<table class="kwey-doc__table">']
    start = 0
    if header:
        out.append("<thead><tr>")
        out.extend(f"<th>{_cell(v)}</th>" for v in rows[0])
        out.append("</tr></thead>")
        start = 1
    out.append("<tbody>")
    for row in rows[start:]:
        out.append("<tr>")
        out.extend(f"<td>{_cell(v)}</td>" for v in row)
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def csv_to_html(data: bytes) -> str:
    rows = csv_rows(data)
    truncated = len(rows) >= MAX_TABLE_ROWS
    text, _enc = decode_text(data[:8192])
    header = False
    try:
        import csv as _csv
        import io as _io

        header = _csv.Sniffer().has_header(text)
    except Exception:  # noqa: BLE001 - 嗅探失败按无表头处理
        header = False
    body = _table_html(rows, header=header)
    return f'<article class="kwey-doc">{body}{_note(truncated)}</article>'


# ---------------------------------------------------------------- Office
def docx_to_html(abs_path: Path) -> str:
    import mammoth

    with abs_path.open("rb") as fh:
        result = mammoth.convert_to_html(fh)
    return f'<article class="kwey-doc">{sanitize_html(result.value)}</article>'


def _xlsx_sheet_rows(sheet) -> Tuple[List[List[Any]], bool]:
    rows: List[List[Any]] = []
    truncated = False
    for row in sheet.iter_rows(values_only=True):
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        rows.append(list(row[:MAX_TABLE_COLS]))
    return rows, truncated


def xlsx_to_html(abs_path: Path) -> str:
    from openpyxl import load_workbook

    parts: List[str] = []
    wb = load_workbook(filename=str(abs_path), read_only=True, data_only=True)
    try:
        for index, sheet in enumerate(wb.worksheets):
            if index >= MAX_SHEETS:
                parts.append('<p class="kwey-doc__note">工作表过多，其余未显示。</p>')
                break
            rows, truncated = _xlsx_sheet_rows(sheet)
            parts.append(f"<h3>{html.escape(sheet.title)}</h3>")
            parts.append(_table_html(rows))
            parts.append(_note(truncated))
    finally:
        wb.close()
    return f'<article class="kwey-doc">{"".join(parts)}</article>'


def xls_to_html(abs_path: Path) -> str:
    import xlrd

    parts: List[str] = []
    book = xlrd.open_workbook(str(abs_path), on_demand=True)
    try:
        for index in range(min(book.nsheets, MAX_SHEETS)):
            sheet = book.sheet_by_index(index)
            rows = [
                sheet.row_values(r)[:MAX_TABLE_COLS]
                for r in range(min(sheet.nrows, MAX_TABLE_ROWS))
            ]
            parts.append(f"<h3>{html.escape(sheet.name)}</h3>")
            parts.append(_table_html(rows))
            if sheet.nrows > MAX_TABLE_ROWS:
                parts.append(_note(True))
    finally:
        book.release_resources()
    return f'<article class="kwey-doc">{"".join(parts)}</article>'


# ---------------------------------------------------------------- .doc 降级
def find_soffice() -> Optional[str]:
    """定位 LibreOffice；找不到时 .doc 只保留下载。"""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


class DocConversionError(RuntimeError):
    pass


# 转换缓存版本：转换逻辑变化时 +1，使旧 docx 缓存自动失效
DOC_CACHE_VERSION = 1


def convert_doc_to_docx(abs_path: Path, cache_dir: Optional[Path] = None) -> Path:
    """把 .doc 转成 .docx 并缓存；命中缓存直接复用（按路径+mtime+大小+版本为键）。

    缓存目录由调用方传入（预览统一用 ``<vault>/.kwey/previews``，可整库删除重建）。
    """
    soffice = find_soffice()
    if not soffice:
        raise DocConversionError("未检测到 LibreOffice/soffice（.doc 转换所需）")

    stat = abs_path.stat()
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "kwey-doc-cache"
    target_dir = Path(cache_dir) / "doc"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path(tempfile.gettempdir()) / "kwey-doc-cache" / "doc"
        target_dir.mkdir(parents=True, exist_ok=True)

    key = hashlib.sha1(
        f"{abs_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{DOC_CACHE_VERSION}".encode("utf-8")
    ).hexdigest()
    cached = target_dir / f"{key}.docx"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached

    with tempfile.TemporaryDirectory(prefix="kwey-doc-") as tmp:
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp, str(abs_path)],
                capture_output=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DocConversionError(f"LibreOffice 转换失败: {exc}") from exc
        converted = Path(tmp) / f"{abs_path.stem}.docx"
        if proc.returncode != 0 or not converted.is_file():
            detail = (proc.stderr or b"").decode("utf-8", "replace")[:200]
            raise DocConversionError(f"LibreOffice 转换失败: {detail or '无输出文件'}")
        try:
            tmp_target = cached.with_name(cached.name + ".tmp")
            shutil.copyfile(converted, tmp_target)
            os.replace(tmp_target, cached)
        except OSError:
            # 缓存目录不可写时仍返回一份可用的 docx（放系统临时目录）
            fallback = Path(tempfile.gettempdir()) / f"kwey-doc-{key}.docx"
            shutil.copyfile(converted, fallback)
            return fallback
    return cached


def doc_to_html(abs_path: Path, cache_dir: Optional[Path] = None) -> str:
    converted = convert_doc_to_docx(abs_path, cache_dir)
    return docx_to_html(converted)
