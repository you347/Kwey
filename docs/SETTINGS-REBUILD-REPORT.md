# 设置模块重建报告（v2）

- 日期：2026-11-17
- 基线：840 个用例 / 5 skipped；首次全量跑时 `tests/test_review_clip.py::test_schedule_and_unschedule` 偶发失败（单跑通过、最终全量跑通过，判定为既有 flaky，与本卡无关）。最终 **840 passed / 5 skipped**，无回归。
- 结论：设置页已从「平铺 HTML + CSS 补丁 + tab 隐藏过滤」改为「schema 驱动渲染 + 组件行 + 单一视觉源」，DOM id 契约 52/52 全部保留，业务行为无删减。

## 1. 架构与数据流

```
static/js/settings/
  schema.js    纯数据：8 分组 → 卡片 → 行声明（无 DOM、无 API、无副作用）
  controls.js  9 种行控件工厂：switch / select / slider / text / path / secret / action / link / custom
  render.js    nav + panel 骨架、分组建卡、跨组搜索、回填 syncControls()、事件委托
  registry.js  动态区块注册（插件市场 / 自定义 CSS），支持 order 与 refresh
  index.js     对外出口：initSettings / openSettings / closeSettings / saveSettings / saveAttachmentDir
```

单向数据流：`schema.js`（声明）→ `render.js`（生成 DOM）→ `controls.js`（控件 + `data-action`）→ `index.js`（HANDLERS → API → `state.settings`）。

- 渲染幂等：分组/卡片只在 `initView()` 建一次；动态区块按组重建；所有事件委托在 `#settings-panel` / `#settings-nav`，重建不重复绑定。
- 只有 `openSettings()` 读 API：`GET /api/settings` → `syncControls()`；其余控件由各自模块（agentlink / vectorsettings / autopush）在打开时回填，避免抢写。
- 分组是真实 DOM 从属（`.settings-group[data-group]` → `.settings-card` → `.settings-row`），切换只切可见性。

## 2. 文件清单

| 类型 | 文件 | 行数 | 说明 |
|---|---|---|---|
| 新增 | `static/js/settings/schema.js` | 265 | 8 组 11 张静态卡 + 2 张动态卡；行声明含 id/label/setting/action/hint/note |
| 新增 | `static/js/settings/controls.js` | 266 | 9 种控件工厂 + `paintRange()` |
| 新增 | `static/js/settings/render.js` | 359 | 导航、卡片、动态区块、搜索、回填、键盘导航 |
| 新增 | `static/js/settings/registry.js` | 51 | `registerSection` / `sectionsFor` / `refreshSections` |
| 新增 | `static/js/settings/index.js` | 213 | 生命周期、HANDLERS、MOUNTS（vault 行 / MCP 命令 / token 行 / 关于） |
| 删除 | `static/js/settings.js` | - | 职责并入 `settings/index.js` |
| 删除 | `static/js/settingsnav.js` | - | 含 MutationObserver 补丁，整段移除 |
| 改动 | `static/index.html` | - | 设置页收缩为 shell（nav + panel），其余全部由 schema 渲染 |
| 改动 | `static/js/app.js` | - | import 路径；移除 `initSettingsNav` 与附件目录绑定（见 §8） |
| 改动 | `static/js/plugins.js` | 285 | 改为 `registerSection({group:'extension', order:10})` |
| 改动 | `static/js/themeeditor.js` | 179 | 改为 `registerSection({order:20})`，模板/AI 逻辑不变 |
| 改动 | `static/css/settings-ui.css` | 400 | 唯一视觉源：token、外壳、导航、卡片、控件、动效、窄屏 |
| 改动 | `static/css/layout.css` | 373 | 删除重复的 `.settings-shell/.settings-nav/.settings-section` 规则，仅留面板复用的行样式 |
| 连带 | `agentlink.js` `vectorsettings.js` `hotkeys.js` `bookmarks.js` `chat.js` `shell.js` | - | `saveSettings/openSettings` 的 import 指向 `./settings/index.js`（旧文件已删） |

