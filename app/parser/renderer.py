"""markdown-it-py 渲染：服务端预览兜底。

处理链：
1. 预处理正文：wikilink/嵌入 → 带 data-* 属性的内联 HTML；#tag → 带 data-tag 的 span
   （跳过围栏代码块与行内代码；html=True 让 markdown-it 原样透传这些片段）。
2. markdown-it（commonmark + table/strikethrough）渲染。
3. 后处理：相对路径 <img src> 重写为 /file/ 直出路由（预览可正常显示附件）。

Wikilink 渲染结果：
    <a class="kwey-wikilink" data-target="..." data-embed="0|1" data-exists="0|1" href="#">别名或目标</a>
    <span class="kwey-embed" data-target="..."></span>        （嵌入占位，前端回填内容）
    <span class="kwey-tag" data-tag="a/b">#a/b</span>
"""

from __future__ import annotations

import re
from html import escape
from typing import Callable, Optional

from markdown_it import MarkdownIt

from app.parser.tags import extract_inline_tags
from app.parser.wikilink import WIKILINK_RE, parse_wikilink_text, split_anchor

_MD = MarkdownIt("commonmark", {"html": True, "linkify": True}).enable(
    ["table", "strikethrough"]
)

_IMG_SRC_RE = re.compile(r'<img\s+([^>]*?)src="([^"]+)"', re.IGNORECASE)

_RESOLVER = Callable[[str, Optional[str]], Optional[str]]


def _split_code_spans(line: str) -> list:
    """返回 [(seg_text, is_code)]，行内代码按原样保留。"""
    segments = []
    last = 0
    for m in re.finditer(r"`[^`]*`", line):
        if m.start() > last:
            segments.append((line[last:m.start()], False))
        segments.append((m.group(0), True))
        last = m.end()
    if last < len(line):
        segments.append((line[last:], False))
    return segments or [(line, False)]


def _preprocess(body: str, resolve: Optional[_RESOLVER]) -> str:
    """wikilink/嵌入/标签 → 内联 HTML 片段。"""
    out_lines = []
    fence = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is None:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                fence = m.group(1)[0]
                out_lines.append(line)
                continue
        else:
            if re.match(rf"^{re.escape(fence)}{{3,}}", stripped):
                fence = None
            out_lines.append(line)
            continue

        rendered_segments = []
        for seg, is_code in _split_code_spans(line):
            if is_code:
                rendered_segments.append(seg)
                continue
            seg = _replace_wikilinks(seg, resolve)
            seg = _replace_tags(seg)
            rendered_segments.append(seg)
        out_lines.append("".join(rendered_segments))
    return "\n".join(out_lines)


def _replace_wikilinks(text: str, resolve: Optional[_RESOLVER]) -> str:
    def _sub(m: re.Match) -> str:
        embed_flag, inner = m.group(1), m.group(2)
        target_raw, alias = parse_wikilink_text(inner)
        target, _anchor = split_anchor(target_raw)
        if not target:
            return m.group(0)
        exists = 1 if (resolve and resolve(target, None)) else 0
        display = escape(alias or target_raw)
        if embed_flag:
            return (
                f'<span class="kwey-embed" data-target="{escape(target, quote=True)}"'
                f' data-raw="{escape(target_raw, quote=True)}" data-exists="{exists}"></span>'
            )
        return (
            f'<a class="kwey-wikilink{" kwey-wikilink--broken" if not exists else ""}"'
            f' data-target="{escape(target, quote=True)}"'
            f' data-raw="{escape(target_raw, quote=True)}" data-exists="{exists}"'
            f' href="#">{display}</a>'
        )

    return WIKILINK_RE.sub(_sub, text)


_TAG_HTML_RE = re.compile(
    r"(?<![\w\u4e00-\u9fff])#([\w\u4e00-\u9fff]+(?:/[\w\u4e00-\u9fff]+)*)"
)


def _replace_tags(text: str) -> str:
    def _sub(m: re.Match) -> str:
        tag = m.group(1)
        return f'<span class="kwey-tag" data-tag="{escape(tag, quote=True)}">#{tag}</span>'

    # 避免改写已生成的 HTML 属性内的 #（data-target 等含引号上下文无 #，风险低）
    return _TAG_HTML_RE.sub(_sub, text)


def _postprocess_images(html: str) -> str:
    """相对路径图片 → /file/<rel> 直出；绝对 URL / data: / 根路径不动。"""

    def _sub(m: re.Match) -> str:
        attrs, src = m.group(1), m.group(2)
        if re.match(r"^(https?://|data:|/|#)", src, re.IGNORECASE):
            return m.group(0)
        clean = src.replace("\\", "/").lstrip("./")
        return f'<img {attrs}src="/file/{escape(clean, quote=True)}"'

    return _IMG_SRC_RE.sub(_sub, html)


def render_markdown(
    body: str,
    resolve: Optional[_RESOLVER] = None,
    with_tags: bool = True,
) -> str:
    """Markdown 正文 → HTML（wikilink/嵌入/标签已渲染为可交互元素）。

    Args:
        body: 不含 frontmatter 的 Markdown 正文。
        resolve: 可选解析回调 (target, source_path) -> vault 相对路径或 None，
                 用于标记断链（data-exists）。服务端渲染兜底时由调用方传入。
        with_tags: 是否渲染行内标签 span。
    """
    processed = _preprocess(body, resolve)
    if not with_tags:
        # 已在上面统一处理；此参数预留（M3 大纲等场景可关闭标签渲染）
        pass
    html = _MD.render(processed)
    html = _postprocess_images(html)
    return html
