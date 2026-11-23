"""Bases 式结构化视图：frontmatter → 表格/卡片。

与 Obsidian Bases 的关系：
- 视图定义存 vault 内 ``views/*.base``（YAML），随仓库一起同步；
- 兼容读取 Obsidian Bases 的常见子集（``views[].type/order/sort`` +
  ``filters.and[].file.hasTag()/file.inFolder()``），迁移时旧文件能直接打开；
- 超越点：自然语言建视图（AI 只产出定义，落盘前由人确认）。

数据来源是 SQLite 索引（``props`` 表，可整库重建），**不读盘**：
20k 篇笔记的视图查询是内存过滤 + 排序，不做 N 次文件 IO。
"""

from app.views.query import (  # noqa: F401
    VIEW_DEFAULTS,
    normalize_definition,
    run_query,
)
from app.views.schema import build_schema, infer_type  # noqa: F401
from app.views.store import ViewStore, obsidian_base_to_definition  # noqa: F401
