"""跨 Agent 会话蒸馏（C 第一批）：标准输入校验 → 六类槽位提取 → 待审草案。

边界（与 docs/INNOVATION-PLAN.md §6 对齐）
----------------------------------------
- **草案与落地分离**：``create_draft`` 只写 ``.kwey/session/drafts/`` 控制文件，
  不写普通笔记、不进索引、不自动批准；``approve_draft`` 只在人工确认后经
  VaultService 统一写路径创建最小证据笔记与蒸馏笔记。
- **来源锚点**：每条候选事实必须带 ``source_event_ids``（会话内 event id），
  无依据的候选直接丢弃；草案同时保存最小引用段（selected_evidence），
  不复制完整原始日志。批准时校验引用段与事件切片一致，证据段不由模型补写。
- **服务端计算 hash**：不采信客户端声明的 export_hash（LLM/适配器输出不是校验值）。
  幂等键 = ``agent + session_id + export_hash + 解析版本``，其中 export_hash 由
  服务端按规范化输入实际计算（输出字段仍叫 ``export_hash``，客户端声明值只记在
  ``client_export_hash`` 备查）。``build_hash`` 额外记录模板/解析器/模型配置版本。
- **状态机幂等**：``pending_review → approved / rejected / expired``；重复确认不
  重复建笔记，已拒绝不能批准，过期草案清理引用段后不可恢复。
- **离线预览**：``preview()`` 纯计算，不落盘、不产生 draft_id，供 CLI 使用。

提取器是确定性的规则实现（离线、无模型调用）：它保证「无证据不保存」与
可审计，但不承诺语义完备；正则重合不能替代人工正确性评估。

**未实测项**：本批只实现标准 JSON 输入；没有真实 Claude Code 日志样本，因此
未内置 ``transcript_path`` / 原始 JSONL 适配器，也未安装任何 hook、未改外部
Agent 配置。原始日志格式适配留待拿到样本后再做。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
PARSER_VERSION = 1
TEMPLATE_VERSION = 1

MAX_EVENTS = 2000
MAX_EVENT_CHARS = 20000
MAX_TOTAL_CHARS = 400000
MAX_GOAL_CHARS = 2000
MAX_CLIENT_HASH_CHARS = 200
MAX_CANDIDATES_PER_SLOT = 200
MAX_QUOTE_CHARS = 500
DRAFT_TTL_DAYS = 30

EVENT_KINDS = ("user", "assistant", "tool_call", "tool_result", "system")
RETENTIONS = ("selected_evidence",)
SLOT_KEYS = (
    "decisions",
    "constraints",
    "refutations",
    "open_items",
    "knowledge_candidates",
)

DRAFT_DIR = Path(".kwey") / "session" / "drafts"

STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
DRAFT_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED)

DISTILL_DIR = "notes/蒸馏"
EVIDENCE_DIR = "notes/蒸馏/证据"

_DRAFT_ID_RE = re.compile(r"^draft_[0-9a-f]{16}$")

_LOCK = threading.RLock()

# 六类槽位的规则（确定性；kind 白名单防止模型自述被提升为授权/决策）。
_RULES: Dict[str, Tuple[re.Pattern[str], Tuple[str, ...]]] = {
    "decisions": (
        re.compile(r"决定|结论|采用|选择|定为|最终|统一用|不再|改成|改为|就用"),
        ("user", "assistant"),
    ),
    "constraints": (
        re.compile(r"不要|不准|不能|必须|只能|仅限|禁止|不得|不许|别动|不碰|只改|不许推送"),
        ("user",),
    ),
    "refutations": (
        re.compile(r"失败|不行|报错|无法|证伪|行不通|不成立|没解决|仍然失败|error|failed"),
        ("user", "assistant", "tool_result"),
    ),
    "open_items": (
        re.compile(r"TODO|待办|待定|未完成|还没|尚未|下一步|还需|剩余|剩下|接下来"),
        ("user", "assistant"),
    ),
    "knowledge_candidates": (
        re.compile(r"发现|经验|原来|规律|事实是|结论是|注意|记住|稳定"),
        ("user", "assistant"),
    ),
}

# 这些说法属于「环境限制 / 达到轮次上限」，不能算方案被证伪（§6.3）。
_ENV_LIMIT = re.compile(
    r"轮次上限|上下文上限|token 上限|环境不可用|环境问题|网络不可用|权限不足|超时|系统限制"
)
_PREFERENCE = re.compile(r"偏好|喜欢|倾向|习惯|更喜欢")
_DONE_CLAIM = re.compile(r"已完成|已经完成|完成了|已解决|done|finished")
_UNVERIFIED = re.compile(r"未实测|未验证|没验证|未测试|待核验")


class DistillError(ValueError):
    """标准输入校验失败（传输层映射为 400，CLI 映射为退出码 1）。"""


class DraftNotFound(DistillError):
    """草案不存在（传输层映射为 404）。"""


class DraftStateError(DistillError):
    """草案状态不允许该操作（默认 409；过期用 410）。"""

    def __init__(self, message: str, *, code: int = 409) -> None:
        super().__init__(message)
        self.code = int(code)


# --------------------------------------------------------------------- 规范化
def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _require_str(value: Any, label: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DistillError(f"{label} 必须是非空字符串")
    if len(value) > max_chars:
        raise DistillError(f"{label} 超长（上限 {max_chars} 字符）")
    return value.strip()


def validate_payload(payload: Any) -> Dict[str, Any]:
    """校验标准 JSON 输入并返回规范化文档（含服务端 hash）。

    硬限制：schema_version 必须是 1；source.agent / source.session_id 必填；
    events 为 1..MAX_EVENTS 的数组，event id 会话内唯一，kind 枚举，
    单条 text ≤ MAX_EVENT_CHARS、整体 ≤ MAX_TOTAL_CHARS。
    """
    if not isinstance(payload, dict):
        raise DistillError("payload 必须是 JSON 对象")

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION or isinstance(version, bool):
        raise DistillError(
            f"schema_version 必须为 {SCHEMA_VERSION}（收到 {version!r}）"
        )

    source = payload.get("source")
    if not isinstance(source, dict):
        raise DistillError("source 必须是对象（含 agent / session_id）")
    agent = _require_str(source.get("agent"), "source.agent", max_chars=100)
    session_id = _require_str(source.get("session_id"), "source.session_id", max_chars=200)
    raw_hash = source.get("export_hash")
    client_export_hash: Optional[str] = None
    if raw_hash is not None:
        if not isinstance(raw_hash, str):
            raise DistillError("source.export_hash 必须是字符串")
        client_export_hash = raw_hash.strip()[:MAX_CLIENT_HASH_CHARS] or None

    goal = payload.get("goal")
    if goal is not None and not isinstance(goal, str):
        raise DistillError("goal 必须是字符串")
    goal = (goal or "").strip()
    if len(goal) > MAX_GOAL_CHARS:
        raise DistillError(f"goal 超长（上限 {MAX_GOAL_CHARS} 字符）")

    retention = payload.get("retention", "selected_evidence")
    if retention not in RETENTIONS:
        raise DistillError(
            f"retention 仅支持 {'/'.join(RETENTIONS)}（首版不复制完整原始日志）"
        )

    events_raw = payload.get("events")
    if not isinstance(events_raw, list) or not events_raw:
        raise DistillError("events 必须是非空数组")
    if len(events_raw) > MAX_EVENTS:
        raise DistillError(f"events 超限：{len(events_raw)} > {MAX_EVENTS}")

    events: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    total_chars = 0
    for i, event in enumerate(events_raw):
        where = f"events[{i}]"
        if not isinstance(event, dict):
            raise DistillError(f"{where} 必须是对象")
        eid = _require_str(event.get("id"), f"{where}.id", max_chars=100)
        if eid in seen_ids:
            raise DistillError(f"{where}.id 重复: {eid}")
        seen_ids.add(eid)
        kind = event.get("kind")
        if kind not in EVENT_KINDS:
            raise DistillError(
                f"{where}.kind 必须是 {'/'.join(EVENT_KINDS)} 之一（收到 {kind!r}）"
            )
        text = event.get("text")
        if not isinstance(text, str):
            raise DistillError(f"{where}.text 必须是字符串")
        if len(text) > MAX_EVENT_CHARS:
            raise DistillError(f"{where}.text 超长（上限 {MAX_EVENT_CHARS} 字符）")
        total_chars += len(text)
        ts = event.get("ts")
        if ts is not None and not isinstance(ts, str):
            raise DistillError(f"{where}.ts 必须是字符串")
        events.append(
            {
                "id": eid,
                "kind": kind,
                "text": text,
                "ts": (ts or "").strip()[:100] or None,
            }
        )

    if total_chars > MAX_TOTAL_CHARS:
        raise DistillError(f"输入正文超限：{total_chars} > {MAX_TOTAL_CHARS} 字符")

    canonical_input = {
        "schema_version": SCHEMA_VERSION,
        "source": {"agent": agent, "session_id": session_id},
        "goal": goal,
        "events": events,
        "retention": retention,
    }
    export_hash = _sha256(_canonical(canonical_input))
    return {
        **canonical_input,
        "export_hash": export_hash,
        "client_export_hash": client_export_hash,
        "parser_version": PARSER_VERSION,
        "stats": {
            "events": len(events),
            "chars": total_chars,
            "empty_text_events": sum(1 for e in events if not e["text"].strip()),
        },
    }


# --------------------------------------------------------------------- 提取
# 切句规则（规则版，宁少断不错断）：
# - 换行、中文句读（。！？；）直接断句；
# - ASCII 标点（.!?;）后跟空白即断句；紧随另一个 ASCII 标点时视为连写（"..." / "?!"），
#   在连写结束处再判；
# - 无空格直接接下一段（"First.Second"）默认断句，但以下 token 内不断：
#   路径/文件名（以点开头如 ".kwey"、带已知扩展名如 "config.yml"）、版本号（"v1.2"、"3.14"）、
#   邮箱/域名（"a@b.com"）、常见缩写（"e.g."、"i.e."、"etc."）。
_CJK_TERMINALS = "。！？；"
_ASCII_TERMINALS = ".!?;"
_CLOSERS = "\"')]”’」』"
_TOKEN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)
_ABBREVIATIONS = {
    "e.g", "i.e", "etc", "vs", "cf", "al", "fig", "no", "mr", "mrs", "ms",
    "dr", "prof", "st", "approx", "resp", "eg", "ie", "approx.",
}
_TOKEN_TAILS = {
    # 文件扩展名 / 常见顶级域：点后命中则视为同一 token
    "md", "markdown", "txt", "py", "js", "ts", "tsx", "jsx", "json", "yaml",
    "yml", "toml", "ini", "cfg", "conf", "ico", "png", "jpg", "jpeg", "gif",
    "svg", "pdf", "html", "htm", "css", "csv", "db", "log", "sh", "ps1", "exe",
    "zip", "com", "org", "net", "io", "cn", "dev", "ai", "app",
}
_VERSION_RE = re.compile(r"[vV]?\d+(?:\.\d+)*")


def _scan_token_back(text: str, index: int) -> str:
    start = index
    while start > 0 and text[start - 1] in _TOKEN_CHARS:
        start -= 1
    return text[start:index]


def _scan_token_forward(text: str, index: int) -> str:
    end = index + 1
    while end < len(text) and text[end] in _TOKEN_CHARS:
        end += 1
    return text[index + 1:end]


def _keeps_token(text: str, index: int) -> bool:
    """点号是否属于同一 token（路径/版本/域名/缩写），是则不在此处断句。"""
    prev_token = _scan_token_back(text, index)
    next_token = _scan_token_forward(text, index)
    if not prev_token:
        # ".kwey"：点号位于 token 起始，按路径片段处理
        return True
    if "@" in prev_token or "@" in next_token:
        return True
    if _VERSION_RE.fullmatch(prev_token):
        return True
    if prev_token.isdigit() and next_token[:1].isdigit():
        return True
    if next_token.lower() in _TOKEN_TAILS or prev_token.lower() in _TOKEN_TAILS:
        return True
    if prev_token.lower() in _ABBREVIATIONS:
        return True
    # "e.g" / "i.e"：首个点号前后都是单字母，按缩写链处理
    if len(prev_token) == 1 and prev_token.islower() and re.match(r"[A-Za-z]\.", next_token):
        return True
    return False


def _is_sentence_end(text: str, index: int) -> bool:
    nxt = text[index + 1] if index + 1 < len(text) else ""
    if nxt and nxt in _ASCII_TERMINALS:
        return False  # "..." / "?!": 由连写末尾统一判定
    prev_token = _scan_token_back(text, index)
    if not nxt or nxt.isspace() or nxt in _CLOSERS:
        return prev_token.lower().rstrip(".") not in _ABBREVIATIONS
    if nxt.isalnum() or nxt in "_-":
        return not _keeps_token(text, index)
    return True


def _split_sentences(text: str) -> List[str]:
    out: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\r\n":
            out.append(text[start:i])
            i += 1
            start = i
            continue
        if ch in _CJK_TERMINALS:
            j = i + 1
            while j < n and (text[j] in _CJK_TERMINALS or text[j] in _ASCII_TERMINALS):
                j += 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            out.append(text[start:j])
            i = j
            start = j
            continue
        if ch in _ASCII_TERMINALS and _is_sentence_end(text, i):
            j = i + 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            out.append(text[start:j])
            while j < n and text[j] in " \t":
                j += 1
            i = j
            start = j
            continue
        i += 1
    out.append(text[start:])
    return out


def _sentences(text: str) -> List[str]:
    """按行与中英文句读切分；保留原顺序，过滤过短片段。"""
    out: List[str] = []
    for part in _split_sentences(text):
        clean = re.sub(r"\s+", " ", part).strip()
        if len(clean) < 4:
            continue
        out.append(clean[:1000])
    return out


def _add_candidate(
    bucket: Dict[str, List[Dict[str, Any]]],
    seen: Dict[Tuple[str, str], Dict[str, Any]],
    slot: str,
    text: str,
    event: Dict[str, Any],
    quote: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if len(bucket[slot]) >= MAX_CANDIDATES_PER_SLOT:
        return
    key = (slot, re.sub(r"\s+", "", text))
    existing = seen.get(key)
    if existing is not None:
        if event["id"] not in existing["source_event_ids"]:
            existing["source_event_ids"].append(event["id"])
        return
    item: Dict[str, Any] = {
        "slot": slot,
        "text": text,
        "source_event_ids": [event["id"]],
        "quote": quote,
    }
    if extra:
        item.update(extra)
    seen[key] = item
    bucket[slot].append(item)


def extract(doc: Dict[str, Any]) -> Dict[str, Any]:
    """从规范化文档提取六类槽位；每条候选都带 source_event_ids。

    ``omissions`` 记录：空文本事件、被截断的引用段、因环境/轮次限制而
    「未实测」的输入（不当作证伪），以及超出单槽上限被丢弃的候选计数。
    """
    candidates: Dict[str, List[Dict[str, Any]]] = {slot: [] for slot in SLOT_KEYS}
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    omissions: List[Dict[str, Any]] = []
    event_meta: Dict[str, Dict[str, Any]] = {e["id"]: e for e in doc["events"]}
    quote_by_event: Dict[str, str] = {}
    slot_drop = 0
    quote_truncated = 0

    for event in doc["events"]:
        eid, kind, text = event["id"], event["kind"], event["text"]
        if not text.strip():
            omissions.append(
                {
                    "event_id": eid,
                    "reason": "empty_text",
                    "detail": "事件正文为空，未参与提取；原文仍在来源会话中",
                    "accessible": True,
                }
            )
            continue

        for sentence in _sentences(text):
            quote = sentence[:MAX_QUOTE_CHARS]
            if len(sentence) > MAX_QUOTE_CHARS:
                quote_truncated += 1
                omissions.append(
                    {
                        "event_id": eid,
                        "reason": "quote_truncated",
                        "detail": f"引用段超过 {MAX_QUOTE_CHARS} 字符，已截断；完整原文在来源会话",
                        "accessible": True,
                    }
                )

            for slot, (pattern, allowed_kinds) in _RULES.items():
                if kind not in allowed_kinds or not pattern.search(sentence):
                    continue

                if slot == "refutations" and _ENV_LIMIT.search(sentence):
                    # 环境限制 / 轮次上限不是方案失败，如实标为未实测。
                    omissions.append(
                        {
                            "event_id": eid,
                            "reason": "environment_or_round_limit_not_refutation",
                            "detail": quote,
                            "accessible": True,
                            "note": "未实测，不作为证伪证据",
                        }
                    )
                    break

                extra: Dict[str, Any] = {}
                if slot == "constraints":
                    extra["scope"] = "user_explicit"
                    extra["authorization"] = True
                elif slot == "refutations":
                    extra["evidence_kind"] = kind
                    extra["not_tested"] = bool(_UNVERIFIED.search(sentence))
                elif slot == "open_items":
                    extra["status"] = (
                        "done_claimed"
                        if _DONE_CLAIM.search(sentence)
                        else "open"
                    )
                    extra["evidence"] = (
                        "tool_result" if kind == "tool_result" else kind
                    )
                elif slot == "knowledge_candidates":
                    extra["confidence_type"] = "rule_candidate"
                    extra["requires_user_confirmation"] = bool(
                        _PREFERENCE.search(sentence)
                    )

                if len(candidates[slot]) >= MAX_CANDIDATES_PER_SLOT:
                    slot_drop += 1
                    continue
                _add_candidate(
                    candidates, seen, slot, sentence, event, quote, extra
                )
                quote_by_event.setdefault(eid, quote)

    # 兜底：无来源锚点的候选一律不入库（防御性；正常路径不会触发）。
    dropped_no_evidence = 0
    for slot in SLOT_KEYS:
        kept = [c for c in candidates[slot] if c.get("source_event_ids")]
        dropped_no_evidence += len(candidates[slot]) - len(kept)
        candidates[slot] = kept

    # sources 只收录被候选实际引用的来源事件（锚点 → 最小引用段）。
    referenced: List[str] = []
    for slot in SLOT_KEYS:
        for item in candidates[slot]:
            for eid in item["source_event_ids"]:
                if eid not in referenced:
                    referenced.append(eid)
    sources = {
        eid: {
            "kind": event_meta[eid]["kind"],
            "ts": event_meta[eid]["ts"],
            "quote": quote_by_event.get(eid, ""),
        }
        for eid in referenced
    }

    if slot_drop:
        omissions.append(
            {
                "event_id": None,
                "reason": "slot_cap",
                "detail": f"单槽最多 {MAX_CANDIDATES_PER_SLOT} 条，{slot_drop} 条候选未进入草案",
                "accessible": True,
            }
        )

    stats = {
        "events": len(doc["events"]),
        "chars": doc["stats"]["chars"],
        "candidates": {slot: len(candidates[slot]) for slot in SLOT_KEYS},
        "total_candidates": sum(len(candidates[slot]) for slot in SLOT_KEYS),
        "omissions": len(omissions),
        "dropped_no_evidence": dropped_no_evidence,
        "quote_truncated": quote_truncated,
    }
    return {
        "candidates": candidates,
        "sources": sources,
        "omissions": omissions,
        "stats": stats,
    }


def build_hash(
    doc: Dict[str, Any],
    candidates: Dict[str, List[Dict[str, Any]]],
    omissions: List[Dict[str, Any]],
) -> str:
    """服务端构建 hash：输入 + 解析器/模板版本，不采信模型或客户端声明。"""
    return _sha256(
        _canonical(
            {
                "schema_version": doc.get("schema_version", SCHEMA_VERSION),
                "export_hash": doc["export_hash"],
                "parser_version": doc["parser_version"],
                "template_version": TEMPLATE_VERSION,
                "model": "rule-based",
                "source": {
                    "agent": doc["source"]["agent"],
                    "session_id": doc["source"]["session_id"],
                },
                "goal": doc.get("goal", ""),
                "candidates": candidates,
                "omissions": omissions,
            }
        )
    )


def evidence_hash(sources: Dict[str, Dict[str, Any]]) -> str:
    """引用段指纹：批准前校验证据段未被改写；值来自实际输入切片。"""
    return _sha256(_canonical(sources))


def _result(doc: Dict[str, Any], extracted: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "source": doc["source"],
        "goal": doc["goal"],
        "retention": doc["retention"],
        "export_hash": doc["export_hash"],
        "client_export_hash": doc["client_export_hash"],
        "idempotency_key": idempotency_key(doc),
        "build_hash": build_hash(
            doc, extracted["candidates"], extracted["omissions"]
        ),
        "evidence_hash": evidence_hash(extracted["sources"]),
        **extracted,
    }


def idempotency_key(doc: Dict[str, Any]) -> str:
    """agent + session_id + 服务端 export_hash + 解析版本（§6.5）。"""
    return "|".join(
        (
            doc["source"]["agent"],
            doc["source"]["session_id"],
            doc["export_hash"],
            str(doc["parser_version"]),
        )
    )


def preview(payload: Any) -> Dict[str, Any]:
    """离线预览：只返回候选 / 来源 / 遗漏 / preview_hash，不落盘、不产生 draft_id。"""
    doc = validate_payload(payload)
    extracted = extract(doc)
    result = _result(doc, extracted)
    preview_hash = _sha256(
        _canonical(
            {
                "export_hash": doc["export_hash"],
                "parser_version": doc["parser_version"],
                "candidates": result["candidates"],
                "omissions": result["omissions"],
            }
        )
    )
    return {
        **result,
        "preview_hash": preview_hash,
        "persisted": False,
        "note": (
            "离线预览：未创建草案 / 普通笔记，未入队；需要持久待审草案请用 "
            "kwey.session.distill。同输入 hash 可相同，但重复生成结果不承诺确定一致。"
        ),
    }


# --------------------------------------------------------------------- 持久草案
def _draft_id(key: str) -> str:
    return "draft_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _draft_path(root: Path, draft_id: str) -> Path:
    return Path(root) / DRAFT_DIR / f"{draft_id}.json"


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _is_expired(created_at: str) -> bool:
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    now = datetime.now(timezone.utc).astimezone()
    if created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)
    return (now - created).days >= DRAFT_TTL_DAYS


def _expire_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    """过期即清理引用段与候选正文，只留计数；不伪造仍可读。"""
    stats = draft.get("stats") or {}
    draft.update(
        {
            "status": "expired",
            "expired_at": _now(),
            "candidates": {slot: [] for slot in SLOT_KEYS},
            "sources": {},
            "omissions": [],
            "stats": {**stats, "purged": True},
            "note": "草案已过期，引用段已清理；重新生成需要显式再次提交",
        }
    )
    return draft


def create_draft(root: Path, payload: Any) -> Dict[str, Any]:
    """校验 → 提取 → 写 ``.kwey/session/drafts/<draft_id>.json``（幂等）。

    只写控制文件：不写普通笔记、不进索引、不自动批准。同一幂等键重复提交
    返回既有 draft_id（``duplicate=true``），不覆盖旧结论；过期草案返回
    ``status=expired``（内容已清理）。
    """
    doc = validate_payload(payload)
    key = idempotency_key(doc)
    draft_id = _draft_id(key)
    path = _draft_path(root, draft_id)
    with _LOCK:
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing is not None:
                if existing.get("status") != STATUS_EXPIRED and _is_expired(
                    existing.get("created_at", "")
                ):
                    existing = _expire_draft(existing)
                    _atomic_write_json(path, existing)
                existing.update(
                    {
                        "duplicate": True,
                        "draft_path": _rel_to_root(root, path),
                    }
                )
                return existing

        extracted = extract(doc)
        draft = {
            **_result(doc, extracted),
            "draft_id": draft_id,
            "version": 1,
            "status": STATUS_PENDING,
            "created_at": _now(),
            "duplicate": False,
            "targets": draft_targets({"draft_id": draft_id, **doc}),
            "draft_path": _rel_to_root(root, path),
            "note": (
                "待审草案：未写普通笔记、未自动批准；重复提交同一幂等键返回此 "
                "draft_id。批准与落笔记由用户在后续审批回路中显式执行。"
            ),
        }
        _atomic_write_json(path, draft)
        return draft


# --------------------------------------------------------------------- 落地链路
def _rel_to_root(root: Path, path: Path) -> str:
    return str(Path(path).relative_to(Path(root))).replace("\\", "/")


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _refresh_draft(root: Path, draft: Dict[str, Any], *, persist: bool) -> Dict[str, Any]:
    """待审草案过期即清理引用段；已进入终态的草案保持不动。"""
    if draft.get("status") == STATUS_PENDING and _is_expired(
        str(draft.get("created_at") or "")
    ):
        _expire_draft(draft)
        if persist:
            _save_draft(root, draft)
    return draft


def _save_draft(root: Path, draft: Dict[str, Any]) -> None:
    path = _draft_path(root, str(draft.get("draft_id") or ""))
    _atomic_write_json(path, draft)


def load_draft(
    root: Path, draft_id: str, *, persist_expiry: bool = True
) -> Dict[str, Any]:
    """读取草案；不存在抛 :class:`DraftNotFound`，待审过期会就地转 expired。"""
    key = str(draft_id or "").strip()
    if not _DRAFT_ID_RE.match(key):
        raise DraftNotFound(f"草案 ID 非法: {draft_id!r}")
    path = _draft_path(root, key)
    if not path.is_file():
        raise DraftNotFound(f"草案不存在: {key}")
    draft = _load_json(path)
    if draft is None:
        raise DraftNotFound(f"草案不存在或已损坏: {key}")
    return _refresh_draft(root, draft, persist=persist_expiry)


def draft_build_hash(draft: Dict[str, Any]) -> str:
    """按草案当前实际内容重算构建 hash（不盲信已存字段）。"""
    return build_hash(
        draft,
        draft.get("candidates") or {slot: [] for slot in SLOT_KEYS},
        list(draft.get("omissions") or []),
    )


def _slug(text: Any, *, limit: int = 48) -> str:
    value = re.sub(r'[\\/:*?"<>|\s]+', "-", str(text or "").strip())
    value = value.strip("-._")[:limit].strip("-._")
    return value or "session"


def draft_targets(draft: Dict[str, Any]) -> Dict[str, str]:
    """两篇目标笔记的稳定 vault 相对路径（不含任何绝对路径）。"""
    draft_id = str(draft.get("draft_id") or "")
    short = draft_id.replace("draft_", "")[:8] or "unknown"
    source = draft.get("source") or {}
    name = f"{_slug(source.get('agent'))}-{_slug(source.get('session_id'))}-{short}"
    return {
        "distill": f"{DISTILL_DIR}/{name}.md",
        "evidence": f"{EVIDENCE_DIR}/{name}.md",
    }


def draft_summary(draft: Dict[str, Any]) -> Dict[str, Any]:
    candidates = draft.get("candidates") or {}
    notes = list(draft.get("notes") or [])
    return {
        "draft_id": draft.get("draft_id"),
        "version": int(draft.get("version") or 1),
        "status": draft.get("status"),
        "created_at": draft.get("created_at"),
        "decided_at": draft.get("decided_at"),
        "source": draft.get("source"),
        "goal": draft.get("goal"),
        "build_hash": draft.get("build_hash") or draft_build_hash(draft),
        "evidence_hash": draft.get("evidence_hash"),
        "total_candidates": sum(len(items or []) for items in candidates.values()),
        "omissions": len(draft.get("omissions") or []),
        "targets": draft.get("targets") or draft_targets(draft),
        "notes": notes,
        "duplicate": bool(draft.get("duplicate")),
        "note": draft.get("note"),
    }


def list_drafts(
    root: Path,
    *,
    status: Optional[str] = None,
    limit: int = 100,
    persist_expiry: bool = True,
) -> List[Dict[str, Any]]:
    """列出草案摘要（最新在前）；读取失败的损坏控制文件跳过。"""
    directory = Path(root) / DRAFT_DIR
    if not directory.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in directory.glob("draft_*.json"):
        draft = _load_json(path)
        if draft is None:
            continue
        _refresh_draft(root, draft, persist=persist_expiry)
        if status and str(draft.get("status")) != str(status):
            continue
        rows.append(draft_summary(draft))
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[: max(1, min(int(limit), 500))]


def _validate_evidence(draft: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """批准前校验：每条候选的 source_event_ids 都能在 sources 找到实际引用段。"""
    sources = draft.get("sources") or {}
    if not isinstance(sources, dict):
        raise DraftStateError("草案来源段缺失，不能生成证据笔记")
    for slot in SLOT_KEYS:
        for item in draft.get("candidates", {}).get(slot) or []:
            ids = list(item.get("source_event_ids") or [])
            if not ids:
                raise DraftStateError("存在无来源锚点的候选，拒绝落地")
            for eid in ids:
                source = sources.get(eid)
                if not isinstance(source, dict) or not str(source.get("quote") or "").strip():
                    raise DraftStateError(
                        f"候选缺少事件 {eid} 的实际输入引用段，拒绝落地"
                    )
            quote = str(item.get("quote") or "")
            if not any(quote == str(sources[eid].get("quote") or "") for eid in ids):
                raise DraftStateError(
                    f"候选引用段与事件 {ids[0]} 的实际输入不一致，拒绝落地"
                )
    expected = draft.get("evidence_hash")
    if expected and str(expected) != evidence_hash(sources):
        raise DraftStateError("证据段指纹与草案记录不一致，拒绝落地")
    return sources


def _note_frontmatter(
    draft: Dict[str, Any],
    *,
    role: str,
    sources: List[str],
    approved_by: str,
) -> Dict[str, Any]:
    source = draft.get("source") or {}
    agent = str(source.get("agent") or "")
    session_id = str(source.get("session_id") or "")
    build = str(draft.get("build_hash") or draft_build_hash(draft))
    label = "证据" if role == "evidence" else "蒸馏"
    tags = ["会话蒸馏", f"会话蒸馏/{_slug(agent, limit=24)}"]
    if role == "evidence":
        tags.append("会话蒸馏/证据")
    fm: Dict[str, Any] = {
        "title": f"会话{label} {agent} {session_id}".strip(),
        "tags": tags,
        "session_agent": agent,
        "session_id": session_id,
        "session_sources": list(sources),
        "session_build_hash": build,
        "session_export_hash": str(draft.get("export_hash") or ""),
        "session_draft_id": str(draft.get("draft_id") or ""),
        "session_approved_at": str(draft.get("approved_at") or _now()),
        "session_approved_by": approved_by,
    }
    goal = str(draft.get("goal") or "").strip()
    if goal:
        fm["session_goal"] = goal
    return fm


def _render_evidence_note(draft: Dict[str, Any]) -> str:
    source = draft.get("source") or {}
    sources = draft.get("sources") or {}
    lines = [
        f"# 会话证据 · {source.get('agent') or ''} / {source.get('session_id') or ''}",
        "",
        "> 本笔记的引用段来自会话输入的实际事件切片，未经模型补写；"
        "仅保存最小引用段，不保存完整原始日志。",
        "",
        f"- 来源 Agent：`{source.get('agent') or ''}`",
        f"- 会话 ID：`{source.get('session_id') or ''}`",
        f"- 采集输入 hash：`{draft.get('export_hash') or ''}`",
        f"- 构建 hash：`{draft.get('build_hash') or ''}`",
        f"- 草案：`{draft.get('draft_id') or ''}`",
        "- 留存策略：`selected_evidence`",
        "",
        "## 引用段",
        "",
    ]
    for eid, item in sources.items():
        lines.append(
            f"### {eid} · {item.get('kind') or ''}"
            + (f" · {item.get('ts')}" if item.get("ts") else "")
        )
        lines.append("")
        lines.append("> " + str(item.get("quote") or "").replace("\n", " "))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_distill_note(draft: Dict[str, Any], evidence_rel: str) -> str:
    source = draft.get("source") or {}
    lines = [
        f"# 会话蒸馏 · {source.get('agent') or ''} / {source.get('session_id') or ''}",
        "",
        f"- 来源 Agent：`{source.get('agent') or ''}`",
        f"- 会话 ID：`{source.get('session_id') or ''}`",
        f"- 构建 hash：`{draft.get('build_hash') or ''}`",
        f"- 证据笔记：[[{evidence_rel}]]",
        f"- 草案：`{draft.get('draft_id') or ''}`",
    ]
    goal = str(draft.get("goal") or "").strip()
    if goal:
        lines.append(f"- 目标：{goal}")
    lines.append("")
    sections = (
        ("decisions", "决策"),
        ("constraints", "约束"),
        ("refutations", "证伪"),
        ("open_items", "未完成"),
        ("knowledge_candidates", "知识候选"),
    )
    candidates = draft.get("candidates") or {}
    for slot, title in sections:
        lines.append(f"## {title}")
        lines.append("")
        items = candidates.get(slot) or []
        if not items:
            lines.append("（无）")
        for item in items:
            refs = ", ".join(str(eid) for eid in item.get("source_event_ids") or [])
            lines.append(f"- {item.get('text') or ''}（来源：{refs}）")
        lines.append("")
    omissions = list(draft.get("omissions") or [])
    lines.append("## 遗漏")
    lines.append("")
    if not omissions:
        lines.append("（无）")
    for item in omissions:
        eid = item.get("event_id")
        prefix = f"事件 `{eid}` · " if eid else ""
        lines.append(f"- {prefix}`{item.get('reason') or ''}`：{item.get('detail') or ''}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _ensure_generated_note(
    vault: Any,
    rel: str,
    *,
    draft_id: str,
    content: str,
    frontmatter: Dict[str, Any],
    actor: Any,
) -> Dict[str, Any]:
    """创建一篇目标笔记；若同草案上次已创建则复用，不重复写。"""
    if _target_owned_by(vault, rel, draft_id):
        return {"path": rel, "created": False}
    vault.create_note(rel, content=content, frontmatter=frontmatter, actor=actor)
    return {"path": rel, "created": True}


def _target_owned_by(vault: Any, rel: str, draft_id: str) -> bool:
    """目标路径可用性：不存在返回 False；属于同草案返回 True；否则 409。"""
    abs_path = vault.paths.to_abs(rel)
    if not abs_path.is_file():
        return False
    note = vault.read_note(rel)
    existing_id = str((note.get("frontmatter") or {}).get("session_draft_id") or "")
    if existing_id != draft_id:
        raise DraftStateError(f"目标笔记已存在且不属于本草案: {rel}")
    return True


def approved_result(draft: Dict[str, Any], *, duplicate: bool) -> Dict[str, Any]:
    return {
        "draft_id": draft.get("draft_id"),
        "version": int(draft.get("version") or 1),
        "status": STATUS_APPROVED,
        "duplicate": bool(duplicate),
        "build_hash": draft.get("build_hash") or draft_build_hash(draft),
        "source": draft.get("source"),
        "notes": list(draft.get("notes") or []),
        "message": (
            "重复确认：沿用既有笔记，未再次创建"
            if duplicate
            else "已通过人工审批并经 VaultService 创建蒸馏笔记与最小证据笔记"
        ),
    }


def reject_draft(
    root: Path,
    draft_id: str,
    *,
    reason: str = "",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """拒绝草案：pending_review → rejected；重复拒绝幂等。"""
    with _LOCK:
        draft = load_draft(root, draft_id)
        status = str(draft.get("status") or "")
        if status == STATUS_REJECTED:
            return {
                "draft_id": draft.get("draft_id"),
                "status": STATUS_REJECTED,
                "duplicate": True,
                "message": "重复拒绝：状态未变化",
            }
        if status == STATUS_APPROVED:
            raise DraftStateError("草案已批准，不能改为拒绝")
        if status == STATUS_EXPIRED:
            raise DraftStateError("草案已过期，不可恢复", code=410)
        stamp = now or _now()
        draft.update(
            {
                "status": STATUS_REJECTED,
                "decided_at": stamp,
                "rejected_at": stamp,
                "reject_reason": str(reason or "")[:500],
                "duplicate": False,
            }
        )
        _save_draft(root, draft)
        return {
            "draft_id": draft.get("draft_id"),
            "status": STATUS_REJECTED,
            "duplicate": False,
            "decided_at": stamp,
            "message": "已拒绝，未创建任何笔记",
        }


def approve_draft(
    root: Path,
    draft_id: str,
    *,
    vault: Any,
    expected_build_hash: str = "",
    approved_by: str = "local-ui",
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """人工确认后落地：经 VaultService 创建最小证据笔记 + 蒸馏笔记（幂等）。"""
    with _LOCK:
        draft = load_draft(root, draft_id)
        status = str(draft.get("status") or "")
        if status == STATUS_APPROVED:
            return approved_result(draft, duplicate=True)
        if status == STATUS_REJECTED:
            raise DraftStateError("草案已被拒绝，不能批准")
        if status == STATUS_EXPIRED:
            raise DraftStateError("草案已过期，不可恢复", code=410)

        current = draft_build_hash(draft)
        stored = str(draft.get("build_hash") or "")
        if stored and stored != current:
            raise DraftStateError("草案内容与构建 hash 不一致，拒绝落地")
        if expected_build_hash and str(expected_build_hash) != current:
            raise DraftStateError("构建 hash 已变化，请重新载入草案", code=409)

        sources = _validate_evidence(draft)
        targets = draft_targets(draft)
        stamp = now or _now()
        draft.update(
            {
                "version": int(draft.get("version") or 1),
                "build_hash": current,
                "evidence_hash": evidence_hash(sources),
                "targets": targets,
                "approved_at": stamp,
                "decided_at": stamp,
                "approved_by": str(approved_by or "local-ui")[:100],
            }
        )

        from app.vault.provenance import ActorContext

        actor = ActorContext(
            kind="agent",
            name="kwey-session-distill",
            origin="session-distill",
            run_id=str(draft.get("draft_id") or ""),
        )

        # 先整体预检两篇目标，避免只落一篇后才因路径冲突半途失败。
        for rel in (targets["evidence"], targets["distill"]):
            _target_owned_by(vault, rel, str(draft.get("draft_id") or ""))

        evidence = _ensure_generated_note(
            vault,
            targets["evidence"],
            draft_id=str(draft.get("draft_id") or ""),
            content=_render_evidence_note(draft),
            frontmatter=_note_frontmatter(
                draft, role="evidence", sources=[], approved_by=approved_by
            ),
            actor=actor,
        )
        # 证据笔记已确定路径后再渲染蒸馏笔记，保证其中的来源链接为 vault 相对路径。
        distill = _ensure_generated_note(
            vault,
            targets["distill"],
            draft_id=str(draft.get("draft_id") or ""),
            content=_render_distill_note(draft, targets["evidence"]),
            frontmatter=_note_frontmatter(
                draft,
                role="distill",
                sources=[targets["evidence"]],
                approved_by=approved_by,
            ),
            actor=actor,
        )

        draft.update(
            {
                "status": STATUS_APPROVED,
                "duplicate": False,
                "notes": [
                    {"role": "evidence", "path": evidence["path"]},
                    {"role": "distill", "path": distill["path"]},
                ],
                "note": "已批准：蒸馏笔记与最小证据笔记经 VaultService 创建",
            }
        )
        _save_draft(root, draft)
        return approved_result(draft, duplicate=False)
