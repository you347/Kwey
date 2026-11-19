"""F3 个人信息库存储：三分区记忆 + 50/30/20 配额注入 + 闭环更新。

设计要点（对应 DESIGN.md 第七节 F3）
--------------------------------------
- 记忆存于 ``<vault>/.kwey/memory/{projects,profile,lessons}.md``，**都是普通 Markdown**，
  用户可在任意编辑器里查看 / 修改 / 删除（文件为源，比云端黑盒记忆透明）。
- 权重机制是**检索注入配额**：``inject_budget`` 按 50/30/20 从三分区取条目拼上下文，
  而不是把文件字面切成 5:3:2。
- 闭环更新：diff 确认落盘时 ``update_from_interaction`` 从结构化 ops 提取新记忆，
  去重合并进对应分区并衰减过期条目。

文件形态（条目 = 结构化列表，元数据放在行尾 HTML 注释里，预览时不可见）::

    ---
    section: projects
    updated: '2026-11-16T19:20:00+08:00'
    ---

    # 项目记忆

    > 注入配额 50% · 可直接编辑下列条目；保存时自动去重合并，权重越高越靠前。

    - 在写《Kwey 个人知识库》 <!-- id: m-1a2b3c4d · category: project · weight: 0.9 · last_used: 2026-11-16T19:20:00+08:00 · source: organize -->

读取同时兼容两种形态：
1. 上述结构化列表（用户手写 ``- 任意内容`` 也能解析，缺省元数据自动补齐，
   ``last_used`` 回退到文件 mtime）；
2. YAML frontmatter 的 ``entries: [{id, category, content, weight, last_used, source}]``
   （程序化写入/迁移用；下次写回会规范化为列表形态，内容不丢）。

写盘说明：``.kwey`` 属于 ``app.paths.IGNORED_DIRS``，``validate_relative`` 会拒绝该路径，
因此**不经 VaultService**（它不是笔记、不进索引），与 ``AppSettings`` / ``journal``
一致采用「临时文件 + os.replace」原子写。用户笔记正文在本模块内一律不读不写。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.parser import frontmatter as fm_mod

# ---------------------------------------------------------------------- 常量
SECTIONS: Tuple[str, ...] = ("projects", "profile", "lessons")

SECTION_LABELS: Dict[str, str] = {
    "projects": "项目记忆",
    "profile": "用户画像",
    "lessons": "经验教训",
}

# 注入配额：项目 50% / 画像 30% / 经验 20%（比例，不是文件字面切分）
SECTION_QUOTAS: Dict[str, float] = {
    "projects": 0.5,
    "profile": 0.3,
    "lessons": 0.2,
}

# 各分区默认 category / 时间衰减半衰期 / 过期阈值
_DEFAULT_CATEGORY: Dict[str, str] = {
    "projects": "project",
    "profile": "preference",
    "lessons": "practice",
}

# projects 衰减最快（项目会结束），profile 最稳定，lessons 按复用频率缓慢衰减
HALF_LIFE_DAYS: Dict[str, float] = {
    "projects": 7.0,
    "profile": 180.0,
    "lessons": 30.0,
}

STALE_DAYS: Dict[str, int] = {
    "projects": 30,
    "profile": 365,
    "lessons": 120,
}

DEFAULT_BUDGET = 4000
MIN_BUDGET = 200
MAX_BUDGET = 20000

MIN_WEIGHT = 0.05   # 权重下限（再低没有注入价值）
MAX_WEIGHT = 5.0    # 权重上限（防止单条记忆霸占配额）

_DIRNAME = "memory"
_SECTION_REL = ".kwey/memory/{section}.md"

# 跨实例共享锁：路由每次请求都新建 MemoryStore，读-改-写必须全局串行，
# 否则并发的面板保存 / 闭环更新会互相覆盖（与 agent/auth.py 同一处理方式）。
_LOCK = threading.RLock()

# 列表条目：- 内容 / * 内容 / 1. 内容
_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?P<body>.+?)\s*$")
# 行尾元数据注释
_META_RE = re.compile(r"\s*<!--\s*(?P<meta>.+?)\s*-->\s*$")
_META_SPLIT_RE = re.compile(r"\s*[·;；｜|]\s*")
_META_PAIR_RE = re.compile(r"^(?P<key>[A-Za-z_]+)\s*[:=]\s*(?P<value>.*)$")


# ---------------------------------------------------------------------- 工具
def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    """datetime → ISO 8601（本地时区、秒级）。"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone().isoformat(timespec="seconds")


