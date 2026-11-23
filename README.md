# Kwey · 个人知识库（M1 + P1 + M2 + Wave A 全量 + A2 + F2）

本地优先、文件为源、Agent 原生的个人知识库桌面软件（对标 Obsidian 基础体验）。
**磁盘 Markdown 文件夹 = 唯一事实源**；SQLite（FTS5 / 链接图）与设置仅做索引/配置，可整库重建。

> 当前进度：**M1（P0 13 项）+ P1 体验补全 + M2 Agent Link + Wave A（权限治理 / CLI / F1 知识编译 / F3 个人信息库 / A8 上下文工程）+ A2 混合检索 / WS 握手鉴权** 已完成。P2 与 F4（RAG/复习/剪藏等）仍按 `DESIGN.md` 规划。

---

## 功能清单

### 基础能力（P0，13 项）

| # | 功能 | 说明 |
|---|------|------|
| 1 | Vault 打开/扫描/增量索引 | watchfiles 防抖监听外部改动，仅增量更新受影响索引 |
| 2 | Markdown 编辑器：源码 + 实时预览 | CodeMirror（vendored）；源码/分屏/预览三模式；**预览渲染编辑器当前未落盘正文** |
| 3 | `[[双链]]`（含别名）+ `![[嵌入]]` | 可点击跳转、断链提示创建、嵌入回填、`[[` 行内补全 |
| 4 | 反向链接 + 未链接提及 | 右面板列出全部反链与提及；一键转双链 |
| 5 | 嵌套标签 `#a/b` + 标签面板 | frontmatter tags 与行内标签合并统计，层级树过滤 |
| 6 | 全文搜索 + 操作符 | FTS5 BM25；`path:` `tag:` `file:`；高亮；侧栏面板 + 浮层双入口 |
| 7 | 文件树：新建/改名/移动/删除 | 改名/移动自动全库重写链接（0 断链）；删除进 `.trash/` |
| 8 | 快速切换器 `Ctrl+O` | 模糊匹配、键盘导航 |
| 9 | 命令面板 `Ctrl+P` | 命令注册表（按 id 去重）+ 模糊匹配 + 快捷键提示 |
| 10 | frontmatter 属性 | 属性区展示/编辑，写回保留自定义字段与正文 |
| 11 | 多标签页 | 开/关/切，localStorage 持久化，重启恢复 |
| 12 | 附件 | 粘贴/拖入图片 → `attachments/`（可配置）→ 插入相对链接 |
| 13 | 暗色/亮色主题 | 全应用 CSS 变量即时切换，持久化到 settings.json |

### P1 体验补全

| # | 功能 | 说明 |
|---|------|------|
| 14 | 全局图谱 + 本地图谱 | 自研 SVG 力导向（零第三方依赖）；节点可拖拽、点击跳转 |
| 15 | 大纲面板 | 按标题层级展示，点击跳到对应行 |
| 16 | 模板插入 | `templates/` 下 .md；`{{date}}` `{{time}}` `{{datetime}}` `{{title}}` |
| 17 | 每日笔记 | `Ctrl+Shift+D` 打开或创建 `daily/YYYY-MM-DD.md` |
| 18 | 书签 / 最近文件 | 设置持久化；`Ctrl+Shift+B` 收藏 |
| 20 | 快捷键自定义 | 设置页录制组合键，冲突检测，命令面板标签同步 |
| 21 | 导出 MD / HTML | HTML 内联图片为 base64，离线可打开；`Ctrl+Shift+E` 导出 MD |
| 24 | 回收站恢复 | 列出/恢复/彻底删除/清空（二次确认）；逐文件回原路径，冲突去重 |

### M2 Agent Link（双向连接，核心差异化）

