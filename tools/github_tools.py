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