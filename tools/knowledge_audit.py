# tools/knowledge_audit.py
"""知识抽查：三次提取产物盘点 / 分层抽样 / 来源可靠性 / 内容实用性（规则层，无 LLM）。

对应 spec《2026-08-25-knowledge-correction.md》§1.3/§1.4 与实施计划 Task 1-4：
- 盘点：v1_events（Pass1 章节事件/实体）+ v3_seed_db_v2/v3_final（Pass3 世界观）统一条目模型
- 来源锚点：Pass1 用 chapter + line_range（对照 stories 行数），Pass3 用 source_records
- 实用性：结构化引用入度（participants / related_* / involved_*），入度 0 判定死数据
"""
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

# 名称规范化：去除书名号/引号/括号/空白（《三山谈》→三山谈）
_NORM_RE = re.compile(r"[《》「」『』【】()（）\[\]·\s]+")


def _norm(name) -> str:
    """规范化名称：去书名号/引号/空白，用于引用匹配。

    真实数据个别字段为 dict/None（schema 漂移），非字符串统一按空处理。
    """
    if not isinstance(name, str):
        return ""
    return _NORM_RE.sub("", name).strip()


@dataclass
class KnowledgeEntry:
    """统一知识条目（三次提取产物的共同视图）。"""
    id: str                    # origin_file::kind[index]
    kind: str                  # event/character/concept/faction/location/timeline
    name: str
    aliases: list[str]
    source_records: list[dict]  # Pass3 来源记录
    line_range: list[int] | None  # Pass1 行号锚点
    chapter: str | None        # Pass1 章节名
    category: str | None       # Pass1 类别 main/side/special
    origin_file: str           # 相对 extractions 的文件路径
    raw: dict = field(default_factory=dict)


