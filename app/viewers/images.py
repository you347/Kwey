"""图片预览支持：格式识别、尺寸读取、服务端转换、SVG 消毒与缩略图。

- 浏览器原生格式直接直出；
- tiff/heic/heif 用 Pillow（HEIC 依赖 pillow-heif）转成 PNG 后缓存；
- SVG 走白名单消毒（去脚本/事件/危险链接）后再直出，并配合 CSP sandbox 双保险。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional, Tuple
from xml.etree import ElementTree as ET

from PIL import Image

try:  # HEIC 可选能力：装了就注册，没装对应格式走降级提示
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001
    pass

# 浏览器可直接渲染的格式（svg 需先消毒）
BROWSER_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
    ".ico",
    ".svg",
}
# 需要服务端转换的格式
CONVERT_IMAGE_EXTS = {".tif", ".tiff", ".heic", ".heif"}

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

IMAGE_CACHE_VERSION = 1
THUMB_SIZE = 72
MAX_RENDER_SIDE = 8192
MAX_IMAGE_PIXELS = 200_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# SVG 中会执行脚本/加载外部文档的标签
_SVG_DANGEROUS_TAGS = {
    "script",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
    "canvas",
}
# href 只允许页内锚点或内嵌位图，避免 javascript:/外部资源
_SAFE_HREF_PREFIXES = ("#", "data:image/")


class ImageConversionError(RuntimeError):
    """图片无法预览/转换（调用方据此降级为提示 + 下载）。"""


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _sha1_key(abs_path: Path) -> str:
    stat = abs_path.stat()
    raw = f"{abs_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{IMAGE_CACHE_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sanitize_svg(raw: bytes) -> bytes:
    """去掉脚本、事件处理器与危险 href；解析失败抛 ImageConversionError。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ImageConversionError("SVG 解析失败") from exc
    if _local_name(root.tag) != "svg":
        raise ImageConversionError("不是有效的 SVG")

    for parent in list(root.iter()):
        for child in list(parent):
            if _local_name(child.tag) in _SVG_DANGEROUS_TAGS:
                parent.remove(child)

    for element in root.iter():
        for attr in list(element.attrib):
            name = _local_name(attr)
            value = element.attrib.get(attr, "")
            if name.startswith("on"):
                del element.attrib[attr]
                continue
            if name == "href" and not value.strip().lower().startswith(_SAFE_HREF_PREFIXES):
                del element.attrib[attr]
                continue
            if name == "style" and "javascript:" in value.lower():
                del element.attrib[attr]
        if _local_name(element.tag) == "style" and element.text:
            if "javascript:" in element.text.lower():
                element.text = ""

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sanitized_svg_path(abs_path: Path, cache_dir: Path) -> Path:
    target = Path(cache_dir) / "svg" / f"{_sha1_key(abs_path)}.svg"
    if target.is_file() and target.stat().st_size > 0:
        return target
    try:
        clean = sanitize_svg(abs_path.read_bytes())
    except OSError as exc:
        raise ImageConversionError("SVG 读取失败") from exc
    _atomic_write(target, clean)
    return target


def _converted_png_path(abs_path: Path, cache_dir: Path) -> Path:
    target = Path(cache_dir) / "images" / f"{_sha1_key(abs_path)}.png"
    if target.is_file() and target.stat().st_size > 0:
        return target
    try:
        with Image.open(abs_path) as im:
            im.seek(0)
            frame = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
            if max(frame.size) > MAX_RENDER_SIDE:
                frame.thumbnail((MAX_RENDER_SIDE, MAX_RENDER_SIDE))
            tmp = target.with_name(target.name + ".tmp")
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.save(tmp, format="PNG", optimize=True)
            os.replace(tmp, target)
    except ImageConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 缺解码器/坏文件统一降级
        raise ImageConversionError(f"图片转换失败: {exc}") from exc
    return target


def image_display_path(abs_path: Path, cache_dir: Path) -> Tuple[Path, str]:
    """返回浏览器可显示的文件与 media type（SVG 已消毒，tiff/heic 已转 PNG）。"""
    ext = abs_path.suffix.lower()
    if ext == ".svg":
        return _sanitized_svg_path(abs_path, cache_dir), "image/svg+xml"
    if ext in BROWSER_IMAGE_EXTS:
        return abs_path, MEDIA_TYPES.get(ext, "application/octet-stream")
    if ext in CONVERT_IMAGE_EXTS:
        return _converted_png_path(abs_path, cache_dir), "image/png"
    raise ImageConversionError("不支持的图片格式")


def _length_px(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$", value, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _svg_size(raw: bytes) -> Optional[Tuple[int, int]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    width = _length_px(root.get("width"))
    height = _length_px(root.get("height"))
    if width and height:
        return int(round(width)), int(round(height))
    view_box = root.get("viewBox") or root.get("viewbox")
    if view_box:
        parts = re.split(r"[\s,]+", view_box.strip())
        if len(parts) == 4:
            try:
                return int(round(float(parts[2]))), int(round(float(parts[3])))
            except ValueError:
                return None
    return None


def image_size(abs_path: Path) -> Optional[Tuple[int, int]]:
    """读取图片宽高；无法确定（如百分比尺寸的 SVG）时返回 None。"""
    ext = abs_path.suffix.lower()
    if ext == ".svg":
        try:
            return _svg_size(abs_path.read_bytes())
        except OSError:
            return None
    try:
        with Image.open(abs_path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 —— 尺寸只是展示信息，失败不影响预览
        return None


def thumbnail_path(abs_path: Path, cache_dir: Path) -> Tuple[Path, str]:
    """文件树缩略图：位图压到 THUMB_SIZE；SVG 返回消毒版由浏览器缩放。"""
    ext = abs_path.suffix.lower()
    if ext == ".svg":
        return _sanitized_svg_path(abs_path, cache_dir), "image/svg+xml"
    target = Path(cache_dir) / "thumbs" / f"{_sha1_key(abs_path)}.png"
    if target.is_file() and target.stat().st_size > 0:
        return target, "image/png"
    try:
        with Image.open(abs_path) as im:
            im.seek(0)
            frame = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
            frame.thumbnail((THUMB_SIZE, THUMB_SIZE))
            tmp = target.with_name(target.name + ".tmp")
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.save(tmp, format="PNG", optimize=True)
            os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001
        raise ImageConversionError(f"缩略图生成失败: {exc}") from exc
    return target, "image/png"
