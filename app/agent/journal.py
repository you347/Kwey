"""Agent 写入日志：.kwey/agent-journal.jsonl。
脉冲首页与未来「评审台」的数据源；写失败只 log，绝不阻断 Agent 写路径。"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
MAX_ENTRIES = 500
FILENAME = "agent-journal.jsonl"


def _path(vault_root: Path) -> Path:
    return vault_root / ".kwey" / FILENAME


def append(vault_root: Path, record: Dict[str, Any]) -> None:
    entry = {"ts": time.time(), **record}
    try:
        with _LOCK:
            p = _path(vault_root)
            p.parent.mkdir(parents=True, exist_ok=True)
            lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
            lines.append(json.dumps(entry, ensure_ascii=False))
            if len(lines) > MAX_ENTRIES:
                lines = lines[-MAX_ENTRIES:]
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(p)
    except Exception:
        logger.warning("agent journal 写入失败", exc_info=True)


def recent(vault_root: Path, limit: int = 5) -> List[Dict[str, Any]]:
    try:
        p = _path(vault_root)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        logger.warning("agent journal 读取失败", exc_info=True)
        return []