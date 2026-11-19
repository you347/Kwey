# 任务卡：重建 Kwey 设置模块前端（v2）

> 复制本文件全文给执行 Agent 即可。目标：把设置页从「手写平铺 HTML + CSS 补丁 + tab 隐藏过滤」改为「schema 驱动渲染 + 组件行 + 单一视觉源」，保留全部业务行为与 DOM id 契约。

---

## 0. 事实基线（动手前必须先读，不要凭猜测改）

| 文件 | 现状 |
|---|---|
| `static/index.html` L520–702 | `#settings-page` 双栏 shell + 12 个 `<section class="settings-section" data-group>` 平铺 |
| `static/js/settings.js`（118 行） | `initSettings()` 绑事件、`openSettings()` 回填、`closeSettings()`、`saveSettings(partial)` → `PUT /api/settings` |
| `static/js/settingsnav.js`（70 行） | tab 切换 + 搜索 + `MutationObserver` 给动态 section 补 `data-group`，靠 `settings-section--filtered` 类隐藏 |
| `static/css/settings-ui.css`（379 行） | `#settings-page` 作用域 token，覆盖 `--bg/--text/--accent` 让动态模块自动深色化 |
| `static/js/plugins.js` L31 | `document.querySelector('#settings-page .settings-page__panel')` 后 `appendChild(section)`，`data-group='extension'` |
| `static/js/themeeditor.js` L28 | 同上，`data-group='extension'` |
| `static/js/clipper.js` L68 | 在自身面板里复用 `.settings-row` 样式（不改动它，但 CSS 类名不能删） |
| 调用链 | `app.js`：`initSettings()`（L83/L120）、`initSettingsNav()`（L162）；命令 `settings` / `hotkeys` / `agent-settings` 均调 `openSettings()` |

基线校验：`python -m pytest -q` → **840 passed / 5 skipped**，任何低于此数量的结果都视为回归。

## 1. 重建理由（五个已确证的结构缺陷）

1. **分组靠隐藏实现**：12 个 section 全部渲染在 DOM，只靠 `--filtered` 类切换可见性；分组与内容没有真实从属关系。
2. **空分组**：`compile`（知识编译）tab 在 HTML 中**没有任何 section**，插件也不往里挂 —— 点开即空白。`raw_dir` / `wiki_dir` 只存在于 `#overlay-compile` 弹窗（`app/api/routes_compile.py`），从未进入设置。
3. **样式补丁化**：视觉规则散在 `settings-ui.css` 与 `layout.css`，缺少尺寸约束曾导致 SVG 图标按 300×150 渲染成大黑块。
4. **动态区块归类靠猜**：新 section 无 `data-group` 时继承「当前分组」，用户在 Agent 组里打开插件就归错组。
5. **导航层级不足**：左侧仅 4 项，Agent 组内挤了 3 张卡（Provider / 向量 / 自动推送），高频的 API Key 配置被埋在长滚动里。

## 2. 目标架构（硬性决定，不要另起方案）

```
static/js/settings/
  schema.js     # 纯数据：分组 → 卡片 → 行声明（无 DOM、无副作用）
  controls.js   # 行控件构造器注册表，返回 HTMLElement
  render.js     # renderGroup() / renderAll()，幂等，可重复调用
  registry.js   # registerSection({id, group, order, title, description, mount, refresh})
  index.js      # 对外只暴露 initSettings / openSettings / closeSettings / saveSettings / saveAttachmentDir
```
`static/js/settings.js` 与 `static/js/settingsnav.js` **删除**，其职责并入上表；`app.js` 的 import 改为 `./settings/index.js`（导出名保持不变，故 `app.js` 只改 import 路径一行）。

单向数据流：`schema.js`（声明） → `render.js`（生成 DOM） → `controls.js`（控件 + `onChange` 回调） → `index.js`（调 API 并同步 `state.settings`）。**渲染过程不读 API**；数据由 `openSettings()` 填入 `state.settings` 后再 `renderAll()`。

行控件类型（`controls.js` 必须实现这 9 种，每种一个工厂函数）：

| 类型 | 用途 | 关键要求 |
|---|---|---|
| `switch` | 布尔开关 | iOS pill 38×22，`role="switch"`，键盘 Space 切换 |
| `select` | 枚举 | 原生 `<select>`，仅重绘外观 |
| `slider` | 数值区间 | 显示当前值 + 单位，拖动不写盘，`change` 才提交，`--fill` 百分比填充 |
| `text` | 单行文本 | 失焦/回车提交 |
| `path` | vault 相对目录 | input + 「选择文件夹」按钮，复用 `bindFolderPicker` |
| `secret` | 长密钥 | **必须 `<textarea rows=3>`** + 显隐按钮 + 独立「保存」按钮（长 key 曾因 160px 单行输入框不可用） |
| `action` | 按钮组 | 主按钮 + 次按钮，支持 `busy` 态与结果行 |
| `link` | 跳转行 | 整行可点，右侧描述灰字 + `#i-chevron` |
| `custom` | 逃生舱 | `mount(el)` 回调，供 `hotkeys-list` / `agent-token-list` / 插件网格使用 |

