# 集成卡：合并 A / B / C 并做全量验收

> 你是集成子智能体。只有在 A、B、C 三张卡都回报「完成」后才开工。
> 你的职责是合并、回归、修集成问题；**不要重写三张卡的实现**。

## 开工前检查

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
git status --porcelain | grep -v "__pycache__"
```

期望只出现这些路径（多出来的先问清楚再动）：

```
app/parser/renderer.py
app/api/routes_chat.py
static/js/chat.js
static/css/panels.css
tests/test_chat_render.py
static/index.html
static/js/app.js
static/css/editor.css
app/agent/registry.py
app/agent/kernel.py
docs/AGENT-CONTRACT.md
tests/test_agent_context_budget.py
docs/tasks/chat-p0/**
```

`static/index.html` 与 `static/js/app.js` 在本次之前已有大量未提交改动，这是正常的；只复核本卡范围内的新增段落。

## 合并顺序

A → C → B。三张卡文件不重叠，理论上无冲突；若出现冲突，**保留三张卡各自的语义**，不要为了消除 diff 而删功能。

## 门禁 1：语法

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
for f in static/js/chat.js static/js/app.js static/js/filetree.js; do
  node --input-type=module --check < "$f" || echo "FAIL $f"
done
python -c "import ast,pathlib;[ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('app').rglob('*.py')]" && echo "ast ok"
```

## 门禁 2：全量测试

用项目内临时根目录，规避 `%TEMP%` 清理钩子导致的假失败：

```bash
cd "C:/Users/Lenovo/Desktop/Kwey"
mkdir -p .pytest_tmp_root
PYTEST_DEBUG_TEMPROOT="C:/Users/Lenovo/Desktop/Kwey/.pytest_tmp_root" \
  python -m pytest -q > .pytest_tmp_root/run.log 2>&1
echo "EXIT=$?"
tail -3 .pytest_tmp_root/run.log
rm -rf .pytest_tmp_root
```

期望：`EXIT=0`，尾行 `N passed, 5 skipped`（N ≥ 863 + 新增用例数）。

## 门禁 3：浏览器端到端

起服务：

```bash
python main.py --server --no-browser --vault demo-vault --port 17631
```

用 Playwright（`channel="msedge"`，`emulate_media(reduced_motion="reduce")`）跑完：

**A · Markdown**
1. 打开「助手」，发一句让模型输出表格 + 代码块（例如「用表格对比 A/B，再给一段 Python 代码」）。
2. 断言助手气泡内有 `.chat-markdown table` 与 `pre code`，且气泡 `white-space` 为 `normal`。
3. 悬停 `pre`，断言 `.chat-code-copy` 可见；点击后 `navigator.clipboard.readText()` 等于代码文本（无权限时退化为「按钮存在 + 无报错」）。
4. 切浅色主题，表格边框与代码底色仍可见（截图留档）。

**B · 冲突**
1. 打开 `欢迎.md`，输入未保存文本。
2. 用 `/api/agent/bootstrap` 取 token，`PUT /api/note` 直接改该文件。
3. 断言 `#conflict-bar` 可见、编辑器仍是本地文本。
4. 「重新载入」→ 编辑器等于磁盘内容、冲突条隐藏。
5. 重复脏状态 + 外部写入 →「保留我的并另存」→ 文件树出现 `欢迎 (冲突副本).md`，内容含本地文本。
6. 干净状态外部写入 → 不出现冲突条、编辑器自动更新。

**C · 上下文预算**
1. 断言 `/api/agent/render`、`/api/agent/chat` 仍正常（C 不应影响）。
2. 长历史压缩主要靠 pytest 覆盖；UI 侧只需确认未知 SSE 事件（`context_trim`）不会让聊天卡住（`default` 分支忽略）。

## 门禁 4：人工回归（必须做）

- 聊天：发送、停止、撤回、点赞/点踩、上下文条切换、恢复会话后上下文。
- 编辑：普通保存、切标签、外部修改干净重载、分屏副屏编辑。
- 设置页四组（常规 / 模型接入 / 检索 / 扩展）无 JS 报错。
- 控制台 0 个 `pageerror`，404 除外。

## 修复原则

- 只修「集成导致的问题」：接口不匹配、事件顺序、样式互相覆盖、测试互相污染。
- 需要改某张卡的语义才能通过时，先停下写清冲突点，不要自行重写。
- 每修一处，重跑对应门禁，不要攒到最后。

## 收尾

1. 重启桌面应用：

   ```bash
   # 先停 17630 监听进程，再
   python main.py --vault "C:/Users/Lenovo/Desktop/Kwey/demo-vault"
   ```

2. 确认窗口标题 `Kwey` 存在、`/api/health` 正常。
3. 追加 `.workbuddy/memory/2026-11-17.md`：三卡改动、全量通过数、遗留问题。
4. 报告：

```
## 合并结果
- A / B / C 各自是否原样合并（有改动写清）

## 门禁
- node / ast：
- 全量 pytest：passed / skipped，EXIT
- A 断言：
- B 断言：
- C 断言：
- 控制台错误：

## 集成修复
- 每条：现象 → 根因 → 改了什么 → 复验

## 遗留
- 未做/降级/后续卡
```
