"""知识体检：把「库开始烂掉」的信号一次性找出来。

六项检查（都只读索引 + 必要时的正文，不改任何文件）：
1. 孤立笔记 —— 没有任何入链/出链（写完就忘的坟场）
2. 断链 —— ``[[不存在的笔记]]``（结构在腐烂）
3. 重复笔记 —— 正文近似（复制粘贴 / 多次导入）
4. 空笔记 —— 只有标题或 frontmatter
5. 长期未更新 —— 超过 N 天没动
6. 无标签 —— 没有任何标签，检索时等于隐身

评分不是学术指标，只用来回答一个问题：**这个库还能不能靠搜索和链接找到东西**。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("kwey.diagnose")

DEFAULTS = {
    "stale_days": 90,
    "duplicate_ratio": 0.92,
    "exclude": [],            # 目录前缀，逗号分隔字符串也接受
    "max_items": 50,          # 每类最多返回多少条样例
    "max_scan_files": 3000,   # 重复检测最多读多少篇正文
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

SEVERITY = {
    "orphans": "warn",
    "broken": "error",
    "duplicates": "warn",
    "empty": "info",
    "stale": "info",
    "untagged": "info",
}
LABELS = {
    "orphans": "孤立笔记",
    "broken": "断链",
    "duplicates": "重复笔记",
    "empty": "空笔记",
    "stale": "长期未更新",
    "untagged": "无标签",
}
WEIGHTS = {
    "orphans": 0.20,
    "broken": 0.20,
    "duplicates": 0.15,
    "empty": 0.15,
    "stale": 0.10,
    "untagged": 0.20,
}


def _normalize_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if value is not None:
            cfg[key] = value
    exclude = cfg.get("exclude") or []
    if isinstance(exclude, str):
        exclude = [part.strip() for part in exclude.replace("，", ",").split(",") if part.strip()]
    cfg["exclude"] = [str(p).strip("/").replace("\\", "/") for p in exclude]
    try:
        cfg["stale_days"] = max(1, int(cfg["stale_days"]))
    except (TypeError, ValueError):
        cfg["stale_days"] = DEFAULTS["stale_days"]
    try:
        cfg["duplicate_ratio"] = min(1.0, max(0.5, float(cfg["duplicate_ratio"])))
    except (TypeError, ValueError):
        cfg["duplicate_ratio"] = DEFAULTS["duplicate_ratio"]
    return cfg


def _excluded(path: str, patterns: List[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in patterns if p)


def _compact(text: str) -> str:
    return _PUNCT_RE.sub("", _WS_RE.sub("", text or "")).lower()


def _shingles(text: str, size: int = 3) -> Set[str]:
    if len(text) <= size:
        return {text} if text else set()
    return {text[i: i + size] for i in range(len(text) - size + 1)}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _item(path: str, title: str, detail: str = "") -> Dict[str, str]:
    return {"path": path, "title": title or path, "detail": detail}


def scan(root: Path, db, config: Optional[Dict[str, Any]] = None, *, deep: bool = True) -> Dict[str, Any]:
    """执行体检。返回 {score, total, issues[], scanned_at}，不写盘。"""
    cfg = _normalize_config(config)
    root = Path(root)
    notes = [n for n in db.all_notes() if not _excluded(n["path"], cfg["exclude"])]
    total = max(len(notes), 1)
    now = time.time()

    inbound: Dict[str, int] = {}
    outbound: Dict[str, int] = {}
    broken: Dict[str, List[str]] = {}
    for link in db.all_links():
        source = link["source_path"]
        target_path = link.get("target_path")
        if _excluded(source, cfg["exclude"]):
            continue
        outbound[source] = outbound.get(source, 0) + 1
        if target_path:
            inbound[target_path] = inbound.get(target_path, 0) + 1
        else:
            broken.setdefault(source, []).append(link["target"])

    tag_map: Dict[str, Set[str]] = {}
    for path, tag in db.all_tag_note_pairs():
        tag_map.setdefault(path, set()).add(tag)

    props = db.all_props()

    orphans: List[Dict[str, str]] = []
    empties: List[Dict[str, str]] = []
    stale: List[Dict[str, str]] = []
    untagged: List[Dict[str, str]] = []
    for note in notes:
        path = note["path"]
        title = note.get("title") or path
        if not inbound.get(path) and not outbound.get(path):
            orphans.append(_item(path, title, "没有入链，也没有出链"))
        if not (note.get("excerpt") or "").strip():
            empties.append(_item(path, title, "正文为空"))
        mtime = float(note.get("mtime") or 0)
        if mtime and (now - mtime) / 86400 > cfg["stale_days"]:
            days = int((now - mtime) / 86400)
            stale.append(_item(path, title, f"{days} 天未修改"))
        if not tag_map.get(path):
            untagged.append(_item(path, title, "没有标签，只能靠标题被找到"))

    broken_items = [
        _item(path, next((n.get("title") or path for n in notes if n["path"] == path), path),
              "、".join(f"[[{t}]]" for t in sorted(set(targets))[:6]))
        for path, targets in broken.items()
    ]

    duplicates: List[Dict[str, str]] = []
    if deep:
        try:
            duplicates = _find_duplicates(root, notes, cfg)
        except Exception:  # noqa: BLE001 —— 重复检测最贵，失败不影响其它项
            logger.exception("重复检测失败（跳过）")

    buckets = {
        "orphans": orphans,
        "broken": broken_items,
        "duplicates": duplicates,
        "empty": empties,
        "stale": stale,
        "untagged": untagged,
    }

    issues: List[Dict[str, Any]] = []
    penalty = 0.0
    for key, items in buckets.items():
        ratio = min(1.0, len(items) / total)
        penalty += WEIGHTS[key] * ratio
        issues.append({
            "key": key,
            "label": LABELS[key],
            "severity": SEVERITY[key],
            "count": len(items),
            "ratio": round(ratio, 4),
            "items": items[: int(cfg["max_items"])],
            "fixable": key in {"empty", "broken", "duplicates"},
        })
    issues.sort(key=lambda item: (-WEIGHTS[item["key"]] * item["ratio"], -item["count"]))

    score = int(round(max(0.0, min(1.0, 1.0 - penalty)) * 100))
    return {
        "score": score,
        "grade": "excellent" if score >= 90 else "good" if score >= 75 else "fair" if score >= 55 else "poor",
        "total": len(notes),
        "tag_total": len(tag_map),
        "scanned_at": time.time(),
        "deep": deep,
        "config": cfg,
        "issues": issues,
        "summary": {
            "orphans": len(orphans),
            "broken": len(broken_items),
            "duplicates": len(duplicates),
            "empty": len(empties),
            "stale": len(stale),
            "untagged": len(untagged),
        },
    }


def _read_body(root: Path, path: str) -> str:
    try:
        raw = (root / path.replace("/", "\\")).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return raw


def _find_duplicates(root: Path, notes: List[Dict], cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """近重复：归一化后 3-gram Jaccard ≥ 阈值。先按精确 hash 抓完全相同的。"""
    candidates = [
        n for n in notes
        if 16 <= int(n.get("size") or 0) <= 2 * 1024 * 1024
    ][: int(cfg["max_scan_files"])]
    texts: Dict[str, str] = {}
    for note in candidates:
        compact = _compact(_read_body(root, note["path"]))
        if len(compact) < 5:          # 只有标题/空白：归到「空笔记」，不算重复
            continue
        texts[note["path"]] = compact

    results: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    # 1) 完全相同：hash 分组
    by_hash: Dict[str, List[str]] = {}
    for note in candidates:
        compact = texts.get(note["path"])
        if not compact:
            continue
        digest = hashlib.md5(compact.encode("utf-8")).hexdigest()
        by_hash.setdefault(digest, []).append(note["path"])
    title_map = {n["path"]: n.get("title") or n["path"] for n in notes}
    for group in by_hash.values():
        if len(group) < 2:
            continue
        base = group[0]
        for other in group[1:]:
            key = tuple(sorted((base, other)))
            if key in seen:
                continue
            seen.add(key)
            results.append(_item(base, title_map[base], f"与 {other} 内容完全相同"))

    # 2) 近似：按长度排序，只在相邻长度窗口内比较，避免 O(n²) 爆炸。
    #    太短的正文（<30 字）做相似度没有意义，只参与上面的精确 hash 分组。
    ordered = sorted(
        ((path, text) for path, text in texts.items() if len(text) >= 30),
        key=lambda kv: len(kv[1]),
    )
    shingle_cache: Dict[str, Set[str]] = {}
    comparisons = 0
    for i, (path_a, text_a) in enumerate(ordered):
        len_a = len(text_a)
        for path_b, text_b in ordered[i + 1:]:
            if len(text_b) > len_a * 1.6 + 200:
                break
            key = tuple(sorted((path_a, path_b)))
            if key in seen:
                continue
            comparisons += 1
            if comparisons > 200_000:
                return results
            sa = shingle_cache.setdefault(path_a, _shingles(text_a))
            sb = shingle_cache.setdefault(path_b, _shingles(text_b))
            ratio = _jaccard(sa, sb)
            if ratio >= cfg["duplicate_ratio"]:
                seen.add(key)
                results.append(_item(
                    path_a, title_map.get(path_a, path_a),
                    f"与 {path_b} 相似度 {int(ratio * 100)}%",
                ))
    return results


# --------------------------------------------------------------------- 修复
_BROKEN_LINK_RE = re.compile(r"(!?)\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")


def strip_broken_links(
    vault,
    rel: str,
    broken_targets: Iterable[str],
    *,
    actor=None,
) -> Tuple[bool, str]:
    """把断链 ``[[x]]`` 降级为纯文本（有别名保留别名）。返回 (改动?, 新正文)。"""
    note = vault.read_note(rel)
    targets = {str(t).strip() for t in broken_targets if str(t).strip()}
    if not targets:
        return False, note["content"]
    changed = {"flag": False}

    def replace(match: re.Match) -> str:
        prefix, target, _anchor, alias = match.groups()
        base = target.split("/")[-1]
        if target not in targets and base not in targets:
            return match.group(0)
        changed["flag"] = True
        text = alias or base
        return text if not prefix else f"{text}（附件缺失）"

    new_content = _BROKEN_LINK_RE.sub(replace, note["content"])
    if changed["flag"]:
        vault.write_note(
            rel, content=new_content, frontmatter=note["frontmatter"], actor=actor
        )
    return changed["flag"], new_content
