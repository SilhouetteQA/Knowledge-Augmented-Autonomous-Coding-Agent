"""评测运行器：遍历基准案例 → 基线预检 → 执行 Issue Agent → 双判定 → 汇总报告。"""
import os
import subprocess
import time

from agent.issue import IssueTask, run_issue_agent
from agent.llm import LLMClient
from benchmark.judge import judge_patch
from benchmark.loader import BenchmarkCase
from benchmark.report import (BenchmarkReport, CaseResult, RunMetadata,
                              current_metadata, enrich_run_metadata, save_json,
                              save_markdown, setup_timeout_s)
from tools.docker_sandbox import sandbox_executor
from tools.file_tools import ToolError
from tools.git_diff_parse import parse_deleted_paths
from tools.github_tools import (GitHubIssue, clone_repository, get_repository,
                                sync_repository)
from tools.shell_tools import TestResult, run_command, run_tests
from tools.tracing import traced

# 模型单价表（每 1K token 的美元成本）；命中 → 计算值 + cost_source="computed"；
# 未命中/空表 → cost_usd=None + cost_source="unknown"（T3：不再静默填 0.0——假零会被
# 下游误读为“真实零成本”，使跨轮成本不可比）。
MODEL_PRICE_USD_PER_1K = {
    # "mimo-v2.5": 0.004,   # 示例：每 1K token 美元（待用户提供实际单价）
}

# T3：成本来源标记
COST_SOURCE_COMPUTED = "computed"
COST_SOURCE_UNKNOWN = "unknown"


def _repo_dir_name(repository: str) -> str:
    """repo_dir_name 的别名（历史调用点保留；实现以 agent.issue 为准）。"""
    """owner/name → owner__name（目录安全，与 agent/issue.py 同规则）。"""
    return repository.replace("/", "__")


def cost_for(model: str, prompt_tokens: int, completion_tokens: int
             ) -> tuple[float | None, str]:
    """按单价表估算成本（T3）：→ (cost_usd, cost_source)。

    单价表未命中/空表 → (None, "unknown")；命中 → (计算值, "computed")。
    未知成本必须显式为 None：0.0 会被下游当作“真实零成本”参与汇总与跨轮比较。
    """
    price = MODEL_PRICE_USD_PER_1K.get(model)
    if price is None:
        return None, COST_SOURCE_UNKNOWN
    return (prompt_tokens + completion_tokens) / 1000.0 * price, COST_SOURCE_COMPUTED


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int
              ) -> float | None:
    """历史薄包装（T3）：等价 cost_for(...)[0]；未知模型 → None（不再是 0.0）。"""
    return cost_for(model, prompt_tokens, completion_tokens)[0]


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
        # T3：生效超时与报告 metadata.timeouts 同源（report.setup_timeout_s）
        setup_timeout = setup_timeout_s()
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


def _git_head(repo_dir: str) -> str:
    """当前 HEAD 提交（T3b）：用于核对"实际跑在哪个 fixture 提交上"。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _stamp_fixture(result: CaseResult, pinned: str, actual: str) -> CaseResult:
    """T3b：把 fixture 提交（钉住值 / 实际检出值）钉在结果上；不匹配即记入 errors。"""
    result.repository_commit_pinned = pinned or ""
    result.repository_commit_actual = actual or ""
    if pinned and actual and pinned != actual:
        result.errors.append(
            f"fixture 提交不匹配：钉住 {pinned[:12]}，实际检出 {actual[:12]}")
    return result


def _ensure_repository(repo_root: str, repo_dir: str, repository: str,
                       commit: str = "") -> str:
    """基线阶段确保仓库就绪：克隆/同步，然后检出 base 分支或**钉住的提交**。

    顺序（P0-2）：创建 workspace_root → 目录无 .git 则 clone_repository，
    否则 sync_repository，再 git checkout；base 取
    get_repository(repository).default_branch（与 run_issue_agent 内部一致）。
    run_issue_agent 内部也会 clone/sync，runner 提前执行一遍是为了基线测试
    跑在干净 base 工作树上。任一 git 操作失败抛 ToolError（克隆/同步失败视为
    执行异常，由外层收敛为该 case 的 error 结果）。

    T3b：`commit` 非空时检出该提交（detached HEAD）——冻结基线必须真正跑在被钉住的
    fixture 上；只把 pin 写进报告却不检出，等于没有固定 fixture 状态。
    返回实际检出的 HEAD 提交（取不到时为空串）。
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
    if commit:
        err = _git_checkout(repo_dir, commit)
        if err is not None:
            raise ToolError(f"检出钉住提交失败（{commit}）: {err}")
    else:
        err = _git_checkout(repo_dir, base)
        if err is not None:
            raise ToolError(f"检出 base 分支失败: {err}")
    return _git_head(repo_dir)


