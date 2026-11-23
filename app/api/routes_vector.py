"""语义检索设置与运维：模型下载 / 后端状态 / 重建索引 / 语义试查。

三个刻意的设计：
1. **模型不随包分发**：24MB 首次启用时下载，下载状态在服务端内存里，前端轮询；
2. **重建放后台线程**：500 篇 ONNX 推理约 20 秒，20k 篇要几分钟，绝不能卡 HTTP；
3. **试查接口**：让用户亲眼看到「语义」比关键词多召回了什么，而不是只看一个开关。

所有端点只读用户笔记、只写 ``.kwey/`` 下的索引文件，不改任何 Markdown。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.api import deps
from app.index import models as model_store
from app.index.vector import build_vector_index

logger = logging.getLogger("kwey.vector.api")

router = APIRouter(prefix="/api/vector", tags=["vector"])

_TASK_LOCK = threading.Lock()
_TASK: Dict[str, Any] = {"state": "idle", "done": 0, "total": 0, "message": "", "at": 0.0, "backend": ""}


def _set_task(**fields) -> None:
    with _TASK_LOCK:
        _TASK.update(fields)
        _TASK["at"] = time.time()


def task_state() -> Dict[str, Any]:
    with _TASK_LOCK:
        return dict(_TASK)


def _vector_cfg() -> Dict[str, Any]:
    cfg = deps.require_config()
    return dict(cfg.get("vector") or {})


def _backend():
    indexer = deps.get_indexer()
    return getattr(indexer, "vector", None) if indexer is not None else None


@router.get("/status")
async def vector_status():
    vcfg = _vector_cfg()
    model = str(vcfg.get("model") or model_store.DEFAULT_MODEL)
    backend = _backend()
    return {
        "code": 0,
        "data": {
            "enabled": bool(vcfg.get("enabled", False)),
            "backend": getattr(backend, "name", "null"),
            "available": bool(backend is not None and backend.available),
            "count": int(backend.count()) if backend is not None else 0,
            "embedder": str(vcfg.get("embedder") or "auto"),
            "api_model": str(vcfg.get("api_model") or ""),
            "weights": {
                "vector": vcfg.get("weight", 1.0),
                "graph": vcfg.get("graph_weight", 0.5),
                "rrf_k": vcfg.get("rrf_k", 60),
            },
            "model": model_store.status(model),
            "task": task_state(),
        },
        "message": "ok",
    }


@router.post("/model/download")
async def download_model():
    vcfg = _vector_cfg()
    model = str(vcfg.get("model") or model_store.DEFAULT_MODEL)
    if model.lower() in {"hashing", "hash", "bow"}:
        raise HTTPException(status_code=400, detail="当前配置用哈希词向量，不需要下载模型")
    progress = model_store.start_download(model)
    return {"code": 0, "data": {"model": model, "progress": progress}, "message": "ok"}


class ReindexRequest(BaseModel):
    wait: bool = False          # 测试/CLI 用；前端一律异步


def _reindex_sync(indexer, vcfg: Dict[str, Any], progress=None) -> Dict[str, Any]:
    """重建向量索引（阻塞）。返回 {backend, count, available}。"""
    backend = build_vector_index(Path(indexer.paths.root), vcfg)
    indexer.vector = backend
    if not backend.available:
        return {"backend": backend.name, "count": 0, "available": False}
    backend.reset()
    notes = indexer.db.all_notes()
    total = len(notes)
    if progress:
        progress(0, total)
    done = 0
    chunk: List[tuple] = []
    for note in notes:
        path = note["path"]
        abs_path = Path(indexer.paths.root) / path.replace("/", "\\")
        try:
            body = abs_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        title = str(note.get("title") or path)
        chunk.append((path, f"{title}\n{body}"))
        if len(chunk) >= 64:
            _write_chunk(backend, chunk)
            done += len(chunk)
            chunk = []
            if progress:
                progress(done, total)
    if chunk:
        _write_chunk(backend, chunk)
        done += len(chunk)
    if progress:
        progress(done, total)
    return {"backend": backend.name, "count": int(backend.count()), "available": True}


def _write_chunk(backend, items) -> None:
    upsert_many = getattr(backend, "upsert_many", None)
    if callable(upsert_many):
        upsert_many(items)
        return
    for path, text in items:
        backend.upsert(path, text)


@router.post("/reindex")
async def reindex(req: ReindexRequest):
    vcfg = _vector_cfg()
    if not vcfg.get("enabled", False):
        raise HTTPException(status_code=400, detail="先在设置中启用向量检索，再重建索引")
    indexer = deps.require_indexer()

    if req.wait:
        result = _reindex_sync(indexer, vcfg)
        _set_task(
            state="done", done=result["count"], total=result["count"],
            backend=result["backend"],
            message=f"{result['backend']} · {result['count']} 篇",
        )
        return {"code": 0, "data": {**result, "task": task_state()}, "message": "ok"}

    if task_state().get("state") == "running":
        return {"code": 0, "data": {"task": task_state()}, "message": "已有重建任务在执行"}

    _set_task(state="running", done=0, total=0, message="准备中…", backend="")

    def worker() -> None:
        try:
            def progress(done: int, total: int) -> None:
                _set_task(done=done, total=total, message=f"已处理 {done}/{total}")

            result = _reindex_sync(indexer, vcfg, progress)
            _set_task(
                state="done", done=result["count"], total=result["count"],
                backend=result["backend"],
                message=f"{result['backend']} · {result['count']} 篇"
                if result["available"] else "向量后端不可用，hybrid 降级为纯 FTS",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("向量重建失败")
            _set_task(state="error", message=str(exc))

    threading.Thread(target=worker, name="kwey-vector-reindex", daemon=True).start()
    return {"code": 0, "data": {"task": task_state()}, "message": "ok"}


@router.get("/search")
async def semantic_search(
    q: str = Query(..., min_length=1, max_length=300),
    limit: int = Query(8, ge=1, le=50),
):
    """语义试查：只走向量通道，用来验证"换个说法也能搜到"。"""
    indexer = deps.get_indexer()
    backend = getattr(indexer, "vector", None) if indexer is not None else None
    if backend is None or not backend.available:
        raise HTTPException(status_code=400, detail="向量后端不可用：先在设置中启用并重建索引")
    try:
        pairs = backend.query(q, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"语义检索失败: {exc}") from None

    notes = {note["path"]: note for note in deps.require_db().all_notes()}
    items = []
    for path, score in pairs:
        meta = notes.get(path) or {}
        items.append({
            "path": path,
            "title": meta.get("title") or path,
            "excerpt": meta.get("excerpt") or "",
            "score": round(float(score), 4),
        })
    return {"code": 0, "data": {"query": q, "items": items, "backend": backend.name}, "message": "ok"}
