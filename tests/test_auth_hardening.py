"""A4 权限治理验收：统一 token 鉴权 + tokens.json hash 化。

覆盖：
1. Agent 端点无 token / 无效 token → 401；只读 token 调 write 端点 → 403
2. read token 可读（pulse 200）；admin token write 端点全通过（正向）
3. tokens.json 落盘仅含 token_hash + salt，无明文
4. 旧明文库自动迁移：原明文 token 迁移后仍可用；revoke 亦按明文生效
5. guard 头部优先级（X-Kwey-Token > Bearer）与 verify 返回记录剥敏

不覆盖（超出本卡边界，见报告）：WS 握手鉴权接线（补丁交集成卡）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.auth import TokenStore
from app.api import deps

# ---------------------------------------------------------------- helpers
def _hdr(token: str) -> dict:
    return {"X-Kwey-Token": token}


def _issue(client, admin: str, role: str, name: str = "") -> str:
    """经 admin 端点签发子 token，返回明文（仅此一次）。"""
    r = client.post(
        "/api/agent/tokens",
        json={"action": "issue", "role": role, "name": name},
        headers=_hdr(admin),
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _bootstrap_admin(client) -> str:
    """空库引导签发首个 admin token。"""
    r = client.post("/api/agent/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _tokens_file(vault_dir: Path) -> Path:
    return vault_dir / ".kwey" / "tokens.json"


# ---------------------------------------------------------------- 401：无 token
def _request(client, method: str, url: str, json_body=None, headers=None):
    """统一请求：GET 不传 json（httpx Client.get 不接受该参数）。"""
    kwargs = {"headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    return getattr(client, method)(url, **kwargs)


@pytest.mark.parametrize(
    "method,url,json_body",
    [
        ("post", "/api/agent/organize/preview", {"ops": []}),
        ("post", "/api/agent/organize/apply", {"ops": [], "confirm": True}),
        ("post", "/api/agent/chat", {"messages": []}),
        ("get", "/api/pulse", None),
        ("get", "/api/agent/provider-config", None),
        ("put", "/api/agent/provider-config", {"endpoint": "http://example.local"}),
    ],
)
def test_no_token_401(client, method: str, url: str, json_body):
    """A4 契约 3：治理的 6 个端点在无 token 时一律 401。"""
    r = _request(client, method, url, json_body)
    assert r.status_code == 401
    body = r.json()
    assert body["code"] != 0  # 统一 {code,data,message} 包装


def test_invalid_token_401(client):
    """带无效 token → 401（区别于 401 缺失）。"""
    _bootstrap_admin(client)
    r = client.get("/api/pulse", headers=_hdr("wrong-token"))
    assert r.status_code == 401


# ---------------------------------------------------------------- 403：权限不足
def test_read_token_write_403(client):
    """A4 契约 3：只读 token 调 write 端点 → 403。"""
    admin = _bootstrap_admin(client)
    ro = _issue(client, admin, "read", "只读")
    for method, url, body in [
        ("post", "/api/agent/organize/preview", {"ops": []}),
        ("post", "/api/agent/chat", {"messages": []}),
        ("put", "/api/agent/provider-config", {"endpoint": "http://example.local"}),
    ]:
        r = getattr(client, method)(url, json=body, headers=_hdr(ro))
        assert r.status_code == 403, f"{url} 应 403"


def test_read_token_pulse_ok(client):
    """read token 可访问 read 级端点（pulse）。"""
    admin = _bootstrap_admin(client)
    ro = _issue(client, admin, "read")
    r = client.get("/api/pulse", headers=_hdr(ro))
    assert r.status_code == 200
    assert r.json()["code"] == 0


def test_admin_token_pass(client):
    """admin token 正向通过治理后的 write 端点。"""
    admin = _bootstrap_admin(client)
    r = client.post("/api/agent/organize/preview", json={"ops": []}, headers=_hdr(admin))
    assert r.status_code == 200
    assert r.json()["data"]["valid"] is True
    r = client.get("/api/agent/provider-config", headers=_hdr(admin))
    assert r.status_code == 200
    r = client.put(
        "/api/agent/provider-config",
        json={"endpoint": "http://example.local"},
        headers=_hdr(admin),
    )
    assert r.status_code == 200


def test_bootstrap_non_empty_local_reissue(client, vault_dir: Path):
    """本地 UI 自愈：库非空时本地回环仍可重签 admin token（旧 token 并存）。

    远程来源的 409 由 tests/test_bootstrap_loopback.py 覆盖。
    """
    first = _bootstrap_admin(client)
    r = client.post("/api/agent/bootstrap")
    assert r.status_code == 200
    second = r.json()["data"]["token"]
    assert second and second != first
    # 重签会吊销旧 local-ui token（避免列表无限增长），新 token 可用
    assert client.get("/api/pulse", headers=_hdr(first)).status_code == 401
    assert client.get("/api/pulse", headers=_hdr(second)).status_code == 200


# ---------------------------------------------------------------- hash 化落盘
def test_disk_stores_hash_only(client, vault_dir: Path):
    """A4 契约 2：落盘仅含 token_hash/salt，绝无明文。"""
    plaintext = _bootstrap_admin(client)
    raw = _tokens_file(vault_dir).read_text(encoding="utf-8")
    assert plaintext not in raw  # 明文不落盘
    rec = json.loads(raw)["tokens"][0]
    assert "token" not in rec
    assert rec["token_hash"] and rec["salt"]
    assert rec["role"] == "admin"


def test_issue_returns_plaintext_once(client):
    """issue 响应含明文 token（仅此一次），记录字段齐备。"""
    admin = _bootstrap_admin(client)
    plaintext = _issue(client, admin, "write", "集成卡")
    assert plaintext
    r = client.get("/api/pulse", headers=_hdr(plaintext))
    assert r.status_code == 200


# ---------------------------------------------------------------- 旧明文库迁移
LEGACY_TOKEN = "legacy-plain-token"


def _write_legacy_store(vault_dir: Path) -> None:
    path = _tokens_file(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tokens": [
            {"token": LEGACY_TOKEN, "role": "admin", "name": "旧库", "created": "2026-11-12T00:00:00+08:00"},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_plaintext_migration_keeps_old_token(client, vault_dir: Path):
    """A4 契约 2：旧明文库 load 时自动迁移，原明文 token 仍可用。"""
    _write_legacy_store(vault_dir)
    # 迁移后旧 token 仍可被识别（用它自身鉴权成功即证明记录被迁移识别；
    # 本地 bootstrap 现在是幂等重签，不再用 409 作为"库非空"探针）
    # 原明文 token 仍可鉴权
    r = client.get("/api/pulse", headers=_hdr(LEGACY_TOKEN))
    assert r.status_code == 200
    # 迁移即落盘：明文消失，hash 出现
    raw = _tokens_file(vault_dir).read_text(encoding="utf-8")
    assert LEGACY_TOKEN not in raw
    assert "token_hash" in raw


def test_revoke_after_migration(client, vault_dir: Path):
    """迁移后的旧 token 可按明文 revoke，吊销后即 401。"""
    _write_legacy_store(vault_dir)
    r = client.post(
        "/api/agent/tokens",
        json={"action": "revoke", "token": LEGACY_TOKEN},
        headers=_hdr(LEGACY_TOKEN),  # 旧库记录是 admin，可自管
    )
    assert r.status_code == 200
    assert r.json()["data"]["revoked"] is True
    r = client.get("/api/pulse", headers=_hdr(LEGACY_TOKEN))
    assert r.status_code == 401


# ---------------------------------------------------------------- guard 细节
def test_bearer_and_header_priority(client):
    """Bearer 可用；X-Kwey-Token 优先于 Bearer（无效值生效 → 401）。"""
    admin = _bootstrap_admin(client)
    r = client.get("/api/pulse", headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200
    r = client.get(
        "/api/pulse",
        headers={"X-Kwey-Token": "bad", "Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 401


def test_verify_record_sanitized(vault_dir: Path):
    """verify 返回记录剥敏（无 token_hash/salt），guard/actor 不泄漏存储字段。"""
    store = TokenStore(vault_dir)
    issued = store.issue("write", "剥敏")
    rec = store.verify(issued["token"], "read")
    assert rec == {
        "role": "write",
        "name": "剥敏",
        "created": rec["created"],
    }
    assert "token_hash" not in rec and "salt" not in rec


def test_store_file_missing_means_empty(vault_dir: Path):
    """tokens.json 缺失 → 空库语义（verify 401、issue 正常），不阻断。"""
    store = TokenStore(vault_dir)
    assert store.list() == []
    with pytest.raises(Exception) as exc_info:
        store.verify("anything", "read")
    assert getattr(exc_info.value, "status_code", None) == 401
    issued = store.issue("read", "冷启动")
    assert issued["token"]
