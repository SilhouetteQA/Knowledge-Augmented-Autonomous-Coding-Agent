# tests/test_domain_checks.py
"""域案例判定模型测试（E9）：diff 删除路径解析 / deletions / bridge 规则检查器。

判据与 tools/knowledge_audit.py（W5 审计）对齐：来源锚点 = source_records/line_range/
source_files（索引级等价的原文出现）；实用性 = 结构化引用入度（related_*/participants/
involved_*）。检查器只读 data_dir 下数据文件与 git 基线，不依赖 LLM。
"""
import json
import subprocess
from pathlib import Path

import pytest

from benchmark import domain_checks
from benchmark.domain_checks import (
    DOMAIN_FAIL,
    DOMAIN_PASS,
    DOMAIN_SKIP,
    check_deletions,
    _parse_deleted_paths,
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _seed_db(concepts=None, timeline=None) -> str:
    return json.dumps({
        "concepts": concepts or [],
        "factions": [],
        "locations": [],
        "timeline_events": timeline or [],
        "_meta": {"version": "test"},
    }, ensure_ascii=False)


def _concept(name, aliases=None, source_records=None, related_concepts=None):
    return {"name": name, "category": "测试", "definition": "d", "summary": "s",
            "aliases": aliases or [], "source_records": source_records or [],
            "related_concepts": related_concepts or [],
            "related_factions": [], "related_locations": []}


def _make_data_dir(tmp_path, concepts=None, events=None, source_map=None,
                   timeline=None) -> Path:
    """构造最小知识数据目录（data/extractions + data/entity_source_map + v1_events）。"""
    _write(tmp_path, "data/extractions/v3_seed_db_v2.json",
           _seed_db(concepts=concepts, timeline=timeline))
    if events is not None:
        _write(tmp_path, "data/extractions/v1_events/main/示例章.json",
               json.dumps({"chapter": "示例章", "category": "main",
                           "events": events}, ensure_ascii=False))
    if source_map is not None:
        _write(tmp_path, "data/entity_source_map.json",
               json.dumps(source_map, ensure_ascii=False))
    return tmp_path


# --- diff 删除路径解析（git quotepath 引号/八进制转义，中文文件名） ---


def test_parse_deleted_paths_plain():
    diff = (
        "diff --git a/deep dir file.md b/deep dir file.md\n"
        "deleted file mode 100644\n"
        "--- a/deep dir file.md\t\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    assert _parse_deleted_paths(diff) == ["deep dir file.md"]


def test_parse_deleted_paths_git_quoted_chinese_octal():
    """git quotepath 默认开启：非 ASCII 文件名整段引号 + 八进制字节转义。"""
    quoted = (r'"a/\343\200\212\344\270\211\345\261\261\350\260\210'
              r'\343\200\213.md"')
    diff = (
        f"diff --git {quoted} {quoted.replace('a/', 'b/')}\n"
        "deleted file mode 100644\n"
        f"--- {quoted}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-内容\n"
    )
    assert _parse_deleted_paths(diff) == ["《三山谈》.md"]


def test_parse_deleted_paths_none_on_pure_modification():
    diff = (
        "diff --git a/README.md b/README.md\n"
        "index 8b5f6f5..2bd9d3a 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n-hello\n+hello world\n"
    )
    assert _parse_deleted_paths(diff) == []


# --- deletions 检查器 ---

DELETION_DIFF = (
    "diff --git a/data/extractions/v3_wiki/concepts/死概念.md "
    "b/data/extractions/v3_wiki/concepts/死概念.md\n"
    "deleted file mode 100644\n"
    "--- a/data/extractions/v3_wiki/concepts/死概念.md\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n-# 死概念\n-定义无。\n"
)


def test_deletions_skip_no_deletion(tmp_path):
    """diff 无删除文件 → SKIP（不误判，与 Judge SKIP 语义一致）。"""
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")])
    diff = (
        "diff --git a/README.md b/README.md\n"
        "index 8b5f6f5..2bd9d3a 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n-hello\n+hello world\n"
    )
    res = check_deletions(diff, str(data_dir))
    assert res.verdict == DOMAIN_SKIP
    assert "无删除" in res.reason


def test_deletions_pass_dead_data(tmp_path):
    """删除条目无来源锚点 ∧ 无事件参与 ∧ 入度 0 → PASS（死数据删除合规）。"""
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")])
    res = check_deletions(DELETION_DIFF, str(data_dir))
    assert res.verdict == DOMAIN_PASS
    assert "删除判据" in res.reason


def test_deletions_fail_structured_refs(tmp_path):
    """删除条目仍被其他条目 related_* 引用（入度 > 0）→ FAIL 附字段证据。"""
    data_dir = _make_data_dir(tmp_path, concepts=[
        _concept("死概念"),
        _concept("活概念", related_concepts=["死概念"]),
    ])
    res = check_deletions(DELETION_DIFF, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "死概念" in res.reason
    assert "结构化引用" in res.reason and "入度" in res.reason


def test_deletions_fail_source_anchor(tmp_path):
    """删除条目在 entity_source_map 仍有来源记录（index 级来源锚点）→ FAIL。"""
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")],
                              source_map={"死概念": {
                                  "type": "concept",
                                  "source_files": {"pass1_events": ["示例章.json"]}}})
    res = check_deletions(DELETION_DIFF, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "来源锚点" in res.reason


def test_deletions_fail_event_participation(tmp_path):
    """删除条目仍被事件 participants 记录（事件参与）→ FAIL 附字段证据。"""
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")],
                              events=[{"event": "会面", "participants": ["死概念"],
                                       "location": "龙门"}])
    res = check_deletions(DELETION_DIFF, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "事件参与" in res.reason


def test_deletions_fail_via_surviving_record_alias(tmp_path):
    """删除 md 但 seed 记录幸存且带别名：引用方用别名引用 → 别名入度 >0 → FAIL。"""
    data_dir = _make_data_dir(tmp_path, concepts=[
        _concept("死概念", aliases=["玛莉娅"]),
        _concept("活概念", related_concepts=["玛莉娅"]),
    ])
    res = check_deletions(DELETION_DIFF, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "结构化引用" in res.reason and "入度" in res.reason


def test_deletions_fail_mixed_reports_violating_entry(tmp_path):
    """多个删除条目：违规条目逐条报告（含路径与字段证据），合规条目不计。"""
    diff = (
        "diff --git a/data/extractions/v3_wiki/concepts/合规概念.md "
        "b/data/extractions/v3_wiki/concepts/合规概念.md\n"
        "deleted file mode 100644\n"
        "--- a/data/extractions/v3_wiki/concepts/合规概念.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-合规\n"
        "diff --git a/data/extractions/v3_wiki/concepts/违规概念.md "
        "b/data/extractions/v3_wiki/concepts/违规概念.md\n"
        "deleted file mode 100644\n"
        "--- a/data/extractions/v3_wiki/concepts/违规概念.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-违规\n"
    )
    data_dir = _make_data_dir(tmp_path, concepts=[
        _concept("违规概念"),
        _concept("合规概念"),
        _concept("活概念", related_concepts=["违规概念"]),
    ])
    res = check_deletions(diff, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "违规概念" in res.reason and "活概念" not in res.reason.split("违规概念")[0]


def test_deletions_fail_destroys_index_file(tmp_path):
    """删除知识索引/种子文件（非条目级）→ FAIL 判破坏性变更。"""
    diff = (
        "diff --git a/data/entity_source_map.json b/data/entity_source_map.json\n"
        "deleted file mode 100644\n"
        "--- a/data/entity_source_map.json\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-{}\n"
    )
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")])
    res = check_deletions(diff, str(data_dir))
    assert res.verdict == DOMAIN_FAIL
    assert "entity_source_map" in res.reason


def test_deletions_skip_no_knowledge_data(tmp_path):
    """data_dir 无知识数据（无法对照判据）→ SKIP（诚实不推断）。"""
    diff = (
        "diff --git a/data/extractions/v3_wiki/concepts/死概念.md "
        "b/data/extractions/v3_wiki/concepts/死概念.md\n"
        "deleted file mode 100644\n"
        "--- a/data/extractions/v3_wiki/concepts/死概念.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-死概念\n"
    )
    res = check_deletions(diff, str(tmp_path))
    assert res.verdict == DOMAIN_SKIP


# --- bridge 检查器（git HEAD 基线 = 改前，工作树 = 改后） ---


def _git(args, cwd):
    """git 子进程封装（UTF-8 解码，规避 Windows GBK 乱码）。"""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _init_bridge_repo(tmp_path, seed_concepts) -> Path:
    """初始化真实 git 仓库：base 提交含 seed db + 死概念 md（改前基线 = HEAD）。"""
    repo = tmp_path / "repo"
    _write(repo, "data/extractions/v3_seed_db_v2.json",
           _seed_db(concepts=seed_concepts))
    _write(repo, "data/extractions/v3_wiki/concepts/死概念.md",
           "# 死概念\n\n**定义:** 无。\n")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t",
          "commit", "-q", "-m", "base"], repo)
    return repo


def test_bridge_pass_related_ref_established(tmp_path):
    """改前入度 0 → 改后入度 >0（增加 related_concepts 引用）→ PASS。"""
    repo = _init_bridge_repo(tmp_path, [
        _concept("死概念"), _concept("活概念"),
    ])
    seed = repo / "data" / "extractions" / "v3_seed_db_v2.json"
    data = json.loads(seed.read_text(encoding="utf-8"))
    for c in data["concepts"]:
        if c["name"] == "活概念":
            c["related_concepts"] = ["死概念"]
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    md = repo / "data" / "extractions" / "v3_wiki" / "concepts" / "死概念.md"
    md.write_text(md.read_text(encoding="utf-8") + "补充 bridge 说明\n", encoding="utf-8")
    diff = _git(["diff", "HEAD"], repo)
    res = domain_checks.check_bridge(diff, str(repo))
    assert res.verdict == DOMAIN_PASS
    assert "死概念" in res.reason and "0 → 1" in res.reason


def test_bridge_pass_alias_bridge(tmp_path):
    """命名 bridge（W5 加别名打通 events.participants 整名匹配）：别名入度 0 → >0 → PASS。"""
    # 基线与工作树共用 v1 events（participants 用真名 玛莉娅，不入索引）
    repo = _init_bridge_repo(tmp_path, [
        _concept("死概念"), _concept("活概念"),
    ])
    _write(repo, "data/extractions/v1_events/main/示例章.json",
           json.dumps({"chapter": "示例章", "category": "main",
                       "events": [{"event": "会面", "participants": ["玛莉娅"],
                                   "location": "龙门"}]}, ensure_ascii=False))
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t",
          "commit", "-q", "-m", "events"], repo)
    # 工作树：死概念 seed 记录补别名 玛莉娅（bridge 打通 participants 引用）
    seed = repo / "data" / "extractions" / "v3_seed_db_v2.json"
    data = json.loads(seed.read_text(encoding="utf-8"))
    for c in data["concepts"]:
        if c["name"] == "死概念":
            c["aliases"] = ["玛莉娅"]
    seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    md = repo / "data" / "extractions" / "v3_wiki" / "concepts" / "死概念.md"
    md.write_text(md.read_text(encoding="utf-8") + "**别名:** 玛莉娅\n", encoding="utf-8")
    diff = _git(["diff", "HEAD"], repo)
    res = domain_checks.check_bridge(diff, str(repo))
    assert res.verdict == DOMAIN_PASS
    assert "入度" in res.reason


def test_bridge_fail_no_in_degree_change(tmp_path):
    """改动未建立任何引用（改前 0 → 改后仍 0）→ FAIL 附改前/改后证据。"""
    repo = _init_bridge_repo(tmp_path, [
        _concept("死概念"), _concept("活概念"),
    ])
    md = repo / "data" / "extractions" / "v3_wiki" / "concepts" / "死概念.md"
    md.write_text(md.read_text(encoding="utf-8") + "补充无关描述\n", encoding="utf-8")
    diff = _git(["diff", "HEAD"], repo)
    res = domain_checks.check_bridge(diff, str(repo))
    assert res.verdict == DOMAIN_FAIL
    assert "死概念" in res.reason and "入度无变化" in res.reason


def test_bridge_skip_no_affected_entity(tmp_path):
    """diff 未命中 v3_wiki/concepts 实体（只改文档）→ SKIP（无法判定不推断）。"""
    repo = _init_bridge_repo(tmp_path, [
        _concept("死概念"), _concept("活概念"),
    ])
    _write(repo, "README.md", "hello v2\n")
    _git(["add", "README.md"], repo)
    diff = _git(["diff", "HEAD"], repo)
    res = domain_checks.check_bridge(diff, str(repo))
    assert res.verdict == DOMAIN_SKIP


def test_bridge_skip_not_git_repo(tmp_path):
    """data_dir 非 git 仓库（无改前基线）→ SKIP（诚实不推断）。"""
    data_dir = _make_data_dir(tmp_path, concepts=[_concept("死概念")])
    diff = (
        "diff --git a/data/extractions/v3_wiki/concepts/死概念.md "
        "b/data/extractions/v3_wiki/concepts/死概念.md\n"
        "index 111..222 100644\n"
        "--- a/data/extractions/v3_wiki/concepts/死概念.md\n"
        "+++ b/data/extractions/v3_wiki/concepts/死概念.md\n"
        "@@ -1 +1 @@\n-死概念\n+死概念v2\n"
    )
    res = domain_checks.check_bridge(diff, str(data_dir))
    assert res.verdict == DOMAIN_SKIP