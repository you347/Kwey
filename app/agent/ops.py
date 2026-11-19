"""结构化 ops：校验 / diff 预演（纯计算，不写盘不碰索引）/ 确认应用（走 VaultService）。

op 结构（``list[dict]``），``type`` 取值见 :data:`OP_TYPES`::

    create : {type, path, content?}          → 新建笔记
    append : {type, path, text}              → 正文末尾追加文本
    tag    : {type, path, tag}               → frontmatter.tags 追加标签
    link   : {type, path, target, alias?}    → 正文末尾追加 [[target|alias]]
    move   : {type, path, target}            → 移动到目标目录（basename 不变）
    delete : {type, path}                    → 删除进 .trash/

约定：
    - 路径一律 vault 相对正斜杠，全部经 ``app.paths.validate_relative`` 校验。
    - diff 的 ``before`` / ``after`` 为「完整文件文本」：before 是磁盘现有原文
      （不存在为 None），after 是逻辑上应用后的文本（不含 VaultService 自动补的
      created / updated 时间戳，便于前端直接做文本 diff）。
    - ``preview_ops`` 纯计算，绝不写盘、不改索引；``apply_ops`` 一律经 VaultService。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.parser import frontmatter as fm_mod
from app.paths import PathError, validate_relative
from app.vault.service import VaultService

OP_TYPES = {"create", "append", "tag", "link", "move", "delete"}

# 各类型除 path 外的必填字段
_REQUIRED: Dict[str, Tuple[str, ...]] = {
    "create": (),
    "append": ("text",),
    "tag": ("tag",),
    "link": ("target",),
    "move": ("target",),
    "delete": (),
}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _check_path(value: Any, label: str) -> str:
    """校验单个路径字段；PathError 统一转 400 HTTPException。"""
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"字段 {label} 必须是非空路径字符串")
    try:
        return validate_relative(value)
    except PathError as exc:
        raise _bad(f"字段 {label} 路径非法: {exc}") from exc


# ---------------------------------------------------------------------- validate
def validate_ops(ops: Any) -> List[Dict[str, Any]]:
    """校验 ops 结构与路径，返回规范化副本（不修改入参）。

    - 未知类型 / 缺必填字段 / 非法路径 → ``HTTPException(400, detail)``。
    - ``link.target`` 是 wikilink 文本（不是 vault 路径），不做路径校验。
    - ``move.target`` 是目标目录，需路径校验。
    """
    if not isinstance(ops, list):
        raise _bad("ops 必须是数组")
    result: List[Dict[str, Any]] = []
    for i, op in enumerate(ops):
        where = f"第 {i + 1} 项 op"
        if not isinstance(op, dict):
            raise _bad(f"{where} 必须是对象")
        # 兼容 LLM 偶发用 ``op`` 作类型键
        op_type = op.get("type") or op.get("op")
        if op_type not in OP_TYPES:
            raise _bad(
                f"{where} 的 type 未知或缺失: {op_type!r}，允许 {sorted(OP_TYPES)}"
            )
        norm: Dict[str, Any] = dict(op)
        norm["type"] = op_type
        norm["path"] = _check_path(op.get("path"), f"{where}.path")
        for field in _REQUIRED[op_type]:
            if field not in op or op[field] is None:
                raise _bad(f"{where} 缺少必填字段 {field!r}")
        if op_type == "move":
            norm["target"] = _check_path(op.get("target"), f"{where}.target")
        result.append(norm)
    return result


# ---------------------------------------------------------------------- helpers
def _read_raw(vault: VaultService, rel: str) -> Optional[str]:
    """只读磁盘现有完整文本；不存在返回 None。绝不写盘。"""
    try:
        abs_path = vault.paths.to_abs(rel)
    except PathError:
        return None
    if not abs_path.is_file():
        return None
    try:
        return abs_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _append_text(base: str, addition: str) -> str:
    """在文本末尾追加，原有内容一字不改；保证追加内容独立成行。"""
    if not addition:
        return base
    sep = "" if (base == "" or base.endswith("\n")) else "\n"
    suffix = "" if addition.endswith("\n") else "\n"
    return base + sep + addition + suffix


def _add_tag(fm: Dict[str, Any], tag: str) -> Dict[str, Any]:
    """返回在 frontmatter.tags 追加标签后的新 dict（去重，保留其它字段）。"""
    result = dict(fm)
    current = result.get("tags")
    if current is None:
        tag_list: List[Any] = []
    elif isinstance(current, list):
        tag_list = list(current)
    else:
        tag_list = [current]
    clean = str(tag).strip().lstrip("#")
    if clean and clean not in tag_list:
        tag_list.append(clean)
    result["tags"] = tag_list
    return result


def _wikilink(op: Dict[str, Any]) -> str:
    target = str(op.get("target") or "").strip()
    alias = op.get("alias")
    return f"[[{target}|{alias}]]" if alias else f"[[{target}]]"


def _create_content(op: Dict[str, Any], rel: str) -> str:
    """create 的正文：用户给了 content 用之，否则与 create_note 一致补 ``# 标题``。"""
    content = str(op.get("content") or "")
    if not content:
        title = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        content = f"# {title}\n\n"
    return content


