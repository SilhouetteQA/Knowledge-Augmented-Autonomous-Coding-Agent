"""评测报告生成测试：metadata / JSON / Markdown / compare。"""
import json
from pathlib import Path

from benchmark.report import (CaseResult, RunMetadata, BenchmarkReport,
                              current_metadata, save_json, save_markdown,
                              compare_reports)


def _meta(run_id="run-001", commit="abc1234"):
    return RunMetadata(run_id=run_id, git_commit=commit, branch="feature/w6",
                       model="mimo-v2.5", timestamp="2026-08-25T00:00:00",
                       executor="local", params={"max_iterations": 30})


def _result(resolved=True, judge="PASS"):
    return CaseResult(
        case_id="schedule-646", category="bug",
        status="resolved" if resolved else "not_resolved",
        resolution=resolved, test_pass=True, patch_acceptance=resolved,
        judge_verdict=judge, judge_reason=judge + " 理由", tool_success_rate=0.9,
        iteration_count=12, latency_s=120.5, tokens_prompt=5000,
        tokens_completion=2000, cost_usd=0.05, diff="+guard",
        errors=[])


def _env_error_result(case_id="schedule-647"):
    """基线预检失败的 case（environment_error，未运行 Agent）。"""
    return CaseResult(
        case_id=case_id, category="bug", status="environment_error",
        resolution=False, test_pass=False, patch_acceptance=False,
        judge_verdict="SKIP", judge_reason="SKIP 基线测试失败（环境缺口，未运行 Agent）",
        tool_success_rate=0.0, iteration_count=0, latency_s=1.0,
        tokens_prompt=0, tokens_completion=0, cost_usd=0.0, diff="",
        errors=["基线测试失败: test_schedule.py: failed=1 error=0 total=1"])


def _error_result(case_id="schedule-648"):
    """执行异常 case（error，Agent 未完成任务）。"""
    return CaseResult(
        case_id=case_id, category="bug", status="error",
        resolution=False, test_pass=False, patch_acceptance=False,
        judge_verdict="SKIP", judge_reason="SKIP 执行异常",
        tool_success_rate=0.0, iteration_count=0, latency_s=0.5,
        tokens_prompt=0, tokens_completion=0, cost_usd=0.0, diff="",
        errors=["clone 失败"])


def test_current_metadata():
    m = current_metadata("mimo-v2.5", "docker", max_iterations=30)
    assert m.model == "mimo-v2.5"
    assert m.executor == "docker"
    assert m.params == {"max_iterations": 30}
    assert len(m.run_id) > 0


def test_save_json(tmp_path):
    report = BenchmarkReport(metadata=_meta(), total=2, resolved=1,
                             resolution_rate=0.5,
                             results=[_result(), _result(False, "FAIL")])
    path = save_json(report, str(tmp_path))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["metadata"]["run_id"] == "run-001"
    assert data["resolution_rate"] == 0.5
    assert data["results"][0]["case_id"] == "schedule-646"


def test_json_includes_test_error_summary(tmp_path):
    """JSON 经 asdict 自动携带 test_error_summary 字段（P1-3，无需显式处理）。"""
    r = _result(False, "FAIL")
    r.test_error_summary = "test_schedule.py: failed=2 error=0 total=3 | 失败明细"
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                             resolution_rate=0.0, results=[r])
    data = json.loads(Path(save_json(report, str(tmp_path))).read_text(
        encoding="utf-8"))
    assert data["results"][0]["test_error_summary"] == r.test_error_summary


def test_save_markdown_contains_sections(tmp_path):
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=1,
                             resolution_rate=1.0, results=[_result()])
    path = save_markdown(report, str(tmp_path))
    text = Path(path).read_text(encoding="utf-8")
    assert "run-001" in text
    assert "Resolution Rate" in text
    assert "schedule-646" in text
    assert "bug" in text


def test_markdown_failure_summary_column_truncated(tmp_path):
    """明细表含失败摘要列，超 80 字符截断为前 80 字符 + 省略号。"""
    long_summary = ("tests/test_schedule.py::test_guard_unit - "
                    "AssertionError: unit is None; " * 5)
    r = _result(False, "FAIL")
    r.test_error_summary = long_summary
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                             resolution_rate=0.0, results=[r])
    text = Path(save_markdown(report, str(tmp_path))).read_text(encoding="utf-8")
    assert "失败摘要" in text
    assert long_summary[:80] + "..." in text
    assert long_summary not in text


