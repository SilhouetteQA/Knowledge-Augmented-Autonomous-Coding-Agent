"""benchmark case 加载器测试：schema 校验与错误路径。"""
import json

import pytest

from benchmark.loader import BenchmarkCase, CaseError, load_cases


def _write_case(cases_dir, category, case_id, **overrides):
    data = {
        "id": case_id,
        "category": category,
        "repository": "dbader/schedule",
        "issue": {"number": 646, "title": "[CRASH] guard self.unit against None",
                  "body": "crash when unit is None", "labels": [], "state": "open"},
        "gold_patch": "gold/%s.diff" % case_id,
        "must_pass": ["test_schedule.py"],
        "max_iterations": 30,
    }
    data.update(overrides)
    p = cases_dir / category / (case_id + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.parent.parent / "gold").mkdir(parents=True, exist_ok=True)
    # gold patch 校验要求 cases/gold/<id>.diff 实际存在，测试夹具补写占位内容
    (p.parent.parent / "gold" / ("%s.diff" % case_id)).write_text(
        "+guard", encoding="utf-8")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_load_valid_case(tmp_path):
    _write_case(tmp_path, "bug", "schedule-646")
    cases = load_cases(str(tmp_path))
    assert len(cases) == 1
    c = cases[0]
    assert isinstance(c, BenchmarkCase)
    assert c.id == "schedule-646"
    assert c.category == "bug"
    assert c.repository == "dbader/schedule"
    assert c.issue.number == 646
    assert c.gold_patch.endswith("schedule-646.diff")
    assert c.must_pass == ["test_schedule.py"]
    assert c.max_iterations == 30


def test_load_multiple_categories_sorted(tmp_path):
    for cat, cid in (("feature", "schedule-99"), ("bug", "schedule-646"),
                     ("test", "schedule-602")):
        _write_case(tmp_path, cat, cid)
    cases = load_cases(str(tmp_path))
    assert [c.id for c in cases] == ["schedule-602", "schedule-646", "schedule-99"]


@pytest.mark.parametrize("bad", [
    {"category": "unknown"},     # 非法类别
    {"id": "dup", "issue": {"number": 1, "title": "t", "body": "",    # issue 缺字段
                            "labels": []}},
])
def test_invalid_case_raises(tmp_path, bad):
    # 夹具修正：category 关键字与 _write_case 位置参数冲突，且非法类别必须写在
    # JSON 内（目录名仍为合法类别 bug 才能被扫描到），故先落盘再覆写 JSON。
    invalid_category = bad.pop("category", None)
    _write_case(tmp_path, "bug", "x", **bad)
    if invalid_category is not None:
        p = tmp_path / "bug" / "x.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["category"] = invalid_category
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CaseError, match=""):
        load_cases(str(tmp_path))


def test_labels_as_string_raises(tmp_path):
    # 负向用例（IMP-1）：labels 为字符串不得静默通过——旧校验放行后被
    # list("open") 悄悄拆成 ['o','p','e','n']，逐键校验须以 CaseError 拒绝。
    _write_case(
        tmp_path, "bug", "schedule-646",
        issue={"number": 646, "title": "t", "body": "",
               "labels": "open", "state": "open"})
    with pytest.raises(CaseError, match="labels"):
        load_cases(str(tmp_path))


def test_real_cases_loaded():
    """真实基准案例库可全量加载（纯本地文件校验）。"""
    cases = load_cases("benchmark/cases")
    assert len(cases) == 5
    cats = {c.category for c in cases}
    assert {"bug", "feature", "test", "refactor"} <= cats
    assert all(c.gold_patch.endswith(".diff") for c in cases)