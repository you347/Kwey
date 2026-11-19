"""T05 笔记/文件树 API 集成测试（统一 {code,data,message} 结构）。"""

from __future__ import annotations


def _body(resp):
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload.keys()) >= {"code", "data", "message"}
    return payload


class TestNoteCrud:
    def test_get_note(self, client):
        payload = _body(client.get("/api/note", params={"path": "欢迎.md"}))
        data = payload["data"]
        assert data["title"] == "欢迎"
        assert "frontmatter" in data and "content" in data and "mtime" in data
        assert data["frontmatter"]["自定义"] == "保留我"

    def test_get_note_404(self, client):
        resp = client.get("/api/note", params={"path": "ghost.md"})
        assert resp.status_code == 404
        assert resp.json()["code"] == 404

    def test_create_read_update_delete(self, client):
        # create
        payload = _body(client.post("/api/note", json={"path": "notes/测试笔记.md", "content": "# 测试"}))
        assert payload["data"]["path"] == "notes/测试笔记.md"
        # duplicate → 409
        resp = client.post("/api/note", json={"path": "notes/测试笔记.md"})
        assert resp.status_code == 409
        # update with mtime optimistic lock
        note = _body(client.get("/api/note", params={"path": "notes/测试笔记.md"}))["data"]
        payload = _body(client.put("/api/note", json={
            "path": "notes/测试笔记.md",
            "content": "# 改过",
            "frontmatter": note["frontmatter"],
            "base_mtime": note["mtime"],
        }))
        assert payload["data"]["updated"] is True
        # conflict → 409
        resp = client.put("/api/note", json={
            "path": "notes/测试笔记.md", "content": "x",
            "frontmatter": {}, "base_mtime": 1.0,
        })
        assert resp.status_code == 409
        # delete → trash
        payload = _body(client.delete("/api/note", params={"path": "notes/测试笔记.md"}))
        assert payload["data"]["trash_path"].startswith(".trash/")

    def test_resolve(self, client):
        data = _body(client.get("/api/note/resolve", params={"target": "2026计划"}))["data"]
        assert data["exists"] is True
        assert data["path"] == "notes/读书/2026计划.md"
        data2 = _body(client.get("/api/note/resolve", params={"target": "不存在"}))["data"]
        assert data2["exists"] is False

    def test_render(self, client):
        data = _body(client.get("/api/note/render", params={"path": "欢迎.md"}))["data"]
        assert "kwey-wikilink" in data["html"]
        assert "kwey-tag" in data["html"]

    def test_embed(self, client):
        data = _body(client.get("/api/note/embed", params={"path": "notes/读书/2026计划.md"}))["data"]
        assert data["exists"] is True
        assert "<h1>" in data["html"] or "<h2>" in data["html"]
        data2 = _body(client.get("/api/note/embed", params={"path": "ghost.md"}))["data"]
        assert data2["exists"] is False

    def test_embed_image(self, client):
        data = _body(client.get("/api/note/embed", params={"path": "attachments/pic.png"}))["data"]
        assert "<img" in data["html"]


class TestFiles:
    def test_create_note_dedup(self, client):
        data = _body(client.post("/api/files/create-note", json={"dir": "notes", "name": "自动命名"}))["data"]
        assert data["path"] == "notes/自动命名.md"
        data2 = _body(client.post("/api/files/create-note", json={"dir": "notes", "name": "自动命名"}))["data"]
        assert data2["path"] == "notes/自动命名 (1).md"

    def test_create_dir(self, client):
        data = _body(client.post("/api/files/create-dir", json={"dir": "", "name": "新目录"}))["data"]
        assert data["path"] == "新目录"

    def test_rename_rewrites_links(self, client):
        data = _body(client.post("/api/files/rename", json={
            "path": "notes/读书/2026计划.md", "new_name": "新年计划",
        }))["data"]
        assert data["new_path"] == "notes/读书/新年计划.md"
        assert data["rewritten_links"] >= 1
        # 引用方已更新
        note = _body(client.get("/api/note", params={"path": "欢迎.md"}))["data"]
        assert "[[新年计划]]" in note["content"]

    def test_move(self, client):
        client.post("/api/files/create-note", json={"dir": "", "name": "待移动"})
        data = _body(client.post("/api/files/move", json={"path": "待移动.md", "new_dir": "notes/工作"}))["data"]
        assert data["new_path"] == "notes/工作/待移动.md"

    def test_tree(self, client):
        tree = _body(client.get("/api/tree"))["data"]
        names = {e["name"] for e in tree}
        assert "欢迎.md" in names and "notes" in names
        assert ".kwey" not in names

    def test_delete_dir_confirm(self, client):
        resp = client.delete("/api/files/dir", params={"path": "notes/工作"})
        assert resp.status_code == 409  # 非空目录未确认
        data = _body(client.delete("/api/files/dir", params={"path": "notes/工作", "confirm": 1}))["data"]
        assert data["trash_path"].startswith(".trash/")


class TestSystem:
    def test_health(self, client):
        data = _body(client.get("/api/health"))["data"]
        assert data["status"] == "ok"
        assert data["indexed"] == 3

    def test_vault_info(self, client):
        data = _body(client.get("/api/vault/info"))["data"]
        assert data["note_count"] == 3

    def test_settings_roundtrip(self, client):
        data = _body(client.put("/api/settings", json={"theme": "dark"}))["data"]
        assert data["theme"] == "dark"
        data2 = _body(client.get("/api/settings"))["data"]
        assert data2["theme"] == "dark"
        resp = client.put("/api/settings", json={"theme": "blue"})
        assert resp.status_code == 400

    def test_fonts_setting_merge(self, client):
        """字号按子键合并：只改一个区域时不能丢其它区域的默认值。"""
        data = _body(client.put("/api/settings", json={"fonts": {"ui": 16}}))["data"]
        assert data["fonts"]["ui"] == 16
        assert data["fonts"]["editor"] == 15  # 默认保留

        data2 = _body(client.put("/api/settings", json={"fonts": {"chat": 15}}))["data"]
        assert data2["fonts"]["ui"] == 16
        assert data2["fonts"]["chat"] == 15
        assert data2["fonts"]["sidebar"] == 13

    def test_attachment_upload_and_serve(self, client):
        resp = client.post(
            "/api/attachments",
            files={"file": ("上传.png", b"\x89PNGdata", "image/png")},
        )
        data = _body(resp)["data"]
        assert data["path"].startswith("attachments/")
        served = client.get(f"/file/{data['path']}")
        assert served.status_code == 200
        served2 = client.get(f"/attachments/{data['path'].split('/', 1)[1]}")
        assert served2.status_code == 200

    def test_traversal_blocked(self, client):
        resp = client.get("/api/note", params={"path": "../secret.md"})
        assert resp.status_code in (400, 404)
