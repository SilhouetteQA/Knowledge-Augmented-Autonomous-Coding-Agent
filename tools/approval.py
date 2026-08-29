"""审批单核心：人工审批门禁的工作工件（W8 Human-in-the-loop）。

审批单 = 一次等待人工决策的高风险操作请求。当前仅 pr_push 动作；
预留 knowledge_apply 等扩展点在 KNOWN_ACTIONS 注册（解冻后实现对应执行逻辑）。
"""
import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field

from tools.file_tools import ToolError
from tools.github_tools import (
    commit_changes, create_pull_request, git_diff_since, push_branch,
)

# 已知动作类型（扩展点：知识纠错 --apply 解冻后注册并实现执行逻辑）
KNOWN_ACTIONS = {"pr_push"}
# 状态机：pending（待审批）→ approved / rejected（终态，防重放）
STATUSES = {"pending", "approved", "rejected"}
# 审批单必填字段（load 校验）
_REQUIRED_FIELDS = [
    "approval_id", "action_type", "status", "repository", "issue_number",
    "branch", "base_branch", "commit_message", "diff", "diff_sha256",
    "review", "verify_rounds", "retry_count", "created_at",
]


class ApprovalError(Exception):
    """审批单相关错误（文件缺失/校验失败/状态非法/前置检查失败）。"""


@dataclass
class ApprovalRequest:
    """审批单：一次待人工审批的高风险操作请求（当前为 pr_push）。"""
    approval_id: str
    action_type: str
    status: str
    repository: str            # owner/name
    issue_number: int
    branch: str                # 待推送分支
    base_branch: str           # diff 基准 / PR base
    commit_message: str
    diff: str                  # 审批时的 diff 全文（sha256 校验漂移）
    diff_sha256: str
    review: str                # Reviewer Agent 结论文本
    verify_rounds: int
    retry_count: int
    created_at: str
    decided_at: str | None = None
    decision_comment: str | None = None
    pr_url: str | None = None
    red_line_reverts: list[str] = field(default_factory=list)   # 红线还原文件清单（A4 审计）
    final_answer: str = ""            # Agent 最终结论（A7：合法零变更时人工审批可见核验依据）

    def to_dict(self) -> dict:
        """序列化为 dict（dataclasses.asdict）。"""
        return asdict(self)


def _sha256(text: str) -> str:
    """diff 指纹：审批执行前重新比对，防仓库状态漂移。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_approval(*, action_type: str, repository: str, issue_number: int,
                    branch: str, base_branch: str, commit_message: str,
                    diff: str, review: str,
                    verify_rounds: int, retry_count: int,
                    created_at: str | None = None,
                    red_line_reverts: list[str] | None = None,
                    final_answer: str | None = None) -> ApprovalRequest:
    """生成审批单：approval_id（repository__issue-runid）+ diff_sha256 指纹。

    red_line_reverts：执行层红线拦截还原的文件清单（审计；缺省 None → []）。
    final_answer：Agent 最终结论（审计/人工核验依据；缺省 None → ""）。
    """
    if action_type not in KNOWN_ACTIONS:
        raise ApprovalError(f"未知动作类型: {action_type}（已知: {sorted(KNOWN_ACTIONS)}）")
    stamp = created_at or time.strftime("%Y%m%d-%H%M%S")
    approval_id = f"{repository.replace('/', '__')}-{issue_number}-{stamp}"
    return ApprovalRequest(
        approval_id=approval_id, action_type=action_type, status="pending",
        repository=repository, issue_number=issue_number, branch=branch,
        base_branch=base_branch, commit_message=commit_message, diff=diff,
        diff_sha256=_sha256(diff), review=review, verify_rounds=verify_rounds,
        retry_count=retry_count, created_at=stamp,
        red_line_reverts=red_line_reverts or [],
        final_answer=final_answer or "")


def save_approval(approval: ApprovalRequest, directory: str) -> str:
    """写审批单到 directory/approval_id.json，返回文件路径。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{approval.approval_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(approval.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_approval(path: str) -> ApprovalRequest:
    """读审批单并校验 schema；非法文件/字段报 ApprovalError。"""
    if not os.path.isfile(path):
        raise ApprovalError(f"审批单不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ApprovalError(f"审批单解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ApprovalError("审批单格式非法：JSON 根必须是对象")
    missing = [k for k in _REQUIRED_FIELDS if k not in data]
    if missing:
        raise ApprovalError(f"审批单缺字段: {missing}")
    if data["status"] not in STATUSES:
        raise ApprovalError(f"非法状态: {data['status']}")
    if data["action_type"] not in KNOWN_ACTIONS:
        raise ApprovalError(f"未知动作类型: {data['action_type']}")
    # 仅取 dataclass 已知字段（前向兼容：忽略未来新增字段）
    known = set(ApprovalRequest.__dataclass_fields__)
    return ApprovalRequest(**{k: v for k, v in data.items() if k in known})


def _branch_exists(repo_dir: str, branch: str) -> bool:
    """本地 refs/heads/<branch> 是否存在（git rev-parse --verify）。"""
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_dir, capture_output=True)
    return probe.returncode == 0


def _check_drift(approval: ApprovalRequest, repo_dir: str) -> None:
    """执行前漂移检查：分支存在 + 当前 diff 与审批时指纹一致。"""
    if not _branch_exists(repo_dir, approval.branch):
        raise ApprovalError(
            f"分支不存在: {approval.branch}（仓库状态变化，请重新运行 --issue 生成新审批单）")
    current = git_diff_since(repo_dir, approval.base_branch)
    if isinstance(current, ToolError):
        raise ApprovalError(f"读取当前 diff 失败: {current.message}")
    if _sha256(current) != approval.diff_sha256:
        raise ApprovalError(
            "仓库状态与审批时不一致（diff 已变化），请重新运行 --issue 生成新审批单")


def approve_request(approval: ApprovalRequest, decision: str,
                    comment: str | None, repo_dir: str) -> ApprovalRequest:
    """执行人工审批：approve → 漂移检查 → commit → push → PR；reject → 仅记录。

    返回更新后的审批单（status/decided_at/decision_comment/pr_url），由调用方 save。
    """
    if approval.status != "pending":
        raise ApprovalError(f"审批单已处理（{approval.status}），拒绝重复审批")
    if decision not in ("approve", "reject"):
        raise ApprovalError(f"非法审批决定: {decision}（仅 approve/reject）")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if decision == "reject":
        approval.decided_at = now
        approval.decision_comment = comment
        approval.status = "rejected"
        return approval
    _check_drift(approval, repo_dir)
    err = commit_changes(repo_dir, approval.commit_message)
    if err is not None:
        raise ApprovalError(f"提交失败: {err.message}")
    err = push_branch(repo_dir, approval.branch)
    if err is not None:
        raise ApprovalError(f"推送失败: {err.message}（已在本机提交，可手工 git push 后处理）")
    pr_body = (f"Closes #{approval.issue_number}\n\n{approval.commit_message}\n\n"
               f"---\n验证轮数: {approval.verify_rounds}\n审查结论:\n{approval.review}")
    pr_url = create_pull_request(approval.repository, approval.branch,
                                 approval.base_branch, approval.commit_message,
                                 pr_body)
    if isinstance(pr_url, ToolError):
        raise ApprovalError(f"创建 PR 失败: {pr_url.message}（分支已推送）")
    approval.decided_at = now
    approval.pr_url = pr_url
    approval.status = "approved"
    return approval
