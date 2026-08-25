"""评测运行器测试：Fake run_issue_agent + 假 must_pass 判定。"""
import json
from pathlib import Path

import pytest

import benchmark.runner as runner_mod
from agent.llm import LLMMessage, MockLLMClient
from benchmark.loader import BenchmarkCase
from benchmark.report import BenchmarkReport
from tools.github_tools import GitHubIssue
from tools.shell_tools import TestResult


def _case(case_id="schedule-646", must_pass=None):
    return BenchmarkCase(
        id=case_id, category="bug", repository="dbader/schedule",
        issue=GitHubIssue(number=646, title="guard self.unit against None",
                          body="crash when unit is None", labels=[], state="open"),
        gold_patch="gold/%s.diff" % case_id, must_pass=must_pass or ["test_ok.py"],
        max_iterations=30, notes="")


def _make_gold(tmp_path, case_id="schedule-646"):
    """在 case_dir 下创建 gold patch 文件，返回 case_dir。"""
    g = tmp_path / "gold"
    g.mkdir(parents=True, exist_ok=True)
    (g / ("%s.diff" % case_id)).write_text("+guard", encoding="utf-8")
    return str(tmp_path)


def _result_diff(diff="+guard"):
    return type("R", (), {
        "diff": diff, "iteration_count": 12, "stopped_by_limit": False,
        "steps": []})()


def test_resolved_case(tmp_path, monkeypatch):
    """测试全绿 + Judge PASS → resolved。"""
    out = {}
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: out.update(task=task) or _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    cases = [_case()]
    report = runner_mod.run_benchmark_cases(
        llm, cases, _make_gold(tmp_path), "", "local", tmp_path)
    assert isinstance(report, BenchmarkReport)
    assert report.total == 1 and report.resolved == 1
    r = report.results[0]
    assert r.status == "resolved" and r.resolution is True
    assert r.test_pass is True and r.patch_acceptance is True
    assert r.judge_verdict == "PASS"
    assert out["task"].push is False
    assert out["task"].issue_snapshot is not None


def test_test_failure_not_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 1, 0, 2, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.resolved == 0
    assert report.results[0].test_pass is False


def test_judge_skip_when_diff_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff(""))
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.results[0].patch_acceptance is False
    assert report.results[0].judge_verdict == "SKIP"


def test_test_pass_wraps_run_tests_in_sandbox(monkeypatch):
    """docker 模式下判定阶段 run_tests 必须包在 sandbox_executor 上下文内（W3 沙箱机制）。"""
    import contextlib

    inside: list[bool] = []
    calls: list[tuple[str, str, bool]] = []

    @contextlib.contextmanager
    def fake_sandbox_executor(repo_dir):
        inside.append(True)
        try:
            yield None
        finally:
            inside.pop()

    def fake_run_tests(path, workspace_root):
        calls.append((path, workspace_root, bool(inside)))
        return TestResult(1, 0, 0, 1, 0.1, [])

    monkeypatch.setattr(runner_mod, "sandbox_executor", fake_sandbox_executor,
                        raising=False)
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    case = _case(must_pass=["test_a.py", "test_b.py"])
    assert runner_mod._test_pass(case, "/repo") is True
    assert calls == [("test_a.py", "/repo", True), ("test_b.py", "/repo", True)]


def test_test_pass_verdicts_and_local_mode_unchanged(monkeypatch):
    """判定语义 + local 模式（默认）行为不变：判定始终经 sandbox_executor 包装。"""
    import contextlib

    sandbox_calls: list[str] = []
    monkeypatch.setattr(runner_mod, "sandbox_executor",
                        lambda repo_dir: contextlib.nullcontext(
                            sandbox_calls.append(repo_dir)),
                        raising=False)

    ok = TestResult(1, 0, 0, 1, 0.1, [])
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: ok)
    assert runner_mod._test_pass(_case(must_pass=["a.py", "b.py"]), "/repo") is True

    failed = TestResult(1, 2, 0, 3, 0.1, [])
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: failed)
    assert runner_mod._test_pass(_case(), "/repo") is False

    errored = TestResult(0, 0, 1, 1, 0.1, [])
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: errored)
    assert runner_mod._test_pass(_case(), "/repo") is False

    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: "boom")
    assert runner_mod._test_pass(_case(), "/repo") is False

    assert sandbox_calls == ["/repo"] * 4


def test_case_error_continues(tmp_path, monkeypatch):
    """单 case 异常 → status=error，其余继续。"""
    calls = []

    def fake_run(task, llm, **kw):
        if task.issue_number == 1:
            raise RuntimeError("clone 失败")
        return _result_diff()

    monkeypatch.setattr(runner_mod, "run_issue_agent", fake_run)
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。"),
                         LLMMessage(role="assistant", content="PASS 一致。")])
    cases = [_case("c1").__class__(
        id="c1", category="bug", repository="r/a",
        issue=GitHubIssue(number=1, title="t", body="b", labels=[], state="open"),
        gold_patch="g", must_pass=["t.py"], max_iterations=30, notes=""),
        _case("c2")]
    report = runner_mod.run_benchmark_cases(
        llm, cases, _make_gold(tmp_path, "c2"), "", "local", tmp_path)
    assert len(report.results) == 2
    by_id = {r.case_id: r for r in report.results}
    assert by_id["c1"].status == "error" and "clone 失败" in by_id["c1"].errors
    assert by_id["c2"].status == "resolved"