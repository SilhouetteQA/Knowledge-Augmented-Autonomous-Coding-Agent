# tests/test_kg_audit_integration.py
"""知识抽查集成测试：真实兄弟项目三次提取产物 2% 抽查。

跳过条件：ARKNIGHTS_WIKI_DIR 未配置或 data/extractions 不存在。
"""
import os
from pathlib import Path

import pytest

from agent.correct import run_audit

_WIKI = os.environ.get("ARKNIGHTS_WIKI_DIR", "")


def _skip_reason() -> str | None:
    if not _WIKI:
        return "ARKNIGHTS_WIKI_DIR 未配置"
    if not (Path(_WIKI) / "data" / "extractions").exists():
        return "data/extractions 不存在"
    return None


pytestmark = pytest.mark.kg


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_real_2pct_audit(tmp_path):
    """验收测试：真实数据 2% 抽查样本量达标、统计自洽、报告生成。"""
    report = run_audit(_WIKI, ratio=0.02, seed=42, out_dir=str(tmp_path))
    assert report.sample_count >= max(1, round(report.total * 0.02))
    assert report.sample_count <= report.total
    assert len(report.samples) == report.sample_count
    assert 0 <= report.reliability["rate"] <= 1
    assert 0 <= report.utility["rate"] <= 1
    assert report.reliability["reliable"] + report.reliability["unreliable"] \
        == report.sample_count
    assert report.utility["dead_count"] + report.utility["used"] \
        == report.sample_count
    assert len(list(tmp_path.glob("audit_report_*.md"))) == 1
    assert len(list(tmp_path.glob("audit_samples_*.jsonl"))) == 1