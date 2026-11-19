"""Canvas 画布路由：vault 内 ``.canvas`` JSON 文件的列表 / 读取 / 保存。

``.canvas`` 是 Obsidian 兼容的普通 JSON 文件（不是 Markdown、不进 FTS 与笔记列表）：

.. code-block:: json

    {
      "nodes": [{"id": "n1", "type": "text", "text": "卡片", "x": 0, "y": 0,
                 "width": 240, "height": 120}],
      "edges": [{"id": "e1", "fromNode": "n1", "toNode": "n2"}]
    }

- 路径：vault 相对正斜杠，必须以 ``.canvas`` 结尾；``.kwey/.trash/.git/templates``
  由 ``app.paths.validate_relative`` 拒绝。
- 读取：直接读盘（读操作允许）+ JSON 解析 + 结构校验；非法 JSON/结构一律 400。
- 保存：校验后序列化为 UTF-8 JSON，走统一写路径——
  若 ``VaultService.write_file`` 已由集成卡补上则直接复用；否则本模块在
  ``vault.lock`` 内做等价原子写（临时文件 + ``os.replace`` → 自写标记 →
  非 md 文件登记 → 事件广播），**不直接改写用户笔记**。
- 响应统一 ``{code, data, message}``；错误抛 ``HTTPException(status_code, detail)``。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import deps
from app.events import EventType, VaultEvent
from app.paths import PathError, is_ignored, validate_relative

logger = logging.getLogger("kwey.canvas")

router = APIRouter(prefix="/api/canvas", tags=["canvas"])

CANVAS_EXT = ".canvas"
NODE_TYPES = {"text", "file", "group", "link"}
MAX_NODES = 2000
MAX_EDGES = 5000
MAX_NODE_TEXT = 20000
MAX_NODE_ID = 64
MAX_COORD = 1_000_000.0
MAX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_WIDTH = 240
DEFAULT_HEIGHT = 120

_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:|[\\/])")


class CanvasSaveRequest(BaseModel):
    """保存请求：``data`` 为完整的 Obsidian canvas 结构（缺省空画布）。"""

    path: str
    data: Dict[str, Any] = Field(default_factory=dict)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


# ---------------------------------------------------------------------- 路径
def _canvas_path(raw: Any) -> str:
    """校验画布路径：vault 相对、非保留目录、以 ``.canvas`` 结尾。"""
    if not isinstance(raw, str) or not raw.strip():
        raise _bad("path 必须是非空字符串")
    text = raw.strip()
    if _ABSOLUTE_RE.match(text):
        raise _bad("path 必须是 vault 相对路径（不能是绝对路径）")
    try:
        rel = validate_relative(text)
    except PathError as exc:
        raise _bad(f"画布路径非法: {exc}") from exc
    name = rel.rsplit("/", 1)[-1]
    if not name.lower().endswith(CANVAS_EXT) or len(name) <= len(CANVAS_EXT):
        raise _bad("画布文件必须以 .canvas 结尾，且文件名不能为空")
    return rel


# ---------------------------------------------------------------------- 校验
def _coord(value: Any, label: str) -> float | int:
    """坐标/尺寸：有限数字、绝对值 ≤ 1e6；整数保持 int（Obsidian 输出更干净）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{label} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_COORD:
        raise _bad(f"{label} 超出允许范围（±{MAX_COORD:g}）")
    number = round(number, 1)
    return int(number) if number.is_integer() else number