def _load_v1_events(base: Path, out: list[KnowledgeEntry]) -> None:
    """Pass1：v1_events/{main,side,special}/<章节>.json 的 events/characters/concepts/factions/locations。"""
    v1 = base / "v1_events"
    if not v1.exists():
        return
    for chap_file in sorted(v1.rglob("*.json")):
        try:
            data = json.loads(chap_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        category = data.get("category") or (
            chap_file.parent.name if chap_file.parent.name in ("main", "side", "special") else "main")
        chapter = data.get("chapter") or chap_file.stem
        rel = chap_file.relative_to(base).as_posix()
        for i, ev in enumerate(data.get("events") or []):
            out.append(KnowledgeEntry(
                id=f"{rel}::events[{i}]", kind="event",
                name=(ev.get("event") or f"event#{i}")[:80], aliases=[],
                source_records=[], line_range=ev.get("line_range"),
                chapter=chapter, category=category, origin_file=rel, raw=ev))
        for i, c in enumerate(data.get("characters") or []):
            out.append(KnowledgeEntry(
                id=f"{rel}::characters[{i}]", kind="character",
                name=c.get("name") or "", aliases=[], source_records=[],
                line_range=None, chapter=chapter, category=category,
                origin_file=rel, raw=c))
        for i, c in enumerate(data.get("concepts") or []):
            out.append(KnowledgeEntry(
                id=f"{rel}::concepts[{i}]", kind="concept",
                name=(c.get("concept") or "")[:80], aliases=[], source_records=[],
                line_range=c.get("line_range"), chapter=chapter, category=category,
                origin_file=rel, raw=c))
        for i, f in enumerate(data.get("factions") or []):
            out.append(KnowledgeEntry(
                id=f"{rel}::factions[{i}]", kind="faction",
                name=f.get("faction") or "", aliases=[], source_records=[],
                line_range=f.get("line_range"), chapter=chapter, category=category,
                origin_file=rel, raw=f))
        for i, loc in enumerate(data.get("locations") or []):
            out.append(KnowledgeEntry(
                id=f"{rel}::locations[{i}]", kind="location",
                name=loc.get("location") or "", aliases=[], source_records=[],
                line_range=loc.get("line_range"), chapter=chapter, category=category,
                origin_file=rel, raw=loc))


def _load_seed_file(path: Path, out: list[KnowledgeEntry]) -> None:
    """Pass3：v3_seed_db_v2/v3_final 的 concepts/factions/locations/timeline_events。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    mapping = {"concepts": "concept", "factions": "faction",
               "locations": "location", "timeline_events": "timeline"}
    for key, kind in mapping.items():
        for i, it in enumerate(data.get(key) or []):
            out.append(KnowledgeEntry(
                id=f"{path.name}::{key}[{i}]", kind=kind,
                name=(it.get("name") or it.get("event") or "")[:80],
                aliases=list(it.get("aliases") or []),
                source_records=list(it.get("source_records") or []),
                line_range=None, chapter=None, category=None,
                origin_file=path.name, raw=it))


def load_inventory(extractions_dir: str) -> list[KnowledgeEntry]:
    """盘点三次提取产物，返回统一条目列表。"""
    base = Path(extractions_dir)
    out: list[KnowledgeEntry] = []
    _load_v1_events(base, out)
    for name in ("v3_seed_db_v2.json", "v3_seed_db_v3_final.json"):
        p = base / name
        if p.exists():
            _load_seed_file(p, out)
    return out


def inventory_stats(entries: list[KnowledgeEntry]) -> dict:
    """统计：总数 / 按 kind / 按来源文件。"""
    by_kind: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for e in entries:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        src = "v1_events" if e.origin_file.startswith("v1_events") else e.origin_file
        by_src[src] = by_src.get(src, 0) + 1
    return {"total": len(entries), "by_kind": by_kind, "by_source_file": by_src}


def sample_entries(entries: list[KnowledgeEntry], ratio: float = 0.02,
                   seed: int = 42) -> tuple[list[KnowledgeEntry], dict]:
    """按 kind 分层比例抽样；样本数 ≥ ceil(ratio × N)；同 seed 可复现。"""
    rng = random.Random(seed)
    by_kind: dict[str, list[KnowledgeEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)
    samples: list[KnowledgeEntry] = []
    for kind, pool in by_kind.items():
        n = min(len(pool), max(1, math.ceil(len(pool) * ratio)))
        samples.extend(rng.sample(pool, n))
    rng.shuffle(samples)
    meta = {
        "ratio": ratio, "seed": seed, "total": len(entries),
        "sample_count": len(samples),
    }
    return samples, meta


def build_name_index(entries: list[KnowledgeEntry]) -> dict[str, list[int]]:
    """规范化名称（name + aliases）→ 条目下标索引。"""
    index: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        for name in [e.name] + list(e.aliases):
            n = _norm(name)
            if n:
                index.setdefault(n, []).append(i)
    return index


def build_reference_map(entries: list[KnowledgeEntry]) -> dict[str, list[tuple[str, str]]]:
    """结构化引用表：规范化名 → [(引用者 entry id, 字段名)]。

    只统计结构化字段（participants / location / related_* / involved_*），
    不做正文文本包含匹配（避免短名误伤）。
    """
    refs: dict[str, list[tuple[str, str]]] = {}

    def add(name: str, rid: str, field: str) -> None:
        n = _norm(name)
        if n:
            refs.setdefault(n, []).append((rid, field))

    for e in entries:
        raw = e.raw
        if e.kind == "event":
            for p in raw.get("participants") or []:
                add(p, e.id, "participants")
            if raw.get("location"):
                add(raw["location"], e.id, "location")
        elif e.kind in ("concept", "faction", "location"):
            for f in ("related_concepts", "related_factions", "related_locations"):
                for x in raw.get(f) or []:
                    add(x, e.id, f)
        elif e.kind == "timeline":
            for f in ("involved_nations", "involved_locations"):
                for x in raw.get(f) or []:
                    add(x, e.id, f)
    return refs


def check_reliability(entry: KnowledgeEntry, stories_dir: str) -> dict:
    """来源可靠性：Pass1 对照章节文件行数；Pass3 检查 source_records / timeline source。

    IM-16：line_range 先做类型/长度校验（list 且末元素为 int 才有效）——畸形
    形态（1 元素 list / dict 等 schema 漂移，docstring 自认真实数据存在）降级
    为无锚点走 elif 链，不再 IndexError/KeyError 中断整场审计。
    """
    issues: list[str] = []
    has_source = False
    verdict = "reliable"
    line_range = entry.line_range if (
        isinstance(entry.line_range, list) and len(entry.line_range) >= 2
        and isinstance(entry.line_range[1], int)) else None
    if line_range:
        has_source = True
        # stories 布局：stories/{category}/{chapter}.json（单文件）或 stories/{category}/{chapter}/（目录）
        chap_file = Path(stories_dir) / (entry.category or "") / f"{entry.chapter}.json"
        chap_dir = Path(stories_dir) / (entry.category or "") / (entry.chapter or "")
        if chap_file.exists():
            n_lines = len(chap_file.read_text(encoding="utf-8").splitlines())
        elif chap_dir.is_dir():
            n_lines = sum(len(p.read_text(encoding="utf-8").splitlines())
                          for p in sorted(chap_dir.glob("*.json")))
        else:
            issues.append(f"章节文件不存在: stories/{entry.category}/{entry.chapter}")
            verdict = "unreliable"
            return {"has_source": has_source, "issues": issues, "verdict": verdict}
        if line_range[1] > n_lines:
            issues.append(f"line_range 越界 {entry.line_range}（超出文件行数 {n_lines}）")
            verdict = "unreliable"
    elif entry.kind == "character" and entry.chapter:
        # 弱锚点：角色名（含别名）出现在对应章节 stories 原文中
        has_source = True
        chap_dir = Path(stories_dir) / (entry.category or "") / (entry.chapter or "")
        names = [n for n in [entry.name] + list(entry.aliases) if n]
        found = False
        if chap_dir.is_dir():
            hay = "".join(p.read_text(encoding="utf-8", errors="replace")
                          for p in sorted(chap_dir.glob("*.json")))
            found = any(n in hay for n in names)
        if not found:
            issues.append(f"角色名未在章节原文出现: {entry.name}")
            verdict = "unreliable"
    elif entry.source_records:
        has_source = True
        for sr in entry.source_records[:5]:
            if not sr.get("source"):
                issues.append("source_records 条目缺少 source 字段")
                verdict = "unreliable"
    elif entry.kind == "timeline" and entry.raw.get("source"):
        has_source = True
    else:
        issues.append("无来源锚点：既无 line_range 也无 source_records")
        verdict = "unreliable"
    return {"has_source": has_source, "issues": issues, "verdict": verdict}


def check_utility(entry: KnowledgeEntry, index: dict[str, list[int]],
                  refs: dict[str, list[tuple[str, str]]]) -> dict:
    """内容实用性：命名实体看引用入度（0 = 死数据）；event/timeline 看引用实体有效性。"""
    if entry.kind in ("character", "concept", "faction", "location"):
        hit: list[tuple[str, list[tuple[str, str]]]] = []
        for name in [entry.name] + list(entry.aliases):
            n = _norm(name)
            if n and refs.get(n):
                hit.append((name, refs[n]))
        in_degree = sum(len(v) for _, v in hit)
        return {
            "in_degree": in_degree, "refs": hit, "dead": in_degree == 0,
            "invalid_refs": [],
        }
    # event / timeline：引用有效性（参与者/涉及实体是否存在于索引）
    ref_names: list[str] = []
    if entry.kind == "event":
        ref_names = list(entry.raw.get("participants") or [])
        if entry.raw.get("location"):
            ref_names.append(entry.raw["location"])
    elif entry.kind == "timeline":
        ref_names = (list(entry.raw.get("involved_nations") or [])
                     + list(entry.raw.get("involved_locations") or []))
    invalid = [rn for rn in ref_names if _norm(rn) not in index]
    return {"in_degree": len(ref_names), "refs": [], "dead": False,
            "invalid_refs": invalid}