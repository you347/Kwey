# Kwey 产品设计文档 v1

> 本地优先、文件为源、Agent 原生的个人知识库。对标 Obsidian 基础体验，Agent 双向连接为核心差异。
> 更新：2026-11-12 技术栈敲定（Python 主方向）

## 一、技术栈（已定）

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 主方向 |
| 桌面壳 | pywebview（Windows 用 Edge WebView2） | Python 核心 + 系统原生 WebView 渲染前端 |
| 本地服务 | FastAPI + uvicorn | REST + WebSocket + SSE，UI 与 Agent Link 共用同一服务 |
| 前端 | HTML/CSS/JS（TS 可选）+ CodeMirror 6 | Obsidian 编辑器本身也是 CodeMirror；图谱用 D3.js |
| Markdown 解析 | markdown-it-py + python-frontmatter | wikilink/嵌入语法自己写插件 |
| 文件监听 | watchfiles | 防抖归一，触发重索引 + 事件广播 |
| 全文检索 | SQLite FTS5（内置 sqlite3） | BM25，支持 path:/tag: 等操作符 |
| 向量检索 | **Chroma（chromadb）** | 与现有 agent 项目同一套向量库；embedding 默认 bge-small-zh-v1.5（本地、中文友好），留配置接口 |
| 图存储 | SQLite 表（links/tags）+ 内存图 | 图谱渲染数据从这出 |
| MCP | 官方 `mcp` SDK（FastMCP） | stdio / streamable-http 两种暴露方式 |
| 快照回滚 | git（GitPython） | agent 写入前自动 commit |
| 打包 | PyInstaller | 单 exe + WebView2 |

**关键原则**：磁盘 Markdown 文件夹 = 唯一事实源；SQLite + Chroma 仅做索引，全部可重建。

## 二、架构分层（Python 版）

```
pywebview 壳（前端：CodeMirror 6 / D3 / 面板）
        │ HTTP/WS
FastAPI 本地服务
 ├─ 笔记 API（CRUD/搜索/图谱）  ← UI 调用
 ├─ Agent Link（MCP Server + REST/WS + 事件总线 + token 权限） ← 外部 agent 调用
 └─ Agent Provider 适配器（HTTP/OpenAI 兼容 → 用户自建 agent 系统）
        │
Vault Service（统一写路径：加锁 → 写盘 → 防抖重索引 → 广播事件）
        │
磁盘 Markdown 仓库（事实源） + SQLite 索引 + Chroma 向量库
```

## 三、数据格式约定

- 笔记：UTF-8 Markdown + YAML frontmatter（title/tags/created/自定义）
- Vault 根目录任意组织，`.kwey/` 存配置：settings.json、tokens.json、provider.json、templates/
- 附件默认 `attachments/`（可配置）
- 回收站：`.trash/`
- agent 返回结构化 ops：
  ```json
  {"ops": [
    {"type": "create", "path": "...", "content": "..."},
    {"type": "append", "path": "...", "section": "..."},
    {"type": "tag",    "path": "...", "add": ["#x"], "remove": []},
    {"type": "link",   "from": "...", "to": "..."}
  ]}
  ```
- 事件推送格式：`{"event": "created|updated|deleted|moved|links_changed", "path": "...", "ts": ...}`

## 四、基础功能清单（仿 Obsidian，尽可能全）

### P0 — 最小可用（M1 里程碑）
| # | 功能 | 对标 |
|---|---|---|
| 1 | Vault 打开/扫描/增量索引 | 打开文件夹 |
| 2 | Markdown 编辑器：源码 + 实时预览 | 编辑模式 |
| 3 | `[[双链]]`（含别名 `[[a\|b]]`）+ 嵌入 `![[...]]` | 核心语法 |
| 4 | 反向链接面板 + 未链接提及 | Backlinks / Unlinked mentions |
| 5 | 嵌套标签 `#a/b` + 标签面板 | Tags |
| 6 | 全文搜索 + 操作符（path: tag: file:） | Search |
| 7 | 文件树：新建/改名/移动/删除，改名自动更新全库链接 | 文件管理 |
| 8 | 快速切换器 Ctrl+O | Quick switcher |
| 9 | 命令面板 Ctrl+P | Command palette |
| 10 | frontmatter 属性 | Properties |
| 11 | 多标签页 | Tabs |
| 12 | 附件：粘贴/拖入图片存 attachments/ | 附件管理 |
| 13 | 暗色/亮色主题切换 | Appearance |

### P1 — 体验补全（M3 里程碑）
| # | 功能 | 对标 |
|---|---|---|
| 14 | 全局图谱 + 本地图谱（D3 力导向） | Graph view |
| 15 | 大纲面板（标题层级跳转） | Outline |
| 16 | 模板插入（templates/） | Templates |
| 17 | 每日笔记（ daily/，快捷键生成） | Daily notes |
| 18 | 书签/星标 + 最近文件 | Bookmarks / Recent |
| 19 | 分屏左右对照编辑 | Split view |
| 20 | 快捷键自定义设置 | Hotkeys |
| 21 | 导出：MD / HTML / PDF | Export |
| 22 | git 快照：时间线 + 一键回滚 | File recovery |
| 23 | 向量语义检索（**sqlite-vec + 本地 ONNX**，已进包）+ 与 FTS5 混合排序 | Omnisearch 类 |
| 24 | 回收站恢复 | .trash |

