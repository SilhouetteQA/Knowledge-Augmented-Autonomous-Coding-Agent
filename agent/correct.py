# agent/correct.py
"""知识抽查编排：run_audit —— 三次提取产物 2% 分层抽样 + 可靠性/实用性检查 + 报告。

对应 spec《2026-08-25-knowledge-correction.md》验收标准 6/7：
- 纯规则层（无 LLM），dry-run 语义：对知识数据零写回，仅输出报告到 out_dir。
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from tools.knowledge_audit import (
    build_name_index,
    build_reference_map,
    check_reliability,
    check_utility,
    inventory_stats,
    load_inventory,
    sample_entries,
)

WIKI_LAYOUT = ("data/extractions", "data/stories")


@dataclass
class AuditReport:
    """抽查汇总结果。"""
    total: int
    sample_count: int
    stats: dict
    reliability: dict          # rate / reliable / unreliable
    utility: dict              # rate / dead_count / used
    dead_list: list[dict]      # 死数据样本（《三山谈》类）
    samples: list[dict]        # 样本明细
    out_dir: str = ""


def run_audit(wiki_dir: str, ratio: float = 0.02, seed: int = 42,
              out_dir: str | None = None) -> AuditReport:
    """执行抽查：盘点 → 分层抽样 ≥ratio → 逐样本可靠性/实用性 → 汇总与报告。"""
    extractions = Path(wiki_dir) / "data" / "extractions"
    stories = Path(wiki_dir) / "data" / "stories"
    if not extractions.exists():
        raise FileNotFoundError(f"extractions 目录不存在: {extractions}")

    entries = load_inventory(str(extractions))
    if not entries:
        raise ValueError(f"extractions 为空: {extractions}")

    stats = inventory_stats(entries)
    samples, meta = sample_entries(entries, ratio, seed)
    index = build_name_index(entries)
    refs = build_reference_map(entries)

    rows: list[dict] = []
    for e in samples:
        rel = check_reliability(e, str(stories))
        util = check_utility(e, index, refs)
        rows.append({
            "id": e.id, "kind": e.kind, "name": e.name,
            "origin_file": e.origin_file, "aliases": list(e.aliases),
            "reliability": rel, "utility": util,
        })

    n = max(1, len(rows))
    reliable = sum(1 for r in rows if r["reliability"]["verdict"] == "reliable")
    dead = [r for r in rows if r["utility"].get("dead")]
    report = AuditReport(
        total=meta["total"], sample_count=meta["sample_count"], stats=stats,
        reliability={"rate": round(reliable / n, 4), "reliable": reliable,
                     "unreliable": n - reliable},
        utility={"rate": round(1 - len(dead) / n, 4), "dead_count": len(dead),
                 "used": n - len(dead)},
        dead_list=[{"id": r["id"], "kind": r["kind"], "name": r["name"],
                    "origin_file": r["origin_file"]} for r in dead],
        samples=rows,
    )
    if out_dir:
        # 回填 out_dir（M3：此前恒空，print_audit_summary 的"报告目录"永不打印）
        report.out_dir = _write_report(report, meta, out_dir)
    return report


def _write_report(report: AuditReport, meta: dict, out_dir: str) -> str:
    """写 audit_report.md + audit_samples.jsonl，返回 md 路径。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out / f"audit_report_{ts}.md"
    jsonl_path = out / f"audit_samples_{ts}.jsonl"

    lines = [
        "# 知识抽查报告（三次提取产物）",
        "",
        f"- 时间: {ts}",
        f"- 抽样参数: ratio={meta['ratio']} seed={meta['seed']}",
        f"- 条目总数: {report.total}，抽样 {report.sample_count} 条"
        f"（占比 {report.sample_count / max(1, report.total):.2%}）",
        "",
        "## 统计",
        "",
        f"- 来源可靠性: {report.reliability['rate']:.2%}"
        f"（reliable {report.reliability['reliable']} / unreliable {report.reliability['unreliable']}）",
        f"- 内容实用性: {report.utility['rate']:.2%}"
        f"（used {report.utility['used']} / dead {report.utility['dead_count']}）",
        "",
        "## 按类型统计",
        "",
        "| kind | 总数 |",
        "|---|---|",
    ]
    for kind, cnt in sorted(report.stats["by_kind"].items()):
        lines.append(f"| {kind} | {cnt} |")

    lines += ["", "## 死数据清单（无任何引用）", ""]
    if report.dead_list:
        lines.append("| id | kind | name | origin_file |")
        lines.append("|---|---|---|---|")
        for d in report.dead_list:
            lines.append(f"| {d['id']} | {d['kind']} | {d['name']} | {d['origin_file']} |")
    else:
        lines.append("（无）")

    lines += ["", "## 样本明细（前 50 条）", ""]
    lines.append("| id | kind | name | 来源判定 | 实用性 |")
    lines.append("|---|---|---|---|---|")
    for r in report.samples[:50]:
        lines.append(f"| {r['id']} | {r['kind']} | {r['name']} | "
                     f"{r['reliability']['verdict']} | "
                     f"{'dead' if r['utility'].get('dead') else 'used'} |")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in report.samples:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(md_path)


def print_audit_summary(report: AuditReport) -> None:
    """控制台摘要打印（main.py --correct 使用）。"""
    print(f"知识抽查完成: 总数 {report.total}，抽样 {report.sample_count} 条")
    print(f"  来源可靠性: {report.reliability['rate']:.2%}"
          f"（{report.reliability['reliable']}/{report.reliability['unreliable']}）")
    print(f"  内容实用性: {report.utility['rate']:.2%}"
          f"（used {report.utility['used']} / dead {report.utility['dead_count']}）")
    if report.dead_list:
        print("  死数据示例:")
        for d in report.dead_list[:5]:
            print(f"    - [{d['kind']}] {d['name']}（{d['origin_file']}）")
    if report.out_dir:
        print(f"  报告目录: {report.out_dir}")