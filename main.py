# main.py
"""CLI 入口：python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10]"""
import argparse
import sys

from dotenv import load_dotenv

from agent.llm import OpenAICompatClient
from agent.loop import run_agent


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行一次 Agent 任务并打印过程。"""
    load_dotenv()
    args = build_parser().parse_args(argv)
    llm = OpenAICompatClient()
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
