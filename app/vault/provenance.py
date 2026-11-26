"""写入溯源与审阅状态（B0 独立模块）。

诚实边界：本模块只记录「经 Kwey 写路径写入的来源」，并提示磁盘上的外部变化；
不是 AI 文本检测器、作者鉴定或防篡改证据链。任何程序都能直接改 Markdown /
frontmatter，所以 frontmatter 中的 ``kwey_`` 字段只是可携带声明，必须与本机
``ProvenanceStore`` 基线核对后才可用于展示状态。

字段语义（``kwey_`` 命名空间，避免占用用户 ``source/title/reviewed_at``）：

===============  ==========================================  ==================
字段              值/用途                                      写入规则
===============  ==========================================  ==================
writer_kind       human / agent / system / unknown            宿主决定，不信任
                                                              模型传来的同名值
last_writer       local-ui、kwey-built-in、mcp-stdio、token   服务端 actor 生成；
                  名称、duty:inbox 等显示名                   仅用于显示，不是
                                                              身份密码
run_id            可选职责/会话批次                            内部传递；缺失则
                                                              为空，不制造假关联
generated         当前语义版本含 Agent 派生内容（保守标记）    Agent 正文/业务字段
                                                              写入置 true；人工
                                                              小改不自动清 false
reviewed_at       明确人工审阅时间（ISO 8601 带时区）         仅单独审阅动作设置
reviewed_hash     人工确认时的 payload SHA-256               与当前 payload 一致
                                                              才显示「当前已审」
hash_version      payload 序列化规则版本                       版本不符一律不算已审
===============  ==========================================  ==================

payload hash = 正文 + 业务 frontmatter（排除 ``created/updated`` 与所有
``kwey_`` 管理字段）的稳定序列化后取 SHA-256。序列化版本固定为
``PAYLOAD_SERIALIZATION_VERSION``；规则变化必须升版本，旧 hash 不得被误判为
当前已审。

五个派生审阅状态：``unknown`` / ``agent_unreviewed`` /
``agent_reviewed_current`` / ``human_or_system_current`` / ``external_changed``。
writer_kind、generated、reviewed 是三个独立维度：「最后是人保存」不等于
「整篇已验证」。

集成约定（B0 接线）：VaultService 写路径显式 actor 优先、其次本模块
ContextVar、均缺失才 unknown；registry.invoke 在 handler 调用期设置 ContextVar
并 try/finally 恢复，kernel/ops 显式传递内部 actor 与 run_id。ContextVar 不跨线程，
worker 必须显式传 actor。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import threading
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from app.parser import frontmatter as fm_mod
from app.paths import PathError, validate_relative

# ---------------------------------------------------------------------- 常量
MANAGED_PREFIX = "kwey_"
FIELD_WRITER_KIND = "kwey_writer_kind"
FIELD_LAST_WRITER = "kwey_last_writer"
FIELD_RUN_ID = "kwey_run_id"
FIELD_GENERATED = "kwey_generated"
FIELD_REVIEWED_AT = "kwey_reviewed_at"
FIELD_REVIEWED_HASH = "kwey_reviewed_hash"
FIELD_HASH_VERSION = "kwey_hash_version"

MANAGED_FIELDS = (
    FIELD_WRITER_KIND,
    FIELD_LAST_WRITER,
    FIELD_RUN_ID,
    FIELD_GENERATED,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWED_HASH,
    FIELD_HASH_VERSION,
)

#: 不参与 payload hash 的用户字段：写入路径自动刷新的时间戳。
NON_PAYLOAD_KEYS = frozenset({"created", "updated"})

#: payload 稳定序列化版本。任何规则变化都必须升版本。
PAYLOAD_SERIALIZATION_VERSION = 1
SUPPORTED_PAYLOAD_VERSIONS = frozenset({PAYLOAD_SERIALIZATION_VERSION})

VALID_WRITER_KINDS = frozenset({"human", "agent", "system", "unknown"})

STATE_UNKNOWN = "unknown"
STATE_AGENT_UNREVIEWED = "agent_unreviewed"
STATE_AGENT_REVIEWED_CURRENT = "agent_reviewed_current"
STATE_HUMAN_OR_SYSTEM_CURRENT = "human_or_system_current"
STATE_EXTERNAL_CHANGED = "external_changed"

REVIEW_STATES = (
    STATE_UNKNOWN,
    STATE_AGENT_UNREVIEWED,
    STATE_AGENT_REVIEWED_CURRENT,
    STATE_HUMAN_OR_SYSTEM_CURRENT,
    STATE_EXTERNAL_CHANGED,
)


# ---------------------------------------------------------------------- Actor
@dataclass(frozen=True)
class ActorContext:
    """一次写入的宿主身份。

    ``kind`` 是 writer_kind（human/agent/system/unknown），``role`` 是权限角色
    （如 admin/user），两者分离：admin 不等于 human。``name`` 只用于显示，
    不是可验证身份；模型自报的同名字段不得直接采信。
    """

    kind: str = "unknown"
    name: str = ""
    origin: str = ""
    run_id: Optional[str] = None
    role: str = ""

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        object.__setattr__(
            self, "kind", kind if kind in VALID_WRITER_KINDS else "unknown"
        )
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "origin", str(self.origin or "").strip())
        rid = str(self.run_id).strip() if self.run_id is not None else ""
        object.__setattr__(self, "run_id", rid or None)
        object.__setattr__(self, "role", str(self.role or "").strip())

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    @property
    def display_name(self) -> str:
        """稳定显示名：显式 name → origin → kind。"""
        return self.name or self.origin or self.kind


UNKNOWN_ACTOR = ActorContext()

#: 普通本机 UI 编辑的固定宿主身份。是本机应用约定，不是抵抗本机恶意调用的认证。
LOCAL_UI_ACTOR = ActorContext(kind="human", name="local-ui", origin="local-ui")

#: 服务内部写（如链接重写）的固定身份；不代表任何外部作者。
SYSTEM_ACTOR = ActorContext(kind="system", name="kwey-system", origin="system")

#: 当前写入身份。registry.invoke 在 handler 调用期设置，try/finally 恢复；
#: 跨线程不会自动传播，worker 必须显式传 actor。
_current_actor: ContextVar[Optional[ActorContext]] = ContextVar(
    "kwey_writer_actor", default=None
)


def get_current_actor() -> Optional[ActorContext]:
    """读取当前上下文里的宿主身份；没有则 None（不是 human）。"""
    return _current_actor.get()


@contextmanager
def use_actor(
    actor: Optional[ActorContext], *, run_id: Optional[str] = None
) -> Iterator[Optional[ActorContext]]:
    """在 with 块内绑定写入身份，退出时严格恢复到进入前的值。"""
    ctx = actor
    if ctx is not None and run_id:
        ctx = replace(ctx, run_id=run_id)
    token = _current_actor.set(ctx)
    try:
        yield ctx
    finally:
        _current_actor.reset(token)


def resolve_actor(actor: Optional[ActorContext] = None) -> ActorContext:
    """显式 actor → ContextVar → unknown。绝不默认 human。"""
    return actor or _current_actor.get() or UNKNOWN_ACTOR



def actor_fields(actor: Optional[ActorContext], *, generated: bool) -> Dict[str, Any]:
    """Actor + generated → 应写入 frontmatter 的 ``kwey_`` 管理字段。

    只产出写入时字段；``reviewed_at/hash`` 由单独审阅动作设置，普通保存不得
    伪装成审阅。
    """
    ctx = actor or UNKNOWN_ACTOR
    fields: Dict[str, Any] = {
        FIELD_WRITER_KIND: ctx.kind,
        FIELD_LAST_WRITER: ctx.display_name,
        FIELD_GENERATED: bool(generated) or ctx.is_agent,
        FIELD_HASH_VERSION: PAYLOAD_SERIALIZATION_VERSION,
    }
    if ctx.run_id:
        fields[FIELD_RUN_ID] = ctx.run_id
    return fields


def is_managed_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(MANAGED_PREFIX)


def business_frontmatter(metadata: Mapping[Any, Any]) -> Dict[Any, Any]:
    """业务 frontmatter：排除 created/updated 与 ``kwey_`` 管理字段。"""
    result: Dict[Any, Any] = {}
    for key, value in (metadata or {}).items():
        if isinstance(key, str) and (
            key in NON_PAYLOAD_KEYS or key.startswith(MANAGED_PREFIX)
        ):
            continue
        result[key] = value
    return result


def has_declared_fields(metadata: Mapping[Any, Any]) -> bool:
    return any(is_managed_key(k) for k in (metadata or {}))


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value) if value is not None else False


def _as_version(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_hash(value: Any) -> Optional[str]:
    text = _clean_text(value).lower()
    return text or None


def read_declared_fields(metadata: Mapping[Any, Any]) -> Dict[str, Any]:
    """读取 frontmatter 中的 ``kwey_`` 声明（未校验，不能直接当作事实）。"""
    meta = metadata or {}
    kind = _clean_text(meta.get(FIELD_WRITER_KIND)).lower()
    return {
        "writer_kind": kind if kind in VALID_WRITER_KINDS else STATE_UNKNOWN,
        "last_writer": _clean_text(meta.get(FIELD_LAST_WRITER)),
        "run_id": _clean_text(meta.get(FIELD_RUN_ID)) or None,
        "generated": _as_bool(meta.get(FIELD_GENERATED)),
        "reviewed_at": _clean_text(meta.get(FIELD_REVIEWED_AT)) or None,
        "reviewed_hash": _as_hash(meta.get(FIELD_REVIEWED_HASH)),
        "hash_version": _as_version(meta.get(FIELD_HASH_VERSION)),
    }


# ---------------------------------------------------------------------- 序列化
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _canonical_key(key: Any) -> str:
    """frontmatter key 的类型化字符串表示，避免 ``1`` 与 ``"1"`` 撞车。"""
    if isinstance(key, str):
        return "s:" + _nfc(key)
    if isinstance(key, bool):
        return "b:" + ("true" if key else "false")
    if isinstance(key, int):
        return "i:" + str(key)
    if isinstance(key, float):
        return "f:" + repr(key)
    if isinstance(key, datetime):
        return "t:datetime:" + key.isoformat()
    if isinstance(key, date):
        return "t:date:" + key.isoformat()
    if isinstance(key, time):
        return "t:time:" + key.isoformat()
    return "r:" + _nfc(f"{type(key).__module__}.{type(key).__qualname__}:{key!r}")


def _canonical_value(value: Any) -> Any:
    """把 YAML 值转成稳定、可 JSON 序列化的规范形态。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {_canonical_key(k): _canonical_value(v) for k, v in value.items()}
    return {
        "$repr": _nfc(
            f"{type(value).__module__}.{type(value).__qualname__}:{value!r}"
        )
    }


