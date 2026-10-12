"""评测报告：run metadata、JSON/Markdown 报告与多轮对比。"""
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime

# T3：resolution 判定口径版本——判定语义/实现变化时必须递增，供跨轮报告比对。
RESOLUTION_DEFINITION_VERSION = "t3-1"

# T3：基准运行的副作用策略。核对依据（读码结论）：benchmark/runner.py 调用
# run_issue_agent 时不传 approval_dir（默认 None）→ agent/issue.py 不生成审批单、
# pr_url 恒为 None；远端写（push / 建 PR）的唯一入口是 main.py --approve →
# tools.approval.approve_request → tools.github_tools.push_branch/
# create_pull_request。该入口不在 benchmark 调用链上，故基准运行对被测仓库只读
# （clone/sync/checkout 均为读操作）。
SIDE_EFFECT_POLICY_READ_ONLY = "read_only"

# T3：judge 提示词文本缓存（judge_prompt_sha256() 惰性取自 benchmark.judge）
_JUDGE_PROMPT_CACHE = None


@dataclass
class RunMetadata:
    """一次评测运行的元信息（版本可比性依据）。"""
    run_id: str
    git_commit: str
    branch: str
    model: str
    timestamp: str
    executor: str
    params: dict
    # T3（可比性元信息）：以下字段全部带默认值，保证旧 report.json 仍可
    # RunMetadata(**data["metadata"]) 反序列化（main.py --compare 路径）。
    provider: str = ""                  # 供应商名（agent.llm 解析结果）
    base_url: str = ""                  # 实际端点（llm.base_url）
    temperature: float | None = None    # 采样温度；未配置记 None（当前 chat 不传该参数）
    max_tokens: int | None = None       # 输出上限；未配置记 None
    executor_actual: str = ""           # 真实创建的执行器（tools.docker_sandbox 记录）
    sandbox_policy: dict = field(default_factory=dict)   # image/cpus/memory/pids/network
    # T3b：case_id → {pinned, actual}，用于核对"这一跑真的跑在被冻结的提交上"
    fixture_commits: dict = field(default_factory=dict)
    iteration_limits: dict = field(default_factory=dict) # 逐 case 迭代上限 + run 级兜底
    timeouts: dict = field(default_factory=dict)         # 生效的 KA_* 超时/重试 env
    resolution_definition_version: str = RESOLUTION_DEFINITION_VERSION
    side_effect_policy: str = SIDE_EFFECT_POLICY_READ_ONLY
    judge_prompt_sha256: str = ""       # judge 提示词 sha256（判定可比性）


@dataclass
class CaseResult:
    """单个基准任务结果。"""
    case_id: str
    category: str
    status: str                 # resolved / not_resolved / error / environment_error
    resolution: bool
    test_pass: bool
    patch_acceptance: bool
    judge_verdict: str          # PASS / FAIL / SKIP
    judge_reason: str
    tool_success_rate: float
    iteration_count: int
    latency_s: float
    tokens_prompt: int
    tokens_completion: int
    # T3：单价未知时为 None（此前静默填 0.0，会被下游误读为“真实零成本”）
    cost_usd: float | None
    diff: str
    errors: list[str] = field(default_factory=list)
    # T3：成本来源标记——computed（单价表命中并计算）/ unknown（无单价，cost_usd=None）
    cost_source: str = "unknown"
    # T3b：fixture 提交——pinned 来自 cases.lock.json（冻结值），actual 是真实检出的 HEAD。
    # 只记录不检出等于没固定 fixture，因此 runner 现在真的 checkout pinned，并把两者都写进结果。
    repository_commit_pinned: str = ""
    repository_commit_actual: str = ""
    test_error_summary: str = ""   # must_pass 失败摘要（判定阶段；无失败为空）
    # E10：交付一致性核查——diff 实际删除数 与 expected_actions 核对；相符/未配 →
    # True 且 note 空，不符 → False + 差异详情（预期/实际 + 实际删除文件前几条）。
    # 仅标注风险，不改变 resolution 语义（控制器裁决）。
    consistency: bool = True
    consistency_note: str = ""


@dataclass
class BenchmarkReport:
    """一轮评测的汇总报告。"""
    metadata: RunMetadata
    total: int
    resolved: int
    resolution_rate: float
    results: list[CaseResult]
    # 双口径（P2-6）：resolution_rate 分母为全部案例（raw，兼容既有报告/对比）；
    # resolution_rate_adjusted 分母排除 error 与 environment_error，由 runner 汇总计算，
    # 分母为 0 时记 0.0；缺省 0.0 仅为兼容直接构造报告的既有调用
    resolution_rate_adjusted: float = 0.0


