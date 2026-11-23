# Kwey 产品规划（2026-11）

> 依据：2026-11 竞品与生态调研 + 本仓代码盘点。
> 结论先行：**Kwey 的功能数量已经够，缺的是"能被装、能被迁、能被看懂"的三件事，外加一个能压过 Obsidian 的杀手视图。**

## 1. 定位（Geoffrey Moore）

**针对** 已经在用 AI Agent（Claude Code / Cursor / 自建 agent）、且笔记内容不适合上云的知识工作者与学生

**谁需要** 一个 Agent 能直接读写、自己又能长期持有的知识底座——而不是把 800 篇笔记锁进别人的数据库

**Kwey**

**是一种** 本地优先的 Agent 原生知识库

**能够** 在 10 分钟内把已有的 Markdown 库变成可检索、可整理、可被 Agent 引用的知识系统，文件始终是本地 Markdown

**不同于** Obsidian（AI 靠插件拼装、需自备 key、插件供应链有风险）与 Notion AI（数据在云端、无文件所有权）

**Kwey**

**提供** 开箱即用的本地语义检索（离线 ONNX，零配置）与 Agent 工具协议（MCP 28 工具），零插件、零账号、零上传

### 压力测试

| 检验项 | 结论 |
|---|---|
| 目标客户能认出自己吗 | 能。"用 agent 但担心笔记上云"是 2026 年真实人群 |
| 需求有研究支撑吗 | 有。Obsidian 社区反复抱怨"最好的 AI 插件把笔记发给 OpenAI"（litmustools, 2026） |
| 类别正确吗 | "本地优先知识库"是已知货架，不需要造词 |
| 差异化可演示吗 | 可。同一篇笔记，Kwey 直接 `/ask` 出带引用答案；Obsidian 要装 3 个插件 + 填 key |
| 能指导取舍吗 | 能。凡"需要用户装插件才能用"的方向一律不做 |

## 2. 竞争格局（2026-11 调研）

| 维度 | Kwey | Obsidian | Notion AI | Mem | Logseq |
|---|---|---|---|---|---|
| 定位 | 本地优先 + Agent 原生 | 本地优先 + 插件生态 | 云端团队工作区 | AI 自动整理 | 本地 outliner |
| 数据所有权 | 本地 Markdown | 本地 Markdown | 云端 | 云端 | 本地 Markdown |
| 内置 AI | 有（语义检索/RAG/整理） | **无**（插件 + 自备 key） | 有（$20/人/月） | 有 | 无（插件） |
| Agent 接口 | **MCP + REST + CLI + 审计** | 无 | API | 无 | 无 |
| 数据库视图 | **有（Bases 式 + AI 建视图）** | Bases（2025，4.4/5） | 强（关系/rollup） | 无 | 弱 |
| 插件生态 | 无（有意不做） | 10,683 个（2026-11-21） | 集成市场 | 无 | 数百 |
| 迁移导入 | **有（Obsidian/Logseq/MD）** | 无（本身是目的地） | 有 | 有 | 有 |
| 安装/分发 | **Inno Setup 安装包 + zip** | 全平台安装包 | SaaS | SaaS | 安装包 |

**关键事实（带来源）**
- Obsidian 插件数 10,683（官方论坛，2026-11-21），社区出现"插件疲劳"，有人退回 restricted mode
- 2026-04 出现利用恶意 Obsidian 插件投递 PHANTOMPULSE RAT 的攻击（rywalker, 2026-06）
- Obsidian 无内置 AI；社区评价"最佳 AI 插件把笔记发给 OpenAI，侵蚀了隐私这个唯一卖点"（litmustools, 2026）
- Bases 社区共识："Dataview 的 1/10 功能覆盖 90% 场景"，20,000 篇笔记仍流畅，支持表格内直接改属性写回（trustmebro 汇总 1,085 条 Reddit 讨论）
- 本地优先阵营（Obsidian / Logseq / AFFiNE / Anytype）**全部缺原生 AI**；AI 原生阵营（Mem / Reflect / Tana / Capacities）**全部是云**——这是空档

## 3. 竞争动态

- **直接对手**：Obsidian（同一买家、同一数据观）
- **间接对手**：Notion AI（要结构化 + 团队）、Mem（要零整理）
- **替代行为**：Obsidian + Copilot/Smart Connections 插件 + 自备 API key（当前最接近的替代方案）

