"""提案应用编排（INNOVATION-PLAN §4.7 / §4.8）。

关键约束：

- 预检、批准抢占与逐项落盘都在 ``VaultService.lock`` 内完成：首项写盘前任何
  manifest 哈希变化 → 整组标 ``stale`` 并停止；已有副作用后再冲突 → ``partial``；
  无法判断落盘结果 → ``uncertain``。绝不静默 rebase。
- 每项先记 ``intent`` 再执行，成功记 ``applied``、确定性拒绝记 ``failed``、
  异常记 ``uncertain``；``applied`` 不可重跑或改判（由 DutyStore 保证）。
- 进程退出时 ``applying`` 由 supervisor 恢复为 ``interrupted``，不会回到 pending。
- 全部写入走 :func:`app.agent.ops.apply_ops` → VaultService 统一写路径；
  链接重写由 VaultService/LinkRewriter 负责，本模块不用正则。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from fastapi import HTTPException

from app.agent.ops import apply_ops
from app.vault.provenance import ActorContext

from app.duty.approval import GrantError

DUTY_APPLY_ACTOR = ActorContext(kind="agent", name="kwey-duty", origin="duty-apply")


class ProposalApplier:
    """单 vault 的提案审批/应用编排。"""

    def __init__(
        self,
        *,
        store: Any,
        vault: Any,
        reader: Any,
        grants: Any,
        runtime: Any,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.reader = reader
        self.grants = grants
        self.runtime = runtime
        self._clock = clock or getattr(store, "_clock", None) or time.time

    # ------------------------------------------------------------------ 预检
    @staticmethod
    def _digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _entry_matches(self, entry: Mapping[str, Any]) -> bool:
        path = str(entry.get("path") or "")
        if not path:
            return False
        raw = self.reader.read_raw(path)
        if bool(entry.get("exists")) != (raw is not None):
            return False
        if raw is None:
            return True
        expected = entry.get("raw_sha256")
        if not expected:
            return True
        return self._digest(raw) == str(expected)

    def manifest_conflicts(self, proposal: Mapping[str, Any]) -> List[str]:
        """当前磁盘状态与提案 manifest 不一致的路径（稳定排序）。"""
        conflicts: List[str] = []
        for entry in proposal.get("manifest") or []:
            if not self._entry_matches(entry):
                path = str(entry.get("path") or "")
                if path and path not in conflicts:
                    conflicts.append(path)
        return conflicts

    # ------------------------------------------------------------------ 摘要
    def _summary(self, proposal: Mapping[str, Any]) -> Dict[str, Any]:
        proposal_id = str(proposal["proposal_id"])
        items = self.store.list_apply_items(proposal_id)
        ops = list(proposal.get("ops") or [])
        failed: List[Dict[str, Any]] = []
        uncertain: List[Dict[str, Any]] = []
        applied = 0
        for item in items:
            if item["status"] == "applied":
                applied += 1
            elif item["status"] in ("failed", "uncertain"):
                index = int(item["op_index"])
                op = ops[index] if 0 <= index < len(ops) else {}
                entry = {
                    "path": str(op.get("path") or ""),
                    "op": str(op.get("type") or ""),
                    "status": item["status"],
                    "error": str(item.get("error") or ""),
                }
                (failed if item["status"] == "failed" else uncertain).append(entry)
        status = str(proposal.get("status") or "")
        if status == "applied":
            message = "已按提案版本应用，全部写入经 VaultService 统一写路径"
        elif status == "partial":
            message = "部分应用：请查看未完成项，未自动回滚"
        elif status == "uncertain":
            message = "结果不确定：请对照实际文件确认，禁止自动重跑"
        elif status == "rejected":
            message = "已拒绝，未修改任何笔记"
        else:
            message = "提案当前状态不可应用"
        return {
            "status": status,
            "applied": applied,
            "total": len(ops),
            "failed_items": failed,
            "uncertain_items": uncertain,
            "failed_count": len(failed),
            "uncertain_count": len(uncertain),
            "message": message,
        }

    # ------------------------------------------------------------------ 拒绝
    def reject(
        self,
        proposal_id: str,
        *,
        version: int,
        reason: str = "",
        actor: str = "local-ui",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        moment = self._now(now)
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")
        if str(proposal["status"]) != "pending":
            raise HTTPException(status_code=409, detail="提案不是待审状态，不能拒绝")
        if int(proposal["version"]) != int(version):
            raise HTTPException(status_code=409, detail="提案版本已变化，请重新载入")
        if float(proposal["expires_at"]) <= moment:
            self.store.transition_proposal(proposal_id, "expired", expected="pending")
            raise HTTPException(status_code=410, detail="提案已过期（410）")
        self.store.record_decision(
            proposal_id,
            version=int(version),
            decision="reject",
            actor=actor,
            reason=reason,
            now=moment,
        )
        fresh = self.store.get_proposal(proposal_id) or proposal
        return self._summary(fresh)

    # ------------------------------------------------------------------ 应用
    def apply(
        self,
        proposal_id: str,
        *,
        version: int,
        proposal_hash: str,
        grant: str,
        actor: str = "local-ui",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        moment = self._now(now)
        with self.vault.lock:
            proposal = self.store.get_proposal(proposal_id)
            if proposal is None:
                raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")
            status = str(proposal["status"])

            # 终态幂等：重复批准返回既有状态，绝不再次执行。
            if status != "pending":
                if status in ("applied", "partial", "uncertain", "interrupted"):
                    decision = self.store.get_decision(proposal_id)
                    if decision and str(decision.get("decision")) == "approve":
                        return self._summary(proposal)
                if status == "expired":
                    raise HTTPException(status_code=410, detail="提案已过期（410）")
                raise HTTPException(
                    status_code=409, detail=f"提案当前状态 {status} 不可批准"
                )

            if int(proposal["version"]) != int(version):
                raise HTTPException(status_code=409, detail="提案版本已变化，请重新载入")
            if proposal_hash and str(proposal["proposal_hash"]) != str(proposal_hash):
                raise HTTPException(status_code=409, detail="提案哈希已变化，请重新载入")
            if float(proposal["expires_at"]) <= moment:
                try:
                    self.store.transition_proposal(proposal_id, "expired", expected="pending")
                except Exception:  # noqa: BLE001 —— 并发下已过期即可
                    pass
                raise HTTPException(status_code=410, detail="提案已过期（410）")

            # 首项落盘前的整组预检：任何变化 → stale，不静默合并。
            conflicts = self.manifest_conflicts(proposal)
            if conflicts:
                try:
                    self.store.transition_proposal(proposal_id, "stale", expected="pending")
                except Exception:  # noqa: BLE001
                    pass
                sample = "、".join(conflicts[:5])
                raise HTTPException(
                    status_code=409,
                    detail=f"提案基准已变化（409）：{sample}；请重新预览后再批准",
                )

            try:
                self.grants.consume(
                    grant,
                    proposal_id=proposal_id,
                    version=int(version),
                    proposal_hash=str(proposal["proposal_hash"]),
                    generation=int(self.runtime.generation),
                    now=moment,
                )
            except GrantError as exc:
                raise HTTPException(status_code=exc.code, detail=str(exc)) from exc

            try:
                claimed = self.store.record_decision(
                    proposal_id,
                    version=int(version),
                    decision="approve",
                    actor=actor,
                    reason="",
                    now=moment,
                )
            except Exception as exc:  # DutyConflictError / DutyStateError
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not claimed.get("created"):
                fresh = self.store.get_proposal(proposal_id) or proposal
                return self._summary(fresh)

            return self._execute(proposal, now=moment)

    # ------------------------------------------------------------------ 逐项执行
    def _execute(self, proposal: Mapping[str, Any], *, now: float) -> Dict[str, Any]:
        proposal_id = str(proposal["proposal_id"])
        run_id = str(proposal.get("run_id") or "")
        ops = list(proposal.get("ops") or [])
        manifest = list(proposal.get("manifest") or [])
        by_path: Dict[str, List[Mapping[str, Any]]] = {}
        for entry in manifest:
            by_path.setdefault(str(entry.get("path") or ""), []).append(entry)
        link_paths = {
            str(entry.get("path") or "")
            for entry in manifest
            if str(entry.get("role") or "") == "link_impact"
        }

        mutated: set = set()
        failed: List[Dict[str, Any]] = []
        uncertain: List[Dict[str, Any]] = []
        applied = 0

        for index, op in enumerate(ops):
            op_type = str(op.get("type") or "")
            path = str(op.get("path") or "")
            self.store.record_apply_item(
                proposal_id, index, status="intent", now=now
            )
            try:
                conflict = self._op_conflict(op, by_path, mutated)
                if conflict:
                    raise HTTPException(
                        status_code=409,
                        detail=f"文件在预览后已变化: {conflict}",
                    )
                result = apply_ops(
                    [dict(op)], self.vault, actor=DUTY_APPLY_ACTOR, run_id=run_id
                )
                paths = [str(p) for p in (result.get("paths") or [])]
                self.store.record_apply_item(
                    proposal_id,
                    index,
                    status="applied",
                    result_paths=paths,
                    now=self._now(None),
                )
                applied += 1
                mutated.add(path)
                mutated.update(paths)
                if op_type == "move":
                    basename = path.rsplit("/", 1)[-1]
                    target = str(op.get("target") or "")
                    mutated.add(f"{target}/{basename}" if target else basename)
                    # 同组 move 会重写全部 link_impact 文件，后续项不再用旧哈希校验。
                    mutated.update(link_paths)
            except HTTPException as exc:
                self.store.record_apply_item(
                    proposal_id,
                    index,
                    status="failed",
                    error=str(exc.detail)[:500],
                    now=self._now(None),
                )
                failed.append(
                    {
                        "path": path,
                        "op": op_type,
                        "status": "failed",
                        "error": str(exc.detail)[:500],
                    }
                )
                break
            except Exception as exc:  # noqa: BLE001 —— 无法判断落盘结果
                self.store.record_apply_item(
                    proposal_id,
                    index,
                    status="uncertain",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    now=self._now(None),
                )
                uncertain.append(
                    {
                        "path": path,
                        "op": op_type,
                        "status": "uncertain",
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
                break

        if uncertain:
            final = "uncertain"
        elif failed:
            final = "partial"
        else:
            final = "applied"
        try:
            self.store.transition_proposal(
                proposal_id, final, expected="applying", now=self._now(None)
            )
        except Exception:  # noqa: BLE001 —— 终态已由并发写入
            pass
        fresh = self.store.get_proposal(proposal_id) or proposal
        result = self._summary(fresh)
        result["applied"] = applied
        result["failed_items"] = failed
        result["uncertain_items"] = uncertain
        result["failed_count"] = len(failed)
        result["uncertain_count"] = len(uncertain)
        # P2-D：批准时重检来源有效期/替代关系；只提示，不自动撤销旧事实。
        result["freshness"] = self._freshness_check(proposal)
        return result

    def _freshness_check(self, proposal: Mapping[str, Any]) -> Dict[str, Any]:
        from app.freshness import inspect_sources

        db = getattr(self.vault, "db", None)
        paths = [str(p) for p in (proposal.get("sources") or []) if p]
        if db is None or not paths:
            return {"checked": 0, "total": len(paths), "truncated": False, "risks": [],
                    "message": "未发现明确的失效声明。"}
        try:
            return inspect_sources(paths, db=db, limit=5)
        except Exception:  # noqa: BLE001 —— 未检查不能冒充无冲突
            return {"checked": 0, "total": len(paths), "truncated": False, "risks": [],
                    "message": "保鲜检查未完成；请人工核对来源有效期。"}

    def _op_conflict(
        self,
        op: Mapping[str, Any],
        by_path: Mapping[str, List[Mapping[str, Any]]],
        mutated: set,
    ) -> str:
        """逐项落盘前的文件状态检查；同组已合法修改的文件跳过。"""
        op_type = str(op.get("type") or "")
        path = str(op.get("path") or "")
        if path and path not in mutated:
            for entry in by_path.get(path, []):
                if str(entry.get("role") or "") in ("input", "write_target"):
                    if not self._entry_matches(entry):
                        return path
        if op_type in ("create", "move"):
            target = (
                f"{op.get('target')}/{path.rsplit('/', 1)[-1]}"
                if op_type == "move" and op.get("target")
                else path
            )
            if target and target not in mutated:
                if self.reader.read_raw(target) is not None:
                    return target
        return ""

    def _now(self, now: Optional[float]) -> float:
        return float(now) if now is not None else float(self._clock())
