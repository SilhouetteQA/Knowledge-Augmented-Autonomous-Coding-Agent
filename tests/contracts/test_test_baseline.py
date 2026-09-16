"""baseline comparator 测试（Spec 09，FND-REG-001~004）。

实现并验证 Appendix E 的失败指纹算法与回归分类逻辑。这是「测试能力」——
真正的全量比对在 Spec 11（freeze A）执行；本文件证明 comparator 逻辑正确，
并用 fixture 覆盖已知失败 / 意外失败 / 意外 PASS / 指纹漂移等分支。
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from agent_core.contracts.conformance.rules import contract_rule

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "config" / "contracts" / "known-test-baseline.json"

#: Appendix E.2 的三条 Wiki 已知失败（fingerprint anchor = 838ba4c）。
APPENDIX_E_ANCHOR_COMMIT = "838ba4c1440ece174da845f900428075f16cf05b"
APPENDIX_E_RECORDS = [
    {
        "nodeid": "tests/test_stats_collector.py::TestStatsCollectorContent::test_collect_content_reads_db",
        "exception_type": "AttributeError",
        "symbolic_failure_locus": "arknights_wiki.stats.collector._get_raw_data",
        "normalized_error_signature": "story_shape_list_has_no_get",
        "scope": "DEFERRED",
        "fingerprint_sha256": "621b20760ce7091bc179a4fbb9bf15e738e7f11487374bd9d0a240ddee254b35",
    },
    {
        "nodeid": "tests/test_stats_collector.py::TestStatsCollectorSnapshot::test_finish_writes_jsonl_line",
        "exception_type": "AttributeError",
        "symbolic_failure_locus": "arknights_wiki.stats.collector._get_raw_data",
        "normalized_error_signature": "story_shape_list_has_no_get",
        "scope": "DEFERRED",
        "fingerprint_sha256": "bbed5cfa4515e997b3d661e333eec49d9065977518c696deeecbea8abe7b0925",
    },
    {
        "nodeid": "tests/test_stats_collector.py::TestStatsCollectorSnapshot::test_finish_resets_state",
        "exception_type": "AttributeError",
        "symbolic_failure_locus": "arknights_wiki.stats.collector._get_raw_data",
        "normalized_error_signature": "story_shape_list_has_no_get",
        "scope": "DEFERRED",
        "fingerprint_sha256": "b59ab80b45001b131aea3e70af8b0fef0a54da9c21270c310e29a71507d37bdf",
    },
]


def fingerprint_sha256(record: dict, *, baseline_commit: str) -> str:
    """Appendix E.1 规范化指纹：只 hash 六个规范化字段的 canonical JSON。"""
    fields = {
        "nodeid": record["nodeid"],
        "exception_type": record["exception_type"],
        "symbolic_failure_locus": record["symbolic_failure_locus"],
        "normalized_error_signature": record["normalized_error_signature"],
        "scope": record["scope"],
        "baseline_commit": baseline_commit,
    }
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def known_failure_by_nodeid(baseline: dict) -> dict[str, str]:
    """返回 {nodeid: fingerprint_sha256}（from known_failures）。"""
    return {rec["nodeid"]: rec["fingerprint_sha256"] for rec in baseline.get("known_failures", [])}


def classify_failure(nodeid: str, failure: dict, baseline: dict) -> str:
    """把一条当前失败归类到 Appendix E.2 的判定分支。"""
    known = known_failure_by_nodeid(baseline)
    if nodeid in known:
        actual = fingerprint_sha256(
            failure,
            baseline_commit=failure.get("baseline_commit")
            or baseline.get("fingerprint_anchor_commit")
            or baseline.get("baseline_commit"),
        )
        return "ALLOWED_BASELINE_FAILURE" if actual == known[nodeid] else "FINGERPRINT_MISMATCH"
    return "NEW_FAILURE"


def classify_status(nodeid: str, status: str, baseline: dict) -> str:
    """把 nodeid 的当前状态归类（相对 baseline 的 PASS/SKIP/known failure）。"""
    known = known_failure_by_nodeid(baseline)
    passes = set(baseline.get("existing_pass_nodeids", []))
    skips = set(baseline.get("existing_skip_nodeids", []))

    if status == "PASS":
        return "RESOLVED_UNEXPECTEDLY" if nodeid in known else "PASS_OK"
    if status == "SKIP":
        return "SKIP_REGRESSION" if nodeid in passes else "SKIP_OK"
    # status == FAIL
    if nodeid in known:
        return "KNOWN_FAILURE_PRESENT"
    if nodeid in passes:
        return "BASELINE_PASS_NOW_FAIL"
    if nodeid in skips:
        return "BASELINE_SKIP_NOW_FAIL"
    return "NEW_FAILURE"


# --------------------------------------------------------------------------- #
# 指纹算法复现
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rec", APPENDIX_E_RECORDS)
def test_fingerprint_reproduces_appendix_e(rec: dict) -> None:
    """逐字节复现 Appendix E.2 的三条 anchor 指纹。"""
    actual = fingerprint_sha256(rec, baseline_commit=APPENDIX_E_ANCHOR_COMMIT)
    assert actual == rec["fingerprint_sha256"]


def test_fingerprint_is_order_and_whitespace_invariant() -> None:
    """指纹只依赖六个规范化字段，与键顺序、无关字段无关。"""
    rec = {
        "nodeid": "n",
        "exception_type": "e",
        "symbolic_failure_locus": "l",
        "normalized_error_signature": "s",
        "scope": "DEFERRED",
        "extra_field": "ignored",
    }
    a = fingerprint_sha256(rec, baseline_commit="c" * 40)
    b = fingerprint_sha256(dict(reversed(list(rec.items()))), baseline_commit="c" * 40)
    assert a == b


# --------------------------------------------------------------------------- #
# 回归分类逻辑
# --------------------------------------------------------------------------- #


def _baseline(*, known: dict[str, str] | None = None) -> dict:
    return {
        "known_failures": [
            {"nodeid": n, "fingerprint_sha256": fp} for n, fp in (known or {}).items()
        ],
        "existing_pass_nodeids": ["p_pass", "p_fail"],
        "existing_skip_nodeids": ["s_skip", "s_fail"],
        "fingerprint_anchor_commit": "c" * 40,
    }


@contract_rule("FND-REG-002")
def test_known_failure_with_matching_fingerprint_is_allowed() -> None:
    fp = fingerprint_sha256(
        {"nodeid": "k", "exception_type": "e", "symbolic_failure_locus": "l",
         "normalized_error_signature": "s", "scope": "DEFERRED"},
        baseline_commit="c" * 40,
    )
    baseline = _baseline(known={"k": fp})
    result = classify_failure(
        "k",
        {"nodeid": "k", "exception_type": "e", "symbolic_failure_locus": "l",
         "normalized_error_signature": "s", "scope": "DEFERRED", "baseline_commit": "c" * 40},
        baseline,
    )
    assert result == "ALLOWED_BASELINE_FAILURE"


@contract_rule("FND-REG-002")
def test_known_failure_with_mismatched_fingerprint_fails() -> None:
    baseline = _baseline(known={"k": "f" * 64})
    result = classify_failure(
        "k",
        {"nodeid": "k", "exception_type": "e", "symbolic_failure_locus": "l",
         "normalized_error_signature": "DIFFERENT", "scope": "DEFERRED",
         "baseline_commit": "c" * 40},
        baseline,
    )
    assert result == "FINGERPRINT_MISMATCH"


@contract_rule("FND-REG-001")
def test_new_failure_is_rejected() -> None:
    baseline = _baseline()
    result = classify_status("brand_new_nodeid", "FAIL", baseline)
    assert result == "NEW_FAILURE"


@contract_rule("FND-REG-001")
def test_baseline_pass_now_fail_is_rejected() -> None:
    baseline = _baseline()
    assert classify_status("p_fail", "FAIL", baseline) == "BASELINE_PASS_NOW_FAIL"
    assert classify_status("p_fail", "SKIP", baseline) == "SKIP_REGRESSION"


@contract_rule("FND-REG-003")
def test_known_failure_that_passes_is_flagged_for_review() -> None:
    baseline = _baseline(known={"k": "f" * 64})
    assert classify_status("k", "PASS", baseline) == "RESOLVED_UNEXPECTEDLY"
    # 意外 PASS 不当作普通失败，也不静默删除
    assert classify_status("k", "PASS", baseline) != "PASS_OK"


def test_baseline_self_consistency() -> None:
    """本仓 known-test-baseline.json 的 known_failures 指纹与 anchor 自洽。"""
    baseline = load_baseline()
    anchor = baseline.get("fingerprint_anchor_commit") or baseline.get("baseline_commit")
    for rec in baseline.get("known_failures", []):
        recomputed = fingerprint_sha256(rec, baseline_commit=rec.get("fingerprint_anchor_commit") or anchor)
        assert recomputed == rec["fingerprint_sha256"], rec["nodeid"]


# --------------------------------------------------------------------------- #
# 项目层规则落点反向核验（Spec 10）
# --------------------------------------------------------------------------- #


def declared_rule_ids_in_project_tests() -> dict[str, list[str]]:
    """静态扫描本仓 ``tests/contracts/*.py`` 的 ``@contract_rule(...)`` 落点。

    以 AST 解析而非 import：``tests/contracts/`` 没有 ``__init__.py``，不能按包路径
    import；同时避免在比较器测试里重复触发项目模块的导入副作用。
    """
    usages: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "tests" / "contracts").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name != "contract_rule":
                    continue
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        usages.setdefault(arg.value, []).append(f"{path.name}::{node.name}")
    for names in usages.values():
        names.sort()
    return usages


def test_project_scoped_rules_have_real_landings() -> None:
    """payload 里声明的 ``PROJECT_SCOPED_RULES`` 必须在项目测试中真实落点。

    conformance 层无法 import 项目模块，只能声明这些规则"由项目层负责"；本断言在
    项目侧反向核验该声明，避免出现"声明有落点、实际无人落点"的假闭环。

    本测试不绑定 Rule ID：它是 traceability 元检查，不对应某条独立规范规则。
    """
    from agent_core.contracts.conformance.test_traceability import PROJECT_SCOPED_RULES

    usages = declared_rule_ids_in_project_tests()
    missing = sorted(rule for rule in PROJECT_SCOPED_RULES if rule not in usages)
    assert not missing, f"payload 声明为项目层落点的规则缺少真实落点：{missing}"