## 3. 分组与卡片（8 组，替代原 4 组）

| group | 标题 | 图标 | 卡片 |
|---|---|---|---|
| general | 常规 | `i-sliders` | 外观（主题 / 自动保存延迟 / 快捷键跳转行）、快捷键（`hotkeys-list`） |
| files | 文件与目录 | `i-folder` | 目录约定（附件 / 模板 / 每日笔记）、当前 Vault（路径 + 选择 + 打开） |
| agent | Agent Link | `i-link` | MCP Server（命令 + 复制）、git 快照、访问令牌（生成 / 列表 / 撤销） |
| provider | 模型接入 | `i-spark` | Provider（endpoint / 模型 / 协议 / API Key） |
| search | 检索与向量 | `i-search` | 向量检索（开关 / 模型 / 三项权重 / 保存 / 重建 / 知识编译跳转） |
| sync | 同步 | `i-clock` | 每日自动推送（开关 / 远端 / 分支 / 时间 / 模板 / 目录 / 保存 / 立即推送） |
| extension | 扩展与外观 | `i-folder-open` | 插件市场（registry）、自定义 CSS（registry） |
| about | 关于 | `i-file` | 版本 / vault 路径 / 索引规模 / 文档入口 |

`compile` 空分组按方案 A 处理：不保留分组，改为 `search` 组末尾的「知识编译面板 · raw → wiki」跳转行（`compile.open`），原空 tab 删除。

## 4. DOM id 契约对照（52/52 全部存在，无重复 id）

| # | id | 位置 | 控件 | 状态 |
|---|---|---|---|---|
| 1 | `settings-page` | 浮层外壳 | div | 存在 |
| 2 | `settings-search` | 左导航 | search | 存在 |
| 3 | `settings-group-title` | 内容区头部 | h2 | 存在 |
| 4 | `settings-theme` | 外观 | select-one | 存在 |
| 5 | `settings-autosave` | 外观 | range | 存在 |
| 6 | `settings-autosave-value` | 外观 | span | 存在 |
| 7 | `settings-attachment-dir` | 目录约定 | text | 存在 |
| 8 | `settings-template-dir` | 目录约定 | text | 存在 |
| 9 | `settings-daily-dir` | 目录约定 | text | 存在 |
| 10 | `settings-vault-path` | 当前 Vault | text | 存在 |
| 11 | `settings-vault-pick` | 当前 Vault | button | 存在 |
| 12 | `settings-vault-info` | 当前 Vault | p | 存在 |
| 13 | `btn-vault-open` | 当前 Vault | button | 存在 |
| 14 | `hotkeys-list` | 快捷键 | div | 存在 |
| 15 | `agent-mcp-cmd` | MCP Server | code | 存在 |
| 16 | `agent-snapshot-status` | git 快照 | p | 存在 |
| 17 | `settings-snapshots-enabled` | git 快照 | checkbox | 存在 |
| 18 | `agent-token` | 访问令牌 | text | 存在 |
| 19 | `agent-token-role` | 访问令牌 | select-one | 存在 |
| 20 | `agent-token-list` | 访问令牌 | div | 存在 |
| 21 | `agent-provider-endpoint` | Provider | text | 存在 |
| 22 | `agent-provider-model` | Provider | text | 存在 |
| 23 | `agent-provider-protocol` | Provider | select-one | 存在 |
| 24 | `agent-provider-key` | Provider | textarea | 存在 |
| 25 | `vector-section` | 向量检索 | section | 存在 |
| 26 | `vector-enabled` | 向量检索 | checkbox | 存在 |
| 27 | `vector-status` | 向量检索 | p | 存在 |
| 28 | `vector-model` | 向量检索 | text | 存在 |
| 29 | `vector-weight` | 向量检索 | number | 存在 |
| 30 | `vector-graph-weight` | 向量检索 | number | 存在 |
| 31 | `vector-rrf-k` | 向量检索 | number | 存在 |
| 32 | `autopush-section` | 每日自动推送 | section | 存在 |
| 33 | `autopush-enabled` | 每日自动推送 | checkbox | 存在 |
| 34 | `autopush-remote` | 每日自动推送 | text | 存在 |
| 35 | `autopush-branch` | 每日自动推送 | text | 存在 |
| 36 | `autopush-time` | 每日自动推送 | time | 存在 |
| 37 | `autopush-commit-template` | 每日自动推送 | text | 存在 |
| 38 | `autopush-include-dirs` | 每日自动推送 | text | 存在 |
| 39 | `autopush-status` | 每日自动推送 | p | 存在 |
| 40 | `btn-settings-close` | 工具栏 / 欢迎页 | button | 存在 |
| 41 | `btn-agent-token` | 访问令牌 | button | 存在 |
| 42 | `btn-agent-provider-save` | Provider | button | 存在 |
| 43 | `btn-vector-save` | 向量检索 | button | 存在 |
| 44 | `btn-vector-rebuild` | 向量检索 | button | 存在 |
| 45 | `autopush-save` | 每日自动推送 | button | 存在 |
| 46 | `autopush-run` | 每日自动推送 | button | 存在 |
| 47 | `btn-agent-key-toggle` | Provider | button | 存在 |
| 48 | `btn-settings` | 工具栏 / 欢迎页 | submit | 存在 |
| 49 | `btn-open-vault` | 工具栏 / 欢迎页 | submit | 存在 |
| 50 | `link-to-hotkeys` | 外观 | button | 存在 |
| 51 | `plugins-section` | 插件市场 | section | 存在 |
| 52 | `theme-editor-section` | 自定义 CSS 主题 | section | 存在 |

