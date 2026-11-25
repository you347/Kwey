"""固定职责定义（A0 值班 Agent）。

本模块只描述**内置固定职责**的 schema 与硬上限，不包含任何调度、IO 或
全局依赖（``app.api.deps``）。职责范围不是给模型的 prompt 建议，而是宿主
读取 / 生成 / 执行的硬约束：所有候选、ops、输出路径都要经
:mod:`app.duty.policy` 校验。

MVP 仅三条 recipe：收件箱值班、每日知识简报、每周健康巡检；不允许脚本、
shell、任意 URL、任意插件或任意模型工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

# recipe 语义版本：职责规则变化时必须递增，否则旧输入哈希会被误当作同一输入。
RECIPE_VERSION = "1"

# 单轮 / 单提案硬上限（见 INNOVATION-PLAN §4.6）。
MAX_OPS_PER_RUN = 10
MAX_FILES_PER_PROPOSAL = 20
MAX_NOTE_CHARS = 50_000
MAX_MODEL_CALLS_PER_RUN = 3
MAX_MODEL_INPUT_CHARS = 24_000
MAX_QUEUE = 20
MAX_RUNS_PER_HOUR = 2

# 待审提案默认 7 天过期。
PROPOSAL_TTL_SECONDS = 7 * 24 * 3600

# 每职责默认预算；用户只能调小，不能超过 LIMIT_CEILINGS。
DEFAULT_LIMITS: Dict[str, int] = {
    "max_ops": MAX_OPS_PER_RUN,
    "max_files": MAX_FILES_PER_PROPOSAL,
    "max_note_chars": MAX_NOTE_CHARS,
    "max_model_calls": MAX_MODEL_CALLS_PER_RUN,
    "max_model_input_chars": MAX_MODEL_INPUT_CHARS,
}

LIMIT_CEILINGS: Dict[str, int] = dict(DEFAULT_LIMITS)


class DutyRecipeError(ValueError):
    """未知 / 非法职责定义。"""


@dataclass(frozen=True)
class DutyRecipe:
    """单条内置职责的固定 schema。"""

    id: str
    title: str
    description: str
    candidate_limit: int
    op_types: Tuple[str, ...]
    default_schedule: Mapping[str, Any]
    default_scope: Tuple[str, ...]
    requires_scope: bool = False
    output_kind: str = ""  # daily | report | ""（由 runtime 决定输出目录）


RECIPES: Dict[str, DutyRecipe] = {
    "inbox_duty": DutyRecipe(
        id="inbox_duty",
        title="收件箱值班",
        description="扫描配置的收件箱目录，只建议补标签与移动到已有目录。",
        candidate_limit=10,
        op_types=("tag", "move"),
        default_schedule={"kind": "daily", "time": "09:00"},
        default_scope=("inbox",),
        requires_scope=True,
    ),
    "daily_brief": DutyRecipe(
        id="daily_brief",
        title="每日知识简报",
        description="汇总最近 24 小时的笔记变化与未完成待办，生成 daily 简报提案。",
        candidate_limit=20,
        op_types=("create",),
        default_schedule={"kind": "daily", "time": "18:00"},
        default_scope=(),
        output_kind="daily",
    ),
    "weekly_health": DutyRecipe(
        id="weekly_health",
        title="每周健康巡检",
        description="复用六类知识体检，只列最重要的 ≤5 项问题；不自动修复。",
        candidate_limit=5,
        op_types=("create",),
        default_schedule={"kind": "weekly", "weekday": 6, "time": "18:00"},
        default_scope=(),
        output_kind="report",
    ),
}


def recipe_for(recipe_id: str) -> DutyRecipe:
    """按 id 取固定职责；未知 id 抛 ``DutyRecipeError``。"""
    try:
        return RECIPES[str(recipe_id)]
    except KeyError as exc:
        raise DutyRecipeError(
            f"未知职责: {recipe_id!r}，允许 {sorted(RECIPES)}"
        ) from exc


def default_scope_for(recipe_id: str, inbox_dir: str = "inbox") -> Tuple[str, ...]:
    """内置职责的默认范围；收件箱跟随配置目录，其余默认全库。"""
    if recipe_id == "inbox_duty":
        return (str(inbox_dir or "inbox").strip("/"),)
    return tuple(recipe_for(recipe_id).default_scope)


@dataclass(frozen=True)
class DutyDefinition:
    """持久化的职责定义（``.kwey/duty.db`` 的 duties 行）。

    字段与 INNOVATION-PLAN §4.7 对齐；``schedule`` / ``limits`` 为 JSON 可
    序列化的普通 dict，写入统一由 :class:`app.duty.store.DutyStore` 完成。
    """

    duty_id: str
    recipe: str
    enabled: bool
    scope: Tuple[str, ...]
    schedule: Dict[str, Any]
    limits: Dict[str, int]
    config_version: int

    @property
    def spec(self) -> DutyRecipe:
        return recipe_for(self.recipe)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duty_id": self.duty_id,
            "recipe": self.recipe,
            "enabled": bool(self.enabled),
            "scope": list(self.scope),
            "schedule": dict(self.schedule),
            "limits": dict(self.limits),
            "config_version": int(self.config_version),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DutyDefinition":
        return cls(
            duty_id=str(data["duty_id"]),
            recipe=str(data["recipe"]),
            enabled=bool(data.get("enabled", False)),
            scope=tuple(str(p) for p in (data.get("scope") or ())),
            schedule=dict(data.get("schedule") or {}),
            limits={k: int(v) for k, v in (data.get("limits") or {}).items()},
            config_version=int(data.get("config_version", 1)),
        )
