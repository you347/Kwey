"""Kwey 能力契约注册表（卡A / P1-22）：内置智能体与外部 Agent 的唯一能力真源。

设计要点
--------
- 契约 v1：工具元数据（name/description/input_schema/permission/side_effect/
  journal_action）+ 边界 schema 校验 + 统一 invoke。v1 内只增不改（additive-only）。
- 传输层改薄：MCP（mcp_server.py）与 REST（routes_agent.py）都只做鉴权/ framing，
  能力语义一律经 ``invoke``，内置智能体（卡B）将来作为进程内客户端走同一入口。
- 错误模型：``ContractError(400)`` 参数校验失败 / ``ContractError(404)`` 未知工具；
  业务异常沿用 VaultService 的 HTTPException（404/409/400），由传输层各自映射
  （MCP → isError + code；REST → 统一异常包装）。
- journal 收口（W2）：invoke(actor={"role","name"}) 成功执行后按 journal_action
  留痕；actor=None 不留痕（进程内纯调用）。传输层负责传入 actor：REST 取 token
  记录，MCP stdio 为 admin 等效本地父进程。journal 失败绝不阻断写路径。
- 副作用分级：read（无落盘）/ reversible（回收站或自动快照可恢复）/
  irreversible（真实抹除，admin + confirm）。
- 危险操作 confirm 闸门：dir.delete 非空 / history.rollback / trash.purge /
  organize.apply 必须显式 confirm=true，否则 400/409。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.agent import journal
from app.api import deps
from app.api.routes_search import _hybrid_search, build_match_terms, parse_query
from app.paths import normalize_rel
from app.vault.service import VaultService

CONTRACT_VERSION = 1


class ContractError(Exception):
    """契约层错误：code 取 HTTP 语义（400 校验 / 404 未知工具），传输层负责映射。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 参数校验（schema 边界）
