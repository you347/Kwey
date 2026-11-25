"""一次性审批凭据（INNOVATION-PLAN §4.9）。

- 只有**桌面主进程原生确认桥**（main.py 的 pywebview js_api）能签发 grant；
  HTTP 端点不提供签发入口，普通 Agent token 无法自行批准。
- grant 绑定 proposal_id / version / proposal_hash / vault_generation，
  默认 60 秒有效，仅可消费一次；消费即删除。
- 纯 ``--server`` 模式没有原生桥 → 前端拿不到 grant → apply 默认关闭。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, Optional

GRANT_TTL_SECONDS = 60.0

# 错误码与 HTTP 对齐：403 无权批准 / 409 库或版本已变化
CODE_FORBIDDEN = 403
CODE_CONFLICT = 409


class GrantError(Exception):
    """审批凭据无效、过期、已使用或与提案版本不匹配。"""

    def __init__(self, message: str, *, code: int = CODE_FORBIDDEN) -> None:
        super().__init__(message)
        self.code = int(code)


class ApprovalGrants:
    """进程内一次性凭据表（线程安全，不落盘）。"""

    def __init__(self, *, clock: Optional[Any] = None) -> None:
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._grants: Dict[str, Dict[str, Any]] = {}

    def _now(self, now: Optional[float]) -> float:
        return float(now) if now is not None else float(self._clock())

    def _prune(self, now: float) -> None:
        expired = [key for key, item in self._grants.items() if item["expires_at"] <= now]
        for key in expired:
            self._grants.pop(key, None)

    def issue(
        self,
        *,
        proposal_id: str,
        version: int,
        proposal_hash: str,
        generation: int,
        ttl: float = GRANT_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> str:
        """签发一次性 grant；调用方必须是桌面原生确认桥。"""
        moment = self._now(now)
        grant = secrets.token_urlsafe(24)
        with self._lock:
            self._prune(moment)
            self._grants[grant] = {
                "proposal_id": str(proposal_id),
                "version": int(version),
                "proposal_hash": str(proposal_hash),
                "generation": int(generation),
                "expires_at": moment + float(ttl),
            }
        return grant

    def consume(
        self,
        grant: str,
        *,
        proposal_id: str,
        version: int,
        proposal_hash: str,
        generation: int,
        now: Optional[float] = None,
    ) -> None:
        """校验并销毁 grant；任何不匹配都抛 :class:`GrantError`。"""
        token = str(grant or "").strip()
        if not token:
            raise GrantError("缺少桌面端一次性审批凭据")
        moment = self._now(now)
        with self._lock:
            self._prune(moment)
            item = self._grants.get(token)
            if item is None:
                raise GrantError("审批凭据无效、已使用或已过期")
            if int(item["generation"]) != int(generation):
                raise GrantError(
                    "审批凭据属于旧 vault 实例，已失效", code=CODE_CONFLICT
                )
            if str(item["proposal_id"]) != str(proposal_id):
                raise GrantError("审批凭据与提案不匹配")
            if int(item["version"]) != int(version):
                raise GrantError("审批凭据与提案版本不匹配", code=CODE_CONFLICT)
            if str(item["proposal_hash"]) != str(proposal_hash):
                raise GrantError("审批凭据与提案哈希不匹配", code=CODE_CONFLICT)
            # 一次性：消费即删除（即便后续预检失败也不可复用）
            self._grants.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()
