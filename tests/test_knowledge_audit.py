# tests/test_knowledge_audit.py
"""知识抽查（Knowledge Audit）测试：盘点 / 分层抽样 / 来源可靠性 / 内容实用性。

fixture：tests/fixtures/kg_mini/（小型 extractions + stories，确定性可单测）。
"""
from pathlib import Path

from tools.knowledge_audit import (
    KnowledgeEntry,
    build_name_index,
    build_reference_map,
    check_reliability,
    check_utility,
    inventory_stats,
    load_inventory,
    sample_entries,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kg_mini"
EXTRACTIONS = FIXTURE / "data" / "extractions"
STORIES = FIXTURE / "data" / "stories"


def test_load_inventory_counts_and_kinds():
    entries = load_inventory(str(EXTRACTIONS))
    # v1_events/main/示例章.json：events 2 + characters 3 + concepts 2 + factions 1 + locations 1 = 9
    # v3_seed_db_v3_final.json：concepts 3 + factions 1 + timeline 1 = 5
    assert len(entries) == 14
    kinds = {k: sum(1 for e in entries if e.kind == k) for k in
             ("event", "character", "concept", "faction", "location", "timeline")}
    assert kinds == {"event": 2, "character": 3, "concept": 5,
                     "faction": 2, "location": 1, "timeline": 1}


def test_entry_fields_extracted():
    entries = load_inventory(str(EXTRACTIONS))
    by_id = {e.id: e for e in entries}
    ev = [e for e in entries if e.kind == "event"][0]
    assert ev.line_range == [3, 8]
    assert ev.chapter == "示例章"
    assert ev.category == "main"
    assert "示例章.json" in ev.origin_file
    sanshantan = [e for e in entries if e.name == "《三山谈》"][0]
    assert sanshantan.source_records == []
    assert sanshantan.kind == "concept"
    source = [e for e in entries if e.name == "源石"][0]
    assert source.source_records[0]["source"] == "terra_book"


def test_inventory_stats():
    entries = load_inventory(str(EXTRACTIONS))
    stats = inventory_stats(entries)
    assert stats["total"] == 14
    assert stats["by_kind"]["concept"] == 5
    assert stats["by_source_file"]["v1_events"] == 9
    assert stats["by_source_file"]["v3_seed_db_v3_final.json"] == 5


def test_sample_size_and_reproducible():
    entries = load_inventory(str(EXTRACTIONS))
    s1, meta1 = sample_entries(entries, ratio=0.5, seed=42)
    s2, meta2 = sample_entries(entries, ratio=0.5, seed=42)
    assert meta1["sample_count"] >= 7  # ceil(0.5 * 14)
    assert meta1["sample_count"] <= 14
    assert [e.id for e in s1] == [e.id for e in s2]  # 同 seed 可复现
    assert meta1["ratio"] == 0.5 and meta1["seed"] == 42
    assert meta1["total"] == 14


def test_sample_stratified_by_kind():
    entries = load_inventory(str(EXTRACTIONS))
    samples, meta = sample_entries(entries, ratio=0.8, seed=7)
    sampled_kinds = {e.kind for e in samples}
    # 0.8 采样下每个 kind 都应仍有代表（分层不整组丢弃）
    assert sampled_kinds == {"event", "character", "concept", "faction",
                             "location", "timeline"}


def test_reliability_line_range_ok_and_out_of_range():
    entries = load_inventory(str(EXTRACTIONS))
    ev = [e for e in entries if e.kind == "event"][0]  # line_range [3,8]，示例章 12 行
    r = check_reliability(ev, str(STORIES))
    assert r["verdict"] == "reliable", r
    # 构造越界行号条目
    bad = KnowledgeEntry(
        id="x", kind="event", name="越界", aliases=[], source_records=[],
        line_range=[3, 100], chapter="示例章", category="main",
        origin_file="v1_events/main/示例章.json", raw={},
    )
    r2 = check_reliability(bad, str(STORIES))
    assert r2["verdict"] == "unreliable"
    assert any("行数" in i for i in r2["issues"])


def test_reliability_no_anchor_unreliable():
    entries = load_inventory(str(EXTRACTIONS))
    no_anchor = [e for e in entries if e.name == "无行号的讨论"][0]
    r = check_reliability(no_anchor, str(STORIES))
    assert r["verdict"] == "unreliable"
    assert r["has_source"] is False


def test_reliability_source_records():
    entries = load_inventory(str(EXTRACTIONS))
    source = [e for e in entries if e.name == "源石"][0]
    assert check_reliability(source, str(STORIES))["verdict"] == "reliable"
    sanshantan = [e for e in entries if e.name == "《三山谈》"][0]
    r = check_reliability(sanshantan, str(STORIES))
    assert r["verdict"] == "unreliable"
    assert any("source_records" in i for i in r["issues"])


def test_chapter_file_missing_unreliable():
    entries = load_inventory(str(EXTRACTIONS))
    missing = KnowledgeEntry(
        id="y", kind="event", name="缺失章节", aliases=[], source_records=[],
        line_range=[1, 2], chapter="不存在的章", category="main",
        origin_file="v1_events/main/不存在的章.json", raw={},
    )
    r = check_reliability(missing, str(STORIES))
    assert r["verdict"] == "unreliable"


def test_reliability_character_weak_anchor():
    """character 无行号锚点：角色名出现在章节原文中即可靠（弱锚点）。"""
    entries = load_inventory(str(EXTRACTIONS))
    jia = [e for e in entries if e.name == "甲"][0]
    r = check_reliability(jia, str(STORIES))
    assert r["verdict"] == "reliable", r
    ghost = KnowledgeEntry(
        id="z", kind="character", name="幽灵角色", aliases=[], source_records=[],
        line_range=None, chapter="示例章", category="main",
        origin_file="v1_events/main/示例章.json", raw={},
    )
    r2 = check_reliability(ghost, str(STORIES))
    assert r2["verdict"] == "unreliable"
    assert any("原文" in i for i in r2["issues"])


def test_name_index_with_aliases():
    entries = load_inventory(str(EXTRACTIONS))
    index = build_name_index(entries)
    names = set(index.keys())
    assert "源石技艺" in names
    assert "源石术" in names  # 别名入索引
    assert "甲" in names


def test_utility_dead_data_sanshantan():
    entries = load_inventory(str(EXTRACTIONS))
    index = build_name_index(entries)
    refs = build_reference_map(entries)
    sanshantan = [e for e in entries if e.name == "《三山谈》"][0]
    u = check_utility(sanshantan, index, refs)
    assert u["in_degree"] == 0
    assert u["dead"] is True


def test_utility_referenced_entry():
    entries = load_inventory(str(EXTRACTIONS))
    index = build_name_index(entries)
    refs = build_reference_map(entries)
    source = [e for e in entries if e.name == "源石"][0]
    u = check_utility(source, index, refs)
    assert u["in_degree"] >= 1  # 被 源石技艺.related_concepts 引用
    assert u["dead"] is False
    jia = [e for e in entries if e.name == "甲"][0]
    u2 = check_utility(jia, index, refs)
    assert u2["in_degree"] >= 1  # 被事件 participants 引用
    assert u2["dead"] is False


def test_utility_event_refs_validity():
    entries = load_inventory(str(EXTRACTIONS))
    index = build_name_index(entries)
    refs = build_reference_map(entries)
    ev = [e for e in entries if e.kind == "event"][0]
    u = check_utility(ev, index, refs)
    # 事件实用性 = 引用实体有效性（甲/乙/龙门 均存在索引）
    assert u["dead"] is False
    assert u["invalid_refs"] == []

def test_check_reliability_malformed_line_range_no_crash(tmp_path):
    """IM-16：畸形 line_range（1 元素 list / dict）不再 IndexError/KeyError 中断审计。

    章节文件必须存在（否则在越界检查前提前 return，测不到崩溃点）。
    """
    import json as _json
    from tools.knowledge_audit import KnowledgeEntry, check_reliability

    stories = tmp_path / "main"
    stories.mkdir()
    (stories / "chap.json").write_text(_json.dumps([1, 2]), encoding="utf-8")
    base = dict(id="x", kind="event", name="畸形", aliases=[], source_records=[],
                chapter="chap", category="main",
                origin_file="v1_events/main/chap.json", raw={})
    r1 = check_reliability(
        KnowledgeEntry(line_range=[3], **base), str(tmp_path))
    assert isinstance(r1, dict) and r1["verdict"] in ("reliable", "unreliable")
    r2 = check_reliability(
        KnowledgeEntry(line_range={"start": 1}, **base), str(tmp_path))
    assert isinstance(r2, dict)
