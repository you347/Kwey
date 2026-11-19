# AGENTS.md — Kwey Vault Agent 操作规范（A5）

面向所有以 **kwey CLI / MCP Server / Agent REST** 接入 Kwey vault 的自动化 Agent。
目标：对抗 schema 漂移——任何 Agent 在读写 vault 前，先读本文档对齐约定。

---

## 1. Vault 目录约定

- vault 根 = 一个普通文件夹；**磁盘 Markdown = 唯一事实源**，索引可整库重建。
- 路径一律 **vault 相对 + 正斜杠**，不含 vault 根：`notes/读书/2026计划.md`。
- 保留目录（索引与文件树忽略；**禁止经普通写路径访问**）：

| 目录 | 作用 | Agent 规则 |
|---|---|---|
| `.kwey/` | `index.db`（FTS5+链接图）、`settings.json`、`provider.json` | **禁止手工修改**。索引损坏 → 删 `index.db` 重开自动重建；设置经设置页/配置 API 修改 |
| `.trash/` | 回收站 | 恢复/清空走服务（REST /trash API），不手工挪文件 |
| `.git/` | git 快照（可选，默认关闭） | 只读；回滚走 `/api/history` API |
| `templates/` | 模板目录（不进索引） | 经模板 API 读取，不当作普通笔记 |

- 约定目录（软件不强制，Agent 写入时应遵守）：`notes/` 正文笔记、`attachments/` 附件、`daily/` 每日笔记（`YYYY-MM-DD.md`）。

## 2. [[双链]] 与标签命名规范

- 双链：`[[目标]]` 或 `[[目标|别名]]`；目标为笔记名（可省略 `.md`）或 vault 相对路径。嵌入：`![[目标]]`。
- 解析顺序：精确路径 → 同目录 → 全库唯一名。**vault 内存在重名笔记时，必须写含目录的完整路径**，否则解析到错误目标。
- 标签两种来源（合并统计）：frontmatter `tags: [读书/2026]`（YAML 列表）与行内 `#读书/2026`。
- 层级用 `/` 嵌套（如 `#项目/kwey/里程碑`）；建议小写短词、避免空格（用连字符）。

## 3. Schema 约定（frontmatter）

| 字段 | 约定 |
|---|---|
| `title` | 显示标题；缺省取文件名 |
| `tags` | YAML 列表；与行内标签合并 |
| `created` / `updated` | ISO 8601 本地时间带时区偏移；**写路径自动补/刷新，勿手改** |
| 自定义字段 | 保留不丢、不排序；写回只替换被编辑的字段 |

- 新建笔记正文为空时自动生成 `# 文件名` 骨架。
- 所有写（CLI / MCP / REST / UI）经 **VaultService 统一写路径**：原子写 → 自写记录 → 增量索引 → 事件广播。Agent 禁止绕过（脚本直接 `open()`/`write_text()` vault 内文件是违规操作）。

## 4. Proposal-first：先 preview 再 apply

任何**批量 / 破坏性**操作（重命名、移动、批量改写正文、删除、整理）必须：

1. **收集现状**：`tree` 摸目录骨架，`read` 取原文，`search` 摸相关笔记；
2. **生成提案**：diff 级描述（路径、改动前后摘要、预计影响的链接数），向用户展示；
3. **确认后 apply**：逐条执行（CLI write / MCP / REST），不擅自放大范围；
4. **验证回报**：apply 后 `read` 复核、`search` 确认可检索、反链未断，报告结果。

单篇小改动可直接写，但写前必须 `read` 原文（拿最新 mtime/正文），写后报告 `action` 与 `links_changed`。

## 5. kwey CLI（A5）

无需 UI、无需启动服务进程。`--vault` 必需（可放在子命令前或后）；`--json` 输出统一信封 `{code, data, message}`。

```bash
# 全文检索（支持 path:/tag:/file: 操作符）
python kwey_cli.py search --vault "D:/我的知识库" "排课 path:notes tag:读书" --json
# 读笔记（--json 含 frontmatter/mtime；人读模式直出正文）
python kwey_cli.py read --vault "D:/我的知识库" notes/读书/2026计划.md --json
# 写笔记（upsert：存在则覆盖正文保留 frontmatter，不存在则创建；缺 .md 自动补）
python kwey_cli.py write --vault "D:/我的知识库" notes/新笔记.md --content "# 标题" --json
echo "多行正文" | python kwey_cli.py write --vault "D:/我的知识库" notes/管道.md --stdin
# 文件树（--json 为嵌套结构：name/path/type/children）
python kwey_cli.py tree --vault "D:/我的知识库" --json
# 导出（md=原始全文；html=独立 HTML 图片内联；print=带分页 CSS；--out 必须在 vault 外）
python kwey_cli.py export --vault "D:/我的知识库" notes/读书/2026计划.md --format html --out D:/tmp/plan.html
```

- 退出码：`0` 成功；`1` 运行期错误（含 HTTPException 404/409/400）；`2` 用法错误。**错误信息一律在 stderr**，stdout 只承载结果数据（可安全管道）。
- search 的 `--json` data 与 `GET /api/search` 形状一致（hits/total/elapsed_ms/mode）；snippet 保留 `<mark>` 高亮。
- CLI 不实现 `serve`（服务进程请用 `python main.py --server`）；不启动 watcher。
- 其他入口：MCP stdio（`python kwey_mcp.py --vault ...`）、Agent REST（`/api/agent/*`，需 token）。约定以本文档为准。

## 6. 大 vault 分层索引用法

- 检索策略**先窄后宽**：`tree` 看骨架 → `path:<子目录>` 限定子树 → `tag:` / `file:` 收窄 → 裸关键词兜底。
- `search --limit` 默认 50、上限 200；大 vault 避免无操作符的全库关键词扫描。
- 目录建议 ≤3 层（如 `notes/领域/主题/`）：层级过深会让 `path:` 前缀过滤与 wikilink 解析变慢，重名风险也更高。
- 索引（FTS5 + 链接图）常驻 `.kwey/index.db`，随每次写增量更新；批量导入后无需手工重建。
