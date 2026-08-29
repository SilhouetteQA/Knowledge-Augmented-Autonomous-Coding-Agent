# W5 GitHub Issue Agent 实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 打通 GitHub Issue → PR 全链路（Get Issue → Clone → Branch → Work → Test → Diff → Review → Commit → Push → PR），输入仓库 URL + Issue 编号产出可审查的 PR（dry-run 停在 review）。

**Architecture:** `tools/github_tools.py` 以 `gh` CLI 封装 GitHub 读（issue/repo）写（clone/branch/commit/push/PR，全部宿主执行，凭据不进沙箱）；`agent/issue.py` 编排：前置阶段（fetch issue → clone → branch）→ 复用 `run_agent_graph`（沙箱内工作与测试）→ 后置阶段（diff → LLM Review → 失败重试 1 次 → commit → push → PR）。`main.py` 增加 `--issue` 与 `--push` 开关。

**Tech Stack:** Python 3.12+ / gh CLI（已认证或 GH_TOKEN）/ git / pytest / 现有 LangGraph + Docker 沙箱

## Global Constraints

- 仓库根：`.worktrees/w5-github-issue`；测试命令：`.venv\Scripts\python.exe -m pytest tests/`（工作目录 = 仓库根）
- 中文注释、UTF-8、无 emoji；Conventional Commits 中文提交
- 不写 fallback：gh 不可用/未认证必须返回结构化 `ToolError`
- 既有 79 项测试必须保持全绿（非 docker 项）
- 设计依据：`docs/specs/2026-08-19-w5-github-issue-agent.md`（已批准）
- 安全边界：**gh 与 git 写操作全部宿主执行**（沙箱容器内无凭据）；默认 dry-run（`--push` 才推送与建 PR）

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `tools/github_tools.py` | 新增 | `_run` / `_gh_json` / `get_issue` / `get_repository` / `clone_repository` / `create_branch` / `git_diff_since` / `commit_changes` / `push_branch` / `create_pull_request` / `comment_issue` |
| `agent/issue.py` | 新增 | `IssueTask` / `IssueAgentResult` / `run_issue_agent` 编排 / `_review_diff` |
| `main.py` | 修改 | `--issue` / `--push` 开关与 `_run_issue_mode` |
| `.env.example` | 修改 | W5 配置项说明 |
| `tests/test_github_tools.py` | 新增 | gh 封装单元测试（Fake gh，不触网）+ 本地 git 仓库写操作测试 |
| `tests/test_issue_agent.py` | 新增 | 编排测试（Mock LLM + Fake GitHub） |
| `tests/test_main.py` | 修改 | `--issue` 模式测试 |

---

### Task 1: github_tools 基础与 gh 只读

**Files:**
- Create: `tools/github_tools.py`
- Test: `tests/test_github_tools.py`

**Interfaces:**
- Produces: `GitHubIssue(number, title, body, labels, state)` / `GitHubRepo(full_name, clone_url, default_branch, language)` / `_run(cmd, cwd=None, timeout=120) -> str | ToolError` / `_gh_json(args) -> object | ToolError` / `get_issue(repo, number) -> GitHubIssue | ToolError` / `get_repository(repo) -> GitHubRepo | ToolError`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_github_tools.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: FAIL（ModuleNotFoundError: tools.github_tools）

- [ ] **Step 3: 最小实现**

```python
# tools/github_tools.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/github_tools.py tests/test_github_tools.py
git commit -m "feat(tools): 新增 github_tools，gh CLI 封装（get_issue / get_repository）"
```

---

### Task 2: git 写操作与克隆

**Files:**
- Modify: `tools/github_tools.py`（追加）
- Test: `tests/test_github_tools.py`（追加）

**Interfaces:**
- Produces: `clone_repository(repo_dir, repo) -> ToolError | None` / `create_branch(repo_dir, branch, base) -> ToolError | None` / `git_diff_since(repo_dir, base) -> str | ToolError` / `commit_changes(repo_dir, message) -> ToolError | None` / `push_branch(repo_dir, branch) -> ToolError | None`
- 语义：全部宿主 subprocess 直调（不经 run_command，避免进容器）；clone 走 `gh repo clone`（https + gh 认证通道）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_github_tools.py`）

```python
import subprocess
from pathlib import Path

