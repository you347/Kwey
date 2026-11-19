# 内置智能体冒烟与验收报告（卡B · P1-23）

日期：2026-11-16 · 设计真源 [AGENT-KERNEL.md](AGENT-KERNEL.md) · 契约基线 [AGENT-CONTRACT.md](AGENT-CONTRACT.md) v1。
本报告覆盖卡B 三个 Wave 的全部运行证据；所有冒烟/E2E 打在克隆 vault + 独立端口，用户 live 17630 全程只读未触碰。

## 1. 环境

| 项 | 值 |
|----|----|
| 启动 | `py main.py --server --vault <克隆库> --port <独立端口> --no-browser` |
| Python | `py`（Windows Store `python` 为 stub，不可用） |
| 浏览器 | Playwright chromium channel=msedge，reduced_motion="reduce"，viewport 1440×900 |
| 桩 Provider | 标准库 ThreadingHTTPServer，OpenAI Chat Completions 格式脚本化响应 |
| 控制台 | GBK 码页致中文输出 garbled 仅为显示问题，文件与断言均 UTF-8 |

## 2. W1 内核冒烟（18/18）

脚本 `.workbuddy/b_w1_smoke.py`（自包含：桩 17699 + 克隆 b-w1-smoke-vault + 后端 17644，真实 urllib 链路）。

| 组 | 检查 | 结果 |
|----|------|------|
| 就绪 | 17644 health 200 | 1/1 |
| A 创建流 | 事件序 tool_call→tool_result→message→done；名字映射回 kwey.create；ok=true；笔记落盘；journal actor=kwey-built-in/create；done 携带 messages | 6/6 |
| B 确认暂停 | 事件序 confirm_required→done；name=kwey.dir.delete；done.pending.id 非空；目录未动 | 4/4 |
| C resume | 事件序 tool_call→tool_result→message→done；ok=true；目录已删；journal rmdir | 4/4 |
| D 未配置 | error→done；文案含「未配置」；全事件流不含 api_key | 3/3 |

## 3. W2 浏览器 E2E（25/25）

脚本 `.workbuddy/chat_e2e.py`（自包含：桩 17698 + 克隆 chat-e2e-vault + 后端 17645）。
截图：`.workbuddy/chat-create.png`、`chat-confirm.png`、`chat-resume.png`。

| 组 | 检查 | 结果 |
|----|------|------|
| T1 入口 | ribbon 打开面板；已配置无横幅；发送可用；右栏 tab 激活 | 4/4 |
| T2 创建流 | 用户气泡文案；步骤名 kwey.create；助手「完成」；笔记落盘；journal actor；文件树刷新 | 6/6 |
| T3 拒绝 | 对话框标题/含工具名；步骤等待态；目录仍在；拒绝提示；模型回应取消 | 6/6 |
| T4 允许 | resume 后目录已删；journal rmdir；最终回答「完成」 | 3/3 |
| T5 清空 | 回空态提示且无消息节点 | 1/1 |
| T6 未配置 | 横幅文案；发送禁用；去设置跳转；恢复后横幅消失 | 4/4 |
| 全局 | 零 pageerror | 1/1 |

## 4. W3 验证

### 4.1 回归与门禁

- `py -m pytest -q` → **585 passed**（564 卡A 基线 + 21 `tests/test_agent_kernel.py`），零回归。
- mermaid 门禁（mermaid@8.8.3，p8 skill validate_mermaid.cjs）对 AGENT-KERNEL.md：
  `总计: 1 块 | parse-OK 1 | PARSE-ERROR 0 | 渲染环境告警 1`（getBBox 告警按门禁可忽略）。
- JS 语法：`node --input-type=module --check` 对 chat.js / app.js / shell.js 全过。

### 4.2 五维打磨记录

| 维度 | 结论 |
|------|------|
| P1 正确性 | 空 content 的 message 事件不再渲染空气泡（chat.js 修复）；resume.id 失配静默回闸门复暂停，无误执行 |
| P2 可读性 | 内核/路由/面板注释与文档章节对齐；无重复抽象 |
| P3 健壮性 | 流漏网异常兜底成帧；abort/拒绝/未配置三态均有 UI 出口；断连限制已披露 |
| P4 性能 | 每请求重建 21 工具表与 O(n) pending 扫描均为常量级开销，无热点 |
| P5 安全 | api_key 不回显（单测+冒烟双证）；前端全输出 escapeHtml + CSS.escape；/chat 无 token 信任边界与 resume 信任级已写入文档 §1.2/§4 |

设置页补充说明文案：内置智能体聊天需 openai 协议 Provider（index.html Agent Link 节）。

## 5. 行为变化与披露

- 新增右栏「助手」面板与 ribbon 图标；命令面板新增「打开内置智能体聊天」。**用户 live 前端需 F5**；
  live 后端未重启则无 /chat 路由，需「拉起」后才可用。
- `/api/agent/chat` 为本地 UI 端点不设 token（与 organize/provider-config 同信任级）。
- 内核 `dir.delete` 一律确认门（比 REST 非空 409 更严）；LLM 自填 confirm 一律忽略。
- 拒绝危险操作会回喂 tool 消息让模型作答，不写盘不留 journal。
- Provider 设置 UI 为既有 agentlink.js 能力，本卡仅补协议说明文案，未新增后端。

## 6. 已知限制

- 停止按钮未实测（桩响应瞬时）；中断后历史保留已发送 messages。
- 助手气泡纯文本 pre-wrap，不渲染 markdown。
- 真实 LLM endpoint 未验证；openai 兼容面以 Chat Completions + tools 为准。
- 客户端断连后当前工具步骤仍执行完（同步生成器 yield 间隙感知断连）。

## 7. 复现

```
py -m pytest tests/test_agent_kernel.py -q
py -m pytest -q
py .workbuddy/b_w1_smoke.py
py .workbuddy/chat_e2e.py
NODE_PATH=.workbuddy/mermaid-check/node_modules node <p8-skill>/scripts/validate_mermaid.cjs docs/AGENT-KERNEL.md
```