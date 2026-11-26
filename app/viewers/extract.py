"""非 Markdown 文件的正文字抽取（预览与索引共用）。

只做「尽量保住顺序的纯文本」，不保留样式；所有解析器都带体积/行列上限，
避免超大文档或压缩炸弹拖垮服务。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable, List, Tuple

MAX_TEXT_CHARS = 2_000_000
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLS = 200
MAX_SHEETS = 20
MAX_PDF_PAGES = 100

TEXT_EXTS = {
    ".txt", ".text", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".ini", ".toml", ".xml", ".conf", ".env", ".sql", ".py", ".js",
    ".ts", ".css", ".html", ".htm", ".sh", ".ps1",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".ico", ".svg"}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".xlsx", ".xls", ".doc"}
SUPPORTED_EXTS = TEXT_EXTS | IMAGE_EXTS | PDF_EXTS | OFFICE_EXTS


def decode_text(data: bytes) -> Tuple[str, str]:
    """按 UTF-8(BOM) → UTF-8 → GB18030 依次尝试解码；返回 (文本, 编码名)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def _clip(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def csv_rows(data: bytes) -> List[List[str]]:
    """解析 CSV/TSV 为行列表（最多 MAX_TABLE_ROWS 行、每行 MAX_TABLE_COLS 列）。"""
    text, _enc = decode_text(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    rows: List[List[str]] = []
    reader = csv.reader(io.StringIO(text), dialect)
    for row in reader:
        if len(rows) >= MAX_TABLE_ROWS:
            break
        rows.append([str(cell) for cell in row[:MAX_TABLE_COLS]])
    return rows


def _rows_to_text(rows: Iterable[Iterable[object]]) -> str:
    lines: List[str] = []
    total = 0
    for row in rows:
        line = "\t".join("" if cell is None else str(cell) for cell in row)
        lines.append(line)
        total += len(line) + 1
        if total >= MAX_TEXT_CHARS:
            break
    return _clip("\n".join(lines))


def extract_docx_text(abs_path: Path) -> str:
    import mammoth

    with abs_path.open("rb") as fh:
        result = mammoth.extract_raw_text(fh)
    return _clip(result.value or "")


def extract_doc_text(abs_path: Path, cache_dir=None) -> str:
    """老 .doc：先经 LibreOffice 转 docx（带缓存），再按 docx 抽取。"""
    from app.viewers.render import convert_doc_to_docx

    return extract_docx_text(convert_doc_to_docx(abs_path, cache_dir))


def extract_xlsx_text(abs_path: Path) -> str:
    from openpyxl import load_workbook

    parts: List[str] = []
    wb = load_workbook(filename=str(abs_path), read_only=True, data_only=True)
    try:
        for index, sheet in enumerate(wb.worksheets):
            if index >= MAX_SHEETS:
                break
            parts.append(f"[{sheet.title}]")
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(row[:MAX_TABLE_COLS])
                if len(rows) >= MAX_TABLE_ROWS:
                    break
            parts.append(_rows_to_text(rows))
    finally:
        wb.close()
    return _clip("\n".join(parts))


def extract_xls_text(abs_path: Path) -> str:
    import xlrd

    book = xlrd.open_workbook(str(abs_path), on_demand=True)
    parts: List[str] = []
    try:
        for index in range(min(book.nsheets, MAX_SHEETS)):
            sheet = book.sheet_by_index(index)
            parts.append(f"[{sheet.name}]")
            rows = []
            for r in range(min(sheet.nrows, MAX_TABLE_ROWS)):
                rows.append(sheet.row_values(r)[:MAX_TABLE_COLS])
            parts.append(_rows_to_text(rows))
    finally:
        book.release_resources()
    return _clip("\n".join(parts))


def extract_pdf_text(abs_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(abs_path))
    parts: List[str] = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        parts.append(page.extract_text() or "")
        if sum(len(p) for p in parts) >= MAX_TEXT_CHARS:
            break
    return _clip("\n".join(parts))


def extract_text(abs_path: Path, cache_dir=None) -> str:
    """按扩展名抽取纯文本；不支持/失败时返回空串（调用方决定降级）。

    ``cache_dir`` 供 .doc 的 LibreOffice 转换缓存使用（索引与预览共用）。
    """
    ext = abs_path.suffix.lower()
    try:
        if ext in {".csv", ".tsv"}:
            return _clip(_rows_to_text(csv_rows(abs_path.read_bytes())))
        if ext in TEXT_EXTS:
            text, _enc = decode_text(abs_path.read_bytes())
            return _clip(text)
        if ext == ".doc":
            return extract_doc_text(abs_path, cache_dir)
        if ext == ".docx":
            return extract_docx_text(abs_path)
        if ext == ".xlsx":
            return extract_xlsx_text(abs_path)
        if ext == ".xls":
            return extract_xls_text(abs_path)
        if ext == ".pdf":
            return extract_pdf_text(abs_path)
    except Exception:  # noqa: BLE001 - 坏文件不该影响索引/预览整体流程
        return ""
    return ""


def is_supported_ext(suffix: str) -> bool:
    return suffix.lower() in SUPPORTED_EXTS
