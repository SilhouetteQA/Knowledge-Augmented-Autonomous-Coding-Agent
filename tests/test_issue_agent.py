"""GitHub Issue Agent 编排测试：Mock LLM + Fake GitHub（不触网、不真推）。"""
import json

import pytest

import agent.issue as issue_mod
from agent.issue import IssueTask, run_issue_agent
from agent.llm import LLMMessage, MockLLMClient
from tools.file_tools import ToolError
from tools.github_tools import GitHubIssue, GitHubRepo


def _fake_issue():
    return GitHubIssue(number=123, title="修复重复创建实体",
                       body="同一角色在不同章节被识别为不同 Entity。",
                       labels=["bug"], state="open")


def _fake_repo():
    return GitHubRepo(full_name="test/arc-wiki",
                      clone_url="https://github.com/test/arc-wiki.git",
                      default_branch="main", language="Python")


def _patch_github(monkeypatch):
    """替换 issue 模块的 GitHub 访问（不触网、不写磁盘仓库）。"""
    monkeypatch.setattr(issue_mod, "get_issue", lambda repo, n: _fake_issue())
    monkeypatch.setattr(issue_mod, "get_repository", lambda repo: _fake_repo())
    monkeypatch.setattr(issue_mod, "clone_repository", lambda d, r: None)
    monkeypatch.setattr(issue_mod, "create_branch", lambda d, b, base: None)
    monkeypatch.setattr(issue_mod, "git_diff_since",
                        lambda d, base: "+fixed\n-fixed\n")


def _graph_script():
    """run_agent_graph 的 plan + decide 两条 Mock 消息。"""
    return [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
    ]


def _make_workdir(tmp_path) -> str:
    """构造克隆目录（test__arc-wiki）并放入通过测试，保证图内 verify 通过。"""
    workdir = tmp_path / "test__arc-wiki"
    workdir.mkdir(parents=True)
    (workdir / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    return str(tmp_path)


def test_dry_run_full_pipeline(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决了问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path), push=False)
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.issue.number == 123
    assert result.branch == "fix/issue-123"
    assert result.review.startswith("PASS")
    assert result.pr_url is None
    assert commits == []


def test_issue_read_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(issue_mod, "get_issue",
                        lambda repo, n: ToolError("gh 未认证"))
    task = IssueTask(repository="test/arc-wiki", issue_number=1,
                     workspace_root=_make_workdir(tmp_path))
    with pytest.raises(ToolError, match="读取 Issue 失败"):
        run_issue_agent(task, MockLLMClient([]))


def test_dry_run_no_changes_review_fail(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    monkeypatch.setattr(issue_mod, "git_diff_since", lambda d, base: "")
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS ok"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.review == "FAIL 无代码变更"
    assert result.pr_url is None
