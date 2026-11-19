# Kwey 项目长期笔记

## 项目
Kwey：本地优先、Agent 原生的个人知识库软件（对标 Obsidian），工作区即代码仓。

## 架构基调（已定）
- 磁盘 Markdown 文件夹 = 唯一事实源；SQLite（FTS5 全文 + 向量）仅索引、可重建。
- 四层：UI 层 / Agent Link 层（MCP Server + REST/WS + 事件总线 + 权限）/ 核心域层（Vault Service 统一写路径 + 混合检索）/ Agent Provider 适配器（指向用户自建 agent 系统）。
- agent 写入走结构化 ops + diff 确认 + 快照回滚。

## 用户自建 Agent 系统
- 作为 Kwey 的 MCP 客户端（检索/读写/管理）与事件订阅方。
- Kwey 内嵌整理功能通过 Provider 适配器委派给该系统（HTTP / OpenAI 兼容协议）。

## 技术栈（已定，2026-11-12）
- 用户主方向是 Python。
- Python 3.11+ / FastAPI+uvicorn 本地服务 / pywebview（WebView2）桌面壳。
- 前端 CodeMirror 6 + D3；解析 markdown-it-py + python-frontmatter；监听 watchfiles。
- 全文 SQLite FTS5；向量 **Chroma（chromadb）**——用户 agent 项目同款，embedding 默认 bge-small-zh-v1.5 留配置口。
- MCP 官方 mcp SDK（FastMCP）；快照用 git（GitPython）；打包 PyInstaller。

## 特点功能（方向已定，2026-11-12 晚）
- 用户提出 3 个：F1 AI 自动构建图谱（向量召回+LLM 确认关系+MOC）；F2 每日自动推送 Gitee/GitHub（vault 级开关、启动补推、pull --rebase）；F3 AI 个人信息库（projects/profile/lessons 三分区 50/30/20 注入配比，存 .kwey/memory/ 普通 md，diff 落盘时刻闭环更新）。
- 补充构思：RAG 问答、孤儿笔记/断链体检、知识库仪表盘、复习流水线、会话式批量整理、网页剪藏。
- 落地顺序：F2 → F3 基础版 → F1 → F4。规格详见 DESIGN.md 第七节。

## 实现现状（2026-11-13）
- M1 P0 13 项已完成并 QA 验收；同日完成阶段 0+1+2：修前端交互硬伤、Obsidian 式 ribbon/多 tab 面板/可拖拽分栏、P1 功能（图谱/大纲/模板/每日笔记/书签/最近/回收站/导出/快捷键自定义）。
- 实际落地与规划差异：编辑器为 vendored **CodeMirror 5**（非 CM6，缺失时降级 textarea）；图谱为**自研 SVG 力导向**（未引入 D3）；未引入任何新第三方依赖。
- 同日晚完成 **M2 Agent Link**（四卡并行子智能体）：MCP stdio Server、Agent REST/SSE + token 三级、Provider + 结构化 ops + diff 确认、git 快照时间线/回滚；编排者统一注册路由、放开设置字段、写路径挂钩快照、补前端三个模块。
- 最终验证：pytest **225 passed**；28 个 JS 模块语法通过；Playwright 端到端 7/7（含 MCP stdio、REST 鉴权、SSE、diff、时间线）。详见 README.md 与当日日志。

