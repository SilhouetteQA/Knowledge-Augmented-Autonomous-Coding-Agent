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


def _mk_case(case_id, number, repository):
    """构造指定 repo 的 case（issue number 用于区分 Agent 是否抛异常）。"""
    return _case(case_id).__class__(
        id=case_id, category="bug", repository=repository,
        issue=GitHubIssue(number=number, title="t", body="b", labels=[], state="open"),
        gold_patch="gold/%s.diff" % case_id, must_pass=["test_ok.py"],
        max_iterations=30, notes="")


def test_report_computes_both_resolution_rates(tmp_path, monkeypatch):
    """汇总两口径：raw=resolved/total（含 error/env），adjusted 分母排除 error 与 environment_error。"""
    baseline_calls: dict[str, int] = {}

    def fake_run_tests(path, workspace_root):
        p = os.path.normpath(path)
        if "r__b" in p:                        # c2：基线失败 → environment_error
            return TestResult(1, 1, 0, 1, 0.1, [])
        n = baseline_calls.get(p, 0)
        baseline_calls[p] = n + 1
        if n == 0:                             # 第一轮为基线预检，须通过
            return TestResult(1, 0, 0, 1, 0.1, [])
        if "r__d" in p:                        # c4：判定阶段失败 → not_resolved
            return TestResult(1, 1, 0, 1, 0.1, [])
        return TestResult(1, 0, 0, 1, 0.1, []) # c1/c3：判定通过

    def fake_run(task, llm, **kw):
        if task.issue_number == 1:             # c1：Agent 抛异常 → error
            raise RuntimeError("clone 失败")
        return _result_diff()

    g = tmp_path / "gold"
    g.mkdir(parents=True, exist_ok=True)
    for cid in ("c1", "c2", "c3", "c4"):
        (g / ("%s.diff" % cid)).write_text("+guard", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "run_issue_agent", fake_run)
    monkeypatch.setattr(runner_mod, "run_tests", fake_run_tests)
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。"),
                         LLMMessage(role="assistant", content="PASS 一致。")])
    cases = [_mk_case("c1", 1, "r/a"), _mk_case("c2", 2, "r/b"),
             _mk_case("c3", 3, "r/c"), _mk_case("c4", 4, "r/d")]
    report = runner_mod.run_benchmark_cases(
        llm, cases, str(tmp_path), "", "local", tmp_path)
    by_id = {r.case_id: r for r in report.results}
    assert by_id["c1"].status == "error"
    assert by_id["c2"].status == "environment_error"
    assert by_id["c3"].status == "resolved"
    assert by_id["c4"].status == "not_resolved"
    assert report.total == 4 and report.resolved == 1
    # raw = 1/4（分母含 error/env）；adjusted = 1/(4-1-1) = 1/2
    assert report.resolution_rate == pytest.approx(0.25)
    assert report.resolution_rate_adjusted == pytest.approx(0.5)


def test_adjusted_rate_zero_denominator(tmp_path, monkeypatch):
    """全部 case 为 error（分母为 0）→ adjusted=0.0，不崩溃。"""
    def boom(task, llm, **kw):
        raise RuntimeError("全挂")

    monkeypatch.setattr(runner_mod, "run_issue_agent", boom)
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([])
    report = runner_mod.run_benchmark_cases(
        llm, [_case("c1"), _case("c2")], _make_gold(tmp_path), "", "local", tmp_path)
    assert report.total == 2 and report.resolved == 0
    assert report.resolution_rate == 0.0
    assert report.resolution_rate_adjusted == 0.0


# --- E8: case 级 evaluation.case span（case_id/category metadata，P2-8） ---