**我们赢在哪**：开箱即用的 AI + 本地文件 + Agent 协议三件套同时成立。对手要么缺 AI，要么缺本地，要么缺 Agent 接口。
**我们输在哪**：生态（插件 0 vs 10,683）、移动端、协作、品牌信任。
**未被满足的空白**：把"已有笔记库"低成本迁进来，并让 Agent 立刻能用——**没有任何一家把这条路径做顺**。

## 4. 现状盘点（代码核实，2026-11-23 收尾）

| 能力 | 状态 |
|---|---|
| 编辑/双链/图谱/Canvas/Slides/模板/书签/回收站/PDF | ✅ 已实现 |
| Agent Link（MCP 28 工具 + REST + diff 确认 + 快照 + 审计） | ✅ 已实现 |
| F1 知识编译 / F3 个人信息库 / F2 自动推送 | ✅ 已实现 |
| F4 网页剪藏 / RAG 问答（带引用校验）/ 间隔复习 | ✅ 已实现 |
| 语义检索（**sqlite-vec + onnxruntime + bge-small-zh 本地模型**） | ✅ 已进包：模型随包分发，离线可用；embedder 可切 API |
| 安装器 / 告知式自动更新 | ✅ Inno Setup 单文件安装包 + Gitee Release 检查 |
| Obsidian / Logseq / Markdown 库导入 | ✅ 已实现（21 测试） |
| Bases 式结构化视图（表格/卡片/列表 + 单元格写回 + AI 建视图） | ✅ 已实现（24 测试） |
| 知识体检（孤立/断链/近重复/空/陈旧/无标签 + 0~100 分） | ✅ 已实现（18 测试） |
| 快速捕获浮窗 / AI 收件箱 | ✅ 已实现（MCP 也可 `kwey.capture`） |
| 移动端原生 | ❌ 不做（见 §7） |

## 5. 路线图（Now / Next / Later）

| 阶段 | 举措 | 结果（可衡量） | 指标 |
|---|---|---|---|
| **Now**（2 周） | ① Obsidian 导入（预览 + 附件重写 + 冲突策略）② 安装器（Inno Setup + 首次启动自检） | 别人的库能 10 分钟迁进来；别人能双击装上 | 导入 500 篇成功率 ≥99%；安装到首次可写 ≤3 分钟 |
| **Next**（4 周） | ③ 语义检索进包（sqlite-vec + fastembed ONNX，替代 chromadb 重依赖）④ Bases 式视图（frontmatter → 表格/卡片，可编辑写回，支持求和） | 发布版 hybrid 名副其实；结构化整理不再需要 Dataview | 语义召回率 ≥ FTS 的 1.3 倍；20k 笔记视图首屏 <300ms |
| **Later**（6 周+） | ⑤ 知识体检 ⑥ 全局快速捕获 ⑦ AI 收件箱整理 ⑧ 自动更新通道 | 从"工具"变成"系统"：会提醒、会归档、会自更新 | 周活留存 ≥40%；体检修复采纳率 ≥25% |

## 6. 优先级（RICE，Pre-PMF 用价值/努力校准）

| # | 举措 | 触达 | 影响 | 信心 | 努力 | RICE | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | Obsidian 导入 | 高 | 3 | 90% | 1.5 人月 | 180 | 获客入口，无它则一切白做 |
| 2 | 安装器 + 自检 | 高 | 3 | 95% | 0.5 人月 | 570 | 没有它谈不上分发 |
| 3 | 语义检索进包 | 高 | 3 | 85% | 1 人月 | 255 | 招牌功能在发布版失效 |
| 4 | Bases 式视图 | 中 | 2 | 80% | 1.5 人月 | 107 | 对标 Obsidian 爆款，可做得更好 |
| 5 | 知识体检 | 中 | 2 | 70% | 1 人月 | 93 | 差异化，Agent 的天然场景 |
| 6 | 快速捕获浮窗 | 中 | 2 | 75% | 0.5 人月 | 300 | 收集环节，成本极低 |
| 7 | AI 收件箱整理 | 中 | 2 | 60% | 1.5 人月 | 53 | 需前 6 项数据积累 |
| 8 | 自动更新 | 中 | 1 | 80% | 0.5 人月 | 213 | 与安装器同一批 |

依赖：③ 依赖 ①（先有真实大库才能验证召回）；⑦ 依赖 ⑤（先能识别问题才能整理）；⑧ 依赖 ②。

## 7. 明确不做（及原因）

