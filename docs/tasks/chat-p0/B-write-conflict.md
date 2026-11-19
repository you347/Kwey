# 任务卡 B：外部/Agent 写入冲突提示（不再静默丢弃本地内容）

> 你是 Kwey 项目的执行子智能体。只做本卡范围，不改其它卡的文件。整份读完再动手。

## 背景（现状是数据安全问题）

- WS 收到 `updated` 时，`static/js/app.js` 只在**编辑器干净**时 `loadNote()`；脏时什么都不做。
- 用户随后保存 → 后端用 `base_mtime` 返回 **409**；现有分支直接 `await loadNote(...)`，**未保存内容被静默丢弃**。
- Agent 现在会写文件，这个窗口会真实发生。

本卡目标：把「静默丢弃」改成「显式冲突条 + 两个安全出口」。

## 先读

1. `static/js/app.js`：`loadNote()`（约 190 行）、`saveNote()`（约 270–310 行）、`handleWsEvent()`（约 384 行）、`isRecentLocalWrite()`
2. `static/index.html`：`#editor-area` 结构（约 224 行）
3. `static/css/editor.css`：`.editor-toolbar` / `.save-state` 附近
4. `static/js/editor.js`：`getValue()` / `setValue()`

## 目标行为

| 场景 | 期望 |
|---|---|
| 编辑器干净，磁盘被外部/Agent 修改 | 与现在一致：静默重载，不显示冲突条 |
| 编辑器脏，磁盘被外部/Agent 修改 | 顶部出现冲突条，**不动编辑器内容**，不自动保存 |
| 脏状态下保存撞 409 | 出现同一条冲突条；不得再自动 `loadNote` 丢弃内容 |
| 点「重新载入」 | 丢弃本地缓冲，加载磁盘最新内容，冲突条消失 |
| 点「保留我的并另存」 | 当前缓冲另存为同目录 `原名 (冲突副本).md`（重名则 ` (冲突副本 2).md`…），然后打开该副本；原笔记标签回到磁盘最新内容 |
| 切换笔记 / 关闭标签 / 保存成功 | 冲突条隐藏，状态复位 |

## 实现要求

### 1. HTML（`static/index.html`）

在 `<div id="editor-area" class="editor-area hidden">` 之后、`.editor-toolbar` 之前插入：

```html
<div id="conflict-bar" class="conflict-bar hidden" role="alert">
  <span id="conflict-text" class="conflict-bar__text"></span>
  <span class="conflict-bar__actions">
    <button id="btn-conflict-reload" class="btn btn--mini">重新载入</button>
    <button id="btn-conflict-keep" class="btn btn--mini btn--ghost">保留我的并另存</button>
  </span>
</div>
```

### 2. 逻辑（`static/js/app.js`）

新增模块内状态与三个函数（放在 `loadNote` 附近，不要新建模块）：

```js
let conflictPath = null;
let conflictBusy = false;

function showConflict(path) { ... }   // 校验 path === state.doc.path，填文案「磁盘上的「xxx」已被外部修改（可能来自智能体）。」并显示
function hideConflict() { ... }       // 隐藏 + conflictPath = null（busy 时不复位）
async function saveConflictCopy() { ... }
```

绑定（在 `bindStaticButtons()` 或 `startVaultUI()` 的既有绑定区，二选一，别两处都加）：

- `#btn-conflict-reload` → `const p = conflictPath || state.doc?.path; hideConflict(); await loadNote(p)`
- `#btn-conflict-keep` → `saveConflictCopy()`

`saveConflictCopy()` 要求：

