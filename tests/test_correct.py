# tests/test_correct.py
"""知识抽查编排测试：run_audit 端到端（fixture kg_mini）+ 报告生成 + 零写回。"""
import hashlib
import json
from pathlib import Path

import pytest

from agent.correct import AuditReport, print_audit_summary, run_audit

FIXTURE = Path(__file__).parent / "fixtures" / "kg_mini"
WIKI = FIXTURE  # fixture 根同时含 data/extractions 与 data/stories


def _dir_hashes(path: Path) -> dict[str, str]:
    """目录内所有文件内容哈希（用于零写回断言）。"""
    out = {}
    for p in sorted(path.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(path))] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


def test_run_audit_end_to_end(tmp_path):
    report = run_audit(str(WIKI), ratio=1.0, seed=42, out_dir=str(tmp_path))
    assert isinstance(report, AuditReport)
    assert report.total == 14
    assert report.sample_count == 14  # ratio=1.0 全量
    assert 0 <= report.reliability["rate"] <= 1
    assert 0 <= report.utility["rate"] <= 1
    assert report.stats["by_kind"]["concept"] == 5
    # 《三山谈》应出现在死数据清单（无 source_records + 无引用）
    names = {d["name"] for d in report.dead_list}
    assert "《三山谈》" in names


def test_run_audit_writes_reports_and_zero_writeback(tmp_path):
    before = _dir_hashes(WIKI / "data")
    report = run_audit(str(WIKI), ratio=0.5, seed=1, out_dir=str(tmp_path))
    after = _dir_hashes(WIKI / "data")
    assert before == after  # 知识数据零写回
    mds = list(tmp_path.glob("audit_report_*.md"))
    jsons = list(tmp_path.glob("audit_samples_*.jsonl"))
    assert len(mds) == 1 and len(jsons) == 1
    md = mds[0].read_text(encoding="utf-8")
    assert "来源可靠性" in md and "内容实用性" in md and "死数据清单" in md
    rows = [json.loads(l) for l in jsons[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == report.sample_count
    assert all("reliability" in r and "utility" in r for r in rows)


def test_run_audit_missing_extractions_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_audit(str(tmp_path))


def test_print_audit_summary(capsys):
    report = run_audit(str(WIKI), ratio=0.5, seed=42)
    print_audit_summary(report)
    out = capsys.readouterr().out
    assert "知识抽查完成" in out
    assert "来源可靠性" in out
    assert "内容实用性" in out