| 不做 | 原因 |
|---|---|
| 插件市场 | 对手 10,683 个插件且已出现供应链攻击；我们的差异恰是"免插件"。开放插件等于放弃定位 |
| 实时协作 | 本地优先与 CRDT 协作冲突；目标用户是单人 + Agent，不是团队 |
| 移动端原生 App | 成本 3 人月以上，验证不足。先用 PWA 只读 + 快速捕获探路 |
| 更多主题/皮肤 | 不驱动留存，纯消耗 |
| 云端同步订阅（短期） | 用户已有 git 推送 + 自建 agent，收费口不成立。先把"本地 + Agent"做透 |

## 8. 风险

| 风险 | 缓解 |
|---|---|
| sqlite-vec 仍 pre-v1，API 可能变 | 锁版本；VectorBackend 抽象已存在，可回退 Chroma/Null |
| 打包 ONNX 模型使安装包变大 | 已量化到 24MB（+24MB），随包分发换零配置；缺失时可重下 |
| 导入大库时内存/耗时 | 流式处理 + 分批写 + 进度事件（WS） |
| Obsidian Bases 快速迭代，功能被追平 | 差异化押在"AI 生成视图 + 自然语言建表"，而非字段编辑器（已落地） |

## 9. 后续动作

**本轮全部落地（2026-11-23）**，路线图 Now / Next / Later 八项清零：

1. ✅ **Obsidian / Markdown 库导入** —— `app/vault/importer.py` + `/api/import/scan|apply` + 设置页「导入已有笔记库」
   - 扫描只读预览（篇数/体积/冲突/断链/重名），确认后落盘
   - 保留原目录结构（附件与双链零重写）、三种冲突策略、批量快照抑制
   - 21 个测试覆盖；真实目录 18 篇 + 10 附件扫描通过
2. ✅ **Bases 式结构化视图** —— `app/views/{query,schema,store}.py` + `static/js/bases.js`
   - 表格 / 卡片 / 列表三模式；表头排序、分组小计、页脚聚合
   - 单元格就地编辑写回 frontmatter（走 `VaultService.write_note`，正文一字不动）
   - 定义存 `views/*.base`（YAML，随 git 同步），兼容 Obsidian Bases 子集
   - **AI 建视图**：一句话 → 只用真实字段 → 先预览再落盘
   - 条件白名单（`= != > >= < <= contains in exists empty before after`），不接受可执行代码
3. ✅ **语义检索进包** —— `app/index/{sqlite_vec,onnx_embedder,models}.py`
   - `sqlite-vec` `vec0` 虚拟表（cosine）+ 纯 Python WordPiece 分词 + `onnxruntime` 跑 bge-small-zh-v1.5
   - 模型 24MB 随包分发；缺失时设置页一键下载（hf-mirror → huggingface.co 双源）
   - 后端链 `sqlite-vec → chroma → null`，embedder 链 `local → API → sentence-transformers → hashing`
   - 实测：load 0.35s / 3 条向量 0.21s；`sim(向量检索, sqlite-vec 存)=0.62` vs `sim(向量检索, 吃什么)=0.21`
4. ✅ **安装器 + 告知式更新** —— `installer/kwey.iss` + `app/update.py` + `static/js/update.js`
   - `PrivilegesRequired=lowest`，装到 `%LOCALAPPDATA%\Programs\Kwey`，卸载保留用户数据
   - 启动后静默查 Gitee Release（回落 tags → GitHub），6 小时缓存，只提示不替换
5. ✅ **知识体检 + 快速捕获 + AI 收件箱** —— `app/diagnose.py` + `routes_health.py` + `routes_inbox.py`
   - 六类问题加权扣分 → 0~100 分；三种一键修复（空笔记进回收站 / 断链降级 / 批量补标签）
   - 捕获只问内容，整理延后：AI 给标题/目录/标签建议，人勾选后批量应用
6. ✅ **MCP 工具矩阵 23 → 28**：新增 `kwey.views.list` / `kwey.views.query` / `kwey.views.save` / `kwey.diagnose` / `kwey.capture`

**下一轮候选**（未排期）：PWA 只读 + 移动端捕获；流式导入进度（WS）；视图关系列（rollup/relation，对齐 Notion）。

---

*调研来源：pilotstack.in / litmustools.com / rywalker.com（Obsidian 2026 评测）；trustmebro.reviews（Bases 1,085 条讨论汇总）；recall.it / aitoolalliance.com（第二大脑横向对比）；aifrontierpost.com（sqlite-vec 实测，2026-11-10）；官方论坛 10,683 插件统计（2026-11-21）*