from tools.github_tools import (
    clone_repository,
    commit_changes,
    create_branch,
    git_diff_since,
    push_branch,
)


def _init_repo(tmp_path) -> str:
    """初始化本地 git 仓库（不触网），返回路径。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return str(repo)


def test_create_branch_and_commit(tmp_path):
    repo = _init_repo(tmp_path)
    assert create_branch(repo, "fix/issue-1", "main") is None
    out = subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "fix/issue-1"
    (Path(repo) / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert commit_changes(repo, "fix: 修复 #1 x") is None
    out = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True)
    assert "fix: 修复 #1 x" in out.stdout


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: FAIL（AttributeError: module 'tools.github_tools' has no attribute 'clone_repository'）

- [ ] **Step 3: 实现**（追加到 `tools/github_tools.py`）

```python
def clone_repository(repo_dir: str, repo: str) -> ToolError | None:
    """宿主 gh repo clone（认证经 gh，https 通道）。"""
    result = _run(["gh", "repo", "clone", repo, repo_dir], timeout=600)
    if isinstance(result, ToolError):
        return result
    return None


def create_branch(repo_dir: str, branch: str, base: str) -> ToolError | None:
    """从 base 检出并创建分支。"""
    r1 = _run(["git", "checkout", base], cwd=repo_dir)
    if isinstance(r1, ToolError):
        return r1
    r2 = _run(["git", "checkout", "-b", branch], cwd=repo_dir)
    if isinstance(r2, ToolError):
        return r2
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/github_tools.py tests/test_github_tools.py
git commit -m "feat(tools): github_tools git 写操作（clone/branch/diff/commit/push，宿主执行）"
```

---

### Task 3: PR 创建与评论

**Files:**
- Modify: `tools/github_tools.py`（追加）
- Test: `tests/test_github_tools.py`（追加）

**Interfaces:**
- Produces: `create_pull_request(repo, head, base, title, body) -> str | ToolError` / `comment_issue(repo, number, body) -> ToolError | None`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_github_tools.py`）

```python
from tools.github_tools import comment_issue, create_pull_request


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
    assert cmd[:3] == ["gh", "pr", "create", "--repo"]
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: FAIL（AttributeError: no attribute 'create_pull_request'）

- [ ] **Step 3: 实现**（追加到 `tools/github_tools.py`）

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_github_tools.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/github_tools.py tests/test_github_tools.py
git commit -m "feat(tools): github_tools 创建 PR 与 Issue 评论"
```

---

### Task 4: agent/issue.py 编排（dry-run 全链路）

**Files:**
- Create: `agent/issue.py`
- Test: `tests/test_issue_agent.py`

**Interfaces:**
- Produces: `IssueTask(repository, issue_number, workspace_root, push=False, max_iterations=20)` / `IssueAgentResult(issue, steps, final_answer, branch, diff, review, pr_url, stopped_by_limit, iteration_count, verify_rounds)` / `run_issue_agent(task, llm, code_graph=None, knowledge_client=None) -> IssueAgentResult` / `_review_diff(llm, diff, issue_text) -> str`
- 语义：错误以 `raise ToolError`（ToolError 是 Exception 子类）；`push=False` 停在 review（无远端副作用）；Review 结论首行 `PASS`/`FAIL`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_issue_agent.py
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


def test_dry_run_full_pipeline(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决了问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=str(tmp_path), push=False)
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
                     workspace_root=str(tmp_path))
    with pytest.raises(ToolError, match="读取 Issue 失败"):
        run_issue_agent(task, MockLLMClient([]))


def test_dry_run_no_changes_review_fail(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    monkeypatch.setattr(issue_mod, "git_diff_since", lambda d, base: "")
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS ok"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=str(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.review == "FAIL 无代码变更"
    assert result.pr_url is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_issue_agent.py -v`
Expected: FAIL（ModuleNotFoundError: agent.issue）

- [ ] **Step 3: 最小实现**

```python
# agent/issue.py
"""GitHub Issue Agent 全链路编排。

