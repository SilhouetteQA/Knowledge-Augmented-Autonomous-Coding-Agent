# main.py
"""CLI 入口：python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10] [--executor local|docker] [--graph]"""
import argparse
import os
import sys

from dotenv import load_dotenv

from agent.graph import run_agent_graph
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
    return parser


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
