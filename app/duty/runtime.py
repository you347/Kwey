"""值班运行时：固定依赖注入、只读候选、去重、预算与 run 状态机。

本批次**不接全局 deps、不订阅 EventBus、不启动 Timer、不执行 apply**。
后续集成批次负责：server/deps 接线、Timer/事件、审批桥与队列 UI。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.agent.ops import preview_ops
from app.agent.provider import ProviderError, chat_json
from app.paths import AppPaths, PathError, normalize_rel

from app.duty.candidates import CandidateSet, collect_candidates, date_label
from app.duty.policy import (
    BudgetExceeded,
    DutyPolicyError,
    RunBudget,
    compute_input_hash,
    compute_proposal_hash,
    dedup_should_skip,
    idempotency_key,
    rate_limit_exceeded,
    run_budget,
    slot_key,
    validate_ops_for_recipe,
)
from app.duty.reader import DutyReader
from app.duty.recipes import MAX_QUEUE, MAX_RUNS_PER_HOUR, PROPOSAL_TTL_SECONDS, recipe_for
from app.duty.store import DutyStateError, DutyStore

logger = logging.getLogger("kwey.duty")


class _PreviewVault:
    """只给 :func:`preview_ops` 用的只读路径门面（不暴露任何写方法）。"""

    def __init__(self, root: Path) -> None:
        self.paths = AppPaths(Path(root))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DutyRuntime:
    """单 vault / 单 worker 的职责运行时。

    依赖全部从构造参数注入：``store``、``reader``、``clock``、``provider``、
    输出目录与 generation。运行结果只写控制库，不写笔记。
    """

    def __init__(
        self,
        *,
        store: DutyStore,
        reader: DutyReader,
        clock: Optional[Callable[[], float]] = None,
        tz: Any = None,
        provider: Any = None,
        dirs: Optional[Mapping[str, str]] = None,
        generation: int = 0,
        id_factory: Optional[Callable[[str], str]] = None,
        links: Any = None,
    ) -> None:
        self.store = store
        self.reader = reader
        # 链接影响适配器（复用 LinkGraph/LinkRewriter 的索引解析，不做正则猜测）。
        self.links = links
        self.clock = clock or time.time
        self.tz = tz
        self.provider = provider
        self.dirs = {
            "inbox": "inbox",
            "daily": "daily",
            "report": "reports",
            **dict(dirs or {}),
        }
        self.generation = int(generation)
        self._paused = False
        self._id_factory = id_factory or (
            lambda prefix: f"{prefix}_{uuid.uuid4().hex}"
        )

    # ------------------------------------------------------------------ lifecycle
    def now(self) -> float:
        return float(self.clock())

    def bump_generation(self) -> int:
        """切库时递增；旧 generation 的 run 结果必须丢弃。"""
        self.generation += 1
        return self.generation

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------ enqueue
    def enqueue(
        self,
        duty_id: str,
        *,
        trigger: str = "manual",
        slot: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """异步入队（不读笔记、不调用模型）；限额不足时返回 not queued。"""
        if trigger not in ("manual", "schedule"):
            raise ValueError(f"未知触发方式: {trigger}")
        if self._paused:
            return {"queued": False, "reason": "paused"}

        definition = self.store.get_definition(duty_id)
        if definition is None:
            raise KeyError(f"职责未定义: {duty_id}")
        if trigger == "schedule" and not definition.get("enabled"):
            return {"queued": False, "reason": "disabled"}

        moment = self.now() if now is None else float(now)
        if self.store.count_active_runs() >= MAX_QUEUE:
            return {"queued": False, "reason": "queue_full"}
        started = self.store.started_run_timestamps(duty_id, moment - 3600)
        if rate_limit_exceeded(started, moment, limit=MAX_RUNS_PER_HOUR):
            return {"queued": False, "reason": "rate_limited"}

        bucket = slot or slot_key(str(definition["recipe"]), moment, self.tz)
        run_id = self._id_factory("run")
        created = self.store.create_run(
            run_id=run_id,
            duty_id=duty_id,
            vault_generation=self.generation,
            config_version=int(definition["config_version"]),
            slot=bucket,
            now=moment,
        )
        return {"queued": bool(created["created"]), "run": created["run"]}

    def run_once(
        self,
        duty_id: str,
        *,
        trigger: str = "manual",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """手动试跑：入队后立即处理队首（仍遵守队列/限额）。"""
        result = self.enqueue(duty_id, trigger=trigger, now=now)
        if not result.get("queued"):
            return result
        return self.process_next(now=now)

    # ------------------------------------------------------------------ worker
    def process_next(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """领取一个 queued run 并执行；无任务或已暂停返回 None。"""
        if self._paused:
            return None
        run = self.store.claim_next_queued(now=self.now() if now is None else now)
        if run is None:
            return None
        return self._execute(run)

    def _execute(self, run: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = str(run["run_id"])
        duty_id = str(run["duty_id"])
        proposal: Optional[Dict[str, Any]] = None
        definition = self.store.get_definition(duty_id)

        try:
            if int(run["vault_generation"]) != self.generation:
                self._fail(
                    run_id,
                    "stale_generation",
                    status="skipped",
                    summary={"reason": "vault generation 已变化，旧结果丢弃"},
                )
                return self._result(run_id, None)

            if definition is None:
                self._fail(run_id, "definition_missing", summary={"reason": "职责定义已删除"})
                return self._result(run_id, None)

            recipe = str(definition["recipe"])
            candset = collect_candidates(
                recipe,
                self.reader,
                definition,
                now=self.now(),
                dirs=self.dirs,
                tz=self.tz,
            )
            summary = dict(candset.summary)
            summary["candidate_count"] = len(candset.items)

            if not candset.items:
                self.store.transition_run(run_id, "no_change", summary=summary)
                return self._result(run_id, None)

            input_hash = compute_input_hash(
                recipe=recipe,
                config_version=int(definition["config_version"]),
                candidates=candset.items,
            )
            priors = self.store.find_prior_runs(
                duty_id=duty_id,
                config_version=int(definition["config_version"]),
                input_hash=input_hash,
                slot=run.get("slot"),
                exclude_run_id=run_id,
            )
            if priors:
                for prior in priors:
                    prior_proposal = self.store.latest_proposal_for_run(prior["run_id"])
                    prior_decision = (
                        self.store.get_decision(prior_proposal["proposal_id"])
                        if prior_proposal
                        else None
                    )
                    if not dedup_should_skip(prior, prior_proposal, prior_decision):
                        continue
                    self.store.set_run_input(run_id, input_hash, None)
                    summary.update(
                        {
                            "reason": "相同输入已处理或已拒绝，本轮不重复提醒",
                            "prior_run_id": prior["run_id"],
                        }
                    )
                    self.store.transition_run(
                        run_id, "skipped", error_code="duplicate_input", summary=summary
                    )
                    return self._result(run_id, None)

            base_key = idempotency_key(
                duty_id,
                int(definition["config_version"]),
                input_hash,
                str(run.get("slot") or ""),
            )
            stored = self.store.set_run_input(run_id, input_hash, base_key)
            if not stored.get("ok"):
                # 同输入在失败后允许重试：唯一键加 run 后缀，不覆盖旧记录。
                stored = self.store.set_run_input(
                    run_id, input_hash, f"{base_key}#{run_id}"
                )
                if not stored.get("ok"):
                    raise DutyStateError("幂等键写入失败")

            budget = run_budget(definition)
            generated = self._generate(
                recipe, definition, candset, budget, now=self.now()
            )
            summary.update(generated.get("summary") or {})
            summary["rule_only"] = bool(generated.get("rule_only"))
            summary["model_usage"] = dict(generated.get("model_usage") or {})
            ops = list(generated.get("ops") or [])
            sources = list(generated.get("sources") or [])
            reason = str(generated.get("reason") or "")

            if not ops:
                self.store.transition_run(
                    run_id,
                    "no_change",
                    summary=summary,
                    model_usage=summary.get("model_usage"),
                )
                return self._result(run_id, None)

            budget.charge_ops(len(ops))
            normalized = validate_ops_for_recipe(
                ops,
                recipe=recipe,
                scope=tuple(definition.get("scope") or ()),
                output_dirs=self._output_dirs(recipe),
                allowed_move_dirs=tuple(self.reader.list_dirs()),
            )
            diffs, manifest = self._preview_and_manifest(normalized, candset, budget)
            # P2-D：预览时识别相关有效期/替代关系；最多 5 份证据，只提示、不阻断。
            summary["freshness"] = self._freshness_risks(
                [str(op.get("path") or "") for op in normalized]
                + [item.path for item in candset.items if item.path]
            )
            # 切库/退出后 generation 已递增：旧实例的在途结果必须丢弃，绝不写向新库。
            if int(run["vault_generation"]) != self.generation:
                self._fail(
                    run_id,
                    "stale_generation",
                    status="skipped",
                    summary={"reason": "vault generation 已变化，旧结果丢弃"},
                )
                return self._result(run_id, None)
            proposal_id = self._id_factory("prop")
            proposal_hash = compute_proposal_hash(
                ops=normalized,
                manifest=manifest,
                sources=sources,
                reason=reason,
            )
            proposal = self.store.create_proposal(
                proposal_id=proposal_id,
                run_id=run_id,
                ops=normalized,
                diffs=diffs,
                manifest=manifest,
                sources=sources,
                reason=reason,
                proposal_hash=proposal_hash,
                expires_at=self.now() + PROPOSAL_TTL_SECONDS,
                now=self.now(),
            )
            summary["proposal_id"] = proposal_id
            self.store.transition_run(
                run_id,
                "awaiting_review",
                summary=summary,
                model_usage=summary.get("model_usage"),
            )
        except ProviderError:
            self._fail(run_id, "provider_error", summary={"reason": "生成模型调用失败"})
        except BudgetExceeded as exc:
            self._fail(
                run_id,
                "budget_exceeded",
                status="budget_exceeded",
                summary={"reason": str(exc)},
            )
        except DutyPolicyError as exc:
            self._fail(
                run_id, "policy_violation", summary={"reason": str(exc)}
            )
        except DutyStateError as exc:
            self._fail(run_id, "state_error", summary={"reason": str(exc)})
        except Exception:  # noqa: BLE001 —— 单轮失败不拖垮 worker，不记录正文
            logger.exception("值班 run 失败: %s", run_id)
            self._fail(run_id, "internal_error", summary={"reason": "运行时内部错误"})

        return self._result(run_id, proposal)

    def _result(self, run_id: str, proposal: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        return {"run": self.store.get_run(run_id), "proposal": proposal}

    def _fail(
        self,
        run_id: str,
        error_code: str,
        *,
        status: str = "failed",
        summary: Optional[Mapping[str, Any]] = None,
    ) -> None:
        try:
            self.store.transition_run(
                run_id, status, error_code=error_code, summary=dict(summary or {})
            )
        except Exception:  # noqa: BLE001 —— 已是终态时保持原记录
            return

    # ------------------------------------------------------------------ 生成器
    def _generate(
        self,
        recipe: str,
        definition: Mapping[str, Any],
        candset: CandidateSet,
        budget: RunBudget,
        *,
        now: float,
    ) -> Dict[str, Any]:
        if recipe == "inbox_duty":
            return self._generate_inbox(definition, candset, budget)
        if recipe == "daily_brief":
            return self._generate_daily(definition, candset, budget, now=now)
        if recipe == "weekly_health":
            return {
                "ops": [],
                "sources": [],
                "reason": "巡检结果保存在控制记录；保存报告需用户单独选择",
                "rule_only": True,
                "model_usage": {},
                "summary": {"issues": candset.summary.get("issues") or []},
            }
        raise DutyPolicyError(f"未实现的职责生成器: {recipe}")

    def _generate_inbox(
        self,
        definition: Mapping[str, Any],
        candset: CandidateSet,
        budget: RunBudget,
    ) -> Dict[str, Any]:
        paths = [item.path for item in candset.items if item.path and not item.oversized]
        scope = tuple(definition.get("scope") or ())
        inbox_dir = scope[0] if scope else normalize_rel(self.dirs.get("inbox", "inbox"))
        allowed_dirs = [d for d in self.reader.list_dirs() if d != inbox_dir]

        if self.provider is None or not getattr(self.provider, "configured", False):
            return {
                "ops": [],
                "sources": paths,
                "reason": "未配置生成模型，仅报告收件箱积压",
                "rule_only": True,
                "model_usage": {},
                "summary": {"ai": False, "backlog": candset.summary.get("backlog")},
            }

        payload = [
            {
                "path": item.path,
                "title": item.title,
                "excerpt": item.excerpt[:200],
                "tags": item.facts.get("tags") or [],
            }
            for item in candset.items
            if item.path and not item.oversized
        ]
        prompt = (
            f"【可归档目录】{allowed_dirs or ['(无)']}\n"
            f"【收件箱条目】{_canonical(payload)}"
        )
        budget.charge_model(len(prompt))
        data = chat_json(
            self.provider,
            [
                {
                    "role": "system",
                    "content": (
                        "你是收件箱整理助手。只返回 JSON："
                        '{"items":[{"path":"原路径","tags":["1-2个标签"],'
                        '"folder":"已有目录或空字符串"}]}。'
                        "path 必须原样来自输入；不得返回 move 以外的写动作。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            timeout=90.0,
        )
        by_path = {item.path: item for item in candset.items if item.path}
        allowed = set(allowed_dirs)
        max_ops = int((definition.get("limits") or {}).get("max_ops", 10))
        ops: List[Dict[str, Any]] = []
        for raw in data.get("items") or []:
            if not isinstance(raw, Mapping):
                continue
            path = str(raw.get("path") or "")
            item = by_path.get(path)
            if item is None or item.oversized:
                continue
            tags = [
                str(tag).strip().lstrip("#")
                for tag in (raw.get("tags") or [])
                if str(tag).strip()
            ][:2]
            for tag in tags:
                if len(ops) >= max_ops:
                    break
                ops.append({"type": "tag", "path": path, "tag": tag})
            folder = normalize_rel(str(raw.get("folder") or ""))
            if folder in allowed and len(ops) < max_ops:
                ops.append({"type": "move", "path": path, "target": folder})

        return {
            "ops": ops,
            "sources": paths,
            "reason": f"模型为 {len(payload)} 条收件箱笔记建议补标签/归档",
            "rule_only": False,
            "model_usage": {"calls": 1, "input_chars": len(prompt)},
            "summary": {"ai": True, "backlog": candset.summary.get("backlog")},
        }

    def _generate_daily(
        self,
        definition: Mapping[str, Any],
        candset: CandidateSet,
        budget: RunBudget,
        *,
        now: float,
    ) -> Dict[str, Any]:
        out_dir = normalize_rel(self.dirs.get("daily", "daily"))
        date_text = date_label(now, self.tz)
        out_path = f"{out_dir}/{date_text}-知识简报.md"

        if self.reader.read_raw(out_path) is not None:
            return {
                "ops": [],
                "sources": [],
                "reason": "今日简报已存在，按约定不覆盖、不追加",
                "rule_only": True,
                "model_usage": {},
                "summary": {"output_exists": True},
            }

        summary = candset.summary
        if self.provider is None or not getattr(self.provider, "configured", False):
            body = _rule_brief(date_text, summary)
            return {
                "ops": [{"type": "create", "path": out_path, "content": body}],
                "sources": [item.path for item in candset.items if item.path],
                "reason": "未配置生成模型，生成规则版事实清单，仍需人工批准",
                "rule_only": True,
                "model_usage": {},
                "summary": {"output_exists": False},
            }

        prompt = _canonical(
            {
                "date": date_text,
                "changed": summary.get("changed") or [],
                "open_todos": summary.get("open_todos") or [],
                "note_count": summary.get("note_count"),
            }
        )
        budget.charge_model(len(prompt))
        data = chat_json(
            self.provider,
            [
                {
                    "role": "system",
                    "content": (
                        "你是每日知识简报助手。只返回 JSON："
                        '{"body":"Markdown 正文"}。'
                        "只可使用给定事实；不得编造待办，未完成待办必须来自输入。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            timeout=90.0,
        )
        body = str(data.get("body") or "").strip()
        if not body:
            return {
                "ops": [],
                "sources": [],
                "reason": "模型未返回可用正文",
                "rule_only": False,
                "model_usage": {"calls": 1, "input_chars": len(prompt)},
                "summary": {"output_exists": False},
            }
        max_chars = int((definition.get("limits") or {}).get("max_note_chars", 50_000))
        if len(body) > max_chars:
            raise BudgetExceeded(f"简报正文超过单文件上限 {max_chars} 字符")
        return {
            "ops": [{"type": "create", "path": out_path, "content": body + "\n"}],
            "sources": [item.path for item in candset.items if item.path],
            "reason": "模型依据最近 24 小时事实生成简报",
            "rule_only": False,
            "model_usage": {"calls": 1, "input_chars": len(prompt)},
            "summary": {"output_exists": False},
        }

    # ------------------------------------------------------------------ 预览
    def _output_dirs(self, recipe: str) -> Tuple[str, ...]:
        spec = recipe_for(recipe)
        if spec.output_kind == "daily":
            return (normalize_rel(self.dirs.get("daily", "daily")),)
        if spec.output_kind == "report":
            return (normalize_rel(self.dirs.get("report", "reports")),)
        return ()

    def _preview_and_manifest(
        self,
        ops: Sequence[Mapping[str, Any]],
        candset: CandidateSet,
        budget: RunBudget,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        try:
            preview = preview_ops(
                [dict(op) for op in ops], _PreviewVault(Path(self.reader.root))
            )
        except Exception as exc:  # noqa: BLE001 —— 预览失败等价于 ops 不可执行
            raise DutyPolicyError(f"ops 预览失败: {exc}") from exc
        diffs = list(preview.get("diffs") or [])

        entries: List[Dict[str, Any]] = []
        seen: set = set()

        def add(path: str, role: str, digest: Optional[str] = None, mtime: Optional[float] = None) -> None:
            clean = normalize_rel(path)
            if not clean or (clean, role) in seen:
                return
            seen.add((clean, role))
            raw = self.reader.read_raw(clean)
            entries.append(
                {
                    "path": clean,
                    "exists": raw is not None,
                    "raw_sha256": digest
                    if digest is not None
                    else (
                        hashlib.sha256(raw.encode("utf-8")).hexdigest()
                        if raw is not None
                        else None
                    ),
                    "mtime": mtime if mtime is not None else self._mtime(clean),
                    "role": role,
                }
            )

        for candidate in candset.items:
            add(candidate.path, "input", candidate.digest, candidate.mtime)
        for op in ops:
            add(str(op["path"]), "write_target")
            if op.get("type") == "move":
                basename = str(op["path"]).rsplit("/", 1)[-1]
                add(f"{op['target']}/{basename}", "write_target")

        # 链接影响：复用 LinkGraph/LinkRewriter 的索引解析结果，不用另一套正则。
        # 影响文件会随 move 被重写，必须进入 manifest 参与哈希预检。
        if self.links is not None:
            for index, op in enumerate(ops):
                if op.get("type") != "move":
                    continue
                try:
                    affected = list(
                        self.links.affected(str(op["path"]), str(op.get("target") or ""))
                    )
                except Exception:  # noqa: BLE001 —— 预览降级为无影响，不伪造
                    affected = []
                for source in affected:
                    add(source, "link_impact")
                if index < len(diffs):
                    diffs[index]["links_affected"] = len(affected)
                    diffs[index]["affected_links"] = affected[:20]

        unique_paths = {entry["path"] for entry in entries}
        budget.charge_files(len(unique_paths))
        return diffs, entries

    def _mtime(self, path: str) -> Optional[float]:
        try:
            target = Path(self.reader.root).joinpath(*normalize_rel(path).split("/"))
            return float(target.stat().st_mtime)
        except (OSError, ValueError, PathError):
            return None

    def _freshness_risks(self, paths: Sequence[str]) -> Dict[str, Any]:
        """提案相关证据的有效期/替代风险（最多 5 份；读取失败按未声明处理）。"""
        from app.freshness import inspect_sources
        from app.parser.frontmatter import parse_frontmatter

        cache: Dict[str, Optional[Dict[str, Any]]] = {}

        def lookup(rel: str) -> Optional[Dict[str, Any]]:
            if rel in cache:
                return cache[rel]
            raw = self.reader.read_raw(rel)
            if raw is None:
                cache[rel] = None
                return None
            fm, _body = parse_frontmatter(raw)
            cache[rel] = fm
            return fm

        def exists(rel: str) -> bool:
            try:
                return self.reader.read_raw(rel) is not None
            except Exception:  # noqa: BLE001 —— 只读探测失败按不存在
                return False

        try:
            return inspect_sources(
                list(paths), lookup=lookup, exists=exists, as_of=self.now(), limit=5
            )
        except Exception:  # noqa: BLE001 —— 保鲜检查失败不能拖垮提案生成
            logger.warning("提案保鲜检查失败（忽略，不阻断预览）", exc_info=True)
            return {"checked": 0, "total": 0, "truncated": False, "risks": [],
                    "message": "保鲜检查未完成（不影响提案生成）。"}


def _rule_brief(date_text: str, summary: Mapping[str, Any]) -> str:
    """无模型时的事实清单；明确标注规则版，不伪称 AI 已运行。"""
    lines = [
        f"# {date_text} 知识简报",
        "",
        "> 规则版：未配置生成模型，以下仅为事实清单，需人工审阅。",
        "",
    ]
    changed = list(summary.get("changed") or [])
    todos = list(summary.get("open_todos") or [])
    if changed:
        lines.extend(["## 最近变动", ""])
        lines.extend(f"- {title}" for title in changed[:20])
        lines.append("")
    if todos:
        lines.extend(["## 未完成待办", ""])
        lines.extend(f"- [ ] {text}" for text in todos[:20])
        lines.append("")
    if not changed and not todos:
        lines.append("今天没有需要汇总的变化。")
    return "\n".join(lines).rstrip() + "\n"
