"""评测运行器：遍历基准案例 → 执行 Issue Agent → 双判定 → 汇总报告。"""
import os
import time

from agent.issue import IssueTask, run_issue_agent
from agent.llm import LLMClient
from benchmark.judge import judge_patch
from benchmark.loader import BenchmarkCase
from benchmark.report import (BenchmarkReport, CaseResult, RunMetadata,
                              current_metadata, save_json, save_markdown)
from tools.docker_sandbox import sandbox_executor
from tools.shell_tools import TestResult, run_tests
from tools.tracing import traced

# 模型单价表（每 1K token 的美元成本）；未知模型或空单价表 → cost_usd=0；CLI 摘要提示单价未知
MODEL_PRICE_USD_PER_1K = {
    # "mimo-v2.5": 0.004,   # 示例：每 1K token 美元（待用户提供实际单价）
}


def _repo_dir_name(repository: str) -> str:
    """owner/name → owner__name（目录安全，与 agent/issue.py 同规则）。"""
    return repository.replace("/", "__")


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按单价表估算成本；未知模型返回 0.0。"""
    price = MODEL_PRICE_USD_PER_1K.get(model)
    if price is None:
        return 0.0
    return (prompt_tokens + completion_tokens) / 1000.0 * price


def _test_pass(case: BenchmarkCase, repo_dir: str) -> bool:
    """must_pass 全部通过（failed==0 且 error==0 且 total>0）才 True。

    判定阶段的测试运行包在 sandbox_executor 上下文内：docker 执行器下
    一个 case 一个沙箱（Agent 工作沙箱已销毁，判定阶段独立沙箱）。
    must_pass 为相对路径时拼接 repo_dir 为绝对路径，保证两种执行器的
    路径解析一致（DockerExecutor 以宿主 CWD 为基准解析相对路径）。
    """
    with sandbox_executor(repo_dir):
        for p in case.must_pass:
            test_path = p if os.path.isabs(p) else os.path.join(repo_dir, p)
            res = run_tests(path=test_path, workspace_root=repo_dir)
            if isinstance(res, TestResult):
                if res.failed != 0 or res.error != 0 or res.total <= 0:
                    return False
            else:
                return False
    return True


def _case_result(case: BenchmarkCase, llm: LLMClient, case_dir: str,
                 repo_dir: str, prompt_before: int,
                 completion_before: int, start: float, end: float,
                 run: object) -> CaseResult:
    """单 case 结果组装（含双判定与指标采集）。"""
    errors: list[str] = []
    diff = getattr(run, "diff", "") or ""
    test_pass = _test_pass(case, repo_dir)
    gold_text = ""
    gold_path = os.path.join(case_dir, case.gold_patch)
    try:
        with open(gold_path, encoding="utf-8") as f:
            gold_text = f.read()
    except OSError as e:
        errors.append(f"gold patch 读取失败: {e}")
    judge = judge_patch(llm, diff, gold_text, case.issue)
    resolution = test_pass and judge.verdict == "PASS"
    prompt, completion = (llm.tokens_total["prompt"] - prompt_before,
                          llm.tokens_total["completion"] - completion_before)
    model = getattr(llm, "model", "unknown")
    return CaseResult(
        case_id=case.id, category=case.category,
        status="resolved" if resolution else "not_resolved",
        resolution=resolution, test_pass=test_pass,
        patch_acceptance=judge.verdict == "PASS",
        judge_verdict=judge.verdict, judge_reason=judge.reason,
        tool_success_rate=0.0,
        iteration_count=getattr(run, "iteration_count", 0),
        latency_s=end - start,
        tokens_prompt=prompt, tokens_completion=completion,
        cost_usd=_cost_usd(model, prompt, completion),
        diff=diff, errors=errors,
    )


def run_benchmark_cases(llm: LLMClient, cases: list[BenchmarkCase],
                        case_dir: str,
                        out_dir: str, executor: str,
                        repo_root: str) -> BenchmarkReport:
    """执行一组案例并返回汇总报告（任一 case 异常不中断）。"""
    results: list[CaseResult] = []
    for case in cases:
        start = time.monotonic()
        prompt_before = llm.tokens_total["prompt"]
        completion_before = llm.tokens_total["completion"]
        repo_dir = os.path.join(repo_root, _repo_dir_name(case.repository))
        try:
            run = run_issue_agent(
                IssueTask(repository=case.repository,
                          issue_number=case.issue.number,
                          workspace_root=repo_root,
                          push=False,
                          max_iterations=case.max_iterations,
                          issue_snapshot=case.issue),
                llm)
            results.append(_case_result(
                case, llm, case_dir, repo_dir, prompt_before, completion_before,
                start, time.monotonic(), run))
        except Exception as e:  # noqa: BLE001 — 单 case 失败不中断评测
            results.append(CaseResult(
                case_id=case.id, category=case.category, status="error",
                resolution=False, test_pass=False, patch_acceptance=False,
                judge_verdict="SKIP", judge_reason="SKIP 执行异常",
                tool_success_rate=0.0, iteration_count=0,
                latency_s=time.monotonic() - start,
                tokens_prompt=llm.tokens_total["prompt"] - prompt_before,
                tokens_completion=llm.tokens_total["completion"] - completion_before,
                cost_usd=0.0, diff="", errors=[str(e)]))
    total = len(results)
    resolved = sum(1 for r in results if r.resolution)
    metadata = current_metadata(getattr(llm, "model", "unknown"), executor)
    report = BenchmarkReport(metadata=metadata, total=total, resolved=resolved,
                             resolution_rate=(resolved / total) if total else 0.0,
                             results=results)
    if out_dir:
        run_out = os.path.join(out_dir, metadata.run_id)
        save_json(report, run_out)
        save_markdown(report, run_out)
    return report


@traced("benchmark.run")
def run_benchmark(llm: LLMClient, case_dir: str, out_dir: str,
                  executor: str = "local",
                  repo_root: str = "workspace/benchmark") -> BenchmarkReport:
    """一键评测：加载案例 → 执行 → 报告。"""
    from benchmark.loader import load_cases
    cases = load_cases(case_dir)
    return run_benchmark_cases(llm, cases, case_dir, out_dir, executor, repo_root)