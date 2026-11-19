"""卡片 F2：每日自动推送测试。

全部 git 调用走 FakeGit 替身（mock subprocess.run），全程无真实网络、无真实 git；
临时 vault 来自 conftest，绝不污染项目根。覆盖：开关持久化、补推判定
（last_push 早于计划时间）、pull --rebase 冲突 paused 且不 push、定时器
排定 / 取消 / 恢复、API 契约与 vault 切换停旧定时器。
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import deps, routes_autopush
from app.server import create_app
from app.vault.autopush import AutoPushService


def epoch(h: int, mi: int, day: int = 14) -> float:
    """固定基准日 2026-11-14 的本地 epoch，避免测试依赖真实时钟。"""
    return datetime(2026, 11, day, h, mi).timestamp()


class FakeGit:
    """subprocess.run 替身：记录 (args, cwd)，按前缀匹配预设响应，默认 rc=0。"""

    def __init__(self) -> None:
        self.calls = []
        self.rules = []

    def set(self, prefix, rc=0, stdout="", stderr=""):
        self.rules.append((tuple(prefix), rc, stdout, stderr))

    def __call__(self, cmd, **kwargs):
        args = tuple(cmd[1:])
        self.calls.append((args, kwargs.get("cwd")))
        for prefix, rc, out, err in self.rules:
            if args[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def find(self, prefix):
        prefix = tuple(prefix)
        return [c for c in self.calls if c[0][: len(prefix)] == prefix]


def make_fake(*, staged=True, pull_rc=0, pull_err="", push_rc=0, push_err="",
              remote="origin\n", porcelain="", version_rc=0) -> FakeGit:
    fake = FakeGit()
    fake.set(["--version"], version_rc, "git version 2.45.0")
    fake.set(["remote"], 0, remote)
    fake.set(["diff", "--cached", "--quiet"], 1 if staged else 0)
    fake.set(["rev-parse", "--short", "HEAD"], 0, "abc1234\n")
    fake.set(["pull"], pull_rc, "", pull_err)
    fake.set(["push"], push_rc, "", push_err)
    fake.set(["status", "--porcelain"], 0, porcelain)
    fake.set(["rebase", "--abort"], 0)
    return fake


@pytest.fixture(autouse=True)
def _stop_timers():
    yield
    routes_autopush.stop_autopush()


@pytest.fixture
def repo_root(vault_dir: Path) -> Path:
    (vault_dir / ".git").mkdir()
    return vault_dir


@pytest.fixture
def svc(repo_root: Path):
    service = AutoPushService(repo_root)
    yield service
    service.stop()


@pytest.fixture
def ap_client(services):
    """自挂 router 的 TestClient：server.py 注册由接线人完成（卡片独占约束）。"""
    app = create_app()
    app.include_router(routes_autopush.router)
    with TestClient(app) as c:
        yield c


def _enable(svc: AutoPushService, last_push=0, time_str="22:00") -> None:
    svc.update_config({"enabled": True, "time": time_str})
    # last_push 由服务维护（API 不可写），单测直接注入历史时间戳
    svc._config["last_push"] = last_push


# ------------------------------------------------------------------ 配置持久化
def test_defaults_when_missing(svc):
    cfg = svc.get_config()
    assert cfg["enabled"] is False
    assert cfg["remote"] == "origin"
    assert cfg["branch"] == "main"
    assert cfg["time"] == "22:00"
    assert cfg["include_dirs"] == []
    assert cfg["last_push"] == 0
    assert not svc.config_path.exists()


def test_update_persists_and_reloads(svc, repo_root):
    svc.update_config({
        "enabled": True, "remote": "gitee", "branch": "master",
        "time": "08:30", "commit_template": "备份 {date}",
        "include_dirs": ["notes", "daily"],
    })
    raw = json.loads(svc.config_path.read_text(encoding="utf-8"))
    assert raw["enabled"] is True
    assert raw["remote"] == "gitee"
    assert raw["branch"] == "master"
    assert raw["time"] == "08:30"
    assert raw["commit_template"] == "备份 {date}"
    assert raw["include_dirs"] == ["notes", "daily"]
    fresh = AutoPushService(repo_root)
    assert fresh.get_config()["remote"] == "gitee"
    assert fresh.get_config()["enabled"] is True


def test_corrupt_config_falls_back(repo_root):
    path = repo_root / ".kwey" / "autopush.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")
    service = AutoPushService(repo_root)
    assert service.get_config()["enabled"] is False
    assert service.get_config()["remote"] == "origin"


def test_update_partial_keeps_others(svc):
    svc.update_config({"remote": "gitee"})
    cfg = svc.get_config()
    assert cfg["remote"] == "gitee"
    assert cfg["branch"] == "main"
    assert cfg["time"] == "22:00"


@pytest.mark.parametrize("updates", [
    {"time": "25:00"}, {"time": "9:00"}, {"time": "22:60"},
    {"branch": "-x"}, {"branch": "a b"}, {"branch": "a..b"},
    {"remote": "o r"}, {"remote": "-x"},
    {"include_dirs": ["../x"]}, {"include_dirs": ["notes/../../x"]},
    {"include_dirs": "notes"},
    {"commit_template": "x" * 201},
])
def test_update_rejects_invalid(svc, updates):
    with pytest.raises(ValueError):
        svc.update_config(updates)
    assert not svc.config_path.exists()


def test_last_push_not_writable(svc):
    svc.update_config({"last_push": 999, "enabled": False})
    assert svc.get_config()["last_push"] == 0


def test_empty_template_falls_back(svc):
    svc.update_config({"commit_template": ""})
    assert svc.get_config()["commit_template"] == "vault backup {date}"


def test_include_dirs_normalized_dedup(svc):
    svc.update_config({"include_dirs": ["notes/", "notes", "daily\\work", ""]})
    assert svc.get_config()["include_dirs"] == ["notes", "daily/work"]


# ------------------------------------------------------------------ 补推判定
def test_catchup_when_last_push_before_slot(svc):
    _enable(svc, last_push=epoch(22, 30, day=13))
    assert svc.needs_catchup(now=epoch(23, 0)) is True


def test_no_catchup_when_pushed_after_slot(svc):
    _enable(svc, last_push=epoch(22, 30))
    assert svc.needs_catchup(now=epoch(23, 0)) is False


def test_catchup_before_today_slot_uses_yesterday(svc):
    _enable(svc, last_push=epoch(21, 0, day=13))
    assert svc.needs_catchup(now=epoch(10, 0)) is True
    svc._config["last_push"] = epoch(22, 30, day=13)
    assert svc.needs_catchup(now=epoch(10, 0)) is False


def test_no_catchup_when_disabled(svc):
    assert svc.needs_catchup(now=epoch(23, 0)) is False


def test_never_pushed_needs_catchup(svc):
    _enable(svc, last_push=0)
    assert svc.needs_catchup(now=epoch(23, 0)) is True


def test_catchup_exact_slot_not_pending(svc):
    _enable(svc, last_push=epoch(22, 0))
    assert svc.needs_catchup(now=epoch(23, 0)) is False


def test_next_run_today_and_tomorrow(svc):
    _enable(svc)
    assert svc.next_run(now=epoch(10, 0)) == epoch(22, 0)
    assert svc.next_run(now=epoch(23, 0)) == epoch(22, 0, day=15)
    svc.update_config({"enabled": False})
    assert svc.next_run(now=epoch(10, 0)) is None


# ------------------------------------------------------------------ 定时器
def test_start_schedules_and_stop_cancels(svc):
    _enable(svc, last_push=int(time.time()))  # 无补推，避免后台线程
    svc.start()
    assert svc._timer is not None and svc._timer.is_alive()
    assert svc.next_run() is not None
    svc.stop()
    assert svc._timer is None


def test_disabled_no_timer(svc):
    svc.update_config({"enabled": False})
    svc.start()
    assert svc._timer is None


def test_update_config_reschedules(svc):
    svc.update_config({"enabled": True})
    assert svc._timer is not None
    svc.update_config({"enabled": False})
    assert svc._timer is None


def test_start_catchup_runs_in_background(svc, monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    _enable(svc, last_push=epoch(22, 30, day=13))
    svc.start()
    deadline = time.time() + 5
    while time.time() < deadline and svc._last_result is None:
        time.sleep(0.02)
    assert svc._last_result is not None
    assert svc._last_result["reason"] == "catchup"
    assert svc._last_result["status"] == "pushed"
    assert fake.find(["push"])


# ------------------------------------------------------------------ 推送流水线
def test_run_success_commits_and_pushes(svc, repo_root, monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "pushed"
    assert result["committed"] is True
    assert result["commit"] == "abc1234"
    assert svc.get_config()["last_push"] == result["ts"]
    raw = json.loads(svc.config_path.read_text(encoding="utf-8"))
    assert raw["last_push"] == result["ts"]
    seq = [c[0][0] for c in fake.calls]
    assert seq.index("add") < seq.index("commit") < seq.index("pull") < seq.index("push")
    assert all(c[1] == repo_root for c in fake.calls)  # git 一律 cwd=vault root
    pull = fake.find(["pull"])[0][0]
    assert "--rebase" in pull and pull[-2:] == ("origin", "main")


def test_run_no_changes_skips_commit(svc, monkeypatch):
    fake = make_fake(staged=False)
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "pushed"
    assert result["committed"] is False
    assert fake.find(["commit"]) == []
    assert fake.find(["push"])


def test_run_conflict_pauses_without_push(svc, monkeypatch):
    fake = make_fake(pull_rc=1, pull_err="CONFLICT (content): Merge conflict in 欢迎.md")
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "paused"
    assert "冲突" in result["message"]
    assert fake.find(["push"]) == []
    assert fake.find(["rebase", "--abort"])
    assert svc._state == "paused"
    assert svc.get_config()["last_push"] == 0


def test_conflict_detected_via_porcelain(svc, monkeypatch):
    fake = make_fake(pull_rc=1, pull_err="error: could not apply abc1234",
                     porcelain="UU 欢迎.md\n")
    monkeypatch.setattr(subprocess, "run", fake)
    assert svc.run()["status"] == "paused"
    assert fake.find(["push"]) == []


def test_conflict_detected_via_rebase_dir(svc, repo_root, monkeypatch):
    (repo_root / ".git" / "rebase-merge").mkdir()
    fake = make_fake(pull_rc=1, pull_err="fatal: unknown error")
    monkeypatch.setattr(subprocess, "run", fake)
    assert svc.run()["status"] == "paused"
    assert fake.find(["push"]) == []


def test_paused_cancels_timer_and_manual_resumes(svc, monkeypatch):
    _enable(svc, last_push=int(time.time()))
    svc.start()
    assert svc._timer is not None
    monkeypatch.setattr(subprocess, "run", make_fake(pull_rc=1, pull_err="CONFLICT x"))
    svc.run(reason="schedule")
    assert svc._state == "paused"
    assert svc._timer is None  # 冲突后不再自动重试
    monkeypatch.setattr(subprocess, "run", make_fake())
    result = svc.run(reason="manual")
    assert result["status"] == "pushed"
    assert svc._timer is not None  # 手动成功后恢复调度


def test_pull_network_error_no_push(svc, monkeypatch):
    fake = make_fake(
        pull_rc=1,
        pull_err="fatal: unable to access 'https://gitee.com/x/y.git/': Could not resolve host",
    )
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "error"
    assert fake.find(["push"]) == []
    assert fake.find(["rebase", "--abort"]) == []
    assert svc.get_config()["last_push"] == 0


def test_first_push_missing_remote_branch(svc, monkeypatch):
    fake = make_fake(pull_rc=1, pull_err="fatal: couldn't find remote ref main")
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "pushed"
    assert fake.find(["push"])


def test_push_failure_keeps_last_push(svc, monkeypatch):
    fake = make_fake(push_rc=1, push_err="remote: Invalid username or password")
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "error"
    assert "push 失败" in result["message"]
    assert svc._state == "error"
    assert svc.get_config()["last_push"] == 0


def test_missing_repo_errors(vault_dir, monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    service = AutoPushService(vault_dir)  # 无 .git
    result = service.run()
    assert result["status"] == "error"
    assert "git 仓库" in result["message"]
    assert fake.find(["push"]) == []


def test_git_unavailable_errors(svc, monkeypatch):
    fake = make_fake(version_rc=1)
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "error"
    assert "未安装 git" in result["message"]
    assert fake.find(["push"]) == []


def test_missing_remote_errors(svc, monkeypatch):
    fake = make_fake(remote="upstream\n")
    monkeypatch.setattr(subprocess, "run", fake)
    result = svc.run()
    assert result["status"] == "error"
    assert "remote 不存在" in result["message"]
    assert fake.find(["push"]) == []


def test_include_dirs_limits_add(svc, monkeypatch):
    svc.update_config({"include_dirs": ["notes", "daily"]})
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    assert svc.run()["status"] == "pushed"
    assert fake.find(["add"])[0][0] == ("add", "-A", "--", "notes", "daily")


def test_commit_template_rendered(svc, monkeypatch):
    svc.update_config({"commit_template": "备份 {date}"})
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    svc.run()
    today = datetime.now().strftime("%Y-%m-%d")
    assert fake.find(["commit"])[0][0] == ("commit", "-m", f"备份 {today}")


def test_manual_run_when_disabled(svc, monkeypatch):
    fake = make_fake()
    monkeypatch.setattr(subprocess, "run", fake)
    assert svc.get_config()["enabled"] is False
    assert svc.run()["status"] == "pushed"  # 手动触发不受开关限制


def test_concurrent_run_skipped(svc):
    svc._run_lock.acquire()
    try:
        result = svc.run()
    finally:
        svc._run_lock.release()
    assert result["status"] == "skipped"


# ------------------------------------------------------------------ API
def test_api_config_defaults(ap_client):
    res = ap_client.get("/api/autopush/config")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    assert body["data"]["enabled"] is False
    assert body["data"]["remote"] == "origin"


def test_api_put_roundtrip(ap_client, vault_dir):
    res = ap_client.put("/api/autopush/config", json={
        "enabled": True, "remote": "gitee", "branch": "master",
        "time": "08:30", "commit_template": "daily {date}",
        "include_dirs": ["notes"],
    })
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled"] is True
    assert data["remote"] == "gitee"
    assert data["include_dirs"] == ["notes"]
    path = vault_dir / ".kwey" / "autopush.json"
    assert json.loads(path.read_text(encoding="utf-8"))["remote"] == "gitee"
    got = ap_client.get("/api/autopush/config").json()["data"]
    assert got["time"] == "08:30"
    # enabled=False 不被部分更新过滤丢弃
    off = ap_client.put("/api/autopush/config", json={"enabled": False}).json()["data"]
    assert off["enabled"] is False


def test_api_put_invalid_time_400(ap_client):
    res = ap_client.put("/api/autopush/config", json={"time": "99:99"})
    assert res.status_code == 400
    assert res.json()["code"] == 400


def test_api_run_conflict_paused(ap_client, vault_dir, monkeypatch):
    (vault_dir / ".git").mkdir()
    fake = make_fake(pull_rc=1, pull_err="CONFLICT (content): Merge conflict in a.md")
    monkeypatch.setattr(subprocess, "run", fake)
    res = ap_client.post("/api/autopush/run")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "paused"
    assert fake.find(["push"]) == []


def test_api_status_fields(ap_client, vault_dir, monkeypatch):
    (vault_dir / ".git").mkdir()
    monkeypatch.setattr(subprocess, "run", make_fake())
    res = ap_client.get("/api/autopush/status")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["state"] == "idle"
    assert data["git_available"] is True
    assert data["repo"] is True
    for key in ("enabled", "next_run", "last_push", "catchup_pending", "scheduled"):
        assert key in data


def test_api_requires_vault():
    deps.reset_services()
    try:
        app = create_app()
        app.include_router(routes_autopush.router)
        with TestClient(app) as c:
            res = c.get("/api/autopush/config")
            assert res.status_code == 400
    finally:
        deps.reset_services()


def test_vault_switch_stops_old_timer(repo_root, tmp_path):
    vault1 = SimpleNamespace(paths=SimpleNamespace(root=repo_root))
    svc1 = routes_autopush._get_autopush(vault1)
    svc1.update_config({"enabled": True})
    assert svc1._timer is not None
    other = tmp_path / "other-vault"
    other.mkdir()
    vault2 = SimpleNamespace(paths=SimpleNamespace(root=other))
    svc2 = routes_autopush._get_autopush(vault2)
    assert svc2 is not svc1
    assert svc1._timer is None  # 旧 vault 定时器已停，不会向旧仓库推送