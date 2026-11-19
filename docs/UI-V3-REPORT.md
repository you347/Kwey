# 全局视觉统一与字号设置（v3）

- 日期：2026-11-17
- 目标：主界面套用设置页的深色仪器面板风格；新增四个可调字号（全局 / 左侧栏 / 编辑与预览 / 智能体聊天）。
- 结果：pytest **841 passed / 5 skipped**；浏览器控制台 0 错误；四档字号独立生效、即时预览、可恢复默认；设置页同时具备深/浅两套配色，跟随主界面主题。

## 1. 视觉统一（token 映射）

| 用途 | 旧（dark） | 新（dark） | 说明 |
|---|---|---|---|
| 画布 | `#1d2126` | `#16181a` | 编辑区 / ribbon |
| 面板 | `#22262c` | `#1c1f21` | 侧栏 / 标题栏 / 状态栏 / tabbar |
| 浮起行 | `#2a2f36` | `#202426` | 卡片、按钮底、脉冲块 |
| Hover | `#2f353d` | `#24282a` | |
| 主/次/弱文字 | `#d8dee6 / #a6b0bc / #8b96a3` | `#ecefed / #a7afab / #6f7874` | 三级不变 |
| 强调色 | `#4c8dff` 蓝 | `#35c9a6` 青绿 | 与设置页同色 |
| 边框 | `rgba(240,246,252,.13/.24/.07)` | `rgba(255,255,255,.08/.14/.055)` | 0.5px 观感 |
| 圆角 | 4 / 6 / 8 | 6 / 7 / 12 | 控件 / 卡片 / 浮层 |
| 阴影 | 8px·24px | 24px·70px | 只给离屏浮层 |

- 默认主题改为 **deep dark**：`AppSettings.DEFAULTS["theme"]="dark"` + `<html data-theme="dark">`；浅色主题为同形状的暖白纸面（`#fbfbf8 / #f1f3ee / #e7eae3`，强调色 `#0c7f6b`）。
- **设置页不再固定深色**：`html[data-theme="light"] #settings-page` 覆盖一套同形状的浅色 token（面板 `#ffffff` / 导航 `#f3f5f0` / 卡片 `#f7f9f3` / 控件底 `#edf0e9` / 强调 `#0c7f6b` / 边框 `rgba(24,32,28,.12/.20/.07)`）。原先写死的白色 alpha（hover、徽章、代码块、滑块轨道、开关轨道、滚动条、遮罩）全部换成语义 token（`--su-fill/chip/tint/control-off/knob/thumb/scrim/scroll`），深浅共用同一套组件规则。
- 外壳：ribbon 用画布色、侧栏/标题栏/状态栏用面板色、编辑区用画布色；激活标签从「下划线 + 底色」改为 pill（`accent-faint` 底 + `accent` 字），与设置页导航一致。
- 左侧面板 tab 同样 pill 化，并允许换行：7 个 tab 在 240px 侧栏内不再被横向裁切（字号放大时自动变两行）。
- 浮层 / 对话框背景改为 `rgba(6,8,8,.55)` + `blur(4px)`，与设置页 `blur(6px)` 同一语言。
- 登录页强调色与焦点环由蓝转青绿，主按钮文字改为深色（`--accent-text`），对比度 > 4.5:1。
- 关键高度随字号伸缩：标题栏 `ui+27`、标签页 `ui+17`、状态栏 `ui+13`、按钮 `ui+15`、文件树行 `sidebar+13`。

## 2. 字号系统

| CSS 变量 | 作用域 | 默认 | 可调范围 |
|---|---|---|---|
| `--font-ui` | 全局：按钮 / 标签 / 面板 / 设置页 | 13px | 11–18 |
| `--font-sidebar` | 左侧栏：文件树、标签、列表、侧栏 tab | 13px | 11–18 |
| `--font-editor` | 中心区：源码编辑 + 预览 | 15px | 12–22 |
| `--font-chat` | 智能体聊天：消息、步骤条、输入框 | 13px | 11–18 |

- 字号阶梯由全局字号派生：`--fs-xs=ui-1`、`--fs-sm=ui`、`--fs-md=ui+2`、`--fs-lg=ui+7`（13 基准时正好是 12/13/15/20，与原四档一致）。
- 左侧栏与聊天面板在各自容器内重定义这四个变量，因此只缩放局部，不影响其它区域。
- 设置页自身也接入了 `--font-ui`，全局字号变化时设置页同步缩放。

## 3. 设置入口

`常规 → 字体` 卡片：四个滑块（实时数值）+ 「恢复默认」。

- **拖动即时预览**：`input` 事件直接写 `<html>` 内联变量；`change`（松手）才 `PUT /api/settings`。
- 保存后 `emit('fonts-changed')` → `app.js#applyFonts()` 统一写变量，并调 `editor.refreshLayout()` 重排 CodeMirror（字体度量变化后行高/光标位置会偏）。
- 「恢复默认」把四项写回 13/13/15/13，同时同步滑块与 `<html>` 变量。

## 4. 后端

- `AppSettings.DEFAULTS` 新增 `fonts`，`theme` 默认改 `dark`。
- `AppSettings.set()` 对 `fonts` 按子键合并（与 `vector` 同策略）：只改聊天字号不会丢全局/侧栏/编辑的已存值。
- `SettingsUpdateRequest` 新增 `fonts: Optional[Dict[str, Any]]`，`PUT /api/settings` 直接透传并持久化到 `.kwey/settings.json`。
- 新增测试 `tests/test_api_notes.py::TestSystem::test_fonts_setting_merge`。

## 5. 验证

| 项 | 结果 |
|---|---|
| `python -m pytest -q` | 841 passed, 5 skipped |
| Playwright：字体卡 | 4 个滑块存在，默认值 13 / 13 / 15 / 13，数值标签同步 |
| Playwright：即时预览 | `input` 后 `--font-ui` = 16px，标签 16px；`change` 后 API `fonts.ui=16` |
| Playwright：编辑器 | `--font-editor` 默认 15px 生效于 `.CodeMirror` 与 `.preview-pane` |
| Playwright：极值 | 18 / 17 / 22 / 17 下文档与设置页 `scrollWidth === clientWidth`，无横向溢出 |
| Playwright：恢复默认 | 变量、API、滑块三处同时回到 13 |
| 浅色设置页 | 面板 `#ffffff` / 导航 `#f3f5f0` / 卡片 `#f7f9f3` / 控件 `#edf0e9`；辅助文字 `#6b746e` 对卡片对比度 4.55:1；无残留白色 alpha |
| 浅色主界面 | 画布 `#fbfbf8`、面板 `#f1f3ee`、浮起 `#e7eae3`，标签 pill 与深色同构 |

截图：`docs/ui-v3/01-app-dark.png`、`02-app-large-fonts.png`、`03-settings-fonts.png`、`04-settings-light.png`、`05-app-light.png`。

## 6. 已知边界

- 设置页跟随主界面主题：深色为仪器面板，浅色为同形状的暖白纸面（不再固定深色）。
- 浅色三级文字取 `#6b746e`（对卡片 4.55:1）；深色三级文字沿用先前规格的 `#6f7874`（对卡片 3.44:1，属辅助说明用色）。
- 字号只作用于使用设计 token 的元素；用户自定义 CSS 里写死的 px 不受影响。
- 左侧栏 7 个面板 tab 在 240px 宽度下默认折成两行（优先保证全部可见，而不是横向滚动）。
- 旧 vault 的 `settings.json` 没有 `fonts` 字段时，加载阶段自动补默认值，无需迁移。