## 待定 / 进行中
- 2026-11-17 聊天上下文：`chat.js` 随焦点标签自动带 `context{path,kind}`，输入框上方三条（本篇/文件夹/不使用）；内核 `_context_hint()` 二次校验后拼 system，工具契约不变。
- 2026-11-17 v3：全局默认深色 + 与设置页统一 token；新增四档字号设置（`fonts`：ui/sidebar/editor/chat，CSS 变量注入）。新增视觉项优先复用 `--font-*` 与 `--fs-*`，别再写死 px；设置页已支持深/浅两套 token（跟随 data-theme），新组件用 `--su-*` 语义 token，不要写白色 alpha。
- 设置页已于 2026-11-17 重建为 **schema 驱动**（`static/js/settings/` 五文件；`settings-ui.css` 为唯一视觉源；52 个 DOM id 契约不变）。新增设置项：在 `schema.js` 加卡片/行即可，动态模块用 `registerSection`。
- 基础功能要求"尽可能多、仿 Obsidian"，完整清单见 DESIGN.md（P0/P1/P2 共 31 项）。
- 2026-11-16 Wave B（A4-A8 并行卡）：A4 权限治理、A5 CLI、A6 F1 知识编译、A7 **F3 个人信息库**（已完成：app/memory/store.py + routes_memory + memory.js，命令 `memory.open`，三分区 50/30/20 注入，待集成卡接线 server.py/app.js）、A8 上下文工程。F2 已于 11-14 完成。
- 2026-11-16 Wave C：C1 **P2 Canvas 无限画布**（已完成：`app/api/routes_canvas.py` + `static/js/canvas.js`，命令 `canvas.open`，Obsidian 兼容 .canvas JSON，待集成卡挂 router/init）。
- 未实现：Chroma 向量混合检索（`mode=hybrid` 仍等同 fts）、F4 构思。（P1-19 分屏、PDF 导出、P2 Slides/Canvas 已完成）

## 打包与发布（2026-11-19）
- 版本 **0.2.0**（单一真源 `app/__init__.py:__version__`，server.py 与 MCP SERVER_INFO 均引用它）。
- `kwey.spec` = onedir（非 onefile：启动快、pythonnet/WebView2 稳）；`build.py` = 打包 + 自动 `--diag` 自检；静态资源经 `app/runtime.py:resource_root()`（`sys._MEIPASS`）解析，**不要再用 `Path(__file__)` 拼 static**。
- 冻结包四模式：桌面窗口 / `--server` / `--mcp`（MCP 实现已下沉 `app/agent/mcp_stdio.py`，`kwey_mcp.py` 只是薄封装）/ `--diag`。
- 端口：被本程序占用→复用已有实例；被其它程序占用→顺延 20 个端口（避免桌面窗口指向别人）。
- 日志 `%LOCALAPPDATA%\Kwey\logs\kwey.log`，自检 `%LOCALAPPDATA%\Kwey\diag.json`；窗口化无控制台时 stdout/stderr 重定向到日志。
- 产物 `release/Kwey/`（52.9MB）+ `release/Kwey-0.2.0-win64.zip`（27MB）；验证 910 passed。上线仍缺：安装器、代码签名、自动更新、LICENSE、图标、仓库提交清理。
- 下一步：Wave B 集成（统一跑全量 pytest + 挂路由/命令/闭环钩子），再按 DESIGN.md 第七节推进 F1/F4 与 P2。

## Verbot 集成（2026-11-19 定案）
- Verbot = 独立多服务栈（FastAPI :8000 + Spring Boot :8082 + Vite :5175 + Tauri 壳），**无 MCP、无 OpenAI 兼容服务端**，是 OpenAI 协议的客户端。
- 因此 **不做反向委派**（Kwey Provider 是同步 chat，Verbot 是异步任务 + WS，语义不匹配；薄 facade 等价直连百炼）。
- 链路定为**单向**：Verbot → Kwey，走 REST 而非 MCP。Verbot 拉 `/api/agent/contract` 取 schema，手工裁 3~5 个工具注册进 `app/tools/registry.py`。
- Kwey 的 MCP 只有 stdio；`KweyMCPServer.handle()` 与传输解耦，要加 Streamable HTTP 只是包一个路由（约 100 行），非必需不做。
- 已补「写入归因」：写类工具落 `.kwey/agent-journal.jsonl`（role/name/action/path/ts），`GET /api/agent/audit?actor=<token名>` 与工具 `kwey.audit` 可查。工具数 23。