## 3. 设计规格（已确认，不得自由发挥）

沿用 `settings-ui.css` 现有 token，**不要另建一套变量**：

```
强调色  #35c9a6（hover #54e0be，soft rgba(53,201,166,.14)）
画布    #16181a   面板 #1c1f21   导航 #191b1d   卡片 #202426   行底 #24282a
文字    #ecefed / #a7afab / #6f7874   （三级，不新增第四级灰）
边框    rgba(255,255,255,.08)，视觉 0.5px，一律用 border 不用 box-shadow 造卡片
圆角    外壳 16 / 卡片 12 / 控件 7
字号    11 / 13 / 14 / 20 四档（正文 13，行标签 14，卡片标题 13+500，分组标题 20）
```

- 窗口 `min(1040px, 94vw) × min(720px, 88vh)`，居中浮层 + 背景 `blur(6px)`。
- 左导航固定 220px，`overflow-y:auto`，独立滚动；右侧内容区独立滚动，滚动条 8px、悬停才显色。
- 卡片内行高 52px，行间分隔线 0.5px；控件右侧对齐；标签左对齐不换行（超长用 `text-overflow` 省略 + `title`）。
- 卡片之间间距 20px，卡片标题与首行间距 8px。**禁止卡片套卡片、禁止给卡片加阴影**，只有外壳有阴影。
- 所有图标 16×16、`stroke-width:1.6`、`fill:none`，`use` 引用 `index.html` 的 sprite（可用：`i-sliders i-file i-folder i-link i-list i-spark i-keyboard i-moon i-sun i-search i-chevron i-close i-folder-open i-graph i-tag i-template i-clock i-download i-copy i-edit i-trash i-restore i-plus i-bookmark i-calendar i-columns`）。

## 4. 分组重组（8 组，替代原 4 组）

| group id | 标题 | 图标 | 卡片（来源） |
|---|---|---|---|
| `general` | 常规 | `i-sliders` | 外观（主题、自动保存延迟滑块）、跳转行「快捷键设置」 |
| `files` | 文件与目录 | `i-folder` | 附件目录（原 general）、模板目录、每日笔记目录、当前 Vault 路径 + 打开 |
| `agent` | Agent Link | `i-link` | MCP 启动命令（info）、git 快照开关、访问 Token（role 选择 + 生成 + 列表 custom） |
| `provider` | 模型接入 | `i-spark` | Provider endpoint、模型、协议、API Key（`secret` 行，从 agent 拆出） |
| `search` | 检索与向量 | `i-search` | 启用向量 + 状态、Embedding 模型、权重三项、保存 / 重建索引 |
| `sync` | 同步 | `i-clock` | 每日推送开关、远端/分支/时间、提交模板、包含目录、保存 / 立即推送、状态行 |
| `extension` | 扩展与外观 | `i-folder-open` | 插件市场（`registry` 动态）、自定义 CSS（`registry` 动态） |
| `about` | 关于 | `i-file` | 版本号、vault 路径、项目文档入口（跳转行，`docs/ARCHITECTURE.md`） |

`compile` 分组处理（二选一，**默认 A**）：
- **A（默认，零后端改动）**：不保留 compile 分组，改为在 `search` 组末尾放 `link` 行「知识编译面板（raw → wiki）」，点击执行 `command: 'compile.open'`；原空 tab 删除。
- B（需后端配合，本卡不实施）：为 `raw_dir` / `wiki_dir` 在 `Settings` 模型加字段并持久化，在 `about` 前新增 `compile` 组。

## 5. 兼容契约（破坏即视为失败）

**DOM id 一个都不能改名**（被其他模块直接 `$('#id')` 引用）：

```
settings-page settings-search settings-group-title settings-theme settings-autosave
settings-autosave-value settings-attachment-dir settings-template-dir settings-daily-dir
settings-vault-path settings-vault-pick settings-vault-info btn-vault-open
hotkeys-list agent-mcp-cmd agent-snapshot-status settings-snapshots-enabled
agent-token agent-token-role agent-token-list agent-provider-endpoint
agent-provider-model agent-provider-protocol agent-provider-key
vector-section vector-enabled vector-status vector-model vector-weight
vector-graph-weight vector-rrf-k autopush-section autopush-enabled autopush-remote
autopush-branch autopush-time autopush-commit-template autopush-include-dirs
autopush-status
```
按钮 id（`btn-settings-close` / `btn-agent-token` / `btn-agent-provider-save` / `btn-vector-save` / `btn-vector-rebuild` / `autopush-save` / `autopush-run` / `btn-agent-key-toggle` / `btn-settings` / `btn-open-vault` / `link-to-hotkeys`）同样保留，因为 `agentlink.js` / `vectorsettings.js` / `autopush.js` / `hotkeys.js` 用它们绑定事件。

