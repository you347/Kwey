"""聊天回复 Markdown 渲染：/api/agent/render 的鉴权、结构、XSS 与链接安全。

覆盖：
1. 鉴权：无 token 401；read 级 token 200（纯渲染不落盘，不需要 write）。
2. 结构：表格 / 围栏代码 / 加粗 / 列表 / 引用 / 删除线 / 分隔线。
3. 安全：原始 HTML 与 <script> 一律转义；javascript: 链接不可点击。
4. 外链：http(s) 补 target="_blank" rel="noopener noreferrer"，站内相对链接不动。
5. 与预览渲染互不干扰：render_markdown 的 wikilink 行为不变。
"""

from __future__ import annotations

import importlib.util

from app.agent.auth import TokenStore
from app.parser.renderer import render_chat_markdown, render_markdown

# markdown-it 的 linkify 规则需要 linkify-it-py；本卡不新增依赖，故按环境实际情况断言。
_LINKIFY_AVAILABLE = importlib.util.find_spec("linkify_it") is not None


def _tk(vault, role: str = "admin") -> str:
    return TokenStore(vault.paths.root).issue(role, "test")["token"]


def _post(client, tk, content):
    return client.post(
        "/api/agent/render", headers={"X-Kwey-Token": tk}, json={"content": content}
    )


# ---------------------------------------------------------------- 鉴权
def test_render_requires_token(client):
    r = client.post("/api/agent/render", json={"content": "hi"})
    assert r.status_code == 401


def test_render_accepts_read_token(client, vault):
    r = _post(client, _tk(vault, "read"), "# 标题\n\n正文")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert "<h1>" in body["data"]["html"]


def test_render_rejects_bad_token(client):
    assert _post(client, "not-a-real-token", "hi").status_code == 401


# ---------------------------------------------------------------- 结构
def test_render_structures(client, vault):
    md = (
        "## 对比\n\n"
        "**结论**：B 更稳。\n\n"
        "| 方案 | 优点 | 缺点 |\n"
        "| --- | --- | --- |\n"
        "| A | 快 | 脆 |\n"
        "| B | 稳 | 慢 |\n\n"
        "- 第一条\n- 第二条\n\n"
        "1. 甲\n2. 乙\n\n"
        "> 引用一行\n\n"
        "~~作废~~\n\n"
        "---\n\n"
        "```python\nprint(\"hi\")\n```\n\n"
        "行内 `code` 结束。\n"
    )
    html = _post(client, _tk(vault), md).json()["data"]["html"]
    assert "<table>" in html and "<th>" in html
    assert "<pre><code" in html
    assert "<strong>结论</strong>" in html
    assert "<ul>" in html and "<ol>" in html
    assert "<blockquote>" in html
    assert "<s>作废</s>" in html or "<del>作废</del>" in html
    assert "<hr" in html
    assert "<code>code</code>" in html


# ---------------------------------------------------------------- 安全
def test_script_is_escaped(client, vault):
    html = _post(client, _tk(vault), "<script>alert(1)</script>").json()["data"]["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_raw_html_block_is_escaped(client, vault):
    """html=False 的硬约束：模型输出里的标签块不能被浏览器执行。"""
    payload = '<div onclick="alert(1)">x</div>\n\n<img src=x onerror="alert(2)">'
    html = _post(client, _tk(vault), payload).json()["data"]["html"]
    assert "<div" not in html
    assert "<img" not in html
    assert "&lt;div" in html and "&lt;img" in html
    assert 'onerror="' not in html   # 引号已转义，属性无法逃逸


def test_javascript_link_not_clickable(client, vault):
    html = _post(client, _tk(vault), "[x](javascript:alert(1))").json()["data"]["html"]
    assert 'href="javascript' not in html
    assert "<a " not in html


# ---------------------------------------------------------------- 链接
def test_external_link_opens_new_tab(client, vault):
    html = _post(client, _tk(vault), "[官网](https://example.com)").json()["data"]["html"]
    assert 'href="https://example.com"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_bare_url_matches_linkify_availability(client, vault):
    """linkify 是否生效取决于 linkify-it-py 是否安装（未装时 markdown-it 静默跳过）。

    不新增依赖：装了就该自动成链，没装就保持纯文本——两种情况都锁定，避免误判回归。
    """
    html = _post(client, _tk(vault), "详见 https://kwey.dev").json()["data"]["html"]
    assert ("<a " in html) is _LINKIFY_AVAILABLE


def test_relative_link_untouched(client, vault):
    html = _post(client, _tk(vault), "[本地](/notes/a.md)").json()["data"]["html"]
    assert 'href="/notes/a.md"' in html
    assert "target=" not in html


# ---------------------------------------------------------------- 边界与隔离
def test_empty_content_rejected(client, vault):
    """空 content 被 Pydantic 拒收；服务端全局 handler 把它统一包成 400 信封。

    （卡内写的是 422，但项目有全局 RequestValidationError -> 400 契约，
    本卡不允许改 app/server.py，故按实际契约断言，偏差已写入报告。）
    """
    r = _post(client, _tk(vault), "")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_wikilink_not_converted_in_chat(client, vault):
    """聊天不解析 wikilink / 行内标签（不调用 _preprocess）。"""
    html = _post(client, _tk(vault), "[[欢迎]] #读书/2026").json()["data"]["html"]
    assert "kwey-wikilink" not in html
    assert "kwey-tag" not in html
    assert "[[欢迎]]" in html


def test_preview_renderer_unchanged(vault):
    """既有预览链路仍透传 wikilink 注入的 HTML（本卡不改它）。"""
    html = render_markdown("[[欢迎]]")
    assert "kwey-wikilink" in html


def test_chat_renderer_is_stateless(vault):
    """同一函数连续调用互不影响（独立实例、无全局可变状态）。"""
    assert render_chat_markdown("**a**") == render_chat_markdown("**a**")