| 卡 | 能力 | 入口 |
|---|---|---|
| M2-1 | **MCP Server**（stdio JSON-RPC 2.0） | `python kwey_mcp.py --vault "<vault>"`；9 个 tools（search/read/create/write/delete/link/tags/graph.query/organize）+ notes as resources；零新依赖，未装 `mcp` SDK 也可跑 |
| M2-2 | **Agent REST / SSE + token** | `/api/agent/*` 检索读写、`GET /api/agent/events` SSE、`POST /api/agent/tokens`；`X-Kwey-Token` 或 `Authorization: Bearer`，角色 read / write / admin；首次引导 `POST /api/agent/bootstrap` |
| M2-3 | **Provider + 结构化 ops + diff 确认** | `.kwey/provider.json`（openai 兼容 / 自定义 HTTP，标准库 urllib）；`/api/agent/organize/preview` 只算 diff 不落盘，`/api/agent/organize/apply` 必须 `confirm=true` 才走统一写路径；UI 命令「整理当前笔记（diff 确认）」 |
| M2-4 | **git 快照时间线 / 回滚** | `/api/history/status|log|show|rollback`；写路径自动 commit（默认关闭，设置页开启）；回滚前自动提交留存当前版本，回放后重索引并广播 |

- 设置页「Agent Link」可生成/撤销 token、配置 Provider、开关 git 快照、复制 MCP 启动命令。
- `organize` 预览/应用为本地 UI 端点（无需 token），REST 读写端点一律需 token；所有笔记写操作仍统一走 `VaultService`。

### Wave A（Agent 治理与知识编译，本轮新增）

| 卡 | 能力 | 入口 |
|---|---|---|
| A4 | **权限治理**：token 改 sha256(salt+token) 存储、明文只回显一次、旧库自动迁移；organize/chat/pulse/provider 全端点鉴权 | `X-Kwey-Token` / Bearer；WS 走 `?token=` |
| A5 | **kwey CLI**：search/read/write/tree/export 五子命令，`--json` 输出统一信封，错误走 stderr | `python kwey_cli.py search --vault "<vault>" "关键词" --json`；Agent 规范见 `AGENTS.md` |
| A6 | **F1 知识编译**：raw/ 只读 → Provider 生成 ops → diff 预览 → 确认落盘；自动 MOC；`health_check` 查孤儿/断链/schema 漂移 | 命令「F1 知识编译」；`POST /api/compile/preview|apply`、`GET /api/compile/health` |
| A7 | **F3 个人信息库**：`.kwey/memory/{projects,profile,lessons}.md` 普通 md，50/30/20 注入配额，diff 落盘时闭环更新 | 命令「个人信息库」；`GET /api/memory`、`POST /api/memory/inject|update` |
| A8 | **上下文工程**：master index + FTS/向量召回 + 双向图扩展，按预算组装；sources 可溯源 | `GET /api/context/build?query=&max_chars=`；`build_context(vault, query, ...)` 供 A6/A7 复用 |
| A2 | **混合检索解锁**：`mode=hybrid` 真做 FTS+向量+图 RRF；settings.vector 可写、缺 chromadb 自动降级；MCP `kwey.search` 同走 hybrid | 设置页「向量检索」开关/模型/权重/重建；`GET /api/search/vector/status`、`POST /api/search/vector/rebuild` |
| WS 鉴权 | A4 遗留落地：`/ws/events?token=` 三级校验，无效 close 4401 / 越权 4403；无 token 仅本地回环放行 | 浏览器 UI 无感知；远端需带 token |
| F2 | **每日自动推送**：启动/切 vault 自动补推，`pull --rebase` 冲突暂停、下次启动补推；设置页可直接绑定仓库地址（写 `.git/config`，凭据交给系统 git） | 设置页「每日自动推送」区块（地址/绑定/远端/分支/时间/模板/包含目录）；`/api/autopush/config|run|status|remotes|bind`；命令 `autopush.run` |
| Import | **库导入（P0）**：从 Obsidian / Logseq / 任意 Markdown 目录迁入，先扫描预览（只读）再落盘；保留目录结构与附件，三种同名冲突策略，批量导入只提交一次快照 | 设置页「导入已有笔记库」；`POST /api/import/scan`、`POST /api/import/apply` |
| PDF | **PDF / 打印导出**：reportlab 开箱、weasyprint 可选增强；图片 base64 内联 | 命令「导出当前笔记为 PDF」「打印 / 另存为 PDF」；`GET /api/export?format=pdf|print` |