按钮 id（`btn-settings-close / btn-agent-token / btn-agent-provider-save / btn-vector-save / btn-vector-rebuild / autopush-save / autopush-run / btn-agent-key-toggle / btn-settings / btn-open-vault / link-to-hotkeys`）同样保留，事件仍由 `agentlink.js` / `vectorsettings.js` / `autopush.js` / `hotkeys.js` 绑定。

## 5. 验收命令与输出

```bash
$ python -m pytest -q
840 passed, 5 skipped, 1 warning in 79.94s

$ for f in static/js/settings/*.js; do node --input-type=module --check < "$f" || echo "FAIL $f"; done
node check: all ok

$ python -c "import ast,pathlib;[ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('app').rglob('*.py')]"
ast check: all ok
```

## 6. Playwright 断言（msedge channel，`emulate_media(reduced_motion='reduce')`）

| # | 断言 | 实测 |
|---|---|---|
| 1 | 点 `#btn-settings` → 页面可见，左导航 8 项，标题「常规」 | visible=true / tabs=8 / title=常规 |
| 2 | 逐个点 8 个 tab：`.settings-card` ≥1，无横向滚动 | cards=2/2/3/1/1/1/2/1；panel 与 doc 的 `scrollWidth === clientWidth` |
| 3 | 搜索「key」→ 结果含 Provider 卡片；清空恢复 | 结果 `['Provider']`，标题「搜索结果」；清空回「常规」2 卡 |
| 4 | 模型接入：`#agent-provider-key` 为 TEXTAREA 且高度 > 60 | `TEXTAREA`，`clientHeight=82` |
| 5 | 拖 `#settings-autosave` → 标签变化且落盘 | 800ms → 1500ms，`GET /api/settings` = 1500 |
| 6 | 扩展与外观：插件区块 + 自定义 CSS 区块都在，`#plugins-list` 非空 | plugins=true / theme=true / list=3 |
| 7 | `Esc` 关闭 | 关闭成功；快捷键录制中按 Esc 只取消录制、不关页面 |

