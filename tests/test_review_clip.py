"""C6 验收：F4 复习流水线（review_due 间隔调度）+ 网页剪藏（抽取/Markdown/落盘/SSRF）。

覆盖：
1. 复习：due 筛选（含逾期天数）、again/good/easy 间隔递增、正文与自定义字段保留、
   stats（待复习 / 今日已完成）、schedule / unschedule 扩展、非法参数 400 / 不存在 404。
2. 剪藏：article/main/整页三级抽取、Markdown 映射、标题与 frontmatter 落盘、
   同 URL 去重更新、同名不同 URL 追加序号、preview 不写盘。
3. 安全：SSRF（本机/内网/链路本地/IPv6 回环/非 http scheme/域名解析到内网）在
   发起任何网络请求前被拦截；超限、非 HTML、抽取失败、网络错误分别 400/502，
   且失败不留下半截笔记。

抓取入口 ``routes_clip._open_request`` 被 monkeypatch 成假响应，测试全程不触网。
"""

from __future__ import annotations

import urllib.error
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from app.api import routes_clip
from app.api.routes_clip import MAX_BYTES

TODAY = date.today()
PUBLIC_URL = "http://93.184.216.34/posts/1"  # 公网字面 IP：不触发 DNS 解析

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>认知负荷与笔记法 - 某站点</title>
</head>
<body>
<nav>首页 | 归档 | 关于</nav>
<article>
  <h1>认知负荷与笔记法</h1>
  <p>工作记忆容量有限，<strong>组块化</strong>能降低<em>认知负荷</em>。</p>
  <h2>三条原则</h2>
  <ul><li>先结构后细节</li><li>用自己的话复述</li></ul>
  <p>参考 <a href="/posts/2025/zettel">卡片盒笔记法</a>，示意图
     <img src="/img/zettel.png" alt="卡片盒"/>。</p>
  <pre><code>def note():
    return "atomic"</code></pre>
  <blockquote>笔记的价值在于连接。</blockquote>