Get Issue → Clone → Branch → Work（LangGraph 沙箱）→ Diff → Review → Commit → Push → PR。
凭据边界：gh/git 写操作在宿主执行（沙箱容器内无凭据）。
"""
import os
from dataclasses import dataclass

from agent.graph import run_agent_graph
from agent.llm import LLMClient
from agent.loop import AgentStep
from tools.file_tools import ToolError
from tools.github_tools import (
    GitHubIssue,
    clone_repository,
    commit_changes,
    create_branch,
    create_pull_request,
    get_issue,
    get_repository,
    git_diff_since,
    push_branch,
)

# Review 提示词：审查 diff 并输出结论（首行 PASS/FAIL）
REVIEW_PROMPT = (
    "你是代码审查者。以下是针对 Issue 的代码变更 diff，请审查：\n"
    "1. 变更是否解决了 Issue 描述的问题；2. 是否有明显错误或遗漏。\n"
    "输出格式：第一行结论（PASS 或 FAIL），后续为中文要点列表。"
)


@dataclass
class IssueTask:
    """Issue 任务输入。"""
    repository: str              # owner/name
    issue_number: int
    workspace_root: str          # 仓库克隆父目录（如 workspace/repos）
    push: bool = False           # True 才 push + 建 PR；False 停在 review（dry-run）
    max_iterations: int = 20


@dataclass
class IssueAgentResult:
    """Issue 任务结果。"""
    issue: GitHubIssue
    steps: list[AgentStep]
    final_answer: str
    branch: str
    diff: str
    review: str
    pr_url: str | None
    stopped_by_limit: bool
    iteration_count: int
    verify_rounds: int


def _repo_dir_name(repository: str) -> str:
    """owner/name → owner__name（目录安全）。"""
    return repository.replace("/", "__")


def _review_diff(llm: LLMClient, diff: str, issue_text: str) -> str:
    """LLM 审查 diff，返回结论文本（首行 PASS/FAIL + 中文要点）。"""
    if not diff.strip():
        return "FAIL 无代码变更"
    msg = llm.chat(
        [{"role": "system", "content": REVIEW_PROMPT},
         {"role": "user", "content": f"Issue:\n{issue_text}\n\nDiff:\n{diff}"}],
        [],
    )
    return (msg.content or "FAIL 审查无输出").strip()


def run_issue_agent(task: IssueTask, llm: LLMClient,
                    code_graph=None, knowledge_client=None) -> IssueAgentResult:
    """执行 Issue 全链路；push=False 时停在 review（不产生远端副作用）。"""
    issue = get_issue(task.repository, task.issue_number)
    if isinstance(issue, ToolError):
        raise ToolError(f"读取 Issue 失败: {issue.message}")
    repo_info = get_repository(task.repository)
    if isinstance(repo_info, ToolError):
        raise ToolError(f"读取仓库失败: {repo_info.message}")

    repo_dir = os.path.join(task.workspace_root, _repo_dir_name(task.repository))
    os.makedirs(task.workspace_root, exist_ok=True)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        err = clone_repository(repo_dir, task.repository)
        if err is not None:
            raise ToolError(f"克隆失败: {err.message}")

    branch = f"{os.environ.get('KA_GITHUB_BRANCH_PREFIX', 'fix/issue-')}{task.issue_number}"
    err = create_branch(repo_dir, branch, repo_info.default_branch)
    if err is not None:
        raise ToolError(f"创建分支失败: {err.message}")

    prompt = (f"仓库: {repo_info.full_name}\n"
              f"Issue #{issue.number}: {issue.title}\n\n{issue.body}")
    result = run_agent_graph(prompt, llm, max_iterations=task.max_iterations,
                             workspace_root=repo_dir, code_graph=code_graph,
                             knowledge_client=knowledge_client)

    diff = git_diff_since(repo_dir, repo_info.default_branch)
    if isinstance(diff, ToolError):
        raise ToolError(f"读取 diff 失败: {diff.message}")
    review = _review_diff(llm, diff, prompt)

    # Review 未通过：以审查意见为任务重跑一轮（上限 1 次）
    if review.startswith("FAIL"):
        retry_prompt = f"{prompt}\n\n审查意见（请修复后重新工作）:\n{review}"
        result = run_agent_graph(retry_prompt, llm, max_iterations=task.max_iterations,
                                 workspace_root=repo_dir, code_graph=code_graph,
                                 knowledge_client=knowledge_client)
        diff = git_diff_since(repo_dir, repo_info.default_branch)
        if isinstance(diff, ToolError):
            raise ToolError(f"读取 diff 失败: {diff.message}")
        review = _review_diff(llm, diff, retry_prompt)

    pr_url = None
    if task.push and review.startswith("PASS"):
        commit_message = f"fix: 修复 #{issue.number} {issue.title}"
        err = commit_changes(repo_dir, commit_message)
        if err is not None:
            raise ToolError(f"提交失败: {err.message}")
        err = push_branch(repo_dir, branch)
        if err is not None:
            raise ToolError(f"推送失败: {err.message}（可手工执行 git push）")
        pr_body = (f"Closes #{issue.number}\n\n{issue.body}\n\n"
                   f"---\n验证轮数: {result.verify_rounds}\n审查结论:\n{review}")
        pr_url = create_pull_request(task.repository, branch,
                                     repo_info.default_branch,
                                     commit_message, pr_body)
        if isinstance(pr_url, ToolError):
            raise ToolError(f"创建 PR 失败: {pr_url.message}")

    return IssueAgentResult(
        issue=issue, steps=result.steps, final_answer=result.final_answer,
        branch=branch, diff=diff, review=review, pr_url=pr_url,
        stopped_by_limit=result.stopped_by_limit,
        iteration_count=result.iteration_count,
        verify_rounds=result.verify_rounds,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_issue_agent.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add agent/issue.py tests/test_issue_agent.py
git commit -m "feat(agent): 新增 issue 编排，GitHub Issue 全链路 dry-run（fetch/clone/branch/work/diff/review）"
```

---

### Task 5: Review 重试与 push 门禁路径

**Files:**
- Modify: `tests/test_issue_agent.py`（追加；`agent/issue.py` 已实现重试与门禁逻辑）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_issue_agent.py`）

```python
def test_review_fail_triggers_retry(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None)
    script = [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
        LLMMessage(role="assistant", content="FAIL 缺少边界测试"),
        LLMMessage(role="assistant", content=json.dumps(["补测试"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已补测试"),
        LLMMessage(role="assistant", content="PASS 测试已补齐。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=str(tmp_path), push=True)
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.review.startswith("PASS")
    assert commits  # 重试通过后才提交


def test_push_mode_creates_pr(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    calls = {"commit": [], "push": [], "pr": []}
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: calls["commit"].append(m) or None)
    monkeypatch.setattr(issue_mod, "push_branch",
                        lambda d, b: calls["push"].append(b) or None)
    monkeypatch.setattr(
        issue_mod, "create_pull_request",
        lambda repo, head, base, title, body:
            calls["pr"].append((head, base, title)) or
            "https://github.com/test/arc-wiki/pull/999")
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=str(tmp_path), push=True)
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.pr_url == "https://github.com/test/arc-wiki/pull/999"
    assert calls["commit"] == ["fix: 修复 #123 修复重复创建实体"]
    assert calls["push"] == ["fix/issue-123"]
    assert calls["pr"][0][:2] == ("fix/issue-123", "main")


def test_push_mode_review_fail_aborts(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None)
    script = [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
        LLMMessage(role="assistant", content="FAIL 有严重问题"),
        LLMMessage(role="assistant", content=json.dumps(["再修"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="修好了"),
        LLMMessage(role="assistant", content="FAIL 仍有问题"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=str(tmp_path), push=True)
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.pr_url is None
    assert commits == []
```

- [ ] **Step 2: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_issue_agent.py -v`
Expected: PASS（6 passed；Task 4 已实现逻辑）

- [ ] **Step 3: 提交**

```bash
git add tests/test_issue_agent.py
git commit -m "test(agent): Review 重试与 push 门禁路径回归测试"
```

---

### Task 6: main.py --issue 模式

**Files:**
- Modify: `main.py`
- Modify: `.env.example`
- Test: `tests/test_main.py`（追加）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_main.py`）

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py::test_main_issue_mode_dry_run -v`
Expected: FAIL（TypeError: main() got an unexpected keyword argument 'issue'）

- [ ] **Step 3: 实现**

`main.py` 顶部追加导入：

```python
from agent.issue import run_issue_agent
```

`build_parser()` 追加参数：

```python
    parser.add_argument("--issue", action="store_true",
                        help="GitHub Issue 模式：任务参数格式 <owner/name>#<issue_number>，例如 test/arc-wiki#123")
    parser.add_argument("--push", action="store_true",
                        help="Issue 模式：通过审查后 push 并创建 PR（默认 dry-run 停在 review）")
```

`main()` 中 `llm = OpenAICompatClient()` 之后插入：

```python
    if args.issue:
        return _run_issue_mode(args, llm)
```

模块级新增 `_run_issue_mode`（放在 `main()` 之前）：

```python
def _run_issue_mode(args: argparse.Namespace, llm) -> int:
    """Issue 模式：解析 owner/name#number 并执行全链路编排。"""
    if "#" not in args.task:
        print("Issue 模式任务格式: <owner/name>#<issue_number>，例如 test/arc-wiki#123")
        return 1
    repo, _, num = args.task.rpartition("#")
    from agent.issue import IssueTask
    task = IssueTask(
        repository=repo, issue_number=int(num),
        workspace_root=args.workspace,
        push=args.push or os.environ.get("KA_ISSUE_PUSH") == "1",
        max_iterations=args.max_iterations,
    )
    result = run_issue_agent(task, llm)
    print(f"Issue #{result.issue.number}: {result.issue.title}")
    print(f"分支: {result.branch}")
    print(f"迭代轮数: {result.iteration_count}（验证 {result.verify_rounds} 轮）")
    if result.stopped_by_limit:
        print("提示: 已达到迭代上限")
    print(f"最终回答: {result.final_answer}")
    print(f"审查结论:\n{result.review}")
    print(f"Diff:\n{result.diff[:2000]}")
    if result.pr_url:
        print(f"PR: {result.pr_url}")
    else:
        print("（dry-run：未推送远端；通过 --push 开启推送与 PR）")
    return 0
