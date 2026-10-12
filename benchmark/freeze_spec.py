"""W11-α T5（Coding 侧）：冻结 case set 与 fixture，并生成 BenchmarkSpec MANIFEST。

母 Spec §24 要求 Coding BenchmarkSpec 至少固定：case set hash / repository commit(fixture state) /
model(provider) / executor and sandbox policy / iteration limit and timeout / judge version /
resolution definition / side-effect policy / pre-registered quality tolerances。

顺序不可颠倒：先冻结本清单，再跑基线。

用法：
  python benchmark/freeze_spec.py --version w11a-v1 --resolve-head
  python benchmark/freeze_spec.py --version w11a-v1 --commit <sha>
  python benchmark/freeze_spec.py --version w11a-v1 --resolve-head --force --supersede-reason "..."

- `--resolve-head`：联网解析每个被测仓库当前的默认分支 HEAD，作为 fixture 提交（冻结时钉住）。
- `--commit`：对全部 case 使用同一个给定提交（离线可复现）。
- 已存在的冻结目录拒绝覆盖（除非 --force + --supersede-reason）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "benchmark" / "cases"
FROZEN_ROOT = ROOT / "benchmark" / "frozen"

#: 预登记容差（不得跑完再定）。与 Wiki 侧同一套纪律，数值按 Coding 口径表达。
PREREGISTERED_TOLERANCES = {
    "deterministic_metrics": "exact_match",
    "deterministic_metrics_list": ["test_pass", "patch_acceptance"],
    "resolution_rate_delta_max": 0.03,
    "per_case_verdict_agreement_min": 0.80,
    "note": "仅 5 个 case：±3% 的分辨率等价于『两次运行的 resolved 计数必须相同』；"
            "judge 与 agent 同模型（自评），verdict 一致率按 80% 预登记并在报告中披露。",
    "cost": "MODEL_PRICE_USD_PER_1K 为空 ⇒ cost_usd=null / cost_source=unknown（不得当 0 用）",
    "safety_violations_max": 0,
    "declared_at": "2026-10-12",
    "declared_by": "W11-α Spec §3.5（用户批准 2026-10-12）",
}

DECLARED_GAPS = {
    "temperature_max_tokens": "LLMClient.chat 不发送 temperature/max_tokens ⇒ 记为 null（provider 默认），"
                              "冻结真实采样参数需改 agent/llm.py（超出本窗口范围）",
    "docker_actual_executor": "无 Docker 环境下 sandbox_policy.executor 只能回显声明值；"
                              "docker 分支的实际执行器记录未在本机验证",
    "pricing_table": "MODEL_PRICE_USD_PER_1K 为空，成本恒 unknown（不再是假 0）",
    "fixture_repo_state": "fixture 提交由 cases.lock.json 钉住并由 runner 真正 checkout（T3b）；"
                          "setup_commands 的包版本仍由网络解析，未逐个 pin",
}


def case_set_hash() -> tuple[str, list[str], dict]:
    """case 文件 + gold diff 的排序后哈希清单（内容哈希，与文件顺序无关）。"""
    files: list[Path] = []
    for pattern in ("*.json", "gold/*.diff"):
        files.extend(sorted(CASES_DIR.glob(pattern)))
        for sub in sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and p.name != "gold"):
            files.extend(sorted(sub.glob(pattern)))
    entries = {}
    for f in sorted(set(files)):
        rel = f.relative_to(ROOT).as_posix()
        entries[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    blob = json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), list(entries), entries


def resolve_head(repo: str) -> str:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits", "--jq", ".[0].sha"],
        capture_output=True, text=True, timeout=120)
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结 Coding benchmark case set 与 fixture")
    ap.add_argument("--version", default="w11a-v1")
    ap.add_argument("--commit", default="")
    ap.add_argument("--resolve-head", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--supersede-reason", default="")
    args = ap.parse_args()

    from benchmark.loader import load_cases
    from benchmark import report as rep

    cases = load_cases(str(CASES_DIR))
    repos = sorted({c.repository for c in cases})
    if not repos:
        print("错误：没有 case", file=sys.stderr)
        return 1

    lock: dict[str, str] = {}
    if args.commit:
        lock = {c.id: args.commit for c in cases}
    elif args.resolve_head:
        heads = {r: resolve_head(r) for r in repos}
        for r, sha in heads.items():
            if not sha:
                print(f"错误：无法解析 {r} 的默认分支 HEAD（联网/gh 认证）", file=sys.stderr)
                return 2
        # 同一仓库的 case 共享同一提交
        lock = {c.id: heads[c.repository] for c in cases}
    else:
        print("错误：请给出 --resolve-head 或 --commit（不得在未固定 fixture 的情况下冻结）", file=sys.stderr)
        return 3

    out_dir = FROZEN_ROOT / args.version
    if out_dir.exists() and not args.force:
        print(f"错误：{out_dir} 已存在（冻结件不可覆盖）", file=sys.stderr)
        return 4
    if args.force and not args.supersede_reason:
        print("错误：--force 需给出 --supersede-reason", file=sys.stderr)
        return 5
    out_dir.mkdir(parents=True, exist_ok=True)

    cases_hash, files, hashes = case_set_hash()
    (out_dir / "cases.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    judge_prompt = None
    try:
        from benchmark.judge import JUDGE_PROMPT  # type: ignore
        judge_prompt = JUDGE_PROMPT
    except Exception:  # noqa: BLE001
        pass

    manifest = {
        "spec_version": args.version,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "harness": {
            "commit": rep._git(["rev-parse", "HEAD"]),
            "branch": rep._git(["branch", "--show-current"]),
            "repo": str(ROOT),
        },
        "case_set": {
            "hash": cases_hash,
            "case_count": len(cases),
            "case_ids": sorted(c.id for c in cases),
            "files": hashes,
        },
        "fixture": {
            "repositories": repos,
            "lock_file": (out_dir / "cases.lock.json").relative_to(ROOT).as_posix(),
            "commits": lock,
            "pinned_by": "cases.lock.json（loader 解析，runner 真正 checkout —— T3b）",
        },
        "models": {
            "provider": rep.llm_provider(),
            "model": os.environ.get("COMMAND_GOAT_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek/deepseek-v4.1-flash",
            "base_url": os.environ.get("COMMAND_GOAT_BASE_URL", ""),
            "temperature": rep._env_float("KA_LLM_TEMPERATURE"),
            "max_tokens": rep._env_int("KA_LLM_MAX_TOKENS", 0) or None,
            "judge_model": "与 agent 同模型（自评）",
        },
        "executor": {
            "declared": os.environ.get("KA_EXECUTOR", "local"),
            "actual_recorded": rep.actual_executor(),
            "sandbox_policy": rep.effective_sandbox_policy(),
            "note": "actual_recorded 由 tools/docker_sandbox 在执行时写入；运行后应与 declared 一致",
        },
        "iteration_and_timeouts": {
            "iteration_limits": rep.effective_iteration_limits(cases, None),
            "timeouts": rep.effective_timeouts(),
        },
        "judge": {
            "prompt_sha256": rep.judge_prompt_sha256(judge_prompt),
            "self_judging": True,
        },
        "resolution_definition": {"version": rep.RESOLUTION_DEFINITION_VERSION,
                                  "definition": "test_pass AND judge PASS"},
        "side_effect_policy": rep.SIDE_EFFECT_POLICY_READ_ONLY,
        "preregistered_tolerances": PREREGISTERED_TOLERANCES,
        "declared_gaps": DECLARED_GAPS,
        "supersede_reason": args.supersede_reason or None,
    }
    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(f"case set hash : {cases_hash}")
    print(f"fixture       : {json.dumps(lock, ensure_ascii=False)}")
    print(f"lock          : {out_dir / 'cases.lock.json'}")
    print(f"manifest      : {manifest_path}")
    print(f"case 数        : {len(cases)} · 仓库 {repos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