_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _validate(schema: Dict[str, Any], args: Dict[str, Any]) -> None:
    """required presence + 类型 / enum / 数值范围 / 数组元素类型。

    可选参数传 None 视同缺省（沿用 MCP 旧行为）；未知键放行（JSON Schema
    additionalProperties 默认 true）。非空字符串等业务约束留在 handler 内。
    """
    for key in schema.get("required", []):
        if key not in args or args[key] is None:
            raise ContractError(400, f"缺少必填参数 {key}")
    props = schema.get("properties", {})
    for key, value in args.items():
        spec = props.get(key)
        if spec is None or value is None:
            continue
        types = spec.get("type")
        type_list = types if isinstance(types, list) else [types]
        if not any(_TYPE_CHECKS[t](value) for t in type_list if t in _TYPE_CHECKS):
            raise ContractError(400, f"{key} 类型不合法（期望 {'/'.join(type_list)}）")
        if isinstance(value, list):
            items = spec.get("items") or {}
            item_type = items.get("type")
            if item_type in _TYPE_CHECKS and not all(_TYPE_CHECKS[item_type](v) for v in value):
                raise ContractError(400, f"{key} 的元素必须是 {item_type}")
        if "enum" in spec and value not in spec["enum"]:
            allowed = "/".join(str(v) for v in spec["enum"])
            raise ContractError(400, f"{key} 必须是 {allowed} 之一")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise ContractError(400, f"{key} 不能小于 {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                raise ContractError(400, f"{key} 不能大于 {spec['maximum']}")


def _req_str(a: Dict[str, Any], key: str) -> str:
    value = a.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(400, f"{key} 必须是非空字符串")
    return value


def _opt_int(a: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    """可选整数参数：拒绝 bool 与字符串，越界报 400（schema 之外再兜一层）。"""
    value = a.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(400, f"{key} 必须是整数")
    if value < lo or value > hi:
        raise ContractError(400, f"{key} 超出范围 {lo}-{hi}")
    return value


def _as_dict(value: Any, key: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractError(400, f"{key} 必须是对象")
    return value


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ContractError(400, "add/remove 必须是字符串数组")


# ---------------------------------------------------------------- 工具实现（9 个，canonical 语义）
def _search(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    q = a["query"]  # schema 保证 str；空串 = 纯操作符过滤浏览（与 REST 旧语义一致）
    mode = a.get("mode", "fts")
    if mode not in ("fts", "hybrid"):
        mode = "fts"  # 非法 mode 回落 fts；hybrid 走下方 RRF 融合分支
    limit = a.get("limit", 50)
    parsed = parse_query(q)
    match_terms = build_match_terms(parsed["terms"])  # type: ignore[arg-type]
    vector_info = None
    if mode == "hybrid":
        # A2：真·混合检索（FTS + 向量 + 链接图 RRF）；向量不可用时自动降级纯 FTS
        hits, total, vector_info = _hybrid_search(vault.db, parsed, match_terms, limit)
    else:
        hits, total = vault.db.search(
            terms=match_terms,
            path_prefix=parsed["path"],  # type: ignore[arg-type]
            tag=parsed["tag"],  # type: ignore[arg-type]
            file_pattern=parsed["file"],  # type: ignore[arg-type]
            limit=limit,
        )
    result = {"hits": hits, "total": total, "query": q, "mode": mode}
    if vector_info is not None:
        result["vector"] = vector_info  # 标注后端状态，便于 Agent 判断召回质量
    return result


def _read(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    """读笔记正文；默认最多 8000 字符，长文按 offset/max_chars 续读（结果截断即声明）。"""
    doc = vault.read_note(_req_str(a, "path"))
    content = doc["content"]
    total = len(content)
    offset = min(_opt_int(a, "offset", 0, 0, 1_000_000), total)
    limit = _opt_int(a, "max_chars", 8000, 1, 40000)
    end = min(total, offset + limit)
    doc["content"] = content[offset:end]
    doc["offset"] = offset
    doc["total_chars"] = total
    doc["truncated"] = end < total
    return doc


def _open(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    """校验路径存在并返回「让界面打开」的意图（正文不回流，避免智能体复述全文）。"""
    raw = _req_str(a, "path")
    rel = normalize_rel(raw)
    if not rel:
        raise ContractError(400, "path 不能为空")
    root = vault.paths.root.resolve()
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise ContractError(400, "path 越出 vault 根目录")
    if target.is_dir():
        return {"path": rel, "kind": "dir"}
    if not target.is_file():
        raise ContractError(404, f"路径不存在: {raw}")
    kind = "note" if target.suffix.lower() in (".md", ".markdown") else "file"
    return {"path": rel, "kind": kind}


def _create(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    content = a.get("content", "") or ""
    fm = _as_dict(a.get("frontmatter"), "frontmatter")
    return vault.create_note(path, content=content, frontmatter=fm)


def _write(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    content = a["content"]  # required + schema str；允许空串（清空正文，REST 旧语义）
    fm = _as_dict(a.get("frontmatter"), "frontmatter")
    base_mtime = a.get("base_mtime")
    if base_mtime is not None:
        base_mtime = float(base_mtime)
    return vault.write_note(path, content, frontmatter=fm, base_mtime=base_mtime)


def _delete(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    trash_rel = vault.delete_note(path)
    return {"path": path, "trash_path": trash_rel}


def _link(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    src = _req_str(a, "from")
    target = _req_str(a, "to")
    note = vault.read_note(src)
    body = note["content"]
    # 末尾追加双链；保留 frontmatter（含自定义字段），统一走 write_note。
    new_body = body.rstrip("\n") + f"\n\n[[{target}]]\n"
    res = vault.write_note(src, new_body, frontmatter=note["frontmatter"])
    return {"path": src, "link": target, **res}


def _tags(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    add = _as_str_list(a.get("add"))
    remove = _as_str_list(a.get("remove"))
    note = vault.read_note(path)
    fm = dict(note["frontmatter"])
    raw = fm.get("tags") or []
    tags: List[str] = [raw] if isinstance(raw, str) else [str(t) for t in raw]
    for tag in add:
        if tag not in tags:
            tags.append(tag)
    remove_set = set(remove)
    tags = [t for t in tags if t not in remove_set]
    fm["tags"] = tags
    res = vault.write_note(path, note["content"], frontmatter=fm, base_mtime=note["mtime"])
    return {"path": path, "tags": tags, **res}


def _graph_query(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = normalize_rel(_req_str(a, "path"))
    depth = a.get("depth", 1)
    graph = vault.indexer.linkgraph
    visited = {path}
    frontier = {path}
    nodes: Dict[str, Dict[str, List[str]]] = {}
    for _ in range(depth):
        next_frontier = set()
        for cur in sorted(frontier):
            ins = sorted(graph.backlinks(cur))
            outs = sorted(graph.out_links_of(cur))
            nodes[cur] = {"in": ins, "out": outs}
            for nb in ins + outs:
                if nb not in visited:
                    visited.add(nb)
                    next_frontier.add(nb)
        frontier = next_frontier
        if not frontier:
            break
    return {"path": path, "depth": depth, "nodes": nodes, "reachable": sorted(visited)}


def _organize(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    instruction = _req_str(a, "instruction")
    paths = a.get("paths")
    from app.agent.ops import preview_ops
    from app.agent.provider import AgentProvider

    # Provider 生成结构化 ops → diff 预演（不落盘，落盘须用户确认）
    provider = AgentProvider(Path(vault.paths.root))
    if not provider.configured:
        return {
            "status": "provider_not_configured",
            "hint": "配置 .kwey/provider.json 后可用",
        }
    try:
        ops = provider.run(instruction, paths)
        preview = preview_ops(ops, vault)
    except Exception as exc:  # noqa: BLE001 —— 外部 LLM/HTTP 失败以数据形式返回
        return {"status": "provider_error", "message": str(exc)}
    return {"status": "ok", "preview": preview}


# ---------------------------------------------------------------- 工具实现（W2 新增 12 个）
def _tree(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    return {"tree": vault.tree()}


def _trash_list(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    return {"items": vault.list_trash()}


def _history_log(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    snap = deps.require_snapshots()
    path = a.get("path")
    return {"entries": snap.log(rel=path or None, limit=a.get("limit", 50))}


def _history_rollback(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    if not a.get("confirm", False):
        raise ContractError(400, "回滚需要 confirm=true 显式确认")
    hash_ = _req_str(a, "hash")
    path = _req_str(a, "path")
    snap = deps.require_snapshots()
    return snap.rollback(hash_, path, vault=vault)


def _append(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    text = _req_str(a, "text")
    note = vault.read_note(path)
    body = note["content"]
    # 与 ops._append_text 同语义：原有内容一字不改，追加内容独立成行
    sep = "" if (body == "" or body.endswith("\n")) else "\n"
    suffix = "" if text.endswith("\n") else "\n"
    res = vault.write_note(
        path, body + sep + text + suffix,
        frontmatter=note["frontmatter"], base_mtime=note["mtime"],
    )
    return {"path": path, **res}


def _move(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    target = a["target"]  # schema str；空串 = 移动到根目录
    result = vault.move_note(path, normalize_rel(target))
    return {"path": path, **result}


def _rename(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    name = _req_str(a, "name")
    result = vault.rename_note(path, name)
    return {"path": path, **result}


def _dir_create(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    return {"path": vault.create_dir(_req_str(a, "path"))}


def _dir_delete(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    path = _req_str(a, "path")
    trash_rel = vault.delete_dir(path, confirm=bool(a.get("confirm", False)))
    return {"path": path, "trash_path": trash_rel}


def _trash_restore(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    trash_path = _req_str(a, "trash_path")
    result = vault.restore_from_trash(trash_path, a.get("target_path"))
    return {"trash_path": trash_path, **result}


def _trash_purge(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    if not a.get("confirm", False):
        raise ContractError(400, "永久删除需要 confirm=true 显式确认")
    trash_path = a.get("trash_path")
    if trash_path is not None:
        trash_path = _req_str(a, "trash_path")
        return {"purged": vault.purge_trash_path(trash_path), "trash_path": trash_path}
    return {"purged": vault.purge_trash_all()}


def _organize_apply(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    if not a.get("confirm", False):
        raise ContractError(
            400, "需要用户确认：请先预演（kwey.organize）并把 confirm 设为 true 后再应用"
        )
    from app.agent.ops import apply_ops, validate_ops

    ops = a["ops"]
    validate_ops(ops)
    return apply_ops(ops, vault)


# ---------------------------------------------------------------- 工具注册（契约 v1）
_TOOLS: Dict[str, Dict[str, Any]] = {}


def _register(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    permission: str,
    side_effect: str,
    journal_action: Optional[str],
    handler: Callable[[VaultService, Dict[str, Any]], Any],
) -> None:
    _TOOLS[name] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "permission": permission,
        "side_effect": side_effect,
        "journal_action": journal_action,
        "handler": handler,
    }


_register(
    "kwey.search",
    "搜索笔记：mode=fts 用 FTS5 BM25；mode=hybrid 用 FTS+向量+链接图 RRF 融合（向量不可用时降级 FTS）。支持 path:/tag:/file: 操作符，返回 hits。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词，可含 path:/tag:/file: 操作符；空串为纯过滤浏览"},
            "limit": {"type": "integer", "description": "返回条数上限（1-200，默认 50）", "minimum": 1, "maximum": 200},
            "mode": {"type": "string", "description": "fts（默认 BM25）；hybrid = FTS+向量+链接图 RRF 融合，向量不可用时降级纯 FTS", "enum": ["fts", "hybrid"]},
        },
        "required": ["query"],
    },
    "read", "read", None, _search,
)
_register(
    "kwey.open",
    "在界面中打开笔记或文件夹：笔记在中心编辑区打开，文件夹在左侧文件树定位。用户说「打开/看看/展示/定位」某个路径时用它，不要用 kwey.read 复述正文。",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "vault 相对路径（笔记或目录）"}},
        "required": ["path"],
    },
    "read", "read", None, _open,
)
_register(
    "kwey.read",
    "读取笔记正文（默认最多 8000 字符）。长笔记用 offset 续读，按 truncated / total_chars 判断是否还有剩余。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径"},
            "offset": {
                "type": "integer",
                "description": "从第几个字符开始读（默认 0）",
                "minimum": 0,
                "maximum": 1000000,
                "default": 0,
            },
            "max_chars": {
                "type": "integer",
                "description": "本次最多返回多少字符（默认 8000，上限 40000）",
                "minimum": 1,
                "maximum": 40000,
                "default": 8000,
            },
        },
        "required": ["path"],
    },
    "read", "read", None, _read,
)
_register(
    "kwey.create",
    "新建笔记（已存在则 409 冲突），走 VaultService 统一写路径。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径（缺省自动补 .md）"},
            "content": {"type": "string", "description": "正文，缺省生成标题行"},
            "frontmatter": {"type": "object", "description": "frontmatter 字段字典（可选）"},
        },
        "required": ["path"],
    },
    "write", "reversible", "create", _create,
)
_register(
    "kwey.write",
    "覆盖写入笔记正文；可带 frontmatter 与 base_mtime 乐观锁（冲突 409）。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径"},
            "content": {"type": "string", "description": "新正文（允许空串清空）"},
            "frontmatter": {"type": "object", "description": "不传则保留磁盘现有 frontmatter"},
            "base_mtime": {"type": "number", "description": "乐观锁：与磁盘 mtime 不符抛 409"},
        },
        "required": ["path", "content"],
    },
    "write", "reversible", "update", _write,
)
_register(
    "kwey.delete",
    "删除笔记（移入 .trash/ 回收站，不直接抹除）。",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "vault 相对路径"}},
        "required": ["path"],
    },
    "write", "reversible", "delete", _delete,
)
_register(
    "kwey.link",
    "在 from 笔记末尾追加 [[to]] 双链，走 write_note 统一写路径。",
    {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "源笔记 vault 相对路径"},
            "to": {"type": "string", "description": "目标（wikilink 文本，如 2026计划 或 notes/x.md）"},
        },
        "required": ["from", "to"],
    },
    "write", "reversible", "link", _link,
)
_register(
    "kwey.tags",
    "增/删 frontmatter 的 tags（保序去重），走 write_note 统一写路径。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径"},
            "add": {"type": ["array", "string"], "items": {"type": "string"}, "description": "要新增的标签（也兼容单个字符串）"},
            "remove": {"type": ["array", "string"], "items": {"type": "string"}, "description": "要移除的标签（也兼容单个字符串）"},
        },
        "required": ["path"],
    },
    "write", "reversible", "tags", _tags,
)
_register(
    "kwey.graph.query",
    "查询笔记的链接邻域（基于内存 LinkGraph 的 in/out，BFS 到指定 depth）。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径"},
            "depth": {"type": "integer", "description": "邻域层数（1-5，默认 1）", "minimum": 1, "maximum": 5},
        },
        "required": ["path"],
    },
    "read", "read", None, _graph_query,
)
_register(
    "kwey.organize",
    "按自然语言指令整理笔记（预览，不落盘）；需配置 Provider 后可用。",
    {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "整理指令（自然语言）"},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "限定整理的笔记路径（可选）"},
        },
        "required": ["instruction"],
    },
    "read", "read", None, _organize,
)
_register(
    "kwey.tree",
    "嵌套文件树（隐藏 .kwey/.trash/.git；目录在前，名称不区分大小写排序）。",
    {"type": "object", "properties": {}},
    "read", "read", None, _tree,
)
_register(
    "kwey.trash.list",
    "列出回收站顶层条目（文件/目录，mtime 倒序），含 trash_path/original_path/is_dir。",
    {"type": "object", "properties": {}},
    "read", "read", None, _trash_list,
)
_register(
    "kwey.history.log",
    "git 快照时间线（需开启快照）；path 缺省为全库，返回 entries。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "限定笔记路径（可选，缺省全库）"},
            "limit": {"type": "integer", "description": "返回条数上限（1-200，默认 50）", "minimum": 1, "maximum": 200},
        },
    },
    "read", "read", None, _history_log,
)
_register(
    "kwey.history.rollback",
    "回滚笔记到指定快照（自动留存回滚前 commit，可再恢复）；必须 confirm=true。",
    {
        "type": "object",
        "properties": {
            "hash": {"type": "string", "description": "目标快照 hash（短 hash 亦可）"},
            "path": {"type": "string", "description": "vault 相对路径"},
            "confirm": {"type": "boolean", "description": "必须显式 true 才执行"},
        },
        "required": ["hash", "path"],
    },
    "write", "reversible", "rollback", _history_rollback,
)
_register(
    "kwey.append",
    "在笔记正文末尾追加文本（原有内容一字不改，独立成行），走 write_note 统一写路径。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对路径"},
            "text": {"type": "string", "description": "要追加的文本"},
        },
        "required": ["path", "text"],
    },
    "write", "reversible", "append", _append,
)
_register(
    "kwey.move",
    "移动笔记到目标目录（basename 不变，全库链接自动重写）；target 空串 = 根目录。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "源笔记 vault 相对路径"},
            "target": {"type": "string", "description": "目标目录（空串 = 根目录）"},
        },
        "required": ["path", "target"],
    },
    "write", "reversible", "move", _move,
)
_register(
    "kwey.rename",
    "重命名笔记（同目录内，自动补 .md，全库链接自动重写）。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "源笔记 vault 相对路径"},
            "name": {"type": "string", "description": "新文件名（缺省自动补 .md）"},
        },
        "required": ["path", "name"],
    },
    "write", "reversible", "rename", _rename,
)
_register(
    "kwey.dir.create",
    "新建目录（含父级；已存在则 409 冲突）。",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "vault 相对目录路径"}},
        "required": ["path"],
    },
    "write", "reversible", "mkdir", _dir_create,
)
_register(
    "kwey.dir.delete",
    "删除目录（整目录移入 .trash/）；非空目录必须 confirm=true，否则 409。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "vault 相对目录路径"},
            "confirm": {"type": "boolean", "description": "非空目录必须显式 true"},
        },
        "required": ["path"],
    },
    "write", "reversible", "rmdir", _dir_delete,
)
_register(
    "kwey.trash.restore",
    "从回收站恢复笔记/目录（目标已存在自动去重不覆盖；目录逐文件恢复重索引）。",
    {
        "type": "object",
        "properties": {
            "trash_path": {"type": "string", "description": ".trash/ 内相对路径"},
            "target_path": {"type": "string", "description": "恢复目标路径（可选，缺省回原路径）"},
        },
        "required": ["trash_path"],
    },
    "write", "reversible", "restore", _trash_restore,
)
_register(
    "kwey.trash.purge",
    "永久删除回收站条目（真实抹除，不可恢复）；trash_path 缺省清空全部；必须 confirm=true。",
    {
        "type": "object",
        "properties": {
            "trash_path": {"type": "string", "description": ".trash/ 内相对路径（可选，缺省清空全部）"},
            "confirm": {"type": "boolean", "description": "必须显式 true 才执行"},
        },
    },
    "admin", "irreversible", "purge", _trash_purge,
)
_register(
    "kwey.organize.apply",
    "应用结构化 ops（create/append/tag/link/move/delete，走 VaultService）；必须 confirm=true。",
    {
        "type": "object",
        "properties": {
            "ops": {"type": "array", "items": {"type": "object"}, "description": "结构化操作数组（见 ops.OP_TYPES）"},
            "confirm": {"type": "boolean", "description": "必须显式 true（先经 kwey.organize 预演）"},
        },
        "required": ["ops"],
    },
    "write", "reversible", "organize", _organize_apply,
)


