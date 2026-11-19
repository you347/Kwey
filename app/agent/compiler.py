"""F1 知识编译层：把 raw/ 原始素材编译为 wiki/ 互链页面（proposal-first）。

设计要点：
- **只读源 / 产物分离**：``raw/`` 永不写入；Provider 产出的 ops 只能落在 ``wiki/``。
- ** proposal-first**：默认 ``dry_run=True``，只调 :func:`app.agent.ops.preview_ops`
  计算 diff，不碰磁盘；落盘必须经过显式 ``dry_run=False`` 或 API 的 confirm。
- **两阶段契约**：Provider 负责内容理解与结构化 ops，本模块负责分层约束、MOC 补全、
  体检建议；Provider 未配置 / 返回非法 ops 都给出清晰错误（上层转 400）。
- 零新依赖；所有路径为 vault 相对正斜杠，所有写入复用 VaultService。

F1 编译产物：实体页 / 概念页 / 关系链接 / MOC 索引页。关系链接若指向同批次新建页，
会被内联进该页正文（``preview_ops`` 按磁盘现状独立算 diff，不支持对同批新页二次修改）。
"""

from __future__ import annotations

from collections import Counter
from math import ceil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException

from app.agent.ops import apply_ops, preview_ops, validate_ops
from app.index.linkgraph import LinkGraph
from app.parser import frontmatter as fm_mod
from app.parser.wikilink import extract_wikilinks, split_anchor
from app.paths import PathError, is_ignored, normalize_rel, validate_relative
from app.vault.service import VaultService

DEFAULT_RAW_DIR = "raw"
DEFAULT_WIKI_DIR = "wiki"
MOC_BASENAME = "MOC.md"

DEFAULT_INSTRUCTION = (
    "你是知识编译助手。把 raw/ 中的原始素材编译为 wiki/ 下的结构化页面："
    "实体页用 wiki/entities/，概念页用 wiki/concepts/；"
    "用 create op 写页面，content 内用 [[...]] 建立关系链接；"
    "可补 tag/link op。只返回 {\"ops\":[...]}，不要写 raw/ 目录。"
)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _clean_dir(value: Optional[str], label: str, default: str) -> str:
    """校验目录参数：相对正斜杠，返回规范化结果；空值回退默认值。"""
    if value is None or not str(value).strip():
        return default
    rel = normalize_rel(str(value))
    try:
        return validate_relative(rel)
    except PathError as exc:
        raise _bad(f"字段 {label} 目录非法: {exc}") from exc


def _under(rel: str, directory: str) -> bool:
    """rel 是否位于 directory 目录内（含目录本身）。"""
    rel, directory = normalize_rel(rel), normalize_rel(directory)
    return rel == directory or rel.startswith(directory + "/")


# --------------------------------------------------------------- 分层约束
def assert_compile_ops(
    ops: Any,
    raw_dir: str = DEFAULT_RAW_DIR,
    wiki_dir: str = DEFAULT_WIKI_DIR,
) -> List[Dict[str, Any]]:
    """校验编译 ops：结构合法 + raw 只读 + 产物仅落在 wiki 目录。"""
    validated = validate_ops(ops)
    for op in validated:
        rel, op_type = op["path"], op["type"]
        if _under(rel, raw_dir):
            raise _bad(f"raw/ 是只读原始素材，禁止写入: {rel}")
        if not _under(rel, wiki_dir):
            raise _bad(f"F1 编译产物只能写入 {wiki_dir}/ 目录: {rel}")
        if op_type == "move":
            target = op["target"]
            if _under(target, raw_dir) or not _under(target, wiki_dir):
                raise _bad(f"move.target 必须留在 {wiki_dir}/ 目录内: {target}")
    return validated


