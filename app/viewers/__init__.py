"""非 Markdown 文件的只读预览：抽取 → 转 HTML → 白名单消毒 → 缓存。

产物只落在 ``.kwey/previews/``，属于可丢弃的派生数据：删目录即整库重建。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.paths import AppPaths
from app.viewers import images, render
from app.viewers.extract import (
    IMAGE_EXTS,
    MAX_TEXT_CHARS,
    OFFICE_EXTS,
    PDF_EXTS,
    SUPPORTED_EXTS,
    TEXT_EXTS,
    decode_text,
)

# 渲染逻辑变更时 +1，使旧缓存自动失效
VIEWER_VERSION = 2
MAX_SOURCE_BYTES = 50 * 1024 * 1024
CACHE_DIRNAME = "previews"


class PreviewError(Exception):
    """带 HTTP 语义的预览错误（由路由层转换）。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def preview_cache_dir(paths: AppPaths) -> Path:
    return paths.ensure_root() / ".kwey" / CACHE_DIRNAME


def clear_preview_cache(paths: AppPaths) -> None:
    import shutil

    cache = preview_cache_dir(paths)
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)


def _cache_paths(paths: AppPaths, rel: str) -> Tuple[Path, Path]:
    key = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    base = preview_cache_dir(paths)
    return base / f"{key}.json", base / f"{key}.html"


def _cache_meta(abs_path: Path) -> Dict[str, Any]:
    stat = abs_path.stat()
    return {
        "rel": None,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "version": VIEWER_VERSION,
    }


def _read_cache(abs_path: Path, rel: str, meta_file: Path, html_file: Path) -> Optional[str]:
    if not (meta_file.is_file() and html_file.is_file()):
        return None
    try:
        cached = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = _cache_meta(abs_path)
    expected["rel"] = rel
    if cached != expected:
        return None
    try:
        return html_file.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_cache(abs_path: Path, rel: str, meta_file: Path, html_file: Path, html: str) -> None:
    meta = _cache_meta(abs_path)
    meta["rel"] = rel
    try:
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        for target, payload in (
            (html_file, html),
            (meta_file, json.dumps(meta, ensure_ascii=False)),
        ):
            tmp = target.with_name(target.name + ".tmp")
            # 按字节写入：避免 Windows 文本模式把缓存里的 \n 再翻成 \r\n
            tmp.write_bytes(payload.encode("utf-8"))
            os.replace(tmp, target)
    except OSError:
        # 缓存失败不影响本次预览
        pass


def _build_html(abs_path: Path, ext: str) -> str:
    if ext in {".csv", ".tsv"}:
        return render.csv_to_html(abs_path.read_bytes())
    if ext in TEXT_EXTS:
        text, _enc = decode_text(abs_path.read_bytes())
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return render.text_to_html(text, truncated=len(text) >= MAX_TEXT_CHARS)
    if ext == ".docx":
        return render.docx_to_html(abs_path)
    if ext == ".xlsx":
        return render.xlsx_to_html(abs_path)
    if ext == ".xls":
        return render.xls_to_html(abs_path)
    raise PreviewError(415, "暂不支持该格式预览，可下载后用本地应用打开")


def preview_file(paths: AppPaths, rel: str, *, refresh: bool = False) -> Dict[str, Any]:
    """生成/读取只读预览。返回 kind=html|image|pdf；url 由路由层补齐。"""
    abs_path = paths.to_abs(rel)
    if not abs_path.is_file():
        raise PreviewError(404, f"文件不存在: {rel}")
    ext = abs_path.suffix.lower()
    size = abs_path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        limit_mb = MAX_SOURCE_BYTES // (1024 * 1024)
        raise PreviewError(413, f"文件超过 {limit_mb}MB，请下载后用本地应用查看")

    base: Dict[str, Any] = {
        "format": ext.lstrip("."),
        "title": abs_path.name,
        "size": size,
    }
    if ext in IMAGE_EXTS:
        # 图片不参与全文索引：显式标注「不适用」，避免文件树出现未索引角标
        index_status = {"state": "not_applicable", "detail": "图片不参与全文索引"}
        try:
            images.image_display_path(abs_path, preview_cache_dir(paths))
        except images.ImageConversionError as exc:
            return {
                **base,
                "kind": "unsupported",
                "degraded": True,
                "reason": f"该图片格式暂不支持应用内预览（{exc}）；可下载后用本地应用查看。",
                "index_status": index_status,
            }
        width, height = images.image_size(abs_path) or (None, None)
        return {
            **base,
            "kind": "image",
            "width": width,
            "height": height,
            "converted": ext in images.CONVERT_IMAGE_EXTS,
            "svg": ext == ".svg",
            "index_status": index_status,
        }
    if ext in PDF_EXTS:
        return {**base, "kind": "pdf"}
    if ext not in SUPPORTED_EXTS:
        raise PreviewError(415, "暂不支持该格式预览，可下载后用本地应用打开")

    meta_file, html_file = _cache_paths(paths, rel)
    if not refresh:
        cached = _read_cache(abs_path, rel, meta_file, html_file)
        if cached is not None:
            return {**base, "kind": "html", "html": cached, "cached": True}

    if ext == ".doc":
        try:
            html = render.doc_to_html(abs_path, preview_cache_dir(paths))
        except render.DocConversionError as exc:
            # 已知格式但缺外部转换器：给出可展示的降级说明（不报 4xx，避免无意义报错）
            reason = (
                "未检测到 LibreOffice/soffice（.doc 转换所需），无法应用内预览或建立全文索引；"
                "可下载后用本地应用打开，或安装 LibreOffice 后点「刷新」。"
                if "未检测到" in str(exc)
                else f".doc 转换失败：{exc}"
            )
            return {**base, "kind": "unsupported", "reason": reason, "degraded": True}
    else:
        html = _build_html(abs_path, ext)

    _write_cache(abs_path, rel, meta_file, html_file, html)
    return {**base, "kind": "html", "html": html, "cached": False}