def _validate_nodes(raw_nodes: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_nodes, list):
        raise _bad("data.nodes 必须是数组")
    if len(raw_nodes) > MAX_NODES:
        raise _bad(f"节点过多（上限 {MAX_NODES}）")

    nodes: List[Dict[str, Any]] = []
    ids: set[str] = set()
    for index, node in enumerate(raw_nodes, 1):
        where = f"第 {index} 个节点"
        if not isinstance(node, dict):
            raise _bad(f"{where} 必须是对象")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise _bad(f"{where} 缺少 id")
        node_id = node_id.strip()
        if len(node_id) > MAX_NODE_ID:
            raise _bad(f"{where} 的 id 过长（上限 {MAX_NODE_ID} 字符）")
        if node_id in ids:
            raise _bad(f"节点 id 重复: {node_id}")
        ids.add(node_id)

        node_type = node.get("type") or ("file" if node.get("file") else "text")
        if node_type not in NODE_TYPES:
            raise _bad(f"{where} 的 type 非法: {node_type!r}（允许 {sorted(NODE_TYPES)}）")

        clean = dict(node)
        clean["id"] = node_id
        clean["type"] = node_type
        for key, default in (("x", 0), ("y", 0), ("width", DEFAULT_WIDTH), ("height", DEFAULT_HEIGHT)):
            clean[key] = _coord(node.get(key, default), f"{where}.{key}")
        if clean["width"] <= 0 or clean["height"] <= 0:
            raise _bad(f"{where} 的宽高必须为正数")

        if node_type == "text":
            text = node.get("text", "")
            if not isinstance(text, str):
                raise _bad(f"{where}.text 必须是字符串")
            if len(text) > MAX_NODE_TEXT:
                raise _bad(f"{where}.text 过长（上限 {MAX_NODE_TEXT} 字）")
            clean["text"] = text
        elif node_type == "file":
            file_rel = node.get("file")
            if not isinstance(file_rel, str) or not file_rel.strip():
                raise _bad(f"{where}（file 节点）缺少 file")
            try:
                clean["file"] = validate_relative(file_rel.strip())
            except PathError as exc:
                raise _bad(f"{where}.file 非法: {exc}") from exc
        elif node_type == "link":
            url = node.get("url")
            if not isinstance(url, str) or not url.strip():
                raise _bad(f"{where}（link 节点）缺少 url")
            clean["url"] = url.strip()
        else:  # group
            label = node.get("label", "")
            if not isinstance(label, str):
                raise _bad(f"{where}.label 必须是字符串")
            clean["label"] = label
        nodes.append(clean)
    return nodes


def _validate_edges(raw_edges: Any, node_ids: set[str]) -> List[Dict[str, Any]]:
    if not isinstance(raw_edges, list):
        raise _bad("data.edges 必须是数组")
    if len(raw_edges) > MAX_EDGES:
        raise _bad(f"连线过多（上限 {MAX_EDGES}）")

    edges: List[Dict[str, Any]] = []
    edge_ids: set[str] = set()
    pairs: set[Tuple[str, str]] = set()
    for index, edge in enumerate(raw_edges, 1):
        where = f"第 {index} 条连线"
        if not isinstance(edge, dict):
            raise _bad(f"{where} 必须是对象")
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id.strip():
            raise _bad(f"{where} 缺少 id")
        edge_id = edge_id.strip()
        if len(edge_id) > MAX_NODE_ID:
            raise _bad(f"{where} 的 id 过长（上限 {MAX_NODE_ID} 字符）")
        if edge_id in edge_ids:
            raise _bad(f"连线 id 重复: {edge_id}")
        edge_ids.add(edge_id)

        endpoints: Dict[str, str] = {}
        for key in ("fromNode", "toNode"):
            value = edge.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _bad(f"{where} 缺少 {key}")
            value = value.strip()
            if value not in node_ids:
                raise _bad(f"{where} 的 {key} 指向不存在的节点: {value}")
            endpoints[key] = value
        if endpoints["fromNode"] == endpoints["toNode"]:
            raise _bad(f"{where} 不能连接自身")
        pair = (endpoints["fromNode"], endpoints["toNode"])
        if pair in pairs:
            raise _bad(f"{where} 与已有连线重复（同一对节点）")
        pairs.add(pair)

        clean = dict(edge)
        clean["id"] = edge_id
        clean.update(endpoints)
        edges.append(clean)
    return edges


def _validate_data(data: Any) -> Dict[str, Any]:
    """校验并规范化 canvas 结构；保留未知字段（Obsidian 的 color / fromSide 等）。"""
    if not isinstance(data, dict):
        raise _bad("data 必须是对象")
    raw_nodes = data.get("nodes", [])
    nodes = _validate_nodes(raw_nodes)
    node_ids = {node["id"] for node in nodes}
    edges = _validate_edges(data.get("edges", []), node_ids)
    result = dict(data)
    result["nodes"] = nodes
    result["edges"] = edges
    return result


# ---------------------------------------------------------------------- 读取
def _abs_path(vault, rel: str) -> Path:
    return vault.paths.to_abs(rel)


def _read_canvas(vault, rel: str) -> Tuple[str, float, int]:
    abs_path = _abs_path(vault, rel)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"画布不存在: {rel}")
    stat = abs_path.stat()
    if stat.st_size > MAX_FILE_BYTES:
        raise _bad(f"画布文件过大（上限 {MAX_FILE_BYTES // (1024 * 1024)} MB）")
    try:
        text = abs_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _bad("画布文件不是 UTF-8 文本") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"画布读取失败: {exc}") from exc
    return text, stat.st_mtime, stat.st_size


