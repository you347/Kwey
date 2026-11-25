"""值班策略：范围、预算、输入哈希、幂等键与调度判定（纯函数，无 IO）。

所有函数都不读全局 ``deps``、不写盘；调用方（runtime / API / store）负责
注入时钟与时区。路径一律 vault 相对正斜杠，复用 :mod:`app.paths` 的校验。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.paths import PathError, normalize_rel, validate_relative

from app.duty.recipes import (
    DEFAULT_LIMITS,
    LIMIT_CEILINGS,
    MAX_FILES_PER_PROPOSAL,
    MAX_OPS_PER_RUN,
    MAX_RUNS_PER_HOUR,
    RECIPE_VERSION,
    DutyDefinition,
    DutyRecipe,
    default_scope_for,
    recipe_for,
)


class DutyPolicyError(ValueError):
    """职责策略校验失败（范围 / 预算 / ops 契约）。"""


class ScopeError(DutyPolicyError):
    """目录范围非法或超出职责允许范围。"""


class BudgetExceeded(DutyPolicyError):
    """单轮 / 单提案预算超限。"""


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DUTY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SPLIT_RE = re.compile(r"[,，\n]")


# --------------------------------------------------------------------- 范围
def normalize_scope(
    value: Any,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    """规范化范围目录列表：拒绝绝对路径、目录穿越、保留目录与盘符。"""
    if value is None:
        parts: List[Any] = []
    elif isinstance(value, str):
        parts = _SPLIT_RE.split(value)
    else:
        parts = list(value)

    result: List[str] = []
    for raw in parts:
        raw_text = str(raw or "").strip()
        # normalize_rel 会吞掉前导斜杠，绝对路径 / 盘符必须先拒绝。
        if raw_text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw_text):
            raise ScopeError(f"范围必须是 vault 相对目录: {raw!r}")
        text = normalize_rel(raw_text)
        if not text:
            continue
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise ScopeError(f"范围必须是 vault 相对目录: {raw!r}")
        try:
            clean = validate_relative(text)
        except PathError as exc:
            raise ScopeError(f"范围目录非法: {raw!r}（{exc}）") from exc
        if clean not in result:
            result.append(clean)

    if not result and not allow_empty:
        raise ScopeError("范围不能为空")
    return tuple(result)


def in_scope(path: str, scope: Sequence[str]) -> bool:
    """路径是否落在任一范围目录内；空范围表示全库。"""
    rel = normalize_rel(path)
    if not scope:
        return True
    return any(rel == root or rel.startswith(root + "/") for root in scope)


def _within_any(path: str, roots: Sequence[str]) -> bool:
    if not roots:
        return False
    return any(path == root or path.startswith(root + "/") for root in roots)


def _normalize_dir(value: Any, label: str) -> str:
    text = normalize_rel(str(value or ""))
    if not text:
        raise DutyPolicyError(f"{label}不能为空")
    try:
        return validate_relative(text)
    except PathError as exc:
        raise DutyPolicyError(f"{label}非法: {value!r}（{exc}）") from exc


# --------------------------------------------------------------------- 定义
def normalize_schedule(recipe_id: str, raw: Any) -> Dict[str, Any]:
    """按 recipe 固定 kind 校验时间 / 星期；允许用户调整时刻。"""
    recipe = recipe_for(recipe_id)
    default = dict(recipe.default_schedule)
    if raw is None:
        return default
    if not isinstance(raw, Mapping):
        raise DutyPolicyError("schedule 必须是对象")

    allowed = {"kind", "time"}
    if default["kind"] == "weekly":
        allowed.add("weekday")
    unknown = set(raw) - allowed
    if unknown:
        raise DutyPolicyError(f"schedule 含未知字段: {sorted(unknown)}")

    merged = dict(default)
    for key, value in raw.items():
        if value is not None:
            merged[key] = value

    if merged.get("kind") != default["kind"]:
        raise DutyPolicyError(
            f"职责 {recipe_id} 的调度类型固定为 {default['kind']}"
        )
    time_text = str(merged.get("time") or "")
    if not _TIME_RE.match(time_text):
        raise DutyPolicyError(f"schedule.time 必须是 HH:MM，收到 {time_text!r}")

    if default["kind"] == "weekly":
        try:
            weekday = int(merged.get("weekday", default.get("weekday", 6)))
        except (TypeError, ValueError) as exc:
            raise DutyPolicyError("schedule.weekday 必须是 0-6 的整数") from exc
        if weekday < 0 or weekday > 6:
            raise DutyPolicyError("schedule.weekday 必须是 0-6 的整数")
        merged["weekday"] = weekday
    return merged


def normalize_limits(recipe_id: str, raw: Any) -> Dict[str, int]:
    """合并默认预算；用户只能调小，不能超过硬上限或写未知字段。"""
    recipe_for(recipe_id)
    merged = dict(DEFAULT_LIMITS)
    if raw is None:
        return merged
    if not isinstance(raw, Mapping):
        raise DutyPolicyError("limits 必须是对象")

    for key, value in raw.items():
        if key not in DEFAULT_LIMITS:
            raise DutyPolicyError(f"limits 含未知字段: {key!r}")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise DutyPolicyError(f"limits.{key} 必须是整数") from exc
        if number < 1 or number > LIMIT_CEILINGS[key]:
            raise DutyPolicyError(
                f"limits.{key} 必须在 1~{LIMIT_CEILINGS[key]} 之间，收到 {number}"
            )
        merged[key] = number
    return merged


def normalize_definition(
    recipe_id: str,
    *,
    duty_id: Optional[str] = None,
    enabled: bool = False,
    scope: Any = None,
    schedule: Any = None,
    limits: Any = None,
    config_version: int = 1,
    inbox_dir: str = "inbox",
) -> DutyDefinition:
    """构造合法职责定义；所有非法输入在落库前抛 ``DutyPolicyError``。"""
    recipe = recipe_for(recipe_id)
    raw_scope = default_scope_for(recipe_id, inbox_dir) if scope is None else scope
    scope_norm = normalize_scope(raw_scope, allow_empty=not recipe.requires_scope)

    if recipe.id == "inbox_duty":
        base = normalize_scope(inbox_dir, allow_empty=False)[0]
        if not scope_norm:
            scope_norm = (base,)
        for item in scope_norm:
            if not (item == base or item.startswith(base + "/")):
                raise ScopeError(
                    f"收件箱职责范围必须在 {base}/ 内，收到 {item!r}"
                )

    final_id = str(duty_id or recipe.id)
    if not _DUTY_ID_RE.match(final_id):
        raise DutyPolicyError(f"duty_id 非法: {final_id!r}")

    try:
        version = int(config_version)
    except (TypeError, ValueError) as exc:
        raise DutyPolicyError("config_version 必须是正整数") from exc
    if version < 1:
        raise DutyPolicyError("config_version 必须是正整数")

    return DutyDefinition(
        duty_id=final_id,
        recipe=recipe.id,
        enabled=bool(enabled),
        scope=scope_norm,
        schedule=normalize_schedule(recipe.id, schedule),
        limits=normalize_limits(recipe.id, limits),
        config_version=version,
    )


# --------------------------------------------------------------------- 预算
@dataclass
class RunBudget:
    """单轮预算计数器；超限抛 ``BudgetExceeded``，不静默截断。"""

    max_ops: int
    max_files: int
    max_note_chars: int
    max_model_calls: int
    max_model_input_chars: int
    ops: int = 0
    files: int = 0
    model_calls: int = 0
    model_input_chars: int = 0

    def charge_ops(self, count: int = 1) -> None:
        self.ops += int(count)
        if self.ops > self.max_ops:
            raise BudgetExceeded(f"单轮 ops 超过上限 {self.max_ops}")

    def charge_files(self, count: int) -> None:
        self.files = max(self.files, int(count))
        if self.files > self.max_files:
            raise BudgetExceeded(f"单提案影响文件超过上限 {self.max_files}")

    def check_note_chars(self, count: int) -> bool:
        """单文件正文是否在读取上限内；超限只报告，不读取全文。"""
        return int(count) <= self.max_note_chars

    def charge_model(self, input_chars: int) -> None:
        """记一次模型调用；调用次数或输入字符超限时拒绝。"""
        chars = int(input_chars)
        if self.model_calls + 1 > self.max_model_calls:
            raise BudgetExceeded(
                f"单轮模型调用超过上限 {self.max_model_calls}"
            )
        if self.model_input_chars + chars > self.max_model_input_chars:
            raise BudgetExceeded(
                f"单轮模型输入超过上限 {self.max_model_input_chars} 字符"
            )
        self.model_calls += 1
        self.model_input_chars += chars

    def as_dict(self) -> Dict[str, int]:
        return {
            "max_ops": self.max_ops,
            "max_files": self.max_files,
            "max_note_chars": self.max_note_chars,
            "max_model_calls": self.max_model_calls,
            "max_model_input_chars": self.max_model_input_chars,
            "ops": self.ops,
            "files": self.files,
            "model_calls": self.model_calls,
            "model_input_chars": self.model_input_chars,
        }


def run_budget(definition: DutyDefinition | Mapping[str, Any]) -> RunBudget:
    limits = definition["limits"] if isinstance(definition, Mapping) else definition.limits
    return RunBudget(
        max_ops=int(limits["max_ops"]),
        max_files=int(limits["max_files"]),
        max_note_chars=int(limits["max_note_chars"]),
        max_model_calls=int(limits["max_model_calls"]),
        max_model_input_chars=int(limits["max_model_input_chars"]),
    )


# --------------------------------------------------------------------- ops 校验
def validate_ops_for_recipe(
    ops: Any,
    *,
    recipe: str,
    scope: Sequence[str],
    output_dirs: Sequence[str] = (),
    allowed_move_dirs: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """按固定职责白名单校验 ops；返回规范化副本，非法即抛异常。

    - 只允许 recipe 声明的写动作（MVP 无 delete / append / 覆盖正文）。
    - tag/move 的源路径必须在职责范围；move 目标必须是已有允许目录。
    - create 的输出路径必须落在明确输出目录内。
    - 影响文件数不得超过单提案上限。
    """
    spec: DutyRecipe = recipe_for(recipe)
    if not isinstance(ops, list):
        raise DutyPolicyError("ops 必须是数组")
    if len(ops) > MAX_OPS_PER_RUN:
        raise BudgetExceeded(f"单轮 ops 超过上限 {MAX_OPS_PER_RUN}")

    outputs = tuple(_normalize_dir(d, "输出目录") for d in output_dirs)
    move_dirs = {_normalize_dir(d, "可移动目录") for d in allowed_move_dirs}

    normalized: List[Dict[str, Any]] = []
    affected: set = set()

    for index, raw in enumerate(ops):
        where = f"第 {index + 1} 项 op"
        if not isinstance(raw, Mapping):
            raise DutyPolicyError(f"{where} 必须是对象")
        op_type = raw.get("type") or raw.get("op")
        if op_type not in spec.op_types:
            raise DutyPolicyError(
                f"{where} 的动作 {op_type!r} 不在职责 {recipe} 白名单 "
                f"{list(spec.op_types)} 内"
            )
        path = _normalize_dir(raw.get("path"), f"{where}.path")
        op: Dict[str, Any] = dict(raw)
        op["type"] = op_type
        op["path"] = path

        if op_type == "create":
            if not _within_any(path, outputs):
                raise ScopeError(f"{where} 的输出路径不在允许目录 {list(outputs)} 内")
        else:
            if not in_scope(path, scope):
                raise ScopeError(f"{where} 的路径超出职责范围: {path}")
            affected.add(path)

        if op_type == "tag":
            tag = str(raw.get("tag") or "").strip().lstrip("#")
            if not tag or "\n" in tag or "\r" in tag:
                raise DutyPolicyError(f"{where} 的 tag 必须是非空单行文本")
            op["tag"] = tag
        elif op_type == "move":
            target = _normalize_dir(raw.get("target"), f"{where}.target")
            if target not in move_dirs:
                raise ScopeError(f"{where} 的移动目标不是已有允许目录: {target}")
            op["target"] = target
            basename = path.rsplit("/", 1)[-1]
            affected.add(f"{target}/{basename}")

        normalized.append(op)

    # create 的写入路径也要计入影响文件数。
    for op in normalized:
        if op["type"] == "create":
            affected.add(op["path"])
    if len(affected) > MAX_FILES_PER_PROPOSAL:
        raise BudgetExceeded(
            f"单提案影响文件 {len(affected)} 个，超过上限 {MAX_FILES_PER_PROPOSAL}"
        )
    return normalized


# --------------------------------------------------------------------- 哈希 / 幂等
def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_input_hash(
    *,
    recipe: str,
    config_version: int,
    candidates: Iterable[Any],
    recipe_version: str = RECIPE_VERSION,
) -> str:
    """输入哈希 = recipe 版本 + 配置版本 + 候选路径/内容摘要（稳定排序）。

    候选可以是 :class:`app.duty.candidates.Candidate` 或含同名键的 dict。
    """
    items: List[Dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            path = str(candidate.get("path") or "")
            digest = str(candidate.get("digest") or "")
            mtime = candidate.get("mtime")
            facts = candidate.get("facts") or {}
        else:
            path = str(getattr(candidate, "path", "") or "")
            digest = str(getattr(candidate, "digest", "") or "")
            mtime = getattr(candidate, "mtime", None)
            facts = getattr(candidate, "facts", {}) or {}
        key = path or f"issue:{facts.get('issue', '')}"
        items.append(
            {
                "k": key,
                "p": path,
                "h": digest,
                "m": round(float(mtime), 3) if mtime is not None else None,
            }
        )
    items.sort(key=lambda item: (item["k"], item["p"], item["h"]))
    payload = {
        "recipe": recipe,
        "recipe_version": recipe_version,
        "config_version": int(config_version),
        "items": items,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def idempotency_key(
    duty_id: str,
    config_version: int,
    input_hash: str,
    slot: str,
) -> str:
    """同一职责 + 配置版本 + 输入哈希 + 日期桶构成幂等键。"""
    raw = f"{duty_id}|{int(config_version)}|{input_hash}|{slot}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_proposal_hash(
    *,
    ops: Sequence[Mapping[str, Any]],
    manifest: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
    reason: str,
) -> str:
    """提案哈希覆盖 ops / manifest / 来源 / 理由，批准时用于固定版本。"""
    payload = {
        "ops": [dict(op) for op in ops],
        "manifest": [dict(entry) for entry in manifest],
        "sources": list(sources),
        "reason": reason,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def dedup_should_skip(
    prior_run: Optional[Mapping[str, Any]],
    prior_proposal: Optional[Mapping[str, Any]] = None,
    decision: Optional[Mapping[str, Any]] = None,
) -> bool:
    """相同输入是否应静默跳过（拒绝后不再次提醒，失败则允许重试）。"""
    if not prior_run:
        return False
    status = str(prior_run.get("status") or "")
    if status in {"queued", "running", "awaiting_review"}:
        return True
    if prior_proposal:
        p_status = str(prior_proposal.get("status") or "")
        if p_status in {"pending", "applying", "applied", "partial", "uncertain"}:
            return True
    if decision and str(decision.get("decision") or "") == "reject":
        return True
    return False


def rate_limit_exceeded(
    started_at: Sequence[float],
    now_ts: float,
    *,
    limit: int = MAX_RUNS_PER_HOUR,
    window: float = 3600.0,
) -> bool:
    """窗口内已开始的轮次是否达到上限。"""
    return sum(1 for ts in started_at if now_ts - float(ts) < window) >= limit


# --------------------------------------------------------------------- 调度
def local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def slot_key(recipe: str, now_ts: float, tz: Optional[tzinfo] = None) -> str:
    """幂等日期桶：日报按日，周报按 ISO 周，收件箱按日。"""
    spec = recipe_for(recipe)
    moment = datetime.fromtimestamp(float(now_ts), tz or local_tz())
    if spec.default_schedule.get("kind") == "weekly":
        return moment.strftime("%G-W%V")
    return moment.strftime("%Y-%m-%d")


def last_due_slot(
    schedule: Mapping[str, Any],
    now_ts: float,
    *,
    tz: Optional[tzinfo] = None,
    max_age: float = 24 * 3600,
) -> Optional[float]:
    """最近一个已到点且未超过 ``max_age`` 的定时槽。

    启动补跑只合并最近 24 小时内最后一个未完成槽；更早的错过不补。
    """
    spec_kind = str(schedule.get("kind") or "daily")
    hour, minute = (int(part) for part in str(schedule.get("time") or "18:00").split(":"))
    moment = datetime.fromtimestamp(float(now_ts), tz or local_tz())

    if spec_kind == "weekly":
        weekday = int(schedule.get("weekday", 6))
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate -= timedelta(days=(moment.weekday() - weekday) % 7)
        if candidate.timestamp() > now_ts:
            candidate -= timedelta(days=7)
    else:
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() > now_ts:
            candidate -= timedelta(days=1)

    if now_ts - candidate.timestamp() > max_age:
        return None
    return candidate.timestamp()
