"""ONNX embedding：纯 Python BERT WordPiece 分词 + onnxruntime 推理。

不依赖 sentence-transformers / transformers / torch：
- 分词器是 BERT 论文描述的标准流程（clean → CJK 切分 → 标点切分 → 小写去重音 → WordPiece 贪心最长匹配）；
- 推理只喂 input_ids / attention_mask（模型没有 token_type_ids 就不喂）；
- 池化默认 CLS（bge 系列官方用法），可切 mean；输出 L2 归一化，余弦相似度=点积。

线程安全：session 懒加载 + 锁；批量推理按 16 条一组，避免一次性吃掉几百 MB。
"""

from __future__ import annotations

import logging
import os
import threading
import unicodedata
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from app.index import models

logger = logging.getLogger("kwey.onnx")

MAX_TOKENS = 512
BATCH = 16
_MAX_CHARS_PER_WORD = 100


def _is_control(char: str) -> bool:
    if char in "\t\n\r":
        return False
    return unicodedata.category(char).startswith("C")


def _is_punctuation(char: str) -> bool:
    code = ord(char)
    if 33 <= code <= 47 or 58 <= code <= 64 or 91 <= code <= 96 or 123 <= code <= 126:
        return True
    return unicodedata.category(char).startswith("P")


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
        or 0x2F800 <= code <= 0x2FA1F
    )


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


class BertTokenizer:
    """BERT WordPiece 分词器（读 vocab.txt）。"""

    def __init__(self, vocab_path: Path, do_lower_case: bool = True) -> None:
        self.vocab: Dict[str, int] = {}
        with open(vocab_path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                self.vocab[line.rstrip("\n")] = index
        self.do_lower_case = do_lower_case
        self.cls_id = self.vocab.get("[CLS]", 101)
        self.sep_id = self.vocab.get("[SEP]", 102)
        self.pad_id = self.vocab.get("[PAD]", 0)
        self.unk_id = self.vocab.get("[UNK]", 100)

    # ---------------------------------------------------------------- basic
    def _basic_tokenize(self, text: str) -> List[str]:
        cleaned = []
        for char in text:
            code = ord(char)
            if code == 0 or code == 0xFFFD or _is_control(char):
                continue
            cleaned.append(" " if char.isspace() else char)
        text = "".join(cleaned).strip()

        # CJK 逐字加空格（BERT 对中文按字切）
        spaced = []
        for char in text:
            if _is_cjk(char):
                spaced.append(" ")
                spaced.append(char)
                spaced.append(" ")
            else:
                spaced.append(char)
        text = "".join(spaced)

        output: List[str] = []
        for token in text.split():
            if self.do_lower_case:
                token = token.lower()
                token = _strip_accents(token)
            pieces: List[str] = []
            current = ""
            for char in token:
                if _is_punctuation(char):
                    if current:
                        pieces.append(current)
                        current = ""
                    pieces.append(char)
                else:
                    current += char
            if current:
                pieces.append(current)
            output.extend(pieces)
        return output

    # ---------------------------------------------------------------- wordpiece
    def _wordpiece(self, token: str) -> List[str]:
        chars = list(token)
        if len(chars) > _MAX_CHARS_PER_WORD:
            return ["[UNK]"]
        out: List[str] = []
        start = 0
        while start < len(chars):
            end = len(chars)
            found = None
            while start < end:
                piece = "".join(chars[start:end])
                if start > 0:
                    piece = "##" + piece
                if piece in self.vocab:
                    found = piece
                    break
                end -= 1
            if found is None:
                return ["[UNK]"]
            out.append(found)
            start = end
        return out

    # ---------------------------------------------------------------- encode
    def encode(self, text: str, max_len: int = MAX_TOKENS) -> Tuple[List[int], List[int], List[int]]:
        ids: List[int] = []
        for token in self._basic_tokenize(text or ""):
            for piece in self._wordpiece(token):
                ids.append(self.vocab.get(piece, self.unk_id))
                if len(ids) >= max_len - 2:
                    break
            if len(ids) >= max_len - 2:
                break
        input_ids = [self.cls_id] + ids + [self.sep_id]
        mask = [1] * len(input_ids)
        types = [0] * len(input_ids)
        pad = max_len - len(input_ids)
        if pad > 0:
            input_ids += [self.pad_id] * pad
            mask += [0] * pad
            types += [0] * pad
        return input_ids, mask, types


class OnnxEmbedder:
    """bge-small-zh-v1.5 等 BERT 结构的 ONNX 句向量模型。"""

    name = "onnx"

    def __init__(self, model: str = models.DEFAULT_MODEL, model_path: Path | None = None) -> None:
        meta = models.spec(model)
        directory = models.model_dir(model)
        self.model = model
        self.dim = int(meta.get("dim") or 512)
        self.pooling = str(meta.get("pooling") or "cls")
        self.query_prefix = str(meta.get("query_prefix") or "")
        self.model_path = Path(model_path) if model_path else directory / "model.onnx"
        self.vocab_path = directory / "vocab.txt"
        if not self.model_path.is_file():
            raise FileNotFoundError(f"模型文件缺失: {self.model_path}")
        if not self.vocab_path.is_file():
            raise FileNotFoundError(f"词表缺失: {self.vocab_path}")
        self.tokenizer = BertTokenizer(self.vocab_path)
        self._session = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- session
    def _get_session(self):
        if self._session is not None:
            return self._session
        with self._lock:
            if self._session is not None:
                return self._session
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2)))
            options.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            return self._session

    def warmup(self) -> None:
        self.embed(["预热"])

    # ---------------------------------------------------------------- infer
    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        import numpy as np

        items = [(text or "")[:2000] for text in texts]
        if not items:
            return []
        session = self._get_session()
        input_names = {item.name for item in session.get_inputs()}
        vectors: List[List[float]] = []
        for start in range(0, len(items), BATCH):
            chunk = items[start:start + BATCH]
            batch_ids, batch_mask, batch_types = [], [], []
            for text in chunk:
                ids, mask, types = self.tokenizer.encode(text)
                batch_ids.append(ids)
                batch_mask.append(mask)
                batch_types.append(types)
            feed = {
                "input_ids": np.asarray(batch_ids, dtype=np.int64),
                "attention_mask": np.asarray(batch_mask, dtype=np.int64),
            }
            if "token_type_ids" in input_names:
                feed["token_type_ids"] = np.asarray(batch_types, dtype=np.int64)
            outputs = session.run(None, feed)
            hidden = outputs[0]                      # [B, seq, dim]
            mask = feed["attention_mask"].astype("float32")[:, :, None]
            if self.pooling == "mean":
                pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-6, None)
            else:
                pooled = hidden[:, 0, :]             # CLS
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            normalized = pooled / np.clip(norm, 1e-12, None)
            vectors.extend(normalized.astype("float32").tolist())
        return vectors

    def embed_query(self, text: str) -> List[float]:
        """查询侧加检索指令前缀（bge 官方用法，召回明显更好）。"""
        return self.embed([f"{self.query_prefix}{text}"])[0]


def build_onnx_embedder(model: str = models.DEFAULT_MODEL) -> OnnxEmbedder | None:
    """可用则返回实例；缺文件/缺 onnxruntime 返回 None（调用方降级）。"""
    if not models.is_ready(model):
        return None
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        logger.warning("未安装 onnxruntime，跳过本地语义模型")
        return None
    try:
        return OnnxEmbedder(model)
    except Exception:  # noqa: BLE001 —— 模型损坏等，降级而不是炸掉
        logger.exception("ONNX embedding 初始化失败")
        return None
