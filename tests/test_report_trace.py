"""trace 导出测试：Fake 数据源 + 摘要组装 + 报告生成。"""
import json
from pathlib import Path

import pytest

from tools.report_trace import (TraceError, TraceSummary, fetch_trace,
                                save_trace_report, trace_report_md)


def _fake_summary():
    return TraceSummary(
        trace_id="abc123", task="修复 bug",
        total_latency_s=123.5, tokens_prompt=5000, tokens_completion=2000,
        cost_usd=0.05, tool_calls=12, errors=["连接超时"], retries=2,
        test_results=[{"test": "test_schedule.py", "passed": 10,
                       "failed": 0, "error": 0, "total": 10, "duration": 0.5}],
        steps=[{"name": "graph.plan", "latency_s": 1.2},
               {"name": "tool.execute", "latency_s": 3.4}])


def test_fetch_trace_with_fake_source(monkeypatch):
    """fetch_trace 用 Fake source 组装摘要。"""
    import tools.report_trace as rt
    captured = {}

    def fake_sdk_fetch(trace_id):
        captured["trace_id"] = trace_id
        return _fake_summary()

    monkeypatch.setattr(rt, "_fetch_via_sdk", fake_sdk_fetch)
    s = fetch_trace("abc123")
    assert isinstance(s, TraceSummary)
    assert s.trace_id == "abc123"
    assert s.tool_calls == 12


def test_fetch_trace_sdk_unavailable_falls_back(monkeypatch):
    """SDK 不可用时回退 ClickHouse 直查；两者不可用 → TraceError。"""
    import tools.report_trace as rt

    def raise_err(trace_id):
        raise rt.TraceError("SDK 不可用")

    monkeypatch.setattr(rt, "_fetch_via_sdk", raise_err)
    monkeypatch.setattr(rt, "_fetch_via_clickhouse",
                        lambda trace_id, env: _fake_summary())
    s = fetch_trace("abc123")
    assert s.trace_id == "abc123"

    monkeypatch.setattr(rt, "_fetch_via_clickhouse",
                        lambda trace_id, env: (_ for _ in ()).throw(
                            rt.TraceError("ClickHouse 不可达")))
    with pytest.raises(TraceError):
        fetch_trace("abc123")


def test_save_report(tmp_path):
    s = _fake_summary()
    path = save_trace_report(s, str(tmp_path))
    assert Path(path).name == "report.md"
    json_path = Path(path).parent / "report.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["trace_id"] == "abc123"
    md = Path(path).read_text(encoding="utf-8")
    assert "abc123" in md and "Tool Calls" in md and "12" in md

def test_sdk_summary_no_nested_double_count():
    """IM-14：SDK 路径总耗时 = trace 全跨度（max end - min start），嵌套 span 不双计。"""
    import datetime as dt
    from types import SimpleNamespace

    from tools.report_trace import _summarize_trace

    t0 = dt.datetime(2026, 8, 30, 0, 0, 0)
    obs = [
        SimpleNamespace(name="issue.run", latency=10.0,
                        start_time=t0, end_time=t0 + dt.timedelta(seconds=10),
                        metadata=None),
        SimpleNamespace(name="graph.execute", latency=6.0,
                        start_time=t0 + dt.timedelta(seconds=2),
                        end_time=t0 + dt.timedelta(seconds=8), metadata=None),
    ]
    s = _summarize_trace("t", SimpleNamespace(name="task", observations=obs))
    assert s.total_latency_s == 10.0   # 而非父子相加的 16.0
