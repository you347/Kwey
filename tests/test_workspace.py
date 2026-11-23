"""知识体检 / 快速捕获 / AI 收件箱。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.api import deps
from app.diagnose import scan

FILES = {
    "a.md": """---
tags: [项目]
---
# 甲

链接到 [[b]]。
""",
    "b.md": """---
tags: [项目]
---
# 乙

回到 [[a]]。
""",
    # 孤立 + 无标签
    "orphan.md": """# 孤儿

没人链接我。
""",
    # 断链
    "broken.md": """---
tags: [杂项]
---
# 断的

这里指向 [[根本不存在]]。
""",
    # 空笔记
    "empty.md": """---
tags: [杂项]
---
""",
    # 重复（与 a 几乎相同）
    "copy-of-a.md": """---
tags: [项目]
---
# 甲

链接到 [[b]]。
""",
}


@pytest.fixture
def ws_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    for rel, text in FILES.items():
        (root / rel).write_text(text, encoding="utf-8")
    # 陈旧笔记（mtime 一年前）
    stale = root / "stale.md"
    stale.write_text("---\ntags: [旧]\n---\n# 旧事\n\n很久没动了。\n", encoding="utf-8")
    old = time.time() - 400 * 86400
    os.utime(stale, (old, old))
    return root


@pytest.fixture
def ws_services(ws_vault: Path):
    deps.reset_services()
    deps.init_services(ws_vault, open_now=True)
    yield deps
    deps.reset_services()


@pytest.fixture
def ws_client(ws_services):
    from fastapi.testclient import TestClient

    from app.server import create_app

    with TestClient(create_app()) as client:
        yield client


class TestScan:
    def test_finds_all_categories(self, ws_services):
        data = scan(deps.require_vault().paths.root, deps.require_db(), {})
        summary = data["summary"]
        assert summary["orphans"] >= 1        # orphan.md
        assert summary["broken"] >= 1         # broken.md
        assert summary["empty"] >= 1          # empty.md
        assert summary["stale"] >= 1          # stale.md
        assert summary["untagged"] >= 1
        assert summary["duplicates"] >= 1     # copy-of-a.md
        assert 0 <= data["score"] <= 100

    def test_orphan_detail(self, ws_services):
        data = scan(deps.require_vault().paths.root, deps.require_db(), {})
        orphans = next(i for i in data["issues"] if i["key"] == "orphans")
        assert "orphan.md" in {item["path"] for item in orphans["items"]}

    def test_exclude_dir(self, ws_services):
        data = scan(deps.require_vault().paths.root, deps.require_db(), {"exclude": ["orphan.md"]})
        orphans = next(i for i in data["issues"] if i["key"] == "orphans")
        assert "orphan.md" not in {item["path"] for item in orphans["items"]}

    def test_score_degrades_with_problems(self, ws_services):
        clean = scan(deps.require_vault().paths.root, deps.require_db(), {}, deep=False)["score"]
        # 同一库里 deep 检测到重复，分数只会更低或相等
        deep = scan(deps.require_vault().paths.root, deps.require_db(), {}, deep=True)["score"]
        assert deep <= clean

    def test_api(self, ws_client):
        data = ws_client.get("/api/diagnose", params={"deep": "0"}).json()["data"]
        assert data["total"] >= 6
        assert any(i["key"] == "broken" for i in data["issues"])


class TestFix:
    def test_trash_empty(self, ws_client):
        res = ws_client.post("/api/diagnose/fix", json={"action": "trash", "paths": ["empty.md"]})
        assert res.json()["data"]["fixed"] == 1
        assert not (Path(deps.require_vault().paths.root) / "empty.md").exists()
        assert (Path(deps.require_vault().paths.root) / ".trash" / "empty.md").exists()

    def test_strip_broken(self, ws_client):
        res = ws_client.post("/api/diagnose/fix", json={"action": "strip_broken", "paths": ["broken.md"]})
        assert res.json()["data"]["fixed"] == 1
        text = (Path(deps.require_vault().paths.root) / "broken.md").read_text(encoding="utf-8")
        assert "[[根本不存在]]" not in text
        assert "根本不存在" in text          # 文字保留

    def test_tag(self, ws_client):
        res = ws_client.post("/api/diagnose/fix", json={"action": "tag", "paths": ["orphan.md"], "tag": "待整理"})
        assert res.json()["data"]["fixed"] == 1
        text = (Path(deps.require_vault().paths.root) / "orphan.md").read_text(encoding="utf-8")
        assert "待整理" in text

    def test_rejects_bad_action(self, ws_client):
        assert ws_client.post("/api/diagnose/fix", json={"action": "rm-rf", "paths": ["a.md"]}).status_code == 400

    def test_requires_paths(self, ws_client):
        assert ws_client.post("/api/diagnose/fix", json={"action": "trash", "paths": []}).status_code == 400


class TestCapture:
    def test_capture_creates_inbox_note(self, ws_client):
        res = ws_client.post("/api/capture", json={"text": "周五和张三聊定价\n第二行", "tags": ["会议"]})
        data = res.json()["data"]
        assert data["path"].startswith("inbox/")
        path = Path(deps.require_vault().paths.root) / data["path"]
        text = path.read_text(encoding="utf-8")
        assert "周五和张三聊定价" in text
        assert "captured: true" in text
        assert "会议" in text

    def test_capture_empty_rejected(self, ws_client):
        assert ws_client.post("/api/capture", json={"text": "   "}).status_code == 400

    def test_capture_same_title_dedup(self, ws_client):
        first = ws_client.post("/api/capture", json={"text": "同一句话"}).json()["data"]["path"]
        second = ws_client.post("/api/capture", json={"text": "同一句话"}).json()["data"]["path"]
        assert first != second

    def test_list_inbox(self, ws_client):
        ws_client.post("/api/capture", json={"text": "一条想法"})
        data = ws_client.get("/api/inbox").json()["data"]
        assert data["dir"] == "inbox"
        assert data["total"] >= 1
        assert data["items"][0]["path"].startswith("inbox/")


class TestInbox:
    def test_suggest_without_provider(self, ws_client):
        ws_client.post("/api/capture", json={"text": "整理我"})
        data = ws_client.post("/api/inbox/suggest", json={}).json()["data"]
        assert data["items"]
        assert data["ai"] is False
        assert data["items"][0]["suggestion"]["title"]

    def test_apply_moves_and_renames(self, ws_client):
        path = ws_client.post("/api/capture", json={"text": "待归档的想法"}).json()["data"]["path"]
        res = ws_client.post("/api/inbox/apply", json={"items": [{
            "path": path, "title": "归档后的标题", "folder": "notes", "tags": ["整理"],
        }]})
        data = res.json()["data"]
        assert data["applied"] == 1
        new_path = data["results"][0]["to"]
        assert new_path.startswith("notes/")
        assert "归档后的标题" in new_path
        text = (Path(deps.require_vault().paths.root) / new_path).read_text(encoding="utf-8")
        assert "整理" in text

    def test_apply_missing_file_reports_error(self, ws_client):
        res = ws_client.post("/api/inbox/apply", json={"items": [{"path": "inbox/nope.md", "title": "x"}]})
        data = res.json()["data"]
        assert data["applied"] == 0 and data["errors"]

    def test_empty_inbox(self, ws_client):
        data = ws_client.post("/api/inbox/suggest", json={}).json()["data"]
        assert data["items"] == []
