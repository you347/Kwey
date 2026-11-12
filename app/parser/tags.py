"""行内 #tag / #a/b 嵌套标签提取（跳过 frontmatter、围栏代码块与行内代码）。

frontmatter 的 tags 字段由 frontmatter.py 解析后由索引层合并，本模块只管正文行内标签。
"""

from __future__ import annotations

import re
from typing import List

from app.parser.wikilink import _iter_outside_code

# 标签字符：字母/数字/下划线/连字符/斜杠/中文；不允许以斜杠开头结尾
TAG_RE = re.compile(r"(?<![\w\u4e00-\u9fff])#([\w\u4e00-\u9fff]+(?:/[\w\u4e00-\u9fff]+)*)")
# 前向排除：紧跟在 [[wikilink 内部或紧邻字母数字的不算（TAG_RE 已用 lookbehind 排除 \w）
URL_LIKE_RE = re.compile(r"https?://\S*#\S*")


def _clean_tag(tag: str) -> str:
    """去掉尾随标点（中文句读/英文逗号句号等）。"""
    return tag.rstrip(".,;:!?)】」》。，、；：！？")


def extract_inline_tags(body: str) -> List[str]:
    """提取正文行内标签（去重、保序），跳过围栏代码块、行内代码与 URL 片段。"""
    tags: List[str] = []
    seen = set()
    for _line_no, line in _iter_outside_code(body):
        # 跳过行内代码 span
        segments = []
        last = 0
        for m in re.finditer(r"`[^`]*`", line):
            segments.append(line[last:m.start()])
            last = m.end()
        segments.append(line[last:])
        for seg in segments:
            # 排除 URL 中的 # 锚点
            seg = URL_LIKE_RE.sub(" ", seg)
            for m in TAG_RE.finditer(seg):
                tag = _clean_tag(m.group(1))
                if tag and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
    return tags


def normalize_fm_tags(value) -> List[str]:
    """frontmatter tags 字段规范化：支持 list[str] / 逗号分隔字符串 / 单字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [p.strip() for p in value.replace("#", "").split(",")]
    elif isinstance(value, (list, tuple)):
        raw_items = []
        for item in value:
            if isinstance(item, str):
                raw_items.append(item.strip().lstrip("#").strip())
            else:
                raw_items.append(str(item).strip())
    else:
        return []
    result = []
    for tag in raw_items:
        tag = tag.strip().strip("#").strip("/")
        tag = re.sub(r"/+", "/", tag)
        if tag and tag not in result:
            result.append(tag)
    return result