- 新命令面板入口：F1 知识编译、个人信息库、自动推送、导出 PDF / 打印；新浮层 `#overlay-compile` 与记忆面板（自绘 DOM）。

### Wave A 第二批（C1–C6：P2 补全 + F4）

| 卡 | 能力 | 入口 |
|---|---|---|
| C1 | **Canvas 无限画布**：笔记文本卡 + 双链连线、平移/缩放/框选，Obsidian 兼容 `.canvas` JSON | 命令「打开画布（Canvas）」；`/api/canvas/list|load|save` |
| C2 | **周期笔记**：ISO 周/月笔记，模板占位符 + 前后周期链接 + 周期内笔记汇总 | 命令「打开/创建本周笔记」「本月笔记」；`/api/periodic/week|month` |
| C3 | **插件市场 + 受信插件**：市场式 UI（搜索/分类/推荐卡片/安装-使用），内置「知识库体检」「随机漫游」「今日回顾」一键安装；权限白名单、启用即注册命令、卸载全撤销 | 设置页「插件市场」；`/api/plugins/catalog|install|uninstall|toggle|entry` |
| C4 | **自定义 CSS 主题**：手写或「用一句话让内置 Agent 生成」CSS，实时预览、`--accent` 等变量覆盖，过滤 `@import`/`url(http`/`javascript:` | 设置页「自定义 CSS 主题」区块；`/api/theme/custom`、`POST /api/theme/generate` |
| C5 | **F4 RAG 问答**：build_context 组证据 → Provider 生成带引用答案 → 幻觉引用剥离；无 Provider 时摘录式回答 | 命令「问我库（RAG 问答）」；`POST /api/rag/ask` |
| C6 | **F4 复习流水线 + 网页剪藏**：frontmatter `review_due` 间隔重复 + 每日清单；URL 抓正文转 MD，SSRF 拦截 | 命令「复习面板」「加入复习」「剪藏网页」；`/api/review/*`、`/api/clip/*` |

- 全部浮层/面板自绘 DOM（C1/C3/C4/C6），仅 RAG 浮层在 index.html 静态声明。

### 界面外壳（Obsidian 式）

左侧 **ribbon** + 左右多 tab 面板（文件/搜索/标签/书签/最近/大纲/回收站 · 反链/本地图谱）、可拖拽分隔条与宽度记忆、状态栏（字数/光标行列/路径/保存/索引）、全量 SVG 图标、0.5px 微边框、单一强调色、4 档字号、`prefers-reduced-motion` 降级。

## 快捷键

| 组合 | 动作 | 组合 | 动作 |
|---|---|---|---|
| `Ctrl+O` | 快速切换 | `Ctrl+P` | 命令面板 |
| `Ctrl+Shift+F` | 搜索 | `Ctrl+N` | 新建笔记 |
| `Ctrl+S` | 保存 | `Ctrl+W` | 关闭标签 |
| `Ctrl+Shift+D` | 每日笔记 | `Ctrl+Shift+T` | 插入模板 |
| `Ctrl+Shift+G` | 全局图谱 | `Ctrl+Shift+E` | 导出 MD |
| `Ctrl+Shift+B` | 收藏 | `Ctrl+Shift+O` | 大纲面板 |

全部可在「设置 → 快捷键」重新录制。M2 的三个入口（Agent Link 设置、整理 diff、快照时间线）走命令面板。

## 启动方式

```bash
python -m pip install -r requirements.txt
python main.py --server                       # 浏览器模式（自动打开）
python main.py                                # pywebview 桌面模式（WebView2）
python main.py --server --vault D:/my/notes --port 17630 --no-browser

# MCP Server（供外部 Agent 以 stdio 接入）
python kwey_mcp.py --vault "D:/my/notes"

# Agent REST 示例（先在设置页生成 admin token）
curl -H "X-Kwey-Token: <token>" "http://127.0.0.1:17630/api/agent/health"
```

默认端口 `17630`。首启未指定 `--vault` 时，在登录层/设置页选择 vault 文件夹。

## 打包分发（Windows）

