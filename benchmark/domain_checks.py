"""域知识案例规则判定器（E9，无 LLM）：deletions / bridge 检查器。

背景：兄弟项目 #1 真实演示暴露域知识任务（删除/bridge）无 gold 可比，LLM Judge
「功能等价」模型不匹配。本模块改为对 Agent diff + 数据目录（仓库工作树）做确定性
规则判定，产出三态判据：DOMAIN_PASS / DOMAIN_FAIL / DOMAIN_SKIP（无法判定诚实 SKIP，
不做过度推断）。

判据与 tools/knowledge_audit.py（W5 审计定义，spec《2026-08-25-knowledge-correction.md》）
对齐：
- 来源可靠性锚点 = line_range / 原文出现 / source_records；本检查器在索引级落地为
  entity_source_map.source_files / seed source_records / v1_events line_range
  （原文出现以 source_files 覆盖，不做全文扫描，避免 2160 个 stories 文件的开销）。
- 实用性 = 结构化引用入度（related_* / participants / involved_*，与
  build_reference_map 同一字段集）；入度 0 判定死数据。

两个检查器的务实口径（无法通用判定的场景诚实 SKIP）：
- deletions：整文件删除块（deleted file mode）→ 每条目的三字段核验基于**剩余数据**
  （被删条目文件已不在工作树，不可能读回自身记录）：无来源锚点 ∧ 无事件参与 ∧ 入度 0
  全部满足才允许删除；任一不满足 → FAIL（附条目与字段证据）；删除知识索引/种子文件
  （非条目级）判破坏性变更 FAIL。
- bridge：受影响规范化名 = diff 中被删除/修改的 v3_wiki/concepts/*.md 文件主语（文件即
  实体）；引用入度「改前（git HEAD 基线）/ 改后（工作树）」对比：入度 0 → >0 为 bridge
  建立 PASS；无变化 FAIL；基线不可得（非 git 仓库/无 HEAD 版本）或 diff 未命中实体 → SKIP。

数据目录布局（兄弟项目 Arknights LLM Wiki 仓库根）：
    data/extractions/v3_seed_db_v2.json / v3_seed_db_v3_final.json（concepts/factions/
        locations/timeline_events，含 related_*、source_records）
    data/extractions/v1_events/{main,side,special}/*.json（events participants/location、
        条目 line_range 锚点）
    data/entity_source_map.json（实体 → type/source_files/related_* 索引）
    data/extractions/v3_wiki/concepts/<名>.md（条目 md，文件名即规范名）
"""
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# 判定三态（runner 直接把 verdict 写入 CaseResult.judge_verdict）
DOMAIN_PASS = "DOMAIN_PASS"
DOMAIN_FAIL = "DOMAIN_FAIL"
DOMAIN_SKIP = "DOMAIN_SKIP"

# 数据文件相对 data_dir（仓库根）的布局（统一正斜杠，与 git diff 路径一致）
DATA_SUBDIR = "data/extractions"
SEED_DB_FILES = ("v3_seed_db_v2.json", "v3_seed_db_v3_final.json")
ENTITY_SOURCE_MAP_REL = "data/entity_source_map.json"
CONCEPTS_DIR_REL = "data/extractions/v3_wiki/concepts"
V1_EVENTS_DIR_REL = "data/extractions/v1_events"

# 结构化引用字段（入度来源，与 tools/knowledge_audit.build_reference_map 对齐）
SEED_REF_FIELDS = ("related_concepts", "related_factions", "related_locations")
MAP_REF_FIELDS = ("related_entities", "related_factions",
                  "related_locations", "related_characters")
EVENT_REF_FIELDS = ("participants", "involved_nations", "involved_locations")

# 删除判据：三条必须同时满足（任一不满足 → FAIL）
DELETION_CRITERIA = ("无来源锚点", "无事件参与", "结构化引用入度 0")

# 名称规范化（与 knowledge_audit 同规则）：去书名号/引号/括号/空白
_NORM_RE = re.compile(r"[《》「」『』【】()（）\[\]·\s]+")


