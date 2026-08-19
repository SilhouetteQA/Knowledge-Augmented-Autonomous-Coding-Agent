"""GitHub 工具测试：Fake gh（monkeypatch _run），不触网。"""
import json
import subprocess
from pathlib import Path

from tools import github_tools
from tools.file_tools import ToolError
from tools.github_tools import (
    GitHubIssue,
    GitHubRepo,
    clone_repository,
    comment_issue,
    commit_changes,
    create_pull_request,
    create_branch,
    get_issue,
    get_repository,
    git_diff_since,
    push_branch,
)


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


def _init_repo(tmp_path) -> str:
    """初始化本地 git 仓库（不触网），返回路径。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(["init", "-b", "main"], repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git_run(["add", "-A"], repo)
    _git_run(["-c", "user.email=t@t", "-c", "user.name=t",
              "commit", "-m", "init"], repo)
    return str(repo)


def _git_run(args, cwd) -> str:
    """git 子进程封装（UTF-8 解码，规避 Windows GBK 乱码）。"""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout


def test_create_branch_and_commit(tmp_path):
    repo = _init_repo(tmp_path)
    assert create_branch(repo, "fix/issue-1", "main") is None
    out = _git_run(["branch", "--show-current"], repo)
    assert out.strip() == "fix/issue-1"
    (Path(repo) / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert commit_changes(repo, "fix: 修复 #1 x") is None
    out = _git_run(["log", "--oneline"], repo)
    assert "fix: 修复 #1 x" in out


def test_git_diff_since(tmp_path):
    repo = _init_repo(tmp_path)
    create_branch(repo, "fix/issue-1", "main")
    (Path(repo) / "a.py").write_text("x = 2\n", encoding="utf-8")
    diff = git_diff_since(repo, "main")
    assert isinstance(diff, str)
    assert "-x = 1" in diff and "+x = 2" in diff


def test_git_diff_since_failure(tmp_path):
    repo = _init_repo(tmp_path)
    assert isinstance(git_diff_since(repo, "nope-branch"), ToolError)


def test_clone_repository_passes_through(monkeypatch):
    calls = []
    monkeypatch.setattr(github_tools, "_run",
                        lambda cmd, **kw: calls.append(cmd) or "")
    assert clone_repository("C:\\tmp\\dst", "test/arc-wiki") is None
    assert calls == [["gh", "repo", "clone", "test/arc-wiki", "C:\\tmp\\dst"]]


def test_push_branch_passes_through(monkeypatch):
    calls = []
    monkeypatch.setattr(github_tools, "_run",
                        lambda cmd, **kw: calls.append(cmd) or "")
    assert push_branch("C:\\tmp\\dst", "fix/issue-1") is None
    assert calls == [["git", "push", "-u", "origin", "fix/issue-1"]]


def test_create_pull_request(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return "https://github.com/test/arc-wiki/pull/999\n"

    monkeypatch.setattr(github_tools, "_run", fake_run)
    url = create_pull_request("test/arc-wiki", "fix/issue-1", "main",
                              "fix: 修复 #1 x", "body text")
    assert url == "https://github.com/test/arc-wiki/pull/999"
    cmd = calls[0]
    assert cmd[:4] == ["gh", "pr", "create", "--repo"]
    assert "--head" in cmd and "fix/issue-1" in cmd
    assert "--base" in cmd and "main" in cmd


def test_create_pull_request_failure(monkeypatch):
    monkeypatch.setattr(github_tools, "_run",
                        lambda *a, **k: ToolError("PR 创建失败"))
    assert isinstance(create_pull_request("a/b", "h", "m", "t", "b"), ToolError)


def test_comment_issue(monkeypatch):
    calls = []
    monkeypatch.setattr(github_tools, "_run",
                        lambda cmd, **kw: calls.append(cmd) or "")
    assert comment_issue("test/arc-wiki", 1, "done") is None
    assert "repos/test/arc-wiki/issues/1/comments" in calls[0]