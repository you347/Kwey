"""纯文本 / CSV 写回：编码/BOM/换行保留、字节哈希基线、乐观锁、无 frontmatter 注入。"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from app.vault.service import detect_text_storage, encode_text_storage


class TestStorageCodec:
    def test_detect_utf8_lf(self):
        assert detect_text_storage("a\nb\n".encode("utf-8")) == ("utf-8", False, "\n")

    def test_detect_utf8_bom_crlf(self):
        assert detect_text_storage(b"\xef\xbb\xbfa\r\nb") == ("utf-8", True, "\r\n")

    def test_detect_gbk(self):
        data = "中文测试".encode("gbk")
        assert detect_text_storage(data) == ("gb18030", False, "\n")

    def test_encode_keeps_bom_and_newline(self):
        assert encode_text_storage("a\nb", "utf-8", True, "\r\n") == b"\xef\xbb\xbfa\r\nb"

    def test_encode_normalizes_mixed_newlines(self):
        assert encode_text_storage("a\r\nb\rc", "utf-8", False, "\n") == b"a\nb\nc"


class TestPlainTextService:
    def test_read_and_write_utf8_lf(self, vault):
        root = vault.paths.ensure_root()
        (root / "a.txt").write_bytes("第一行\n第二行\n".encode("utf-8"))

        data = vault.read_plain_text("a.txt")
        assert data["content"] == "第一行\n第二行\n"
        assert (data["encoding"], data["bom"], data["newline"]) == ("utf-8", False, "\n")
        assert data["format"] == "txt"

        result = vault.write_plain_text("a.txt", "新内容\n第二行\n", base_mtime=data["mtime"])
        assert result["updated"] is True
        assert (root / "a.txt").read_bytes() == "新内容\n第二行\n".encode("utf-8")

    def test_write_preserves_crlf_and_bom(self, vault):
        root = vault.paths.ensure_root()
        (root / "crlf.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")

        data = vault.read_plain_text("crlf.txt")
        assert data["bom"] is True and data["newline"] == "\r\n"
        vault.write_plain_text("crlf.txt", "one2\ntwo2\n", base_mtime=data["mtime"])
        assert (root / "crlf.txt").read_bytes() == b"\xef\xbb\xbfone2\r\ntwo2\r\n"

    def test_write_preserves_gbk(self, vault):
        root = vault.paths.ensure_root()
        (root / "gbk.csv").write_bytes("名称,数量\r\n苹果,3\r\n".encode("gbk"))

        data = vault.read_plain_text("gbk.csv")
        assert data["encoding"] == "gb18030"
        assert data["content"].startswith("名称,数量")

        result = vault.write_plain_text("gbk.csv", "名称,数量\n香蕉,5\n", base_mtime=data["mtime"])
        assert result["encoding"] == "gb18030"
        assert (root / "gbk.csv").read_bytes() == "名称,数量\r\n香蕉,5\r\n".encode("gbk")

    def test_no_frontmatter_injection(self, vault):
        root = vault.paths.ensure_root()
        (root / "plain.txt").write_bytes(b"hello\n")

        data = vault.read_plain_text("plain.txt")
        vault.write_plain_text("plain.txt", "# 标题\n正文\n", base_mtime=data["mtime"])
        text = (root / "plain.txt").read_text(encoding="utf-8")
        assert text == "# 标题\n正文\n"
        assert "---" not in text

    def test_base_mtime_conflict(self, vault):
        root = vault.paths.ensure_root()
        (root / "c.txt").write_bytes(b"x\n")
        data = vault.read_plain_text("c.txt")

        with pytest.raises(HTTPException) as exc:
            vault.write_plain_text("c.txt", "y\n", base_mtime=data["mtime"] - 60)
        assert exc.value.status_code == 409

    def test_rejects_non_plain_ext(self, vault):
        root = vault.paths.ensure_root()
        (root / "x.md").write_text("# hi", encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            vault.read_plain_text("x.md")
        assert exc.value.status_code == 415
        with pytest.raises(HTTPException) as exc2:
            vault.write_plain_text("x.md", "# bye")
        assert exc2.value.status_code == 415

    def test_missing_file_404(self, vault):
        with pytest.raises(HTTPException) as exc:
            vault.read_plain_text("nope.txt")
        assert exc.value.status_code == 404

    def test_provenance_uses_bytes_hash(self, vault):
        root = vault.paths.ensure_root()
        (root / "h.txt").write_bytes(b"hash me\n")
        data = vault.read_plain_text("h.txt")

        vault.write_plain_text("h.txt", "hash me\nmore\n", base_mtime=data["mtime"])
        entry = vault.provenance.get_baseline("h.txt")
        assert entry is not None
        assert entry["hash_kind"] == "bytes"
        assert entry["payload_hash"] == hashlib.sha256((root / "h.txt").read_bytes()).hexdigest()

    def test_attachment_registered(self, services, vault):
        root = vault.paths.ensure_root()
        (root / "log.txt").write_bytes(b"line\n")
        data = vault.read_plain_text("log.txt")

        vault.write_plain_text("log.txt", "line2\n", base_mtime=data["mtime"])
        linkgraph = services.require_linkgraph()
        assert linkgraph.resolve("log.txt") == "log.txt"


class TestPlainTextApi:
    def test_get_and_put_roundtrip(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "接口.txt").write_bytes("旧内容\n".encode("utf-8"))

        resp = client.get("/api/plain-file", params={"path": "接口.txt"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["content"] == "旧内容\n"

        put = client.put(
            "/api/plain-file",
            json={"path": "接口.txt", "content": "新内容\n", "base_mtime": data["mtime"]},
        )
        assert put.status_code == 200
        assert put.json()["data"]["updated"] is True
        assert (root / "接口.txt").read_bytes() == "新内容\n".encode("utf-8")

    def test_conflict_returns_409(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "冲突.csv").write_bytes("a,b\n".encode("utf-8"))

        resp = client.put(
            "/api/plain-file",
            json={"path": "冲突.csv", "content": "c,d\n", "base_mtime": 1.0},
        )
        assert resp.status_code == 409

    def test_markdown_rejected(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "note.md").write_text("# 标题\n", encoding="utf-8")
        assert client.get("/api/plain-file", params={"path": "note.md"}).status_code == 415

    def test_traversal_rejected(self, client):
        resp = client.get("/api/plain-file", params={"path": "../secret.txt"})
        assert resp.status_code in (400, 404)
