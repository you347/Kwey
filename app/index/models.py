"""本地 embedding 模型管理：按需下载 + 完整性校验 + 进度状态。

为什么不让用户 `pip install sentence-transformers`：
桌面产品不能要求用户装 Python 环境。这里只做两件事——把 4 个文件下到用户目录、
交给 onnxruntime 推理。模型不随安装包分发（+24MB 会拖慢首装），首次启用时下载。

目录优先级（第一个命中即用）：
1. ``$KWEY_MODELS_DIR``（测试/自定义）
2. 冻结包内的 ``_internal/models``（如果发布时选择内置）
3. 源码项目的 ``models/``（开发者已下载）
4. 用户数据目录 ``%LOCALAPPDATA%/Kwey/models``（运行时下载落点）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional

from app.runtime import data_dir, is_frozen, resource_root

logger = logging.getLogger("kwey.models")

DEFAULT_MODEL = "bge-small-zh-v1.5"

MODELS: Dict[str, Dict] = {
    "bge-small-zh-v1.5": {
        "dim": 512,
        "size_mb": 24,
        "pooling": "cls",
        # bge 中文模型的检索指令前缀：只加在 query 上，文档不加（官方用法）
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "files": {
            "model.onnx": "onnx/model_quantized.onnx",
            "vocab.txt": "vocab.txt",
            "tokenizer.json": "tokenizer.json",
            "config.json": "config.json",
            "special_tokens_map.json": "special_tokens_map.json",
        },
        "repo": "Xenova/bge-small-zh-v1.5",
    },
}

MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kwey/0.3"


# --------------------------------------------------------------------- paths
def models_root() -> Path:
    env = os.environ.get("KWEY_MODELS_DIR")
    if env:
        path = Path(env)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if is_frozen():
        bundled = resource_root() / "models"
        if (bundled / DEFAULT_MODEL / "model.onnx").is_file():
            return bundled
        path = data_dir() / "models"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return resource_root() / "models"


def model_dir(model: str = DEFAULT_MODEL) -> Path:
    return models_root() / model


def spec(model: str = DEFAULT_MODEL) -> Dict:
    return MODELS.get(model) or MODELS[DEFAULT_MODEL]


def is_ready(model: str = DEFAULT_MODEL) -> bool:
    """四个必需文件齐了才算可用（缺 tokenizer 会退化成哈希，别让用户误以为语义检索生效）。"""
    directory = model_dir(model)
    required = ("model.onnx", "vocab.txt")
    return all((directory / name).is_file() for name in required)


def size_bytes(model: str = DEFAULT_MODEL) -> int:
    directory = model_dir(model)
    if not directory.is_dir():
        return 0
    return sum(p.stat().st_size for p in directory.glob("*") if p.is_file())


# --------------------------------------------------------------------- progress
_PROGRESS_LOCK = threading.Lock()
_PROGRESS: Dict[str, Dict] = {}


def _set_progress(**fields) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(fields)
        _PROGRESS["at"] = time.time()


def progress() -> Dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


# --------------------------------------------------------------------- download
def _download_file(url: str, target: Path, on_chunk: Callable[[int, int], None]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        tmp = target.with_suffix(target.suffix + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                on_chunk(done, total)
        tmp.replace(target)


def download(
    model: str = DEFAULT_MODEL,
    *,
    on_progress: Optional[Callable[[Dict], None]] = None,
    timeout_per_file: int = 600,
) -> Dict:
    """同步下载（线程里调用）。返回最新状态；失败抛 RuntimeError。"""
    meta = spec(model)
    repo = meta["repo"]
    files = meta["files"]
    directory = model_dir(model)
    directory.mkdir(parents=True, exist_ok=True)

    _set_progress(state="downloading", model=model, file="", downloaded=0, total=0, error=None)
    try:
        for name, remote in files.items():
            target = directory / name
            if target.is_file() and target.stat().st_size > 0:
                continue
            last_error: Optional[Exception] = None
            for base in MIRRORS:
                url = f"{base}/{repo}/resolve/main/{remote}"
                try:
                    def chunk(done: int, total: int, _name=name) -> None:
                        _set_progress(file=_name, downloaded=done, total=total)
                        if on_progress:
                            on_progress(progress())

                    _download_file(url, target, chunk)
                    last_error = None
                    break
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                    last_error = exc
                    logger.warning("模型文件下载失败（换源重试）%s: %s", url, exc)
            if last_error is not None:
                raise RuntimeError(f"下载 {name} 失败：{last_error}") from last_error
        if not is_ready(model):
            raise RuntimeError("模型文件不完整（model.onnx / vocab.txt 缺失）")
        _set_progress(state="ready", file="", downloaded=size_bytes(model), total=size_bytes(model))
        return progress()
    except Exception as exc:  # noqa: BLE001
        _set_progress(state="error", error=str(exc))
        raise


def start_download(model: str = DEFAULT_MODEL) -> Dict:
    """后台下载（HTTP 请求不能阻塞等 40 秒）。重复调用复用同一任务。"""
    current = progress()
    if current.get("state") == "downloading":
        return current
    if is_ready(model):
        _set_progress(state="ready", error=None)
        return progress()

    def worker() -> None:
        try:
            download(model)
        except Exception:  # noqa: BLE001 —— 已写入 progress.error
            logger.exception("模型下载失败")

    threading.Thread(target=worker, name="kwey-model-download", daemon=True).start()
    return progress()


def status(model: str = DEFAULT_MODEL) -> Dict:
    meta = spec(model)
    return {
        "model": model,
        "dim": meta.get("dim"),
        "pooling": meta.get("pooling"),
        "ready": is_ready(model),
        "dir": str(model_dir(model)),
        "size_mb": round(size_bytes(model) / 1024 / 1024, 1),
        "expected_mb": meta.get("size_mb"),
        "progress": progress(),
    }


def read_config(model: str = DEFAULT_MODEL) -> Dict:
    path = model_dir(model) / "config.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