### P2 — 远期（M4+）
| # | 功能 | 对标 |
|---|---|---|
| 25 | Canvas 无限白板 | Canvas |
| 26 | 幻灯片放映模式 | Slides |
| 27 | 自定义 CSS 主题 | Themes |
| 28 | 周期笔记（周/月/季/年） | Periodic notes |
| 29 | 查询块 ```query 内嵌搜索结果 | Dataview 风格 |
| 30 | 文件级同步（WebDAV/对象存储，可选） | Sync |
| 31 | 插件系统（Python hook + 前端插件） | Community plugins |

## 五、Agent 双向连接规格

### 外部 Agent → Kwey（MCP Server，sidecar 进程）

契约 v1 共 **29 个工具**（`app/agent/registry.py` 为唯一真源，MCP 与 REST 同源）：
检索读类 9 + 结构/体检读类 3 + 写类 16 + admin 1。

| tool | 功能 |
|---|---|
| kwey.search | 混合检索（FTS5+向量+图扩展），mode=fts/hybrid |
| kwey.read | 读笔记原文 + frontmatter + 链接（offset/max_chars 续读） |
| kwey.open | 让界面打开笔记/目录（正文不回流） |
| kwey.write / kwey.create / kwey.append | 写入三态（覆写 / 新建 / 追加），全部走统一写路径 |
| kwey.delete / kwey.move / kwey.rename / kwey.dir.create / kwey.dir.delete | 文件管理（进回收站，非抹除） |
| kwey.link / kwey.tags / kwey.graph.query | 双链 / 标签 / 邻域 |
| kwey.tree / kwey.trash.list / kwey.trash.restore / kwey.trash.purge | 树 / 回收站（purge 需 admin+confirm） |
| kwey.history.log / kwey.history.rollback | 快照时间线 / 回滚（confirm） |
| kwey.audit | 写入归因（谁改了什么） |
| kwey.organize / kwey.organize.apply | 自然语言整理（预演 / confirm 后落盘） |
| **kwey.views.list / kwey.views.query / kwey.views.save** | **结构化视图：列定义 / 按视图取行 / 存视图** |
| **kwey.diagnose** | **知识体检（六类问题 + 健康分，只读）** |
| **kwey.capture** | **快速捕获进收件箱** |
| **kwey.session.distill** | **跨 Agent 会话蒸馏：六类槽位 → 待审草案（幂等，不写普通笔记）** |

笔记同时作为 MCP resources 暴露。鉴权：本地 token，权限分 read-only / write / admin。

### Kwey → 外部 Agent
- 事件总线：watchfiles → WS/SSE 推送（agent 订阅同步自身记忆）
- Provider 适配器：配置 endpoint（OpenAI 兼容或自定义 /agent/run），打包上下文（当前笔记+选中+图检索相关笔记，token 预算控制）→ 返回结构化 ops → UI diff 确认 → 统一写路径落盘

## 六、里程碑

- **M1 可用内核**：P0 全部（编辑器、双链、搜索、文件管理）✅
- **M2 Agent Link**：MCP server、事件总线、Provider+diff 确认、快照回滚 ← 核心差异先落地 ✅
- **M3 体验补全**：图谱、向量检索、模板、每日笔记、分屏、导出 ✅
- **M4 特点功能**：F1/F2/F3/F4 + P2（Canvas/Slides/周期/主题/插件市场）✅
- **M5 分发与结构化**（2026-11）：Bases 式视图、语义检索进包、安装器/更新、体检/捕获/收件箱 ✅

## 六之二、结构化视图规格（Bases，M5）

**为什么不做 Dataview 式查询语言**：Dataview 用 JS 表达式换来表达力，也换来「笔记里的代码能读文件」的安全洞。
Kwey 只做属性白名单：≥90% 的日常需求是「按属性筛 + 排序 + 分组 + 求和」，这些不需要图灵完备。

- 定义存 `<vault>/views/<slug>.base`（YAML，`kwey: true` 清单字段），属于**用户资产**，随 git 同步
- 引擎：`app/views/query.py` 纯内存（索引快照 `notes/props/tags`），条件操作符白名单
  `= != > >= < <= contains not_contains in not_in exists empty before after`
- 快照缓存以 `(COUNT(*), MAX(mtime))` 为戳，TTL 1s；写路径后显式 `invalidate_cache()`
- 单元格写回 → `VaultService.write_note`（只改一个 frontmatter 字段，正文原样），绝不直接改文件
- AI 建视图：只用 `schema.py` 给出的真实字段名，产出定义先预览、人确认才落盘（不自动保存）
- 兼容 Obsidian Bases 子集（`filters.and` / `views[0].type|order|sort` / `file.hasTag()` / `file.inFolder()`）

## 六之三、语义检索规格（M5）

- 存储：`<vault>/.kwey/vector.db`（独立连接，避免给主索引加载扩展）
- 索引：`vec0` 虚拟表，`distance_metric=cosine`；模型名写 `vec_meta`，换模型即 DROP 重建
- Embedder 链：本地 ONNX（bge-small-zh-v1.5，CLS 池化 + L2 归一化）→ OpenAI 兼容 `/embeddings` → sentence-transformers → hashing
- 分词：纯 Python BERT WordPiece（CJK 逐字、标点切分、`##` 续词、贪心最长匹配），不引 tokenizers 依赖
- 查询侧加 bge 前缀「为这个句子生成表示以用于检索相关文章：」
- 零向量不入索引（否则 `distance` 为 NULL 会污染排序）；向量永远可重建，不是事实源
- 打包：`sqlite_vec` 的 `vec0.dll` 与 `onnxruntime` 动态库由 spec 收集，模型进 `_internal/models/`

