"""LLM-as-a-Judge：对照 gold patch 判定 Agent diff 的功能等价性。"""
from dataclasses import dataclass

from agent.llm import LLMClient
from tools.github_tools import GitHubIssue

JUDGE_PROMPT = (
    "你是基准评测判定器。对比以下两项针对同一 Issue 的代码变更，判断它们"
    "是否解决了同一问题（功能等价）：\n"
    "1. Agent 提交的变更 diff（Agent patch）\n"
    "2. 社区已合并的参考变更 diff（Gold patch）\n"
    "判定标准：Agent patch 是否修复了 Gold patch 所针对的缺陷/实现同一功能点"
    "（允许实现细节不同，如命名/写法差异）。\n"
    "输出格式：第一行结论（PASS 或 FAIL），后续为中文理由。"
)


@dataclass
class JudgeResult:
    """判定结果：PASS / FAIL / SKIP。"""
    verdict: str
    reason: str


def judge_patch(llm: LLMClient, agent_diff: str, gold_patch: str,
                issue: GitHubIssue) -> JudgeResult:
    """对照 gold patch 判定 agent diff 功能等价性。"""
    if not agent_diff.strip():
        return JudgeResult(verdict="SKIP", reason="SKIP 无代码变更")
    if not gold_patch.strip():
        return JudgeResult(verdict="SKIP", reason="SKIP gold patch 为空")
    msg = llm.chat(
        [{"role": "system", "content": JUDGE_PROMPT},
         {"role": "user", "content": (
             f"Issue #{issue.number}: {issue.title}\n{issue.body}\n\n"
             f"Agent patch:\n{agent_diff}\n\nGold patch:\n{gold_patch}")}],
        [],
    )
    text = (msg.content or "FAIL 判定无输出").strip()
    if text.startswith("PASS"):
        return JudgeResult(verdict="PASS", reason=text)
    return JudgeResult(verdict="FAIL", reason=text)