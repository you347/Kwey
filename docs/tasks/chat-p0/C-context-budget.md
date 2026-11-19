# 任务卡 C：上下文预算（`kwey.read` 截断续读 + 历史编译）

> 你是 Kwey 项目的执行子智能体。只做本卡范围，不改其它卡的文件。整份读完再动手。

## 背景

内置智能体每轮把**完整历史**回传服务端，服务端再原样交给模型（`app/api/routes_chat.py` → `kernel.run_chat`）；`kwey.read` 返回整篇正文、无上限（`app/agent/registry.py` 的 `_read`）。两个乘起来：一篇长笔记 + 十几轮对话就会超模型上下文，表现为报错或模型开始丢前置约束。

本卡按 CLC（守恒账本式上下文）的最小落地做三件事：

1. **账本不可变**：客户端携带的 `messages` 是账本，只读不改；压缩只生成「派生视图」。
2. **工具结果不裸回流**：`kwey.read` 默认截断并声明 `truncated`，模型可按 offset 续读。
3. **省略可见**：历史被裁剪时，派生视图的 system 明确声明「较早消息已省略」，并向前端发一个 `context_trim` 事件。

不做 LLM 摘要、不引入摘要套摘要、不加依赖。

## 先读

1. `app/agent/registry.py`：`_req_str`、`_read`、`kwey.read` 注册块、`ContractError`
2. `app/agent/kernel.py`：`run_chat()` 主循环、`done` 事件、`_ensure_system`
3. `app/api/routes_chat.py`：确认它只是转发 `messages`，本卡不改
4. `docs/AGENT-CONTRACT.md`：`kwey.read` 行
5. `tests/test_contract.py`：read 的既有断言（不要破坏）

## 目标（可验收）

- 读 12000 字笔记：默认只回 8000 字，`truncated=true`、`total_chars=12000`；用 `offset=8000` 能拿到剩余部分。
- 30 轮、每轮 2000 字的历史：发给模型的派生视图 ≤ 24000 字，仍以 system 开头、第一条非 system 是 user、最后一条是当前请求。
- `done.messages` 仍返回**完整账本**（不因裁剪丢历史）。
- 发生裁剪时 SSE 多一个 `{"type":"context_trim", ...}` 事件；前端现有 switch 的 default 会忽略，无需改前端。

## 实现要求

### 1. `kwey.read` 截断续读（`app/agent/registry.py`）

新增一个模块内小助手（不要做成通用工具库）：

```python
def _opt_int(a: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = a.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(400, f"{key} 必须是整数")
    if value < lo or value > hi:
        raise ContractError(400, f"{key} 超出范围 {lo}-{hi}")
    return value
```

改写 `_read`：

```python
def _read(vault: VaultService, a: Dict[str, Any]) -> Dict[str, Any]:
    doc = vault.read_note(_req_str(a, "path"))
    content = doc["content"]
    total = len(content)
    offset = min(_opt_int(a, "offset", 0, 0, 1_000_000), total)
    limit = _opt_int(a, "max_chars", 8000, 1, 40000)
    end = min(total, offset + limit)
    doc["content"] = content[offset:end]
    doc["offset"] = offset
    doc["total_chars"] = total
    doc["truncated"] = end < total
    return doc
```

注册块更新：

- description 改为：`读取笔记正文（默认最多 8000 字符）。长笔记用 offset 续读，按 truncated / total_chars 判断是否还有剩余。`
- schema 增加 `offset`（integer，minimum 0，maximum 1000000，default 0）与 `max_chars`（integer，minimum 1，maximum 40000，default 8000），`required` 仍只有 `path`。
- 权限、副作用保持不变（read / read）。

注意：`doc["content"]` 始终是切片后的字符串；`frontmatter/mtime/title/path` 原样保留。短笔记（≤8000）行为与现在一致，只多三个字段。

### 2. 历史编译（`app/agent/kernel.py`）

新增模块级常量与纯函数（放在 `_ensure_system` 之后）：

```python
HISTORY_CHAR_BUDGET = 24_000

def _msg_chars(msg: Dict[str, Any]) -> int: ...
def _compile_history(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]: ...
```

算法（确定性，不做摘要）：

