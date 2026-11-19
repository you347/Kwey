# 卡 S：P2 幻灯片演示模式（Slides）

> 你是 Kwey 项目的执行子智能体。只做本卡范围。整份读完再动手。
> **不要启动服务、不要用 Playwright、不要跑全量 pytest**（集成者统一验收）。你只做实现 + `node --check` + 自检脚本。

## 背景

P2 清单里唯一未实现的功能（全仓 grep `slides` 无任何代码）。目标：不引入依赖、不改后端，把当前笔记变成全屏演示。

项目事实（先读再动手）：

- 前端为原生 ES Module，无构建链。模块约定：`$` / `$$` / `escapeHtml` / `toast` 来自 `static/js/utils.js`；`state` 与 `emit` / `on` 来自 `static/js/state.js`；命令注册 `registerCommand` 来自 `static/js/palette.js`（范例：`static/js/rag.js` 的 `rag.open`）。
- 笔记渲染接口：`POST /api/note/render`，body `{content}`，返回 `{code,data:{html},message}`。请求头必须带 token：用 `static/js/api.js` 导出的 `authHeaders`；401 时用同一模块导出的 `renewToken()` 重试一次。
- 主题 token：`--bg` / `--bg-alt` / `--text` / `--text-secondary` / `--accent` / `--border` / `--radius-md` / `--font-*` / `--fs-*`，深浅两套由 `html[data-theme]` 切换。**不要写死颜色**。
- 当前笔记路径在 `state.activePath`。

## 目标行为

1. 命令面板出现「演示当前笔记（幻灯片）」，命令 id `slides.present`。
2. 执行后全屏 overlay 演示**当前笔记**；没有打开笔记时 `toast('先打开一篇笔记', 'error')` 并返回。
3. 分页规则（按优先级取第一个能切出 ≥2 页的规则；都不满足则整篇一页）：
   - **`---`**：独立成行的水平分割线（正则 `^---+\s*$`）。注意 frontmatter 的 `---` 必须先剥掉（文件开头两个 `---` 之间的块）。
   - **`## `**（二级标题）：每页从标题开始。
   - **`# `**（一级标题）：同上。
4. 每页内容用 `/api/note/render` 渲染成 HTML 后插入 `.slide__body`；某页渲染失败降级为 `<pre>` 纯文本，**不阻断演示**。
5. 交互：
   - `←` / `PageUp` 上一页；`→` / `PageDown` / `Space` 下一页；`Home` / `End` 首末页；`Esc` 退出。
   - 底部工具条：`‹` 按钮、`n / N` 页码、`›` 按钮、`×` 关闭按钮。
   - 第一页时 `‹` 禁用、最后一页时 `›` 禁用（`disabled` 属性，不要只是降低透明度）。
   - 点击遮罩空白处**不退出**（防误触）。
   - 演示时键盘事件必须 `preventDefault`，避免落到 CodeMirror 或触发全局快捷键。
6. 退出后恢复：关闭 overlay、`document.body` 不留滚动锁、焦点还给编辑器（`#editor-textarea` 或 `.CodeMirror` 的文本域，取到哪个用哪个）。

## 文件（只允许这些）

- **新建** `static/js/slides.js`
  - 导出 `initSlides()` / `openSlides()` / `closeSlides()`；`initSlides()` 幂等，被调用时 `registerCommand` 并绑定一次性监听。
  - 自建 DOM（不依赖 index.html 里的容器）：`#slides-overlay` > `.slides__stage` > `.slide` > `.slide__body` + `.slides__bar`。
  - 分页逻辑抽成纯函数 `splitSlides(markdown) -> string[]`，**函数内不得引用 DOM**（便于 node 自检）。
- **新建** `static/css/slides.css`：全部用现有 token，作用域限定 `#slides-overlay`。
- **改** `static/index.html`：仅两行（与 `splash.css` / `splash.js` 同风格）
  - 在 `<link ... splash.css>` 之后加 `<link rel="stylesheet" href="/static/css/slides.css?v=1">`
  - 在 `<script type="module" src="/static/js/splash.js"></script>` 之后加 `<script type="module" src="/static/js/slides.js"></script>`
  - `slides.js` 模块顶层调用 `initSlides()`（保持与 splash.js 一致的自举方式）。

## 禁止

- 不改 `app.js` / `chat.js` / `panels.css` / `editor.css` / 后端任何 `.py`。
- 不引入任何第三方依赖（无 reveal.js、无 markdown 库）。
- 不改 `/api/note/render` 的契约。
- 不做导出 PDF / 演讲者视图（后续卡再说）。

## 验收（你必须在报告里贴输出）

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
node --input-type=module --check < static/js/slides.js
```

分页纯函数自检（用 node 直接跑，不走浏览器）：

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
node --input-type=module -e "
import('./static/js/slides.js').then(m => {
  const cases = {
    frontmatter: '---\ntitle: x\n---\n# A\n\n---\n\n# B',
    headings: '# A\n\n## B\n\n## C',
    single: 'just one page',
  };
  for (const [k, v] of Object.entries(cases)) console.log(k, JSON.stringify(m.splitSlides(v)));
});
"
```

若因 `slides.js` 顶部 import 了 `utils.js`（引用 `document`）导致 node 导入失败，**把 `splitSlides` 抽到新文件 `static/js/slides-paging.js`**（零依赖，纯函数），由 `slides.js` import 它；自检改为导入 `slides-paging.js`。这是允许的第四个文件。

期望：
- `frontmatter` → 2 页且第一页不含 `title: x`；
- `headings` → 3 页；
- `single` → 1 页。

## 交付报告格式

```
## 改动
- 文件：做了什么（逐条）

## 验证
- node check 输出
- splitSlides 三种用例输出

## 偏差
- 与卡的差异及原因（没有就写「无」）

## 未决 / 风险
```
