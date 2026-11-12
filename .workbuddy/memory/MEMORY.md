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

## 待定 / 进行中
- 基础功能要求"尽可能多、仿 Obsidian"，完整清单见工作区 DESIGN.md（P0/P1/P2 共 31 项）。
