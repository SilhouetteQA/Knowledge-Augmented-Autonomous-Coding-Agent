"""评测运行器：遍历基准案例 → 基线预检 → 执行 Issue Agent → 双判定 → 汇总报告。"""
import os
import subprocess
import time

from agent.issue import IssueTask, run_issue_agent
from agent.llm import LLMClient
from benchmark.domain_checks import (DOMAIN_PASS, _parse_deleted_paths,
                                     check_bridge, check_deletions)
from benchmark.judge import judge_patch
from benchmark.loader import BenchmarkCase
from benchmark.report import (BenchmarkReport, CaseResult, RunMetadata,
                              current_metadata, save_json, save_markdown)
from tools.docker_sandbox import sandbox_executor
from tools.file_tools import ToolError
from tools.github_tools import (GitHubIssue, clone_repository, get_repository,
                                sync_repository)
from tools.shell_tools import TestResult, run_command, run_tests
from tools.tracing import traced

# 模型单价表（每 1K token 的美元成本）；未知模型或空单价表 → cost_usd=0；CLI 摘要提示单价未知
MODEL_PRICE_USD_PER_1K = {
    # "mimo-v2.5": 0.004,   # 示例：每 1K token 美元（待用户提供实际单价）
}


def _repo_dir_name(repository: str) -> str:
    """repo_dir_name 的别名（历史调用点保留；实现以 agent.issue 为准）。"""
    """owner/name → owner__name（目录安全，与 agent/issue.py 同规则）。"""
    return repository.replace("/", "__")


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按单价表估算成本；未知模型返回 0.0。"""
    price = MODEL_PRICE_USD_PER_1K.get(model)
    if price is None:
        return 0.0
    return (prompt_tokens + completion_tokens) / 1000.0 * price


def _tool_success_rate(steps: list) -> float:
    """工具调用成功率 = 成功次数 / 总调用次数。

    判定口径：AgentStep.result 为 ToolError（agent/loop.py 的工具分发
    对缺参/未知工具/执行失败返回 ToolError）视为失败，其余视为成功；
    无任何工具调用时返回 0.0。
    """
    if not steps:
        return 0.0
    failed = sum(1 for s in steps if isinstance(getattr(s, "result", None), ToolError))
    return (len(steps) - failed) / len(steps)


def _test_summary(case: BenchmarkCase, repo_dir: str) -> tuple[bool, str]:
    """must_pass 全部通过 → (True, "")；否则 (False, 失败摘要)。

    基线与判定阶段共用，运行方式与原 _test_pass 一致：判定阶段的测试运行包在
    sandbox_executor 上下文内（docker 执行器下一个 case 一个沙箱）；must_pass
    为相对路径时拼接 repo_dir 为绝对路径，保证两种执行器的路径解析一致
    （DockerExecutor 以宿主 CWD 为基准解析相对路径）。失败摘要含测试路径、
    pytest 计数与失败明细（前 3 条 test - message + 总失败数），作为
    environment_error case 的 reason（errors 首条）与判定阶段的
    test_error_summary 来源。
    """
    with sandbox_executor(repo_dir):
        for p in case.must_pass:
            test_path = p if os.path.isabs(p) else os.path.join(repo_dir, p)
            res = run_tests(path=test_path, workspace_root=repo_dir)
            if isinstance(res, TestResult):
                if res.failed != 0 or res.error != 0 or res.total <= 0:
                    detail = _failure_detail(res)
                    return (False, f"{p}: failed={res.failed} error={res.error} "
                                   f"total={res.total}" + detail)
            else:
                detail = getattr(res, "message", repr(res))
                return False, f"{p}: 测试执行异常（{detail}）"
    return True, ""


def _failure_detail(res: TestResult) -> str:
    """失败明细摘要：前 3 条 test - message + 总失败数；无 failed 时为空串。"""
    if not res.failures:
        return ""
    head = " | ".join(f"{f.test} - {f.message}" for f in res.failures[:3])
    total = f"（共 {len(res.failures)} 个失败）" if len(res.failures) > 3 else ""
    return "；" + head + total


def _test_pass(case: BenchmarkCase, repo_dir: str) -> bool:
    """must_pass 全部通过（failed==0 且 error==0 且 total>0）才 True（判定阶段）。"""
    return _test_summary(case, repo_dir)[0]


def _run_setup_commands(case: BenchmarkCase, repo_dir: str) -> None:
    """逐条执行 case.setup_commands（基线预检之前、未修改的 base 工作树上）。

    评测语义（P0-1b + P0-2 前置重构）：环境依赖先装好基线测试才有意义——
    基线失败 = 环境缺口而非用例问题。命令在 repo_dir（克隆目录）执行，与
    run_tests 同层工具（run_command）且包在 sandbox_executor 上下文内（docker
    执行器下一任务一沙箱，与 _test_pass 一致）；任一命令失败抛 RuntimeError
    （含命令与输出），由 run_benchmark_cases 收敛为该 case 的 error 结果并
    中止剩余 setup。依赖为环境级安装，Agent 内部 sync 的 clean 不会清掉，
    判定阶段不再重复执行。
    """
    if not case.setup_commands:
        return
    with sandbox_executor(repo_dir):
        setup_timeout = int(os.environ.get("KA_SETUP_TIMEOUT_S", "60"))
        for cmd in case.setup_commands:
            res = run_command(cmd, cwd=repo_dir, workspace_root=repo_dir,
                              timeout=setup_timeout)
            if isinstance(res, ToolError):
                raise RuntimeError(f"setup 命令失败: {cmd} -> {res}")
            if res.exit_code != 0:
                detail = (res.stderr or res.stdout or "").strip()
                raise RuntimeError(
                    f"setup 命令失败: {cmd}（exit={res.exit_code}）: {detail}")


def _git_checkout(repo_dir: str, base: str) -> str | None:
    """检出 base 分支（宿主执行）；成功返回 None，失败返回错误信息。"""
    try:
        proc = subprocess.run(
            ["git", "checkout", base], cwd=repo_dir, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"git checkout 失败: {e}"
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "").strip() or "git checkout 失败"
    return None


def _ensure_repository(repo_root: str, repo_dir: str, repository: str) -> None:
    """基线阶段确保仓库就绪：克隆/同步到 base 分支（未修改的原仓库）。

    顺序（P0-2）：创建 workspace_root → 目录无 .git 则 clone_repository，
    否则 sync_repository，再 git checkout <base>；base 取
    get_repository(repository).default_branch（与 run_issue_agent 内部一致）。
    run_issue_agent 内部也会 clone/sync，runner 提前执行一遍是为了基线测试
    跑在干净 base 工作树上。任一 git 操作失败抛 ToolError（克隆/同步失败视为
    执行异常，由外层收敛为该 case 的 error 结果）。
    """
    os.makedirs(repo_root, exist_ok=True)
    repo = get_repository(repository)
    if isinstance(repo, ToolError):
        raise ToolError(f"读取仓库元信息失败: {repo.message}")
    base = repo.default_branch
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        err = clone_repository(repo_dir, repository)
        if err is not None:
            raise ToolError(f"克隆仓库失败: {err.message}")
    else:
        err = sync_repository(repo_dir, base)
        if err is not None:
            raise ToolError(f"同步仓库失败: {err.message}")
    err = _git_checkout(repo_dir, base)
    if err is not None:
        raise ToolError(f"检出 base 分支失败: {err}")


def _check_consistency(case: BenchmarkCase, diff: str) -> tuple[bool, str]:
    """交付一致性核查（E10）：diff 实际删除文件数与 expected_actions 核对。

    背景：兄弟项目 #1 演示中 Agent 交付报告声称删除 17 条而实际 diff 仅 5 条，
    deliverable 与 diff 不符。本函数在判定后核对——diff 删除文件数（复用 E9
    domain_checks 的 _parse_deleted_paths，同款 git quotepath 风格解析）与
    expected_actions["deletions"] 比较：相符 → (True, "")；不符 → (False, 差异详情：
    预期 N / 实际 M + 实际删除文件前几条)。未配 expected_actions → (True, "")。
    仅标注风险，不改变 resolution 语义（控制器裁决）。
    """
    if "deletions" not in (case.expected_actions or {}):
        return True, ""
    expected_n = case.expected_actions["deletions"]
    actual = _parse_deleted_paths(diff)
    actual_n = len(actual)
    if actual_n == expected_n:
        return True, ""
    head = "、".join(actual[:3]) if actual else "无"
    return False, (f"预期删除 {expected_n} 个文件，实际删除 {actual_n} 个文件"
                   f"（实际删除：{head}）")


def _environment_error_result(case: BenchmarkCase, summary: str,
                              llm: LLMClient, prompt_before: int,
                              completion_before: int, start: float,
                              end: float) -> CaseResult:
    """基线预检失败的 case 结果：environment_error，跳过 Agent 与判定。

    summary 为 _test_summary 的失败摘要（测试路径 + pytest 计数），写入
    errors 首条作为 reason；resolution=False，Agent 未运行故无 diff/tokens。
    """
    return CaseResult(
        case_id=case.id, category=case.category, status="environment_error",
        resolution=False, test_pass=False, patch_acceptance=False,
        judge_verdict="SKIP",
        judge_reason="SKIP 基线测试失败（环境缺口，未运行 Agent）",
        tool_success_rate=0.0, iteration_count=0,
        latency_s=end - start,
        tokens_prompt=llm.tokens_total["prompt"] - prompt_before,
        tokens_completion=llm.tokens_total["completion"] - completion_before,
        cost_usd=0.0, diff="",
        errors=[f"基线测试失败: {summary}"])


def _case_result(case: BenchmarkCase, llm: LLMClient, case_dir: str,
                 repo_dir: str, prompt_before: int,
                 completion_before: int, start: float, end: float,
                 run: object) -> CaseResult:
    """单 case 结果组装（含双判定与指标采集）。"""
    errors: list[str] = []
    diff = getattr(run, "diff", "") or ""
    test_pass, test_summary = _test_summary(case, repo_dir)
    gold_text = ""
    if not case.domain_check:
        # B9（E9 自审遗留）：域 case 判定走规则检查器，gold 读取纯浪费且
        # 读失败还会污染 errors——仅功能等价 case 需要 gold
        gold_path = os.path.join(case_dir, case.gold_patch)
        try:
            with open(gold_path, encoding="utf-8") as f:
                gold_text = f.read()
        except OSError as e:
            errors.append(f"gold patch 读取失败: {e}")
    if case.domain_check:
        # E9：域案例判定——规则检查器（deletions/bridge）替代 LLM Judge。域知识任务
        # （删除/bridge）无 gold 可比，「功能等价」模型不匹配；判定证据进 judge_reason，
        # 不追加 errors；DOMAIN_SKIP 记录 SKIP 且 resolution=False（与 Judge SKIP 语义一致）。
        checker = {"deletions": check_deletions, "bridge": check_bridge}[case.domain_check]
        dc = checker(diff, repo_dir)
        judge_verdict, judge_reason = dc.verdict, dc.reason
        patch_acceptance = judge_verdict == DOMAIN_PASS
    else:
        judge = judge_patch(llm, diff, gold_text, case.issue)
        judge_verdict, judge_reason = judge.verdict, judge.reason
        patch_acceptance = judge.verdict == "PASS"
    resolution = test_pass and patch_acceptance
    # E10：判定后核查交付一致性（diff 实际删除数 vs expected_actions）——仅标注
    # consistency/consistency_note，不参与 resolution（控制器裁决：resolution 语义不变）。
    consistency, consistency_note = _check_consistency(case, diff)
    prompt, completion = (llm.tokens_total["prompt"] - prompt_before,
                          llm.tokens_total["completion"] - completion_before)
    model = getattr(llm, "model", "unknown")
    tool_success = _tool_success_rate(getattr(run, "steps", None) or [])
    return CaseResult(
        case_id=case.id, category=case.category,
        status="resolved" if resolution else "not_resolved",
        resolution=resolution, test_pass=test_pass,
        patch_acceptance=patch_acceptance,
        judge_verdict=judge_verdict, judge_reason=judge_reason,
        tool_success_rate=tool_success,
        iteration_count=getattr(run, "iteration_count", 0),
        latency_s=end - start,
        tokens_prompt=prompt, tokens_completion=completion,
        cost_usd=_cost_usd(model, prompt, completion),
        diff=diff, errors=errors,
        test_error_summary=test_summary[:600],
        consistency=consistency, consistency_note=consistency_note,
    )


def _case_metadata(args, kwargs, result) -> dict:
    """evaluation.case span metadata：case_id + category（+ status）。

    _run_one_case(case, llm, case_dir, repo_root) 的 case 位于 args[0]；
    result 为 CaseResult，status 缺省时仅返回 case_id/category。
    """
    case = args[0] if args else None
    meta = {
        "case_id": getattr(case, "id", "") or "",
        "category": getattr(case, "category", "") or "",
    }
    status = getattr(result, "status", None)
    if status:
        meta["status"] = status
    return meta


@traced("evaluation.case", as_type="span", metadata_fn=_case_metadata)
def _run_one_case(case: BenchmarkCase, llm: LLMClient, case_dir: str,
                  repo_root: str) -> CaseResult:
    """执行单 case 全流程（repo 就绪 → setup → 基线预检 → Agent → 判定）并返回结果。

    单独抽成函数以便 traced 装饰为 case 级 evaluation.case span（P2-8，spec
    §4.6 遗留）：metadata 含 case_id/category/status；tracing 关闭态直通
    零开销。任一环节异常收敛为该 case 的 error 结果，不中断其余 case。
    """
    start = time.monotonic()
    prompt_before = llm.tokens_total["prompt"]
    completion_before = llm.tokens_total["completion"]
    # B13 探针暴露：repo_root 相对（main.py 默认 workspace/benchmark）时 repo_dir
    # 亦相对——_run_setup_commands/_test_summary 把 repo_dir 同时当 cwd 与
    # workspace_root，resolve_workspace_path 会拼出 root/repo_dir 嵌套路径（docker
    # 下 cd 不存在目录）。入口处归一为绝对路径。
    repo_dir = os.path.abspath(os.path.join(repo_root, _repo_dir_name(case.repository)))
    try:
        # 单 case 单沙箱（审查 I2）：setup/基线/Agent/判定共享同一容器——
        # docker 下 setup 的环境级安装此前随独立容器销毁而蒸发；嵌套的
        # sandbox_executor 调用按 workspace 匹配自动复用外层容器
        with sandbox_executor(repo_dir):
            _ensure_repository(repo_root, repo_dir, case.repository)
            _run_setup_commands(case, repo_dir)
            baseline_ok, summary = _test_summary(case, repo_dir)
            if not baseline_ok:
                return _environment_error_result(
                    case, summary, llm, prompt_before, completion_before,
                    start, time.monotonic())
            run = run_issue_agent(
                IssueTask(repository=case.repository,
                          issue_number=case.issue.number,
                          workspace_root=repo_root,
                          max_iterations=case.max_iterations,
                          issue_snapshot=case.issue),
                llm)
            return _case_result(
                case, llm, case_dir, repo_dir, prompt_before, completion_before,
                start, time.monotonic(), run)
    except Exception as e:  # noqa: BLE001 — 单 case 失败不中断评测
        return CaseResult(
            case_id=case.id, category=case.category, status="error",
            resolution=False, test_pass=False, patch_acceptance=False,
            judge_verdict="SKIP", judge_reason="SKIP 执行异常",
            tool_success_rate=0.0, iteration_count=0,
            latency_s=time.monotonic() - start,
            tokens_prompt=llm.tokens_total["prompt"] - prompt_before,
            tokens_completion=llm.tokens_total["completion"] - completion_before,
            cost_usd=0.0, diff="", errors=[str(e)])


def run_benchmark_cases(llm: LLMClient, cases: list[BenchmarkCase],
                        case_dir: str,
                        out_dir: str, executor: str,
                        repo_root: str) -> BenchmarkReport:
    """执行一组案例并返回汇总报告（任一 case 异常不中断）。

    每 case 顺序（P0-2，见 _run_one_case）：repo 就绪 → setup_commands（依赖
    先装好）→ 基线预检（未修改的原仓库跑 must_pass）→ 基线失败标
    environment_error 并跳过 Agent → 基线通过则 Agent → 测试判定 → judge。基线
    失败 = 环境缺口而非用例问题；Agent 内部 sync 会 clean 未跟踪缓存，依赖是
    环境级的不会丢，故判定阶段不再重复 setup。
    """
    results: list[CaseResult] = []
    for case in cases:
        results.append(_run_one_case(case, llm, case_dir, repo_root))
    total = len(results)
    resolved = sum(1 for r in results if r.resolution)
    # P2-6 双口径：raw 分母为全部案例（兼容既有报告）；adjusted 分母排除 error 与
    # environment_error（未实际运行 Agent 的案例），分母为 0 时记 0.0
    errors = sum(1 for r in results if r.status == "error")
    env_errors = sum(1 for r in results if r.status == "environment_error")
    adj_denom = total - errors - env_errors
    metadata = current_metadata(getattr(llm, "model", "unknown"), executor)
    report = BenchmarkReport(metadata=metadata, total=total, resolved=resolved,
                             resolution_rate=(resolved / total) if total else 0.0,
                             results=results,
                             resolution_rate_adjusted=(
                                 resolved / adj_denom) if adj_denom else 0.0)
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