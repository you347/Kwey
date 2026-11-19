"""Kwey 内置智能体内核（卡B）：驱动 LLM 经能力契约 registry.invoke 操作笔记。

设计要点
--------
- 能力真源仍是 :mod:`app.agent.registry`；内核只做 LLM ↔ 工具编排，不新增能力语义。
- tool-calling 循环：system + 用户消息 → ``provider.chat(tools)`` → 执行工具 →
  回灌结果 → 迭代，直到模型给出最终答复或达到步数上限。
- 危险操作 confirm 拦截：``CONFIRM_TOOLS`` 一律不自动确认（即便 LLM 自填 confirm=true
  也忽略），发 ``confirm_required`` 事件暂停；用户在 UI 批准后经 ``resume`` 续跑。
  服务端无状态：对话历史由客户端携带，resume 只带被批准工具调用的 id。
- actor 固定 ``kwey-built-in/admin``（进程内 trusted，危险操作仍由 UI confirm 把关）；
  journal 留痕经 registry.invoke 收口，全归因 kwey-built-in。
- 工具名净化：OpenAI function name 不允许 ``.``，对 LLM 暴露 ``kwey_search``，
  回调时经 name_map 映射回真名 ``kwey.search``。
- 零新依赖：复用 AgentProvider（urllib）。ProviderError 绝不回显 api_key。
- 同步生成器：路由用 StreamingResponse 包裹，Starlette 在线程池迭代，不阻塞事件循环。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.agent import registry
from app.agent import feedback
from app.agent.provider import AgentProvider, ProviderError
from app.paths import PathError, validate_relative
from app.api import deps
from app.vault.service import VaultService

BUILTIN_ACTOR = {"role": "admin", "name": "kwey-built-in"}
MAX_STEPS = 8

# 危险 / 结构性操作：必须经 UI 用户确认，内核绝不自动放行。
CONFIRM_TOOLS = {
    "kwey.history.rollback",
    "kwey.trash.purge",
    "kwey.organize.apply",
    "kwey.dir.delete",
}

SYSTEM_PROMPT = (
    "你是 Kwey 本地知识库的内置智能体，与用户共同读写笔记。"
    "你通过工具操作 vault：搜索、阅读、新建、改写、追加、加链、改标签、移动、重命名、"
    "目录管理、回收站、快照历史与结构化整理。"
    "约定：路径一律 vault 相对正斜杠；写操作走统一写路径，删除进回收站可恢复。"
    "回滚、永久删除、整理应用、删目录属危险操作，系统会要求用户确认，你无需也不得自行确认。"
    "先用搜索 / 阅读了解现状再动手；一次只做用户要求的变更；完成后用简洁中文总结结果。"
    "用户说「打开 / 看看 / 展示 / 定位」某个笔记或目录时，调用 kwey.open 让界面呈现，"
    "不要用 kwey.read 把正文复述给用户；只有用户明确要求总结、解释或改写内容时才输出正文摘录。"
    "所有操作尽量落到界面（打开笔记、定位目录、刷新列表），回复保持一两句话，不复述工具返回的全文。"
)


# ---------------------------------------------------------------- 工具名净化
def _llm_name(name: str) -> str:
    return name.replace(".", "_")


def _tools_for_llm() -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """契约 → OpenAI function 工具列表 + llm_name→真名 映射。"""
    tools: List[Dict[str, Any]] = []
    name_map: Dict[str, str] = {}
    for t in registry.contract()["tools"]:
        ln = _llm_name(t["name"])
        name_map[ln] = t["name"]
        tools.append({
            "type": "function",
            "function": {
                "name": ln,
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return tools, name_map


# ---------------------------------------------------------------- 消息格式 helpers
def _wire_assistant(norm: Dict[str, Any]) -> Dict[str, Any]:
    """规范化 assistant 消息 → OpenAI wire 格式（arguments 序列化为 JSON 字符串）。"""
    msg: Dict[str, Any] = {"role": "assistant", "content": norm.get("content")}
    calls = norm.get("tool_calls") or []
    if calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                },
            }
            for c in calls
        ]
    return msg


def _wire_calls(assistant: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 wire assistant 消息解析出规范化 tool_calls（arguments → dict）。"""
    out: List[Dict[str, Any]] = []
    for tc in assistant.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, str) and raw.strip():
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw, dict):
            args = raw
        else:
            args = {}
        out.append({"id": tc.get("id") or "", "name": fn.get("name") or "", "arguments": args})
    return out