# ---------------------------------------------------------------- 公共 API
def contract() -> Dict[str, Any]:
    """契约快照（不含 handler）：version + 全量工具元数据，供 /contract 与文档生成。"""
    return {
        "version": CONTRACT_VERSION,
        "tools": [
            {k: v for k, v in tool.items() if k != "handler"}
            for tool in _TOOLS.values()
        ],
    }


def get_tool(name: str) -> Dict[str, Any]:
    """按名取工具元数据（不含 handler）；未知工具抛 ContractError(404)。"""
    tool = _TOOLS.get(name)
    if tool is None:
        raise ContractError(404, f"未知工具: {name}")
    return {k: v for k, v in tool.items() if k != "handler"}


def _journal_paths(name: str, args: Dict[str, Any], data: Any) -> List[str]:
    """一次调用对应的 journal 路径列表；organize.apply 按实际应用路径逐条记录。"""
    if name == "kwey.organize.apply" and isinstance(data, dict):
        return [str(p) for p in (data.get("paths") or [])] or [""]
    path = args.get("path") or args.get("from") or args.get("trash_path") or ""
    return [str(path)]


def invoke(
    name: str,
    arguments: Optional[Dict[str, Any]],
    vault: Optional[VaultService] = None,
    actor: Optional[Dict[str, Any]] = None,
) -> Any:
    """唯一能力入口：schema 校验 → handler → journal（仅写类工具且带 actor）。

    vault=None 取 deps 单例（未打开 vault 抛 400）；进程内测试/内置智能体可注入。
    actor={"role","name"} 由传输层注入（MCP=mcp-stdio/admin，REST=token 记录）；
    actor=None 时不写 journal（内部调用 / 测试保持纯净）。
    ContractError 由传输层映射；HTTPException / 其它异常原样上抛由传输层兜底。
    """
    tool = _TOOLS.get(name)
    if tool is None:
        raise ContractError(404, f"未知工具: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ContractError(400, "arguments 必须是对象")
    _validate(tool["input_schema"], arguments)
    service = vault if vault is not None else deps.require_vault()
    data = tool["handler"](service, arguments)
    action = tool["journal_action"]
    if action and actor is not None:
        root = service.paths.ensure_root()
        for path in _journal_paths(name, arguments, data):
            journal.append(
                root,
                {
                    "role": actor.get("role", ""),
                    "name": actor.get("name", ""),
                    "action": action,
                    "path": path,
                },
            )
    return data