1. system 恒保留（`msgs[0]`）。
2. 若 `sum(_msg_chars(m) for m in msgs) <= HISTORY_CHAR_BUDGET`，原样返回同一列表对象。
3. 否则从后往前、只在 **user 消息边界**切：先保留最后一条 user 及其后全部消息；再逐条向前吃「user 到下一个 user 之前」的完整回合，超预算即停。
4. 绝不把 `assistant.tool_calls` 与其后的 `tool` 消息拆开（按 user 边界切天然满足）。
5. 至少保留最后一个 user 回合，即使它本身超预算。
6. 若没有 user 消息（异常输入），返回 `[msgs[0]] + msgs[-3:]`，且保证 system 在首位。
7. 返回值是新列表；**不得修改 `msgs` 里的任何 dict**。需要声明省略时，复制 system dict 后追加：

   ```
   【上下文】较早的 {dropped} 条消息已省略；需要旧事实请用 kwey.search / kwey.read 重新获取，不要臆测。
   ```

在 `run_chat` 里：

- 保留现有 `msgs` 作为完整账本，所有 `done` 事件仍 `messages: msgs`。
- 每次调用 `provider.chat` 前：`wire = _compile_history(msgs)`，改为 `provider.chat(wire, tools)`。
- 首次发生裁剪时 yield 一次：

  ```python
  {"type": "context_trim", "dropped_messages": len(msgs) - len(wire), "kept_chars": sum(_msg_chars(m) for m in wire), "budget": HISTORY_CHAR_BUDGET}
  ```

  用局部 `trim_reported = False` 控制只发一次；事件顺序必须在本次 `provider.chat` 的事件之前。
- `pending` / `resume` / 工具执行仍基于完整 `msgs`，不要改成 `wire`。
- 不要改 `MAX_STEPS`、确认闸门、feedback、context 注入。

### 3. 文档（`docs/AGENT-CONTRACT.md`）

只改 `kwey.read` 那一行：参数补 `offset` / `max_chars`，返回补 `offset/total_chars/truncated`。工具总数不变（22），不要改权限表。

## 允许改的文件（只限这些）

- `app/agent/registry.py`
- `app/agent/kernel.py`
- `docs/AGENT-CONTRACT.md`
- `tests/test_agent_context_budget.py`（新建）

## 禁止

- 不改 `routes_chat.py`、`chat.js`、`app.js`、`index.html`。
- 不做模型摘要、不加新事件类型之外的 UI 改动、不加依赖。
- 不改 `_ensure_system` 的既有语义（仍丢弃客户端旧 system 并重建）。
- 不改 `tests/test_contract.py` 的既有断言；新字段是 additive。

## 验收（必须全部实跑）

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
python -m pytest tests/test_agent_context_budget.py tests/test_contract.py tests/test_agent_kernel.py tests/test_chat_feedback.py -q
```

`tests/test_agent_context_budget.py` 至少覆盖：

1. **默认截断**：造 12000 字笔记 → `len(content)==8000`、`truncated is True`、`total_chars==12000`、`offset==0`。
2. **续读**：`offset=8000` → 剩余 4000 字、`truncated is False`、拼起来等于原文。
3. **窗口**：`offset=100, max_chars=50` → 精确切片。
4. **参数校验**：`offset=-1`、`offset=True`、`max_chars=0`、`max_chars=40001`、`offset="10"` → `ContractError(400)`。
5. **短笔记不变**：≤8000 字 → `content` 原文、`truncated False`。
6. **历史不裁剪**：短历史 → `_compile_history(msgs) is msgs`（同一对象）。
7. **裁剪边界**：30 轮 × 2000 字 → 返回列表 `[0]` 是 system、`[1]["role"]=="user"`、最后一条保留、总字符 ≤ 24000 + system 声明长度。
8. **工具对不拆**：构造 user → assistant(tool_calls) → tool → assistant 的多轮，确认裁剪后首条非 system 不是 `tool`，且不存在孤立的 `tool`（每条 tool 的 `tool_call_id` 都能在紧邻前面的 assistant 里找到）。
9. **账本完整**：monkeypatch `AgentProvider.chat` 记录收到的 messages，长历史跑 `run_chat` 后断言 `events[-1]["messages"]` 长度 ≥ 原始消息数 + 1，且包含最早那条 user；收到的 messages 少于账本。
10. **事件**：长历史时事件流里恰有一个 `context_trim`，`dropped_messages > 0`。

## 交付报告格式

```
## 改动
- 文件：做了什么（逐条）

## 验证
- pytest 命令 + 尾行原文
- 每个用例一句话结论（1–10）

## 偏差
- 与本卡的差异及原因（没有就写「无」）

## 未决 / 风险
- 例如：未做摘要型压缩；超长单轮仍可能超预算
```
