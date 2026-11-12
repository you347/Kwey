"""Kwey —— 本地优先、文件为源、Agent 原生的个人知识库。

M1 里程碑后端包。分层：
    app/api/    REST + WebSocket 路由层（统一 {code, data, message} 响应）
    app/vault/  VaultService 统一写路径（锁 → 原子写盘 → 增量索引 → 广播）
    app/index/  SQLite FTS5 索引 + 内存链接图（仅索引，可整库重建）
    app/parser/ Markdown / frontmatter / wikilink / 标签 / 渲染
磁盘 Markdown 永远是唯一事实源；.kwey/index.db 可随时删除重建。
"""

__version__ = "0.1.0"
