"""GitHub Issue Agent 全链路编排。

Get Issue → Clone → Branch → Work（LangGraph 沙箱）→ Diff → Review（独立 Reviewer）→ 生成审批单并停止；
推送唯一入口为 main.py --approve（人工审批门禁，W8）。
凭据边界：gh/git 写操作在宿主与 --approve 阶段执行（沙箱容器内无凭据）。
"""
import os
from dataclasses import dataclass

from agent.graph import run_agent_graph
from agent.llm import LLMClient
from agent.loop import AgentStep
from tools.approval import create_approval, save_approval
from tools.file_tools import ToolError
from tools.github_tools import (
    GitHubIssue,
    clone_repository,
    create_branch,
    get_issue,
    get_repository,
    git_diff_since,
    sync_repository,
)
from tools.tracing import traced

# Review 提示词（独立审查角色）：审查输入 = Issue + diff + 验证轮数，不见工作过程。
REVIEW_PROMPT = (
    "你是独立代码审查者（Reviewer Agent），独立于实施该变更的 Coder，审查以下变更：\n"
    "1. 变更是否解决 Issue 描述的问题；\n"
    "2. 是否有明显错误或遗漏（语法/逻辑/回归风险）；\n"
    "3. 是否混入与 Issue 无关的改动（格式调整、文档顺手修改等无关变更应明确指出，"
    "此类改动不得进入 PR）；\n"
    "4. 测试是否已并入仓库现有测试套件（若发现独立验证脚本而非正式测试，应指出并要求并入）。\n"
    "输出格式：第一行结论（PASS 或 FAIL），后续为中文要点列表；"
    "需要人工特别关注的事项以「人工关注」开头单独列出。"
)


@dataclass
class IssueTask:
    """Issue 任务输入。"""
    repository: str              # owner/name
    issue_number: int
    workspace_root: str          # 仓库克隆父目录（如 workspace/repos）
    approval_dir: str | None = None   # 非 None：review 后生成审批单并停止（等待人工审批）
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
    pr_url: str | None            # 恒为 None（推送唯一入口为 --approve，兼容旧字段）
    stopped_by_limit: bool
    iteration_count: int
    verify_rounds: int
    retry_count: int = 0          # FAIL 重试发生次数（0/1）
    approval_path: str | None = None   # approval_dir 非 None 时生成的审批单路径


def repo_dir_name(repository: str) -> str:
    """owner/name → owner__name（目录安全）。"""
    return repository.replace("/", "__")


def _review_metadata(args, kwargs, result) -> dict:
    """review span metadata：记录结论首行（PASS/FAIL）与「人工关注」标记。"""
    text = str(result or "").strip()
    first = (text.splitlines() or [""])[0]
    return {
        "first_line": first[:80],
        "has_manual_flag": "人工关注" in text,
    }


@traced("review", as_type="generation", metadata_fn=_review_metadata)
def _review_diff(llm: LLMClient, diff: str, issue_text: str,
                 verify_rounds: int) -> str:
    """独立 Reviewer 审查 diff，返回结论文本（首行 PASS/FAIL + 中文要点）。"""
    if not diff.strip():
        return "FAIL 无代码变更"
    msg = llm.chat(
        [{"role": "system", "content": REVIEW_PROMPT},
         {"role": "user",
          "content": f"Issue:\n{issue_text}\n\n验证轮数: {verify_rounds}\n\nDiff:\n{diff}"}],
        [],
    )
    return (msg.content or "FAIL 审查无输出").strip()


def _issue_run_metadata(args, kwargs, result) -> dict:
    """issue.run span metadata：记录验证轮数与重试次数。"""
    return {
        "verify_rounds": getattr(result, "verify_rounds", 0),
        "retry_count": getattr(result, "retry_count", 0),
    }


@traced("issue.run", as_type="agent", metadata_fn=_issue_run_metadata)
def run_issue_agent(task: IssueTask, llm: LLMClient,
                    code_graph=None, knowledge_client=None) -> IssueAgentResult:
    """执行 Issue 全链路；review 后生成审批单并停止（approval_dir 非 None 时）。"""
    if task.issue_snapshot is not None:
        issue = task.issue_snapshot
    else:
        issue = get_issue(task.repository, task.issue_number)
        if isinstance(issue, ToolError):
            raise ToolError(f"读取 Issue 失败: {issue.message}")
    repo_info = get_repository(task.repository)
    if isinstance(repo_info, ToolError):
        raise ToolError(f"读取仓库失败: {repo_info.message}")

    repo_dir = os.path.join(task.workspace_root, repo_dir_name(task.repository))
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
    review = _review_diff(llm, diff, prompt, result.verify_rounds)

    # Review 未通过：以审查意见为任务重跑一轮（上限 1 次）。
    # retried 在首轮判定时捕获（review 后续会被重试后的结论覆盖），
    # 保证 "FAIL→后一轮 PASS" 场景下 retry_count 仍正确记为 1。
    retried = review.startswith("FAIL")
    if retried:
        retry_prompt = f"{prompt}\n\n审查意见（请修复后重新工作）:\n{review}"
        result = run_agent_graph(retry_prompt, llm, max_iterations=task.max_iterations,
                                 workspace_root=repo_dir, code_graph=code_graph,
                                 knowledge_client=knowledge_client)
        diff = git_diff_since(repo_dir, repo_info.default_branch)
        if isinstance(diff, ToolError):
            raise ToolError(f"读取 diff 失败: {diff.message}")
        review = _review_diff(llm, diff, retry_prompt, result.verify_rounds)

    approval_path = None
    if task.approval_dir:
        approval = create_approval(
            action_type="pr_push", repository=task.repository,
            issue_number=issue.number, branch=branch,
            base_branch=repo_info.default_branch,
            commit_message=f"fix: 修复 #{issue.number} {issue.title}",
            diff=diff, review=review,
            verify_rounds=result.verify_rounds, retry_count=1 if retried else 0)
        approval_path = save_approval(approval, task.approval_dir)

    return IssueAgentResult(
        issue=issue, steps=result.steps, final_answer=result.final_answer,
        branch=branch, diff=diff, review=review, pr_url=None,
        stopped_by_limit=result.stopped_by_limit,
        iteration_count=result.iteration_count,
        verify_rounds=result.verify_rounds,
        retry_count=1 if retried else 0,
        approval_path=approval_path,
    )