**动态区块改造（只允许改这几行）**：
- `plugins.js`：把「找 panel 并 appendChild」替换为 `registerSection({id:'plugins', group:'extension', order:10, title:'插件市场', mount: (el) => { el.innerHTML = `...原模板...`; listEl = el.querySelector('#plugins-list'); }})`，其余筛选/安装逻辑不动。
- `themeeditor.js`：同法注册，`order:20`，模板与 AI 生成逻辑不动。
- `clipper.js`：不改代码；确保 `.settings-row` / `.settings-row--inline` / `.settings-muted` / `.btn` 类名与视觉仍在。

**行为保留**：`saveSettings()` 仍是 `PUT /api/settings` 并同步 `state.settings`；主题切换后 `emit('theme-changed')`；打开 vault 后 `closeSettings()` + `emit('vault-opened')`；快捷键录制、token 生成/吊销、Provider 保存（`agentFetch` 带 token）、向量重建、自动推送保存/立即推送全部照旧可用。

## 6. 硬约束

- 不新增任何第三方依赖，不引入构建链，纯 ES Module + 原生 DOM。
- 不改 `app/` 下任何后端文件（字段名与路由是既有契约）。
- 单文件 ≤ 400 行；`schema.js` 纯数据，禁止 import DOM 模块。
- 文案简体中文，不用 emoji；数字与英文单位间不空格（`800ms`）。
- 可访问性：`role="tablist"/"tab"`、`aria-selected`、开关 `role="switch"`、`aria-checked`；`Esc` 关页面；搜索框聚焦时上下键在可见行间移动。
- 渲染必须幂等：连续 `openSettings()` 两次不产生重复 DOM、不重复绑定事件。

## 7. 执行阶段（每阶段结束都要能跑）

1. **骨架**：建 `settings/` 五文件，`schema.js` 只填 `general` + `files`，`index.js` 完成 init/open/close/save 与 Vault 打开逻辑；`app.js` 改 import；删旧 `settings.js`、`settingsnav.js`；跑 `python -m pytest -q`。
2. **迁移**：按第 4 节补齐其余 6 组，逐个对照第 5 节 id 清单核对，绑定原有事件（快捷键、token、provider、向量、推送）。
3. **动态区块**：改 `plugins.js` / `themeeditor.js` 挂载方式；`registry.js` 支持 `refresh()` 在 vault 变更后重渲染；删除 `MutationObserver` 补丁逻辑。
4. **收口**：清理 `layout.css` 与 `settings-ui.css` 中已失效的 `.settings-shell` 旧规则与重复定义，`settings-ui.css` 收敛为唯一视觉源；截图自检。

## 8. 验收（必须逐条给出证据）

```bash
python -m pytest -q                      # 必须 ≥ 840 passed, 5 skipped
for f in static/js/settings/*.js; do node --input-type=module --check < "$f" || echo "FAIL $f"; done
python -c "import ast,pathlib;[ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('app').rglob('*.py')]"
```

Playwright 断言（`msedge` channel，`emulate_media(reduced_motion='reduce')`）：
1. 点 `#btn-settings` → `#settings-page` 可见，左导航 8 项，`#settings-group-title` = 「常规」。
2. 逐个点 8 个 tab：内容区 `.settings-card` 数量 ≥ 1（无空组），无横向滚动条。
3. 搜索「key」→ 可见结果包含 Provider 组卡片；清空恢复。
4. 切到「模型接入」，`#agent-provider-key` 的 `tagName === 'TEXTAREA'` 且 `clientHeight > 60`。
5. 拖 `#settings-autosave` → 标签文本变化；`change` 后 `GET /api/settings` 的 `autosave_delay_ms` 已更新。
6. 切到「扩展与外观」→ `#plugins-section` 与自定义 CSS 区块均在，`#plugins-list` 子元素 > 0。
7. 截图三张（常规 / 模型接入 / 扩展与外观），人工确认：无 300×150 大图标、无元素重叠、卡片间距一致、强调色仅出现在开关/滑块/主按钮。

## 9. 交付物

- 新增：`static/js/settings/{schema,controls,render,registry,index}.js`
- 删除：`static/js/settings.js`、`static/js/settingsnav.js`
- 改动：`static/index.html`（设置页收缩为 `<div id="settings-page" class="settings-page hidden"><div class="settings-shell"><aside id="settings-nav"></aside><div id="settings-panel"></div></div></div>`）、`static/js/app.js`（import 一行）、`static/js/plugins.js`、`static/js/themeeditor.js`、`static/css/settings-ui.css`、`static/css/layout.css`
- 报告：改动的文件清单 + 上述命令输出 + 三张截图 + 与第 5 节 id 契约的逐项对照表（缺一即失败）
