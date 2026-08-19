"""GitHub 工具测试：Fake gh（monkeypatch _run），不触网。"""
import json

from tools import github_tools
from tools.file_tools import ToolError
from tools.github_tools import GitHubIssue, GitHubRepo, get_issue, get_repository


def test_get_issue_parses(monkeypatch):
    payload = {
        "number": 123, "title": "修复重复实体", "body": "body text",
        "labels": [{"name": "bug"}], "state": "open",
    }

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"]
        assert cmd[2] == "repos/test/arc-wiki/issues/123"
        return json.dumps(payload)

    monkeypatch.setattr(github_tools, "_run", fake_run)
    issue = get_issue("test/arc-wiki", 123)
    assert isinstance(issue, GitHubIssue)
    assert issue.number == 123
    assert issue.title == "修复重复实体"
    assert issue.labels == ["bug"]


def test_get_issue_failure_returns_toolerror(monkeypatch):
    monkeypatch.setattr(github_tools, "_run",
                        lambda *a, **k: ToolError("gh 未认证: 请运行 gh auth login"))
    result = get_issue("test/arc-wiki", 1)
    assert isinstance(result, ToolError)


def test_get_repository_parses(monkeypatch):
    payload = {
        "full_name": "test/arc-wiki",
        "clone_url": "https://github.com/test/arc-wiki.git",
        "default_branch": "main", "language": "Python",
    }
    monkeypatch.setattr(github_tools, "_run",
                        lambda cmd, **kwargs: json.dumps(payload))
    repo = get_repository("test/arc-wiki")
    assert isinstance(repo, GitHubRepo)
    assert repo.default_branch == "main"


def test_run_file_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(github_tools.subprocess, "run", fake_run)
    result = github_tools._run(["gh", "api"])
    assert isinstance(result, ToolError)
    assert "不可用" in result.message