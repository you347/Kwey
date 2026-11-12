# Kwey · 个人知识库（M1）

本地优先、文件为源、Agent 原生的个人知识库桌面软件（对标 Obsidian 基础体验）。
**磁盘 Markdown 文件夹 = 唯一事实源**；SQLite（FTS5/链接图）仅做索引、可整库重建。

> 当前里程碑：**M1 —— P0 全部 13 项功能**。M2（Agent Link / MCP）、M3（图谱/向量检索）仅预留架构位置。

---

## 功能清单（P0，全部实现）

| # | 功能 | 说明 |
|---|------|------|
| 1 | Vault 打开/扫描/增量索引 | 任意文件夹作 vault；watchfiles 防抖监听外部改动，仅增量更新受影响索引 |
| 2 | Markdown 编辑器：源码 + 实时预览 | CodeMirror（vendored，无 npm 构建链）；源码/分屏/预览三模式；防抖自动保存 |
| 3 | `[[双链]]`（含别名）+ `![[嵌入]]` | 预览可点击跳转；断链红色虚线、点击提示创建；嵌入实时回填 |
| 4 | 反向链接 + 未链接提及 | 右面板列出全部反链与提及；一键转双链 |
| 5 | 嵌套标签 `#a/b` + 标签面板 | frontmatter tags 与行内标签合并统计，层级树点击过滤 |
| 6 | 全文搜索 + 操作符 | FTS5 BM25；`path:` `tag:` `file:` 操作符；结果 `<mark>` 高亮（中文按字切分） |
| 7 | 文件树：新建/改名/移动/删除 | 改名/移动自动全库重写链接（0 断链，含 `![[嵌入]]`）；删除进 `.trash/` |
| 8 | 快速切换器 | Ctrl+O，模糊匹配、键盘导航 |
| 9 | 命令面板 | Ctrl+P，命令注册表 + 模糊匹配 + 快捷键提示 |
| 10 | frontmatter 属性 | 属性区展示/编辑，写回保留自定义字段与正文 |
| 11 | 多标签页 | 开/关/切，localStorage 持久化，重启恢复 |
| 12 | 附件 | 编辑器内粘贴/拖入图片 → `attachments/`（可配置）→ 插入相对链接 |
| 13 | 暗色/亮色主题 | 全应用 CSS 变量即时切换，持久化到 settings.json |

快捷键：`Ctrl+O` 切换器 · `Ctrl+P` 命令面板 · `Ctrl+Shift+F` 搜索 · `Ctrl+S` 保存 · `Ctrl+W` 关标签 · `Ctrl+N` 新建笔记。

## 启动方式

```bash
# 1. 安装依赖（Windows / Python 3.13）
python -m pip install -r requirements.txt

# 2a. 纯浏览器模式（推荐先用这个验证）：uvicorn 启动 + 自动打开浏览器
python main.py --server

# 2b. 桌面模式：pywebview（Windows WebView2）窗口
python main.py

# 常用参数
python main.py --server --vault D:/my/notes --port 17630 --no-browser
```

默认端口 `17630`。首启未指定 `--vault` 时，在欢迎页/设置页输入 vault 绝对路径打开即可。

## 目录说明

```
Kwey/
├── main.py                 # 唯一入口：--server / pywebview 双模式
├── requirements.txt        # 依赖
├── app/
│   ├── config.py           # .kwey/settings.json 读写
│   ├── paths.py            # 路径工具（Windows 中文路径/非法字符/安全校验集中处理）
│   ├── models.py           # Pydantic 请求/响应模型
│   ├── events.py           # EventBus 内存 pub/sub（事件：created/updated/deleted/moved/links_changed/index_ready）
│   ├── server.py           # FastAPI 工厂 + 统一 {code,data,message} 包装
│   ├── parser/             # frontmatter / wikilink / 行内标签 / markdown-it 渲染
│   ├── index/              # SQLite FTS5 索引（.kwey/index.db）+ 内存链接图
│   ├── vault/              # VaultService 统一写路径 + watcher + 链接重写
│   ├── api/                # REST 路由（system/notes/files/search/graph）+ /ws/events
│   └── agent/              # M2 (T11) 占位，本期不实现
├── static/
│   ├── index.html          # 三栏布局 + 浮层 + 设置页
│   ├── css/                # themes/layout/base/panels/editor
│   ├── js/                 # 16 个 ES Module（无构建链）
│   └── vendor/codemirror/  # vendored CodeMirror（cdnjs 下载；缺失时自动降级增强 textarea）
├── tests/                  # pytest：解析器/索引/链接图/写路径/重写/watcher/API/性能基准
└── docs/                   # ARCHITECTURE.md / PRD.md / 时序与类图
```

## 架构硬约束（实现已遵守）

1. **磁盘 Markdown = 唯一事实源**：删掉 `.kwey/` 后重启，索引自动整库重建，内容零丢失（G4）。
2. **统一写路径**：一切写操作经 `VaultService`：`RLock → 原子写盘（tmp+os.replace）→ 自写时间戳记录 → 防抖增量索引 → EventBus 广播`；watcher 命中自写记录则跳过（防回环）。
3. **路径约定**：API 全程 vault 相对正斜杠路径；`paths.py` 集中校验（拒绝 `..`/盘符/保留名/非法字符）。
4. **API 响应**：统一 `{code, data, message}`；错误码 400/404/409/500。
5. **性能**：1000 笔记全文搜索（含操作符）稳态 ≤200ms（`tests/test_api_search.py` 基准，`@pytest.mark.perf`）。

## 测试

```bash
python -m pytest tests/ -v            # 全量
python -m pytest tests/ -m perf -v    # 仅性能基准
```

## 已知限制（M1）

- 编辑器为 CodeMirror 5（vendored 单文件方案，架构允许；CodeMirror 6 无 cdnjs 单文件分发）；`static/vendor/codemirror/` 缺失时自动降级为带行号的增强 textarea，功能不缩水。
- FTS5 中文采用 unicode61 + 按字切分短语策略，无分词级语义（M3 向量混合检索补足）。
- 多 vault 并存、回收站恢复（P1-24）、分屏对照编辑等见 PRD P1/P2，本版未实现。
- `.trash/` 为普通目录移入，无自动清理；嵌入图片按文件名全局解析（含 attachments/）。
- pywebview 模式依赖系统 WebView2 运行时；不可用时自动降级浏览器模式。
