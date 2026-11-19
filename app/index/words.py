"""字数统计：与前端 utils.wordCount 同规则（CJK 字符 + 拉丁词 token）。"""
import re

_CJK = re.compile("[一-鿿㐀-䶿]")
_WORD = re.compile(r"[A-Za-z0-9_]+")


def word_count(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    words = len(_WORD.findall(_CJK.sub(" ", text)))
    return cjk + words