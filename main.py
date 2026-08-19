# main.py
"""CLI 入口：python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10] [--executor local|docker] [--graph]；Issue 模式：python main.py "<owner/name>#<issue_number>" --issue [--push]"""
import argparse
import os
import sys

from dotenv import load_dotenv

from agent.graph import run_agent_graph
from agent.issue import run_issue_agent
from agent.llm import OpenAICompatClient
from agent.loop import run_agent
from tools.code_graph import build_code_graph
from tools.file_tools import ToolError
from tools.knowledge_client import get_knowledge_client


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Knowledge-Augmented Autonomous Coding Agent - W1 本地工作区",
    )
    parser.add_argument("task", help="任务描述，例如：在 demo-project 中定位某函数并修改")
    parser.add_argument("--workspace", default="workspace",
                        help="工作区根目录（默认 workspace）")
    parser.add_argument("--max-iterations", type=int, default=10,
                        help="最大迭代轮数（默认 10）")
    parser.add_argument("--executor", choices=["local", "docker"], default=None,
                        help="命令执行器：local（宿主机）或 docker（容器沙箱）；缺省读 KA_EXECUTOR（默认 local）")
    parser.add_argument("--graph", action="store_true",
                        help="使用 LangGraph 编排（完整工具集，含 run_command/run_tests/git；默认使用 W1 最小循环）")
    parser.add_argument("--issue", action="store_true",
                        help="GitHub Issue 模式：任务参数格式 <owner/name>#<issue_number>，例如 test/arc-wiki#123")
    parser.add_argument("--push", action="store_true",
                        help="Issue 模式：通过审查后 push 并创建 PR（默认 dry-run 停在 review）")
    return parser


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


def main(argv: list[str] | None = None) -> int:
    """运行一次 Agent 任务并打印过程。"""
    # Windows 控制台/重定向默认 GBK 编码，LLM 输出可能含 emoji 等 GBK 无法表示的字符，
    # 直接 print 会 UnicodeEncodeError 崩溃；统一以 UTF-8 + replace 输出（字符健壮性修复）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.executor:
        os.environ["KA_EXECUTOR"] = args.executor
    llm = OpenAICompatClient()
    if args.issue:
        return _run_issue_mode(args, llm)
    if args.graph:
        code_graph = None
        if os.environ.get("KA_CODE_INDEX", "1") != "0":
            built = build_code_graph(args.workspace)
            if not isinstance(built, ToolError):
                code_graph = built
        knowledge = get_knowledge_client()
        if isinstance(knowledge, ToolError):
            knowledge = None
        result = run_agent_graph(args.task, llm, max_iterations=args.max_iterations,
                                 workspace_root=args.workspace, code_graph=code_graph,
                                 knowledge_client=knowledge)
        print(f"任务: {args.task}")
        print(f"工作区: {args.workspace}")
        print(f"计划: {result.plan}")
        for i, step in enumerate(result.steps, 1):
            print(f"[步骤 {i}] {step.tool_name}({step.arguments})")
        print(f"迭代轮数: {result.iteration_count}")
        if result.stopped_by_limit:
            print("提示: 已达到迭代上限")
        print(f"最终回答: {result.final_answer}")
        return 0
    result = run_agent(args.task, llm, max_iterations=args.max_iterations,
                       workspace_root=args.workspace)
    print(f"任务: {args.task}")
    print(f"工作区: {args.workspace}")
    for i, step in enumerate(result.steps, 1):
        print(f"[步骤 {i}] {step.tool_name}({step.arguments})")
    print(f"迭代轮数: {result.iteration_count}")
    if result.stopped_by_limit:
        print("提示: 已达到迭代上限")
    print(f"最终回答: {result.final_answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
