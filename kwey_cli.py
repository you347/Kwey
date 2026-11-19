#!/usr/bin/env python3
"""Kwey CLI（A5 可编程入口）：对标 Obsidian 1.12 CLI，Agent 无需 UI 即可脚本化操作 vault。

用法::

    python kwey_cli.py search --vault "D:/我的知识库" "排课" --json
    python kwey_cli.py read   --vault "D:/我的知识库" notes/读书/2026计划.md --json
    python kwey_cli.py write  --vault "D:/我的知识库" notes/新笔记.md --content "# 标题" --json
    echo "正文" | python kwey_cli.py write --vault "D:/我的知识库" notes/管道.md --stdin
    python kwey_cli.py tree   --vault "D:/我的知识库"
    python kwey_cli.py export --vault "D:/我的知识库" notes/读书/2026计划.md --format html --out D:/tmp/plan.html

设计约束：
- 标准库 argparse；子命令 search / read / write / tree / export；**不实现 serve**（与 main.py 重复）。
- 直接 ``deps.init_services(root, open_now=True)`` 后调服务层；与 REST/MCP/UI 同一事实源。
- **写必须经 VaultService**（统一写路径：原子写 + 增量索引 + 事件广播），禁止直接 open()/write_text()。
  （唯一例外：``export --out`` 写 vault 外的导出文件，以及 ``export md`` 为与 /api/export 保持
  字节级一致而原样只读磁盘全文——均为只读/旁路导出，不触碰 vault 笔记写路径。）
- ``--json`` 输出统一信封 ``{code, data, message}``（与 REST API 一致）；成功 code=0。
- 退出码：0 成功；1 运行期错误（HTTPException / 未预期异常）；2 用法错误（argparse / vault 不存在 /
  参数非法）。所有错误信息写 **stderr**。
- 与 kwey_mcp.py 相同：**不启动 watcher 线程**（deps.init_services 仅 bind_watcher），
  进程退出前 ``deps.reset_services()`` 释放 SQLite 连接。
- 零新增第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 保证以脚本方式从任意 cwd 启动时都能 import 项目包
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402

from app.api import deps  # noqa: E402
from app.api.routes_export import (  # noqa: E402
    _PRINT_CSS,
    _document,
    _inline_images,
)
from app.api.routes_search import (  # noqa: E402
    _fts_search,
    _hybrid_search,
    build_match_terms,
    parse_query,
)
from app.parser.renderer import render_markdown  # noqa: E402
from app.paths import normalize_rel  # noqa: E402

MARKDOWN_EXTS = (".md", ".markdown")

# CLI 复用 /api/search 与 /api/export 的私有渲染/检索函数（_fts_search/_hybrid_search/
# _document/_inline_images/_PRINT_CSS）：与 UI 输出保持行为一致，避免逻辑漂移。
# 若这些函数重命名，需同步修改本文件（属于有意的耦合取舍，见报告）。


class CliError(Exception):
    """CLI 用法级错误：message 写 stderr，退出码 code（默认 1）。"""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _force_utf8() -> None:
    """Windows 控制台默认可能是 cp936/gbk，强制 stdio 使用 UTF-8。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 —— 某些被替换的流不支持 reconfigure
            pass


