"""C2 周期笔记单测：ISO 周边界（含跨年/53 周年）、幂等创建、占位符替换、周期内笔记汇总。

路由接线说明：server.py 由集成卡统一 include；本文件用本地 FastAPI 只挂
routes_periodic.router，服务族复用 conftest 的 vault_dir 生命周期。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_periodic import router

WEEK = "/api/periodic/week"
MONTH = "/api/periodic/month"


@pytest.fixture
def api(vault_dir: Path):
    """本地 FastAPI（只挂本卡 router）+ 服务族初始化；结束统一 reset。"""
    deps.reset_services()
    deps.init_services(vault_dir, open_now=True)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client
    deps.reset_services()


def _data(resp) -> dict:
    """断言统一信封并取 data。"""
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0
    assert payload["message"] == "ok"
    return payload["data"]


# --------------------------------------------------------------------- ISO 周边界
class TestWeekPaths:
    def test_iso_week_basic(self, api):
        # 2026-11-11 周三 → ISO 2026-W46（周一 2026-11-09）
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert data["path"] == "periodic/2026-W46.md"
        assert data["prev_path"] == "periodic/2026-W45.md"
        assert data["next_path"] == "periodic/2026-W47.md"

    def test_iso_week_year_crossing(self, api):
        # 年初跨年：2025-12-29（周一）按 ISO 归属 2026-W01
        data = _data(api.get(WEEK, params={"date": "2025-12-29"}))
        assert data["path"] == "periodic/2026-W01.md"
        assert data["prev_path"] == "periodic/2025-W52.md"
        assert data["next_path"] == "periodic/2026-W02.md"
        # 2026-01-01（周四）同样归属 2026-W01
        data = _data(api.get(WEEK, params={"date": "2026-01-01"}))
        assert data["path"] == "periodic/2026-W01.md"

    def test_iso_week_53rd_week(self, api):
        # 2026-01-01 是周四 → 2026 有 53 个 ISO 周；2027-01-01（周五）归属 2026-W53
        data = _data(api.get(WEEK, params={"date": "2027-01-01"}))
        assert data["path"] == "periodic/2026-W53.md"
        assert data["prev_path"] == "periodic/2026-W52.md"
        assert data["next_path"] == "periodic/2027-W01.md"


# --------------------------------------------------------------------- 幂等创建
class TestIdempotent:
    def test_week_create_then_exists(self, api, vault_dir: Path):
        first = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert first["exists"] is False
        assert (vault_dir / "periodic" / "2026-W46.md").is_file()  # 已落盘

        second = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert second["exists"] is True  # 幂等：第二次直接返回
        files = list((vault_dir / "periodic").glob("2026-W46.md"))
        assert len(files) == 1  # 不重复创建

    def test_month_create_then_exists(self, api, vault_dir: Path):
        first = _data(api.get(MONTH, params={"date": "2026-11-05"}))
        assert first["path"] == "periodic/2026-11.md"
        assert first["exists"] is False
        second = _data(api.get(MONTH, params={"date": "2026-11-20"}))
        assert second["exists"] is True

    def test_month_paths_cross_year(self, api):
        data = _data(api.get(MONTH, params={"date": "2026-01-01"}))
        assert data["path"] == "periodic/2026-01.md"
        assert data["prev_path"] == "periodic/2025-12.md"
        assert data["next_path"] == "periodic/2026-02.md"
        data = _data(api.get(MONTH, params={"date": "2026-12-31"}))
        assert data["next_path"] == "periodic/2027-01.md"


# --------------------------------------------------------------------- 占位符
class TestPlaceholders:
    def test_custom_template_replaced(self, api, vault_dir: Path):
        tpl_dir = vault_dir / "templates"
        tpl_dir.mkdir(exist_ok=True)
        (tpl_dir / "week.md").write_text(
            "---\n"
            'title: "{{year}} W{{week}}"\n'
            "tags: [periodic/week]\n"
            "---\n\n"
            "# {{year}} 第 {{week}} 周\n\n"
            "prev=[[{{prev}}]] next=[[{{next}}]]\n"
            "date={{date}} month={{month}}\n",
            encoding="utf-8",
        )
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert data["template"] == "week.md"  # 报告实际使用的模板
        raw = (vault_dir / "periodic" / "2026-W46.md").read_text(encoding="utf-8")
        assert "title: 2026 W46" in raw          # frontmatter 占位符已替换
        assert "# 2026 第 46 周" in raw          # {{year}}=ISO 年 {{week}}=46
        assert "[[periodic/2026-W45]]" in raw    # {{prev}} 不含 .md，双链友好
        assert "[[periodic/2026-W47]]" in raw
        assert "date=2026-11-09" in raw          # 周一
        assert "month=11" in raw
        assert "{{" not in raw                   # 无残留占位符

    def test_builtin_skeleton_when_no_template(self, api, vault_dir: Path):
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert data["template"] is None  # 内置骨架
        raw = (vault_dir / "periodic" / "2026-W46.md").read_text(encoding="utf-8")
        # create_note 会自动补 created/updated frontmatter，骨架标题在其后
        assert "# 2026 年第 46 周" in raw
        assert "[[periodic/2026-W45]]" in raw and "[[periodic/2026-W47]]" in raw


# --------------------------------------------------------------------- 周期笔记列表
class TestNotesList:
    @staticmethod
    def _mk_note(vault, rel: str, created: str) -> None:
        """受控笔记：created 用字符串（safe_dump 会加引号，重新解析仍为 str，可被索引）。"""
        vault.create_note(rel, content=f"{rel} 正文", frontmatter={"created": created})

    def test_notes_within_week(self, api):
        vault = deps.require_vault()
        self._mk_note(vault, "notes/w46内.md", "2026-11-10T09:00:00+08:00")     # W46 内
        self._mk_note(vault, "notes/w45外.md", "2026-11-02T09:00:00+08:00")     # W45 外
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        paths = [n["path"] for n in data["notes"]]
        assert "notes/w46内.md" in paths
        assert "notes/w45外.md" not in paths
        # conftest vault：2026计划/会议记录 frontmatter 无 created/updated → 不计入
        assert "notes/读书/2026计划.md" not in paths
        assert "notes/工作/会议记录.md" not in paths
        assert all(set(n.keys()) == {"path", "title"} for n in data["notes"])

    def test_notes_within_month(self, api, vault_dir: Path):
        # 边界外笔记：直接落盘 fixture（created/updated 均在 10 月；引号保证 YAML 按字符串解析，
        # 否则会被既有 indexer 丢弃），随后重建索引。写路径会强制 updated=now，故周期外笔记
        # 无法经 create_note 造出，只能 fixture 直写。
        (vault_dir / "notes" / "10月外.md").write_text(
            "---\n"
            "created: '2026-10-31T23:00:00+08:00'\n"
            "updated: '2026-10-31T23:30:00+08:00'\n"
            "---\n\n# 十月\n",
            encoding="utf-8",
        )
        deps.init_services(vault_dir, open_now=True)  # 重扫索引（路由运行时取单例，无需重建 client）
        vault = deps.require_vault()
        self._mk_note(vault, "notes/11月内.md", "2026-11-01T00:00:00+08:00")
        data = _data(api.get(MONTH, params={"date": "2026-11-01"}))
        paths = [n["path"] for n in data["notes"]]
        assert "notes/11月内.md" in paths
        assert "notes/10月外.md" not in paths

    def test_updated_in_range_counts(self, api):
        """created 在周期外、updated 在周期内的笔记也计入（契约：created/updated 任一命中）。

        写路径会把 updated 刷新为 now，故查询「今天所在周」：created 固定在过去（任意运行日期
        均不在本周内），updated=now 必在本周内，结果确定性不依赖运行日期。
        """
        vault = deps.require_vault()
        self._mk_note(vault, "notes/旧的.md", "2026-09-01T09:00:00+08:00")
        vault.write_note("notes/旧的.md", content="今天改过")  # updated → now
        today = datetime.now().strftime("%Y-%m-%d")
        data = _data(api.get(WEEK, params={"date": today}))
        assert "notes/旧的.md" in [n["path"] for n in data["notes"]]

    def test_excludes_itself_and_caps_50(self, api):
        vault = deps.require_vault()
        for i in range(55):  # 批量造 55 篇 created 在 W46 内的笔记
            self._mk_note(vault, f"notes/批量{i:02d}.md", "2026-11-10T09:00:00+08:00")
        self._mk_note(vault, "notes/最新的.md", "2026-11-12T10:00:00+08:00")  # 更晚，必进前 50
        # 第二次请求：周期笔记已在索引内，须被排除；且上限 50
        _data(api.get(WEEK, params={"date": "2026-11-11"}))
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        paths = [n["path"] for n in data["notes"]]
        assert len(paths) == 50
        assert "periodic/2026-W46.md" not in paths
        assert "notes/最新的.md" in paths


# --------------------------------------------------------------------- 参数与信封
class TestContract:
    def test_response_shape(self, api):
        data = _data(api.get(WEEK, params={"date": "2026-11-11"}))
        assert set(data.keys()) == {"path", "exists", "template", "prev_path", "next_path", "notes"}

    def test_invalid_date_400(self, api):
        assert api.get(WEEK, params={"date": "2026/11/11"}).status_code == 400
        assert api.get(MONTH, params={"date": "not-a-date"}).status_code == 400

    def test_default_date_today(self, api):
        # 不传 date → 今天；只验证 200 且路径合法，不绑定具体日期
        data = _data(api.get(WEEK))
        assert data["path"].startswith("periodic/") and data["path"].endswith(".md")
