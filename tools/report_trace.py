"""Trace 导出与汇总报告：从 Langfuse 数据源（SDK 或 ClickHouse）拉取单任务 trace → 摘要 → JSON/Markdown。

数据源说明（W6 实测）：本机 Langfuse 为 v4 events_only 部署，经典读接口不可用；
优先 SDK（若 events_only 下可用），回退 ClickHouse 直查 events_core（clickhouse-connect）。
"""
import json
import os
from dataclasses import asdict, dataclass


class TraceError(Exception):
    """trace 获取失败（数据源不可用/无权限等）。"""


@dataclass
class TraceSummary:
    """单任务 trace 汇总。"""
    trace_id: str
    task: str
    total_latency_s: float
    tokens_prompt: int
    tokens_completion: int
    cost_usd: float
    tool_calls: int
    errors: list[str]
    retries: int
    test_results: list[dict]
    steps: list[dict]


def _clickhouse_env(env_file: str | None) -> dict:
    """读取 ClickHouse 凭据（docker/langfuse/.env 或环境变量）。"""
    result: dict = {}
    if env_file and os.path.isfile(env_file):
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("CLICKHOUSE_PASSWORD="):
                        result["CLICKHOUSE_PASSWORD"] = line.split("=", 1)[1]
        except OSError:
            result["CLICKHOUSE_PASSWORD"] = os.environ.get("CLICKHOUSE_PASSWORD", "")
    else:
        result["CLICKHOUSE_PASSWORD"] = os.environ.get("CLICKHOUSE_PASSWORD", "")
    result.setdefault("CLICKHOUSE_PASSWORD", "clickhouse")  # 仅本地 compose 默认；生产必须显式注入
    return result


def _fetch_via_sdk(trace_id: str) -> TraceSummary:
    """经 Langfuse SDK 读 trace（若 events_only 部署下可用）。"""
    try:
        from langfuse import get_client
        client = get_client()
        trace = client.trace.get(trace_id)
    except Exception as e:  # noqa: BLE001 —— 数据源不可用转 TraceError
        raise TraceError(f"SDK 读 trace 失败: {e}") from e
    # events_only 下 get 可能 404/空——统一转 TraceError 让上层回退
    if trace is None:
        raise TraceError("SDK trace.get 返回空（可能 events_only 不支持经典读）")
    return _summarize_trace(trace_id, trace)


def _fetch_via_clickhouse(trace_id: str, env_file: str | None) -> TraceSummary:
    """经 ClickHouse 直查 events_core 汇总（按 trace_id 过滤）。

    实际 schema（W6/W7 冒烟确认，Langfuse v4 events_only 直写表）：
      - 过滤列是 trace_id（非 id）；observation 名在 name、类别在 type
      - 无 value JSON 列：usage/total_cost 为独立列，metadata 为
        metadata_names/metadata_values 并行数组
      - is_app_root=1 标识 trace 根 observation（用于 task 取名）
    """
    try:
        from clickhouse_connect import get_client as ch_get
    except ImportError as e:
        raise TraceError("clickhouse-connect 未安装（数据源不可用）") from e
    env = _clickhouse_env(env_file)
    try:
        ch = ch_get(host="127.0.0.1", port=8123, username="clickhouse",
                    password=env["CLICKHOUSE_PASSWORD"])
        rows = ch.query(
            "SELECT type, name, start_time, end_time, usage_details, "
            "total_cost, status_message, level, metadata_names, "
            "metadata_values, is_app_root FROM events_core "
            "WHERE trace_id = {t:String} AND is_deleted = 0",
            parameters={"t": trace_id})
    except Exception as e:  # noqa: BLE001 —— 连接/查询失败转 TraceError
        raise TraceError(f"ClickHouse 查询失败: {e}") from e
    return _summarize_events(trace_id, rows.result_rows or [])


