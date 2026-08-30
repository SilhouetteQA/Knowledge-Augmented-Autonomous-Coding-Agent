# main.py
"""CLI 入口：python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10] [--executor local|docker] [--graph]；Issue 模式：python main.py "<owner/name>#<issue_number>" --issue；审批模式：python main.py --approve <审批单路径> --decision approve|reject"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

from agent.graph import run_agent_graph
from agent.issue import auto_approve_if_eligible, repo_dir_name, run_issue_agent
from agent.llm import OpenAICompatClient
from agent.loop import run_agent
from benchmark.loader import load_cases
from benchmark.report import (BenchmarkReport, CaseResult, RunMetadata,
                              compare_reports)
from benchmark.runner import MODEL_PRICE_USD_PER_1K, run_benchmark
from tools.approval import (ApprovalError, approve_request,
                            load_approval, save_approval)
from tools.code_graph import build_code_graph
from tools.file_tools import ToolError
from tools.knowledge_client import get_knowledge_client
from tools.report_trace import TraceError, fetch_trace, save_trace_report


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Knowledge-Augmented Autonomous Coding Agent - W1 本地工作区",
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="任务描述，例如：在 demo-project 中定位某函数并修改（--correct/--issue 模式可为空）")
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
    parser.add_argument("--approve", default=None, metavar="审批单路径",
                        help="审批模式：审批 W8 审批单（配合 --decision approve|reject，approve 才推送并建 PR）")
    parser.add_argument("--decision", choices=["approve", "reject"], default=None,
                        help="审批决定：approve（commit+push+创建 PR）或 reject（拒绝，零远端副作用）")
    parser.add_argument("--comment", default=None,
                        help="审批意见（reject 建议填写原因，将写入审批单 decision_comment）")
    parser.add_argument("--auto-approve", action="store_true",
                        help="D1 条件自动放行：--issue 产单后满足条件（PASS+红线0+非删除型）自动 approve，否则停等人工")
    parser.add_argument("--benchmark", action="store_true",
                        help="评测模式：在固定基准上运行 Agent 并产出报告")
    parser.add_argument("--cases", default="benchmark/cases",
                        help="基准案例目录（默认 benchmark/cases）")
    parser.add_argument("--benchmark-out", default="output/benchmark",
                        help="评测报告输出目录（默认 output/benchmark）")
    parser.add_argument("--compare", default=None,
                        help="对比多轮报告：run_id1,run_id2（读取 output/benchmark/<run_id>/report.json）")
    parser.add_argument("--trace-report", default=None,
                        help="trace 导出模式：按 trace_id 拉取并生成汇总报告（独立模式，无需任务描述）")
    parser.add_argument("--trace-out", default="output/trace",
                        help="trace 报告输出目录（默认 output/trace）")
    return parser


def _run_trace_report_mode(args: argparse.Namespace) -> int:
    """trace 导出模式：fetch → 报告 → 打印摘要（独立模式，无需任务描述/LLM key）。

    无效/无数据 trace_id：拉取到的 summary steps 为空 → 打印「无数据」并
    return 1（P2-9）；有效 trace（steps 非空）正常出报告 rc=0。
    """
    try:
        summary = fetch_trace(
            args.trace_report,
            clickhouse_env=os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "docker", "langfuse", ".env"))
    except TraceError as e:
        print(f"trace 获取失败: {e}")
        return 1
    if not summary.steps:
        print(f"trace 无数据: {summary.trace_id}")
        return 1
    report_path = save_trace_report(summary, args.trace_out)
    print(f"trace: {summary.trace_id} | 任务: {summary.task}")
    print(f"耗时: {summary.total_latency_s:.1f}s | 工具调用: {summary.tool_calls} | "
          f"tokens: {summary.tokens_prompt}/{summary.tokens_completion}")
    print(f"报告: {report_path}")
    return 0


def _run_issue_mode(args: argparse.Namespace, llm) -> int:
    """Issue 模式：解析 owner/name#number 并执行全链路编排。"""
    if not args.task or "#" not in args.task:
        print("Issue 模式任务格式: <owner/name>#<issue_number>，例如 test/arc-wiki#123")
        return 1
    repo, _, num = args.task.rpartition("#")
    # CR-2（不可信输入隔离）：Issue 内容属不可信输入，local 执行器 = 宿主 shell
    # 直达。未显式配置执行器时默认 docker 沙箱；显式 local 放行但警示。
    executor = os.environ.get("KA_EXECUTOR")
    if executor is None:
        os.environ["KA_EXECUTOR"] = "docker"
        print("提示: Issue 模式默认使用 docker 沙箱（Issue 内容为不可信输入，需隔离执行）；"
              "如需宿主执行请显式设置 KA_EXECUTOR=local 或 --executor local")
    elif executor == "local":
        print("警告: Issue 模式使用 local 执行器——不可信 Issue 内容可驱动宿主命令，"
              "建议改用 docker 沙箱（KA_EXECUTOR=docker）")
    from agent.issue import IssueTask
    task = IssueTask(
        repository=repo, issue_number=int(num),
        workspace_root=args.workspace,
        approval_dir=os.environ.get("KA_APPROVAL_DIR", "output/approvals"),
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
    elif result.approval_path and args.auto_approve:
        repo_dir = os.path.join(args.workspace, repo_dir_name(repo))
        action = auto_approve_if_eligible(result.approval_path, repo_dir)
        print(f"自动放行判定: {action}")
        if not action.startswith("approved"):
            print("等待人工审批：审阅差异与审查报告后执行 "
                  f"--approve {result.approval_path} --decision approve|reject")
    elif result.approval_path:
        print(f"审批单已生成: {result.approval_path}")
        print("等待人工审批：审阅差异与审查报告后执行 "
              f"--approve {result.approval_path} --decision approve|reject")
    else:
        print("（无审批单：approval_dir 未配置）")
    return 0


def _run_approve_mode(args: argparse.Namespace) -> int:
    """审批模式：加载审批单 → 人工决定 → 执行（approve 才产生远端副作用）。"""
    path = args.approve
    if not os.path.isfile(path):
        print(f"审批单不存在: {path}")
        return 1
    if args.decision is None:
        print("缺少审批决定：--decision approve|reject")
        return 1
    try:
        approval = load_approval(path)
        repo_dir = os.path.join(args.workspace,
                                repo_dir_name(approval.repository))
        approval = approve_request(approval, args.decision, args.comment, repo_dir)
        # abspath 归一：裸相对文件名的 dirname 为空串，save_approval 需非空目录（IM-4）
        saved = save_approval(approval, os.path.dirname(os.path.abspath(path)))
    except ApprovalError as e:
        print(f"审批失败: {e}")
        return 1
    if approval.status == "approved":
        print(f"已批准并推送: {approval.pr_url}")
    else:
        print("已拒绝（零远端副作用）")
    print(f"审批单已更新: {saved}")
    return 0


def _run_benchmark_mode(args: argparse.Namespace, llm) -> int:
    """评测模式：加载基准案例 → 运行 → 报告；--compare 输出版本对比表。"""
    if args.compare:
        reports = []
        for run_id in args.compare.split(","):
            path = os.path.join(args.benchmark_out, run_id, "report.json")
            if not os.path.isfile(path):
                print(f"报告不存在: {path}")
                return 1
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            cases = [CaseResult(**d) for d in data["results"]]
            reports.append(BenchmarkReport(
                metadata=RunMetadata(**data["metadata"]),
                total=data["total"], resolved=data["resolved"],
                resolution_rate=data["resolution_rate"], results=cases))
        print(compare_reports(reports))
        return 0
    executor = args.executor or os.environ.get("KA_EXECUTOR", "local")
    report = run_benchmark(llm, args.cases, args.benchmark_out, executor,
                           repo_root=os.path.join(args.workspace, "benchmark"))
    print(f"评测完成: {report.metadata.run_id}")
    print(f"案例: {report.total}  解决: {report.resolved}  "
          f"Resolution Rate: {report.resolution_rate:.0%}")
    print(f"报告: {os.path.join(args.benchmark_out, report.metadata.run_id)}")
    if report.results and not MODEL_PRICE_USD_PER_1K:
        print("提示: 模型单价表为空，成本列全部为 0（待提供实际单价后填入 benchmark/runner.py 的 MODEL_PRICE_USD_PER_1K）")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：执行主体后在退出前统一 flush 未导出的 trace。"""
    rc = _main_inner(argv)
    # 短生命周期进程结束前冲刷 trace（tracing 关闭态为 no-op）
    from tools.tracing import flush as tracing_flush
    tracing_flush()
    return rc


def _main_inner(argv: list[str] | None = None) -> int:
    """运行一次 Agent 任务并打印过程（原 main 主体，不含退出前 flush）。"""
    # Windows 控制台/重定向默认 GBK 编码，LLM 输出可能含 emoji 等 GBK 无法表示的字符，
    # 直接 print 会 UnicodeEncodeError 崩溃；统一以 UTF-8 + replace 输出（字符健壮性修复）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.trace_report:
        # trace 导出独立模式：无需任务描述与 LLM key，最先处理
        return _run_trace_report_mode(args)
    if args.approve:
        # 审批模式无需 LLM：直接执行人工决定的推送/拒绝（唯一远端写入口）
        return _run_approve_mode(args)
    if args.executor:
        os.environ["KA_EXECUTOR"] = args.executor
    if args.benchmark:
        # 评测模式在创建 LLM 前处理 --compare（纯读报告无需 key）
        if args.compare:
            return _run_benchmark_mode(args, None)
    llm = OpenAICompatClient()
    if args.benchmark:
        return _run_benchmark_mode(args, llm)
    if args.issue:
        return _run_issue_mode(args, llm)
    if not args.task:
        print("缺少任务描述：python main.py \"<任务描述>\"（--correct 模式无需任务）")
        return 1
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