def _parse_ts(value: Any) -> Optional[datetime]:
    """宽松解析时间：ISO 字符串 / 日期 / 时间戳 / datetime（含 YAML 的 date）；失败返回 None。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):  # YAML ``last_used: 2026-11-10`` 会被解析成 date
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 无时区按本地时间处理
    return dt


def _norm(text: str) -> str:
    """去重归一化：大小写 / 空白 / 常见标点不计差异。"""
    lowered = str(text or "").lower()
    stripped = re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】<>《》·…—-]+", "", lowered)
    return stripped


def _similar(a: str, b: str) -> float:
    """相似度（0~1）：归一化相等 → 1；一方是另一方的「精简版」→ 1；其余 → 0。

    刻意不用 difflib 的编辑距离：中文里一字之差（「低价值」/「高价值」）相似度就有
    0.9，按阈值合并会把不同记忆吞掉。这里只认「完全同义」与「包含关系」，
    保证去重可预期、不丢信息。
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) < 8:
        return 0.0
    if short in long and len(short) / len(long) >= 0.85:
        return 1.0
    return 0.0


def _clip_weight(value: Any, default: float = 1.0) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(MIN_WEIGHT, min(MAX_WEIGHT, weight)), 3)


def _title_of(path: Any) -> str:
    """vault 相对路径 → 笔记标题（去目录与扩展名）。"""
    text = str(path or "").replace("\\", "/").strip()
    name = text.rsplit("/", 1)[-1]
    return re.sub(r"\.(md|markdown)$", "", name, flags=re.IGNORECASE) or text or "未命名"


def _norm_path(path: Any) -> str:
    text = str(path or "").replace("\\", "/").strip().lstrip("./")
    return re.sub(r"\.(md|markdown)$", "", text, flags=re.IGNORECASE)


def _make_id(section: str, content: str) -> str:
    digest = hashlib.sha1(f"{section}\x00{_norm(content)}".encode("utf-8")).hexdigest()
    return f"m-{digest[:8]}"


# ---------------------------------------------------------------------- 条目
@dataclass
class MemoryEntry:
    """单条记忆。``section`` 只在内存里标注来源分区，不写入文件。"""

    id: str
    category: str
    content: str
    weight: float = 1.0
    last_used: str = ""
    source: str = "manual"
    section: str = field(default="", compare=False)

    def to_dict(self, score: Optional[float] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "content": self.content,
            "weight": self.weight,
            "last_used": self.last_used,
            "source": self.source,
            "section": self.section,
        }
        if score is not None:
            data["score"] = score
        return data


