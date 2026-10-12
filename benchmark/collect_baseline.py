"""W11-α：把一次 benchmark 运行的产物收束为不可变基线目录 + SHA256SUMS。

为什么需要它：`/output/` 被 .gitignore 忽略，而冻结基线必须进版本控制才谈得上"不可变"。
本脚本把一次运行的报告与冻结清单复制到受控目录 `benchmark/baselines/<name>/`，
并把"这次运行实际是什么配置"抽成 run-metadata.json（含 fixture pinned/actual、声明执行器/实际执行器）。

用法：
  python benchmark/collect_baseline.py --run-dir output/benchmark/<run_id> --name w11a-v1
  python benchmark/collect_baseline.py --run-dir output/benchmark/<run_id> --name w11a-v1-run2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINES = ROOT / "benchmark" / "baselines"

METADATA_KEYS = (
    "run_id", "git_commit", "branch", "model", "timestamp", "executor",
    "provider", "base_url", "temperature", "max_tokens", "executor_actual",
    "sandbox_policy", "iteration_limits", "timeouts",
    "resolution_definition_version", "side_effect_policy", "judge_prompt_sha256",
    "fixture_commits", "params",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="收集 benchmark 基线产物")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--spec", default="benchmark/frozen/w11a-v1/MANIFEST.json")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    report_json = run_dir / "report.json"
    if not report_json.is_file():
        print(f"错误：找不到 {report_json}", file=sys.stderr)
        return 1

    out = BASELINES / args.name
    if out.exists() and not args.force:
        print(f"错误：{out} 已存在（基线不可覆盖）", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    report = json.loads(report_json.read_text(encoding="utf-8"))
    meta = report.get("metadata", {})
    shutil.copy2(report_json, out / "report.json")
    if (run_dir / "report.md").is_file():
        shutil.copy2(run_dir / "report.md", out / "report.md")

    spec_src = ROOT / args.spec
    if spec_src.is_file():
        shutil.copy2(spec_src, out / "benchmark-spec.json")

    picked = {k: meta.get(k) for k in METADATA_KEYS if k in meta}
    mismatches = [
        cid for cid, v in (meta.get("fixture_commits") or {}).items()
        if v.get("pinned") and v.get("actual") and v["pinned"] != v["actual"]
    ]
    collected = {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_dir": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
        "summary": {
            "total": report.get("total"),
            "resolved": report.get("resolved"),
            "resolution_rate": report.get("resolution_rate"),
            "resolution_rate_adjusted": report.get("resolution_rate_adjusted"),
        },
        "metadata": picked,
        "fixture_pin_mismatches": mismatches,
        "cases": [
            {
                "case_id": r.get("case_id"),
                "status": r.get("status"),
                "resolution": r.get("resolution"),
                "test_pass": r.get("test_pass"),
                "judge_verdict": r.get("judge_verdict"),
                "iteration_count": r.get("iteration_count"),
                "latency_s": r.get("latency_s"),
                "cost_usd": r.get("cost_usd"),
                "cost_source": r.get("cost_source"),
                "repository_commit_pinned": r.get("repository_commit_pinned"),
                "repository_commit_actual": r.get("repository_commit_actual"),
                "errors": r.get("errors", []),
            }
            for r in report.get("results", [])
        ],
    }
    (out / "run-metadata.json").write_text(
        json.dumps(collected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    readme = [
        f"# Coding baseline — {args.name}",
        "",
        f"- 源运行目录（未入库，`/output/` 被忽略）：`{collected['source_run_dir']}`",
        f"- 收集时间：{collected['collected_at']}",
        f"- harness: `{meta.get('git_commit')}` ({meta.get('branch')})",
        f"- 结果：{collected['summary']['resolved']}/{collected['summary']['total']}"
        f"（raw {collected['summary']['resolution_rate']:.0%} / adjusted "
        f"{collected['summary']['resolution_rate_adjusted']:.0%}）",
        f"- fixture pin 不匹配：{mismatches or '无'}",
        "",
        "## 复算校验",
        "",
        "```powershell",
        "Get-Content SHA256SUMS | ForEach-Object { $h,$f = $_ -split '  ',2;",
        "  if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $h) { \"MISMATCH $f\" } }",
        "```",
        "",
        "## 对应 Spec",
        "",
        "`docs/specs/2026-10-12-pre-extraction-benchmark-freeze.md` §3.1（Coding）/ §3.4（产物）/ §3.5（容差）",
        "",
    ]
    (out / "README.md").write_text("\n".join(readme), encoding="utf-8")

    sums = []
    for f in sorted(out.iterdir()):
        if f.name == "SHA256SUMS" or not f.is_file():
            continue
        sums.append(f"{sha256_file(f)}  {f.name}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")

    print(f"基线目录: {out}")
    print(f"结果: {collected['summary']['resolved']}/{collected['summary']['total']}"
          f" (adjusted {collected['summary']['resolution_rate_adjusted']:.0%})")
    print(f"fixture pin 不匹配: {mismatches or '无'}")
    for line in sums:
        print("  " + line[:16] + "  " + line.split("  ", 1)[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
