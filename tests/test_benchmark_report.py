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


def test_save_markdown_contains_sections(tmp_path):
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=1,
                             resolution_rate=1.0, results=[_result()])
    path = save_markdown(report, str(tmp_path))
    text = Path(path).read_text(encoding="utf-8")
    assert "run-001" in text
    assert "Resolution Rate" in text
    assert "schedule-646" in text
    assert "bug" in text


def test_compare_reports():
    r1 = BenchmarkReport(metadata=_meta("run-001", "aaaa"), total=2, resolved=1,
                         resolution_rate=0.5, results=[_result(), _result(False)])
    r2 = BenchmarkReport(metadata=_meta("run-002", "bbbb"), total=2, resolved=2,
                         resolution_rate=1.0,
                         results=[_result(), _result()])
    out = compare_reports([r1, r2])
    assert "run-001" in out and "run-002" in out
    assert "Resolution Rate" in out and "0.50" in out and "1.00" in out