def _check_consistency(case: BenchmarkCase, diff: str) -> tuple[bool, str]:
    """交付一致性核查（E10）：diff 实际删除文件数与 expected_actions 核对。

    背景：真实演示中 Agent 交付报告声称删除 17 条而实际 diff 仅 5 条，
    deliverable 与 diff 不符。本函数在判定后核对——diff 删除文件数（复用 E9
    domain_checks 的 _parse_deleted_paths，同款 git quotepath 风格解析）与
    expected_actions["deletions"] 比较：相符 → (True, "")；不符 → (False, 差异详情：
    预期 N / 实际 M + 实际删除文件前几条)。未配 expected_actions → (True, "")。
    仅标注风险，不改变 resolution 语义（控制器裁决）。
    """
    if "deletions" not in (case.expected_actions or {}):
        return True, ""
    expected_n = case.expected_actions["deletions"]
    actual = parse_deleted_paths(diff)
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
    # T3：成本与正常路径同口径（单价未知 → None/unknown，不填假零）
    prompt = llm.tokens_total["prompt"] - prompt_before
    completion = llm.tokens_total["completion"] - completion_before
    cost, cost_source = cost_for(getattr(llm, "model", "unknown"), prompt, completion)
    return CaseResult(
        case_id=case.id, category=case.category, status="environment_error",
        resolution=False, test_pass=False, patch_acceptance=False,
        judge_verdict="SKIP",
        judge_reason="SKIP 基线测试失败（环境缺口，未运行 Agent）",
        tool_success_rate=0.0, iteration_count=0,
        latency_s=end - start,
        tokens_prompt=prompt,
        tokens_completion=completion,
        cost_usd=cost, cost_source=cost_source, diff="",
        errors=[f"基线测试失败: {summary}"])


def _case_result(case: BenchmarkCase, llm: LLMClient, case_dir: str,
                 repo_dir: str, prompt_before: int,
                 completion_before: int, start: float, end: float,
                 run: object) -> CaseResult:
    """单 case 结果组装（含双判定与指标采集）。"""
    errors: list[str] = []
    diff = getattr(run, "diff", "") or ""
    test_pass, test_summary = _test_summary(case, repo_dir)
    gold_path = os.path.join(case_dir, case.gold_patch)
    try:
        with open(gold_path, encoding="utf-8") as f:
            gold_text = f.read()
    except OSError as e:
        errors.append(f"gold patch 读取失败: {e}")
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
    cost, cost_source = cost_for(model, prompt, completion)
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
        cost_usd=cost, cost_source=cost_source,
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
    # T3b：fixture 提交在 try 之外初始化，保证异常路径也能记录（未就绪时留空）
    fixture_pinned = getattr(case, "repository_commit", "") or ""
    fixture_actual = ""
    try:
        # 仓库就绪必须在进入沙箱**之前**完成：DockerExecutor.create() 要求
        # workspace_root 已存在（tools/docker_sandbox.py），而
        # _ensure_repository 才是创建并克隆它的人；顺序颠倒会让 docker 执行器下
        # 每个**全新** case 都以「沙箱工作区不存在」失败 —— 只有复用旧 workspace
        # 时才偶然通过，所以 local 执行器（无此前置检查）与预置 workspace 的单测
        # 都发现不了。repo 就绪本身走宿主 gh/git（凭据不进沙箱，与执行器无关），
        # 因此把它移到 with 之外语义不变。
        fixture_actual = _ensure_repository(repo_root, repo_dir, case.repository,
                                            fixture_pinned)
        # 单 case 单沙箱（审查 I2）：setup/基线/Agent/判定共享同一容器——
        # docker 下 setup 的环境级安装此前随独立容器销毁而蒸发；嵌套的
        # sandbox_executor 调用按 workspace 匹配自动复用外层容器
        with sandbox_executor(repo_dir):
            _run_setup_commands(case, repo_dir)
            baseline_ok, summary = _test_summary(case, repo_dir)
            if not baseline_ok:
                result = _environment_error_result(
                    case, summary, llm, prompt_before, completion_before,
                    start, time.monotonic())
                _stamp_fixture(result, fixture_pinned, fixture_actual)
                return result
            run = run_issue_agent(
                IssueTask(repository=case.repository,
                          issue_number=case.issue.number,
                          workspace_root=repo_root,
                          max_iterations=case.max_iterations,
                          issue_snapshot=case.issue),
                llm)
            result = _case_result(
                case, llm, case_dir, repo_dir, prompt_before, completion_before,
                start, time.monotonic(), run)
            _stamp_fixture(result, fixture_pinned, fixture_actual)
            return result
    except Exception as e:  # noqa: BLE001 — 单 case 失败不中断评测
        prompt = llm.tokens_total["prompt"] - prompt_before
        completion = llm.tokens_total["completion"] - completion_before
        cost, cost_source = cost_for(getattr(llm, "model", "unknown"),
                                     prompt, completion)
        result = CaseResult(
            case_id=case.id, category=case.category, status="error",
            resolution=False, test_pass=False, patch_acceptance=False,
            judge_verdict="SKIP", judge_reason="SKIP 执行异常",
            tool_success_rate=0.0, iteration_count=0,
            latency_s=time.monotonic() - start,
            tokens_prompt=prompt,
            tokens_completion=completion,
            cost_usd=cost, cost_source=cost_source, diff="", errors=[str(e)])
        _stamp_fixture(result, fixture_pinned, fixture_actual)
        return result


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
    # T3：除 git/时间戳外补齐可比性元信息（真实执行器 / 沙箱策略 / 超时 / 迭代上限 /
    # judge 指纹等）。run_max_iterations=None：基准逐 case 自带 max_iterations
    # （loader 保证），run 级上限仅在 case 给不出时作兜底。
    metadata = enrich_run_metadata(
        current_metadata(getattr(llm, "model", "unknown"), executor),
        llm=llm, cases=cases, run_max_iterations=None)
    # T3b：fixture 状态进 metadata —— 冻结基线必须能自证"跑在哪个提交上"
    metadata.fixture_commits = {
        r.case_id: {"pinned": r.repository_commit_pinned,
                    "actual": r.repository_commit_actual}
        for r in results
    }
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