"""审批单核心：人工审批门禁的工作工件（W8 Human-in-the-loop）。

审批单 = 一次等待人工决策的高风险操作请求。当前仅 pr_push 动作；
预留 knowledge_apply 等扩展点在 KNOWN_ACTIONS 注册（解冻后实现对应执行逻辑）。
"""
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

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
                    created_at: str | None = None) -> ApprovalRequest:
    """生成审批单：approval_id（repository__issue-runid）+ diff_sha256 指纹。"""
    if action_type not in KNOWN_ACTIONS:
        raise ApprovalError(f"未知动作类型: {action_type}（已知: {sorted(KNOWN_ACTIONS)}）")
    stamp = created_at or time.strftime("%Y%m%d-%H%M%S")
    approval_id = f"{repository.replace('/', '__')}-{issue_number}-{stamp}"
    return ApprovalRequest(
        approval_id=approval_id, action_type=action_type, status="pending",
        repository=repository, issue_number=issue_number, branch=branch,
        base_branch=base_branch, commit_message=commit_message, diff=diff,
        diff_sha256=_sha256(diff), review=review, verify_rounds=verify_rounds,
        retry_count=retry_count, created_at=stamp)


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