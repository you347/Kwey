"""P1-21 知识脉冲：/api/pulse 聚合 + agent journal + 字数规则。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from app.agent import journal
from app.agent.auth import TokenStore
from app.index.words import word_count


@pytest.fixture(autouse=True)
def _pulse_auth(client, vault_dir):
    """A4：pulse 需 read token，统一注入（本卡测试专注统计逻辑）。"""
    client.headers.update({
        "X-Kwey-Token": TokenStore(vault_dir).issue("read", "pulse-test")["token"]
    })


# ---------------------------------------------------------------- 字数规则
@pytest.mark.parametrize("text,expect", [
    ("", 0),
    ("你好 world foo_bar 123", 5),   # 2 CJK + 3 拉丁词
    ("纯中文三字", 5),
    ("a-b c", 3),                    # a,b,c（连字符切词）
])
def test_word_count_matches_frontend(text, expect):
    assert word_count(text) == expect


# ---------------------------------------------------------------- pulse 聚合
def test_pulse_shape_and_counts(client):
    r = client.get("/api/pulse")
    assert r.status_code == 200
    d = r.json()["data"]
    for key in ("today_words", "streak_days", "orphan_count", "broken_count",
                "note_count", "today_notes", "agent_recent"):
        assert key in d
    # conftest：会议记录.md 无出入链 → 孤儿 1；![[pic.png]] 已解析不算断链
    assert d["orphan_count"] == 1
    assert d["note_count"] == 3
    assert d["streak_days"] >= 1          # fixture 刚写入，mtime=今天
    assert d["today_words"] > 0
    assert isinstance(d["agent_recent"], list)


def test_pulse_streak_gap(client, services):
    db = services.require_db()
    old = (datetime.now() - timedelta(days=5)).timestamp()
    with db.conn:
        db.conn.execute("UPDATE notes SET mtime=?", (old,))
    d = client.get("/api/pulse").json()["data"]
    assert d["streak_days"] == 0          # 断档 5 天
    assert d["today_words"] == 0


def test_pulse_streak_yesterday(client, services):
    db = services.require_db()
    y = (datetime.now() - timedelta(days=1)).timestamp()
    with db.conn:
        db.conn.execute("UPDATE notes SET mtime=?", (y,))
    d = client.get("/api/pulse").json()["data"]
    assert d["streak_days"] == 1          # 止于昨天仍计 1


# ---------------------------------------------------------------- journal
def test_journal_append_cap_and_recent(tmp_path):
    root = tmp_path / "v"
    (root / ".kwey").mkdir(parents=True)
    for i in range(journal.MAX_ENTRIES + 20):
        journal.append(root, {"action": "create", "path": f"n{i}.md"})
    lines = (root / ".kwey" / journal.FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == journal.MAX_ENTRIES
    rec = journal.recent(root, 3)
    assert [r["path"] for r in rec] == [f"n{journal.MAX_ENTRIES + 19 - i}.md" for i in range(3)]


def test_journal_write_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # .kwey 父路径是文件 → mkdir 抛错；append 必须吞掉
    journal.append(blocker, {"action": "create", "path": "a.md"})
    assert journal.recent(blocker, 5) == []


def test_agent_write_lands_in_journal_and_pulse(client, vault_dir):
    token = TokenStore(vault_dir).issue("write", "pulse-agent")["token"]
    h = {"X-Kwey-Token": token}
    r = client.post("/api/agent/note", headers=h,
                    json={"path": "agent写.md", "content": "# agent写\n\n正文"})
    assert r.status_code == 200
    d = client.get("/api/pulse").json()["data"]
    assert d["agent_recent"][0]["action"] == "create"
    assert d["agent_recent"][0]["name"] == "pulse-agent"
    assert d["agent_recent"][0]["path"] == "agent写.md"