def _summarize_trace(trace_id: str, trace: object) -> TraceSummary:
    """SDK trace 对象 → 摘要（实施时按 SDK 4.14 实际字段打点）。

    IM-14：总耗时与 ClickHouse 路径同口径 = trace 全跨度（max end - min start），
    嵌套 span 不再父子双计（此前直接累加每条 latency，totals 偏大且与 CH 路径
    不可比）；SDK 读口无时间戳字段时诚实记 0。
    """
    obs = getattr(trace, "observations", None) or []
    steps = []
    tool_calls = errors = retries = 0
    tokens_prompt = tokens_completion = 0
    test_results: list[dict] = []
    starts: list = []
    ends: list = []
    task = getattr(trace, "name", "") or trace_id
    for o in obs:
        name = getattr(o, "name", "") or ""
        d = getattr(o, "latency", None)
        steps.append({"name": name,
                      "latency_s": float(d) if d is not None else 0.0})
        st = getattr(o, "start_time", None) or getattr(o, "startTime", None)
        en = getattr(o, "end_time", None) or getattr(o, "endTime", None)
        if st is not None and en is not None:
            starts.append(st)
            ends.append(en)
        if name.startswith("tool"):
            tool_calls += 1
        meta = getattr(o, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("retry"):
            retries += 1
    latency = 0.0
    if starts and ends:
        latency = (max(ends) - min(starts)).total_seconds()
    return TraceSummary(
        trace_id=trace_id, task=task, total_latency_s=latency,
        tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
        cost_usd=0.0, tool_calls=tool_calls, errors=errors,
        retries=retries, test_results=test_results, steps=steps,
    )


def _metadata_dict(names: list, values: list) -> dict:
    """metadata_names / metadata_values 并行数组 → dict（冒烟实测结构）。"""
    if not names:
        return {}
    return dict(zip(names, values))


def _as_int(v) -> int:
    """字符串/数值 → int（metadata_values 为 Array(String)，需容错）。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _as_float(v) -> float:
    """字符串/Decimal/数值 → float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _summarize_events(trace_id: str, rows: list) -> TraceSummary:
    """events_core 行 → 摘要”。

    rows 元组列序与 _fetch_via_clickhouse 的 SELECT 一致：
    (type, name, start_time, end_time, usage_details, total_cost,
     status_message, level, metadata_names, metadata_values, is_app_root)
    """
    steps: list[dict] = []
    tool_calls = 0
    test_results: list[dict] = []
    errors: list[str] = []
    retries = 0
    task = trace_id
    tokens_prompt = tokens_completion = 0
    cost_usd = 0.0
    starts: list = []
    ends: list = []
    for row in rows:
        (event_type, name, start_time, end_time, usage, total_cost,
         status, level, meta_names, meta_values, is_root) = row
        meta = _metadata_dict(meta_names, meta_values)
        d = None
        if start_time is not None and end_time is not None:
            d = (end_time - start_time).total_seconds()
            starts.append(start_time)
            ends.append(end_time)
        name = name or event_type
        steps.append({"name": name,
                      "latency_s": float(d) if d is not None else 0.0})
        if is_root:
            task = name
        usage = usage or {}
        tokens_prompt += _as_int(usage.get("input"))
        tokens_completion += _as_int(usage.get("output"))
        cost_usd += _as_float(total_cost)
        if status or (level or "").upper() in ("ERROR", "FATAL"):
            errors.append(f"{name}: {status or level}")
        if name.startswith("tool."):
            tool_calls += 1
        # retry 观测本身计 1 次；issue.run 根 span 记录 retry_count（W7 Task 3）
        if name == "retry" or meta.get("retry"):
            retries += 1
        retries += _as_int(meta.get("retry_count"))
        if name.startswith("test."):
            test_results.append({
                "test": name,
                "passed": _as_int(meta.get("passed", 0)),
                "failed": _as_int(meta.get("failed", 0)),
                "error": _as_int(meta.get("error", 0)),
                "total": _as_int(meta.get("total", 0)),
                "duration": _as_float(meta.get("duration", 0.0)),
            })
    # 总耗时 = trace 全跨度（max end - min start），避免嵌套 span 重复累加
    if starts and ends:
        latency = (max(ends) - min(starts)).total_seconds()
    return TraceSummary(
        trace_id=trace_id, task=task, total_latency_s=latency,
        tokens_prompt=tokens_prompt, tokens_completion=tokens_completion,
        cost_usd=cost_usd, tool_calls=tool_calls, errors=errors,
        retries=retries, test_results=test_results, steps=steps,
    )


def fetch_trace(trace_id: str, *, clickhouse_env: str | None = None) -> TraceSummary:
    """获取 trace 摘要：优先 SDK，回退 ClickHouse；均不可用 → TraceError。"""
    try:
        return _fetch_via_sdk(trace_id)
    except TraceError:
        return _fetch_via_clickhouse(trace_id, clickhouse_env)


def trace_report_md(s: TraceSummary) -> str:
    """Markdown 汇总报告文本。"""
    lines = [
        f"# Trace Report {s.trace_id}",
        "",
        f"- task: `{s.task}`",
        f"- 总耗时: {s.total_latency_s:.1f}s | tokens: {s.tokens_prompt}/{s.tokens_completion}",
        f"- 成本: ${s.cost_usd:.4f} | Tool Calls: {s.tool_calls} | 重试: {s.retries}",
        "",
        "## 步骤",
        "",
        "| 名称 | 耗时(s) |",
        "|------|---------|",
    ]
    for st in s.steps:
        lines.append(f"| {st.get('name', '')} | {st.get('latency_s', 0.0):.1f} |")
    lines += ["", "## 测试结果", ""]
    for t in s.test_results:
        lines.append(f"- {t.get('test', '')}: {t.get('passed', 0)} passed / "
                     f"{t.get('failed', 0)} failed / {t.get('error', 0)} error")
    lines += ["", "## 错误", ""]
    for e in s.errors:
        lines.append(f"- {e}")
    return "\n".join(lines) + "\n"


def save_trace_report(s: TraceSummary, out_dir: str) -> str:
    """写 report.json + report.md，返回 report.md 路径。"""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, ensure_ascii=False, indent=2)
    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(trace_report_md(s))
    return md_path