"""P2-D 知识保鲜与失效语义：日期边界、时区、环、invalid_metadata、as_of 历史模式。

纯规则测试不依赖服务；最后几条用临时 vault 验证检索/RAG 口径一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent import registry
from app.freshness import (
    STATE_CURRENT,
    STATE_EXPIRED,
    STATE_INVALID,
    STATE_NOT_YET,
    STATE_SUPERSEDED,
    STATE_UNKNOWN,
    evaluate,
    inspect_sources,
    rerank,
)

TZ8 = timezone(timedelta(hours=8))


def _notes(mapping):
    return mapping.get, (lambda p: p in mapping)


def _eval(fm, as_of, mapping=None, historical=False):
    lookup, exists = _notes(mapping or {})
    path = "notes/a.md"
    return evaluate(
        path,
        fm,
        as_of=as_of,
        tz=TZ8,
        lookup=lookup,
        exists=exists,
        historical=historical,
    )


# ---------------------------------------------------------------- 日期 / 时区
def test_valid_until_date_is_exclusive_end_of_local_day():
    fm = {"valid_until": "2026-01-01"}
    assert _eval(fm, datetime(2026, 1, 1, 23, 59, 59, tzinfo=TZ8))["state"] == STATE_CURRENT
    assert _eval(fm, datetime(2026, 1, 2, 0, 0, 0, tzinfo=TZ8))["state"] == STATE_EXPIRED
    # 输出规范到次日 00:00 本地时间
    assert _eval(fm, datetime(2026, 1, 2, tzinfo=TZ8))["valid_until"] == (
        "2026-01-02T00:00:00+08:00"
    )


def test_valid_from_date_starts_local_midnight():
    fm = {"valid_from": "2026-01-01"}
    assert _eval(fm, datetime(2025, 12, 31, 23, 59, tzinfo=TZ8))["state"] == STATE_NOT_YET
    assert _eval(fm, datetime(2026, 1, 1, 0, 0, tzinfo=TZ8))["state"] == STATE_CURRENT


def test_timezone_aware_datetime_compares_by_instant():
    # UTC 午夜 = 本地 +08:00 08:00；前一秒仍有效
    fm = {"valid_until": "2026-01-01T00:00:00+00:00"}
    assert _eval(fm, datetime(2026, 1, 1, 7, 59, 59, tzinfo=TZ8))["state"] == STATE_CURRENT
    assert _eval(fm, datetime(2026, 1, 1, 8, 0, 0, tzinfo=TZ8))["state"] == STATE_EXPIRED


def test_naive_datetime_uses_supplied_timezone():
    fm = {"valid_until": "2026-01-01T12:00:00"}
    assert _eval(fm, datetime(2026, 1, 1, 11, 59, tzinfo=TZ8))["state"] == STATE_CURRENT
    assert _eval(fm, datetime(2026, 1, 1, 12, 0, tzinfo=TZ8))["state"] == STATE_EXPIRED


# ---------------------------------------------------------------- 状态 / invalid
def test_unknown_is_not_expired():
    assert _eval({}, datetime(2030, 1, 1, tzinfo=TZ8))["state"] == STATE_UNKNOWN
    assert _eval({"valid_until": "2020-01-01"}, datetime(2030, 1, 1, tzinfo=TZ8))["state"] == STATE_EXPIRED


def test_single_boundary_still_declares_state():
    assert _eval({"valid_from": "2030-01-01"}, datetime(2026, 1, 1, tzinfo=TZ8))["state"] == STATE_NOT_YET
    assert _eval({"valid_until": "2025-01-01"}, datetime(2026, 1, 1, tzinfo=TZ8))["state"] == STATE_EXPIRED


def test_invalid_metadata_when_from_after_until():
    info = _eval(
        {"valid_from": "2026-02-01", "valid_until": "2026-01-01"},
        datetime(2026, 1, 15, tzinfo=TZ8),
    )
    assert info["state"] == STATE_INVALID
    assert any("valid_from" in p for p in info["problems"])


def test_invalid_metadata_when_date_unparseable():
    info = _eval({"valid_until": "不是一个日期"}, datetime(2026, 1, 1, tzinfo=TZ8))
    assert info["state"] == STATE_INVALID
    assert info["problems"]


def test_self_supersede_is_invalid():
    notes = {"notes/a.md": {"superseded_by": "notes/a.md"}}
    info = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    assert info["state"] == STATE_INVALID
    assert any("自身" in p for p in info["problems"])


def test_supersede_cycle_is_invalid():
    notes = {
        "notes/a.md": {"superseded_by": "notes/b.md"},
        "notes/b.md": {"superseded_by": "notes/a.md"},
    }
    info = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    assert info["state"] == STATE_INVALID
    assert any("成环" in p for p in info["problems"])


def test_missing_supersede_target_is_invalid_not_silent():
    notes = {"notes/a.md": {"superseded_by": "notes/不存在.md"}}
    info = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    assert info["state"] == STATE_INVALID
    assert any("不存在" in p for p in info["problems"])


def test_supersede_target_in_reserved_dir_is_invalid():
    notes = {
        "notes/a.md": {"superseded_by": ".trash/old.md"},
        ".trash/old.md": {},
    }
    info = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    assert info["state"] == STATE_INVALID


# ---------------------------------------------------------------- 替代 / 历史模式
def test_supersede_waits_for_target_valid_from():
    notes = {
        "notes/a.md": {"superseded_by": "notes/b.md"},
        "notes/b.md": {"valid_from": "2026-03-01"},
    }
    before = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    after = _eval(notes["notes/a.md"], datetime(2026, 3, 1, tzinfo=TZ8), notes)
    assert before["state"] == STATE_CURRENT
    assert before["supersede_effective"] is False
    assert after["state"] == STATE_SUPERSEDED
    assert after["supersede_effective"] is True


def test_history_as_of_does_not_use_today_status():
    fm = {"valid_until": "2026-01-01"}
    assert _eval(fm, datetime(2025, 6, 1, tzinfo=TZ8))["state"] == STATE_CURRENT
    assert _eval(fm, datetime(2026, 6, 1, tzinfo=TZ8))["state"] == STATE_EXPIRED


def test_history_without_replacement_valid_from_only_notes_relation():
    notes = {
        "notes/a.md": {"superseded_by": "notes/b.md"},
        "notes/b.md": {},
    }
    now = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    past = _eval(
        notes["notes/a.md"],
        datetime(2019, 1, 1, tzinfo=TZ8),
        notes,
        historical=True,
    )
    assert now["state"] == STATE_SUPERSEDED
    assert past["state"] == STATE_CURRENT
    assert past["supersede_effective"] is False
    assert past["superseded_by"] == "notes/b.md"  # 只提示关系


def test_alias_and_anchor_targets_are_normalized():
    notes = {
        "notes/a.md": {"superseded_by": "[[notes/b|新规则]]"},
        "notes/b.md": {"valid_from": "2020-01-01"},
    }
    info = _eval(notes["notes/a.md"], datetime(2026, 1, 1, tzinfo=TZ8), notes)
    assert info["state"] == STATE_SUPERSEDED
    assert info["superseded_by"] == "notes/b"


# ---------------------------------------------------------------- 重排 / 证据检查
class _FakeDB:
    def __init__(self, notes):
        self.notes = notes

    def get_note(self, path):
        return {"path": path, "title": path} if path in self.notes else None

    def get_props(self, path):
        return dict(self.notes.get(path) or {})

    def tags_of_note(self, path):
        return []


def test_rerank_downweights_and_adds_replacement():
    db = _FakeDB({
        "old.md": {"valid_until": "2020-01-01", "superseded_by": "new.md"},
        "new.md": {"valid_from": "2020-01-02"},
    })
    hits, summary = rerank(
        [{"path": "old.md", "score": 10.0, "title": "旧"}],
        db,
        as_of=datetime(2026, 1, 1, tzinfo=TZ8),
        downweight=True,
    )
    paths = [h["path"] for h in hits]
    assert paths[0] == "new.md"
    assert summary["downweighted"] == 1
    assert summary["replacements_added"] == 1
    old = [h for h in hits if h["path"] == "old.md"][0]
    assert old["freshness"]["state"] == STATE_SUPERSEDED
    new = [h for h in hits if h["path"] == "new.md"][0]
    assert new["replacement_for"] == "old.md"


def test_rerank_disabled_keeps_order_but_annotates():
    db = _FakeDB({"old.md": {"valid_until": "2020-01-01"}})
    hits, summary = rerank(
        [{"path": "old.md", "score": 10.0}],
        db,
        as_of=datetime(2026, 1, 1, tzinfo=TZ8),
        downweight=False,
    )
    assert hits[0]["score"] == 10.0
    assert summary["enabled"] is False
    assert hits[0]["freshness"]["state"] == STATE_EXPIRED


def test_rerank_historical_does_not_downweight_missing_target_valid_from():
    db = _FakeDB({"old.md": {"superseded_by": "new.md"}, "new.md": {}})
    hits, summary = rerank(
        [{"path": "old.md", "score": 10.0}],
        db,
        as_of=datetime(2019, 1, 1, tzinfo=TZ8),
        downweight=True,
        historical=True,
    )
    assert hits[0]["path"] == "old.md"
    assert hits[0]["score"] == 10.0
    assert summary["downweighted"] == 0
    assert summary["replacements_added"] == 0


def test_inspect_sources_limits_to_five_and_reports_truncation():
    db = _FakeDB({f"n{i}.md": {"valid_until": "2020-01-01"} for i in range(8)})
    result = inspect_sources([f"n{i}.md" for i in range(8)], db=db, limit=5)
    assert result["checked"] == 5
    assert result["total"] == 8
    assert result["truncated"] is True
    assert len(result["risks"]) == 5


# ---------------------------------------------------------------- 端到端（临时 vault）
def _create(client, path, content, frontmatter):
    resp = client.post(
        "/api/note",
        json={"path": path, "content": content, "frontmatter": frontmatter},
    )
    assert resp.status_code == 200, resp.text


def test_search_downweights_expired_and_injects_replacement(client):
    _create(
        client,
        "notes/旧规则.md",
        "# 旧规则\n当前规则是旧版",
        {"valid_until": "2020-01-01", "superseded_by": "notes/新规则.md"},
    )
    _create(
        client,
        "notes/新规则.md",
        "# B\n完全无关的占位正文",
        {"valid_from": "2020-01-02"},
    )

    data = client.get("/api/search", params={"q": "当前规则"}).json()["data"]
    assert data["freshness"]["downweighted"] >= 1
    old = [h for h in data["hits"] if h["path"] == "notes/旧规则.md"]
    assert old and old[0]["freshness"]["state"] == STATE_SUPERSEDED
    # 替代证据在截断前补入
    assert any(h["path"] == "notes/新规则.md" for h in data["hits"])


def test_search_as_of_history_keeps_then_valid_rule(client):
    _create(
        client,
        "notes/历史规则.md",
        "# 历史规则\n历史期间有效",
        {"valid_until": "2020-01-01"},
    )
    data = client.get(
        "/api/search", params={"q": "历史规则", "as_of": "2019-06-01"}
    ).json()["data"]
    hit = [h for h in data["hits"] if h["path"] == "notes/历史规则.md"][0]
    assert hit["freshness"]["state"] == STATE_CURRENT

    today = client.get("/api/search", params={"q": "历史规则"}).json()["data"]
    hit_today = [h for h in today["hits"] if h["path"] == "notes/历史规则.md"][0]
    assert hit_today["freshness"]["state"] == STATE_EXPIRED


def test_freshness_endpoint_and_agent_read(client, vault):
    _create(
        client,
        "notes/待替代.md",
        "# 待替代\n正文",
        {"superseded_by": "notes/替代.md"},
    )
    _create(client, "notes/替代.md", "# 替代\n正文", {"valid_from": "2020-01-01"})

    resp = client.get("/api/freshness/note", params={"path": "notes/待替代.md"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == STATE_SUPERSEDED
    assert data["replacement"] == "notes/替代.md"
    assert data["downweight"] is True

    doc = registry.invoke("kwey.read", {"path": "notes/待替代.md"}, vault=vault)
    assert doc["freshness"]["state"] == STATE_SUPERSEDED
    assert doc["freshness"]["superseded_by"] == "notes/替代.md"

    result = registry.invoke(
        "kwey.search", {"query": "待替代", "as_of": "2019-06-01T12:00:00"}, vault=vault
    )
    hit = [h for h in result["hits"] if h["path"] == "notes/待替代.md"][0]
    assert hit["freshness"]["state"] == STATE_CURRENT
    assert result["freshness"]["as_of"].startswith("2019-06-01T12:00:00")


def test_invalid_as_of_is_rejected(client):
    resp = client.get("/api/search", params={"q": "x", "as_of": "不是时间"})
    assert resp.status_code == 400
    resp = client.get("/api/freshness/note", params={"path": "欢迎.md", "as_of": "x"})
    assert resp.status_code == 400
