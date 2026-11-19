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


def query(
    vault_root: Path,
    limit: int = 50,
    actor: str = "",
    role: str = "",
    action: str = "",
    path: str = "",
    since: float = 0.0,
) -> List[Dict[str, Any]]:
    """按 actor(名字)/role/action/path(子串)/since(时间戳) 过滤，倒序返回。

    外部 agent 的归因查询入口：token 的 name 在写入时已落到 name 字段，
    ``actor="verbot"`` 即可拿到它改过的全部条目。文件上限 MAX_ENTRIES 行，
    全量读入内存过滤即可，不需要额外索引。
    """
    try:
        p = _path(vault_root)
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.warning("agent journal 读取失败", exc_info=True)
        return []

    out: List[Dict[str, Any]] = []
    for line in reversed(raw):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if actor and item.get("name") != actor:
            continue
        if role and item.get("role") != role:
            continue
        if action and item.get("action") != action:
            continue
        if path and path not in str(item.get("path") or ""):
            continue
        if since:
            try:
                if float(item.get("ts") or 0) < since:
                    continue
            except (TypeError, ValueError):
                continue
        out.append(item)
        if len(out) >= limit:
            break
    return out