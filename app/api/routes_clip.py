"""F4 网页剪藏：URL → 正文抽取 → Markdown → 剪藏笔记（clips/）。

只依赖标准库（urllib + html.parser），零新增第三方依赖：

1. **SSRF 防护**（先于任何网络动作）
   - scheme 仅 http / https；
   - 主机名为 localhost / *.localhost / *.local / *.internal 一律拒绝；
   - 字面 IP 必须是公网地址（``ipaddress.is_global``），127./10./192.168./169.254./
     172.16-31./::1 等内网段全部拦截；
   - 域名做一次 DNS 解析，解析结果命中内网地址同样拒绝（防 DNS 指向内网）；
   - 重定向由自定义 ``HTTPRedirectHandler`` 在跳转前二次校验，绕不过去。
2. **抓取约束**：10s 超时、固定 User-Agent、响应上限 2MB、只接受 HTML 内容类型。
3. **正文抽取**：优先 ``<article>``，其次 ``<main>``，都没有才退回整页；跳过
   script/style/nav/footer 等噪声，转 Markdown（标题/段落/链接/列表/代码/图片/引用）。
4. **落盘**：全部内容在内存里组装完成后才走 ``VaultService.create_note``，
   抓取或转换失败不会留下半截笔记；同一 source URL 重复剪藏时原地更新（去重）。

接口（prefix=/api/clip）：
- ``POST /preview``  {url, tags?, path?}  只抓取与转换，返回 Markdown 预览，不写盘
- ``POST /url``      {url, tags?, path?}  抓取并写入 ``<clip_dir>/<标题>.md``
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import deps
from app.parser.frontmatter import parse_frontmatter
from app.paths import AppPaths, PathError, normalize_rel, sanitize_filename
from app.vault.provenance import ActorContext

router = APIRouter(prefix="/api/clip", tags=["clip"])

# 浏览器剪藏是 Kwey 抓取/转换写入，不是人工逐字编辑。
CLIP_ACTOR = ActorContext(kind="system", name="kwey-clip", origin="clip")

MAX_BYTES = 2 * 1024 * 1024          # 响应上限 2MB
FETCH_TIMEOUT = 10.0                 # 秒
MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 120
MAX_FILENAME_LENGTH = 80
MIN_MARKDOWN_CHARS = 16              # 正文过短视为抽取失败（多为 JS 渲染页）
DEFAULT_CLIP_DIR = "clips"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) KweyClipper/1.0 Safari/537.36"
)

# 内网/本机域名后缀（DNS 解析之外的第二道防线）
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")
_BLOCKED_HOSTS = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


# ------------------------------------------------------------------ 请求模型
class ClipRequest(BaseModel):
    """剪藏 / 预览请求。path 为目标目录（或以 .md 结尾的完整笔记路径）。"""

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    path: Optional[str] = Field(default=None, max_length=1024)
    tags: Optional[Union[List[str], str]] = None


# ------------------------------------------------------------------ SSRF 校验
def _is_public_ip(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """公网地址判定：非 private/loopback/link-local/reserved/multicast/unspecified。"""
    return bool(getattr(ip, "is_global", False))


def _assert_public_url(url: str) -> str:
    """校验 URL 可安全抓取（仅 http/https + 公网主机），非法一律 400。"""
    text = (url or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="URL 不能为空")
    if len(text) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"URL 过长（上限 {MAX_URL_LENGTH} 字符）")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"URL 解析失败: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"仅支持 http/https 链接，收到: {scheme or '(空)'}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail="URL 缺少主机名")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail=f"禁止剪藏本机/内网地址: {host}")

    literal: Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]
    try:
        literal = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public_ip(literal):
            raise HTTPException(status_code=400, detail=f"禁止剪藏内网地址: {host}")
        return text

    # 域名：解析一次，全部结果都必须是公网地址；解析失败交给实际请求报 502
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        infos = []
    for info in infos:
        addr = str(info[4][0]).split("%")[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not _is_public_ip(resolved):
            raise HTTPException(
                status_code=400,
                detail=f"域名 {host} 解析到内网地址 {addr}，已拦截（SSRF 防护）",
            )
    return text


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向跳转前再校验一次，避免公网 URL 302 到内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _open_request(req: urllib.request.Request, timeout: float):
    """模块级抓取入口（测试 monkeypatch 此函数即可离线验证，不触网）。"""
    return _OPENER.open(req, timeout=timeout)


# ------------------------------------------------------------------ 抓取
def _decode(raw: bytes, content_type: str) -> str:
    """按 Content-Type / meta charset 解码，异常字符替换不抛错。"""
    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1).strip("\"'").lower()
    if not charset:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", raw[:4096], re.I)
        if meta:
            charset = meta.group(1).decode("ascii", "ignore").lower()
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _fetch_html(url: str) -> Tuple[str, str, str]:
    """抓取 URL → (最终 URL, 解码后的 HTML, Content-Type)。失败抛 400/502。"""
    _assert_public_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        response = _open_request(req, FETCH_TIMEOUT)
    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"抓取失败：HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"抓取失败：{exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HTTPException(status_code=502, detail=f"抓取失败：{FETCH_TIMEOUT:.0f}s 超时") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"抓取失败：{exc}") from exc

    try:
        final_url = str(response.geturl() or url)
        # 双重校验：即便重定向处理器被绕过，最终 URL 也必须通过 SSRF 检查
        _assert_public_url(final_url)
        content_type = ""
        headers = getattr(response, "headers", None)
        if headers is not None:
            content_type = str(headers.get("Content-Type") or "")
        raw = response.read(MAX_BYTES + 1)
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            try:
                closer()
            except OSError:
                pass

    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"页面超过 {MAX_BYTES // (1024 * 1024)}MB 上限，未剪藏",
        )
    main_type = (content_type.split(";", 1)[0] or "").strip().lower()
    if main_type and main_type not in ("text/html", "application/xhtml+xml", "text/plain"):
        raise HTTPException(status_code=400, detail=f"不是 HTML 页面（Content-Type: {main_type}）")
    return final_url, _decode(raw, content_type), content_type


# ------------------------------------------------------------------ 正文抽取
class _TargetScanner(HTMLParser):
    """找出页面首选正文容器：优先 article，其次 main。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article = False
        self.main = False
        self._title: List[str] = []
        self._in_title = False
        self._first_h1: List[str] = []
        self._in_h1 = False
        self._h1_done = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "article":
            self.article = True
        elif tag == "main":
            self.main = True
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            self._h1_done = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        if self._in_h1:
            self._first_h1.append(data)

    @property
    def target(self) -> Optional[str]:
        if self.article:
            return "article"
        if self.main:
            return "main"
        return None

    @property
    def title(self) -> str:
        text = _clean_text(" ".join(self._title))
        if not text:
            text = _clean_text(" ".join(self._first_h1))
        return text


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_list_line(line: str) -> bool:
    """是否无序/有序列表项（决定相邻块用单换行拼接）。"""
    return bool(re.match(r"^\s*(?:[-*]|\d+\.)\s", line or ""))