```bash
python -m pip install -r requirements.txt pyinstaller
python build.py                 # 产物：dist/Kwey/Kwey.exe（约 53 MB，onedir）
python build.py --console       # 带控制台的调试版，便于看 traceback
```

- 分发时**整个 `dist/Kwey/` 目录一起打包**（`Kwey.exe` 与 `_internal/` 必须同级）；压缩包形式可直接给用户解压运行。
- 目标机要求：Windows 10 1809+ / Windows 11；**WebView2 Runtime**（Win11 自带，Win10 需装 Evergreen Runtime）；`.NET Framework 4.7.2+`（系统自带）。缺 WebView2 时自动降级为浏览器模式。
- 可选依赖：`git` 仅 F2 自动推送与快照回滚需要，缺失时相关功能自动降级；`chromadb` / `sentence-transformers` 未打包，混合检索自动降级为 FTS。
- 用户数据位置：vault 内 `.kwey/`（索引、设置、token 哈希、自定义 CSS）；日志 `%LOCALAPPDATA%\Kwey\logs\kwey.log`；自检报告 `%LOCALAPPDATA%\Kwey\diag.json`。
- 打包后同样支持全部命令行能力：

```bash
Kwey.exe                                   # 桌面窗口（默认 17630，端口占用时自动顺延）
Kwey.exe --vault "D:/notes" --port 17631
Kwey.exe --server --no-browser             # 纯服务模式
Kwey.exe --mcp --vault "D:/notes"          # MCP stdio，供外部 Agent 直接接入
Kwey.exe --diag                            # 环境自检，排障时先跑这个
```

## 目录说明

```
Kwey/
├── main.py                 # 唯一入口：--server / pywebview / --mcp / --diag 四模式
├── build.py                # 一键打包（PyInstaller）+ 产物自检
├── kwey.spec               # 打包配置：onedir、static 资源、uvicorn/webview hiddenimports
├── kwey_mcp.py             # MCP stdio 入口（薄封装，实现见 app/agent/mcp_stdio.py）
├── app/
│   ├── config.py           # .kwey/settings.json 读写（P1 + M2 设置字段）
│   ├── runtime.py          # 冻结包适配：资源根 / 用户数据目录 / 标准流重定向
│   ├── paths.py / models.py / events.py / server.py
│   ├── agent/              # M2：mcp_server / auth / provider / ops
│   ├── parser/             # frontmatter / wikilink / tags / renderer / outline
│   ├── index/              # SQLite FTS5 + 内存链接图
│   ├── vault/              # VaultService 统一写路径 + watcher + 链接重写 + 回收站 + snapshots
│   └── api/                # REST 路由（system/notes/files/search/graph/trash/templates/export/
│                           #   agent/organize/history/provider）+ /ws/events
├── static/
│   ├── index.html          # 三栏 + ribbon + 面板 + 浮层 + 设置页 + 登录/开屏
│   ├── css/                # themes/base/layout/panels/shell/editor/splash
│   ├── js/                 # 28 个 ES Module（无构建链）
│   └── vendor/codemirror/  # vendored CM5（缺失时自动降级增强 textarea）
├── tests/                  # pytest：解析器/索引/写路径/API/P1/M2/性能
└── docs/                   # ARCHITECTURE.md / PRD.md / QA-REPORT.md / 图
```

## 架构硬约束

1. **磁盘 Markdown = 唯一事实源**：删掉 `.kwey/` 后重启，索引自动整库重建，内容零丢失（G4）。
2. **统一写路径**：一切写操作经 `VaultService`：`RLock → 原子写盘 → 自写记录 → 防抖增量索引 → EventBus 广播`；UI、REST、MCP、ops apply 共用此通道。git 快照是写路径的可选挂钩，失败只记日志、绝不阻断写入。
3. **路径约定**：API 全程 vault 相对正斜杠路径；`.kwey/` `.trash/` `.git/` `templates/` 不可经普通路径 API 写入。
4. **API 响应**：统一 `{code, data, message}`；错误码 400/401/403/404/409/500。
5. **性能**：1000 笔记全文搜索（含操作符）稳态 ≤200ms。

## 测试

```bash
python -m pytest tests/ -v            # 全量：837 passed / 5 skipped
python -m pytest tests/ -m perf -v    # 仅性能基准
```

