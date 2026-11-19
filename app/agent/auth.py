"""Agent Link token 三级鉴权（M2 / T11；A4 权限治理改造）。

TokenStore 持久化到 ``<vault>/.kwey/tokens.json``。A4 起磁盘仅存 hash，记录结构：

    {"tokens": [{"token_hash", "salt", "role", "name", "created"}, ...]}

- ``token_hash = sha256(salt + token)``（hexdigest）；明文 token 仅在 issue 响应中出现
  一次，磁盘与 ``list()`` 均不回传明文。
- 读取时自动迁移旧明文库（``migrate_plaintext``）：逐条生成独立 salt 转换为 hash 记录
  并原子回写；迁移后原明文 token 仍可通过 verify（同算法可复算）。
- 原子写（同目录临时文件 + os.replace）；文件缺失 / 损坏 / 结构异常一律视为空库，
  不阻断启动，也不抛异常冒泡到 lifespan。
- 所有实例共享一把模块级 RLock：verify 每次读盘（触发惰性迁移），issue/revoke 串行化，
  「外部直接 new 一个 TokenStore 引导签发」与「路由内实例」天然看到同一份数据。
- 角色等级 read < write < admin；无 / 无效 token → 401，权限不足 → 403。
- 错误统一抛 fastapi.HTTPException，由 server.py 包装为 {code, data, message}。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

# 角色等级：数值越大权限越高
ROLE_LEVEL: Dict[str, int] = {"read": 1, "write": 2, "admin": 3}

# 跨实例共享锁：任意线程 / 任意 TokenStore 实例的读写都串行化，保证原子写不丢更新。
_LOCK = threading.RLock()

TOKENS_DIRNAME = ".kwey"
TOKENS_FILENAME = "tokens.json"

_SALT_BYTES = 16  # 每条记录独立 salt（token_urlsafe 等长随机）


def _hash_token(salt: str, token: str) -> str:
    """sha256(salt + token) hexdigest——存储与校验共用的唯一算法。"""
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


class TokenStore:
    """vault 内的 agent 访问令牌库（A4 起磁盘仅存 hash + salt）。

    无内存态：所有数据以磁盘 ``<root>/.kwey/tokens.json`` 为准，每次操作都重新读盘。
    ``.kwey/`` 位于 paths.IGNORED_DIRS，token 文件与临时文件均不会触发 watcher 索引。
    """

    def __init__(self, root: Path) -> None:
        self.root: Path = Path(root)
        self.path: Path = self.root / TOKENS_DIRNAME / TOKENS_FILENAME

    # ------------------------------------------------------------------ migration
    @staticmethod
    def migrate_plaintext(items: List[Any]) -> Tuple[List[Dict[str, Any]], bool]:
        """旧明文记录 → hash 记录（纯转换，不落盘）。

        - 新格式（token_hash + salt 齐备）原样保留；
        - 旧格式（含明文 token 字段）逐条生成独立 salt 并 hash 化；
        - 结构残缺（无 role / 无 token_hash 且无 token）记录直接丢弃；
        - 返回 (记录列表, 是否发生了迁移)。
        """
        result: List[Dict[str, Any]] = []
        migrated = False
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if not isinstance(role, str) or role not in ROLE_LEVEL:
                continue  # 未知角色无法参与分级，丢弃防污染
            name = item.get("name")
            created = item.get("created")
            base = {
                "role": role,
                "name": name if isinstance(name, str) else "",
                "created": created if isinstance(created, str) else "",
            }
            salt, token_hash = item.get("salt"), item.get("token_hash")
            if isinstance(salt, str) and salt and isinstance(token_hash, str) and token_hash:
                result.append({**base, "salt": salt, "token_hash": token_hash})
                continue
            plaintext = item.get("token")
            if isinstance(plaintext, str) and plaintext:
                new_salt = secrets.token_hex(_SALT_BYTES)
                result.append({
                    **base,
                    "salt": new_salt,
                    "token_hash": _hash_token(new_salt, plaintext),
                })
                migrated = True
        return result, migrated

    # ------------------------------------------------------------------ io
    def _load(self) -> List[Dict[str, Any]]:
        """读取令牌库；缺失 / 损坏 / 结构异常一律返回空列表。

        命中旧明文记录时惰性迁移并原子回写（调用方均持 _LOCK，RLock 可重入）。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):  # JSONDecodeError、UnicodeDecodeError 均属 ValueError
            return []
        items = raw.get("tokens") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        records, migrated = self.migrate_plaintext(items)
        if migrated:
            self._save(records)  # 迁移即落盘：明文从此不再存在于磁盘
        return records

    def _save(self, records: List[Dict[str, Any]]) -> None:
        """原子写：同目录临时文件 + os.replace（同分区替换，Windows 亦原子）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"tokens": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ api
    def issue(self, role: str, name: str = "") -> Dict[str, Any]:
        """签发令牌。role 必须是 read / write / admin 之一。

        返回记录含**明文 token**（仅此一次，磁盘只存 hash）；调用方务必回传给用户。
        """
        if role not in ROLE_LEVEL:
            raise HTTPException(
                status_code=400,
                detail=f"非法角色: {role}（仅 read/write/admin）",
            )
        plaintext = secrets.token_urlsafe(24)
        salt = secrets.token_hex(_SALT_BYTES)
        record: Dict[str, Any] = {
            "salt": salt,
            "token_hash": _hash_token(salt, plaintext),
            "role": role,
            "name": name or "",
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with _LOCK:
            records = self._load()
            records.append(record)
            self._save(records)
        return {
            "token": plaintext,
            "role": record["role"],
            "name": record["name"],
            "created": record["created"],
        }

    def revoke(self, token: str) -> bool:
        """吊销令牌（入参为明文 token）；返回是否确实删除了记录。

        校验用 sha256(salt+token) 与 token_hash 的 compare_digest，防时序侧信道。
        """
        candidate = token or ""
        with _LOCK:
            records = self._load()
            kept = [
                r for r in records
                if not secrets.compare_digest(
                    _hash_token(r["salt"], candidate), r["token_hash"]
                )
            ]
            if len(kept) == len(records):
                return False
            self._save(kept)
            return True

    def revoke_by_name(self, name: str) -> int:
        """吊销所有指定名称的令牌，返回吊销条数（本地 UI 重签前清理旧 local-ui）。"""
        with _LOCK:
            records = self._load()
            kept = [r for r in records if r.get("name") != name]
            removed = len(records) - len(kept)
            if removed:
                self._save(kept)
            return removed

    def list(self) -> List[Dict[str, Any]]:
        """列出全部令牌（脱敏）。

        A4 起磁盘无明文，返回 ``{"id","role","name","created"}``：
        ``id`` 为 token_hash 前 8 位（hex 不可逆，仅用于 UI 展示区分与核对）。
        revoke 需用户自持的明文 token，管理端不回传。
        """
        with _LOCK:
            records = self._load()
        return [
            {
                "id": r["token_hash"][:8],
                "role": r["role"],
                "name": r["name"],
                "created": r["created"],
            }
            for r in records
        ]

    def verify(self, token: str, required_role: str) -> Dict[str, Any]:
        """校验令牌与角色等级，返回**脱敏**记录 ``{"role","name","created"}``。

        - 无 / 空 token、或令牌库中找不到 → 401
        - 角色等级低于 required_role → 403
        - required_role 非法时按最高级处理，保守拒绝（内部调用均传合法值）
        """
        if not token:
            raise HTTPException(
                status_code=401,
                detail="缺少访问 token（X-Kwey-Token 或 Authorization: Bearer <token>）",
            )
        with _LOCK:
            records = self._load()
        matched: Optional[Dict[str, Any]] = None
        for rec in records:
            digest = _hash_token(rec["salt"], token)
            if secrets.compare_digest(digest, rec["token_hash"]):
                matched = rec
                break
        if matched is None:
            raise HTTPException(status_code=401, detail="访问 token 无效")
        need = ROLE_LEVEL.get(required_role, max(ROLE_LEVEL.values()))
        level = ROLE_LEVEL.get(matched["role"], 0)
        if level < need:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：当前 {matched['role']}，需要 {required_role} 及以上",
            )
        # 剥敏返回：调用方（actor / 响应体）只见 role/name/created，不携带 hash/salt
        return {
            "role": matched["role"],
            "name": matched["name"],
            "created": matched["created"],
        }
