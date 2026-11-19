"""卡片 M2-4：git 快照时间线与一键回滚测试。

- 使用 conftest 的 ``vault`` / ``vault_dir`` fixtures（临时目录），绝不污染项目根；
- 使用真实系统 git（环境已具备），全部为同步 ``subprocess.run``，无后台进程残留；
- 直接写临时文件仅用于隔离验证 git 行为，集成路径仍走 ``vault.create_note`` /
  ``vault.write_note`` 统一写路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps, routes_history
from app.events import EventType
from app.vault.snapshots import SnapshotService


def _service(root: Path, vault=None, enabled: bool = True) -> SnapshotService:
    return SnapshotService(root, vault=vault, enabled=enabled)


# ------------------------------------------------------------------ ensure_repo
def test_ensure_repo_and_gitignore(vault_dir: Path):
    svc = _service(vault_dir)
    assert svc.is_available() is True
    svc.ensure_repo()
    assert (vault_dir / ".git").exists()
    lines = (vault_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".kwey/", ".trash/", "*.kwey-tmp"):
        assert entry in lines
    # 幂等：再次 ensure_repo 不重复追加
    svc.ensure_repo()
    assert (vault_dir / ".gitignore").read_text(encoding="utf-8").count("*.kwey-tmp") == 1


def test_ensure_repo_preserves_user_content(vault_dir: Path):
    gi = vault_dir / ".gitignore"
    gi.write_text("# 我的自定义规则\nmysecret.txt", encoding="utf-8")  # 结尾无换行
    svc = _service(vault_dir)
    svc.ensure_repo()
    text = gi.read_text(encoding="utf-8")
    assert "# 我的自定义规则" in text
    assert "mysecret.txt" in text
    for entry in (".kwey/", ".trash/", "*.kwey-tmp"):
        assert entry in text.splitlines()
    # 原内容行未被覆盖，且末尾补换行后再追加缺失条目
    assert text.startswith("# 我的自定义规则\nmysecret.txt\n")


# ------------------------------------------------------------------ commit
def test_commit_no_change_returns_none(vault_dir: Path):
    svc = _service(vault_dir)
    svc.ensure_repo()
    (vault_dir / "a.md").write_text("内容A\n", encoding="utf-8")
    h1 = svc.commit("第一次提交")
    assert h1 and len(h1) >= 4
    assert svc.commit("无改动提交") is None
    assert svc.commit("无改动提交", rel="a.md") is None


# ------------------------------------------------------------------ log/show/rollback 主流程
def test_log_show_status_and_rollback(vault):
    root = Path(vault.paths.root)
    svc = _service(root, vault=vault)
    svc.ensure_repo()

    rel = "快照测试.md"
    vault.create_note(rel, content="第一版内容\n")
    h1 = svc.commit("v1 快照测试")
    vault.write_note(rel, content="第二版内容\n")
    h2 = svc.commit("v2 快照测试", rel=rel)
    assert h1 and h2 and h1 != h2

    logs = svc.log(rel=rel)
    assert len(logs) >= 2
    item = logs[0]
    assert set(item) == {"hash", "short", "ts", "message", "author"}
    assert isinstance(item["ts"], int) and item["ts"] > 0
    assert item["hash"] and item["short"] and item["author"]

    old = svc.show(h1, rel)
    assert "第一版内容" in old
    assert "第二版内容" not in old

    received = []
    vault.eventbus.subscribe(EventType.UPDATED, lambda e: received.append(e))

    other_before = (root / "欢迎.md").read_text(encoding="utf-8")
    result = svc.rollback(h1, rel, vault=vault)

    assert result["path"] == rel
    assert result["hash"] == h1
    assert result["saved_commit"]

    disk = (root / rel).read_text(encoding="utf-8")
    assert "第一版内容" in disk
    assert "第二版内容" not in disk
    # 回滚不得破坏其它文件
    assert (root / "欢迎.md").read_text(encoding="utf-8") == other_before
    # 重索引生效
    assert vault.db.get_note(rel) is not None
    # 事件广播
    assert any(e.path == rel for e in received)

    # git log 中存在 before rollback 的留存 commit
    assert any("before rollback" in e["message"] for e in svc.log())


def test_rollback_without_vault(vault_dir: Path):
    """未注入 vault 时回滚仍工作，只是不重索引 / 不广播。"""
    svc = _service(vault_dir)
    svc.ensure_repo()
    p = vault_dir / "a.md"
    p.write_text("旧内容\n", encoding="utf-8")
    h1 = svc.commit("v1")
    p.write_text("新内容\n", encoding="utf-8")
    svc.commit("v2", rel="a.md")
    res = svc.rollback(h1, "a.md")
    assert res["saved_commit"]
    assert "旧内容" in p.read_text(encoding="utf-8")
    assert "新内容" not in p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ status
def test_status_fields(vault_dir: Path):
    svc = _service(vault_dir)
    assert svc.status() == {"available": True, "repo": False, "head": None, "count": 0}
    svc.ensure_repo()
    (vault_dir / "a.md").write_text("A\n", encoding="utf-8")
    svc.commit("c1")
    (vault_dir / "a.md").write_text("B\n", encoding="utf-8")
    svc.commit("c2")
    s = svc.status()
    assert s["available"] is True
    assert s["repo"] is True
    assert isinstance(s["head"], str) and s["head"]
    assert s["count"] == 2


# ------------------------------------------------------------------ 错误 / 安全
def test_show_missing_raises_404(vault_dir: Path):
    svc = _service(vault_dir)
    svc.ensure_repo()
    (vault_dir / "a.md").write_text("A\n", encoding="utf-8")
    h = svc.commit("c1")
    with pytest.raises(HTTPException) as exc:
        svc.show(h, "不存在.md")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        svc.show("00000000", "a.md")  # 合法 hex 但不存在的对象
    assert exc.value.status_code == 404


def test_invalid_rev_and_path_raise_400(vault_dir: Path):
    svc = _service(vault_dir)
    svc.ensure_repo()
    (vault_dir / "a.md").write_text("A\n", encoding="utf-8")
    svc.commit("c1")
    with pytest.raises(HTTPException) as exc:
        svc.show("--help", "a.md")  # 以 '-' 开头，防命令选项注入
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        svc.show("HEAD", "../越界.md")  # 路径穿越
    assert exc.value.status_code == 400


# ------------------------------------------------------------------ 降级
def test_git_unavailable_degradation(vault_dir: Path, monkeypatch):
    monkeypatch.setattr(SnapshotService, "is_available", lambda self: False)
    svc = _service(vault_dir)
    assert svc.status()["available"] is False
    assert svc.commit("任何提交") is None
    assert svc.log() == []
    svc.ensure_repo()  # 不抛、不初始化
    assert not (vault_dir / ".git").exists()
    with pytest.raises(HTTPException) as exc:
        svc.show("HEAD", "a.md")
    assert exc.value.status_code == 503


def test_disabled_service_is_noop(vault_dir: Path):
    svc = _service(vault_dir, enabled=False)
    svc.ensure_repo()
    assert not (vault_dir / ".git").exists()
    (vault_dir / "a.md").write_text("A\n", encoding="utf-8")
    assert svc.commit("c1") is None
    assert svc.log() == []


# ------------------------------------------------------------------ HTTP 端点冒烟
def test_http_endpoints(vault):
    """用自建 app 挂载本 router（不依赖 server.py 接线），验证统一响应与 confirm 校验。"""
    # 接线后快照默认关闭（不擅自 git init 用户 vault），本用例显式开启
    vault.config.set({"snapshots": {"enabled": True, "auto_commit": True}})
    app = FastAPI()
    app.include_router(routes_history.router)

    svc = deps.require_snapshots()
    rel = "接口测试.md"
    vault.create_note(rel, content="接口版本一\n")
    # 开启 auto_commit 后统一写路径可能已经自动提交；取最新一条快照作为基准
    h = svc.commit("api v1") or svc.log(limit=1)[0]["short"]

    with TestClient(app) as c:
        r = c.get("/api/history/status")
        assert r.status_code == 200 and r.json()["code"] == 0
        assert set(r.json()["data"]) == {"available", "repo", "head", "count"}

        r = c.get("/api/history/log", params={"path": rel})
        assert r.json()["code"] == 0 and len(r.json()["data"]) >= 1

        r = c.get("/api/history/show", params={"hash": h, "path": rel})
        assert r.json()["code"] == 0 and "接口版本一" in r.json()["data"]

        r = c.post("/api/history/rollback", json={"hash": h, "path": rel, "confirm": False})
        assert r.status_code == 400

        r = c.post("/api/history/rollback", json={"hash": h, "path": rel, "confirm": True})
        assert r.status_code == 200 and r.json()["code"] == 0
        assert r.json()["data"]["saved_commit"]
