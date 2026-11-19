# Kwey 聊天 P0 改造 · 任务卡索引

三张并行卡 + 一张集成卡。每张卡都是自包含提示词，可直接整份投喂给一个子智能体。

## 总览

| 卡 | 目标 | 独占文件 | 新增测试 |
|---|---|---|---|
| A | 聊天回复 Markdown 渲染 + 代码块复制 | `app/parser/renderer.py`、`app/api/routes_chat.py`、`static/js/chat.js`、`static/css/panels.css`、`tests/test_chat_render.py` | `test_chat_render.py` |
| B | 外部/Agent 写入冲突提示（不再静默丢弃本地内容） | `static/js/app.js`、`static/index.html`、`static/css/editor.css` | 脚本化验收（无 pytest） |
| C | 上下文预算：`kwey.read` 截断续读 + 历史编译 | `app/agent/registry.py`、`app/agent/kernel.py`、`tests/test_agent_context_budget.py`、`docs/AGENT-CONTRACT.md` | `test_agent_context_budget.py` |
| 集成 | 合并、全量回归、重启应用 | 无（只允许修集成问题） | 全量 |

**文件所有权是硬约束**：每个子智能体只改自己那一列的文件；发现必须改别人的文件时，停下来写进报告的「未决」而不是动手。

## 公共约束（每张卡都适用）

- 项目根：`C:/Users/Lenovo/Desktop/Kwey`，Python 3.11+，Windows，Git Bash。
- 不加任何第三方依赖（前端不引 CDN，后端不改 `requirements.txt`）。
- Python 文件全部 UTF-8；前端是原生 ES Module，无构建步骤。
- 路径一律 vault 相对路径、正斜杠；API 返回统一 `{"code":0,"data":…,"message":"ok"}`。
- 写代码时字符串定界符用 ASCII 直引号；注释用中文，风格与邻近代码一致。
- 只做本卡要求的事：不顺手重构、不格式美化、不删既有死代码（发现了写进报告）。
- 不运行全量 `pytest`（集成卡负责）；只跑本卡新增/相关测试。
- 不重启桌面应用、不 `git stash` / `git checkout` / `git reset`。
- 完成后按固定格式报告：改动文件与行为 → 测试命令与原始输出 → 偏差 → 未决/风险。

## 并行与合并

三张卡文件互不重叠，可同时开工。合并顺序：

```
A（渲染） → C（上下文） → B（冲突） → 集成
```

B 放最后是因为它改 `app.js` 的 WS/保存分支，最容易和其他改动产生语义耦合；文件虽不冲突，集成时仍以 B 后的 `app.js` 为准。

## 集成门禁（集成卡执行）

1. `node --input-type=module --check` 通过所有被改 JS。
2. 全量 pytest 通过（用项目内临时目录规避清理钩子）：

   ```bash
   cd "C:/Users/Lenovo/Desktop/Kwey" && mkdir -p .pytest_tmp_root && \
   PYTEST_DEBUG_TEMPROOT="C:/Users/Lenovo/Desktop/Kwey/.pytest_tmp_root" \
   python -m pytest -q
   ```

3. 浏览器验收：Markdown 表格/代码、冲突条两条路径、长历史不中断。
4. 重启桌面应用并记录到 `.workbuddy/memory/`。

## 派发方式

- 一个子智能体只拿一张卡（A / B / C 各一个会话）。
- 集成卡等三张都回报完成后再开一个会话。
- 把对应 `.md` 文件整份贴进会话首条消息即可，卡里已包含背景、边界与验收命令。
