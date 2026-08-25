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
    sync_repository,
)
from tools.tracing import traced

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
    issue_snapshot: GitHubIssue | None = None   # 非 None 时离线模式：跳过 get_issue


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


@traced("issue.run", as_type="agent")
def run_issue_agent(task: IssueTask, llm: LLMClient,
                    code_graph=None, knowledge_client=None) -> IssueAgentResult:
    """执行 Issue 全链路；push=False 时停在 review（不产生远端副作用）。"""
    if task.issue_snapshot is not None:
        issue = task.issue_snapshot
    else:
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
    else:
        err = sync_repository(repo_dir, repo_info.default_branch)
        if err is not None:
            raise ToolError(f"同步仓库失败: {err.message}")

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