def _probe_runner_module(monkeypatch, fake_traced):
    """以独立模块名重放 benchmark/runner.py 顶层，捕获模块级 traced 装饰注册。

    runner.py 的 _run_one_case 是模块级装饰（import 时已执行），无法在
    runner_mod 上直接断言注册；重放源文件使 @traced 经 monkeypatch 的
    tools.tracing.traced 执行（沿用 test_issue_agent.py 的 probe 模式）。
    """
    import importlib.util
    import pathlib
    import sys
    import tools.tracing as tracing
    monkeypatch.setattr(tracing, "traced", fake_traced)
    src = pathlib.Path(__file__).resolve().parent.parent / "benchmark" / "runner.py"
    spec = importlib.util.spec_from_file_location("benchmark.runner_probe", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def test_run_one_case_traced_registered_with_case_span(monkeypatch):
    """_run_one_case 模块级注册 traced("evaluation.case") span，且 metadata_fn 已接线。"""
    registry = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        registry.append((name, as_type, metadata_fn))
        return lambda f: f

    mod = _probe_runner_module(monkeypatch, fake_traced)
    assert mod._run_one_case is not None
    regs = [(n, t, m) for n, t, m in registry if n == "evaluation.case"]
    assert len(regs) == 1
    _, as_type, metadata_fn = regs[0]
    assert as_type == "span"
    assert metadata_fn is not None


def test_evaluation_case_metadata_extracts_case_id_category_status():
    """_case_metadata 从 args/result 提取 case_id、category 与 status。"""
    from benchmark.runner import _case_metadata
    case = _case("schedule-646")
    result = type("R", (), {"status": "resolved"})()
    assert _case_metadata((case,), {}, result) == {
        "case_id": "schedule-646", "category": "bug", "status": "resolved"}
    # 结果无 status 属性时仅返回 case_id/category（旧对象直通不报错）
    assert _case_metadata((case,), {}, object()) == {
        "case_id": "schedule-646", "category": "bug"}


def test_run_one_case_under_span_wrapper_still_runs(tmp_path, monkeypatch):
    """evaluation.case span 包装（fake traced 开启态）下 _run_one_case 正常执行并返回。"""
    calls: list[str] = []
    metas: list[dict] = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        def deco(func):
            import functools

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                calls.append(name)
                result = func(*args, **kwargs)
                if metadata_fn is not None:
                    metas.append(metadata_fn(args, kwargs, result))
                return result
            return wrapper
        return deco

    mod = _probe_runner_module(monkeypatch, fake_traced)
    monkeypatch.setattr(mod, "_ensure_repository", lambda *a, **k: None)
    monkeypatch.setattr(mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    result = mod._run_one_case(_case(), llm, _make_gold(tmp_path), str(tmp_path))
    assert result.status == "resolved" and result.resolution is True
    assert calls == ["evaluation.case"]
    assert metas == [{"case_id": "schedule-646", "category": "bug",
                      "status": "resolved"}]


# --- E9：域案例判定模型——runner 经 domain_check 走规则检查器（替代 LLM Judge） ---

DELETION_DIFF = (
    "diff --git a/data/extractions/v3_wiki/concepts/死概念.md "
    "b/data/extractions/v3_wiki/concepts/死概念.md\n"
    "deleted file mode 100644\n"
    "--- a/data/extractions/v3_wiki/concepts/死概念.md\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n-# 死概念\n-定义无。\n"
)


# --- E10：交付一致性核查——expected_actions 与 diff 实际删除文件数核对 ---

DELETIONS_3_DIFF = (
    "diff --git a/old_a.py b/old_a.py\n"
    "deleted file mode 100644\n"
    "--- a/old_a.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-old\n"
    "diff --git a/old_b.py b/old_b.py\n"
    "deleted file mode 100644\n"
    "--- a/old_b.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-old\n"
    "diff --git a/old_c.py b/old_c.py\n"
    "deleted file mode 100644\n"
    "--- a/old_c.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-old\n"
)


def _expected_actions_case(expected):
    """构造配了 expected_actions 的 case（仅测核查接线，不走域判定）。"""
    c = _case("schedule-646")
    c.expected_actions = expected
    return c


def test_consistency_match_expected_deletions(tmp_path, monkeypatch):
    """expected_actions.deletions 与 diff 实际删除数相符 → consistency=True、note 空。"""
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff(diff=DELETION_DIFF))
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_expected_actions_case({"deletions": 1})],
        _make_gold(tmp_path), "", "local", tmp_path)
    r = report.results[0]
    assert r.consistency is True
    assert r.consistency_note == ""


