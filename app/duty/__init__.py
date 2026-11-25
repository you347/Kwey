"""A0 值班 Agent 内核（固定职责 / 只读候选 / 策略 / 控制存储 / 运行时）。

本包不导入 ``app.api.deps``，所有依赖由调用方注入；首批交付不接 Timer、
EventBus、HTTP 路由或前端队列。
"""

from app.duty.candidates import Candidate, CandidateSet, collect_candidates
from app.duty.policy import (
    BudgetExceeded,
    DutyPolicyError,
    RunBudget,
    ScopeError,
    compute_input_hash,
    compute_proposal_hash,
    dedup_should_skip,
    idempotency_key,
    normalize_definition,
    normalize_limits,
    normalize_scope,
    rate_limit_exceeded,
    slot_key,
    validate_ops_for_recipe,
)
from app.duty.reader import DutyReader, VaultDutyReader
from app.duty.recipes import (
    RECIPES,
    RECIPE_VERSION,
    DutyDefinition,
    DutyRecipe,
    DutyRecipeError,
    recipe_for,
)
from app.duty.runtime import DutyRuntime
from app.duty.store import (
    DutyConflictError,
    DutySchemaVersionError,
    DutyStateError,
    DutyStore,
    DutyStoreError,
)

__all__ = [
    "BudgetExceeded",
    "Candidate",
    "CandidateSet",
    "DutyConflictError",
    "DutyDefinition",
    "DutyPolicyError",
    "DutyReader",
    "DutyRecipe",
    "DutyRecipeError",
    "DutyRuntime",
    "DutySchemaVersionError",
    "DutyStateError",
    "DutyStore",
    "DutyStoreError",
    "RECIPES",
    "RECIPE_VERSION",
    "RunBudget",
    "ScopeError",
    "VaultDutyReader",
    "collect_candidates",
    "compute_input_hash",
    "compute_proposal_hash",
    "dedup_should_skip",
    "idempotency_key",
    "normalize_definition",
    "normalize_limits",
    "normalize_scope",
    "rate_limit_exceeded",
    "recipe_for",
    "slot_key",
    "validate_ops_for_recipe",
]
