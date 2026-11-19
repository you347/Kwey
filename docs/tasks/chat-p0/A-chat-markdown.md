# 任务卡 A：聊天回复 Markdown 渲染 + 代码块复制

> 你是 Kwey 项目的执行子智能体。只做本卡范围，不改其它卡的文件。整份读完再动手。

## 项目背景

Kwey 是本地优先的个人知识库（FastAPI + 原生 ES Module 前端 + pywebview）。内置智能体聊天走：

```
static/js/chat.js  --POST /api/agent/chat (SSE)-->  app/api/routes_chat.py  -->  app/agent/kernel.py
```

当前助手回复用 `bubble.textContent = text` 直接塞纯文本（`static/js/chat.js` 约 224 行），模型输出的列表、加粗、表格、代码块全部退化成纯文本。本卡只解决「把回复渲染成 HTML + 代码块可复制」，**不做流式输出**。

## 先读这些文件

1. `static/js/chat.js`：`buildMessage()`、`addAssistant()`、`onChatAction()`、`stream()`
2. `app/parser/renderer.py`：现有 `render_markdown()`（注意它用 `html=True` 透传 wikilink 注入的 HTML）
3. `app/api/routes_chat.py`：现有路由与鉴权写法
4. `app/api/routes_notes.py` 的 `POST /note/render`：返回包格式参考
5. `static/css/panels.css`：`#panel-chat` 与 `.chat-msg__bubble` 段

## 目标（可验收）

助手气泡渲染 Markdown：标题、段落、有序/无序列表、引用、表格、行内代码、围栏代码块、链接、分隔线、删除线。代码块右上角有「复制」。用户气泡与报错气泡保持纯文本。

## 实现要求

### 1. 后端：新增安全的聊天渲染函数（`app/parser/renderer.py`）

新增：

```python
def render_chat_markdown(text: str) -> str: ...
```

硬约束：

- **必须用独立实例**，`html=False`。禁止复用 `_MD`（它是 `html=True`，为 wikilink 注入服务）；把模型输出透传成 HTML 会引入脚本注入。
- 只启用 `table`、`strikethrough`，开启 `linkify`，与此前预览能力对齐。
- **不要**调用 `_preprocess()`（wikilink/标签预处理会注入 HTML，在 `html=False` 下会被转义成字面量）。
- 渲染后把外部链接补上 `target="_blank" rel="noopener noreferrer"`（仅 `http://` / `https://`）。`javascript:` 链接必须不可点击（markdown-it 默认会拒绝，补一条测试锁住）。
- 无新依赖。

### 2. 后端：新增路由（`app/api/routes_chat.py`）

```python
class RenderRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)

@router.post("/render")
async def agent_render(request: Request, req: RenderRequest):
    require_token(request, "read")   # 纯渲染，无落盘；read 级即可
    return {"code": 0, "data": {"html": render_chat_markdown(req.content)}, "message": "ok"}
```

- 空内容由 Pydantic 返回 422；不要吞异常。
- 不要改 `/chat` 的鉴权级别与 SSE 行为。
- **不要**在本卡改 `app/agent/kernel.py`。

### 3. 前端：助手气泡异步渲染（`static/js/chat.js`）

- 保持 `buildMessage()` 同步：先 `bubble.textContent = text`（失败兜底），再对 `role === 'assistant'` 调一个异步 `renderAssistantMarkdown(bubble, text)`。
- 该函数 `fetch('/api/agent/render')`（用现有 `authHeaders()`，401 时用现有 `renewToken()` 重放一次，和 `stream()` 同策略），成功后：

  ```js
  bubble.innerHTML = data.html;
  bubble.classList.add('chat-markdown');
  ```

  失败则保持纯文本，不弹错误（聊天不该因为渲染失败而中断）。
- 请求未返回前消息已插入 DOM，顺序不变；用 `bubble.isConnected` 防御「已撤回」的竞态。
- 代码块复制：渲染后给每个 `pre` 追加

  ```html
  <button type="button" class="chat-code-copy" data-copy-code>复制</button>
  ```

  在现有 `#chat-messages` 的点击委托里处理 `[data-copy-code]`：取 `pre > code` 的 `textContent`，用 `utils.js` 的 `copyText()`，toast「已复制 / 复制失败」。不要为每个按钮单独绑事件。
- 用户气泡、`error` 气泡维持纯文本；工具步骤（`.chat-step`）不动。
- 不要改发送、历史、上下文条、feedback 的任何逻辑。

### 4. 样式（`static/css/panels.css`）

只在 `#panel-chat` 作用域内追加 `.chat-markdown` 规则（约 40 行以内）：

- `.chat-msg__bubble.chat-markdown { white-space: normal; }`（覆盖现有 `pre-wrap`）。
- `p / ul / ol / li / h1-h4 / blockquote / hr / a / table / th / td / img / pre / code` 的基础排版；间距紧凑，适合 320–400px 侧栏。
- 颜色只用现有 token（`--text`、`--text-secondary`、`--border`、`--border-faint`、`--bg`、`--bg-alt`、`--accent`、`--radius-md`），不要写死颜色，不要写 `rgba(255,255,255,…)`。
- `pre` 需要 `position: relative` 给复制按钮定位；按钮默认 `opacity: 0`，`pre:hover` / `:focus-within` 时显示。触屏无 hover 时也要能点到（用 `opacity: .35` 兜底或 `@media (hover: none)`）。
- 表格允许横向滚动，不撑破气泡。

## 允许改的文件（只限这些）

- `app/parser/renderer.py`
- `app/api/routes_chat.py`
- `static/js/chat.js`
- `static/css/panels.css`
- `tests/test_chat_render.py`（新建）

## 禁止

- 不加依赖、不改 `requirements.txt`、不引前端库。
- 不改 `kernel.py` / `registry.py` / `app.js` / `index.html` / `settings*`。
- 不重构 `render_markdown()` 的既有行为（预览仍走它）。
- 不做流式、不做语法高亮、不做 KaTeX/Mermaid。

## 验收（必须全部实跑）

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
python -m pytest tests/test_chat_render.py -q
node --input-type=module --check < static/js/chat.js
```

`tests/test_chat_render.py` 至少覆盖：

1. 无 token → 401；带 read token → 200。
2. 表格 + 围栏代码 + 加粗 → HTML 含 `<table>`、`<pre><code`、`<strong>`。
3. `<script>alert(1)</script>` → 输出含 `&lt;script&gt;`，不含 `<script>`。
4. `[x](javascript:alert(1))` → 输出不含 `javascript:` 可点击链接。
5. `[官网](https://example.com)` → 含 `target="_blank"` 与 `rel="noopener noreferrer"`。
6. 空 content → 422。

token 获取参考 `tests/test_chat_feedback.py` 的 `_tk()`（`TokenStore(vault.paths.root).issue("admin", "test")`）。

浏览器手工验收（写进报告）：

- 在「助手」里发一句「用表格对比 A/B 两个方案，并给一段 Python 代码」。
- 表格有边框且不撑破面板；代码块右上角悬停出现「复制」，点击后剪贴板为代码原文。
- 切浅色主题后表格边框、代码底色仍清晰。

## 交付报告格式

```
## 改动
- 文件：做了什么（逐条）

## 验证
- 命令 + 原始输出（pytest 尾行、node check）

## 偏差
- 与本卡的差异及原因（没有就写「无」）

## 未决 / 风险
- 需要集成卡或后续处理的事项
```