def _strip_marks(text: str) -> str:
    """snippet 后处理（人类可读输出用）：剥离 <mark></mark> 高亮标签，
    并把换行/连续空白折叠为单空格（FTS5 snippet 保留原文换行，多行会打乱列表排版）。
    --json 输出保留 snippet 原样，与 /api/search 一致。"""
    text = re.sub(r"</?mark>", "", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------- handlers
def _cmd_search(args: argparse.Namespace) -> Dict[str, Any]:
    """search：FTS5 BM25 + path:/tag:/file: 操作符（--mode hybrid 复用 API 融合逻辑）。"""
    if not (1 <= args.limit <= 200):
        raise CliError("--limit 取值范围为 1-200", code=2)
    db = deps.require_db()
    parsed = parse_query(args.query)
    match_terms = build_match_terms(parsed["terms"])  # type: ignore[arg-type]

    started = time.perf_counter()
    vector_info: Optional[Dict[str, Any]] = None
    if args.mode == "hybrid":
        hits, total, vector_info = _hybrid_search(db, parsed, match_terms, args.limit)
    else:
        hits, total = _fts_search(db, parsed, match_terms, args.limit)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    # data 形状与 GET /api/search 完全一致
    data: Dict[str, Any] = {
        "hits": hits,
        "total": total,
        "elapsed_ms": elapsed_ms,
        "mode": args.mode,
    }
    if vector_info is not None:
        data["vector"] = vector_info
    return data


def _cmd_read(args: argparse.Namespace) -> Dict[str, Any]:
    """read：读单篇笔记（frontmatter + 正文 + 元信息）。"""
    vault = deps.require_vault()
    return vault.read_note(args.path)


def _cmd_write(args: argparse.Namespace) -> Dict[str, Any]:
    """write：新建或覆盖（upsert 语义）。写路径统一走 VaultService。

    - 已存在 → write_note（保留原 frontmatter，只替换正文）；
    - 不存在 → create_note（自动补 created/updated，空正文生成 ``# 标题`` 骨架）；
    - 扩展名缺失自动补 .md。
    """
    vault = deps.require_vault()
    rel = normalize_rel(args.path)
    if not rel.lower().endswith(MARKDOWN_EXTS):
        rel += ".md"
    content = sys.stdin.read() if args.stdin else args.content

    try:
        res = vault.write_note(rel, content)
        return {
            "path": rel,
            "action": "updated",
            "links_changed": res["links_changed"],
            "mtime": res["mtime"],
        }
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        vault.create_note(rel, content)
        note = vault.db.get_note(rel) or {}
        return {
            "path": rel,
            "action": "created",
            "links_changed": 0,
            "mtime": note.get("mtime"),
        }


def _cmd_tree(args: argparse.Namespace) -> Dict[str, Any]:
    """tree：嵌套文件树（隐藏 .kwey/.trash/.git/templates；目录在前）。"""
    vault = deps.require_vault()
    return {"name": vault.paths.name, "tree": vault.tree()}


def _cmd_export(args: argparse.Namespace) -> Dict[str, Any]:
    """export：导出 md / html / print（打印 HTML）。**不实现 pdf**（CLI 场景 print 即可再打印）。

    - md：原样读取磁盘全文（含 frontmatter），与 GET /api/export?format=md 字节级一致；
    - html/print：render_markdown + 图片 base64 内联 + 独立 HTML 文档（print 追加分页 CSS），
      与 UI 导出产物一致。
    """
    vault = deps.require_vault()
    doc = vault.read_note(args.path)
    rel = normalize_rel(doc["path"])
    basename = rel.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]

    if args.format == "md":
        # 与 /api/export md 一致：原样只读磁盘全文（含 frontmatter），不重建序列化。
        content = vault.paths.to_abs(rel).read_text(encoding="utf-8-sig")
        filename = basename
    else:
        linkgraph = deps.require_linkgraph()
        resolver = lambda target, source=None: linkgraph.resolve(target)  # noqa: E731
        body_html = render_markdown(doc["content"], resolve=resolver)
        body_html = _inline_images(body_html, vault.paths.root)
        if args.format == "html":
            content = _document(doc["title"], body_html)
        else:  # print
            content = _document(doc["title"], body_html, _PRINT_CSS)
        filename = f"{stem}.html"

    if args.out:
        out = Path(args.out).expanduser().resolve()
        root = Path(str(vault.paths.root)).resolve()
        if out == root or root in out.parents:
            raise CliError(
                "--out 不能位于 vault 内（导出文件绕过统一写路径）；"
                "请导出到 vault 之外，或不带 --out 直接重定向 stdout",
                code=2,
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")  # vault 外的导出产物，非笔记写路径
        return {
            "path": rel,
            "format": args.format,
            "out": str(out),
            "filename": filename,
            "bytes": len(content.encode("utf-8")),
        }
    return {"path": rel, "format": args.format, "filename": filename, "content": content}


HANDLERS: Dict[str, Callable[[argparse.Namespace], Dict[str, Any]]] = {
    "search": _cmd_search,
    "read": _cmd_read,
    "write": _cmd_write,
    "tree": _cmd_tree,
    "export": _cmd_export,
}


# --------------------------------------------------------------------- output
def _emit_json(data: Dict[str, Any]) -> None:
    """统一信封 {code, data, message}（与 REST API 一致），单行紧凑 JSON。"""
    envelope = {"code": 0, "data": data, "message": "ok"}
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _emit_human(args: argparse.Namespace, data: Dict[str, Any]) -> None:
    """人类可读输出：检索摘要 / 正文直出 / 单行结果 / ASCII 树 / 内容直出。"""
    command = args.command
    if command == "search":
        hits: List[Dict[str, Any]] = data["hits"]
        if not hits:
            print("（无结果）")
            return
        for i, hit in enumerate(hits, 1):
            print(f"{i}. [{hit['score']:.3f}] {hit['path']}  ·  {hit['title']}")
            snip = _strip_marks(hit.get("snippet") or "")
            if snip:
                print(f"   {snip}")
    elif command == "read":
        # 直出正文（不含 frontmatter）；完整原文请用 export --format md
        content: str = data["content"]
        sys.stdout.write(content if content.endswith("\n") else content + "\n")
    elif command == "write":
        verb = "已创建" if data["action"] == "created" else "已更新"
        print(f"{verb} {data['path']}（链接变更 {data['links_changed']} 处）")
    elif command == "tree":
        print(f"{data['name']}/")

        def walk(nodes: List[Dict[str, Any]], prefix: str) -> None:
            for i, node in enumerate(nodes):
                last = i == len(nodes) - 1
                connector = "└── " if last else "├── "
                name = node["name"] + ("/" if node["type"] == "dir" else "")
                print(prefix + connector + name)
                children = node.get("children")
                if children:
                    walk(children, prefix + ("    " if last else "│   "))

        walk(data["tree"], "")
    elif command == "export":
        if data.get("out"):
            print(f"已导出 {data['path']} → {data['out']}（{data['format']}，{data['bytes']} 字节）")
        else:
            content = data["content"]
            sys.stdout.write(content if content.endswith("\n") else content + "\n")


# --------------------------------------------------------------------- parser
def _add_shared_args(sp: argparse.ArgumentParser) -> None:
    """在子命令上重复登记 --vault/--json（default=SUPPRESS 不覆盖主命令已解析值），
    使参数既可放在子命令之前也可放在之后。SUPPRESS 同时在子命令帮助中隐藏重复项。"""
    sp.add_argument("--vault", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kwey_cli.py",
        description="Kwey CLI：无需 UI 即可脚本化操作 vault（检索 / 读取 / 写入 / 文件树 / 导出）。",
        epilog=(
            "示例：\n"
            '  python kwey_cli.py search --vault "D:/我的知识库" "排课" --json\n'
            '  python kwey_cli.py read --vault "D:/我的知识库" notes/读书/2026计划.md\n'
            '  python kwey_cli.py write --vault "D:/我的知识库" notes/新.md --content "# 标题" --json\n'
            '  echo 正文 | python kwey_cli.py write --vault "D:/我的知识库" notes/管道.md --stdin\n'
            '  python kwey_cli.py export --vault "D:/我的知识库" notes/x.md --format html --out D:/tmp/x.html\n'
            "退出码：0 成功；1 运行期错误；2 用法错误（错误信息均在 stderr）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vault", help="vault 根目录（建议绝对路径），必须提供")
    parser.add_argument("--json", action="store_true", help="输出 {code,data,message} JSON 信封（机器可读）")

    sub = parser.add_subparsers(dest="command", metavar="命令", required=True)

    sp_search = sub.add_parser(
        "search", help="FTS5 全文检索，支持 path:/tag:/file: 操作符",
        description='检索词可混合操作符，如 "排课 path:notes tag:读书 file:计划"',
    )
    sp_search.add_argument("query", help="检索词，可混合 path:/tag:/file: 操作符")
    sp_search.add_argument("--limit", type=int, default=50, help="返回条数上限（1-200，默认 50）")
    sp_search.add_argument(
        "--mode", choices=("fts", "hybrid"), default="fts",
        help="fts（默认）；hybrid = FTS5+向量+链接图（向量不可用自动降级）",
    )
    _add_shared_args(sp_search)

    sp_read = sub.add_parser("read", help="读单篇笔记（输出正文；--json 含 frontmatter）")
    sp_read.add_argument("path", help="vault 相对正斜杠路径，如 notes/读书/2026计划.md")
    _add_shared_args(sp_read)

    sp_write = sub.add_parser("write", help="新建或覆盖笔记（upsert；写经 VaultService 统一写路径）")
    sp_write.add_argument("path", help="vault 相对正斜杠路径（缺 .md 自动补）")
    group = sp_write.add_mutually_exclusive_group(required=True)
    group.add_argument("--content", help="笔记正文（frontmatter 保留/自动生成）")
    group.add_argument("--stdin", action="store_true", help="从 stdin 读取正文（适合多行/管道）")
    _add_shared_args(sp_write)

    sp_tree = sub.add_parser("tree", help="嵌套文件树（隐藏系统目录）")
    _add_shared_args(sp_tree)

    sp_export = sub.add_parser("export", help="导出笔记为 md / html / print（打印 HTML）")
    sp_export.add_argument("path", help="vault 相对正斜杠路径")
    sp_export.add_argument(
        "--format", choices=("md", "html", "print"), default="md",
        help="md=原始全文（含 frontmatter）；html=独立 HTML（图片内联）；print=带分页 CSS 的打印 HTML",
    )
    sp_export.add_argument("--out", help="输出文件路径（必须在 vault 之外）；缺省写到 stdout")
    _add_shared_args(sp_export)

    return parser


# --------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。返回退出码：0 成功 / 1 运行期错误 / 2 用法错误。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    vault = getattr(args, "vault", None)
    if not vault:
        parser.error("缺少 --vault <绝对路径>（可放在子命令前或后）")  # SystemExit(2)

    root = Path(vault).expanduser().resolve()
    if not root.is_dir():
        print(f"[kwey-cli] vault 目录不存在或不是目录: {root}", file=sys.stderr)
        return 2

    try:
        # 全量建索引；不启动 watcher 线程（同 kwey_mcp.py 的 stdio 场景取舍）。
        deps.init_services(root, open_now=True)
        data = HANDLERS[args.command](args)
    except HTTPException as exc:
        print(f"[kwey-cli] 错误 [{exc.status_code}]: {exc.detail}", file=sys.stderr)
        return 1
    except CliError as exc:
        print(f"[kwey-cli] {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("[kwey-cli] 已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 —— CLI 兜底：任何异常都给非零退出码
        print(f"[kwey-cli] 未预期错误: {exc!r}", file=sys.stderr)
        return 1
    finally:
        deps.reset_services()  # 释放 SQLite 连接；未启动的 watcher.stop 无副作用

    if getattr(args, "json", False):
        _emit_json(data)
    else:
        _emit_human(args, data)
    return 0


if __name__ == "__main__":
    _force_utf8()
    raise SystemExit(main())
