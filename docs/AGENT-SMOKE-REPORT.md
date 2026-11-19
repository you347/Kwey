# Kwey 能力契约冒烟报告（卡A W1–W3）

对象：`app/agent/registry.py` 契约 v1（21 工具）及其 MCP / REST 两条传输。
原则：全部冒烟打在克隆 vault + 独立端口；用户 live 17630 测试服全程只读、未重启。

## 1. 环境

| 项 | 值 |
|---|---|
| 平台 | Windows 10.0.26100 x64 |
| Python | `py`（Windows Store `python` 为占位 stub，禁用） |
| 服务启动 | `py main.py --server --vault <克隆库> --port <独立端口> --no-browser` |
| git | 系统 git 可用（快照用例依赖） |
| 控制台 | GBK 中文乱码为显示问题，文件与断言均 UTF-8，不影响结果 |

## 2. W1 冒烟（28/28 PASS）

脚本：`.workbuddy/w1_smoke.py`；克隆库 `.workbuddy/w1-smoke-vault`；REST 端口 17642；
stdio 子进程库 `.workbuddy/w1-stdio-vault`。

| 组 | 断言要点 |
|---|---|
| stdio（7） | exit 0；5 响应；initialize serverInfo=kwey；tools/list=9（W1 时点）；schema 源自 registry；kwey.read/create 往返；resources/list=2 |
| /contract（4） | 无 token 401；read 200；version=1；9 工具元数据六字段齐全 |
| /tool（8） | read 200；read→write 403；未知工具 404；缺必填 400；create 200 + 落盘；search 命中；delete 入回收站；create journal 归属 token name |
| 旧端点 parity（5） | create/read 200；link 返回 linked_to+link；search 带 elapsed_ms；delete 返回 trash_path+path |
| 用户功能（1） | pulse 200 且含 today_words |
| 其余（3） | 服务就绪探测、token 签发、journal 文件读取 |

## 3. W2 冒烟（50/50 PASS）

脚本：`.workbuddy/w2_smoke.py`；克隆库 `.workbuddy/w2-smoke-vault`（源 split-e2e-vault）；
REST 端口 17643；stdio 子进程库 `.workbuddy/w2-stdio-vault`。运行后服务已停止。

| 组 | 断言要点 |
|---|---|
| stdio（10） | exit 0；6 响应；tools/list=21；schema 源自 registry；read/create/tree 往返；resources/list=2；journal 出现 mcp-stdio/admin/create |
| /contract（7） | 200；version=1；21 工具；元数据齐全；tiers 7/13/1；admin=trash.purge irreversible；write 全 reversible+journal |
| 权限矩阵（5） | read 200；read→append 403；write→purge 403；admin purge 200（空站 purged=0）；未知工具 404 |
| 新工具往返（17） | tree 含笔记A；append 200 + 读回可见；dir.create 200 / 重复 409；目录内建笔记；dir.delete 非空 409 / confirm 200；trash.list 目录条目 is_dir；trash.restore 目录 200 + 磁盘复原；move 归档 / 移回根；rename 笔记C→笔记丙；history.log 200（快照关闭→[]）；rollback 闸门 400；/api/history/status available=true |
| organize + purge 闸门（3） | organize.apply 无 confirm 400 / confirm 200 applied=2；purge 无 confirm 400 |
| journal 收口（3） | write token 名下出现 append/mkdir/rmdir/restore/move/rename/organize；organize 按 paths 逐条（整理W2/新篇.md + 笔记A.md）；无 read/search/tree 留痕 |
| 旧端点 + 用户功能（4） | legacy create 200 + journal actor=w2-smoke-write；search elapsed_ms；pulse 200 today_words |
| 清理（1） | 删 4 笔记 + 2 目录后 admin purge all ≥4 |

## 4. W3 验证

| 项 | 命令 | 结果 |
|---|---|---|
| 矩阵一致性测试 | `py -m pytest tests/test_contract_matrix.py -q` | 165 passed |
| 全量回归 | `py -m pytest -q` | 564 passed（W2 基线 399 + 矩阵 165），43.97s |
| mermaid 门禁 | `validate_mermaid.cjs docs/AGENT-CONTRACT.md`（mermaid@8.8.3 + jsdom） | 见 §4.1 |

### 4.1 mermaid 校验结果

命令（依赖安装于 `.workbuddy/mermaid-check/`，经 NODE_PATH 注入）：

```bash
NODE_PATH="C:/Users/Lenovo/Desktop/Kwey/.workbuddy/mermaid-check/node_modules" \
  node "C:/Users/Lenovo/.qoder/skills/p8-full-stack/scripts/validate_mermaid.cjs" \
  docs/AGENT-CONTRACT.md
```

输出原文（退出码 0）：

```text
===== AGENT-CONTRACT.md：发现 1 个 mermaid 块 =====
  [OK]   block 0  (graph TD)  语法校验通过
         └ [WARN] 渲染环境限制，可忽略: labelSvg.node(...).getBBox is not a function
========================================
总计: 1 块 | parse-OK 1 | PARSE-ERROR 0 | 渲染环境告警 1
========================================
结论: 无语法错误，可交付。
```

getBBox 告警为 jsdom 无头环境限制，pytora 1.0.3 实际渲染不受影响（门禁既定口径）。

## 5. 行为变更披露（W2 起生效）

1. MCP stdio 写操作现在写 journal（署名 mcp-stdio/admin）；W1 及以前不留痕。
   方向：评审台需要全通道归因。无测试钉住旧「MCP 不留痕」行为。
2. 快照 `enabled` 缺省统一为 `false`（随配置实时读取）；旧 routes_history 私有
   缓存缺省 `true` 的不一致已消除。用户 vault 不会被擅自 git init。
3. M2 测试工具数断言 9→21 为卡内批准的演进（W1 门禁「基线不动」在 W2 按卡放开）。
4. `pulse.js` ACTION_LABEL 扩充 9 个新动作标签；前端需 F5 生效。

## 6. 已知限制 / 未验证项

1. `kwey.organize` 的 Provider 生成路径未在冒烟中走通（需配置 LLM Provider）；
   冒烟与单测覆盖的是「未配置 → 400」与 ops 直传预演/应用。Provider 真实生成
   质量不在本卡验收范围。
2. `kwey.history.rollback` 的 git happy path 由 pytest 覆盖
   （test_contract_w2 / test_m2_snapshots，真实系统 git）；REST 冒烟仅验证闸门
   与 status（克隆库快照缺省关闭）。
3. `pulse.js` 新标签为字典扩充，渲染回退路径（`ACTION_LABEL[a.action] || a.action`）
   在卡3 已浏览器实测；本轮未再起浏览器验证新标签渲染，需用户 F5 后目视确认。
4. 内置智能体（卡B）与 Verbot 对齐（卡C）未落地，本契约仅保证传输层 parity；
   进程内客户端 actor 注入约定见 AGENT-CONTRACT §5.1。

## 7. 复现

```bash
# 全量 + 矩阵
py -m pytest -q
py -m pytest tests/test_contract_matrix.py -q

# W2 冒烟（两个终端）
py main.py --server --vault .workbuddy/w2-smoke-vault --port 17643 --no-browser
py .workbuddy/w2_smoke.py

# mermaid 门禁（依赖一次性安装于 .workbuddy/mermaid-check/）
node "C:/Users/Lenovo/.qoder/skills/p8-full-stack/scripts/validate_mermaid.cjs" docs/AGENT-CONTRACT.md
```

克隆库与冒烟脚本均在 `.workbuddy/`，不进交付包；live 17630 由用户掌控，
重启仅在用户明示「拉起」时执行。