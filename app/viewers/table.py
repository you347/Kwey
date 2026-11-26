"""CSV/TSV 无损表格模型：为前端表格编辑提供可安全回写的行列数据。

安全基线：只有「解析 → 用同一分隔符/换行/结尾换行重写 → 与原文逐字一致」才返回模型；
任何引号、换行或分隔符不规则的文件都退回源码编辑，绝不在保存时悄悄改写原文。
"""

from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional, Sequence

from app.viewers.extract import MAX_TABLE_COLS, MAX_TABLE_ROWS

# 候选分隔符；.tsv 优先制表符，其余按常见程度排列
DELIMITERS = (",", ";", "\t", "|")
TSV_DELIMITERS = ("\t", ",", ";", "|")
# 表格视图的输入上限：过大的文本在浏览器里渲染/同步都不划算，退回源码
MAX_TABLE_CHARS = 2_000_000


def _newline_of(content: str) -> str:
    """按占多数的换行判定主换行符（与 detect_text_storage 同规则）。"""
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    cr = content.count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr and cr > lf:
        return "\r"
    return "\n"


def _parse(content: str, delimiter: str) -> List[List[str]]:
    reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter)
    return [list(row) for row in reader]


def serialize_table(
    rows: Sequence[Sequence[str]],
    delimiter: str,
    newline: str = "\n",
    trailing: bool = True,
) -> str:
    """按 RFC4180 QUOTE_MINIMAL 序列化；与前端 csvtable.js 的规则保持一致。"""
    buf = io.StringIO()
    writer = csv.writer(
        buf,
        delimiter=delimiter,
        lineterminator=newline,
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerows([list(row) for row in rows])
    text = buf.getvalue()
    if not trailing and newline and text.endswith(newline):
        text = text[: -len(newline)]
    return text


def build_table(content: str, ext: str) -> Optional[Dict[str, object]]:
    """返回可安全表格化的模型；无法无损往返时返回 None。

    模型：``{delimiter, rows, trailing_newline, row_count, col_count}``
    """
    if not content or len(content) > MAX_TABLE_CHARS:
        return None
    newline = _newline_of(content)
    trailing = content.endswith(newline)
    candidates = TSV_DELIMITERS if ext.lower() == ".tsv" else DELIMITERS
    best = None  # (定位键, 模型)
    for delimiter in candidates:
        try:
            rows = _parse(content, delimiter)
            if not rows:
                continue
            width = len(rows[0])
            # 单列文件无法可靠判断分隔符（任何候选都能“往返成功”），按不安全处理
            if width < 2 or any(len(row) != width for row in rows):
                continue
            if len(rows) > MAX_TABLE_ROWS or width > MAX_TABLE_COLS:
                continue
            if serialize_table(rows, delimiter, newline, trailing) != content:
                continue
        except (csv.Error, ValueError):
            continue
        score = (width, content.count(delimiter))
        if best is None or score > best[0]:
            best = (score, {
                "delimiter": delimiter,
                "rows": rows,
                "trailing_newline": trailing,
                "row_count": len(rows),
                "col_count": width,
            })
    return best[1] if best else None
