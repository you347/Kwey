# 卡 F：聊天渲染遗留修复（多 message 合并 / 代码块按钮定位 / 表格渐隐 / 本地写窗口）

> 你是 Kwey 项目的执行子智能体。只做本卡范围。整份读完再动手。
> **不要启动服务、不要用 Playwright、不要跑全量 pytest**（集成者统一验收）。你只做实现 + `node --check` + 定向 pytest。

## 背景（都是上一批卡留下的已知问题，有实测证据）

1. `static/js/chat.js` 的 `message` 事件处理是 `case 'message': if (ev.content) addAssistant(ev.content); break;` —— 每来一条就新建一个 assistant 气泡。当前内核一次回复只有一条 `message`，但一旦改流式输出就会变成多条碎消息（上一卡报告已列为风险）。
2. `#panel-chat .chat-code-copy` 用 `position: absolute` 挂在 `pre` 上，而 `pre` 自身是 `overflow-x: auto` 滚动容器 —— 代码横向滚动时按钮会跟着内容移出视野（实测：代码块 `scrollWidth 554 > clientWidth 325`，93 行）。
3. 宽表格（实测 480px 内容 / 325px 视口）只能看到前两列，横向滚动条仅滚动时可见，**没有任何视觉提示**。
4. `static/js/app.js` 的 `isRecentLocalWrite(path)` 用 `2500ms` 时间窗无条件跳过 WS `updated` 事件：本地保存后 2.5s 内 Agent 写入，UI 不刷新也不提示（集成验收记录为 ④ 号遗留）。

## 目标行为

### 1. 多 message 合并（`static/js/chat.js`）

- `run()` 内维护本轮 `currentAssistantEl`：`case 'message'` 时若存在则**追加文本**并重渲染 Markdown，否则新建气泡并记住。
- 一轮结束（`done` / `error` / `AbortError` / 函数收尾）重置该变量为 `null`。
- 追加渲染要防抖 120ms，避免每条 fragment 都打一次 `/api/agent/render`；防抖未触发时先显示纯文本。
- 现有 `renderAssistantMarkdown(bubble, text)` 保持不变（先填纯文本再异步渲染）。
- 撤回（`undoMessage`）仍按 `data-cut` 移除整条消息，不受影响。

### 2. 代码块结构（`static/js/chat.js` + `static/css/panels.css`）

- 渲染后把每个 `pre` 包一层：`<div class="chat-code"><pre>…</pre><button class="chat-code-copy" data-copy-code>复制</button></div>`。
- 按钮已是 `#chat-messages` 委托点击（`onChatAction` 里按 `[data-copy-code]` 找 `pre > code`），**保持这个取值路径**；包层后仍能取到。
- CSS：`.chat-code { position: relative; }`，按钮相对 `.chat-code` 定位（`top: 4px; right: 4px`），`pre` 继续 `overflow-x: auto`。这样横滚时按钮不动。
- 悬停显示规则从 `pre:hover` 改为 `.chat-code:hover`，保留 `@media (hover: none)` 兜底。
- 旧的 `#panel-chat .chat-code-copy` 规则要改，不要留两套。

### 3. 表格右缘渐隐（`static/js/chat.js` + `static/css/panels.css`）

- 渲染后对每个 `table` 判断 `scrollWidth > clientWidth + 1`，是则给**父级**（`table` 的 `parentElement`）加类 `is-scrollable`。
- CSS：`.chat-markdown .is-scrollable { mask-image: linear-gradient(to right, #000 calc(100% - 20px), transparent); -webkit-mask-image: ...; }`
- 只在可横滚时生效；不新增颜色 token（渐变里的 `#000` 是 mask，不是可见色）。
- 注意 `table` 当前是 `display: block; overflow-x: auto`，父级是气泡本身时不要给整个气泡加遮罩 —— 若父级是 `.chat-msg__bubble`，改为给 `table` 自身加类 `is-scrollable` 并对 `table` 应用 mask（`display:block` 的元素可以应用 mask）。**实现时实测哪种能生效就用哪种，报告里说明。**

### 4. 本地写窗口（`static/js/app.js`）

- `isRecentLocalWrite` 窗口从 `2500` 降到 `700`（ms）。
- `saveNote()` 成功后：`setTimeout(() => { if (lastLocalWrite && lastLocalWrite.path === path) lastLocalWrite = null; }, 800)`，主动清理，避免窗口叠加。
- 不改 `handleWsEvent` 的其它分支，不动冲突条逻辑。

## 文件（只允许这些）

- `static/js/chat.js`
- `static/css/panels.css`
- `static/js/app.js`

## 禁止

- 不改后端任何 `.py`、`static/index.html`、`editor.css`、`tabs.js`、`splitview.js`。
- 不引入依赖、不改 `/api/agent/render` 契约、不改 SSE 事件处理顺序。
- 不顺手重构 `saveNote` / `onChatAction` 的其它分支。

## 验收（你必须在报告里贴输出）

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
node --input-type=module --check < static/js/chat.js
node --input-type=module --check < static/js/app.js
python -m pytest tests/test_chat_render.py tests/test_chat_feedback.py tests/test_api_notes.py -q
```

要求：两个 node check 退出码 0；pytest 全绿（不回退）。

自查清单（贴结论，不用跑浏览器）：
- `grep -n "chat-code" static/js/chat.js static/css/panels.css`：确认只有新结构，没有残留旧的 `pre` 直接子节点写法。
- `grep -n "is-scrollable" static/js/chat.js static/css/panels.css`：确认 JS 加类与 CSS 选择器对得上。
- `grep -n "2500" static/js/app.js`：确认已无 2500ms 窗口。

## 交付报告格式

```
## 改动
- 文件：做了什么（逐条）

## 验证
- 两个 node check 输出
- pytest 尾行
- 三条 grep 自查结论

## 偏差
- 与卡的差异及原因（没有就写「无」）

## 未决 / 风险
```
