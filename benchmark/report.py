"""评测报告：run metadata、JSON/Markdown 报告与多轮对比。"""
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime


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
    cost_usd: float
    diff: str
    errors: list[str] = field(default_factory=list)
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


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001 — metadata 采集失败不阻塞
        return "unknown"


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
            f"{r.cost_usd:.4f} | {summary} | "
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
         lambda r: f"{sum(x.cost_usd for x in r.results):.4f}"),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(r) for r in reports) + " |")
    return "\n".join(lines) + "\n"