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
    sync_repository,
    worktree_full_diff,
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


def test_worktree_full_diff_includes_untracked(tmp_path):
    """CR-1：完整 diff = git diff + 未跟踪新文件伪 diff（内容内联可审查）。"""
    repo = _init_repo(tmp_path)
    create_branch(repo, "fix/issue-1", "main")
    (Path(repo) / "a.py").write_text("x = 2\n", encoding="utf-8")
    (Path(repo) / "new_mod.py").write_text("agent_wrote = True\n", encoding="utf-8")
    diff = worktree_full_diff(repo, "main")
    assert isinstance(diff, str)
    assert "-x = 1" in diff and "+x = 2" in diff           # tracked 变更保留
    assert "diff --git a/new_mod.py b/new_mod.py" in diff   # untracked 入 diff
    assert "new file mode" in diff
    assert "+agent_wrote = True" in diff


def test_worktree_full_diff_untracked_binary_hashed(tmp_path):
    """二进制 untracked：不内联内容，以 sha256 指纹行占位（内容变化仍改变指纹）。"""
    repo = _init_repo(tmp_path)
    (Path(repo) / "blob.bin").write_bytes(b"ab\x00cd")
    diff = worktree_full_diff(repo, "main")
    assert "blob.bin" in diff and "sha256=" in diff


def test_worktree_full_diff_untracked_drift_changes_text(tmp_path):
    """untracked 内容变化 → 完整 diff 文本变化（sha256 漂移检查可检出篡改）。"""
    repo = _init_repo(tmp_path)
    p = Path(repo) / "new_mod.py"
    p.write_text("v = 1\n", encoding="utf-8")
    d1 = worktree_full_diff(repo, "main")
    p.write_text("v = 2\n", encoding="utf-8")
    d2 = worktree_full_diff(repo, "main")
    assert d1 != d2


def test_worktree_full_diff_no_untracked_matches_git_diff(tmp_path):
    """无 untracked：与 git diff 输出逐字节一致（既有审批单兼容）。"""
    repo = _init_repo(tmp_path)
    (Path(repo) / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert worktree_full_diff(repo, "main") == git_diff_since(repo, "main")


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


def test_sync_repository_passes_through(monkeypatch):
    calls = []
    monkeypatch.setattr(github_tools, "_run",
                        lambda cmd, **kw: calls.append(cmd) or "")
    assert sync_repository("C:\\tmp\\dst", "main") is None
    assert calls == [
        ["git", "fetch", "origin"],
        ["git", "checkout", "main"],
        ["git", "reset", "--hard", "origin/main"],
        ["git", "clean", "-fd"],
    ]


def test_sync_repository_fetch_and_reset(tmp_path):
    src = _init_repo(tmp_path)
    dst = tmp_path / "clone"
    subprocess.run(["git", "clone", src, str(dst)], check=True,
                   capture_output=True)
    (dst / "a.py").write_text("x = 99\n", encoding="utf-8")
    subprocess.run(["git", "checkout", "-b", "dirty"], cwd=dst,
                   check=True, capture_output=True)
    assert sync_repository(str(dst), "main") is None
    out = subprocess.run(["git", "branch", "--show-current"], cwd=dst,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "main"
    assert (dst / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_create_branch_recreates_existing(tmp_path):
    repo = _init_repo(tmp_path)
    assert create_branch(repo, "fix/issue-1", "main") is None
    # 第二次运行：同名分支已存在，应重置重建而非报错
    assert create_branch(repo, "fix/issue-1", "main") is None
    out = subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "fix/issue-1"