def _norm(name) -> str:
    """规范化名称：去书名号/引号/空白；非字符串按空处理（schema 漂移）。"""
    if not isinstance(name, str):
        return ""
    return _NORM_RE.sub("", name).strip()


@dataclass
class DomainCheckResult:
    """域判定结果：verdict（DOMAIN_PASS/FAIL/SKIP）+ reason（中文证据）。"""
    verdict: str
    reason: str


# --- diff 解析（git quotepath：非 ASCII 路径整段引号 + 八进制字节转义） ---


@dataclass
class _DiffFile:
    """diff 中一个文件块：a 侧相对路径（已解引号）+ 是否为整文件删除。"""
    path: str
    deleted: bool = False


def _unquote_git_path(s: str) -> str:
    """解析 git 引号包裹的路径：\\ooo 八进制字节序列 / \\" / \\\\ / 字面字符 → UTF-8 解码。

    git core.quotepath 默认开启：非 ASCII 路径按 C 风格整段引号包裹，字节以 \\ooo
    八进制表示（如 《三山谈》.md → "\\343\\200\\212..."），ASCII 部分保持字面。
    """
    if not (s.startswith('"') and '"' in s[1:]):
        return s
    out = bytearray()
    i = 1
    while i < len(s):
        c = s[i]
        if c == '"':
            break
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in ('"', "\\"):
                out += nxt.encode("utf-8")
                i += 2
                continue
            if nxt.isdigit():
                digits = nxt
                j = i + 2
                while j < len(s) and s[j].isdigit() and len(digits) < 3:
                    digits += s[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out += ("\\" + nxt).encode("utf-8")
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_a_prefix(p: str) -> str:
    """去掉路径的 a/ 前缀（可能包在引号内，如 "a/\\343..."）。"""
    p = p.strip()
    if p.startswith('"'):
        inner = _unquote_git_path(p)
        return inner[2:] if inner.startswith("a/") else inner
    return p[2:] if p.startswith("a/") else p


def _parse_diff_files(diff: str) -> list[_DiffFile]:
    """解析 diff 的文件块：diff --git 头 + deleted file mode 标记，路径取 a 侧。

    删除块固定带 deleted file mode 与 --- a/<path> 行（git 输出可能带尾部制表符，
    先 strip 再取路径）；改造块只取 diff --git 头路径（带空格文件名 git 不引号包裹，
    直接整行取用）。
    """
    files: list[_DiffFile] = []
    cur: _DiffFile | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = _DiffFile(path=_header_a_path(line[len("diff --git "):]))
            files.append(cur)
            continue
        if line.startswith("--- ") and cur is not None:
            cur.path = _strip_a_prefix(line[4:])
            continue
        if line.startswith("deleted file mode") and cur is not None:
            cur.deleted = True
    # 统一正斜杠（git diff 恒用 /，Windows os.sep 为 \\ 会破坏比较）
    for f in files:
        f.path = f.path.replace("\\", "/")
    return [f for f in files if f.path]


def _header_a_path(rest: str) -> str:
    """diff --git 头的 a 侧路径：完整引号对或第一个未引号 ' b/' 分隔。"""
    if rest.startswith('"'):
        parsed = _unquote_git_path(rest)
        return _strip_a_prefix(parsed)
    idx = rest.find(" b/")
    return _strip_a_prefix(rest[:idx] if idx != -1 else rest)


def _parse_deleted_paths(diff: str) -> list[str]:
    """diff 中删除文件（deleted file mode 块）的路径列表，按出现顺序。"""
    return [f.path for f in _parse_diff_files(diff) if f.deleted]


def _entity_name_from_path(rel: str) -> str:
    """v3_wiki/concepts/<名>.md 相对路径 → 规范名（文件名 stem）；非概念文件返回空串。"""
    if rel.startswith(CONCEPTS_DIR_REL + "/") and rel.endswith(".md"):
        return Path(rel).stem
    return ""


# --- 知识数据索引（工作树 / git HEAD 基线共用一套解析） ---


class _KnowledgeData:
    """知识数据索引：seed db / v1_events / entity_source_map → 引用与锚点证据表。

    由 feed(rel, text) 逐文件喂入（工作树读文件 / git HEAD 读内容两种来源统一走这里），
    finish() 后即可查询。只依赖 data_dir 下文件，不依赖 LLM 与兄弟项目代码。
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        # norm 引用名 → 结构化引用入度（related_* / participants / involved_*）
        self.refs: Counter = Counter()
        # norm 名 → 来源锚点证据列表（source_files / source_records / line_range）
        self.anchors: dict[str, set[str]] = {}
        # norm 名 ∈ 事件/时间线参与（participants/location/involved_*）
        self.event_participants: set[str] = set()
        # seed 记录（name → raw），供 bridge 取别名
        self.seed_records: dict[str, dict] = {}
        self.valid = False

    def _anchor(self, name: str, evidence: str) -> None:
        n = _norm(name)
        if n:
            self.anchors.setdefault(n, set()).add(evidence)

    def feed(self, rel: str, text: str) -> None:
        """按文件喂入（JSON 解析失败整文件跳过，不中断其余数据）。"""
        try:
            if rel == ENTITY_SOURCE_MAP_REL:
                self._feed_source_map(json.loads(text))
            elif rel in SEED_DB_FILES:
                self._feed_seed_db(json.loads(text))
            elif rel.startswith(V1_EVENTS_DIR_REL) and rel.endswith(".json"):
                self._feed_v1_events(json.loads(text))
        except (json.JSONDecodeError, OSError):
            return

    def _feed_seed_db(self, data: dict) -> None:
        for key, out in (("concepts", SEED_REF_FIELDS),
                         ("factions", SEED_REF_FIELDS),
                         ("locations", SEED_REF_FIELDS)):
            for rec in data.get(key) or []:
                if not isinstance(rec, dict):
                    continue
                name = rec.get("name") or ""
                self.seed_records[rec.get("name") or ""] = rec
                if rec.get("source_records"):
                    self._anchor(name, "seed source_records")
                for f in out:
                    for v in rec.get(f) or []:
                        n = _norm(v)
                        if n:
                            self.refs[n] += 1
        for tl in data.get("timeline_events") or []:
            if not isinstance(tl, dict):
                continue
            for f in ("involved_nations", "involved_locations"):
                for v in tl.get(f) or []:
                    n = _norm(v)
                    if n:
                        self.refs[n] += 1
                        self.event_participants.add(n)

    def _feed_v1_events(self, data: dict) -> None:
        for i, ev in enumerate(data.get("events") or []):
            if not isinstance(ev, dict):
                continue
            for f in ("participants",):
                for v in ev.get(f) or []:
                    n = _norm(v)
                    if n:
                        self.refs[n] += 1
                        self.event_participants.add(n)
            loc = ev.get("location")
            if loc:
                n = _norm(loc)
                if n:
                    self.refs[n] += 1
                    self.event_participants.add(n)
            if ev.get("line_range"):
                self._anchor(ev.get("event") or f"event#{i}", "v1_events line_range")
        for key, name_field in (("characters", "name"), ("concepts", "concept"),
                                ("factions", "faction"), ("locations", "location")):
            for it in data.get(key) or []:
                if not isinstance(it, dict):
                    continue
                if it.get("line_range"):
                    self._anchor(it.get(name_field) or "", "v1_events line_range")

    def _feed_source_map(self, data: dict) -> None:
        for key, ent in data.items():
            if not isinstance(ent, dict):
                continue
            srcs = ent.get("source_files") or {}
            if srcs and any(isinstance(v, list) and v for v in srcs.values()):
                self._anchor(key, "entity_source_map source_files")
            for f in MAP_REF_FIELDS:
                for v in ent.get(f) or []:
                    n = _norm(v)
                    if n:
                        self.refs[n] += 1

    def finish(self) -> None:
        """数据加载完成标记：至少一个数据源（seed/map/v1）才算可判定。"""
        self.valid = bool(self.seed_records or self.refs or self.anchors)

    def in_degree(self, name: str, aliases: list[str] | None = None) -> int:
        """规范化名（含别名）被结构化引用的入度。"""
        targets = {_norm(name)} | {_norm(a) for a in (aliases or [])}
        targets.discard("")
        return sum(self.refs[t] for t in targets)

    def deletion_evidence(self, name: str) -> list[str]:
        """删除条目三字段核验：任一不满足 → 证据列表（满足 → 空列表）。

        匹配目标 = 规范名 + 幸存 seed 记录的别名（删除 md 但 seed 记录未删时，
        别名引用的引用方仍指向该条目，须一并核验）。
        """
        rec = self.seed_records.get(name) or {}
        targets = {_norm(name)} | {_norm(a) for a in (rec.get("aliases") or [])}
        targets.discard("")
        ev: list[str] = []
        anchor_hits = sorted(self.anchors.get(_norm(name), set()))
        if anchor_hits:
            ev.append("来源锚点：仍存在 " + "/".join(anchor_hits))
        if targets & self.event_participants:
            ev.append("事件参与：仍被事件/时间线引用（participants/location/involved_*）")
        deg = sum(self.refs[t] for t in targets)
        if deg > 0:
            ev.append(f"结构化引用：入度 {deg}（related_* 仍指向该条目）")
        return ev


def _load_work_tree(data_dir: str) -> _KnowledgeData:
    """从工作树文件构建索引。"""
    data = _KnowledgeData(data_dir)
    ext = Path(data_dir) / DATA_SUBDIR
    for name in SEED_DB_FILES:
        p = ext / name
        if p.is_file():
            data.feed(name, p.read_text(encoding="utf-8"))
    for p in sorted((Path(data_dir) / V1_EVENTS_DIR_REL).rglob("*.json")):
        rel = os.path.relpath(p, data_dir).replace("\\", "/")
        data.feed(rel, p.read_text(encoding="utf-8"))
    p = Path(data_dir) / ENTITY_SOURCE_MAP_REL
    if p.is_file():
        data.feed(ENTITY_SOURCE_MAP_REL, p.read_text(encoding="utf-8"))
    data.finish()
    return data


def _git_show(data_dir: str, rel: str) -> str | None:
    """git -C data_dir show HEAD:<rel> → 基线内容；不可用/不存在返回 None。"""
    try:
        proc = subprocess.run(["git", "-C", data_dir, "show", f"HEAD:{rel}"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _load_git_head(data_dir: str) -> _KnowledgeData | None:
    """从 git HEAD 基线（= base 分支未修改状态）构建索引；不可用返回 None。"""
    data = _KnowledgeData(data_dir)
    ext = Path(data_dir) / DATA_SUBDIR
    for name in SEED_DB_FILES:
        if (ext / name).is_file():
            text = _git_show(data_dir, f"{DATA_SUBDIR}/{name}")
            if text is not None:
                data.feed(name, text)
    for p in sorted((Path(data_dir) / V1_EVENTS_DIR_REL).rglob("*.json")):
        rel = os.path.relpath(p, data_dir).replace("\\", "/")
        text = _git_show(data_dir, rel)
        if text is not None:
            data.feed(rel, text)
    if (Path(data_dir) / ENTITY_SOURCE_MAP_REL).is_file():
        text = _git_show(data_dir, ENTITY_SOURCE_MAP_REL)
        if text is not None:
            data.feed(ENTITY_SOURCE_MAP_REL, text)
    data.finish()
    return data if data.valid else None


# --- deletions 检查器 ---

# 删除即破坏性变更的知识库文件（非条目级）：索引 / 种子数据
_DESTRUCTIVE_DELETES = (
    ENTITY_SOURCE_MAP_REL,
    "data/extractions/v3_seed_db_v2.json",
    "data/extractions/v3_seed_db_v3_final.json",
)


def check_deletions(diff: str, data_dir: str) -> DomainCheckResult:
    """diff 删除文件集 → 逐条核验三字段；无删除 SKIP，任一违规 FAIL（附条目与字段证据）。"""
    deleted = _parse_deleted_paths(diff)
    if not deleted:
        return DomainCheckResult(DOMAIN_SKIP, "SKIP diff 无删除文件")
    data = _load_work_tree(data_dir)
    if not data.valid:
        return DomainCheckResult(
            DOMAIN_SKIP, "SKIP 无法判定：数据目录无知识数据（data/extractions 缺失或为空）")
    violations: list[str] = []
    checked = 0
    for rel in deleted:
        if rel in _DESTRUCTIVE_DELETES:
            violations.append(f"{rel}: 删除知识库索引/种子文件（非条目级删除，判破坏性变更）")
            continue
        name = _entity_name_from_path(rel)
        if not name:
            continue  # 非知识条目文件（如代码/文档），不在数据判据核验范围
        checked += 1
        ev = data.deletion_evidence(name)
        if ev:
            violations.append(f"{rel}: {name}——" + "；".join(ev))
    if checked == 0 and not violations:
        return DomainCheckResult(
            DOMAIN_SKIP, "SKIP 删除文件均非知识数据条目，无法按数据判据核验")
    if violations:
        ev_text = "；".join(violations)
        return DomainCheckResult(
            DOMAIN_FAIL, f"FAIL 删除违规（{ev_text}）")
    return DomainCheckResult(
        DOMAIN_PASS, "PASS 全部删除条目满足删除判据"
        f"（{' ∧ '.join(DELETION_CRITERIA)}）")


# --- bridge 检查器 ---


def _affected_entities(diff: str) -> list[str]:
    """diff 中被删除/修改的实体规范名（v3_wiki/concepts/*.md 文件主语，文件即实体）。"""
    names: list[str] = []
    seen: set[str] = set()
    for f in _parse_diff_files(diff):
        name = _entity_name_from_path(f.path)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def check_bridge(diff: str, data_dir: str) -> DomainCheckResult:
    """受影响实体引用入度改前（git HEAD）→ 改后（工作树）：0 → >0 = PASS；无变化 = FAIL。

    无法判定（diff 未命中实体 / 无 git 基线 / 无知识数据）→ 诚实 SKIP。
    """
    entities = _affected_entities(diff)
    if not entities:
        return DomainCheckResult(
            DOMAIN_SKIP, "SKIP 无法判定：diff 未命中受影响实体（v3_wiki/concepts 文件变更）")
    pre = _load_git_head(data_dir)
    if pre is None:
        return DomainCheckResult(
            DOMAIN_SKIP, "SKIP 无法判定：无法取得基线（data_dir 非 git 仓库或数据文件无 HEAD 版本）")
    post = _load_work_tree(data_dir)
    if not post.valid:
        return DomainCheckResult(DOMAIN_SKIP, "SKIP 无法判定：数据目录无知识数据")
    detail: list[str] = []
    unaffected: list[str] = []
    for name in entities:
        pre_aliases = list((pre.seed_records.get(name) or {}).get("aliases") or [])
        post_aliases = list((post.seed_records.get(name) or {}).get("aliases") or [])
        pre_deg = pre.in_degree(name, pre_aliases)
        post_deg = post.in_degree(name, post_aliases)
        if pre_deg == 0 and post_deg > 0:
            detail.append(f"{name}: 入度 {pre_deg} → {post_deg}（bridge 已建立）")
        elif pre_deg != post_deg:
            unaffected.append(f"{name}: 入度 {pre_deg} → {post_deg}")
        else:
            unaffected.append(f"{name}: 入度无变化（改前/改后均为 {pre_deg}）")
    if detail:
        return DomainCheckResult(
            DOMAIN_PASS, "PASS bridge 建立：" + "；".join(detail))
    return DomainCheckResult(
        DOMAIN_FAIL, "FAIL bridge 未建立：" + "；".join(unaffected))

