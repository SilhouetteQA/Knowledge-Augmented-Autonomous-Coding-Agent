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


def test_setup_commands_default_empty(tmp_path):
    """无 setup_commands 字段 → 默认空列表。"""
    _write_case(tmp_path, "bug", "schedule-646")
    c = load_cases(str(tmp_path))[0]
    assert c.setup_commands == []


def test_setup_commands_loaded(tmp_path):
    """合法字符串列表正常载入。"""
    cmds = ["pip install -r requirements.txt", "python -m compileall ."]
    _write_case(tmp_path, "bug", "schedule-646", setup_commands=cmds)
    c = load_cases(str(tmp_path))[0]
    assert c.setup_commands == cmds


@pytest.mark.parametrize("bad", [
    {"setup_commands": "pip install pytz"},   # 非列表
    {"setup_commands": [None]},               # 元素非 str
    {"setup_commands": ["ok", 42]},           # 部分元素非 str
])
def test_invalid_setup_commands_raises(tmp_path, bad):
    _write_case(tmp_path, "bug", "x", **bad)
    with pytest.raises(CaseError, match="setup_commands"):
        load_cases(str(tmp_path))


def test_invalid_setup_command_element_reports_index(tmp_path):
    """非 str 元素报错须含下标，便于定位。"""
    _write_case(tmp_path, "bug", "schedule-646",
                setup_commands=["pip install pytz", 42])
    with pytest.raises(CaseError, match=r"setup_commands\[1\]"):
        load_cases(str(tmp_path))


def test_real_cases_loaded():
    """真实基准案例库可全量加载（纯本地文件校验）。"""
    cases = load_cases("benchmark/cases")
    assert len(cases) == 5
    cats = {c.category for c in cases}
    assert {"bug", "feature", "test", "refactor"} <= cats
    assert all(c.gold_patch.endswith(".diff") for c in cases)


# --- E7（P2-7）：task_type 校验与按任务类型的默认迭代上限 ---


def _write_case_without_max_iterations(cases_dir, category, case_id, **overrides):
    """先落盘显式 max_iterations 的 case，再移除该键（模拟未配置）。"""
    _write_case(cases_dir, category, case_id, **overrides)
    p = cases_dir / category / (case_id + ".json")
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("max_iterations", None)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_task_type_defaults_to_category(tmp_path):
    """未配置 task_type → 缺省取 category。"""
    _write_case(tmp_path, "bug", "schedule-646")
    c = load_cases(str(tmp_path))[0]
    assert c.task_type == "bug"


def test_task_type_explicit(tmp_path):
    """显式 task_type 独立于 category 载入。"""
    _write_case(tmp_path, "feature", "x", task_type="test")
    c = load_cases(str(tmp_path))[0]
    assert c.task_type == "test"
    assert c.category == "feature"


def test_task_type_invalid_raises(tmp_path):
    """非法 task_type → CaseError（报错含取值与允许集）。"""
    _write_case(tmp_path, "bug", "x", task_type="unknown")
    with pytest.raises(CaseError, match="task_type 'unknown' 非法.*bug"):
        load_cases(str(tmp_path))


@pytest.mark.parametrize("cat,expected", [
    ("bug", 30),              # 修复类：默认 30
    ("feature", 30),          # 功能类：默认 30
    ("test", 20),             # 开放型：默认 20
    ("refactor", 20),         # 开放型：默认 20
    ("domain", 20),           # 开放型：默认 20
])
def test_default_max_iterations_by_task_type(tmp_path, cat, expected):
    """无 max_iterations 时按 task_type 补默认（bug/feature=30，test/refactor/domain=20）。"""
    _write_case_without_max_iterations(tmp_path, cat, "c-" + cat)
    c = load_cases(str(tmp_path))[0]
    assert c.max_iterations == expected
    assert c.task_type == cat
    assert c.category == cat


def test_default_max_iterations_from_task_type_not_category(tmp_path):
    """默认值取 task_type 而非目录 category：category=bug + task_type=test → 20。"""
    _write_case_without_max_iterations(tmp_path, "bug", "x", task_type="test")
    c = load_cases(str(tmp_path))[0]
    assert c.max_iterations == 20
    assert c.task_type == "test"


def test_explicit_max_iterations_wins_over_task_type_default(tmp_path):
    """显式 max_iterations 永远优先，不被 task_type 默认覆盖。"""
    _write_case(tmp_path, "test", "x", task_type="test", max_iterations=15)
    c = load_cases(str(tmp_path))[0]
    assert c.max_iterations == 15
    assert c.task_type == "test"


