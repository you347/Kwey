"""写入溯源（B）：读视图 + 人工审阅。

- ``GET  /api/provenance/note?path=...``：返回派生审阅状态、本机基线与 frontmatter
  声明；声明未校验时 ``claim_unverified=true``，仍保持 unknown。
- ``POST /api/provenance/review``：提交 ``path + expected_payload_hash``；服务端重新
  读盘计算当前 payload hash，不符返回 409（内容已变化，须刷新重审）。

审阅只写 ``kwey_reviewed_*`` 管理字段，且一律经 VaultService 统一写路径；
普通保存不会被当成审阅。审阅不改变原写入者（writer_kind/last_writer/run_id/
generated 保留），本机约定不是抵抗本机恶意调用的认证。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api import deps

router = APIRouter(prefix="/api/provenance", tags=["provenance"])


def _ok(data) -> dict:
    return {"code": 0, "data": data, "message": "ok"}


@router.get("/note")
async def provenance_note(path: str = Query(..., description="vault 相对正斜杠路径")):
    vault = deps.require_vault()
    return _ok(vault.provenance_info(path))


class ReviewBody(BaseModel):
    path: str
    expected_payload_hash: str


@router.post("/review")
async def provenance_review(body: ReviewBody):
    vault = deps.require_vault()
    result = vault.review_note(
        body.path, expected_payload_hash=body.expected_payload_hash
    )
    return _ok(vault.provenance_info(result["path"]))