## 已知限制 / 未实现

- 编辑器为 **CodeMirror 5**（vendored）；`split` 指同一篇笔记的源码+预览并排；**P1-19 两篇独立笔记对照编辑已实现**：`Ctrl+Shift+S` / 工具栏分屏按钮 / 文件树与标签右键「在右屏打开」，副屏有独立标签栏、独立编辑器实例与独立保存（`splitview.js` + `tabs.paneState('secondary')`）。
- FTS5 中文为按字切分；`/api/search?mode=hybrid` 已做 FTS+向量+链接图 RRF 融合，但默认无 `chromadb`，此时**自动降级为纯 FTS**（结果等价、返回 `data.vector.backend="null"`）。设置页「向量检索」可开启并重建索引；模型填 `hashing` 用免下载词向量，填 `bge-small-zh-v1.5` 需先装 `sentence-transformers`（首次联网下载）。
- **PDF 导出已落地**：`format=pdf` 走 reportlab（已在 requirements，纯 Python 开箱可用）或 weasyprint（可选，保真更高）；命令面板「导出当前笔记为 PDF」「打印 / 另存为 PDF」。无引擎时明确 400 并引导 `format=print`。
- **F2 每日自动推送已接线**：设置页「每日自动推送」区块（仓库地址 + 绑定按钮、开关、远端、分支、时间、提交模板、包含目录）+ 命令面板入口；启动与切换 vault 时自动补推，`pull --rebase` 冲突暂停。远端地址在应用内绑定（写入仓库 `.git/config`），凭据交给系统 git credential helper；分支留空自动跟随当前分支。
- **未实现**：F4 中的「会话式批量整理」已有 M2 organize 近似能力；真正未做的仅**移动端原生**。F1 编译、F2 推送、F3 记忆、F4 RAG/复习/剪藏/pulse、P2 Canvas/周期/插件/自定义 CSS/**Slides 幻灯片**均已落地（Slides：命令面板「演示当前笔记（幻灯片）」，按 `---` / `##` / `#` 分页，←→/空格翻页、Esc 退出）。
- MCP 为自研最小 JSON-RPC（未装 `mcp` SDK 也可运行），23 个工具；stdio 本地管道无 token（父进程即信任边界）。**只有 stdio 传输**，无 Streamable HTTP（协议分发层 `KweyMCPServer.handle` 与传输解耦，加 HTTP 只需包一个路由）。
- **写入归因**：写类工具都会把 `{role, name, action, path, ts}` 落到 `.kwey/agent-journal.jsonl`，可用 `GET /api/agent/audit?actor=<token名>` 或工具 `kwey.audit` 查询——外部 agent 用自己的 token 写入后可按名字定位它改过什么。
- `/ws/events` 渐进式鉴权：带 `?token=` 严格校验三级 token（无效 close 4401 / 权限不足 4403）；无 token 仅本地回环放行（浏览器 UI / TestClient）。远端Exposure 前务必启用 token。
- Provider 需在设置页配置 endpoint 才能用「指令生成 ops」；未配置时整理面板的本地规则（标签 / 建链）仍可用。
- **API Key 输入框**：用可换行的 textarea（长 key 完整可见、默认圆点遮盖、可切「显示」）；设置页本地 UI 会自动 bootstrap token 并带 token 保存 Provider 配置——A4 鉴权后不带 token 会 401，已修复。
- git 快照**默认关闭**（不擅自在用户 vault 里 `git init`）；开启后每次写盘自动提交，可用 `/api/history` 查时间线与回滚。
- SSE `/api/agent/events` 需 `X-Kwey-Token` 头；浏览器内 UI 用 `/ws/events`（可选 `?token=`），EventSource 无法带自定义头。
- 图谱为自研 SVG 力导向（非 D3），节点 >500 时 O(n²) 计算偏重，无缩放平移。
- `templates/` 被索引忽略，模板 frontmatter 占位符须加引号（`title: "{{title}}"`）；大纲基于已落盘正文。
- pywebview 模式依赖系统 WebView2 运行时；不可用时自动降级浏览器模式。