def test_real_cases_explicit_max_iterations_unaffected():
    """既有 5 个 case 均显式配 max_iterations → 不受新默认值影响；无 task_type → 取 category。"""
    cases = load_cases("benchmark/cases")
    assert all(c.max_iterations == 30 for c in cases)
    assert all(c.task_type == c.category for c in cases)


# --- E9：domain_check 域判定配置（deletions / bridge，缺省无） ---


def test_domain_check_default_empty(tmp_path):
    """未配置 domain_check → 缺省空串（不触发域判定）。"""
    _write_case(tmp_path, "domain", "c-1")
    c = load_cases(str(tmp_path))[0]
    assert c.domain_check == ""


def test_domain_check_valid_values(tmp_path):
    """合法 domain_check（deletions/bridge）正常载入且与类别独立。"""
    _write_case(tmp_path, "domain", "c-del", domain_check="deletions")
    _write_case(tmp_path, "domain", "c-bridge", domain_check="bridge")
    cases = {c.id: c for c in load_cases(str(tmp_path))}
    assert cases["c-del"].domain_check == "deletions"
    assert cases["c-bridge"].domain_check == "bridge"


@pytest.mark.parametrize("bad", ["unknown", 42, None, ["deletions"]])
def test_domain_check_invalid_raises(tmp_path, bad):
    """非法 domain_check → CaseError（报错含取值与允许集）；显式空串与缺省同义不报错。"""
    _write_case(tmp_path, "domain", "x", domain_check=bad)
    with pytest.raises(CaseError, match=r"domain_check '.*' 非法.*deletions"):
        load_cases(str(tmp_path))


# --- E10：expected_actions 交付一致性核查配置（可选 dict，键 deletions 非负 int） ---


def test_expected_actions_default_empty(tmp_path):
    """未配置 expected_actions → 缺省空 dict（不触发一致性核查）。"""
    _write_case(tmp_path, "bug", "schedule-646")
    c = load_cases(str(tmp_path))[0]
    assert c.expected_actions == {}


def test_expected_actions_valid(tmp_path):
    """合法 expected_actions（键 deletions 非负 int；空 dict 亦合法）正常载入。"""
    _write_case(tmp_path, "bug", "c-1", expected_actions={"deletions": 5})
    _write_case(tmp_path, "bug", "c-2", expected_actions={})
    cases = {c.id: c for c in load_cases(str(tmp_path))}
    assert cases["c-1"].expected_actions == {"deletions": 5}
    assert cases["c-2"].expected_actions == {}


@pytest.mark.parametrize("bad", [
    ["deletions"],          # 非 dict（列表）
    "deletions=5",          # 非 dict（字符串）
    {"deletions": -1},      # deletions 为负
    {"deletions": "5"},     # deletions 非 int（字符串）
    {"deletions": 5.0},     # deletions 非 int（float）
    {"deletions": True},    # deletions 为 bool（int 子类，须拒绝）
])
def test_expected_actions_invalid_raises(tmp_path, bad):
    """非法 expected_actions → CaseError（非 dict 或 deletions 非非负 int）。"""
    _write_case(tmp_path, "bug", "x", expected_actions=bad)
    with pytest.raises(CaseError, match="expected_actions"):
        load_cases(str(tmp_path))

def _domain_case_dict() -> dict:
    return {
        "id": "dom-1", "category": "domain", "repository": "o/r",
        "issue": {"number": 4, "title": "清理死数据", "body": "b",
                  "labels": [], "state": "open"},
        "gold_patch": "gold/dom-1.diff",
        "must_pass": ["test_x.py"],
        "domain_check": "deletions",
    }


def test_loader_domain_case_without_gold_file(tmp_path):
    """BM-2：domain case 不要求 gold_patch 文件存在（E9 规则判定器无 gold 可比）。"""
    d = tmp_path / "domain"
    d.mkdir()
    (d / "dom-1.json").write_text(
        json.dumps(_domain_case_dict(), ensure_ascii=False), encoding="utf-8")
    cases = load_cases(str(tmp_path))
    assert len(cases) == 1
    assert cases[0].domain_check == "deletions"


def test_loader_non_domain_case_still_requires_gold(tmp_path):
    """BM-2：非域 case 维持 gold_patch 文件必须存在（不放宽）。"""
    d = tmp_path / "bug"
    d.mkdir()
    case = _domain_case_dict()
    case.update({"id": "bug-1", "category": "bug"})
    case.pop("domain_check")
    (d / "bug-1.json").write_text(
        json.dumps(case, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CaseError, match="gold_patch"):
        load_cases(str(tmp_path))
