# tests/test_main.py
"""CLI 测试：参数解析与 main 输出（注入 fake，不触发真实 LLM）"""
import os

import pytest

import main
from agent.loop import AgentResult


def test_parser_defaults():
    args = main.build_parser().parse_args(["任务"])
    assert args.max_iterations == 10
    assert args.workspace == "workspace"


def test_main_prints_result(monkeypatch, capsys):
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["测试任务", "--max-iterations", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "测试任务" in out
    assert "搞定" in out


def test_main_executor_docker_sets_env(monkeypatch, capsys):
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    try:
        rc = main.main(["测试任务", "--executor", "docker"])
        assert rc == 0
        assert os.environ["KA_EXECUTOR"] == "docker"
    finally:
        # 恢复环境，避免 KA_EXECUTOR=docker 泄漏影响同进程内其他测试
        os.environ.pop("KA_EXECUTOR", None)


def test_main_graph_flag_uses_graph(monkeypatch, capsys):
    from agent.graph import AgentGraphResult
    from tools.file_tools import ToolError
    fake = AgentGraphResult(plan=["步骤1"], steps=[], final_answer="搞定", iteration_count=1,
                            verify_rounds=0, stopped_by_limit=False, test_results=[])
    monkeypatch.setattr("main.run_agent_graph", lambda *a, **k: fake)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走 loop")))
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    monkeypatch.setattr("main.build_code_graph", lambda *a, **k: None)
    monkeypatch.setattr("main.get_knowledge_client", lambda: ToolError("未启用"))
    monkeypatch.delenv("KA_CODE_INDEX", raising=False)
    rc = main.main(["测试任务", "--graph"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "搞定" in out
    assert "计划" in out


def test_main_prints_emoji_in_arguments_without_crash(monkeypatch, capsys):
    """字符健壮性回归：工具参数含 GBK 无法表示的字符（emoji）时打印不得崩溃。"""
    from agent.loop import AgentStep
    fake_result = AgentResult(
        steps=[AgentStep(tool_name="write_file",
                         arguments={"path": "a.txt", "content": "emoji \U0001f600 test"},
                         result=None)],
        final_answer="搞定", iteration_count=1, stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["测试任务"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "write_file" in out


def test_main_issue_mode_generates_approval(monkeypatch, capsys):
    from agent.issue import IssueAgentResult
    from tools.github_tools import GitHubIssue
    fake_issue = GitHubIssue(number=1, title="标题", body="b", labels=[], state="open")
    fake = IssueAgentResult(issue=fake_issue, steps=[], final_answer="ok",
                            branch="fix/issue-1", diff="+x", review="PASS ok",
                            pr_url=None, stopped_by_limit=False,
                            iteration_count=1, verify_rounds=1,
                            approval_path="output/approvals/x-1-1-20260826-120000.json")
    monkeypatch.setattr("main.run_issue_agent", lambda *a, **k: fake)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["test/arc-wiki#1", "--issue"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix/issue-1" in out
    assert "等待人工审批" in out       # 停等审批提示（替换原 dry-run 文案）


def test_main_issue_mode_bad_format(monkeypatch, capsys):
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["no-issue-format", "--issue"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "格式" in out


def test_main_correct_mode_dry_run(monkeypatch, capsys, tmp_path):
    from agent.correct import AuditReport
    (tmp_path / "data" / "extractions").mkdir(parents=True)
    fake = AuditReport(
        total=100, sample_count=2, stats={"by_kind": {"concept": 60}},
        reliability={"rate": 0.5, "reliable": 1, "unreliable": 1},
        utility={"rate": 0.9, "dead_count": 1, "used": 1},
        dead_list=[{"id": "x", "kind": "concept", "name": "《三山谈》",
                    "origin_file": "f.json"}],
        samples=[], out_dir="")
    monkeypatch.setattr("main.run_audit", lambda *a, **k: fake)
    rc = main.main(["--correct", "--wiki-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "知识抽查完成" in out
    assert "《三山谈》" in out


def test_main_correct_mode_requires_wiki_dir(monkeypatch, capsys):
    monkeypatch.delenv("ARKNIGHTS_WIKI_DIR", raising=False)
    rc = main.main(["--correct"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "兄弟项目目录" in out


def test_benchmark_cli_reads_args(monkeypatch):
    """--benchmark 模式解析并调用 run_benchmark（Fake 不触网）。"""
    import main as main_mod
    captured = {}
    class FakeReport:
        metadata = type("M", (), {"run_id": "r1"})()
        total = 2
        resolved = 1
        resolution_rate = 0.5
        results = []
    monkeypatch.setattr(main_mod, "run_benchmark",
                        lambda llm, case_dir, out_dir, executor, repo_root: (
                            captured.update(case_dir=case_dir, out_dir=out_dir,
                                            executor=executor) or FakeReport()))
    monkeypatch.setattr(main_mod, "load_cases", lambda d: [])
    rc = main_mod._run_benchmark_mode(
        type("A", (), {"benchmark": True, "cases": "benchmark/cases",
                       "benchmark_out": "output/benchmark",
                       "executor": "docker", "compare": None,
                       "task": None, "workspace": "workspace"}), None)
    assert rc == 0
    assert captured["executor"] == "docker"


def test_compare_flag_parses():
    import main as main_mod
    parser = main_mod.build_parser()
    args = parser.parse_args(["--compare", "a,b"])
    assert args.compare == "a,b"


def test_trace_report_cli_reads_args(monkeypatch):
    """--trace-report 模式解析并调用 fetch_trace/save_trace_report（Fake，有效 trace rc=0）。"""
    import main as main_mod
    captured = {}
    class FakeSummary:
        trace_id = "abc123"
        task = "修复 bug"
        total_latency_s = 10.0
        tool_calls = 3
        tokens_prompt = 100
        tokens_completion = 50
        cost_usd = 0.0
        errors = []
        retries = 0
        test_results = []
        steps = [{"name": "tool.search_code", "latency_s": 1.0}]
    monkeypatch.setattr(main_mod, "fetch_trace",
                        lambda tid, **kw: captured.update(tid=tid) or FakeSummary())
    monkeypatch.setattr(main_mod, "save_trace_report",
                        lambda s, out: captured.update(out=out) or "out/report.md")
    rc = main_mod._run_trace_report_mode(
        type("A", (), {"trace_report": "abc123", "trace_out": "output/trace"})())
    assert rc == 0
    assert captured["tid"] == "abc123"


def test_trace_report_empty_steps_returns_1(monkeypatch, capsys):
    """--trace-report 拉取到空 steps（无效/无数据 trace_id）→ 打印「无数据」并 return 1。"""
    import main as main_mod
    class FakeSummary:
        trace_id = "abc123"
        task = "修复 bug"
        total_latency_s = 0.0
        tool_calls = 0
        tokens_prompt = 0
        tokens_completion = 0
        cost_usd = 0.0
        errors = []
        retries = 0
        test_results = []
        steps = []
    monkeypatch.setattr(main_mod, "fetch_trace", lambda tid, **kw: FakeSummary())
    monkeypatch.setattr(main_mod, "save_trace_report",
                        lambda s, out: "out/report.md")
    rc = main_mod._run_trace_report_mode(
        type("A", (), {"trace_report": "abc123", "trace_out": "output/trace"})())
    out = capsys.readouterr().out
    assert rc == 1
    assert "无数据" in out


def test_trace_report_flag_parses():
    import main as main_mod
    parser = main_mod.build_parser()
    args = parser.parse_args(["--trace-report", "abc123", "--trace-out", "x"])
    assert args.trace_report == "abc123" and args.trace_out == "x"


# --- W8: --approve 审批模式 ---


def test_approve_cli_happy_path(monkeypatch, capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="PASS ok", verify_rounds=1, retry_count=0,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    decided = a
    decided.status = "approved"
    decided.pr_url = "https://github.com/test/arc-wiki/pull/9"
    decided.decided_at = "2026-08-26T12:01:00"
    monkeypatch.setattr("main.approve_request",
                        lambda *a2, **k: decided)
    monkeypatch.setattr("main.save_approval", lambda *a2, **k: path)
    rc = main.main(["--approve", path, "--decision", "approve"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已批准并推送" in out
    assert "pull/9" in out


def test_approve_cli_reject(monkeypatch, capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="FAIL 测试未并入套件", verify_rounds=1, retry_count=1,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    decided = a
    decided.status = "rejected"
    decided.decision_comment = "测试未并入现有套件"
    decided.decided_at = "2026-08-26T12:01:00"
    monkeypatch.setattr("main.approve_request", lambda *a2, **k: decided)
    rc = main.main(["--approve", path, "--decision", "reject",
                    "--comment", "测试未并入现有套件"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已拒绝" in out


def test_approve_cli_missing_file(capsys):
    rc = main.main(["--approve", "no-such-approval.json", "--decision", "approve"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "不存在" in out


def test_approve_cli_missing_decision(capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="PASS ok", verify_rounds=1, retry_count=0,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    rc = main.main(["--approve", path])
    out = capsys.readouterr().out
    assert rc == 1
    assert "缺少审批决定" in out


def test_push_flag_removed():
    """--push 已移除：任何绕过人工门禁的自动推送入口都不存在。"""
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["--push"])