</article>
<footer>版权所有 © 某站点</footer>
</body>
</html>
"""


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def api(client):
    """把本卡两个 router 挂到测试 app 上（集成卡统一在 server.py 注册）。"""
    from app.api.routes_clip import router as clip_router
    from app.api.routes_review import router as review_router

    client.app.include_router(review_router)
    client.app.include_router(clip_router)
    return client


class FakeResponse:
    """urllib 响应的最小替身：read / geturl / headers / close。"""

    def __init__(self, body: Any, url: str, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self._url = url
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._body[:size] if size is not None and size >= 0 else self._body

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fetch(monkeypatch):
    """替换抓取入口，返回可调状态的假响应（默认 ARTICLE_HTML）。"""
    state: Dict[str, Any] = {
        "body": ARTICLE_HTML,
        "content_type": "text/html; charset=utf-8",
        "error": None,
        "calls": [],
    }

    def _open(req, timeout):
        state["calls"].append((req.full_url, timeout))
        if state["error"] is not None:
            raise state["error"]
        return FakeResponse(state["body"], req.full_url, state["content_type"])

    monkeypatch.setattr(routes_clip, "_open_request", _open)
    return state


def _seed(vault, path: str, due: Optional[str] = None, interval: Optional[int] = None,
          extra: Optional[Dict[str, Any]] = None) -> str:
    """建一篇带复习字段的笔记（走 VaultService 统一写路径）。"""
    frontmatter: Dict[str, Any] = {"tags": ["学习/生物"], "自定义": "保留我"}
    if due is not None:
        frontmatter["review_due"] = due
    if interval is not None:
        frontmatter["review_interval"] = interval
    frontmatter.update(extra or {})
    vault.create_note(
        path,
        content="# 细胞结构\n\n线粒体是能量工厂，叶绿体负责光合作用。\n",
        frontmatter=frontmatter,
    )
    return path


# ================================================================== 复习：筛选
def test_due_filters_and_sorts(api, vault):
    _seed(vault, "notes/复习/早到期.md", due=(TODAY - timedelta(days=3)).isoformat(), interval=2)
    _seed(vault, "notes/复习/今天到期.md", due=TODAY.isoformat(), interval=4)
    _seed(vault, "notes/复习/未来到期.md", due=(TODAY + timedelta(days=5)).isoformat(), interval=6)
    _seed(vault, "notes/复习/未排期.md")  # 没有 review_due：不进列表

    r = api.get("/api/review/due")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    items = body["data"]
    assert [i["path"] for i in items] == [
        "notes/复习/早到期.md",
        "notes/复习/今天到期.md",
    ]
    assert items[0]["overdue_days"] == 3
    assert items[1]["overdue_days"] == 0
    assert items[0]["interval"] == 2
    assert items[0]["title"] == "早到期"  # 无 title 字段 → 取文件名


def test_stats_counts(api, vault):
    _seed(vault, "notes/复习/a.md", due=(TODAY - timedelta(days=1)).isoformat(), interval=1)
    _seed(vault, "notes/复习/b.md", due=(TODAY + timedelta(days=2)).isoformat(), interval=2)
    _seed(vault, "notes/复习/c.md")

    data = api.get("/api/review/stats").json()["data"]
    assert data["today"] == TODAY.isoformat()
    assert data["due"] == 1
    assert data["scheduled"] == 2
    assert data["done_today"] == 0
    assert data["max_overdue_days"] == 1


# ================================================================== 复习：标记
def test_mark_interval_progression(api, vault):
    path = _seed(vault, "notes/复习/递进.md", due=TODAY.isoformat(), interval=2)
    note = vault.read_note(path)
    original_body = note["content"]

    r = api.post("/api/review/mark", json={"path": path, "result": "good"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["previous_interval"] == 2 and d["interval"] == 4
    assert d["due"] == (TODAY + timedelta(days=4)).isoformat()
    assert d["last"] == TODAY.isoformat()

    d = api.post("/api/review/mark", json={"path": path, "result": "good"}).json()["data"]
    assert d["interval"] == 8

    d = api.post("/api/review/mark", json={"path": path, "result": "easy"}).json()["data"]
    assert d["interval"] == 24

    d = api.post("/api/review/mark", json={"path": path, "result": "again"}).json()["data"]
    assert d["interval"] == 1
    assert d["due"] == (TODAY + timedelta(days=1)).isoformat()

    # 正文与自定义字段原样保留，只增/改 review_* 三个字段
    after = vault.read_note(path)
    assert after["content"] == original_body
    assert after["frontmatter"]["自定义"] == "保留我"
    assert after["frontmatter"]["tags"] == ["学习/生物"]
    assert after["frontmatter"]["review_interval"] == 1
    assert after["frontmatter"]["review_due"] == (TODAY + timedelta(days=1)).isoformat()


def test_mark_without_prior_interval_uses_default(api, vault):
    path = _seed(vault, "notes/复习/首次.md", due=TODAY.isoformat())
    d = api.post("/api/review/mark", json={"path": path, "result": "good"}).json()["data"]
    assert d["previous_interval"] == 3  # DEFAULT_INTERVAL
    assert d["interval"] == 6


def test_mark_updates_done_today_and_due_list(api, vault):
    path = _seed(vault, "notes/复习/当天.md", due=TODAY.isoformat(), interval=1)
    assert len(api.get("/api/review/due").json()["data"]) == 1

    api.post("/api/review/mark", json={"path": path, "result": "good"})
    assert api.get("/api/review/due").json()["data"] == []
    stats = api.get("/api/review/stats").json()["data"]
    assert stats["done_today"] == 1
    assert stats["due"] == 0


def test_mark_validation_errors(api, vault):
    path = _seed(vault, "notes/复习/参数.md", due=TODAY.isoformat(), interval=2)

    bad = api.post("/api/review/mark", json={"path": path, "result": "perfect"})
    assert bad.status_code == 400
    assert "again" in bad.json()["message"]

    missing = api.post("/api/review/mark", json={"path": "notes/不存在.md", "result": "good"})
    assert missing.status_code == 404

    # 空 path 由 pydantic 拦下（仍是统一 {code,data,message} 包装）
    empty = api.post("/api/review/mark", json={"path": "", "result": "good"})
    assert empty.status_code == 400
    assert empty.json()["data"] is None


# ================================================================== 复习：排期
def test_schedule_and_unschedule(api, vault):
    path = _seed(vault, "notes/复习/排期.md")

    r = api.post("/api/review/schedule", json={"path": path, "interval": 5})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["due"] == (TODAY + timedelta(days=5)).isoformat()
    assert d["interval"] == 5
    assert api.get("/api/review/stats").json()["data"]["scheduled"] == 1

    explicit = api.post(
        "/api/review/schedule",
        json={"path": path, "due": (TODAY - timedelta(days=1)).isoformat(), "interval": 5},
    )
    assert explicit.status_code == 200
    assert len(api.get("/api/review/due").json()["data"]) == 1

    bad = api.post("/api/review/schedule", json={"path": path, "due": "明天"})
    assert bad.status_code == 400

    removed = api.delete("/api/review/schedule", params={"path": path}).json()["data"]
    assert set(removed["removed"]) == {"review_due", "review_interval"}
    assert api.get("/api/review/due").json()["data"] == []
    fm = vault.read_note(path)["frontmatter"]
    assert "review_due" not in fm and "review_interval" not in fm


# ================================================================== 剪藏：抽取
def test_clip_extracts_article_and_writes_note(api, vault, vault_dir: Path, fetch):
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL, "tags": "剪藏, 阅读"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["path"] == "clips/认知负荷与笔记法 - 某站点.md"
    assert data["deduped"] is False
    assert data["tags"] == ["剪藏", "阅读"]

    note = vault.read_note(data["path"])
    # frontmatter：标题 + 来源 URL + 标签 + 剪藏日期
    assert note["frontmatter"]["source"] == PUBLIC_URL
    assert note["frontmatter"]["clipped"] == TODAY.isoformat()
    assert note["frontmatter"]["tags"] == ["剪藏", "阅读"]
    assert note["frontmatter"]["title"] == "认知负荷与笔记法 - 某站点"

    body = note["content"]
    # 正文：H1 标题 + 来源引用 + Markdown 结构
    assert "# 认知负荷与笔记法 - 某站点" in body
    assert "## 认知负荷与笔记法" in body  # 页面 h1 降级
    assert "### 三条原则" in body
    assert "**组块化**" in body and "*认知负荷*" in body
    assert "- 先结构后细节" in body and "- 用自己的话复述" in body
    assert f"[卡片盒笔记法]({PUBLIC_URL.rsplit('/posts/1', 1)[0]}/posts/2025/zettel)" in body
    assert "![卡片盒](" in body and "/img/zettel.png" in body
    assert "```\ndef note():" in body
    assert "> 笔记的价值在于连接。" in body
    # nav/footer 噪声被跳过
    assert "首页 | 归档" not in body
    assert "版权所有" not in body
    assert (vault_dir / data["path"]).is_file()


def test_clip_prefers_article_and_falls_back_to_body(api, vault_dir: Path, fetch):
    page = """<html><head><title>无容器</title></head><body>
    <div><p>第一段正文，足够长以通过长度校验。</p><p>第二段正文。</p></div>
    </body></html>"""
    fetch["body"] = page
    data = api.post("/api/clip/url", json={"url": PUBLIC_URL}).json()["data"]
    note = (vault_dir / data["path"]).read_text(encoding="utf-8")
    assert "第一段正文" in note and "第二段正文" in note

    main_page = """<html><head><title>main 容器</title></head><body>
    <div>侧栏广告：不该出现</div>
    <main><p>main 里的正文内容，足够长了。</p></main>
    </body></html>"""
    fetch["body"] = main_page
    data = api.post("/api/clip/url", json={"url": PUBLIC_URL + "2"}).json()["data"]
    note = (vault_dir / data["path"]).read_text(encoding="utf-8")
    assert "main 里的正文内容" in note
    assert "侧栏广告" not in note


def test_clip_preview_does_not_write(api, vault_dir: Path, fetch):
    r = api.post("/api/clip/preview", json={"url": PUBLIC_URL, "tags": ["临时"]})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["title"] == "认知负荷与笔记法 - 某站点"
    assert "组块化" in data["markdown"]
    assert data["suggested_path"] == "clips/认知负荷与笔记法 - 某站点.md"
    assert data["container"] == "article"
    assert not (vault_dir / "clips").exists()


# ================================================================== 剪藏：去重
def test_clip_same_url_updates_in_place(api, vault_dir: Path, fetch):
    first = api.post("/api/clip/url", json={"url": PUBLIC_URL, "tags": "剪藏"}).json()["data"]
    second = api.post("/api/clip/url", json={"url": PUBLIC_URL, "tags": "重读"}).json()["data"]

    assert second["path"] == first["path"]
    assert second["deduped"] is True
    files = list((vault_dir / "clips").glob("*.md"))
    assert len(files) == 1
    note = files[0].read_text(encoding="utf-8")
    assert "重读" in note


def test_clip_same_title_different_url_appends_serial(api, vault_dir: Path, fetch):
    first = api.post("/api/clip/url", json={"url": PUBLIC_URL}).json()["data"]
    second = api.post("/api/clip/url", json={"url": PUBLIC_URL + "?v=2"}).json()["data"]

    assert first["path"] != second["path"]
    assert second["path"].endswith(" (1).md")
    assert len(list((vault_dir / "clips").glob("*.md"))) == 2


def test_clip_custom_target_dir(api, vault_dir: Path, fetch):
    data = api.post(
        "/api/clip/url", json={"url": PUBLIC_URL, "path": "notes/剪藏/技术"}
    ).json()["data"]
    assert data["path"] == "notes/剪藏/技术/认知负荷与笔记法 - 某站点.md"
    assert (vault_dir / data["path"]).is_file()


# ================================================================== 安全：SSRF
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://127.1.2.3/",
    "http://10.0.0.7/",
    "http://192.168.1.10/",
    "http://172.16.5.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:17630/api/health",
    "http://foo.localhost/",
    "http://[::1]/",
    "ftp://example.com/a",
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
])
def test_clip_ssrf_blocked_before_network(api, vault_dir: Path, fetch, url: str):
    r = api.post("/api/clip/url", json={"url": url})
    assert r.status_code == 400
    assert r.json()["code"] == 400
    assert fetch["calls"] == []          # 未发起任何网络请求
    assert not (vault_dir / "clips").exists()


def test_clip_blocks_domain_resolving_to_private_ip(api, fetch, monkeypatch):
    def _fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(routes_clip.socket, "getaddrinfo", _fake_getaddrinfo)
    r = api.post("/api/clip/url", json={"url": "http://notes.example.com/post"})
    assert r.status_code == 400
    assert "SSRF" in r.json()["message"] or "内网" in r.json()["message"]
    assert fetch["calls"] == []


def test_redirect_handler_rejects_internal_target():
    """重定向处理器在跳转前校验新 URL（防 302 到内网）。"""
    from fastapi import HTTPException

    handler = routes_clip._SafeRedirectHandler()
    with pytest.raises(HTTPException) as err:
        handler.redirect_request(None, None, 302, "Found", {}, "http://192.168.0.9/admin")
    assert err.value.status_code == 400

    # 公网目标放行（不实际发起请求，只看 handler 是否抛错）
    req = routes_clip.urllib.request.Request(PUBLIC_URL)
    forwarded = handler.redirect_request(req, None, 302, "Found", {}, PUBLIC_URL + "/next")
    assert forwarded is not None and forwarded.full_url.endswith("/next")


# ================================================================== 安全：抓取异常
def test_clip_rejects_oversized_response(api, vault_dir: Path, fetch):
    fetch["body"] = "<html><body><p>" + ("字" * (MAX_BYTES // 3 + 100)) + "</p></body></html>"
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL})
    assert r.status_code == 400
    assert "2MB" in r.json()["message"]
    assert not (vault_dir / "clips").exists()


def test_clip_rejects_non_html(api, fetch):
    fetch["content_type"] = "application/pdf"
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL})
    assert r.status_code == 400
    assert "HTML" in r.json()["message"]


def test_clip_network_error_is_502(api, vault_dir: Path, fetch):
    fetch["error"] = urllib.error.URLError("连接被拒绝")
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL})
    assert r.status_code == 502
    assert "抓取失败" in r.json()["message"]
    assert not (vault_dir / "clips").exists()


def test_clip_extraction_failure_leaves_no_note(api, vault_dir: Path, fetch):
    fetch["body"] = (
        "<html><head><title>空页</title></head>"
        "<body><script>window.__DATA__ = {}</script></body></html>"
    )
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL})
    assert r.status_code == 400
    assert "未能" in r.json()["message"]
    assert not (vault_dir / "clips").exists()


def test_clip_redirect_to_internal_is_blocked(api, fetch, monkeypatch):
    """假响应最终 URL 是内网时，抓取阶段直接拒绝（重定向二次校验兜底）。"""
    internal = "http://10.0.0.9/secret"

    def _open(req, timeout):
        return FakeResponse(ARTICLE_HTML, internal)

    monkeypatch.setattr(routes_clip, "_open_request", _open)
    r = api.post("/api/clip/url", json={"url": PUBLIC_URL})
    assert r.status_code == 400
    assert "内网" in r.json()["message"]


# ------------------------------------------------------------------ 扫描缓存
def test_review_cache_invalidated_by_write(api, vault):
    """扫描缓存按 mtime 失效：write 后立刻能读到新 review_due。"""
    path = _seed(vault, "notes/复习/缓存.md")
    assert api.get("/api/review/due").json()["data"] == []
    api.post("/api/review/schedule", json={"path": path, "interval": 1})
    api.post("/api/review/mark", json={"path": path, "result": "again"})
    stats = api.get("/api/review/stats").json()["data"]
    assert stats["scheduled"] == 1
    assert stats["done_today"] == 1