# ---------------------------------------------------------------------- preview
def preview_ops(ops: Any, vault: VaultService) -> Dict[str, Any]:
    """计算 diff（纯计算，绝对不写盘、不改索引）。

    返回 ``{"valid": True, "diffs": [...], "count": N}``。
    每个 diff 基于「当前磁盘状态」独立计算（不累积前面 op 的效果）。
    """
    validated = validate_ops(ops)
    diffs = [_preview_one(op, vault) for op in validated]
    return {"valid": True, "diffs": diffs, "count": len(diffs)}


def _preview_one(op: Dict[str, Any], vault: VaultService) -> Dict[str, Any]:
    op_type = op["type"]
    rel = op["path"]
    raw = _read_raw(vault, rel)

    diff: Dict[str, Any] = {
        "op": op_type,
        "path": rel,
        "action": "",
        "before": raw,
        "after": None,
    }

    if op_type == "create":
        diff["action"] = "create"
        diff["after"] = _create_content(op, rel)
        return diff

    if op_type == "move":
        # target 为目标目录，basename 不变（与 VaultService.move_note 一致）
        basename = rel.rsplit("/", 1)[-1]
        new_dir = op["target"]
        diff["action"] = "move"
        diff["target"] = f"{new_dir}/{basename}" if new_dir else basename
        diff["after"] = None  # 内容不变
        return diff

    if op_type == "delete":
        diff["action"] = "delete"
        diff["target"] = f".trash/{rel}"  # 示意；实际去重由 VaultService 决定
        diff["after"] = None
        return diff

    # append / tag / link 要求目标笔记已存在
    if raw is None:
        raise _bad(f"笔记不存在，无法 {op_type}: {rel}")

    diff["action"] = "modify"
    fm, body = fm_mod.parse_frontmatter(raw)
    if op_type == "append":
        diff["after"] = _append_text(raw, str(op.get("text") or ""))
    elif op_type == "link":
        diff["after"] = _append_text(raw, _wikilink(op))
    elif op_type == "tag":
        diff["after"] = fm_mod.dump_frontmatter(
            _add_tag(fm, str(op.get("tag") or "")), body
        )
    return diff


# ---------------------------------------------------------------------- apply
def apply_ops(ops: Any, vault: VaultService) -> Dict[str, Any]:
    """校验后逐项应用（一律走 VaultService）。

    返回 ``{"applied": N, "paths": [...], "rewritten_links": N}``。
    单项失败抛 ``HTTPException`` 并说明第几项；已应用的前置项不回滚（见报告取舍）。
    """
    validated = validate_ops(ops)
    applied_paths: List[str] = []
    rewritten_total = 0
    for i, op in enumerate(validated):
        where = f"第 {i + 1} 项 op"
        op_type, rel = op["type"], op["path"]
        try:
            final_path, rewritten = _apply_one(op, vault)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"{where}（{op_type} {rel}）应用失败: {exc.detail}",
            ) from exc
        applied_paths.append(final_path)
        rewritten_total += rewritten
    return {
        "applied": len(applied_paths),
        "paths": applied_paths,
        "rewritten_links": rewritten_total,
    }


def _apply_one(op: Dict[str, Any], vault: VaultService) -> Tuple[str, int]:
    """应用单项 → (结果路径, 重写链接数)。"""
    op_type, rel = op["type"], op["path"]

    if op_type == "create":
        # 传空串让 create_note 自行补默认标题 / frontmatter
        meta = vault.create_note(rel, content=str(op.get("content") or ""))
        return str(meta.get("path") or rel), 0

    if op_type == "move":
        result = vault.move_note(rel, op["target"])
        return str(result.get("new_path") or rel), int(result.get("rewritten_links") or 0)

    if op_type == "delete":
        return str(vault.delete_note(rel)), 0

    # append / tag / link：基于现有 (frontmatter, 正文) 计算后写回
    raw = _read_raw(vault, rel)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"笔记不存在: {rel}")
    fm, body = fm_mod.parse_frontmatter(raw)
    if op_type == "append":
        content = _append_text(body, str(op.get("text") or ""))
    elif op_type == "link":
        content = _append_text(body, _wikilink(op))
    elif op_type == "tag":
        fm = _add_tag(fm, str(op.get("tag") or ""))
        content = body
    vault.write_note(rel, content=content, frontmatter=fm)
    return rel, 0