## 七、特点功能（Kwey 差异化核心，M4 重点）

### F1 AI 自动构建图谱
- 场景：导入已有笔记库或日常新建笔记时，AI 自动分析内容之间的关系，生成双链、标签、MOC 索引页（Map of Content）
- 两阶段实现（省 token）：Chroma 向量召回候选笔记对 → LLM 确认关系类型（相关/从属/时间线）→ 结构化 ops → diff 确认落盘
- 增量模式：新笔记入库时只分析它与已有库的关系，不全库重扫
- 卖点：Obsidian 图谱靠手动建链后被动生长，Kwey 是 AI 主动构建

### F2 每日自动推送 Gitee / GitHub
- vault 级开关：勾选启用的仓库才推送；配置 remote（Gitee/GitHub）、branch、推送时间、commit message 模板、包含目录
- 实现：subprocess 调系统 git + threading.Timer（零新依赖）；应用未运行时下次启动补推（对比 last_push 时间戳）
- push 前自动 pull --rebase，冲突时暂停并提示手动处理
- **远端绑定在应用内完成**：设置页填仓库地址 → 后端执行 `git remote add/set-url`，地址写进 `.git/config`（随仓库迁移），Kwey 自己的 json 只存 remote 名
  - 凭据不进 Kwey：认证交给系统 git credential helper（Windows 凭据管理器 / keychain / libsecret）
  - 地址若内嵌 userinfo（`https://user:token@…`）不阻断但返回警告，建议改用 SSH
  - branch 留空 = 跟随仓库当前分支（避免 main/master 硬编码错位）
- 定位：免费版"Obsidian Sync"，Gitee 国内快

### F3 AI 智能个人信息库（个性化根基）
- 记忆三分区，存于 vault 内 `.kwey/memory/`，**均为普通 md 文件——用户可直接查看/编辑/删除**（符合"文件为源"，比云端黑盒记忆透明）：
  - `projects.md` —— 最近项目记忆（权重 50%，随时间衰减快）
  - `profile.md` —— 用户信息/偏好/性格（30%，稳定，允许人工维护）
  - `lessons.md` —— 任务处理经验（20%，按复用次数升权）
- 权重机制 = **检索注入配额**：组装 agent 上下文时按 50/30/20 配比取各分区条目，而非把文件字面切成 5:3:2
- 记忆条目格式：`{id, category, content, weight, last_used, source}`
- 闭环更新：用户确认 diff 落盘的时刻触发——LLM 提取本次交互的新记忆（新项目信息/新偏好/新经验），去重合并进对应分区，衰减过期条目
- 服务对象：内嵌整理任务、RAG 问答、外部 agent（经 MCP 以 resources / memory tools 暴露）

### F4 补充构思
- **RAG 问答**：内置"问我库"入口，Chroma + 记忆层，答案带笔记引用（与你 agent 项目技术栈同款，成本低）
- **孤儿笔记/断链体检**：定期扫描孤立笔记（无入链无出链）与失效链接，agent 建议修复或并入 MOC（与 F1 配套）
- **知识库仪表盘**：活跃度、断链数、标签集中度、最久未复习的知识
- **学习复习流水线**：笔记标"待复习"，简化间隔重复，agent 生成每日复习清单（贴合学生场景）
- **会话式批量整理**：对话下达"按项目归类这个文件夹"→ agent 产出批量 ops（移动+改名+建链）→ 一次 diff 确认全部执行
- **网页剪藏**：粘贴 URL 自动抓取正文转 Markdown 入库 + 打标 + 建链

### 落地顺序建议
F2（最简单、独立）→ F3 基础版（三分区 + 闭环更新）→ F1（依赖 F3 的内容理解能力）→ F4 按需穿插
