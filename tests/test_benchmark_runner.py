"""评测运行器测试：Fake run_issue_agent + 假 must_pass 判定。"""
import json
import os
from pathlib import Path

import pytest

import benchmark.runner as runner_mod
from agent.llm import LLMMessage, MockLLMClient
from benchmark.loader import BenchmarkCase
from benchmark.report import BenchmarkReport
from tools.github_tools import GitHubIssue
from tools.shell_tools import CommandResult, TestFailure, TestResult


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


def _result_diff(diff="+guard", steps=None):
    return type("R", (), {
        "diff": diff, "iteration_count": 12, "stopped_by_limit": False,
        "steps": steps or []})()


@pytest.fixture(autouse=True)
def _no_repo_network(monkeypatch):
    """基线前 repo 就绪步骤打桩：单元测试不触发真实 gh/git 网络操作。"""
    monkeypatch.setattr(runner_mod, "_ensure_repository",
                        lambda *args, **kwargs: None)


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
    assert r.test_error_summary == ""          # 全绿无失败摘要
    assert out["task"].approval_dir is None      # 评估模式不产审批单（approval_dir 未配置）
    assert out["task"].issue_snapshot is not None


def test_resolved_case_tool_success_rate(tmp_path, monkeypatch):
    """tool_success_rate 由 AgentStep.result 真实采集：ToolError 计失败、其余计成功。"""
    from agent.loop import AgentStep
    from tools.file_tools import ToolError

    steps = [
        AgentStep(tool_name="read_file", arguments={"path": "a.py"},
                  result="1: x"),
        AgentStep(tool_name="search_code", arguments={"query": "foo"},
                  result=[]),
        AgentStep(tool_name="write_file", arguments={"path": "a.py"},
                  result="written"),
        AgentStep(tool_name="read_file", arguments={"path": "missing.py"},
                  result=ToolError("文件不存在")),
    ]
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff(steps=steps))
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.results[0].tool_success_rate == pytest.approx(0.75)


def test_test_failure_not_resolved(tmp_path, monkeypatch):
    """基线预检通过、Agent 修复后 must_pass 仍失败 → not_resolved（用例问题而非环境问题）。"""
    state = {"baseline": True}

    def fake_run_tests(path, workspace_root):
        # 第一轮是基线预检（须通过才进入 Agent），之后是判定阶段的 must_pass
        if state["baseline"]:
            state["baseline"] = False
            return TestResult(1, 0, 0, 1, 0.1, [])
        return TestResult(1, 1, 0, 2, 0.1, [])

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.resolved == 0
    r = report.results[0]
    assert r.status == "not_resolved" and r.test_pass is False


def test_judge_failure_summary_carried(tmp_path, monkeypatch):
    """判定阶段 must_pass 失败 → test_error_summary 非空且含失败测试名（P1-3）。"""
    state = {"baseline": True}

    def fake_run_tests(path, workspace_root):
        # 第一轮是基线预检（须通过），之后是判定阶段的 must_pass（带 failures 明细）
        if state["baseline"]:
            state["baseline"] = False
            return TestResult(1, 0, 0, 1, 0.1, [])
        return TestResult(1, 1, 0, 2, 0.1, [
            TestFailure(test="tests/test_schedule.py::test_guard_unit_against_none",
                        message="AssertionError: unit is None"),
            TestFailure(test="tests/test_schedule.py::test_guard_negative",
                        message="AssertionError: -1")])

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    r = report.results[0]
    assert r.test_pass is False
    assert r.test_error_summary != ""
    assert "test_guard_unit_against_none" in r.test_error_summary
    assert "test_guard_negative" in r.test_error_summary


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
    assert calls == [(os.path.join("/repo", "test_a.py"), "/repo", True),
                     (os.path.join("/repo", "test_b.py"), "/repo", True)]


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


def test_test_pass_joins_relative_must_pass_to_abs(monkeypatch):
    """相对 must_pass 路径必须拼接 repo_dir 为绝对路径（docker 执行器宿主 CWD 基准解析）。"""
    import contextlib

    seen: list[str] = []
    monkeypatch.setattr(runner_mod, "sandbox_executor",
                        lambda repo_dir: contextlib.nullcontext(),
                        raising=False)
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: (
                            seen.append(path) or TestResult(1, 0, 0, 1, 0.1, [])))
    assert runner_mod._test_pass(_case(must_pass=["test_schedule.py"]),
                                 "/work/repo") is True
    assert seen == [os.path.join("/work/repo", "test_schedule.py")]


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


def test_setup_commands_run_before_baseline_in_order(tmp_path, monkeypatch):
    """setup 命令逐条在 repo_dir 执行（基线预检之前），基线预检与判定阶段各跑一次 must_pass。"""
    events: list[tuple[str, str, str]] = []

    def fake_run_command(command, cwd=None, timeout=60, workspace_root=None):
        events.append(("setup", command, cwd))
        return CommandResult(timeout=False, stdout="", stderr="",
                             exit_code=0, duration=0.1)

    def fake_run_tests(path, workspace_root):
        events.append(("test", path, workspace_root))
        return TestResult(1, 0, 0, 1, 0.1, [])

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_command", fake_run_command)
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    case = _case()
    case.setup_commands = ["pip install pytz==2024.1", "python -m compileall ."]
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [case], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.results[0].status == "resolved"
    repo_dir = os.path.join(str(tmp_path), "dbader__schedule")
    assert events == [
        ("setup", "pip install pytz==2024.1", repo_dir),
        ("setup", "python -m compileall .", repo_dir),
        ("test", os.path.join(repo_dir, "test_ok.py"), repo_dir),
        ("test", os.path.join(repo_dir, "test_ok.py"), repo_dir),
    ]