def _parse_json(text: str, rel: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _bad(f"画布 JSON 解析失败（{rel} 第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}") from exc


# ---------------------------------------------------------------------- 写入
def _write_canvas(vault, rel: str, text: str) -> Dict[str, Any]:
    """写画布：优先新的 ``VaultService.write_file``，否则等价原子写兜底。

    兜底路径与 VaultService 写路径一致：vault 锁内 → 临时文件 + ``os.replace`` →
    自写标记（防 watcher 回环）→ 非 md 文件登记（不进 FTS/笔记树）→ 事件广播。
    """
    writer = getattr(vault, "write_file", None)
    created = False
    if callable(writer):
        result = writer(rel, text) or {}
        created = bool(result.get("created", False))
    else:
        with vault.lock:
            abs_path = _abs_path(vault, rel)  # 锁内再次校验路径
            created = not abs_path.is_file()
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = abs_path.with_name(abs_path.name + ".kwey-tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, abs_path)
            mark = getattr(vault, "_mark_self_write", None)
            if callable(mark):
                mark(rel)
            # 非 md：indexer 只把文件名登记进链接图，不会进笔记/FTS
            try:
                vault.indexer.reindex_note(rel)
            except Exception:  # noqa: BLE001 —— 索引是增强能力，失败不影响文件已落盘
                logger.warning("画布登记索引失败（文件已写入）: %s", rel, exc_info=True)
            vault.eventbus.publish(
                VaultEvent(
                    EventType.CREATED if created else EventType.UPDATED, path=rel
                )
            )
            snapshot = getattr(vault, "_auto_snapshot", None)
            if callable(snapshot):
                try:
                    snapshot("canvas-create" if created else "canvas-update", rel)
                except Exception:  # noqa: BLE001 —— 快照是增强能力，失败不影响保存
                    pass

    abs_path = _abs_path(vault, rel)
    stat = abs_path.stat()
    return {
        "path": rel,
        "created": created,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


# ---------------------------------------------------------------------- 路由
@router.get("/list")
async def list_canvases() -> Dict[str, Any]:
    """列出 vault 内全部 ``.canvas`` 文件（按 mtime 倒序），跳过保留目录。"""
    vault = deps.require_vault()
    root = vault.paths.ensure_root()
    items: List[Dict[str, Any]] = []
    try:
        candidates = sorted(root.rglob(f"*{CANVAS_EXT}"))
    except OSError:
        candidates = []
    for abs_path in candidates:
        if not abs_path.is_file():
            continue
        if abs_path.name.lower() == CANVAS_EXT:  # 只有扩展名的非法文件，不列出
            continue
        try:
            rel = vault.paths.to_rel(abs_path)
        except Exception:  # noqa: BLE001 —— 越界路径直接忽略
            continue
        if is_ignored(rel):
            continue
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        items.append(
            {
                "path": rel,
                "name": abs_path.stem,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    items.sort(key=lambda item: (-item["mtime"], item["path"]))
    return {"code": 0, "data": {"canvases": items, "count": len(items)}, "message": "ok"}


@router.get("/load")
async def load_canvas(path: str) -> Dict[str, Any]:
    """读取画布：返回校验后的 JSON 结构与 mtime（文件不存在 404，非法 JSON 400）。"""
    vault = deps.require_vault()
    rel = _canvas_path(path)
    text, mtime, size = _read_canvas(vault, rel)
    data = _validate_data(_parse_json(text, rel))
    return {
        "code": 0,
        "data": {"path": rel, "data": data, "mtime": mtime, "size": size},
        "message": "ok",
    }


@router.post("/save")
async def save_canvas(req: CanvasSaveRequest) -> Dict[str, Any]:
    """保存画布（创建或覆盖）：校验 → JSON 序列化 → 统一写路径落盘。"""
    vault = deps.require_vault()
    rel = _canvas_path(req.path)
    data = _validate_data(req.data)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise _bad(f"画布数据过大（上限 {MAX_FILE_BYTES // (1024 * 1024)} MB）")
    try:
        result = _write_canvas(vault, rel, text)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"画布保存失败: {exc}") from exc
    result.update(
        {"nodes": len(data["nodes"]), "edges": len(data["edges"])}
    )
    return {"code": 0, "data": result, "message": "ok"}