```

`.env.example` 追加：

```text
# W5 GitHub Issue Agent（可选）
# GH_TOKEN=                               # GitHub 认证令牌（或 gh auth login；不入库）
# KA_ISSUE_PUSH=0                        # =1 允许 push + PR（默认 dry-run）
# KA_GITHUB_BRANCH_PREFIX=fix/issue-     # 分支前缀
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -v`
Expected: PASS（原有 + 新增 2 项全绿）

- [ ] **Step 5: 提交**

```bash
git add main.py .env.example tests/test_main.py
git commit -m "feat(main): 新增 --issue/--push 开关，GitHub Issue 模式入口"
```

---

### Task 7: 全量回归 + 真实演示 + 窗口收尾

- [ ] **Step 1: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/ -m "not docker" -q`
Expected: 全部通过（79 原有 + W5 新增 19 项）

- [ ] **Step 2: 真实演示（dry-run，真实仓库）**

选择一个真实公开 Python 仓库 + 一个可复现的 Issue（用户确认目标；建议用兄弟项目《Arknights LLM Wiki》若其已有 GitHub remote，否则选小型 Python 仓库如 `textualize/rich` 的简单 bug issue）。

运行：

```powershell
.venv\Scripts\python.exe "textualize/rich#<issue号>" --issue --max-iterations 30
```