def test_consistency_mismatch_notes_detail_and_resolution_unchanged(tmp_path, monkeypatch):
    """预期 5 实际 3 → consistency=False、note 含差异（预期/实际 + 实际删除文件前几条）；
    resolution 仅由 test_pass ∧ patch_acceptance 决定——核查不改变 resolution。"""
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff(diff=DELETIONS_3_DIFF))
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_expected_actions_case({"deletions": 5})],
        _make_gold(tmp_path), "", "local", tmp_path)
    r = report.results[0]
    assert r.consistency is False
    assert "预期删除 5 个文件" in r.consistency_note
    assert "实际删除 3 个文件" in r.consistency_note
    assert "old_a.py" in r.consistency_note and "old_c.py" in r.consistency_note
    # 既有判定不受影响：测试全绿 + Judge PASS → resolution 仍为 True
    assert r.test_pass is True and r.patch_acceptance is True
    assert r.resolution is True and r.status == "resolved"


def test_consistency_unconfigured_true_and_note_empty(tmp_path, monkeypatch):
    """未配 expected_actions → consistency=True、note 空（不干扰既有判定）。"""
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "local", tmp_path)
    r = report.results[0]
    assert r.consistency is True
    assert r.consistency_note == ""
    assert r.status == "resolved"

def test_run_one_case_relative_repo_root_setup_fail(tmp_path, monkeypatch):
    """B13 探针暴露：相对 repo_root 时 setup 的 cwd/workspace 不得拼接嵌套路径。"""
    from benchmark import runner as rm
    from benchmark.loader import BenchmarkCase
    from tools.github_tools import GitHubIssue

    monkeypatch.setattr(rm, "_ensure_repository", lambda *a, **k: None)
    (tmp_path / "workspace" / "local__probe").mkdir(parents=True)
    case = BenchmarkCase(
        id="p", category="bug", repository="local/probe",
        issue=GitHubIssue(number=1, title="t", body="", labels=[], state="open"),
        gold_patch="", must_pass=["x.py"],
        setup_commands=['python -c "import sys; sys.exit(2)"'])
    monkeypatch.chdir(tmp_path)
    r = rm._run_one_case(case, MockLLMClient([]), ".", "workspace")   # repo_root 相对
    assert r.status == "error"
    assert any("exit=2" in e for e in r.errors)   # 命令真执行失败，而非 cd 不存在目录


def test_sandbox_entered_only_after_repository_is_ready(tmp_path, monkeypatch):
    """A6 回归钉子：进入沙箱**之前** repo 必须已就绪。

    `DockerExecutor.create()` 要求 `workspace_root` 已存在（tools/docker_sandbox.py），
    而负责创建并克隆它的是 `_ensure_repository`。一旦两者顺序颠倒，"全新 workspace
    + docker 执行器" 的每个 case 都会以「沙箱工作区不存在」失败 —— 而且**只有复用旧
    workspace 时才偶然通过**，所以用 local 执行器（无此前置检查）或预置 workspace 的
    单测都发现不了。本测试用「与 DockerExecutor.create() 同款前置检查」的替身
    `sandbox_executor` 把它钉住：旧顺序下替身会直接断言失败。
    """
    from contextlib import contextmanager

    entered: list[str] = []
    workspace_existed_at_entry: list[bool] = []

    def fake_ensure_repository(repo_root, repo_dir, repository):
        # 真实实现的职责：创建 workspace（随后才由 gh/git 填充）
        os.makedirs(repo_dir, exist_ok=True)

    @contextmanager
    def fake_sandbox_executor(workspace_root):
        # 只记录观测值，不在 with 内抛错：_run_one_case 的兜底 except 会把异常
        # 收敛成 error 结果，断言放在运行之后才能给出准确诊断。
        workspace_existed_at_entry.append(os.path.isdir(str(workspace_root)))
        entered.append(str(workspace_root))
        yield None

    monkeypatch.setattr(runner_mod, "_ensure_repository", fake_ensure_repository)
    monkeypatch.setattr(runner_mod, "sandbox_executor", fake_sandbox_executor)
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: _result_diff())
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])

    report = runner_mod.run_benchmark_cases(
        llm, [_case()], _make_gold(tmp_path), "", "docker", tmp_path)

    assert entered, "docker 执行器下沙箱必须被进入"
    assert all(workspace_existed_at_entry), (
        "沙箱在 workspace 就绪之前被进入：docker 执行器下会以「沙箱工作区不存在」"
        "失败（_ensure_repository 必须先于 with 执行）"
    )
    assert report.results[0].status == "resolved"