额外回归：主题切换（写盘 + `data-theme` 生效）、模板目录保存、搜索框 ↓ 直达首个结果控件、知识编译跳转行（关设置 + 开浮层）、无 vault 场景（登录跳过 → 打开 Vault → 设置页可用，提示「当前未打开 vault」）。

截图：`docs/settings-v2/01-general.png`、`02-provider.png`、`03-extension.png`、`04-search.png`。

## 7. 设计规格落实

| 项 | 实现 |
|---|---|
| 外壳 | `min(1040px,94vw) × min(720px,88vh)` 居中；`blur(6px)`；仅外壳有阴影 |
| 左导航 | 220px 固定，独立滚动；搜索框 + 8 tab（`role=tablist/tab` + `aria-selected` + roving tabindex + ↑↓ Home End） |
| 卡片 | 圆角 12、0.5px 发丝边框、`box-shadow:none`、间距 20；行高 52、行间 0.5px、标题与首行 8px |
| 字号 | 仅 11 / 13 / 14 / 20 四档（辅助 11、正文 13、行标签 14、分组标题 20） |
| 图标 | 全页 `.icon` 强制 16×16 + `stroke-width:1.6` + `fill:none`，引用 sprite（修复 300×150 黑块） |
| 控件 | iOS pill 38×22（checkbox + `role=switch` + `aria-checked`）、原生 select 重绘、滑块 `--fill` 填充、`secret` 三行 textarea + 显隐 + 独立保存 |
| 动效 | 外壳 220ms、卡片 240ms 错峰 40ms；`prefers-reduced-motion` 全关 |
| 滚动条 | 8px，悬停才显色 |
| 暗色一致性 | `#settings-page` 覆盖 `--bg/--text/--border/--accent`，动态模块自动深色化 |

## 8. 与任务卡的差异（均为必要改动，已逐条说明）

1. **`app.js` 多改 3 处**：删 `import initSettingsNav`、删 `initSettingsNav()` 调用（模块已删除）；删 `#settings-attachment-dir` 的 change 绑定。后者的原因是：DOM 改为运行时渲染，`bindStaticButtons()` 早于设置页构建，`$('#settings-attachment-dir')` 会拿到 null 直接抛错；附件目录保存改由 schema 行 `action:'dir.change'` 统一处理（`saveAttachmentDir()` 仍导出，行为不变）。
2. **`plugins.js` / `themeeditor.js` 模板去掉自带 `<h3>`**：标题统一由卡片头渲染（registry 的 `title/description`），否则一卡两标题。另加 CSS 兜底 `#settings-page .settings-card__body > h3 { display:none }`。`themeeditor` 的 AI 生成逻辑、`FORBIDDEN_RE`、预览链路未改；仅给两个控件补了 `class="settings-input"` 以接入新控件样式。
3. **动态区块「先入文档再 mount」**：`registerSection` 的 `mount(body)` 在 `card` 挂到文档后才执行。首次实现是先 mount 再 append，插件/主题模块内部的 `$('#theme-css')` 在游离节点上找不到元素，已修正（否则报 `Cannot read properties of null`）。

## 9. 已知边界

- **无 vault 时**：`extension` 组不会注册插件/主题模块，此时显示一张「该分组暂无可配置项」兜底卡，保证不出现空白分组；其余组用 fallback 值正常渲染。
- **搜索键盘**：`↑/↓` 从搜索框跳到末个/首个结果控件，`Enter` 直达首个；之后用 `Tab` / `Shift+Tab` 在控件间移动（不在控件内劫持方向键，避免与 select / 滑块冲突）。
- **字体**：本地优先产品不引入 webfont，使用系统 UI 字体栈（Segoe UI Variable / PingFang SC）；视觉识别靠深色仪器面板、发丝边框与青绿强调色，而非字体。
- **`secret` 行宽**：与其它控件同宽（340px），三行 + 自动换行，长 key 可完整查看；不再出现 160px 单行框。
