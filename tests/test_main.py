# tests/test_main.py
"""CLI 测试：参数解析与 main 输出（注入 fake，不触发真实 LLM）"""
import os

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


def test_main_issue_mode_dry_run(monkeypatch, capsys):
    from agent.issue import IssueAgentResult
    from tools.github_tools import GitHubIssue
    fake_issue = GitHubIssue(number=1, title="标题", body="b", labels=[], state="open")
    fake = IssueAgentResult(issue=fake_issue, steps=[], final_answer="ok",
                            branch="fix/issue-1", diff="+x", review="PASS ok",
                            pr_url=None, stopped_by_limit=False,
                            iteration_count=1, verify_rounds=1)
    monkeypatch.setattr("main.run_issue_agent", lambda *a, **k: fake)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["test/arc-wiki#1", "--issue"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix/issue-1" in out
    assert "dry-run" in out


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
