"""GitHub 工具：gh CLI 封装（宿主执行）+ git 写操作。

凭据边界：gh 与 git 写操作全部在宿主执行（凭据不进沙箱容器）。
"""
import json
import subprocess
from dataclasses import dataclass

from tools.file_tools import ToolError


@dataclass
class GitHubIssue:
    """GitHub Issue 摘要。"""
    number: int
    title: str
    body: str
    labels: list[str]
    state: str


@dataclass
class GitHubRepo:
    """GitHub 仓库元信息。"""
    full_name: str
    clone_url: str
    default_branch: str
    language: str | None


def run_host(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> str | ToolError:
    """宿主只读命令执行器（公开 API）：统一编码/超时/ToolError 语义。"""
    return _run(cmd, cwd=cwd, timeout=timeout)


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> str | ToolError:
    """宿主执行命令返回 stdout；失败返回 ToolError（含 stderr 原文）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        return ToolError(f"命令不可用: {cmd[0]}（请安装并加入 PATH）")
    except subprocess.TimeoutExpired:
        return ToolError(f"命令超时（{timeout}s）: {' '.join(cmd[:2])}")
    if proc.returncode != 0:
        return ToolError(
            f"命令失败: {' '.join(cmd[:4])}\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _gh_json(args: list[str]) -> object | ToolError:
    """执行 gh api 并解析 JSON 输出。"""
    out = _run(["gh", "api", *args])
    if isinstance(out, ToolError):
        return out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return ToolError(f"gh 输出解析失败: {out[:200]}")


def get_issue(repo: str, number: int) -> GitHubIssue | ToolError:
    """读取仓库 issue（gh api repos/{repo}/issues/{number}）。"""
    data = _gh_json([f"repos/{repo}/issues/{number}"])
    if isinstance(data, ToolError):
        return data
    return GitHubIssue(
        number=data["number"], title=data["title"], body=data.get("body") or "",
        labels=[l["name"] for l in data.get("labels", [])], state=data["state"],
    )


def get_repository(repo: str) -> GitHubRepo | ToolError:
    """读取仓库元信息（gh api repos/{repo}）。"""
    data = _gh_json([f"repos/{repo}"])
    if isinstance(data, ToolError):
        return data
    return GitHubRepo(
        full_name=data["full_name"], clone_url=data["clone_url"],
        default_branch=data["default_branch"], language=data.get("language"),
    )


def clone_repository(repo_dir: str, repo: str) -> ToolError | None:
    """宿主 gh repo clone（认证经 gh，https 通道）。"""
    result = _run(["gh", "repo", "clone", repo, repo_dir], timeout=600)
    if isinstance(result, ToolError):
        return result
    return None


def create_branch(repo_dir: str, branch: str, base: str) -> ToolError | None:
    """从 base 检出并创建分支（-B 幂等：重复运行时重置同名分支而非报错）。"""
    r1 = _run(["git", "checkout", base], cwd=repo_dir)
    if isinstance(r1, ToolError):
        return r1
    r2 = _run(["git", "checkout", "-B", branch], cwd=repo_dir)
    if isinstance(r2, ToolError):
        return r2
    return None


def sync_repository(repo_dir: str, base: str) -> ToolError | None:
    """已存在仓库：fetch 远端并把 base 重置到远端状态（丢弃残留未提交变更）。"""
    r1 = _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=600)
    if isinstance(r1, ToolError):
        return r1
    r2 = _run(["git", "checkout", base], cwd=repo_dir)
    if isinstance(r2, ToolError):
        return r2
    r3 = _run(["git", "reset", "--hard", f"origin/{base}"], cwd=repo_dir)
    if isinstance(r3, ToolError):
        return r3
    r4 = _run(["git", "clean", "-fd"], cwd=repo_dir)
    if isinstance(r4, ToolError):
        return r4
    return None


def git_diff_since(repo_dir: str, base: str) -> str | ToolError:
    """工作区相对 base 的未提交变更 diff（宿主执行）。"""
    return _run(["git", "diff", base], cwd=repo_dir)


def commit_changes(repo_dir: str, message: str) -> ToolError | None:
    """宿主 git add -A + commit。"""
    r1 = _run(["git", "add", "-A"], cwd=repo_dir)
    if isinstance(r1, ToolError):
        return r1
    r2 = _run(["git", "commit", "-m", message], cwd=repo_dir)
    if isinstance(r2, ToolError):
        return r2
    return None


def push_branch(repo_dir: str, branch: str) -> ToolError | None:
    """宿主 git push -u origin。"""
    r = _run(["git", "push", "-u", "origin", branch], cwd=repo_dir, timeout=600)
    if isinstance(r, ToolError):
        return r
    return None


def create_pull_request(repo: str, head: str, base: str, title: str,
                        body: str) -> str | ToolError:
    """gh pr create → 返回 PR URL。"""
    out = _run(
        ["gh", "pr", "create", "--repo", repo, "--head", head, "--base", base,
         "--title", title, "--body", body],
        timeout=300,
    )
    if isinstance(out, ToolError):
        return out
    return out.strip()


def comment_issue(repo: str, number: int, body: str) -> ToolError | None:
    """gh api POST 评论（演示辅助）。"""
    r = _run(["gh", "api", f"repos/{repo}/issues/{number}/comments",
              "--method", "POST", "-f", f"body={body}"])
    if isinstance(r, ToolError):
        return r
    return None