def test_compare_reports():
    r1 = BenchmarkReport(metadata=_meta("run-001", "aaaa"), total=2, resolved=1,
                         resolution_rate=0.5, results=[_result(), _result(False)])
    r2 = BenchmarkReport(metadata=_meta("run-002", "bbbb"), total=2, resolved=2,
                         resolution_rate=1.0,
                         results=[_result(), _result()])
    out = compare_reports([r1, r2])
    assert "run-001" in out and "run-002" in out
    assert "Resolution Rate" in out and "0.50" in out and "1.00" in out


def test_markdown_marks_environment_error(tmp_path):
    """environment_error 需在核心指标计数、分类统计与明细状态列可见。"""
    report = BenchmarkReport(metadata=_meta(), total=2, resolved=0,
                             resolution_rate=0.0,
                             results=[_env_error_result(), _result(False, "FAIL")])
    text = Path(save_markdown(report, str(tmp_path))).read_text(encoding="utf-8")
    assert "environment_error" in text          # 明细状态列标注
    assert "**环境错误: 1**" in text             # 核心指标计数标注
    assert "| bug | 2 | 0 | 0% | 1 |" in text   # 分类统计含环境错误列


def test_json_carries_adjusted_rate(tmp_path):
    """JSON 经 asdict 自动携带 resolution_rate_adjusted（P2-6，无需显式处理）。"""
    results = [_result(), _result(False, "FAIL"), _error_result(), _env_error_result()]
    report = BenchmarkReport(metadata=_meta(), total=4, resolved=1,
                             resolution_rate=0.25, resolution_rate_adjusted=0.5,
                             results=results)
    data = json.loads(Path(save_json(report, str(tmp_path))).read_text(
        encoding="utf-8"))
    assert data["resolution_rate"] == 0.25
    assert data["resolution_rate_adjusted"] == 0.5


def test_markdown_shows_both_rates_with_comment(tmp_path):
    """核心指标同时展示 raw 与 adjusted 两口径，并附中文口径说明（P2-6）。"""
    results = [_result(), _result(False, "FAIL"), _error_result(), _env_error_result()]
    report = BenchmarkReport(metadata=_meta(), total=4, resolved=1,
                             resolution_rate=0.25, resolution_rate_adjusted=0.5,
                             results=results)
    text = Path(save_markdown(report, str(tmp_path))).read_text(encoding="utf-8")
    assert "**Issue Resolution Rate: 25%** (1/4)" in text
    assert "**Adjusted Resolution Rate: 50%** (1/2)" in text
    assert "口径说明" in text


# --- E10：交付一致性核查——明细表「交付一致」列与 JSON 字段 ---


def test_markdown_consistency_column(tmp_path):
    """明细表「交付一致」列：consistency=True → 一致、False → 不一致（B6：✓✗→中文）。"""
    match = _result()
    mismatch = _result(False, "FAIL")
    mismatch.consistency = False
    mismatch.consistency_note = "预期删除 5 个文件，实际删除 3 个文件（实际：old_a.py 等）"
    report = BenchmarkReport(metadata=_meta(), total=2, resolved=1,
                             resolution_rate=0.5, results=[match, mismatch])
    text = Path(save_markdown(report, str(tmp_path))).read_text(encoding="utf-8")
    assert "交付一致" in text                       # 列头
    assert "| 一致 |" in text                       # 相符行
    assert "| 不一致 |" in text                     # 不符行
    assert mismatch.consistency_note not in text    # 备注省略（不展开细节）


def test_json_carries_consistency_fields(tmp_path):
    """JSON 经 asdict 自动携带 consistency / consistency_note（E10，无需显式处理）。"""
    r = _result(False, "FAIL")
    r.consistency = False
    r.consistency_note = "预期删除 5 个文件，实际删除 3 个文件"
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                             resolution_rate=0.0, results=[r])
    data = json.loads(Path(save_json(report, str(tmp_path))).read_text(
        encoding="utf-8"))
    assert data["results"][0]["consistency"] is False
    assert data["results"][0]["consistency_note"] == r.consistency_note