def _truncate(text: str, limit: int) -> str:
    """明细表格文本截断：超长保留前 limit 字符并追加省略号。"""
    return text if len(text) <= limit else text[:limit] + "..."


def _fmt_cost(cost: float | None) -> str:
    """成本渲染（T3）：None（单价未知）→ unknown，绝不做假零渲染。"""
    return "unknown" if cost is None else f"{cost:.4f}"


def _total_cost(report: "BenchmarkReport") -> str:
    """总成本（T3）：任一 case 成本未知即标注 unknown，绝不把 None 当 0 求和。"""
    known = [x.cost_usd for x in report.results if x.cost_usd is not None]
    if not known:
        return "unknown"
    total = sum(known)
    if len(known) < len(report.results):
        return f"{total:.4f} (部分 unknown)"
    return f"{total:.4f}"


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001 — metadata 采集失败不阻塞
        return "unknown"


def judge_prompt_sha256(judge_prompt: str | None = None) -> str:
    """judge 提示词 sha256（T3）：默认取 benchmark.judge.JUDGE_PROMPT 真实文本。

    惰性导入避免 report ↔ judge 的模块级耦合；导入失败（缺依赖）返回空串而非崩溃。
    """
    global _JUDGE_PROMPT_CACHE
    text = judge_prompt
    if text is None:
        if _JUDGE_PROMPT_CACHE is None:
            try:
                from benchmark.judge import JUDGE_PROMPT
                _JUDGE_PROMPT_CACHE = JUDGE_PROMPT
            except Exception:  # noqa: BLE001 — 指纹缺失不阻塞报告
                _JUDGE_PROMPT_CACHE = ""
        text = _JUDGE_PROMPT_CACHE
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _env_int(name: str, default: int) -> int:
    """env 整数解析：未设/非法 → default。"""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    """env 浮点解析：未设/非法 → None。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def setup_timeout_s() -> int:
    """setup_commands 生效超时秒数：KA_SETUP_TIMEOUT_S，缺省 60（与 runner 同源）。"""
    return _env_int("KA_SETUP_TIMEOUT_S", 60)


def llm_timeout_s() -> float | None:
    """LLM 生效超时：KA_LLM_TIMEOUT_S（未设 → None = SDK 默认；非法 → 120.0）。"""
    try:
        from agent.llm import _env_timeout
        return _env_timeout()
    except Exception:  # noqa: BLE001 — 依赖缺失时退回原始 env 解析
        return _env_float("KA_LLM_TIMEOUT_S")


def llm_max_retries() -> int | None:
    """LLM 生效重试：KA_LLM_MAX_RETRIES（未设 → None = SDK 默认；非法 → 2）。"""
    try:
        from agent.llm import _env_max_retries
        return _env_max_retries()
    except Exception:  # noqa: BLE001
        raw = (os.environ.get("KA_LLM_MAX_RETRIES") or "").strip()
        return _env_int("KA_LLM_MAX_RETRIES", 2) if raw else None


def effective_timeouts() -> dict:
    """生效超时/重试 env（T3）：值即运行期真实取值（含缺省规则）。"""
    return {
        "KA_SETUP_TIMEOUT_S": setup_timeout_s(),
        "KA_LLM_TIMEOUT_S": llm_timeout_s(),
        "KA_LLM_MAX_RETRIES": llm_max_retries(),
    }


def effective_iteration_limits(cases: list | None = None,
                               run_level: int | None = None) -> dict:
    """迭代上限（T3）：逐 case 的 max_iterations 优先，缺失时退回 run 级上限。"""
    per_case: dict = {}
    fallback: list = []
    for c in cases or []:
        cid = getattr(c, "id", "") or "?"
        v = getattr(c, "max_iterations", None)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            per_case[cid] = v
        else:
            fallback.append(cid)
            if isinstance(run_level, int) and not isinstance(run_level, bool):
                per_case[cid] = run_level
    return {"run_level": run_level, "per_case": per_case,
            "fallback_to_run_level": sorted(fallback)}


def actual_executor() -> str:
    """真实创建的执行器名（T3）：tools.docker_sandbox 模块级记录；未创建 → ""。"""
    try:
        from tools.docker_sandbox import get_actual_executor
        return get_actual_executor()
    except Exception:  # noqa: BLE001 — 记录缺失不阻塞报告
        return ""


def effective_sandbox_policy() -> dict:
    """有效沙箱策略 dict（T3）：image/cpus/memory/pids/network（docker_sandbox 提供）。"""
    try:
        from tools.docker_sandbox import get_effective_sandbox_policy
        return get_effective_sandbox_policy()
    except Exception:  # noqa: BLE001
        return {}


def llm_provider(llm: object | None = None) -> str:
    """供应商名（T3）：llm.provider → agent.llm 解析 → KA_LLM_PROVIDER 原值。"""
    prov = getattr(llm, "provider", None)
    if prov:
        return str(prov)
    try:
        from agent.llm import _resolve_provider_name
        return _resolve_provider_name(os.environ)
    except Exception:  # noqa: BLE001
        return os.environ.get("KA_LLM_PROVIDER", "") or ""


def enrich_run_metadata(meta: RunMetadata, llm: object | None = None,
                        cases: list | None = None,
                        run_max_iterations: int | None = None) -> RunMetadata:
    """就地补齐 T3 可比性字段（只写新键，不改既有键与语义）。"""
    meta.provider = llm_provider(llm)
    meta.base_url = str(getattr(llm, "base_url", "") or "")
    temp = getattr(llm, "temperature", None)
    if isinstance(temp, (int, float)) and not isinstance(temp, bool):
        meta.temperature = float(temp)
    else:
        meta.temperature = _env_float("KA_LLM_TEMPERATURE")
    mt = getattr(llm, "max_tokens", None)
    if isinstance(mt, int) and not isinstance(mt, bool):
        meta.max_tokens = mt
    else:
        meta.max_tokens = _env_int("KA_LLM_MAX_TOKENS", 0) or None
    meta.executor_actual = actual_executor() or meta.executor
    meta.sandbox_policy = effective_sandbox_policy()
    meta.iteration_limits = effective_iteration_limits(cases, run_max_iterations)
    meta.timeouts = effective_timeouts()
    meta.resolution_definition_version = RESOLUTION_DEFINITION_VERSION
    meta.side_effect_policy = SIDE_EFFECT_POLICY_READ_ONLY
    meta.judge_prompt_sha256 = judge_prompt_sha256()
    return meta


def current_metadata(model: str, executor: str, **params) -> RunMetadata:
    """构造当前运行 metadata（git commit / branch / 时间戳）。"""
    now = datetime.now().isoformat(timespec="seconds")
    return RunMetadata(
        run_id=now.replace("-", "").replace(":", "").replace("T", "-"),
        git_commit=_git(["rev-parse", "HEAD"]),
        branch=_git(["branch", "--show-current"]),
        model=model, timestamp=now, executor=executor, params=params,
    )


def save_json(report: BenchmarkReport, out_dir: str) -> str:
    """写 report.json（dataclass asdict 序列化）。"""
    import os
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2)
    return path


def save_markdown(report: BenchmarkReport, out_dir: str) -> str:
    """写 report.md：metadata + 核心指标 + 五类分组 + 明细表。"""
    import os
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "report.md")
    m = report.metadata
    lines = [
        f"# Benchmark Report {m.run_id}",
        "",
        f"- git: `{m.git_commit}` ({m.branch})",
        f"- model: `{m.model}` | executor: `{m.executor}`",
        f"- timestamp: {m.timestamp}",
        f"- params: {json.dumps(m.params, ensure_ascii=False)}",
        f"- provider: `{m.provider}` | base_url: `{m.base_url}`",
        f"- temperature: {m.temperature} | max_tokens: {m.max_tokens}",
        f"- executor: 声明 `{m.executor}` / 实际 `{m.executor_actual}`",
        f"- sandbox_policy: {json.dumps(m.sandbox_policy, ensure_ascii=False)}",
        f"- iteration_limits: {json.dumps(m.iteration_limits, ensure_ascii=False)}",
        f"- timeouts: {json.dumps(m.timeouts, ensure_ascii=False)}",
        f"- resolution_definition_version: `{m.resolution_definition_version}`",
        f"- side_effect_policy: `{m.side_effect_policy}`",
        f"- judge_prompt_sha256: `{m.judge_prompt_sha256}`",
        f"- fixture_commits: {json.dumps(m.fixture_commits, ensure_ascii=False)}",
        "",
        f"## 核心指标",
        "",
        f"**Issue Resolution Rate: {report.resolution_rate:.0%}** "
        f"({report.resolved}/{report.total})",
        "",
    ]
    err_n = sum(1 for r in report.results if r.status == "error")
    env_n = sum(1 for r in report.results if r.status == "environment_error")
    adj_denom = report.total - err_n - env_n
    adj_note = "（分母为 0，Adjusted 记 0.0）" if adj_denom == 0 else ""
    lines += [
        f"**Adjusted Resolution Rate: {report.resolution_rate_adjusted:.0%}** "
        f"({report.resolved}/{adj_denom})" + adj_note,
        "",
        "> 口径说明：Resolution Rate 分母为全部案例（raw，含 error / environment_error）；"
        "Adjusted Resolution Rate 分母排除执行异常（error）与基线环境错误"
        "（environment_error），仅统计实际运行 Agent 的案例，代表被测能力口径；"
        "分母为 0 时 Adjusted 记 0.0。",
        "",
        f"**环境错误: {env_n}**"
        "（基线测试失败，未运行 Agent）",
        "",
    ]
    cats = {}
    for r in report.results:
        cats.setdefault(r.category, []).append(r)
    lines.append("## 分类统计")
    lines.append("")
    lines.append("| 类别 | 任务数 | 解决 | 解决率 | 环境错误 |")
    lines.append("|------|--------|------|--------|----------|")
    for cat in sorted(cats):
        rs = cats[cat]
        n = len(rs)
        ok = sum(1 for r in rs if r.resolution)
        env = sum(1 for r in rs if r.status == "environment_error")
        lines.append(f"| {cat} | {n} | {ok} | {ok / n if n else 0:.0%} | {env} |")
    lines += ["", "## 明细", "",
              "| case_id | 类别 | 状态 | 测试 | 接受 | Judge | 迭代 | 耗时(s) | 成本($) | 失败摘要 | 交付一致 |",
              "|---------|------|------|------|------|-------|------|---------|---------|----------|---------|"]
    for r in report.results:
        summary = _truncate(r.test_error_summary, 80).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.case_id} | {r.category} | {r.status} | "
            f"{'PASS' if r.test_pass else 'FAIL'} | "
            f"{'PASS' if r.patch_acceptance else 'FAIL'} | "
            f"{r.judge_verdict} | {r.iteration_count} | {r.latency_s:.1f} | "
            f"{_fmt_cost(r.cost_usd)} | {summary} | "
            f"{'一致' if r.consistency else '不一致'} |")
    lines += ["", "## Judge 理由", ""]
    for r in report.results:
        lines.append(f"### {r.case_id} ({r.judge_verdict})")
        lines.append("")
        lines.append(r.judge_reason.replace("\n", "\n\n"))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def compare_reports(reports: list[BenchmarkReport]) -> str:
    """多轮报告对比 → Markdown 演进对比表（行=指标，列=run_id）。"""
    n = len(reports)
    total = sum(r.total for r in reports) or 1
    lines = ["# 版本对比（Benchmark 演进）", "",
             "| 指标 | " + " | ".join(r.metadata.run_id for r in reports) + " |",
             "|------|" + "------|" * n]
    def _adjusted(r: BenchmarkReport) -> str:
        """adjusted 口径（P2-6）：分母排除 error/environment_error 的 case（B11 回填 compare）。"""
        valid = [x for x in r.results if x.status not in ("error", "environment_error")]
        return f"{sum(1 for x in valid if x.resolution) / max(len(valid), 1):.2f}"             if valid else "n/a"

    rows = [
        ("Issue Resolution Rate",
         lambda r: f"{r.resolution_rate:.2f}"),
        ("Resolution Rate (adjusted)",
         _adjusted),
        ("Test Pass Rate",
         lambda r: f"{sum(1 for x in r.results if x.test_pass) / max(r.total, 1):.2f}"),
        ("Patch Acceptance Rate",
         lambda r: f"{sum(1 for x in r.results if x.patch_acceptance) / max(r.total, 1):.2f}"),
        ("平均迭代轮数",
         lambda r: f"{sum(x.iteration_count for x in r.results) / max(len(r.results), 1):.1f}"),
        ("平均耗时(s)",
         lambda r: f"{sum(x.latency_s for x in r.results) / max(len(r.results), 1):.1f}"),
        ("总成本($)",
         _total_cost),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(r) for r in reports) + " |")
    return "\n".join(lines) + "\n"