def canonical_body(body: str) -> str:
    """正文规范化：统一换行、NFC、忽略文件末尾多余空行，不裁剪行内空格。"""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return _nfc(text).rstrip("\n")


def canonical_payload(
    raw: str, *, version: int = PAYLOAD_SERIALIZATION_VERSION
) -> str:
    """(frontmatter, 正文) → 固定版本的稳定 JSON 文本。

    不支持的版本直接抛 ``ValueError``，防止旧规则 hash 被当成新规则有效。
    """
    if version not in SUPPORTED_PAYLOAD_VERSIONS:
        raise ValueError(f"不支持的 payload 序列化版本: {version!r}")
    metadata, body = fm_mod.parse_frontmatter(raw or "")
    envelope = {
        "v": version,
        "body": canonical_body(body),
        "frontmatter": _canonical_value(business_frontmatter(metadata)),
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(raw: str, *, version: int = PAYLOAD_SERIALIZATION_VERSION) -> str:
    """正文 + 业务 frontmatter 的 payload SHA-256（64 位小写十六进制）。"""
    data = canonical_payload(raw, version=version).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def raw_hash(raw: str) -> str:
    """磁盘原始文本的 SHA-256，用于外部变化检测（不做规范化）。"""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def raw_bytes_hash(data: bytes) -> str:
    """磁盘原始字节的 SHA-256（纯文本/二进制写回用，避免编码往返造成误判）。"""
    return hashlib.sha256(data or b"").hexdigest()


# ---------------------------------------------------------------------- 派生状态
def _entry_kind(entry: Mapping[str, Any]) -> str:
    kind = _clean_text(entry.get("writer_kind")).lower()
    return kind if kind in VALID_WRITER_KINDS else STATE_UNKNOWN


def derive_review_state(
    raw: str,
    baseline: Optional[Mapping[str, Any]],
) -> str:
    """按「本机基线 + 当前磁盘文本」推导五个审阅状态之一。

    - 无基线 / 基线版本不支持 / 来源身份未知 → ``unknown``；
    - 原始 hash 或 payload hash 与基线不符 → ``external_changed``；
    - generated 或 agent 写入 → 已审 hash 与当前 payload、版本都一致才算
      ``agent_reviewed_current``，否则 ``agent_unreviewed``；
    - 人/系统写入且无 Agent 派生标记 → ``human_or_system_current``。
    """
    if not baseline:
        return STATE_UNKNOWN

    version = _as_version(baseline.get("payload_version"))
    if version is None or version not in SUPPORTED_PAYLOAD_VERSIONS:
        return STATE_UNKNOWN

    current_raw = raw_hash(raw)
    current_payload = payload_hash(raw, version=version)
    expected_raw = _as_hash(baseline.get("raw_hash"))
    if expected_raw is not None and current_raw != expected_raw:
        return STATE_EXTERNAL_CHANGED
    expected_payload = _as_hash(baseline.get("payload_hash"))
    if expected_payload is not None and current_payload != expected_payload:
        return STATE_EXTERNAL_CHANGED

    kind = _entry_kind(baseline)
    if kind == STATE_UNKNOWN:
        return STATE_UNKNOWN

    generated = _as_bool(baseline.get("generated"))
    if kind == "agent" or generated:
        reviewed_hash = _as_hash(baseline.get("reviewed_hash"))
        reviewed_version = _as_version(baseline.get("reviewed_version"))
        if (
            reviewed_hash is not None
            and reviewed_hash == current_payload
            and reviewed_version == version
        ):
            return STATE_AGENT_REVIEWED_CURRENT
        return STATE_AGENT_UNREVIEWED

    return STATE_HUMAN_OR_SYSTEM_CURRENT


def describe_note(
    store: "ProvenanceStore",
    path: str,
    raw: str,
) -> Dict[str, Any]:
    """汇总单篇笔记的溯源视图（供后续 read 路由直接复用）。

    ``claim_unverified`` 表示没有本机基线、但 frontmatter 携带 ``kwey_`` 声明：
    此时状态仍为 ``unknown``，声明只能展示为「未校验」，不能冒充 human。
    """
    rel = validate_relative(path)
    baseline = store.get_baseline(rel)
    metadata, _body = fm_mod.parse_frontmatter(raw or "")
    declared = read_declared_fields(metadata)
    state = derive_review_state(raw, baseline)
    return {
        "path": rel,
        "state": state,
        "baseline": baseline,
        "declared": declared,
        "claim_unverified": baseline is None and has_declared_fields(metadata),
        "current_raw_hash": raw_hash(raw),
        "current_payload_hash": payload_hash(raw),
        "payload_version": PAYLOAD_SERIALIZATION_VERSION,
    }


# ---------------------------------------------------------------------- 存储
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ProvenanceStore:
    """``.kwey/provenance.json``：本机基线与有限事件保留。

    - 原子保存：临时文件 + ``os.replace``；
    - 基线按插入顺序有限保留，超限淘汰最旧项（被删基线自然降级 unknown）；
    - 事件只记 actor/path/hash/时间/动作，不保存正文与 provider prompt；
    - 线程内用 RLock 串行化，每次写入后立即落盘；
    - 同库多进程（桌面 UI + MCP stdio）共享此文件：读前按 mtime/size 检测外部
      改写并重读，避免把另一进程写入的 Agent 基线当成「来源未知」或覆盖掉。
    """

    STORE_VERSION = 1
    FILENAME = "provenance.json"
    DEFAULT_MAX_BASELINES = 2000
    DEFAULT_MAX_EVENTS = 200

    def __init__(
        self,
        root: Path,
        *,
        filename: str = FILENAME,
        max_baselines: int = DEFAULT_MAX_BASELINES,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / ".kwey" / filename
        self.max_baselines = max(1, int(max_baselines))
        self.max_events = max(0, int(max_events))
        self._lock = threading.RLock()
        self._data: Optional[Dict[str, Any]] = None
        self._sig: Optional[tuple] = None

    # ------------------------------------------------------------- 加载 / 保存
    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {
            "version": ProvenanceStore.STORE_VERSION,
            "baselines": {},
            "events": [],
        }

    def _file_sig(self) -> Optional[tuple]:
        """磁盘签名 (mtime_ns, size)；文件不存在返回 None。"""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load_locked(self) -> Dict[str, Any]:
        sig = self._file_sig()
        if self._data is not None and sig == self._sig:
            return self._data
        data = self._empty()
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                raw = None
            if (
                isinstance(raw, dict)
                and raw.get("version") == self.STORE_VERSION
                and isinstance(raw.get("baselines"), dict)
            ):
                data["baselines"] = raw["baselines"]
                events = raw.get("events")
                data["events"] = events if isinstance(events, list) else []
        self._data = data
        self._sig = sig
        return data

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        self._sig = self._file_sig()

    def reload(self) -> None:
        """丢弃内存态并从磁盘重读（切库/外部清理后调用）。"""
        with self._lock:
            self._data = None
            self._sig = None
            self._load_locked()

    # ------------------------------------------------------------- 工具
    @staticmethod
    def _normalize_path(path: str) -> str:
        try:
            return validate_relative(path)
        except PathError as exc:
            raise ValueError(str(exc)) from exc

    def _append_event_locked(self, entry: Mapping[str, Any]) -> None:
        if self.max_events <= 0:
            return
        self._data["events"].append(dict(entry))
        overflow = len(self._data["events"]) - self.max_events
        if overflow > 0:
            del self._data["events"][:overflow]

    def _evict_baselines_locked(self) -> None:
        baselines: Dict[str, Any] = self._data["baselines"]
        while len(baselines) > self.max_baselines:
            oldest = next(iter(baselines))
            baselines.pop(oldest, None)

    # ------------------------------------------------------------- 读
    @property
    def baseline_count(self) -> int:
        with self._lock:
            return len(self._load_locked()["baselines"])

    def get_baseline(self, path: str) -> Optional[Dict[str, Any]]:
        rel = self._normalize_path(path)
        with self._lock:
            entry = self._load_locked()["baselines"].get(rel)
            return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """最近事件（最新在后）；只返回副本。"""
        size = max(0, int(limit))
        with self._lock:
            events = self._load_locked()["events"]
            selected = events[-size:] if size else []
            return copy.deepcopy(selected)

    # ------------------------------------------------------------- 写
    def record_write(
        self,
        path: str,
        raw: str,
        actor: Optional[ActorContext] = None,
        *,
        action: str = "write",
        generated: bool = False,
    ) -> Dict[str, Any]:
        """记录一次服务端写入后的最后已知基线，返回新基线副本。

        ``generated`` 具粘性：一旦为 Agent 派生，后续人工小改不会洗回 false；
        审阅字段随基线保留，但内容/版本变化会让旧 reviewed hash 自然失效。
        """
        rel = self._normalize_path(path)
        ctx = actor or UNKNOWN_ACTOR
        with self._lock:
            data = self._load_locked()
            previous = data["baselines"].get(rel)
            previous = previous if isinstance(previous, dict) else {}

            generated_value = (
                bool(generated)
                or ctx.is_agent
                or _as_bool(previous.get("generated"))
            )
            version = PAYLOAD_SERIALIZATION_VERSION
            entry: Dict[str, Any] = {
                "path": rel,
                "raw_hash": raw_hash(raw),
                "payload_hash": payload_hash(raw, version=version),
                "payload_version": version,
                "writer_kind": ctx.kind,
                "last_writer": ctx.display_name,
                "origin": ctx.origin,
                "run_id": ctx.run_id,
                "generated": generated_value,
                "recorded_at": _now_iso(),
                "action": action,
                "reviewed_at": previous.get("reviewed_at"),
                "reviewed_hash": previous.get("reviewed_hash"),
                "reviewed_version": previous.get("reviewed_version"),
            }
            data["baselines"][rel] = entry
            self._append_event_locked({
                "action": action,
                "path": rel,
                "at": entry["recorded_at"],
                "raw_hash": entry["raw_hash"],
                "payload_hash": entry["payload_hash"],
                "writer_kind": ctx.kind,
                "last_writer": ctx.display_name,
                "run_id": ctx.run_id,
            })
            self._evict_baselines_locked()
            self._save_locked()
            return copy.deepcopy(entry)

    def record_write_bytes(
        self,
        path: str,
        data: bytes,
        actor: Optional[ActorContext] = None,
        *,
        action: str = "write_text",
        generated: bool = False,
    ) -> Dict[str, Any]:
        """纯文本/二进制写回的基线：raw 与 payload 都用字节 SHA-256。

        非 Markdown 文件没有 frontmatter 语义，按字节比对能精确识别「外部改过」，
        也不会因为 UTF-8/GBK 解码往返被误判为变化。
        """
        rel = self._normalize_path(path)
        ctx = actor or UNKNOWN_ACTOR
        with self._lock:
            data_store = self._load_locked()
            previous = data_store["baselines"].get(rel)
            previous = previous if isinstance(previous, dict) else {}
            generated_value = (
                bool(generated) or ctx.is_agent or _as_bool(previous.get("generated"))
            )
            digest = raw_bytes_hash(data)
            version = PAYLOAD_SERIALIZATION_VERSION
            entry: Dict[str, Any] = {
                "path": rel,
                "raw_hash": digest,
                "payload_hash": digest,
                "payload_version": version,
                "hash_kind": "bytes",
                "writer_kind": ctx.kind,
                "last_writer": ctx.display_name,
                "origin": ctx.origin,
                "run_id": ctx.run_id,
                "generated": generated_value,
                "recorded_at": _now_iso(),
                "action": action,
                "reviewed_at": None,
                "reviewed_hash": None,
                "reviewed_version": None,
            }
            data_store["baselines"][rel] = entry
            self._append_event_locked({
                "action": action,
                "path": rel,
                "at": entry["recorded_at"],
                "raw_hash": digest,
                "payload_hash": digest,
                "writer_kind": ctx.kind,
                "last_writer": ctx.display_name,
                "run_id": ctx.run_id,
            })
            self._evict_baselines_locked()
            self._save_locked()
            return copy.deepcopy(entry)

    def record_review(
        self,
        path: str,
        *,
        reviewed_hash: str,
        payload_version: Optional[int] = None,
        reviewed_at: Optional[str] = None,
        raw: Optional[str] = None,
    ) -> bool:
        """把人工确认的 payload hash 写入基线；无基线或版本不支持返回 False。

        ``raw`` 为审阅写盘后的完整文本（含新增的 ``kwey_reviewed_*`` 字段）：
        提供时同步刷新基线的 raw/payload hash，避免审阅自身写盘被误判为外部变化。
        写入者字段（writer_kind/last_writer/run_id/generated）一律保持不变。
        """
        rel = self._normalize_path(path)
        version = (
            PAYLOAD_SERIALIZATION_VERSION
            if payload_version is None
            else _as_version(payload_version)
        )
        if version not in SUPPORTED_PAYLOAD_VERSIONS:
            raise ValueError(f"不支持的 payload 序列化版本: {payload_version!r}")
        digest = _as_hash(reviewed_hash)
        if digest is None:
            raise ValueError("reviewed_hash 不能为空")
        with self._lock:
            data = self._load_locked()
            entry = data["baselines"].get(rel)
            if not isinstance(entry, dict):
                return False
            entry["reviewed_at"] = reviewed_at or _now_iso()
            entry["reviewed_hash"] = digest
            entry["reviewed_version"] = version
            if raw is not None:
                entry["payload_version"] = version
                entry["raw_hash"] = raw_hash(raw)
                entry["payload_hash"] = payload_hash(raw, version=version)
            self._append_event_locked({
                "action": "review",
                "path": rel,
                "at": entry["reviewed_at"],
                "raw_hash": entry.get("raw_hash"),
                "payload_hash": digest,
                "writer_kind": entry.get("writer_kind"),
                "last_writer": entry.get("last_writer"),
                "run_id": entry.get("run_id"),
            })
            self._save_locked()
            return True

    def delete_baseline(self, path: str) -> bool:
        """删除基线；之后该笔记的派生状态一律降级 ``unknown``（不是 human）。"""
        rel = self._normalize_path(path)
        with self._lock:
            data = self._load_locked()
            if rel not in data["baselines"]:
                return False
            data["baselines"].pop(rel, None)
            self._append_event_locked({
                "action": "delete_baseline",
                "path": rel,
                "at": _now_iso(),
            })
            self._save_locked()
            return True

    def detect_external(self, path: str, raw: str) -> bool:
        """有基线且当前原始 hash 不同 → 外部/绕写变化。"""
        baseline = self.get_baseline(path)
        if not baseline:
            return False
        expected = _as_hash(baseline.get("raw_hash"))
        return expected is not None and expected != raw_hash(raw)

    def record_external(self, path: str, raw: str) -> bool:
        """只记一条外部变化事件，不覆盖基线（状态继续保持 external_changed）。"""
        rel = self._normalize_path(path)
        with self._lock:
            entry = self._load_locked()["baselines"].get(rel)
            if not isinstance(entry, dict):
                return False
            expected = _as_hash(entry.get("raw_hash"))
            current = raw_hash(raw)
            if expected is None or expected == current:
                return False
            self._append_event_locked({
                "action": "external_changed",
                "path": rel,
                "at": _now_iso(),
                "raw_hash": current,
                "payload_hash": payload_hash(raw),
                "writer_kind": "unknown",
                "last_writer": "",
                "run_id": None,
            })
            self._save_locked()
            return True


__all__ = [
    "ActorContext",
    "UNKNOWN_ACTOR",
    "LOCAL_UI_ACTOR",
    "SYSTEM_ACTOR",
    "get_current_actor",
    "use_actor",
    "resolve_actor",
    "actor_fields",
    "is_managed_key",
    "business_frontmatter",
    "has_declared_fields",
    "read_declared_fields",
    "canonical_body",
    "canonical_payload",
    "payload_hash",
    "raw_hash",
    "derive_review_state",
    "describe_note",
    "ProvenanceStore",
    "PAYLOAD_SERIALIZATION_VERSION",
    "SUPPORTED_PAYLOAD_VERSIONS",
    "VALID_WRITER_KINDS",
    "MANAGED_FIELDS",
    "NON_PAYLOAD_KEYS",
    "STATE_UNKNOWN",
    "STATE_AGENT_UNREVIEWED",
    "STATE_AGENT_REVIEWED_CURRENT",
    "STATE_HUMAN_OR_SYSTEM_CURRENT",
    "STATE_EXTERNAL_CHANGED",
    "REVIEW_STATES",
]