1. `conflictBusy` 置位，两个按钮 `disabled`，`finally` 复位。
2. `const path = conflictPath || state.doc.path`；`content = editor.getValue()`；`frontmatter = state.doc.frontmatter`。
3. 候选路径：同目录下 `stem + ' (冲突副本)' + ext`，冲突则 `stem + ' (冲突副本 2)' + ext`，最多 20 次。
4. `api.post('/api/note', { path: target, content, frontmatter })`；409 换下一个候选，其它错误 `reportError` 并停止。
5. 成功后：`toast('已另存为 ' + target, 'success')`、`emit('tree-refresh')`、`await tabs.openTab(target)`（让用户看到保住的文本）。原笔记无需手工 reload —— 重开原标签时会读到磁盘最新内容；若原标签仍是激活态，`loadNote(path)` 同步一次。
6. 失败时保留冲突条（不能把唯一出口关掉）。

`handleWsEvent()` 的 `case 'updated'` 改为：

```js
if (!state.doc || path !== state.doc.path || isRecentLocalWrite(path)) break;
if (state.dirty) showConflict(path);
else loadNote(path);
```

`saveNote()` 的 `catch` 中 `err.code === 409` 分支改为：`showConflict(state.doc.path)` + `setSaveState('保存冲突')` + toast「磁盘内容已更新，请选择重新载入或另存副本」。**删除原来的 `await loadNote(...)`。**

其他复位点：

- `loadNote()` 开头 `hideConflict()`；成功后再确保一次。
- `showNoNote()` 里 `hideConflict()`。
- `saveNote()` 成功后 `hideConflict()`。

### 3. 样式（`static/css/editor.css`）

约 25 行，插在 `.editor-toolbar` 规则之前：

- `.conflict-bar { display:flex; align-items:center; gap:10px; flex:none; padding:6px 14px; background:var(--bg-alt); border-bottom:1px solid var(--border); border-left:3px solid var(--broken); font-size:var(--fs-sm); color:var(--text); }`
- `.conflict-bar.hidden { display:none; }`
- 文案 `flex:1; min-width:0;` 允许换行；按钮组 `flex:none; display:flex; gap:6px;`
- 不要新增颜色 token，不要写死颜色；深/浅主题都靠 `--bg-alt / --border / --broken / --text`。

## 允许改的文件（只限这些）

- `static/index.html`
- `static/js/app.js`
- `static/css/editor.css`

**不要**改 `editor.js`（`getValue` 已够用）、`tabs.js`、`chat.js`、后端任何文件。

## 禁止

- 不引入三方 diff/合并库，不做自动合并。
- 不改 `base_mtime` 语义、不改后端 409。
- 不顺手重构 `saveNote` / `handleWsEvent` 的其它分支。
- 不删既有 `lastLocalWrite` 逻辑（它防止自己的保存触发重载）。

## 验收（必须实跑并贴输出）

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
node --input-type=module --check < static/js/app.js
```

再用脚本化验收（起服务 `python main.py --server --no-browser --vault demo-vault --port 17631`，用 Playwright msedge）：

1. 打开 `欢迎.md`，在编辑器输入「本地未保存内容」→ `state` 变脏。
2. 用后端 API 直接 `PUT /api/note` 改同一个文件（`X-Kwey-Token` 用 `/api/agent/bootstrap` 取；不带 `base_mtime`）。
3. 等 1s：断言 `#conflict-bar` 可见，编辑器内容仍是「本地未保存内容」。
4. 点「重新载入」：断言编辑器内容变成外部写入内容，冲突条隐藏。
5. 重复 1–2，点「保留我的并另存」：断言文件树出现 `欢迎 (冲突副本).md` 且内容含「本地未保存内容」，当前激活标签是该副本。
6. 干净状态（不输入）重复外部写入：断言不出现冲突条且编辑器自动更新。

把脚本放 `C:/tmp/` 下运行，不要提交进仓库。报告里贴关键断言输出。

## 交付报告格式

```
## 改动
- 文件：做了什么（逐条）

## 验证
- node check 输出
- 6 条浏览器断言的实测结果

## 偏差
- 与本卡的差异及原因（没有就写「无」）

## 未决 / 风险
- 例如：分屏副屏（textarea）暂未接冲突条
```
