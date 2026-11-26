"""图片预览：多格式、SVG 消毒、缩略图、文件树类型与「索引不适用」状态。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.viewers.extract import IMAGE_EXTS

RASTER_FORMATS = [
    ("png", "PNG"),
    ("jpg", "JPEG"),
    ("jpeg", "JPEG"),
    ("gif", "GIF"),
    ("webp", "WEBP"),
    ("bmp", "BMP"),
    ("tif", "TIFF"),
    ("tiff", "TIFF"),
    ("avif", "AVIF"),
    ("heic", "HEIF"),
]

NATIVE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".ico"}


def _make_raster(path: Path, fmt: str, size=(320, 180)) -> None:
    if fmt == "HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
    Image.new("RGB", size, (30, 120, 200)).save(path, format=fmt)


def _preview(client, rel: str) -> dict:
    resp = client.get("/api/preview", params={"path": rel})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestImageFormats:
    def test_image_exts_cover_required_formats(self):
        required = {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".avif",
            ".heic",
            ".heif",
            ".svg",
        }
        assert required <= IMAGE_EXTS

    @pytest.mark.parametrize("ext,fmt", RASTER_FORMATS)
    def test_each_raster_format_previews(self, client, vault, ext: str, fmt: str):
        root = vault.paths.ensure_root()
        rel = f"attachments/样例.{ext}"
        _make_raster(root / rel, fmt)

        data = _preview(client, rel)
        assert data["kind"] == "image"
        assert (data["width"], data["height"]) == (320, 180)
        assert data["index_status"]["state"] == "not_applicable"
        assert data["url"].startswith("/api/preview/image?path=")

        resp = client.get(data["url"])
        assert resp.status_code == 200, resp.text
        if ext in {".tif", ".tiff", ".heic"}:
            assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert resp.headers["content-type"].startswith("image/png")
        else:
            assert resp.headers["content-type"].startswith("image/")

        thumb = client.get("/api/preview/thumb", params={"path": rel})
        assert thumb.status_code == 200, thumb.text
        with Image.open(io.BytesIO(thumb.content)) as im:
            assert max(im.size) <= 72

    def test_svg_is_sanitized_and_hardened(self, client, vault):
        root = vault.paths.ensure_root()
        rel = "attachments/危险.svg"
        (root / rel).write_text(
            """<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"
                 viewBox="0 0 240 120">
                 <script>alert('xss')</script>
                 <rect width="240" height="120" fill="#cde" onload="alert('load')"/>
                 <a href="javascript:alert(1)"><text x="10" y="60">点我</text></a>
               </svg>""",
            encoding="utf-8",
        )

        data = _preview(client, rel)
        assert data["kind"] == "image"
        assert (data["width"], data["height"]) == (240, 120)
        assert data["svg"] is True

        resp = client.get(data["url"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "sandbox" in resp.headers["content-security-policy"]
        body = resp.text.lower()
        assert "<svg" in body
        assert "<script" not in body
        assert "onload" not in body
        assert "javascript:" not in body

        thumb = client.get("/api/preview/thumb", params={"path": rel})
        assert thumb.status_code == 200
        assert thumb.headers["content-type"].startswith("image/svg+xml")
        assert "<script" not in thumb.text.lower()

    def test_malformed_svg_degrades_to_download(self, client, vault):
        root = vault.paths.ensure_root()
        rel = "attachments/坏图.svg"
        (root / rel).write_text("<svg><script>", encoding="utf-8")
        data = _preview(client, rel)
        assert data["kind"] == "unsupported"
        assert data["degraded"] is True
        assert "下载" in data["reason"]
        assert data["index_status"]["state"] == "not_applicable"

    def test_unconvertible_image_degrades(self, client, vault, monkeypatch):
        import app.viewers as viewers

        root = vault.paths.ensure_root()
        rel = "attachments/无解码.heic"
        (root / rel).write_bytes(b"not-heic")
        monkeypatch.setattr(
            viewers.images,
            "image_display_path",
            lambda *a, **k: (_ for _ in ()).throw(viewers.images.ImageConversionError("缺解码器")),
        )
        data = _preview(client, rel)
        assert data["kind"] == "unsupported"
        assert "缺解码器" in data["reason"]
        assert "下载" in data["reason"]

    def test_tree_node_type_image_without_badge(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "树图.png").write_bytes((root / "attachments" / "pic.png").read_bytes())

        tree = client.get("/api/tree").json()["data"]
        node = _find(tree, "attachments/树图.png")
        assert node is not None
        assert node["type"] == "image"
        assert "index_state" not in node

    def test_thumbnail_broken_file_returns_415(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "attachments" / "坏缩略图.png").write_bytes(b"not-a-png")
        resp = client.get("/api/preview/thumb", params={"path": "attachments/坏缩略图.png"})
        assert resp.status_code == 415


def _find(nodes, path: str):
    for node in nodes or []:
        if node.get("path") == path:
            return node
        if node.get("type") == "dir":
            found = _find(node.get("children") or [], path)
            if found:
                return found
    return None