def _last_pending(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """最近一条带 tool_calls 的 assistant 消息中，尚未回灌结果的调用（规范化）。"""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {
                x.get("tool_call_id")
                for x in msgs[i + 1:]
                if x.get("role") == "tool"
            }
            return [c for c in _wire_calls(m) if c["id"] not in answered]
    return []


def _tool_message(call_id: str, ok: bool, data: Any = None, error: str = "") -> Dict[str, Any]:
    payload = {"code": 0 if ok else 1, "data": data if ok else None, "message": "ok" if ok else error}
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _context_hint(vault: Optional[VaultService], context: Optional[Dict[str, Any]]) -> str:
    """把前端「当前工作区」翻译成一句指代约定；路径非法 / 不存在时静默忽略。"""
    if vault is None or not isinstance(context, dict):
        return ""
    raw = context.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        rel = validate_relative(raw)
    except PathError:
        return ""
    if not rel:
        return ""
    root = vault.paths.root.resolve()
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        return ""
    if target.is_dir():
        return (
            f"【当前工作区】用户正在查看文件夹「{rel}/」。"
            "用户说「这里 / 这个文件夹 / 这些笔记」时默认指该目录；"
            f"检索先用 kwey.search 的 path:{rel} 操作符限定范围，新建笔记默认放这里。"
        )
    if target.is_file():
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        where = f"（所在目录：{parent}/）" if parent else "（vault 根目录）"
        return (
            f"【当前工作区】用户正在查看笔记「{rel}」{where}。"
            "用户说「这个 / 这篇 / 这里 / 当前文件」时默认指它；"
            "需要正文先用 kwey.read 读它，改写走 kwey.update / kwey.append，不要反问路径。"
        )
    return ""


def _system_prompt(
    vault: Optional[VaultService] = None,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """基础约定 + 当前工作区 + 用户反馈摘要（点赞/点踩调教，vault 级持久化）。"""
    if vault is None:
        return SYSTEM_PROMPT
    parts = [SYSTEM_PROMPT, _context_hint(vault, context)]
    try:
        parts.append(feedback.prompt_hint(vault.paths.root))
    except Exception:  # noqa: BLE001 反馈文件异常绝不能挡住对话
        pass
    return "\n".join(part for part in parts if part)


def _ui_intent(real: str, args: Dict[str, Any], data: Any) -> Optional[Dict[str, Any]]:
    """把「打开 / 新建」映射成前端可执行的界面动作。"""
    if not isinstance(data, dict):
        return None
    if real == "kwey.open":
        path = str(data.get("path") or args.get("path") or "")
        if not path:
            return None
        return {"action": "open", "path": path, "kind": data.get("kind") or "note"}
    if real == "kwey.create":
        path = str(data.get("path") or args.get("path") or "")
        if not path:
            return None
        return {"action": "open", "path": path, "kind": "note"}
    return None


def _ensure_system(
    messages: List[Dict[str, Any]],
    vault: Optional[VaultService] = None,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """总在最前面放一条最新的 system：丢弃客户端旧 system，避免反馈/上下文过期。"""
    msgs = [
        dict(m) for m in messages
        if isinstance(m, dict) and m.get("role") != "system"
    ]
    msgs.insert(0, {"role": "system", "content": _system_prompt(vault, context)})
    return msgs


# ---------------------------------------------------------------- 上下文预算
# CLC 最小落地：客户端 messages 是不可变账本；这里只生成「派生视图」给模型，
# 不改账本、不做摘要；省略事实写进派生 system，并向前端发一次 context_trim。
HISTORY_CHAR_BUDGET = 24_000
OMISSION_NOTE = (
    "\n【上下文】较早的 {dropped} 条消息已省略；"
    "需要旧事实请用 kwey.search / kwey.read 重新获取，不要臆测。"
)


def _msg_chars(msg: Dict[str, Any]) -> int:
    """粗略字符数（content + tool_calls 的 JSON），用于确定性预算。"""
    content = msg.get("content")
    size = len(content) if isinstance(content, str) else len(str(content or ""))
    calls = msg.get("tool_calls")
    if calls:
        size += len(json.dumps(calls, ensure_ascii=False))
    return size


def _with_omission_note(system: Dict[str, Any], dropped: int) -> Dict[str, Any]:
    head = dict(system)  # 复制，绝不改账本里的 system
    head["content"] = str(head.get("content") or "") + OMISSION_NOTE.format(dropped=dropped)
    return head


def _compile_history(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """超预算时按 user 边界裁剪，返回派生视图；未超预算原样返回同一对象。

    - 只在 user 消息处切，assistant.tool_calls 与其后的 tool 结果天然同进同出；
    - 至少保留最后一个 user 回合，即使它本身超预算；
    - system 恒在首位，并在裁剪时声明省略条数。
    """
    if not msgs:
        return msgs
    total = sum(_msg_chars(m) for m in msgs)
    if total <= HISTORY_CHAR_BUDGET:
        return msgs

    system, rest = msgs[0], msgs[1:]
    user_idx = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    if not user_idx:
        kept = rest[-3:]
        dropped = len(msgs) - 1 - len(kept)
        return [_with_omission_note(system, dropped)] + kept

    keep_from = user_idx[-1]
    kept_chars = sum(_msg_chars(m) for m in rest[keep_from:])
    for idx in reversed(user_idx[:-1]):
        seg_chars = sum(_msg_chars(m) for m in rest[idx:keep_from])
        if kept_chars + seg_chars > HISTORY_CHAR_BUDGET:
            break
        keep_from = idx
        kept_chars += seg_chars

    kept = rest[keep_from:]
    dropped = len(msgs) - 1 - len(kept)
    return [_with_omission_note(system, dropped)] + kept


# ---------------------------------------------------------------- 主循环
def run_chat(
    messages: List[Dict[str, Any]],
    vault: Optional[VaultService] = None,
    resume: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """执行一轮内置智能体对话，yield SSE 步骤事件。

    ``context={"path", "kind"}`` 为前端上报的当前工作区（打开中的笔记 / 文件夹），
    仅拼进 system 供模型解析「这个 / 这篇 / 这里」，不影响鉴权与工具权限。
    事件 type：tool_call / tool_result / ui / confirm_required / message / error / done。
    ``resume={"id": 被批准工具调用 id}`` 时先强制执行该调用（confirm=true），再续跑。
    危险操作暂停时 done 事件携带 pending，客户端批准后原样回传 messages + resume。
    """
    service = vault if vault is not None else deps.require_vault()
    provider = AgentProvider(service.paths.ensure_root())
    tools, name_map = _tools_for_llm()
    msgs = _ensure_system(messages, service, context)

    def execute(call: Dict[str, Any], force_confirm: bool = False) -> Iterator[Dict[str, Any]]:
        real = name_map.get(call["name"], call["name"])
        args = dict(call["arguments"])
        if force_confirm:
            args["confirm"] = True
        yield {"type": "tool_call", "id": call["id"], "name": real, "arguments": args}
        try:
            data = registry.invoke(real, args, vault=service, actor=BUILTIN_ACTOR)
            ok, error = True, ""
        except Exception as exc:  # noqa: BLE001 业务/契约异常回灌给模型，不中断对话
            data, ok, error = None, False, str(getattr(exc, "detail", None) or getattr(exc, "message", None) or exc)
        msgs.append(_tool_message(call["id"], ok, data=data, error=error))
        yield {"type": "tool_result", "id": call["id"], "name": real, "ok": ok, "data": data, "error": error}
        # 界面意图：让前端把结果落到产品里，而不是让模型用文字复述
        intent = _ui_intent(real, args, data) if ok else None
        if intent:
            yield {"type": "ui", **intent}

    # resume：强制执行用户批准的危险调用，跳过闸门
    if resume and resume.get("id"):
        pending = _last_pending(msgs)
        target = next((c for c in pending if c["id"] == resume["id"]), None)
        if target is not None:
            yield from execute(target, force_confirm=True)

    provider_calls = 0
    trim_reported = False
    while True:
        pending = _last_pending(msgs)
        if pending:
            for call in pending:
                real = name_map.get(call["name"], call["name"])
                if real in CONFIRM_TOOLS:
                    yield {
                        "type": "confirm_required",
                        "id": call["id"],
                        "name": real,
                        "arguments": call["arguments"],
                        "reason": "危险操作需要用户确认",
                    }
                    yield {
                        "type": "done",
                        "messages": msgs,
                        "pending": {"id": call["id"], "name": real, "arguments": call["arguments"]},
                    }
                    return
                yield from execute(call)
            continue

        if provider_calls >= MAX_STEPS:
            yield {"type": "error", "message": f"达到最大步数上限（{MAX_STEPS}），已停止"}
            yield {"type": "done", "messages": msgs, "pending": None}
            return
        provider_calls += 1
        # 账本 msgs 不动；只把派生视图交给模型（providers 侧不感知裁剪）
        wire = _compile_history(msgs)
        if not trim_reported and len(wire) < len(msgs):
            trim_reported = True
            yield {
                "type": "context_trim",
                "dropped_messages": len(msgs) - len(wire),
                "kept_chars": sum(_msg_chars(m) for m in wire),
                "budget": HISTORY_CHAR_BUDGET,
            }
        try:
            norm = provider.chat(wire, tools)
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done", "messages": msgs, "pending": None}
            return
        msgs.append(_wire_assistant(norm))
        if not norm.get("tool_calls"):
            yield {"type": "message", "content": norm.get("content") or ""}
            yield {"type": "done", "messages": msgs, "pending": None}
            return