# --------------------------------------------------------------- 上下文
def _list_raw_notes(vault: VaultService, raw_dir: str) -> List[str]:
    """扫描 raw/ 下 md 笔记（稳定排序，跳过系统忽略目录）。"""
    root = vault.paths.ensure_root()
    base = root.joinpath(*raw_dir.split("/"))
    if not base.is_dir():
        return []
    rels: List[str] = []
    for path in sorted(base.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not path.is_file() or path.suffix.lower() not in (".md", ".markdown"):
            continue
        try:
            rel = vault.paths.to_rel(path)
        except PathError:
            continue
        if not is_ignored(rel) and _under(rel, raw_dir):
            rels.append(rel)
    return rels


def _requested_paths(paths: Optional[Sequence[str]], raw_dir: str) -> List[str]:
    """把调用方指定的路径收敛到 raw/ 内（缺省补前缀，非法路径跳过）。"""
    result: List[str] = []
    for item in paths or []:
        try:
            rel = validate_relative(item)
        except PathError:
            continue
        if not _under(rel, raw_dir):
            rel = f"{raw_dir}/{rel}"
        result.append(rel)
    return result


def _provider_ops(provider: Any, instruction: str, paths: List[str]) -> List[Dict[str, Any]]:
    """调用 Provider.run(instruction, paths)；兼容可调用对象，异常原样上抛。"""
    runner = getattr(provider, "run", None)
    if runner is None and callable(provider):
        runner = provider
    if runner is None:
        raise _bad("provider 必须提供 run(instruction, paths) 方法")
    ops = runner(instruction, paths)
    if ops is None:
        return []
    if not isinstance(ops, list):
        raise _bad("Provider 返回的 ops 必须是数组")
    return [dict(op) for op in ops if isinstance(op, dict)]


# --------------------------------------------------------------- ops 后处理
def _append_block(content: str, block: str) -> str:
    """向 content 末尾追加独立段落（已存在则不重复追加）。"""
    block = block.strip()
    if not block:
        return content
    base = str(content or "").rstrip()
    if block in base:
        return base + ("\n" if base else "")
    return base + ("\n\n" if base else "") + block + "\n"


def _inline_into_created(
    ops: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把作用于「同批次新建页」的 link/append/tag 内联进 create.content。

    ``preview_ops`` 的每个 diff 独立基于磁盘现状计算，因此同一个 create 页无法
    再接受后续 link/append/tag op（会误判为「笔记不存在」）。内联后语义等价且可预演。
    """
    created = {op["path"] for op in ops if op["type"] == "create"}
    additions: Dict[str, List[str]] = {path: [] for path in created}
    kept: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for op in ops:
        op_type, rel = op["type"], op["path"]
        if op_type == "create":
            if not str(op.get("content") or "").strip():
                title = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                op["content"] = f"# {title}\n\n"
            kept.append(op)
            continue
        if op_type in ("link", "append", "tag") and rel in created:
            if op_type == "link":
                target = str(op.get("target") or "").strip()
                alias = str(op.get("alias") or "").strip()
                additions[rel].append(f"- [[{target}|{alias}]]" if alias else f"- [[{target}]]")
            elif op_type == "append":
                additions[rel].append(str(op.get("text") or "").strip())
            else:
                tag = str(op.get("tag") or "").strip().lstrip("#")
                additions[rel].append(f"#{tag}" if tag else "")
            warnings.append(f"已把 {op_type} 内联进新建页 {rel}（同批次新页不支持二次改动预演）")
            continue
        kept.append(op)

    for op in kept:
        if op["type"] != "create":
            continue
        unique = list(dict.fromkeys(x for x in additions.get(op["path"], []) if x))
        if unique:
            op["content"] = _append_block(op["content"], "## 关联\n" + "\n".join(unique))
    return kept, warnings


def _page_title(op: Dict[str, Any]) -> str:
    """取 create op 的标题：优先 H1，其次文件名。"""
    for line in str(op.get("content") or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return op["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _classify(op: Dict[str, Any]) -> str:
    """实体 / 概念 / 其它：优先 op.kind，其次路径片段约定。"""
    kind = str(op.get("kind") or "").strip().lower()
    if kind in ("entity", "实体"):
        return "entity"
    if kind in ("concept", "概念"):
        return "concept"
    parts = [p.lower() for p in op["path"].split("/")[:-1]]
    if any(p in ("entities", "entity", "实体") for p in parts):
        return "entity"
    if any(p in ("concepts", "concept", "概念") for p in parts):
        return "concept"
    return "other"


def _moc_block(ops: Sequence[Dict[str, Any]]) -> Tuple[str, List[str], List[str]]:
    """生成 MOC 索引段落 → (block, entity_titles, concept_titles)。"""
    groups: Dict[str, List[str]] = {"entity": [], "concept": [], "other": []}
    titles: Dict[str, List[str]] = {"entity": [], "concept": [], "other": []}
    for op in ops:
        if op["type"] != "create":
            continue
        rel = op["path"]
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        title = _page_title(op)
        kind = _classify(op)
        groups[kind].append(f"- [[{rel}|{title}]]")
        titles[kind].append(title)

    lines = ["# 知识地图（MOC）", "", "> 由 F1 知识编译自动生成；raw/ 原始素材保持只读。", ""]
    for heading, key in (("## 实体", "entity"), ("## 概念", "concept"), ("## 其它", "other")):
        lines.append(heading)
        lines.extend(groups[key] or ["- （暂无）"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", titles["entity"], titles["concept"]


def _ensure_moc(
    vault: VaultService,
    ops: List[Dict[str, Any]],
    wiki_dir: str,
) -> Tuple[List[Dict[str, Any]], List[str], List[str], List[str], str]:
    """确保 ops 里有一项生成/更新 MOC → (ops, warnings, entities, concepts, moc_path)。"""
    moc_path = f"{wiki_dir}/{MOC_BASENAME}"
    block, entities, concepts = _moc_block([op for op in ops if op["path"] != moc_path])
    existing = [op for op in ops if op["path"] == moc_path]
    warnings: List[str] = []

    if existing:
        for op in existing:
            if op["type"] == "create":
                op["content"] = _append_block(str(op.get("content") or ""), block)
            elif op["type"] == "append":
                op["text"] = _append_block(str(op.get("text") or ""), block)
            else:
                warnings.append(f"MOC 页 {moc_path} 已由 Provider 以 {op['type']} 处理，未重复补全索引")
    else:
        try:
            exists = vault.paths.to_abs(moc_path).is_file()
        except PathError:
            exists = False
        if exists:
            ops.append({"type": "append", "path": moc_path, "text": block})
            warnings.append(f"MOC 页 {moc_path} 已存在，采用追加方式刷新索引")
        else:
            ops.append({"type": "create", "path": moc_path, "content": block})

    # MOC 自身也受分层约束（防止 Provider 用 delete / move 破坏索引页）
    ops = assert_compile_ops(ops, DEFAULT_RAW_DIR, wiki_dir)
    return ops, warnings, entities, concepts, moc_path


# --------------------------------------------------------------- 编译入口
def compile_vault(
    vault: VaultService,
    provider: Any,
    raw_dir: str = DEFAULT_RAW_DIR,
    wiki_dir: str = DEFAULT_WIKI_DIR,
    dry_run: bool = True,
    instruction: Optional[str] = None,
    paths: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """编译原始素材 → 结构化 ops + diff + 报告。

    Args:
        vault: VaultService（写入一律经它）。
        provider: 提供 ``run(instruction, paths) -> list[dict]`` 的对象（可注入 fake）。
        raw_dir: 只读源目录；wiki_dir: Agent 产物目录。
        dry_run: True（默认）只预演零落盘；False 经 apply_ops 落盘。
        instruction: 附加给 Provider 的编译指令。
        paths: 指定要编译的 raw 笔记路径；缺省扫描 raw_dir 全部 md。

    Returns:
        ``{"ops": [...], "diffs": [...], "report": {...}}``；``dry_run=False`` 时
        report 额外带 ``applied``（apply_ops 的结果）。
    """
    raw = _clean_dir(raw_dir, "raw_dir", DEFAULT_RAW_DIR)
    wiki = _clean_dir(wiki_dir, "wiki_dir", DEFAULT_WIKI_DIR)
    if raw == wiki:
        raise _bad("raw_dir 与 wiki_dir 不能相同：原始素材与 Agent 产物必须分离")

    raw_notes = _list_raw_notes(vault, raw)
    context_paths = _requested_paths(paths, raw) if paths else raw_notes
    prompt = f"{instruction.strip()}\n\n{DEFAULT_INSTRUCTION}" if instruction else DEFAULT_INSTRUCTION

    ops = _provider_ops(provider, prompt, context_paths)
    warnings: List[str] = []
    if not ops:
        warnings.append("Provider 未返回任何 ops；仅生成空 MOC 索引（如有必要）")

    ops, inline_warnings = _inline_into_created(assert_compile_ops(ops, raw, wiki))
    warnings.extend(inline_warnings)
    ops, moc_warnings, entities, concepts, moc_path = _ensure_moc(vault, ops, wiki)
    warnings.extend(moc_warnings)

    preview = preview_ops(ops, vault)  # 纯计算：绝不写盘
    report: Dict[str, Any] = {
        "instruction": prompt,
        "raw_dir": raw,
        "wiki_dir": wiki,
        "raw_notes": len(raw_notes),
        "compiled_paths": context_paths,
        "op_count": len(ops),
        "entity_pages": entities,
        "concept_pages": concepts,
        "moc_path": moc_path,
        "dry_run": bool(dry_run),
        "warnings": warnings,
    }
    data: Dict[str, Any] = {"ops": ops, "diffs": preview["diffs"], "report": report}
    if not dry_run:
        report["applied"] = apply_ops(ops, vault)
    return data


# --------------------------------------------------------------- 体检
def health_check(
    vault: VaultService,
    wiki_dir: str = DEFAULT_WIKI_DIR,
) -> Dict[str, Any]:
    """全库体检 → ``{orphans, broken_links, schema_drift, missing_moc}``。

    只读磁盘、不改索引：用 :class:`LinkGraph` 的解析规则（精确路径 → 相对源目录 →
    basename）判断断链；孤儿 = 无入链且无出链；schema_drift = 缺少多数笔记都有的
    frontmatter 字段；missing_moc = 没有 MOC 页或笔记未被任何 MOC 索引。
    """
    root = vault.paths.ensure_root()
    graph = LinkGraph()
    notes: List[str] = []

    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not path.is_file():
            continue
        try:
            rel = vault.paths.to_rel(path)
        except PathError:
            continue
        if is_ignored(rel):
            continue
        if path.suffix.lower() in (".md", ".markdown"):
            notes.append(rel)
            graph.upsert_note(rel)
        else:
            graph.upsert_file(rel)

    broken_links: List[Dict[str, Any]] = []
    out_links: Dict[str, Set[str]] = {}
    in_links: Dict[str, Set[str]] = {}
    fields: Dict[str, Dict[str, Any]] = {}

    for rel in notes:
        abs_path = root.joinpath(*rel.split("/"))
        try:
            raw = abs_path.read_text(encoding="utf-8-sig")
        except OSError:
            raw = ""
        fm, body = fm_mod.parse_frontmatter(raw)
        fields[rel] = fm

        outs: Set[str] = set()
        for ref in extract_wikilinks(body):
            target, _anchor = split_anchor(ref.target)
            target = target.strip()
            if not target or target.startswith("#") or "://" in target:
                continue  # 锚点 / 外部链接不参与库内体检
            resolved = graph.resolve(target, source_path=rel)
            if resolved is None:
                broken_links.append(
                    {
                        "source": rel,
                        "target": target,
                        "line": ref.line,
                        "kind": ref.kind,
                        "suggestion": f"新建 {target}.md，或把 [[{target}]] 改为已存在笔记名",
                    }
                )
            else:
                outs.add(resolved)
                in_links.setdefault(resolved, set()).add(rel)
        out_links[rel] = outs

    orphans = [
        {
            "path": rel,
            "suggestion": f"补充双链，或在 {wiki_dir}/MOC.md 中列入 [[{rel[:-3] if rel.lower().endswith('.md') else rel}]]",
        }
        for rel in notes
        if not out_links.get(rel) and not in_links.get(rel)
    ]

    # 字段漂移：过半笔记都带的字段视为基准 schema
    schema_drift: List[Dict[str, Any]] = []
    with_fm = [rel for rel in notes if fields[rel]]
    if with_fm:
        counts = Counter(key for rel in with_fm for key in fields[rel])
        threshold = max(1, ceil(len(with_fm) / 2))
        baseline = {key for key, count in counts.items() if count >= threshold}
        for rel in notes:
            missing = sorted(baseline - set(fields[rel]))
            if missing:
                schema_drift.append(
                    {
                        "path": rel,
                        "missing": missing,
                        "suggestion": "补齐 frontmatter 字段 " + ", ".join(missing) + "，或统一笔记模板",
                    }
                )

    wiki = _clean_dir(wiki_dir, "wiki_dir", DEFAULT_WIKI_DIR)
    mocs = [rel for rel in notes if Path(rel).name.upper().startswith("MOC")]
    if not mocs:
        missing_moc = [
            {
                "path": f"{wiki}/{MOC_BASENAME}",
                "reason": "未找到 MOC 索引页",
                "suggestion": "运行 F1 知识编译生成 MOC 索引页",
            }
        ]
    else:
        indexed: Set[str] = set(mocs)
        for moc in mocs:
            indexed |= out_links.get(moc, set())
        missing_moc = [
            {
                "path": rel,
                "reason": "未被 MOC 索引",
                "suggestion": f"在 {mocs[0]} 中补充 [[{rel[:-3] if rel.lower().endswith('.md') else rel}]]",
            }
            for rel in notes
            if rel not in indexed
        ]

    return {
        "orphans": orphans,
        "broken_links": broken_links,
        "schema_drift": schema_drift,
        "missing_moc": missing_moc,
    }
