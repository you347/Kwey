"""F3 个人信息库：三分区读写 / 50/30/20 配额 / 去重 / 衰减 / 闭环更新 / REST。

覆盖契约（A7）：
1. ``.kwey/memory/`` 三分区普通 md 的读写（用户手写条目与 frontmatter 条目都能解析）；
2. ``inject_budget`` 按 50/30/20 配额取条目（配额是比例，不是文件字面切分）；
3. ``update_from_interaction`` 的提取 / 去重合并 / 衰减；
4. ``/api/memory`` 四个端点的统一响应与错误码。

说明：``routes_memory`` 尚未挂到 ``app/server.py``（共享文件由集成卡改），
因此 API 用例用「只挂本路由的 FastAPI 应用」验证路由本身的契约。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_memory import router as memory_router
from app.memory.store import (
    DEFAULT_BUDGET,
    SECTION_QUOTAS,
    SECTIONS,
    MemoryStore,
)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def store(vault_dir: Path) -> MemoryStore:
    """直接操作临时 vault 的记忆库（不依赖 FastAPI）。"""
    return MemoryStore(vault_dir)


@pytest.fixture
def mem_client(services) -> TestClient:
    """只挂记忆路由的应用（services 保证 deps 里有 vault）。"""
    app = FastAPI()
    app.include_router(memory_router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def now() -> datetime:
    return datetime.now().astimezone()


# ------------------------------------------------------------------ 三分区读写
class TestSections:
    def test_missing_files_are_empty_and_readonly(self, store: MemoryStore):
        """读不创建文件：用户没写过记忆时磁盘保持干净。"""
        for section in SECTIONS:
            assert store.read(section) == []
            assert not store.path(section).exists()

        snap = store.snapshot()
        assert set(snap["sections"]) == set(SECTIONS)
        assert snap["quotas"] == SECTION_QUOTAS
        assert snap["dir"] == ".kwey/memory"
        assert snap["default_budget"] == DEFAULT_BUDGET
        assert snap["sections"]["projects"]["percent"] == 50
        assert snap["sections"]["profile"]["percent"] == 30
        assert snap["sections"]["lessons"]["percent"] == 20

    def test_write_creates_plain_markdown_with_metadata(self, store: MemoryStore):
        store.replace(
            "profile",
            [
                {"content": "用户偏好简洁直接的沟通风格", "weight": 1.5, "category": "preference"},
                "在做 Kwey 个人知识库",
            ],
        )
        path = store.path("profile")
        assert path.relative_to(path.parents[2]) == Path(".kwey/memory/profile.md")
        text = path.read_text(encoding="utf-8")
        # 普通 md：frontmatter + 可读标题 + 列表条目（元数据在 HTML 注释里）
        assert "section: profile" in text
        assert "# 用户画像" in text
        assert "- 用户偏好简洁直接的沟通风格" in text
        assert "weight: 1.5" in text
        assert "<!--" in text and "-->" in text

        entries = store.read("profile")
        assert [e.content for e in entries] == ["用户偏好简洁直接的沟通风格", "在做 Kwey 个人知识库"]
        assert entries[0].weight == 1.5
        assert entries[1].weight == 1.0
        assert entries[0].category == "preference"
        assert entries[1].category == "preference"  # 缺省按分区补
        assert entries[0].id.startswith("m-")
        assert entries[0].last_used  # ISO 时间

    def test_user_hand_written_bullets_and_manual_edit(self, store: MemoryStore, vault_dir: Path):
        """用户直接编辑 md（手写 bullet / 行尾注释）必须被解析，且改动即时可见。"""
        path = vault_dir / ".kwey" / "memory" / "lessons.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# 经验教训\n\n"
            "> 这段引用不是条目\n\n"
            "- 先跑测试再改代码\n"
            "- 写完报告要复核 <!-- id: m-deadbeef · category: practice · weight: 2.0"
            " · last_used: 2026-11-01 · source: manual -->\n"
            "1. 带序号的条目也算\n",
            encoding="utf-8",
        )
        entries = store.read("lessons")
        assert [e.content for e in entries] == ["先跑测试再改代码", "写完报告要复核", "带序号的条目也算"]
        assert entries[0].id.startswith("m-")
        assert entries[1].id == "m-deadbeef"          # 手写 id 被尊重
        assert entries[1].weight == 2.0
        assert entries[1].last_used.startswith("2026-11-01")

        # 用户改内容 → 立即反映（无缓存）
        path.write_text("# 经验教训\n\n- 只保留这一条\n", encoding="utf-8")
        assert [e.content for e in store.read("lessons")] == ["只保留这一条"]

    def test_frontmatter_entries_supported(self, store: MemoryStore, vault_dir: Path):
        """兼容 YAML frontmatter 的 entries 列表（程序化/迁移形态）。"""
        path = vault_dir / ".kwey" / "memory" / "projects.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "section: projects\n"
            "entries:\n"
            "  - content: 迁移过来的项目记忆\n"
            "    category: project\n"
            "    weight: 1.2\n"
            "    last_used: 2026-11-10\n"
            "---\n\n"
            "# 项目记忆\n\n"
            "- 手写补充的一条\n",
            encoding="utf-8",
        )
        entries = store.read("projects")
        assert [e.content for e in entries] == ["迁移过来的项目记忆", "手写补充的一条"]
        assert entries[0].weight == 1.2
        assert entries[0].last_used.startswith("2026-11-10")

        # 写回后统一为列表形态，frontmatter 中的 entries 不再保留（内容不丢）
        store.replace("projects", entries)
        text = path.read_text(encoding="utf-8")
        assert "entries:" not in text
        assert "迁移过来的项目记忆" in text
        assert len(store.read("projects")) == 2

    def test_user_notes_around_entries_preserved(self, store: MemoryStore, vault_dir: Path):
        """面板保存只重写 bullet 条目块，用户写的标题/备注/末尾说明原样保留。"""
        path = vault_dir / ".kwey" / "memory" / "lessons.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nsection: lessons\n---\n"
            "# 我的经验本\n\n"
            "这里是我自己的备注，别删\n\n"
            "- 条目A\n\n"
            "末尾备注\n",
            encoding="utf-8",
        )
        store.replace("lessons", ["条目A", "条目B"])
        text = path.read_text(encoding="utf-8")
        assert "这里是我自己的备注，别删" in text
        assert "末尾备注" in text
        assert "条目B" in text
        assert text.count("# 我的经验本") == 1        # 不自作主张叠加默认标题
        assert [e.content for e in store.read("lessons")] == ["条目A", "条目B"]

    def test_custom_frontmatter_preserved(self, store: MemoryStore):
        store.replace("profile", ["第一条"])
        path = store.path("profile")
        text = path.read_text(encoding="utf-8").replace(
            "section: profile", "section: profile\n自定义: 保留我"
        )
        path.write_text(text, encoding="utf-8")
        store.replace("profile", ["第二条"])
        assert "自定义: 保留我" in path.read_text(encoding="utf-8")

    def test_memory_dir_never_indexed(self, store: MemoryStore, services, vault_dir: Path):
        """``.kwey`` 不进索引/笔记树：记忆文件不是用户笔记。"""
        store.replace("projects", ["记忆不应该进索引"])
        indexer = services.require_indexer()
        indexer.full_scan()
        db = services.require_db()
        notes = db.all_notes()
        assert all(not n["path"].startswith(".kwey/") for n in notes)
        assert db.get_note(".kwey/memory/projects.md") is None

        paths: list[str] = []

        def walk(nodes):
            for node in nodes:
                paths.append(node["path"])
                walk(node.get("children") or [])

        walk(services.require_vault().tree())
        assert not any(p.startswith(".kwey") for p in paths)

    def test_delete_entries_via_replace(self, store: MemoryStore):
        store.replace("lessons", ["A", "B", "C"])
        payload = store.replace("lessons", ["A", "C"])
        assert [e["content"] for e in payload["entries"]] == ["A", "C"]
        assert payload["count"] == 2


# ------------------------------------------------------------------ 配额注入
class TestQuotaInjection:
    def _fill(self, store: MemoryStore, now: datetime, per_section: int = 40) -> None:
        for index, section in enumerate(SECTIONS):
            items = [
                {
                    "content": f"{section}-{i} " + "内容" * 20,
                    "weight": 1.0,
                    "last_used": now.isoformat(),
                }
                for i in range(per_section)
            ]
            store.replace(section, items, now=now)

    def test_quota_is_ratio_not_file_slice(self, store: MemoryStore, now: datetime):
        self._fill(store, now)
        budget = 3000
        data = store.build_injection(budget, now=now)
        assert len(data["text"]) <= budget
        # 每个分区都不超过自己的配额
        for section, ratio in SECTION_QUOTAS.items():
            assert data["sections"][section]["used"] <= int(budget * ratio)
            assert data["sections"][section]["used"] > 0
        used = [data["sections"][s]["used"] for s in SECTIONS]
        assert used[0] > used[1] > used[2], used          # 50% > 30% > 20%
        assert data["text"].startswith("## 项目记忆（50%）")
        assert "## 用户画像（30%）" in data["text"]
        assert "## 经验教训（20%）" in data["text"]

    def test_inject_budget_returns_string(self, store: MemoryStore, now: datetime):
        self._fill(store, now, per_section=3)
        text = store.inject_budget(800)
        assert isinstance(text, str)
        assert 0 < len(text) <= 800
        assert "\n- " in text

    def test_long_entry_truncated_within_budget(self, store: MemoryStore, now: datetime):
        store.replace(
            "projects",
            [{"content": "超长记忆" + "字" * 1500, "last_used": now.isoformat()}],
            now=now,
        )
        data = store.build_injection(400, now=now)
        assert len(data["text"]) <= 400
        assert data["text"].rstrip().endswith("…")
        assert data["sections"]["projects"]["used"] <= int(400 * 0.5)

    def test_empty_store_returns_empty_text(self, store: MemoryStore):
        data = store.build_injection(1000)
        assert data["text"] == ""
        assert data["used"] == 0
        assert data["entry_count"] == 0

    def test_invalid_budget_rejected(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.build_injection(10)
        with pytest.raises(ValueError):
            store.build_injection(999999)

    def test_ranking_by_weight_and_decay(self, store: MemoryStore, now: datetime):
        old = (now - timedelta(days=60)).isoformat()
        fresh = now.isoformat()
        store.replace(
            "projects",
            [
                {"content": "旧的高权重记忆", "weight": 1.2, "last_used": old},
                {"content": "新的普通记忆", "weight": 1.0, "last_used": fresh},
            ],
            now=now,
        )
        entries = {e.content: e for e in store.read("projects")}
        old_score = store.score(entries["旧的高权重记忆"], "projects", now)
        new_score = store.score(entries["新的普通记忆"], "projects", now)
        assert new_score > old_score  # 项目记忆半衰期 7 天：60 天前显著衰减
        data = store.build_injection(600, now=now)
        assert data["sections"]["projects"]["entries"][0]["content"] == "新的普通记忆"

    def test_touch_refreshes_last_used_once_per_day(self, store: MemoryStore, now: datetime):
        """注入即使用：touch=True 刷新 last_used 并升权，同一天不重复升权。"""
        old = (now - timedelta(days=10)).isoformat()
        store.replace(
            "lessons", [{"content": "复用经验", "weight": 1.0, "last_used": old}], now=now
        )
        # 默认预览只读
        store.build_injection(600, now=now)
        assert store.read("lessons")[0].weight == 1.0

        store.build_injection(600, touch=True, now=now)
        entry = store.read("lessons")[0]
        assert entry.weight == 1.05
        assert entry.last_used.startswith(now.date().isoformat())

        # 同一天再注入：不重复升权
        store.build_injection(600, touch=True, now=now)
        assert store.read("lessons")[0].weight == 1.05

    def test_redistribute_uses_leftover(self, store: MemoryStore, now: datetime):
        store.replace(
            "projects",
            [{"content": f"项目条目 {i} " + "细" * 10, "last_used": now.isoformat()} for i in range(40)],
            now=now,
        )
        strict = store.build_injection(600, now=now)
        loose = store.build_injection(600, redistribute=True, now=now)
        assert loose["used"] > strict["used"]
        assert loose["sections"]["projects"]["used"] > strict["sections"]["projects"]["used"]
        assert loose["used"] <= 600


# ------------------------------------------------------------------ 去重 / 衰减
class TestDedupeAndDecay:
    def test_upsert_merges_similar_content(self, store: MemoryStore, now: datetime):
        first = store.upsert("profile", "用户偏好简洁的沟通风格", weight=1.0, now=now)
        assert first["merged"] is False and first["count"] == 1
        second = store.upsert(
            "profile", "用户偏好简洁的沟通风格！", weight=1.0, now=now + timedelta(minutes=1)
        )
        assert second["merged"] is True and second["count"] == 1
        entries = store.read("profile")
        assert len(entries) == 1
        assert entries[0].weight == 1.1      # 命中重复 → 轻微升权
        assert entries[0].id == first["id"]

    def test_upsert_keeps_distinct_entries(self, store: MemoryStore, now: datetime):
        store.upsert("lessons", "改代码前先跑测试", weight=1.0, now=now)
        store.upsert("lessons", "写中文注释解释取舍", weight=1.0, now=now)
        assert len(store.read("lessons")) == 2

    def test_upsert_rejects_blank(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.upsert("profile", "   ")

    def test_replace_dedupes_payload(self, store: MemoryStore, now: datetime):
        payload = store.replace(
            "profile",
            ["同一个偏好", "同一个偏好。", {"content": "另一个偏好", "weight": 2.0}],
            now=now,
        )
        assert payload["count"] == 2
        weights = {e["content"]: e["weight"] for e in payload["entries"]}
        assert weights["另一个偏好"] == 2.0
        # 归一化后相等（仅标点差异）→ 合并为一条，内容保留信息量更大的写法
        assert weights["同一个偏好。"] == 1.0
        assert "同一个偏好" not in weights

    def test_decay_lowers_and_drops_stale(self, store: MemoryStore, now: datetime):
        store.replace(
            "lessons",
            [
                {"content": "很久以前的低价值经验", "weight": 0.12,
                 "last_used": (now - timedelta(days=400)).isoformat()},
                {"content": "很久以前的高价值经验", "weight": 3.0,
                 "last_used": (now - timedelta(days=400)).isoformat()},
                {"content": "刚发生的经验", "weight": 1.0, "last_used": now.isoformat()},
            ],
            now=now,
        )
        dropped = store.decay(now=now)
        assert dropped["lessons"] == 1
        entries = {e.content: e for e in store.read("lessons")}
        assert "很久以前的低价值经验" not in entries        # 跌破 0.1 → 淘汰
        assert entries["很久以前的高价值经验"].weight == 2.1  # 3.0 × 0.7
        assert entries["刚发生的经验"].weight == 1.0         # 未过期不动

    def test_trim_keeps_strongest_when_overflow(self, store: MemoryStore, now: datetime):
        """自动增长（upsert）超上限时按分数淘汰最弱的；用户手动保存不裁剪。"""
        store.MAX_ENTRIES = 3
        for index, weight in enumerate([0.2, 0.3, 0.4, 0.5, 0.6]):
            store.upsert("profile", f"偏好 {index}", weight=weight, now=now)
        entries = store.read("profile")
        assert len(entries) == 3
        assert {e.content for e in entries} == {"偏好 2", "偏好 3", "偏好 4"}

        # 手动整分区保存 5 条 → 全部保留
        payload = store.replace(
            "profile",
            [{"content": f"手写 {i}", "weight": 1.0, "last_used": now.isoformat()} for i in range(5)],
            now=now,
        )
        assert payload["count"] == 5


# ------------------------------------------------------------------ 闭环更新
class TestClosedLoop:
    OPS = [
        {"type": "create", "path": "notes/新项目笔记.md", "content": "# 新项目笔记\n"},
        {"type": "link", "path": "notes/新项目笔记.md", "target": "2026计划"},
        {"type": "tag", "path": "欢迎.md", "tag": "#项目/进行中"},
        {"type": "delete", "path": "notes/旧笔记.md"},
        {"type": "unknown", "path": "x.md"},
        "不是对象",
    ]
    DIFF = {
        "applied": 3,
        "paths": ["notes/新项目笔记.md", "欢迎.md"],
        "rewritten_links": 0,
    }

    def test_extracts_and_merges(self, store: MemoryStore, now: datetime):
        result = store.update_from_interaction(self.OPS, self.DIFF, now=now)
        assert result["added"] == 3      # create / link / tag
        assert result["merged"] == 0
        assert result["skipped"] == 3    # delete / unknown / 非对象
        assert result["sections"]["projects"] == 2
        assert result["sections"]["lessons"] == 1
        contents = [e.content for e in store.read("projects")]
        assert "在写《新项目笔记》" in contents
        assert "《新项目笔记》关联《2026计划》" in contents
        assert [e.content for e in store.read("lessons")] == ["整理《欢迎》时使用标签 #项目/进行中"]

    def test_second_run_merges_without_duplicates(self, store: MemoryStore, now: datetime):
        store.update_from_interaction(self.OPS, self.DIFF, now=now)
        again = store.update_from_interaction(
            self.OPS, self.DIFF, now=now + timedelta(minutes=5)
        )
        assert again["added"] == 0 and again["merged"] == 3
        assert len(store.read("projects")) == 2
        assert len(store.read("lessons")) == 1

    def test_only_applied_paths_are_recorded(self, store: MemoryStore, now: datetime):
        ops = [
            {"type": "create", "path": "notes/成功.md"},
            {"type": "create", "path": "notes/失败.md"},
        ]
        diff = {"applied": 1, "paths": ["notes/成功.md"], "rewritten_links": 0}
        result = store.update_from_interaction(ops, diff, now=now)
        assert result["added"] == 1 and result["skipped"] == 1
        assert [e.content for e in store.read("projects")] == ["在写《成功》"]

    def test_move_op_matches_new_path(self, store: MemoryStore, now: datetime):
        """move 的 op.path 是旧路径，落盘结果里是新路径——按 basename 认领，不能误判为未应用。"""
        ops = [{"type": "move", "path": "notes/旧/文章.md", "target": "归档"}]
        diff = {"applied": 1, "paths": ["归档/文章.md"], "rewritten_links": 0}
        result = store.update_from_interaction(ops, diff, now=now)
        assert result["added"] == 1
        assert [e.content for e in store.read("lessons")] == ["《文章》归档到 归档"]

    def test_explicit_candidates_from_provider(self, store: MemoryStore, now: datetime):
        """Provider/LLM 可在 diff_result 里给出语义化候选；非法项只跳过不报错。"""
        diff = {
            "paths": [],
            "memory_candidates": [
                {"section": "profile", "content": "用户偏好中文注释解释取舍", "weight": 1.4},
                {"section": "bogus", "content": "非法分区"},
                {"section": "lessons", "content": "   "},
            ],
        }
        result = store.update_from_interaction([], diff, now=now)
        assert result["added"] == 1
        assert result["skipped"] == 2
        assert store.read("profile")[0].content == "用户偏好中文注释解释取舍"
        assert store.read("profile")[0].weight == 1.4

    def test_empty_ops_still_decays(self, store: MemoryStore, now: datetime):
        store.replace(
            "lessons",
            [{"content": "过期经验", "weight": 0.12,
              "last_used": (now - timedelta(days=400)).isoformat()}],
            now=now,
        )
        result = store.update_from_interaction([], None, now=now)
        assert result["decayed"]["lessons"] == 1
        assert store.read("lessons") == []

    def test_rejects_non_list_ops(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.update_from_interaction({"type": "create"}, None)


# ------------------------------------------------------------------ REST 端点
class TestRoutes:
    def test_get_memory_envelope(self, mem_client: TestClient):
        resp = mem_client.get("/api/memory")
        assert resp.status_code == 200
        payload = resp.json()
        assert set(payload) >= {"code", "data", "message"}
        assert payload["code"] == 0
        data = payload["data"]
        assert set(data["sections"]) == set(SECTIONS)
        assert data["quotas"] == SECTION_QUOTAS

    def test_put_and_get_section(self, mem_client: TestClient, vault_dir: Path):
        resp = mem_client.put(
            "/api/memory/projects",
            json={"entries": [{"content": "在写 Kwey", "weight": 1.4}, "整理实验报告"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["count"] == 2
        assert data["path"] == ".kwey/memory/projects.md"
        assert (vault_dir / ".kwey" / "memory" / "projects.md").is_file()

        one = mem_client.get("/api/memory/projects").json()["data"]
        assert [e["content"] for e in one["entries"]] == ["在写 Kwey", "整理实验报告"]
        assert one["entries"][0]["weight"] == 1.4

    def test_inject_endpoint_budget(self, mem_client: TestClient, now: datetime):
        mem_client.put(
            "/api/memory/profile",
            json={"entries": [{"content": f"偏好 {i}", "weight": 1.0} for i in range(20)]},
        )
        resp = mem_client.post("/api/memory/inject", json={"max_chars": 600})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert 0 < data["used"] <= 600
        assert len(data["text"]) == data["used"]
        assert data["sections"]["profile"]["quota"] == 180

    def test_inject_rejects_budget_out_of_range(self, mem_client: TestClient):
        resp = mem_client.post("/api/memory/inject", json={"max_chars": 5})
        assert resp.status_code == 400
        assert "max_chars" in resp.json()["detail"]

    def test_inject_touch_flag(self, mem_client: TestClient):
        mem_client.put(
            "/api/memory/lessons",
            json={"entries": [{"content": "很久以前的经验", "weight": 1.0,
                               "last_used": "2026-01-01T00:00:00+08:00"}]},
        )
        mem_client.post("/api/memory/inject", json={"max_chars": 600, "touch": True})
        entry = mem_client.get("/api/memory/lessons").json()["data"]["entries"][0]
        assert entry["weight"] == 1.05
        assert entry["last_used"].startswith(datetime.now().astimezone().date().isoformat())

    def test_update_endpoint(self, mem_client: TestClient):
        body = {
            "ops": [{"type": "create", "path": "notes/接口笔记.md"}],
            "diff_result": {"applied": 1, "paths": ["notes/接口笔记.md"]},
        }
        resp = mem_client.post("/api/memory/update", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["added"] == 1
        again = mem_client.post("/api/memory/update", json=body).json()["data"]
        assert again["merged"] == 1

    def test_unknown_section_404(self, mem_client: TestClient):
        assert mem_client.get("/api/memory/unknown").status_code == 404
        assert mem_client.put("/api/memory/unknown", json={"entries": []}).status_code == 404
        assert "未知记忆分区" in mem_client.get("/api/memory/unknown").json()["detail"]

    def test_blank_entry_rejected(self, mem_client: TestClient):
        resp = mem_client.put("/api/memory/profile", json={"entries": ["   "]})
        assert resp.status_code == 400
        assert "为空" in resp.json()["detail"]

    def test_too_long_entry_rejected(self, mem_client: TestClient):
        resp = mem_client.put(
            "/api/memory/lessons", json={"entries": ["长" * (MemoryStore.MAX_CONTENT + 1)]}
        )
        assert resp.status_code == 400
        assert "过长" in resp.json()["detail"]

    def test_memory_files_not_notes_after_api_write(self, mem_client: TestClient, services):
        mem_client.put("/api/memory/lessons", json={"entries": ["REST 写入的经验"]})
        services.require_indexer().full_scan()
        assert services.require_db().get_note(".kwey/memory/lessons.md") is None

    def test_vault_not_open_returns_400(self):
        """vault 未打开时与其它 UI 端点一致：400 + 中文提示。"""
        deps.reset_services()
        app = FastAPI()
        app.include_router(memory_router)
        with TestClient(app) as client:
            resp = client.get("/api/memory")
            assert resp.status_code == 400
            assert "vault 未打开" in resp.json()["detail"]
        deps.reset_services()