def test_setup_failure_marks_case_error_and_skips_must_pass(tmp_path, monkeypatch):
    """setup 任一命令失败 → status=error（errors 含命令与输出），该 case 不跑基线预检与 Agent，其余继续。"""
    test_calls: list[str] = []

    def fake_run_command(command, cwd=None, timeout=60, workspace_root=None):
        if command == "pip install pytz==2024.1":
            return CommandResult(timeout=False, stdout="", stderr="pip 安装失败: 超时",
                                 exit_code=1, duration=1.0)
        raise AssertionError("setup 失败后不应执行剩余命令")

    def fake_run_tests(path, workspace_root):
        test_calls.append(path)
        return TestResult(1, 0, 0, 1, 0.1, [])

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_command", fake_run_command)
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    case = _case("c1")
    case.setup_commands = ["pip install pytz==2024.1", "python -m compileall ."]
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。"),
                         LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [case, _case("c2")], _make_gold(tmp_path, "c2"), "", "local", tmp_path)
    by_id = {r.case_id: r for r in report.results}
    r1 = by_id["c1"]
    assert r1.status == "error" and r1.test_pass is False and r1.resolution is False
    assert any("pip install pytz==2024.1" in e and "pip 安装失败" in e
               for e in r1.errors)
    assert len(test_calls) == 2          # 仅 c2 跑 must_pass：基线预检 + 判定阶段各一次
    assert by_id["c2"].status == "resolved"


def test_baseline_failure_marks_environment_error_and_skips_agent(tmp_path, monkeypatch):
    """基线预检失败 → status=environment_error、resolution=False、跳过 Agent 与判定。"""
    agent_calls: list[int] = []
    test_paths: list[str] = []

    def fake_run_tests(path, workspace_root):
        test_paths.append(path)
        return TestResult(0, 0, 1, 1, 0.1, [])     # error=1 → 基线失败（环境缺口）

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: agent_calls.append(task.issue_number)
                        or _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    llm = MockLLMClient([])     # 无消息：若走到判定（judge_patch 调 chat）将失败
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    assert agent_calls == []                       # run_issue_agent 未被调用
    r = report.results[0]
    assert r.status == "environment_error"
    assert r.resolution is False and r.test_pass is False
    assert r.patch_acceptance is False and r.judge_verdict == "SKIP"
    assert r.diff == ""
    assert len(test_paths) == 1                    # 仅基线预检一次，判定阶段未跑
    assert any("基线测试失败" in e and "test_ok.py" in e for e in r.errors)


def test_baseline_failure_continues_to_next_case(tmp_path, monkeypatch):
    """基线失败的 case 不影响后续 case：环境错误单独标注，其余 case 正常执行。"""
    agent_calls: list[int] = []

    def fake_run_tests(path, workspace_root):
        # r__a 的基线预检失败（错误）；dbader__schedule 基线通过
        if os.path.join("r__a", "test_ok.py") in path:
            return TestResult(0, 1, 0, 1, 0.1, [])     # failed=1 → 基线失败
        return TestResult(1, 0, 0, 1, 0.1, [])

    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: agent_calls.append(task.issue_number)
                        or _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    c1 = _case("c1").__class__(
        id="c1", category="bug", repository="r/a",
        issue=GitHubIssue(number=1, title="t", body="b", labels=[], state="open"),
        gold_patch="gold/c1.diff", must_pass=["test_ok.py"], max_iterations=30,
        notes="")
    report = runner_mod.run_benchmark_cases(
        llm, [c1, _case("c2")], _make_gold(tmp_path, "c2"), "", "local", tmp_path)
    assert agent_calls == [646]                      # 仅 c2 运行了 Agent（issue 646）
    by_id = {r.case_id: r for r in report.results}
    assert by_id["c1"].status == "environment_error" and by_id["c1"].resolution is False
    assert by_id["c2"].status == "resolved"


def test_baseline_runs_before_agent(tmp_path, monkeypatch):
    """执行顺序：repo 就绪 → setup → 基线预检 → Agent → 判定（setup 前置重构）。"""
    events: list[str] = []

    def fake_ensure(repo_root, repo_dir, repository):
        events.append("ensure")

    def fake_run_command(command, cwd=None, timeout=60, workspace_root=None):
        events.append("setup")
        return CommandResult(timeout=False, stdout="", stderr="",
                             exit_code=0, duration=0.1)

    def fake_run_tests(path, workspace_root):
        events.append("test")
        return TestResult(1, 0, 0, 1, 0.1, [])

    monkeypatch.setattr(runner_mod, "_ensure_repository", fake_ensure)
    monkeypatch.setattr(runner_mod, "run_command", fake_run_command)
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: events.append("agent") or _result_diff())
    case = _case()
    case.setup_commands = ["pip install pytz==2024.1"]
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [case], _make_gold(tmp_path), "", "local", tmp_path)
    assert events == ["ensure", "setup", "test", "agent", "test"]
    assert report.results[0].status == "resolved"