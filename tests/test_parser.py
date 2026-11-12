"""T02 解析器单测：frontmatter 往返 / wikilink 提取 / 行内标签 / 渲染。"""

from __future__ import annotations

from app.parser.frontmatter import (
    dump_frontmatter,
    normalize_frontmatter_for_write,
    parse_frontmatter,
    update_fields,
)
from app.parser.renderer import render_markdown
from app.parser.tags import extract_inline_tags, normalize_fm_tags
from app.parser.wikilink import extract_wikilinks, parse_wikilink_text, split_anchor

RAW = """---
title: 测试
tags: [a, b/c]
自定义: 保留我
---

正文第一行。

```
#不是标签 [[不是链接]]
```

行内 `#code [[nolink]]` 不算。

#my/tag 和 [[目标|别名]] 和 ![[嵌入图.png]]。
"""


class TestFrontmatter:
    def test_parse(self):
        fm, body = parse_frontmatter(RAW)
        assert fm["title"] == "测试"
        assert fm["tags"] == ["a", "b/c"]
        assert fm["自定义"] == "保留我"
        assert body.lstrip().startswith("正文第一行")

    def test_roundtrip_preserves_unknown_fields(self):
        fm, body = parse_frontmatter(RAW)
        text = dump_frontmatter(fm, body)
        fm2, body2 = parse_frontmatter(text)
        assert fm2["自定义"] == "保留我"
        assert fm2["title"] == "测试"
        assert body2.strip() == body.strip()

    def test_update_fields_partial(self):
        fm, _ = parse_frontmatter(RAW)
        new = update_fields(fm, {"title": "新标题", "removed": None})
        assert new["title"] == "新标题"
        assert new["自定义"] == "保留我"
        assert "removed" not in new
        assert fm["title"] == "测试"  # 原对象不变

    def test_no_frontmatter(self):
        fm, body = parse_frontmatter("只有正文")
        assert fm == {}
        assert body == "只有正文"

    def test_dump_without_fm(self):
        assert dump_frontmatter({}, "正文") == "正文\n"

    def test_normalize_timestamps(self):
        fm = normalize_frontmatter_for_write({}, is_new=True)
        assert "created" in fm and "updated" in fm
        fm2 = normalize_frontmatter_for_write({"created": "旧值"}, is_new=False)
        assert fm2["created"] == "旧值"
        assert "updated" in fm2


class TestWikilink:
    def test_extract_basic(self):
        body = "第一行 [[目标]]\n第二行 [[目标|别名]]\n第三行 ![[嵌入]]"
        refs = extract_wikilinks(body)
        assert [(r.target, r.kind, r.line) for r in refs] == [
            ("目标", "wikilink", 1),
            ("目标", "wikilink", 2),
            ("嵌入", "embed", 3),
        ]
        assert refs[1].alias == "别名"

    def test_skip_code_blocks(self):
        body = "```\n[[代码里的链接]]\n```\n[[真实链接]]"
        refs = extract_wikilinks(body)
        assert len(refs) == 1 and refs[0].target == "真实链接"

    def test_skip_inline_code(self):
        body = "正文 `[[不是链接]]` 然后 [[是链接]]"
        refs = extract_wikilinks(body)
        assert len(refs) == 1 and refs[0].target == "是链接"

    def test_parse_text(self):
        assert parse_wikilink_text("a|b") == ("a", "b")
        assert parse_wikilink_text("a") == ("a", "")
        assert split_anchor("笔记#标题") == ("笔记", "标题")


class TestTags:
    def test_inline_tags(self):
        tags = extract_inline_tags("普通 #tag 与 #a/b 嵌套，以及 #中文标签。")
        assert tags == ["tag", "a/b", "中文标签"]

    def test_skip_code_and_urls(self):
        body = "```\n#代码块标签\n```\n看 https://example.com/#anchor 页面。\n`#行内码` #real"
        tags = extract_inline_tags(body)
        assert tags == ["real"]

    def test_normalize_fm_tags(self):
        assert normalize_fm_tags(["a", "b/c"]) == ["a", "b/c"]
        assert normalize_fm_tags("x, y") == ["x", "y"]
        assert normalize_fm_tags(["#hash"]) == ["hash"]
        assert normalize_fm_tags(None) == []


class TestRenderer:
    def test_wikilink_html(self):
        html = render_markdown("见 [[目标|别名]]", resolve=lambda t, s=None: None)
        assert 'class="kwey-wikilink kwey-wikilink--broken"' in html
        assert 'data-target="目标"' in html
        assert "别名" in html

    def test_embed_html(self):
        html = render_markdown("![[嵌入]]", resolve=lambda t, s=None: "嵌入.md")
        assert 'class="kwey-embed"' in html
        assert 'data-target="嵌入"' in html

    def test_tag_html(self):
        html = render_markdown("打上 #a/b 标签")
        assert '<span class="kwey-tag" data-tag="a/b">#a/b</span>' in html

    def test_code_block_untouched(self):
        html = render_markdown("```\n[[x]] #y\n```")
        assert "kwey-wikilink" not in html
        assert "kwey-tag" not in html

    def test_image_rewrite(self):
        html = render_markdown("![说明](attachments/pic.png)")
        assert 'src="/file/attachments/pic.png"' in html
        html2 = render_markdown("![外链](https://example.com/a.png)")
        assert 'src="https://example.com/a.png"' in html2

    def test_markdown_basics(self):
        html = render_markdown("# 标题\n\n**粗体**")
        assert "<h1>" in html and "<strong>粗体</strong>" in html
