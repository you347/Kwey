"""值班生命周期：Timer、EventBus、单 worker、启动补跑与切库/退出恢复。

架构（INNOVATION-PLAN §4.5 / §4.6）：

    Timer / EventBus（回调只做路径规范化、排除目录与入队）
      → DutyRuntime（固定 vault 实例、generation、有限队列）
      → 单 worker（只读候选 → 固定职责 → 可选模型）
      → 提案队列 → 审批 → ProposalApplier（锁内预检 + 逐项应用）

本模块**不导入** ``app.api.deps``：所有依赖由 deps 装配注入，worker 捕获明确
vault 实例，不在中途读取全局 deps。``--server`` 与桌面模式的区别只体现在
``mode`` 与审批 grant 是否有原生桥可签发。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from app.events import EventType, VaultEvent
from app.paths import is_ignored, normalize_rel

from app.duty.policy import (
    DutyPolicyError,
    in_scope,
    last_due_slot,
    normalize_definition,
    slot_key,
)
from app.duty.recipes import recipe_for
from app.duty.store import DutyConflictError, DutyStateError, DutyStore

logger = logging.getLogger("kwey.duty")

TICK_SECONDS = 15.0
EVENT_DEBOUNCE_SECONDS = 30.0
APPLIED_COOLDOWN_SECONDS = 10.0
MAX_PENDING_EVENTS = 200
DUTY_RECIPES = ("inbox_duty", "daily_brief", "weekly_health")


class DutySupervisor:
    """单 vault 的职责调度与恢复。"""

    def __init__(
        self,
        *,
        store: DutyStore,
        reader: Any,
        runtime: Any,
        vault: Any,
        applier: Any,
        grants: Any,
        mode: str = "server",
        clock: Optional[Callable[[], float]] = None,
        tz: Any = None,
        provider_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.store = store
        self.reader = reader
        self.runtime = runtime
        self.vault = vault
        self.applier = applier
        self.grants = grants
        self.mode = mode
        self.clock = clock or time.time
        self.tz = tz
        self._provider_factory = provider_factory

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._prepared = False
        self._started = False
        self._paused_duties: set = set()
        self._pending_paths: set = set()
        self._pending_since: Optional[float] = None
        self._inbox_duties: List[Dict[str, Any]] = []
        self._recent_applied: Dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle
    def ensure_seeded(self) -> None:
        """首次打开时写入三条内置职责（默认关闭），让 UI 有可启用卡片。"""
        inbox_dir = normalize_rel(self.runtime.dirs.get("inbox") or "inbox") or "inbox"
        existing = {item["duty_id"] for item in self.store.list_definitions()}
        for recipe_id in DUTY_RECIPES:
            if recipe_id in existing:
                continue
            try:
                definition = normalize_definition(
                    recipe_id, enabled=False, scope=None, inbox_dir=inbox_dir
                )
            except DutyPolicyError:
                logger.exception("内置职责定义非法: %s", recipe_id)
                continue
            self.store.save_definition(definition)

    def prepare(self) -> None:
        """恢复上次进程状态、计算 generation、订阅 EventBus（不启动线程）。"""
        with self._lock:
            if self._prepared:
                return
            self.ensure_seeded()
            self._bump_generation_from_store()
            self._recover_previous_process()
            self._bind_bus()
            self._refresh_inbox_duties()
            if self._provider_factory is not None:
                try:
                    self.runtime.provider = self._provider_factory()
                except Exception:  # noqa: BLE001 —— 模型未配置不应阻断值班
                    self.runtime.provider = None
            self._prepared = True

    def start(self) -> None:
        """lifespan / vault open 后调用：补跑检查 + 启动 scheduler/worker 线程。"""
        self.prepare()
        with self._lock:
            if self._started:
                return
            self._stop.clear()
            self._started = True
            self._threads = [
                threading.Thread(target=self._scheduler_loop, name="kwey-duty-timer", daemon=True),
                threading.Thread(target=self._worker_loop, name="kwey-duty-worker", daemon=True),
            ]
            for thread in self._threads:
                thread.start()

    def stop(self) -> None:
        """退出 / 切库：停接新任务、解绑旧 bus、bump generation 再停线程。"""
        with self._lock:
            was_prepared = self._prepared
            self._started = False
            self._prepared = False
        self._stop.set()
        self._wake.set()
        if was_prepared:
            self._unbind_bus()
            # 旧 generation 的在途结果必须丢弃，绝不写向新库。
            self.runtime.bump_generation()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=1.5)
        self._threads = []
        with self._lock:
            self._pending_paths.clear()
            self._pending_since = None
            self._paused_duties.clear()
            self._recent_applied.clear()
        if hasattr(self.grants, "clear"):
            self.grants.clear()

    # ------------------------------------------------------------------ generation / recovery
    def _bump_generation_from_store(self) -> None:
        """跨重启保持 generation 单调，旧 run 结果不会误判为当前库。"""
        seen = [int(run["vault_generation"]) for run in self.store.list_runs(limit=500)]
        self.runtime.generation = max(seen + [int(self.runtime.generation) - 1]) + 1

    def _recover_previous_process(self) -> None:
        """applying 不得自动恢复成 pending；旧 queued/running 不自动重跑。"""
        for proposal in self.store.list_proposals(status="applying", limit=500):
            proposal_id = str(proposal["proposal_id"])
            try:
                self.store.transition_proposal(
                    proposal_id, "interrupted", expected="applying"
                )
            except (DutyStateError, DutyConflictError):
                pass
            for item in self.store.list_apply_items(proposal_id):
                if item["status"] != "intent":
                    continue
                try:
                    self.store.record_apply_item(
                        proposal_id,
                        int(item["op_index"]),
                        status="uncertain",
                        error="进程退出时该项已开始但结果未记录，请对照实际文件",
                    )
                except (DutyStateError, DutyConflictError):
                    pass

        for status in ("queued", "running"):
            for run in self.store.list_runs(status=status, limit=500):
                try:
                    self.store.transition_run(
                        str(run["run_id"]),
                        "skipped",
                        expected=status,
                        error_code="process_restarted",
                        summary={"reason": "上次进程已退出，本轮不自动重跑"},
                    )
                except (DutyStateError, DutyConflictError):
                    pass

    # ------------------------------------------------------------------ bus
    def _bind_bus(self) -> None:
        bus = getattr(self.vault, "eventbus", None)
        if bus is None:
            return
        bus.unsubscribe(None, self._on_event)
        bus.subscribe(None, self._on_event)

    def _unbind_bus(self) -> None:
        bus = getattr(self.vault, "eventbus", None)
        if bus is not None:
            bus.unsubscribe(None, self._on_event)

    def _on_event(self, event: VaultEvent) -> None:
        """EventBus 回调：只做路径规范化、排除目录与入队（不读全库/不跑模型）。"""
        kind = event.event.value if isinstance(event.event, EventType) else str(event.event)
        if kind not in (EventType.CREATED.value, EventType.UPDATED.value, EventType.MOVED.value):
            return
        path = normalize_rel(event.path)
        if not path or is_ignored(path) or path.endswith(".kwey-tmp"):
            return
        if self._is_output_path(path):
            return
        now = float(self.clock())
        with self._lock:
            stamp = self._recent_applied.get(path)
            if stamp is not None and now - stamp <= APPLIED_COOLDOWN_SECONDS:
                return
            if self.runtime.paused or any(
                defn["duty_id"] in self._paused_duties for defn in self._inbox_duties
            ):
                return
            if not any(in_scope(path, defn.get("scope") or ()) for defn in self._inbox_duties):
                return
            if len(self._pending_paths) >= MAX_PENDING_EVENTS:
                self._pending_paths.clear()
            self._pending_paths.add(path)
            # 静默等待：以最后一次事件为计时起点。
            self._pending_since = now

    def _is_output_path(self, path: str) -> bool:
        for key in ("daily", "report"):
            folder = normalize_rel(self.runtime.dirs.get(key) or "")
            if folder and (path == folder or path.startswith(folder + "/")):
                return True
        return False

    # ------------------------------------------------------------------ scheduler
    def tick(self, now: Optional[float] = None) -> None:
        """一次调度检查：到期槽补跑 + 事件防抖入队。测试可直接调用。"""
        moment = float(now) if now is not None else float(self.clock())
        with self._lock:
            self._refresh_inbox_duties()
            self._prune_recent(moment)
            definitions = self.store.list_definitions()

        if not self.runtime.paused:
            for definition in definitions:
                duty_id = str(definition["duty_id"])
                if not definition.get("enabled") or duty_id in self._paused_duties:
                    continue
                slot_ts = last_due_slot(
                    definition.get("schedule") or {},
                    moment,
                    tz=self.tz,
                    max_age=24 * 3600,
                )
                if slot_ts is None:
                    continue
                slot = slot_key(str(definition["recipe"]), slot_ts, self.tz)
                if self._has_run_for_slot(duty_id, slot):
                    continue
                self._enqueue(duty_id, trigger="schedule", slot=slot, now=moment)

        with self._lock:
            due = (
                self._pending_paths
                and self._pending_since is not None
                and moment - self._pending_since >= EVENT_DEBOUNCE_SECONDS
            )
            pending = list(self._pending_paths)
            if due:
                self._pending_paths.clear()
                self._pending_since = None
            duties = [dict(item) for item in self._inbox_duties]
        if not due or not pending or self.runtime.paused:
            return
        for definition in duties:
            duty_id = str(definition["duty_id"])
            if not definition.get("enabled") or duty_id in self._paused_duties:
                continue
            self._enqueue(duty_id, trigger="schedule", now=moment)

    def _has_run_for_slot(self, duty_id: str, slot: str) -> bool:
        for run in self.store.list_runs(duty_id=duty_id, limit=200):
            if str(run.get("slot") or "") == slot:
                return True
        return False

    def _enqueue(
        self,
        duty_id: str,
        *,
        trigger: str,
        slot: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if duty_id in self._paused_duties or self.runtime.paused:
            return None
        try:
            result = self.runtime.enqueue(
                duty_id, trigger=trigger, slot=slot, now=now
            )
        except KeyError:
            return None
        if result.get("queued"):
            self._wake.set()
        return result

    def _refresh_inbox_duties(self) -> None:
        self._inbox_duties = [
            definition
            for definition in self.store.list_definitions()
            if definition.get("recipe") == "inbox_duty" and definition.get("enabled")
        ]

    def _prune_recent(self, moment: float) -> None:
        expired = [
            path
            for path, stamp in self._recent_applied.items()
            if moment - stamp > APPLIED_COOLDOWN_SECONDS
        ]
        for path in expired:
            self._recent_applied.pop(path, None)

    def mark_applied_paths(self, paths: List[str]) -> None:
        """应用后冷却：忽略本次写入产生的 UPDATED 事件，防回环。"""
        now = float(self.clock())
        with self._lock:
            for path in paths:
                clean = normalize_rel(path)
                if clean:
                    self._recent_applied[clean] = now

    # ------------------------------------------------------------------ threads
    def _scheduler_loop(self) -> None:
        # 启动即补跑：只合并最近 24 小时内最后一个未完成槽。
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 —— 定时器不因单轮异常退出
                logger.exception("值班定时检查失败")
            self._stop.wait(TICK_SECONDS)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._provider_factory is not None:
                    self.runtime.provider = self._provider_factory()
                result = self.runtime.process_next()
            except Exception:  # noqa: BLE001 —— 单轮失败不拖垮 worker
                logger.exception("值班 worker 异常")
                result = None
            if result is None:
                self._wake.wait(1.0)
                self._wake.clear()

    # ------------------------------------------------------------------ definitions
    def _definition_payload(self, definition: Mapping[str, Any]) -> Dict[str, Any]:
        scope = [str(item) for item in (definition.get("scope") or [])]
        recipe = recipe_for(str(definition["recipe"]))
        return {
            "id": str(definition["duty_id"]),
            "duty_id": str(definition["duty_id"]),
            "recipe": str(definition["recipe"]),
            "enabled": bool(definition.get("enabled")),
            "scope": ",".join(scope),
            # 空范围对 daily_brief / weekly_health 是「全库」默认，仅 inbox_duty 要求必填。
            "scope_required": bool(recipe.requires_scope),
            "schedule": dict(definition.get("schedule") or {}),
            "limits": dict(definition.get("limits") or {}),
            "config_version": int(definition.get("config_version") or 1),
        }

    def update_definition(self, duty_id: str, patch: Mapping[str, Any]) -> Dict[str, Any]:
        self.ensure_seeded()
        current = self.store.get_definition(duty_id)
        if current is None:
            raise KeyError(duty_id)
        expected = patch.get("config_version")
        if expected is not None and int(expected) != int(current["config_version"]):
            raise DutyConflictError(
                f"配置版本不匹配：当前 v{current['config_version']}，收到 v{expected}"
            )
        inbox_dir = normalize_rel(self.runtime.dirs.get("inbox") or "inbox") or "inbox"
        definition = normalize_definition(
            str(current["recipe"]),
            duty_id=duty_id,
            enabled=bool(patch.get("enabled", current.get("enabled"))),
            scope=patch.get("scope", current.get("scope")),
            schedule=patch.get("schedule", current.get("schedule")),
            limits=patch.get("limits", current.get("limits")),
            config_version=int(current["config_version"]) + 1,
            inbox_dir=inbox_dir,
        )
        saved = self.store.save_definition(definition)
        with self._lock:
            self._refresh_inbox_duties()
        return self._definition_payload(saved)

    # ------------------------------------------------------------------ queries
    def overview(self) -> Dict[str, Any]:
        self.ensure_seeded()
        definitions = [
            self._definition_payload(item) for item in self.store.list_definitions()
        ]
        running = self.store.list_runs(status="running", limit=1)
        queued = self.store.list_runs(status="queued", limit=200)
        provider = getattr(self.runtime, "provider", None)
        configured = bool(getattr(provider, "configured", False))
        model = ""
        if configured:
            model = str((getattr(provider, "config", {}) or {}).get("model") or "")
        pending = self.store.list_proposals(status="pending", limit=500)
        return {
            "definitions": definitions,
            "runtime_state": {
                "paused": bool(self.runtime.paused),
                "queue_depth": len(queued),
                "active_run_id": str(running[0]["run_id"]) if running else "",
                "generation": int(self.runtime.generation),
                "vault_generation": int(self.runtime.generation),
                "model": model,
                "ai_ready": configured,
                "mode": self.mode,
                "apply_enabled": self.mode == "desktop",
                "note": "暂停后不再调度或应用" if self.runtime.paused else "",
            },
            "pending_count": len(pending),
        }

    def list_runs(self, *, limit: int = 50, cursor: int = 0) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(cursor or 0))
        rows = self.store.list_runs(limit=offset + limit + 1)
        page = rows[offset : offset + limit]
        definitions = {
            str(item["duty_id"]): item for item in self.store.list_definitions()
        }
        items = []
        for run in page:
            proposal = self.store.latest_proposal_for_run(str(run["run_id"]))
            items.append(
                {
                    "run_id": str(run["run_id"]),
                    "duty_id": str(run["duty_id"]),
                    "recipe": str(definitions.get(str(run["duty_id"]), {}).get("recipe") or ""),
                    "status": str(run["status"]),
                    "slot": run.get("slot"),
                    "queued_at": run.get("queued_at"),
                    "started_at": run.get("started_at"),
                    "ended_at": run.get("ended_at"),
                    "error_code": run.get("error_code"),
                    "model_usage": dict(run.get("model_usage") or {}),
                    "summary": dict(run.get("summary") or {}),
                    "proposal_id": str(proposal["proposal_id"]) if proposal else "",
                }
            )
        return {
            "runs": items,
            "next_cursor": offset + limit if len(rows) > offset + limit else None,
            "total": None,
        }

    def list_proposals(
        self, *, status: Optional[str] = None, limit: int = 50, cursor: int = 0
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(cursor or 0))
        wanted = None if status in (None, "", "all") else str(status)
        rows = self.store.list_proposals(status=wanted, limit=500)
        page = rows[offset : offset + limit]
        summaries = [self._proposal_summary(item) for item in page]
        return {
            "proposals": summaries,
            "next_cursor": offset + limit if len(rows) > offset + limit else None,
            "total": len(rows),
        }

    def _proposal_summary(self, proposal: Mapping[str, Any]) -> Dict[str, Any]:
        run = self.store.get_run(str(proposal.get("run_id") or "")) or {}
        definition = self.store.get_definition(str(run.get("duty_id") or "")) or {}
        diffs = list(proposal.get("diffs") or [])
        links = sum(int(item.get("links_affected") or 0) for item in diffs)
        recipe = str(definition.get("recipe") or "")
        title = ""
        try:
            title = recipe_for(recipe).title if recipe else ""
        except Exception:  # noqa: BLE001
            title = recipe
        return {
            "proposal_id": str(proposal["proposal_id"]),
            "run_id": str(proposal.get("run_id") or ""),
            "duty_id": str(run.get("duty_id") or ""),
            "recipe": recipe,
            "title": title or str(proposal.get("reason") or ""),
            "reason": str(proposal.get("reason") or ""),
            "version": int(proposal.get("version") or 1),
            "status": str(proposal.get("status") or ""),
            "ops_count": len(proposal.get("ops") or []),
            "files_count": len(proposal.get("manifest") or []),
            "links_affected": links,
            "created_at": proposal.get("created_at"),
            "expires_at": proposal.get("expires_at"),
            "proposal_hash": str(proposal.get("proposal_hash") or ""),
        }

    def proposal_detail(self, proposal_id: str) -> Dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        data = self._proposal_summary(proposal)
        conflicts = (
            self.applier.manifest_conflicts(proposal)
            if str(proposal.get("status")) in ("pending", "applying", "partial", "uncertain")
            else []
        )
        data.update(
            {
                "ops": list(proposal.get("ops") or []),
                "diffs": list(proposal.get("diffs") or []),
                "sources": list(proposal.get("sources") or []),
                "manifest": list(proposal.get("manifest") or []),
                "base_version": int(proposal.get("version") or 1),
                "current_version": int(proposal.get("version") or 1),
                "version_changed": bool(conflicts),
                "stale": bool(conflicts),
                "stale_reason": (
                    "以下文件在预览后已变化：" + "、".join(conflicts[:5])
                    if conflicts
                    else ""
                ),
                "policy": {},
                "freshness": self._freshness_check(proposal),
            }
        )
        return data

    def _freshness_check(self, proposal: Mapping[str, Any]) -> Dict[str, Any]:
        """提案详情重检：最多 5 份来源的有效期/替代关系，只提示不阻断。"""
        from app.freshness import inspect_sources

        db = getattr(self.vault, "db", None)
        paths = [str(p) for p in (proposal.get("sources") or []) if p]
        if db is None or not paths:
            return {"checked": 0, "total": len(paths), "truncated": False, "risks": [],
                    "message": "未发现明确的失效声明。"}
        try:
            return inspect_sources(paths, db=db, limit=5)
        except Exception:  # noqa: BLE001 —— 检查失败明确为未检查，不冒充无冲突
            logger.warning("提案保鲜重检失败", exc_info=True)
            return {"checked": 0, "total": len(paths), "truncated": False, "risks": [],
                    "message": "保鲜检查未完成；请人工核对来源有效期。"}

    # ------------------------------------------------------------------ approve bridge
    def grant_context(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """原生确认框需要的最小上下文（路径/动作/版本），不暴露完整正文。"""
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None or str(proposal.get("status")) != "pending":
            return None
        actions = []
        for diff in proposal.get("diffs") or []:
            actions.append(
                {
                    "action": str(diff.get("action") or diff.get("op") or ""),
                    "path": str(diff.get("path") or ""),
                    "target": str(diff.get("target") or ""),
                }
            )
        return {
            "proposal_id": str(proposal["proposal_id"]),
            "version": int(proposal["version"]),
            "proposal_hash": str(proposal["proposal_hash"]),
            "generation": int(self.runtime.generation),
            "actions": actions[:20],
        }

    def issue_grant(self, proposal_id: str) -> Optional[str]:
        """桌面原生桥确认后签发一次性 grant；HTTP 端点不得调用。"""
        context = self.grant_context(proposal_id)
        if context is None:
            return None
        return self.grants.issue(
            proposal_id=context["proposal_id"],
            version=context["version"],
            proposal_hash=context["proposal_hash"],
            generation=context["generation"],
        )

    # ---------------------------------------------------------- 会话草案审批桥
    def session_grant_context(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """会话待审草案的最小确认上下文；复用同一 ApprovalGrants 实例。"""
        from app.agent import session_distill as sd

        try:
            draft = sd.load_draft(self.vault.paths.root, draft_id)
        except sd.DistillError:
            return None
        if str(draft.get("status")) != sd.STATUS_PENDING:
            return None
        targets = sd.draft_targets(draft)
        return {
            "draft_id": str(draft.get("draft_id")),
            "version": int(draft.get("version") or 1),
            "proposal_hash": str(draft.get("build_hash") or sd.draft_build_hash(draft)),
            "generation": int(self.runtime.generation),
            "actions": [
                {"action": "create", "path": targets["evidence"]},
                {"action": "create", "path": targets["distill"]},
            ],
        }

    def issue_session_grant(self, draft_id: str) -> Optional[str]:
        """桌面原生桥确认后签发会话草案 grant；HTTP 端点不得调用。"""
        context = self.session_grant_context(draft_id)
        if context is None:
            return None
        return self.grants.issue(
            proposal_id=context["draft_id"],
            version=context["version"],
            proposal_hash=context["proposal_hash"],
            generation=context["generation"],
        )

    def consume_session_grant(
        self,
        grant: str,
        *,
        draft_id: str,
        version: int,
        build_hash: str,
    ) -> None:
        """校验并消费会话草案 grant；与值班提案共用同一授权表。"""
        self.grants.consume(
            grant,
            proposal_id=str(draft_id),
            version=int(version),
            proposal_hash=str(build_hash),
            generation=int(self.runtime.generation),
        )

    # ------------------------------------------------------------------ pause
    def pause(self, *, all: bool = False, duty_id: Optional[str] = None) -> Dict[str, Any]:
        if duty_id:
            self.ensure_seeded()
        with self._lock:
            if all:
                self.runtime.pause()
                self._pending_paths.clear()
                self._pending_since = None
                return {"paused": True, "scope": "all"}
            if not duty_id:
                raise ValueError("必须指定 all 或 duty_id")
            if self.store.get_definition(duty_id) is None:
                raise KeyError(duty_id)
            self._paused_duties.add(str(duty_id))
            return {"paused": True, "scope": str(duty_id)}

    def _now(self) -> float:
        return float(self.clock())
