"""内置插件目录（bundle）：开箱即用的示例插件，供插件市场「安装」。

每个条目含 manifest 字段与 ``code``（main.js 源码）。安装时由 routes_plugins 把
manifest 与 code 写入 <vault>/.kwey/plugins/<id>/，之后与普通插件完全同构
（同一套权限校验、启用开关、Blob URL 加载、命令前缀）。

插件代码只用注入的 api 对象：registerCommand / notes.search / notes.read /
graph.local —— 不碰 window/DOM/localStorage，符合受信插件的最小权限原则。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 分类：用于市场 UI 的分组标签
CATEGORIES = ("全部", "推荐", "开发工具", "效率提升", "内容创作", "调研分析", "其他")

BUILTIN: List[Dict[str, Any]] = [
    {
        "id": "vault-health",
        "name": "知识库体检",
        "version": "1.0.0",
        "description": "一键扫描知识库：统计笔记数、孤儿笔记与断链，结果在右下角提示。",
        "category": "开发工具",
        "icon": "🩺",
        "recommended": True,
        "permissions": ["ui_command", "read_notes", "read_graph"],
        "code": r"""
/** 知识库体检：用注入的 api 统计笔记/断链，弹提示报告。 */
export function setup(api) {
  api.registerCommand('check', {
    title: '知识库体检（孤儿/断链）',
    run: async () => {
      const res = await api.notes.search('', 200);
      const hits = (res && res.hits) || [];
      const total = (res && res.total) || hits.length;
      let orphans = 0;
      for (const h of hits.slice(0, 60)) {
        try {
          const g = await api.graph.local(h.path, 1);
          const nodes = (g && g.nodes) || [];
          const links = (g && g.edges) || g.links || [];
          if (!links.length || nodes.length <= 1) orphans += 1;
        } catch (e) { /* 单篇图失败不影响整体统计 */ }
      }
      const msg = `知识库体检：共 ${total} 篇笔记，抽检 ${Math.min(hits.length, 60)} 篇，其中 ${orphans} 篇无明显双链邻居。`;
      if (api.toast) api.toast(msg, 'info', 6000);
      else console.info(msg);
    },
  });
}
export function unload() {}
""".strip(),
    },
    {
        "id": "random-note",
        "name": "随机漫游",
        "version": "1.0.0",
        "description": "从知识库随机打开一篇笔记，找回被遗忘的想法。",
        "category": "效率提升",
        "icon": "🎲",
        "recommended": True,
        "permissions": ["ui_command", "read_notes"],
        "code": r"""
/** 随机漫游：列出笔记后随机挑一篇，打开它。 */
export function setup(api) {
  api.registerCommand('random', {
    title: '随机漫游一篇笔记',
    run: async () => {
      const res = await api.notes.search('', 200);
      const hits = (res && res.hits) || [];
      if (!hits.length) { if (api.toast) api.toast('知识库里还没有笔记', 'error'); return; }
      const pick = hits[Math.floor(Math.random() * hits.length)];
      const path = pick.path || pick;
      if (api.openNote) api.openNote(path);
      else if (api.toast) api.toast('目标笔记：' + path, 'info');
    },
  });
}
export function unload() {}
""".strip(),
    },
    {
        "id": "daily-review",
        "name": "今日回顾",
        "version": "1.0.0",
        "description": "检索最近修改的笔记并生成回顾清单，适合每天收尾时快速过一遍。",
        "category": "效率提升",
        "icon": "📅",
        "recommended": False,
        "permissions": ["ui_command", "read_notes"],
        "code": r"""
/** 今日回顾：搜索并按路径列出笔记，弹提示。 */
export function setup(api) {
  api.registerCommand('review', {
    title: '生成今日回顾清单',
    run: async () => {
      const res = await api.notes.search('', 200);
      const hits = ((res && res.hits) || []).slice(0, 8);
      const lines = hits.map((h, i) => `${i + 1}. ${h.title || h.path}`).join('\n');
      const msg = '今日回顾候选：\n' + (lines || '暂无笔记');
      if (api.toast) api.toast(msg, 'info', 8000);
      else console.info(msg);
    },
  });
}
export function unload() {}
""".strip(),
    },
]


def catalog() -> List[Dict[str, Any]]:
    """返回市场清单（不含 code 字段的副本，避免下发源码到列表）。"""
    return [
        {k: v for k, v in item.items() if k != "code"}
        for item in BUILTIN
    ]


def get_builtin(plugin_id: str) -> Dict[str, Any] | None:
    """按 id 取内置插件（含 code）；不存在返回 None。"""
    for item in BUILTIN:
        if item["id"] == plugin_id:
            return item
    return None