class _MarkdownParser(HTMLParser):
    """HTML → Markdown（h1-h6 / p / a / ul-ol-li / pre-code / img / blockquote / strong-em）。"""

    SKIP_TAGS = {
        "script", "style", "noscript", "svg", "iframe", "canvas", "template",
        "form", "button", "input", "select", "textarea", "nav", "footer", "aside",
    }
    BLOCK_TAGS = {
        "p", "div", "section", "article", "main", "header", "figure", "figcaption",
        "table", "thead", "tbody", "tfoot", "tr", "dl", "dt", "dd", "address",
        "details", "summary", "fieldset", "center",
    }
    HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self, target: Optional[str], base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target = target
        self.base_url = base_url
        self.capturing = target is None
        self.target_depth = 0
        self.skip_depth = 0
        self.blocks: List[str] = []
        self.buf: List[str] = []
        self.prefix = ""
        self.quote_depth = 0
        self.list_stack: List[Tuple[str, int]] = []   # [(ul|ol, 已用序号)]
        self.in_pre = False
        self.pre_buf: List[str] = []
        self.link_stack: List[Tuple[str, int]] = []
        self.mark_stack: Dict[str, List[int]] = {}
        self.table_rows: List[List[str]] = []
        self.table_cells: List[str] = []
        self.in_table = 0

    # ------------------------------------------------------------ 捕获控制
    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if self.target and tag == self.target:
            self.target_depth += 1
            self.capturing = True
            return
        if not self.capturing:
            return
        self._start(tag, dict(attrs))

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        # <img/>、<br/> 这类自闭合标签：只走开始分支
        if tag in self.SKIP_TAGS or self.skip_depth or not self.capturing:
            return
        self._start(tag, dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if self.target and tag == self.target:
            self._flush()
            if self.target_depth:
                self.target_depth -= 1
            if self.target_depth == 0:
                self.capturing = False
            return
        if not self.capturing:
            return
        self._end(tag)

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not self.capturing or not data:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if text:
            self.buf.append(text)

    # ------------------------------------------------------------ 标签映射
    def _start(self, tag: str, attrs: Dict[str, str]) -> None:
        if tag in self.HEADING_LEVELS:
            self._flush()
            # 标题降一级：笔记正文已有 H1（标题），页面 H1 不该与它平级
            level = min(6, self.HEADING_LEVELS[tag] + 1)
            self.prefix = "#" * level + " "
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append((tag, 0))
        elif tag == "li":
            self._flush()
            indent = "  " * max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1][0] == "ol":
                tag_name, used = self.list_stack[-1]
                used += 1
                self.list_stack[-1] = (tag_name, used)
                self.prefix = f"{indent}{used}. "
            else:
                self.prefix = f"{indent}- "
        elif tag == "blockquote":
            self._flush()
            self.quote_depth += 1
        elif tag == "table":
            self._flush()
            self.in_table += 1
            self.table_rows = []
            self.table_cells = []
        elif tag == "tr":
            self._flush()
            self.table_cells = []
        elif tag == "pre":
            self._flush()
            self.in_pre = True
            self.pre_buf = []
        elif tag == "br":
            self._flush()
        elif tag == "hr":
            self._flush()
            self.blocks.append("---")
        elif tag == "a":
            href = (attrs.get("href") or "").strip()
            self.link_stack.append((href, len(self.buf)))
            self.buf.append("[" if self._safe_href(href) else "")
        elif tag == "img":
            src = (attrs.get("src") or "").strip()
            alt = _clean_text(attrs.get("alt") or "")
            if src and not src.startswith(("data:", "javascript:")):
                self.buf.append(f"![{alt}]({self._absolute(src)})")
        elif tag in ("strong", "b"):
            self.mark_stack.setdefault("strong", []).append(len(self.buf))
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.mark_stack.setdefault("em", []).append(len(self.buf))
            self.buf.append("*")
        elif tag == "code" and not self.in_pre:
            self.mark_stack.setdefault("code", []).append(len(self.buf))
            self.buf.append("`")
        elif tag in self.BLOCK_TAGS:
            self._flush()

    def _end(self, tag: str) -> None:
        if tag in self.HEADING_LEVELS:
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "li":
            self._flush()
        elif tag == "blockquote":
            self._flush()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag == "table":
            self._flush()
            self._emit_table()
            self.in_table = max(0, self.in_table - 1)
        elif tag in ("td", "th"):
            cell = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            self.buf = []
            if self.in_table:
                self.table_cells.append(cell)
        elif tag == "tr":
            self._flush()
            if self.in_table and self.table_cells:
                self.table_rows.append(self.table_cells)
                self.table_cells = []
        elif tag == "pre":
            text = "".join(self.pre_buf).strip("\n")
            self.pre_buf = []
            self.in_pre = False
            if text.strip():
                self.blocks.append("```\n" + text.rstrip() + "\n```")
        elif tag == "a":
            self._close_link()
        elif tag in ("strong", "b"):
            self._close_mark("strong", "**")
        elif tag in ("em", "i"):
            self._close_mark("em", "*")
        elif tag == "code" and not self.in_pre:
            self._close_mark("code", "`")
        elif tag in self.BLOCK_TAGS:
            self._flush()

    def _emit_table(self) -> None:
        """表格 → GFM 表格（首行表头 + 分隔行；列数不齐时按最大列补齐）。"""
        rows = [r for r in self.table_rows if any(c for c in r)]
        self.table_rows = []
        self.table_cells = []
        if len(rows) < 2:
            # 单元格太少，退回段落文本，避免产出半截表格
            for row in rows:
                line = " | ".join(c for c in row if c)
                if line:
                    self.blocks.append(line)
            return
        width = max(len(r) for r in rows)
        padded = [(r + [""] * width)[:width] for r in rows]
        lines = ["| " + " | ".join(padded[0]) + " |",
                 "| " + " | ".join(["---"] * width) + " |"]
        for row in padded[1:]:
            lines.append("| " + " | ".join(row) + " |")
        self.blocks.append("\n".join(lines))

    # ------------------------------------------------------------ 收尾
    def _close_link(self) -> None:
        if not self.link_stack:
            return
        href, start = self.link_stack.pop()
        if not self._safe_href(href):
            return
        text = "".join(self.buf[start:])
        if text.strip() in ("[", ""):
            del self.buf[start:]  # 空链接（图标/锚点）不产出 Markdown
            return
        self.buf.append(f"]({self._absolute(href)})")

    def _close_mark(self, kind: str, mark: str) -> None:
        stack = self.mark_stack.get(kind) or []
        start = stack.pop() if stack else None
        if start is None:
            return
        if len(self.buf) <= start + 1:
            # 空内容（如 <strong></strong>）：撤掉开标记
            del self.buf[start:]
            return
        self.buf.append(mark)

    def _flush(self) -> None:
        raw = "".join(self.buf)
        self.buf = []
        prefix, self.prefix = self.prefix, ""
        text = re.sub(r"[ \t]+", " ", raw).strip()
        if not text:
            return
        if self.quote_depth:
            text = "> " * self.quote_depth + text
        line = prefix + text
        # 连续的列表项用单换行拼成紧凑列表，避免每项之间出现空行
        if _is_list_line(line) and self.blocks and _is_list_line(self.blocks[-1]):
            self.blocks[-1] = self.blocks[-1] + "\n" + line
            return
        self.blocks.append(line)

    def _safe_href(self, href: str) -> bool:
        return bool(href) and not href.lower().startswith(("javascript:", "data:", "#"))

    def _absolute(self, href: str) -> str:
        try:
            return urllib.parse.urljoin(self.base_url, href)
        except ValueError:
            return href

    def close(self) -> None:  # noqa: D401 —— 覆盖 HTMLParser.close，补最后的缓冲
        super().close()
        if self.in_pre and self.pre_buf:
            text = "".join(self.pre_buf).strip("\n")
            self.pre_buf = []
            self.in_pre = False
            if text.strip():
                self.blocks.append("```\n" + text.rstrip() + "\n```")
        self._flush()

    def markdown(self) -> str:
        text = "\n\n".join(b for b in self.blocks if b.strip())
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _extract(url: str, html: str) -> Dict[str, Any]:
    """HTML → {title, markdown, target}；正文过短抛 400。"""
    scanner = _TargetScanner()
    try:
        scanner.feed(html)
        scanner.close()
    except Exception:  # noqa: BLE001 —— 畸形 HTML 不应 500
        pass

    target = scanner.target
    parser = _MarkdownParser(target, url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 —— 畸形 HTML 不抛 500，保留已解析部分
        pass
    markdown = parser.markdown()

    title = scanner.title or _title_from_url(url)
    title = re.sub(r"\s+", " ", title).strip()[:MAX_TITLE_LENGTH]
    if len(markdown) < MIN_MARKDOWN_CHARS:
        raise HTTPException(
            status_code=400,
            detail="未能从页面提取到正文（可能是 JS 渲染或需要登录的页面）",
        )
    return {"title": title, "markdown": markdown, "target": target or "body"}


def _title_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    if tail:
        stem = tail.rsplit(".", 1)[0]
        if stem:
            return urllib.parse.unquote(stem)
    return urllib.parse.urlsplit(url).hostname or "网页剪藏"


# ------------------------------------------------------------------ 落盘辅助
def _normalize_tags(tags: Optional[Union[List[str], str]]) -> List[str]:
    """标签归一化：接受数组或逗号/顿号分隔字符串，去空去重保序。"""
    if tags is None:
        return []
    raw: List[str] = tags if isinstance(tags, list) else re.split(r"[,，、;；\s]+", str(tags))
    result: List[str] = []
    for item in raw:
        tag = str(item).strip().strip("#")
        if tag and tag not in result:
            result.append(tag)
    return result


def _clip_dir() -> str:
    config = deps.require_config()
    rel = normalize_rel(str(config.get("clip_dir") or DEFAULT_CLIP_DIR))
    return rel or DEFAULT_CLIP_DIR


def _resolve_target(raw_path: Optional[str], title: str) -> str:
    """解析目标路径：path 为目录，或以 .md 结尾的完整笔记路径。"""
    base = normalize_rel(raw_path or "") or _clip_dir()
    if base.lower().endswith((".md", ".markdown")) and len(base) > 3:
        note_rel = base
    else:
        # 标题里的斜杠不能当目录分隔符用；截断后再清洗一次（Windows 不允许结尾空格/点）
        stem = sanitize_filename(title.replace("/", "-").replace("\\", "-"))
        filename = stem[:MAX_FILENAME_LENGTH].strip().rstrip(". ")
        note_rel = f"{base}/{filename or '未命名剪藏'}.md"
    return normalize_rel(note_rel)


def _find_by_source(vault, dir_rel: str, url: str) -> Optional[str]:
    """在目标目录内按 frontmatter.source 找已剪藏过的同一 URL。"""
    root = vault.paths.ensure_root()
    base = root / dir_rel if dir_rel else root
    if not base.is_dir():
        return None
    target = url.rstrip("/")
    for path in sorted(base.rglob("*.md"), key=lambda p: p.as_posix().lower()):
        try:
            rel = vault.paths.to_rel(path)
        except PathError:
            continue
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        frontmatter, _ = parse_frontmatter(raw)
        source = str(frontmatter.get("source") or "").strip().rstrip("/")
        if source and source == target:
            return rel
    return None


def _compose(title: str, url: str, markdown: str, clipped: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or url
    return f"# {title}\n\n> 来源：[{host}]({url})\n> 剪藏于 {clipped}\n\n{markdown}\n"


def _build_frontmatter(title: str, url: str, tags: List[str], clipped: str) -> Dict[str, Any]:
    frontmatter: Dict[str, Any] = {"title": title, "source": url, "clipped": clipped}
    if tags:
        frontmatter["tags"] = tags
    return frontmatter


# ------------------------------------------------------------------ 接口
@router.post("/preview")
async def preview(req: ClipRequest) -> Dict[str, Any]:
    """只抓取并转换成 Markdown 供确认，不写任何文件。"""
    vault = deps.require_vault()
    url = _assert_public_url(req.url)
    final_url, html, content_type = _fetch_html(url)
    extracted = _extract(final_url, html)
    target_path = _resolve_target(req.path, extracted["title"])
    return {
        "code": 0,
        "data": {
            "title": extracted["title"],
            "markdown": extracted["markdown"],
            "source": final_url,
            "requested_url": url,
            "content_type": content_type,
            "container": extracted["target"],
            "tags": _normalize_tags(req.tags),
            "suggested_path": target_path,
            "existing_path": _find_by_source(vault, target_path.rsplit("/", 1)[0] if "/" in target_path else "", final_url),
            "chars": len(extracted["markdown"]),
        },
        "message": "ok",
    }


@router.post("/url")
async def clip_url(req: ClipRequest) -> Dict[str, Any]:
    """抓取网页并写入剪藏笔记（同 source URL 已存在时原地更新）。"""
    vault = deps.require_vault()
    url = _assert_public_url(req.url)
    final_url, html, _content_type = _fetch_html(url)
    extracted = _extract(final_url, html)

    title = extracted["title"]
    markdown = extracted["markdown"]
    tags = _normalize_tags(req.tags)
    clipped = date.today().isoformat()
    target_path = _resolve_target(req.path, title)
    dir_rel = target_path.rsplit("/", 1)[0] if "/" in target_path else ""
    body = _compose(title, final_url, markdown, clipped)

    existing = _find_by_source(vault, dir_rel, final_url)
    if existing:
        # 去重：同一 URL 再次剪藏 → 原地更新正文，保留 created 与原有标签
        note = vault.read_note(existing)
        frontmatter = dict(note.get("frontmatter") or {})
        frontmatter["title"] = title
        frontmatter["source"] = final_url
        frontmatter["clipped"] = clipped
        merged_tags = tags
        if not merged_tags:
            existing_tags = frontmatter.get("tags")
            if isinstance(existing_tags, list):
                merged_tags = [str(t) for t in existing_tags if str(t).strip()]
            elif isinstance(existing_tags, str):
                merged_tags = _normalize_tags(existing_tags)
        if merged_tags:
            frontmatter["tags"] = merged_tags
        else:
            frontmatter.pop("tags", None)
        vault.write_note(existing, body, frontmatter, actor=CLIP_ACTOR)
        return {
            "code": 0,
            "data": {
                "path": existing,
                "title": title,
                "source": final_url,
                "tags": merged_tags,
                "chars": len(markdown),
                "deduped": True,
            },
            "message": "ok",
        }

    root = vault.paths.ensure_root()
    note_rel = AppPaths.unique_path(
        target_path, exists_fn=lambda p: (root / p).exists()
    )
    meta = vault.create_note(
        note_rel,
        content=body,
        frontmatter=_build_frontmatter(title, final_url, tags, clipped),
        actor=CLIP_ACTOR,
    )
    return {
        "code": 0,
        "data": {
            "path": meta.get("path") or note_rel,
            "title": title,
            "source": final_url,
            "tags": tags,
            "chars": len(markdown),
            "deduped": False,
        },
        "message": "ok",
    }