Expected：控制台输出 Issue 标题 → 分支 `fix/issue-<号>` → 迭代轮数/验证轮数 → 最终回答 → 审查结论（PASS/FAIL）→ Diff 摘要；**远端零副作用**（无 push、无 PR、无评论）。

- [ ] **Step 3: 真实演示（push 模式，可选）**

需要用户提供有写权限的测试仓库（如 `Silhouette/ka-demo`）与其一个 open issue；运行 `--issue --push`，Expected：commit → push → `gh pr create` 返回 PR URL，PR body 含测试结果与审查结论。

- [ ] **Step 4: 双轴审查（requesting-code-review）**：Standards（工程约束/注释/无 emoji/凭据边界）+ Spec（Issue→PR 全链路与 dry-run 安全阀）；发现问题修复并补测试

- [ ] **Step 5: 更新 `docs/roadmap.md`**：W5 状态改为 `[x] 已完成`；更新 `docs/devlog.md`：W5 完成记录（实现、测试数、演示结论、决策修订、遗留问题，如沙箱内 git 只读与宿主 git 写操作的边界、超时 124/OOM 判别待 W6/W7 跟进）

- [ ] **Step 6: 合并回 main 并删除 worktree**

```bash
git checkout main
git merge feature/w5-github-issue
git worktree remove ../.worktrees/w5-github-issue
```