class MemoryStore:
    """``<vault>/.kwey/memory/`` 三分区记忆库。

    所有公开方法都做 section 白名单校验（非法分区抛 ``ValueError``，
    由路由层转 400）；写操作在**模块级共享锁**内完成读-改-写（路由每次请求都会
    新建实例，实例锁不足以串行化），避免并发覆盖。
    """

    SECTIONS = SECTIONS
    QUOTAS = SECTION_QUOTAS
    LABELS = SECTION_LABELS
    MAX_ENTRIES = 80          # 单分区条目上限（超出按分数淘汰最弱的）
    MAX_CONTENT = 2000        # 单条内容字符上限
    DECAY_FACTOR = 0.7        # 过期条目权重衰减系数
    DECAY_FLOOR = 0.1         # 衰减到该值以下直接淘汰
    REUSE_BUMP = 0.05         # 被注入复用时的升权步长（按天，最多一次）
    DUPLICATE_RATIO = 0.9     # 判重阈值（_similar 当前只产出 0/1：完全同义或包含式精简）

    def __init__(self, root: Path | str) -> None:
        self.root: Path = Path(root)
        self._lock = _LOCK  # 模块级共享锁：跨请求实例串行化读-改-写

    # ------------------------------------------------------------------ path
    @property
    def memory_dir(self) -> Path:
        return self.root / ".kwey" / _DIRNAME

    def path(self, section: str) -> Path:
        return self.root / _SECTION_REL.format(section=self._check(section))

    def rel_path(self, section: str) -> str:
        return _SECTION_REL.format(section=self._check(section))

    @staticmethod
    def _check(section: str) -> str:
        if section not in SECTIONS:
            raise ValueError(
                f"未知记忆分区: {section!r}（可选 {' / '.join(SECTIONS)}）"
            )
        return section

    # ------------------------------------------------------------------ read
    def read(self, section: str) -> List[MemoryEntry]:
        """读取分区条目（缺文件 → 空列表，不创建任何文件）。"""
        section = self._check(section)
        path = self.path(section)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return []
        try:
            fallback = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except OSError:
            fallback = _now()

        metadata, body = fm_mod.parse_frontmatter(text)
        entries: List[MemoryEntry] = []
        seen: set[str] = set()

        raw_entries = metadata.get("entries")
        if isinstance(raw_entries, list):
            for item in raw_entries:
                entry = self._entry_from_mapping(item, section, fallback)
                if entry is None:
                    continue
                entries.append(entry)
                seen.add(_norm(entry.content))

        for line in body.splitlines():
            entry = self._entry_from_line(line, section, fallback)
            if entry is None or _norm(entry.content) in seen:
                continue
            entries.append(entry)
            seen.add(_norm(entry.content))
            if len(entries) >= 1000:  # 防御异常文件（正常几十条）
                break
        return entries

    def _entry_from_mapping(
        self, item: Any, section: str, fallback: datetime
    ) -> Optional[MemoryEntry]:
        if isinstance(item, str):
            content, meta = item, {}
        elif isinstance(item, dict):
            content, meta = item.get("content"), item
        else:
            return None
        try:
            text = self._clean_content(content)
        except ValueError:
            return None
        return self._build_entry(
            section,
            text,
            id_value=meta.get("id"),
            category=meta.get("category"),
            weight=meta.get("weight"),
            last_used=meta.get("last_used"),
            source=meta.get("source"),
            fallback=fallback,
        )

    def _entry_from_line(
        self, line: str, section: str, fallback: datetime
    ) -> Optional[MemoryEntry]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">", "```")):
            return None
        match = _BULLET_RE.match(stripped)
        if not match:
            return None
        body = match.group("body")
        meta: Dict[str, str] = {}
        meta_match = _META_RE.search(body)
        if meta_match:
            body = body[: meta_match.start()].strip()
            meta = self._parse_meta(meta_match.group("meta"))
        try:
            text = self._clean_content(body)
        except ValueError:
            return None
        return self._build_entry(
            section,
            text,
            id_value=meta.get("id"),
            category=meta.get("category"),
            weight=meta.get("weight"),
            last_used=meta.get("last_used"),
            source=meta.get("source"),
            fallback=fallback,
        )

    @staticmethod
    def _parse_meta(raw: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for chunk in _META_SPLIT_RE.split(raw or ""):
            pair = _META_PAIR_RE.match(chunk.strip())
            if pair:
                result[pair.group("key").strip().lower()] = pair.group("value").strip()
        return result

    def _build_entry(
        self,
        section: str,
        content: str,
        *,
        id_value: Any = None,
        category: Any = None,
        weight: Any = None,
        last_used: Any = None,
        source: Any = None,
        fallback: Optional[datetime] = None,
    ) -> MemoryEntry:
        entry_id = str(id_value or "").strip()[:40] or _make_id(section, content)
        cat = str(category or "").strip()[:32] or _DEFAULT_CATEGORY[section]
        src = str(source or "").strip()[:32] or "manual"
        ts = _parse_ts(last_used) or fallback or _now()
        return MemoryEntry(
            id=entry_id,
            category=cat,
            content=content,
            weight=_clip_weight(weight),
            last_used=_iso(ts),
            source=src,
            section=section,
        )

    def _clean_content(self, content: Any) -> str:
        """单条内容规范化：去首尾空白、压成单行（列表条目不允许换行）。"""
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        if len(text) > self.MAX_CONTENT:
            raise ValueError(f"单条记忆过长（上限 {self.MAX_CONTENT} 字）")
        return text

    # ------------------------------------------------------------------ write
    def write(self, section: str, entries: Iterable[MemoryEntry]) -> Dict[str, Any]:
        """整分区写回（原子写）。

        保留用户自己写的东西：frontmatter 自定义字段、条目块前后的散文/备注都原样留存，
        只重写中间的 bullet 条目块（这正是「文件为源、用户可编辑」的关键）。
        """
        section = self._check(section)
        items = list(entries)
        path = self.path(section)

        metadata: Dict[str, Any] = {}
        preamble: List[str] = []
        postamble: List[str] = []
        if path.is_file():
            try:
                old_text = path.read_text(encoding="utf-8-sig")
            except OSError:
                old_text = ""
            if old_text:
                metadata, old_body = fm_mod.parse_frontmatter(old_text)
                preamble, postamble = self._split_surrounding(old_body)
        metadata = dict(metadata)
        metadata.pop("entries", None)  # 规范化后不再使用 frontmatter 条目
        metadata["section"] = section
        metadata["updated"] = _iso(_now())

        preamble = self._trim_blank_edges(preamble)
        postamble = self._trim_blank_edges(postamble)
        if not preamble:
            percent = int(round(self.QUOTAS[section] * 100))
            preamble = [
                f"# {self.LABELS[section]}",
                "",
                f"> 注入配额 {percent}% · 可直接编辑下列条目；保存时自动去重合并，权重越高越靠前。",
            ]

        block = [
            f"- {self._escape_content(entry.content)} {self._meta_comment(entry)}"
            for entry in items
        ]
        lines = list(preamble)
        if block:
            lines += [""] + block
        if postamble:
            lines += [""] + postamble
        body = "\n".join(lines).rstrip() + "\n"

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(fm_mod.dump_frontmatter(metadata, body), encoding="utf-8")
        os.replace(tmp, path)  # 同分区原子替换（Windows 同样安全）
        return {
            "section": section,
            "path": self.rel_path(section),
            "count": len(items),
        }

    @staticmethod
    def _split_surrounding(body: str) -> Tuple[List[str], List[str]]:
        """把正文切成「条目块之前 / 之后」两段，用于保留用户手写的散文与备注。"""
        lines = body.splitlines()
        bullet_idx = [i for i, line in enumerate(lines) if _BULLET_RE.match(line.strip())]
        if not bullet_idx:
            return lines, []
        return lines[: bullet_idx[0]], lines[bullet_idx[-1] + 1:]

    @staticmethod
    def _trim_blank_edges(lines: List[str]) -> List[str]:
        result = list(lines)
        while result and not result[0].strip():
            result.pop(0)
        while result and not result[-1].strip():
            result.pop()
        return result

    @staticmethod
    def _escape_content(content: str) -> str:
        """避免正文里的 ``-->`` 提前闭合元数据注释。"""
        return content.replace("-->", "—>")

    @staticmethod
    def _meta_comment(entry: MemoryEntry) -> str:
        return (
            "<!-- "
            f"id: {entry.id} · category: {entry.category} · weight: {entry.weight:g}"
            f" · last_used: {entry.last_used} · source: {entry.source}"
            " -->"
        )

    # ------------------------------------------------------------------ 评分
    @staticmethod
    def score(entry: MemoryEntry, section: str, now: Optional[datetime] = None) -> float:
        """排序分 = weight × 时间衰减（按分区半衰期做指数衰减）。"""
        now = now or _now()
        ts = _parse_ts(entry.last_used) or now
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        half_life = HALF_LIFE_DAYS.get(section, 30.0)
        return round(entry.weight * (0.5 ** (age_days / half_life)), 4)

    def _sorted_entries(
        self, section: str, now: Optional[datetime] = None
    ) -> List[Tuple[MemoryEntry, float]]:
        now = now or _now()
        scored = [(entry, self.score(entry, section, now)) for entry in self.read(section)]
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored

    # ------------------------------------------------------------------ 写接口
    def upsert(
        self,
        section: str,
        content: Any,
        *,
        category: Any = None,
        weight: Any = None,
        source: Any = "manual",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """新增或合并一条记忆（去重：归一化相等，或一方是另一方的精简版）。"""
        section = self._check(section)
        text = self._clean_content(content)
        now = now or _now()
        with self._lock:
            entries = self.read(section)
            candidate = self._build_entry(
                section,
                text,
                category=category,
                weight=weight,
                last_used=now,
                source=source,
                fallback=now,
            )
            merged, target = self._merge_into(entries, candidate, bump=0.1)
            trimmed = self._trim(entries, section, now)
            self.write(section, entries)
        result = {
            "section": section,
            "id": (target or candidate).id,
            "merged": merged,
            "count": len(entries),
            "trimmed": trimmed,
        }
        return result

    def replace(
        self,
        section: str,
        items: Iterable[Any],
        *,
        source: str = "manual",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """整分区替换（UI 保存用）：逐条规范化 + 去重，返回最新分区负载。"""
        section = self._check(section)
        now = now or _now()
        entries: List[MemoryEntry] = []
        for item in items:
            if isinstance(item, dict):
                text = self._clean_content(item.get("content"))
                entry = self._build_entry(
                    section,
                    text,
                    id_value=item.get("id"),
                    category=item.get("category"),
                    weight=item.get("weight"),
                    last_used=item.get("last_used") or now,
                    source=item.get("source") or source,
                    fallback=now,
                )
            else:
                if isinstance(item, MemoryEntry):
                    entry = self._build_entry(
                        section,
                        self._clean_content(item.content),
                        id_value=item.id,
                        category=item.category,
                        weight=item.weight,
                        last_used=item.last_used,
                        source=item.source or source,
                        fallback=now,
                    )
                else:
                    text = self._clean_content(item)
                    entry = self._build_entry(
                        section, text, weight=1.0, last_used=now,
                        source=source, fallback=now,
                    )
            self._merge_into(entries, entry, bump=0.0)
        with self._lock:
            # 用户主动保存不裁剪：面板/外部编辑器写多少就是多少（自动增长才裁剪）
            self.write(section, entries)
        return self.section_payload(section, now=now)

    def remove(self, section: str, entry_id: str) -> Dict[str, Any]:
        """按 id 删除单条记忆；返回删除后的分区负载。"""
        section = self._check(section)
        with self._lock:
            entries = [e for e in self.read(section) if e.id != entry_id]
            self.write(section, entries)
        return self.section_payload(section)

    def _merge_into(
        self, entries: List[MemoryEntry], candidate: MemoryEntry, *, bump: float = 0.0
    ) -> Tuple[bool, Optional[MemoryEntry]]:
        """把 candidate 合并进 entries（就地修改）；返回 (是否命中重复, 命中条目)。"""
        for existing in entries:
            same = existing.id == candidate.id or _norm(existing.content) == _norm(candidate.content)
            if not same and _similar(existing.content, candidate.content) >= self.DUPLICATE_RATIO:
                same = True
            if not same:
                continue
            # 保留信息量更大的一条；权重取高者（闭环更新时可轻微升权）
            if len(candidate.content) > len(existing.content):
                existing.content = candidate.content
            existing.weight = _clip_weight(
                max(existing.weight, candidate.weight) + bump, existing.weight
            )
            if _parse_ts(candidate.last_used):
                existing.last_used = candidate.last_used
            if candidate.source and candidate.source != "manual":
                existing.source = candidate.source
            return True, existing
        entries.append(candidate)
        return False, candidate

    def _trim(
        self, entries: List[MemoryEntry], section: str, now: Optional[datetime]
    ) -> int:
        """超上限时按分数淘汰最弱条目；返回淘汰条数。"""
        overflow = len(entries) - self.MAX_ENTRIES
        if overflow <= 0:
            return 0
        ordered = sorted(entries, key=lambda e: (self.score(e, section, now), e.last_used))
        drop = {id(e) for e in ordered[:overflow]}
        kept = [e for e in entries if id(e) not in drop]
        entries[:] = kept
        return overflow

    def decay(
        self, now: Optional[datetime] = None, *, sections: Optional[Iterable[str]] = None
    ) -> Dict[str, int]:
        """衰减过期条目：超过分区 stale 阈值 → 权重 ×0.7，跌破下限即淘汰。"""
        now = now or _now()
        targets = list(sections) if sections is not None else list(SECTIONS)
        dropped: Dict[str, int] = {}
        for section in targets:
            section = self._check(section)
            with self._lock:
                entries = self.read(section)
                kept: List[MemoryEntry] = []
                removed = 0
                changed = False
                for entry in entries:
                    ts = _parse_ts(entry.last_used) or now
                    age_days = max(0, (now - ts).days)
                    if age_days >= STALE_DAYS[section]:
                        new_weight = round(entry.weight * self.DECAY_FACTOR, 3)
                        if new_weight < self.DECAY_FLOOR:
                            removed += 1
                            changed = True
                            continue
                        if new_weight != entry.weight:
                            entry.weight = new_weight
                            changed = True
                    kept.append(entry)
                if changed:
                    self.write(section, kept)
                dropped[section] = removed
        return dropped

    # ------------------------------------------------------------------ 注入
    def build_injection(
        self,
        max_chars: int = DEFAULT_BUDGET,
        *,
        redistribute: bool = False,
        touch: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """按 50/30/20 配额拼装记忆上下文。

        - 每个分区先按 ``weight × 时间衰减`` 排序，在自己的配额内取条目；
        - 配额是**注入比例**，不是文件字面切分：文件再长也只取配额内的条目；
        - ``redistribute=True`` 时，某分区没用完的配额按 projects→profile→lessons
          顺序让给还有条目的分区（默认关闭，保持严格 5:3:2 可预期）；
        - ``touch=True`` 时把被注入条目的 ``last_used`` 刷新到当天并按天轻微升权
          （``REUSE_BUMP``，即 DESIGN 里「按复用次数升权」；默认关闭，预览只读）。
        """
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            raise ValueError("max_chars 必须是整数")
        if max_chars < MIN_BUDGET or max_chars > MAX_BUDGET:
            raise ValueError(f"max_chars 超出范围（{MIN_BUDGET}~{MAX_BUDGET}）")
        now = now or _now()

        state: Dict[str, Dict[str, Any]] = {}
        for section in SECTIONS:
            state[section] = {
                "scored": self._sorted_entries(section, now),
                "quota": int(max_chars * self.QUOTAS[section]),
                "used": 0,
                "lines": [],
                "included": [],
                "dropped": [],
            }

        def header_of(section: str) -> str:
            percent = int(round(self.QUOTAS[section] * 100))
            return f"## {self.LABELS[section]}（{percent}%）\n"

        def try_add(section: str, pair: Tuple[MemoryEntry, float], capacity: int) -> bool:
            """在 capacity 内写入一条；放不下时按剩余空间截断。"""
            st = state[section]
            remain = capacity - st["used"]
            prefix = "- "
            if remain < len(prefix) + 4:
                return False
            content = pair[0].content
            line = f"{prefix}{content}\n"
            if len(line) > remain:
                keep = max(1, remain - len(prefix) - 2)  # 预留省略号与换行
                if keep < 4:
                    return False
                line = f"{prefix}{content[:keep]}…\n"
            st["lines"].append(line)
            st["used"] += len(line)
            st["included"].append(pair)
            return True

        # 第一遍：严格配额
        for section in SECTIONS:
            st = state[section]
            if not st["scored"]:
                continue
            header = header_of(section)
            if st["quota"] <= len(header) + 4:
                st["dropped"] = list(st["scored"])
                continue
            st["lines"].append(header)
            st["used"] = len(header)
            for pair in st["scored"]:
                if not try_add(section, pair, st["quota"]):
                    st["dropped"].append(pair)
            if not st["included"]:
                # 一条都放不下：退掉表头，避免只剩空标题
                st["lines"].clear()
                st["used"] = 0
                st["dropped"] = list(st["scored"])

        # 第二遍（可选）：把未用满的配额让给仍有条目的分区
        if redistribute:
            leftover = max_chars - sum(state[s]["used"] for s in SECTIONS)
            for section in SECTIONS:
                st = state[section]
                if leftover <= 0 or not st["dropped"]:
                    continue
                if not st["lines"]:
                    header = header_of(section)
                    if leftover <= len(header) + 6:  # 至少要能放下一条（含省略空间）
                        continue
                    st["lines"].append(header)
                    st["used"] += len(header)
                    leftover -= len(header)
                for pair in list(st["dropped"]):
                    before = st["used"]
                    if not try_add(section, pair, st["used"] + leftover):
                        break
                    leftover -= st["used"] - before
                    st["dropped"].remove(pair)

        blocks = ["".join(state[s]["lines"]) for s in SECTIONS if state[s]["lines"]]
        text = "".join(blocks)
        if len(blocks) > 1:
            # 分区间留空行（Markdown 渲染更清晰）；空间不够就退回单换行/直拼
            for gap in ("\n\n", "\n"):
                candidate = gap.join(block.rstrip("\n") for block in blocks) + "\n"
                if len(candidate) <= max_chars:
                    text = candidate
                    break

        # 注入即「使用」：按天刷新 last_used 并轻微升权（默认关闭，预览保持只读）
        if touch:
            self.touch_used(
                {s: [pair[0].id for pair in state[s]["included"]] for s in SECTIONS},
                now=now,
            )

        sections_detail: Dict[str, Any] = {}
        for section in SECTIONS:
            st = state[section]
            sections_detail[section] = {
                "label": self.LABELS[section],
                "quota": st["quota"],
                "used": st["used"],
                "percent": int(round(self.QUOTAS[section] * 100)),
                "entries_total": len(st["scored"]),
                "entries": [entry.to_dict(score) for entry, score in st["included"]],
                "dropped": len(st["dropped"]),
            }
        return {
            "text": text,
            "budget": max_chars,
            "used": len(text),
            "remaining": max(0, max_chars - len(text)),
            "sections": sections_detail,
            "entry_count": sum(len(state[s]["included"]) for s in SECTIONS),
        }

    def touch_used(
        self, used_ids: Dict[str, Iterable[str]], *, now: Optional[datetime] = None
    ) -> int:
        """刷新被注入条目的 ``last_used``（同一自然日只升权一次）；返回变更条数。"""
        now = now or _now()
        today = now.date()
        changed_entries = 0
        for section, ids in used_ids.items():
            if section not in SECTIONS:
                continue
            wanted = set(ids)
            if not wanted:
                continue
            with self._lock:
                entries = self.read(section)
                changed = False
                for entry in entries:
                    if entry.id not in wanted:
                        continue
                    ts = _parse_ts(entry.last_used)
                    if ts is not None and ts.date() >= today:
                        continue  # 今天已经用过：不重复升权，避免刷权重
                    entry.weight = _clip_weight(entry.weight + self.REUSE_BUMP)
                    entry.last_used = _iso(now)
                    changed = True
                    changed_entries += 1
                if changed:
                    self.write(section, entries)
        return changed_entries

    def inject_budget(
        self, max_chars: int = DEFAULT_BUDGET, *, touch: bool = False
    ) -> str:
        """契约方法：按配额返回拼好的上下文字符串（≤ max_chars）。"""
        return self.build_injection(max_chars, touch=touch)["text"]

    # ------------------------------------------------------------------ 闭环更新
    def update_from_interaction(
        self,
        ops: Any,
        diff_result: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """diff 确认落盘后的闭环更新：提取 → 去重合并 → 衰减。

        提取有两个来源，可同时生效：
        1. ``diff_result["memory_candidates"]``：Provider / LLM 给出的语义化候选
           （``[{section, content, category?, weight?}]``，可选扩展点）；
        2. 结构化 ops 的启发式翻译（create/link/tag/move/append → 项目/经验记忆）。

        Args:
            ops: 已应用的结构化 ops（``app.agent.ops`` 的格式）；单项非法只跳过。
            diff_result: ``apply_ops`` 的返回值（含 ``paths``）；给了就只处理
                确实落盘的路径，避免把失败项写进记忆。

        Returns:
            ``{"added","merged","skipped","decayed","sections"}`` 统计。
        """
        if not isinstance(ops, list):
            raise ValueError("ops 必须是数组")
        now = now or _now()
        applied = self._applied_paths(diff_result)

        added = merged = skipped = 0

        def record(section: str, content: Any, category: Any, weight: Any, source: str) -> None:
            """合并一条候选；非法内容只计 skipped，绝不打断整批更新。"""
            nonlocal added, merged, skipped
            try:
                result = self.upsert(
                    section, content, category=category, weight=weight,
                    source=source, now=now,
                )
            except ValueError:
                skipped += 1
                return
            if result["merged"]:
                merged += 1
            else:
                added += 1

        # 1) 显式候选：Provider / LLM 在 diff_result["memory_candidates"] 里给出的
        #    语义化记忆（可选扩展点；未接 Provider 时该键不存在，走下面的启发式）
        for cand in self._explicit_candidates(diff_result):
            section = cand.get("section")
            if section not in SECTIONS:
                skipped += 1
                continue
            record(
                section,
                cand.get("content"),
                cand.get("category"),
                cand.get("weight") or 1.0,
                str(cand.get("source") or "organize"),
            )

        # 2) 启发式候选：把结构化 ops 翻译成可读的项目/经验记忆
        for op in ops:
            if not isinstance(op, dict):
                skipped += 1
                continue
            candidate = self._candidate_from_op(op)
            if candidate is None:
                skipped += 1
                continue
            if not self._is_applied(op, applied):
                skipped += 1  # 该项并未真正落盘
                continue
            section, content, category, weight = candidate
            record(section, content, category, weight, "organize")

        decayed = self.decay(now=now)
        sections = {s: len(self.read(s)) for s in SECTIONS}
        return {
            "added": added,
            "merged": merged,
            "skipped": skipped,
            "decayed": decayed,
            "sections": sections,
        }

    @staticmethod
    def _explicit_candidates(diff_result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """``diff_result["memory_candidates"]`` 里的语义化候选（可选，Provider 用）。"""
        if not isinstance(diff_result, dict):
            return []
        raw = diff_result.get("memory_candidates")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _applied_paths(diff_result: Optional[Dict[str, Any]]) -> set[str]:
        if not isinstance(diff_result, dict):
            return set()
        raw = diff_result.get("paths")
        if not isinstance(raw, list):
            return set()
        return {_norm_path(p) for p in raw if p}

    @staticmethod
    def _is_applied(op: Dict[str, Any], applied: set[str]) -> bool:
        """该项是否真的落盘：``move`` 的 op.path 是旧路径，落盘结果里是新路径。"""
        if not applied:
            return True  # 没给 diff_result 时信任调用方（手动触发场景）
        path = _norm_path(op.get("path"))
        if path in applied:
            return True
        if (op.get("type") or op.get("op")) == "move":
            base = path.rsplit("/", 1)[-1]
            return any(p.rsplit("/", 1)[-1] == base for p in applied)
        return False

    @staticmethod
    def _candidate_from_op(op: Dict[str, Any]) -> Optional[Tuple[str, str, str, float]]:
        """从单条 op 推导记忆候选 → (分区, 内容, category, weight)。

        纯启发式（零依赖、可离线）：不调用 LLM，只把「整理动作」翻译成可读经验。
        """
        op_type = op.get("type") or op.get("op")
        title = _title_of(op.get("path"))
        if op_type == "create":
            return "projects", f"在写《{title}》", "project", 0.9
        if op_type == "link":
            target = str(op.get("target") or "").strip()
            if not target:
                return None
            return "projects", f"《{title}》关联《{target}》", "relation", 0.7
        if op_type == "tag":
            tag = str(op.get("tag") or "").strip().lstrip("#")
            if not tag:
                return None
            return "lessons", f"整理《{title}》时使用标签 #{tag}", "practice", 0.6
        if op_type == "move":
            target = str(op.get("target") or "").strip()
            return "lessons", f"《{title}》归档到 {target or 'vault 根目录'}", "practice", 0.5
        if op_type == "append":
            return "lessons", f"《{title}》有内容更新", "practice", 0.4
        return None  # delete 等不产生记忆

    # ------------------------------------------------------------------ 负载
    def section_payload(
        self, section: str, *, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """单分区负载（GET / PUT 响应）。"""
        section = self._check(section)
        now = now or _now()
        scored = self._sorted_entries(section, now)
        return {
            "section": section,
            "label": self.LABELS[section],
            "quota": self.QUOTAS[section],
            "percent": int(round(self.QUOTAS[section] * 100)),
            "path": self.rel_path(section),
            "exists": self.path(section).is_file(),
            "count": len(scored),
            "entries": [entry.to_dict(score) for entry, score in scored],
        }

    def snapshot(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """全量负载（GET /api/memory）：三分区 + 配额 + 预算约定。"""
        now = now or _now()
        return {
            "sections": {s: self.section_payload(s, now=now) for s in SECTIONS},
            "quotas": dict(self.QUOTAS),
            "labels": dict(self.LABELS),
            "default_budget": DEFAULT_BUDGET,
            "budget_range": [MIN_BUDGET, MAX_BUDGET],
            "dir": f".kwey/{_DIRNAME}",
        }
