"""Spec 13 — L3 fresh smoke 驱动（`scripts/contracts/run_l3_smoke.py`）测试。

覆盖 A2 修复的三个 blocker：

```text
B1  run-summary.json 从**已落盘证据**推导写出（键集 = validate_local.RUN_SUMMARY_KEYS）
B2  驱动真实业务命令（main.py --benchmark）而不是只校验
B3  预登记 case_ids 从 benchmark/cases 解析；缺失即 SPEC_INCOMPLETE（退出码 2）
```

测试纪律：**全部离线**。业务路径通过 fixture seam
（`KA_L3_SMOKE_BUSINESS_CMD`）替换为一个写**合成 EvidenceRecord** 的脚本 —— 合成
证据只用于验证 driver 的读回/汇总/闭合判定与 gate 集成，绝不冒充真实 L3 观察
（Master §13.4："Fixture 可以证明合法性，但不能冒充 L2/L3 观察"）。真实模型、
网络、GitHub、Langfuse/ClickHouse 全部不触碰。

`scripts/contracts/` 不是包，因此按**文件路径**加载被测模块（与
`test_validate_local_tools.py` 同一模式）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = REPO_ROOT / "scripts" / "contracts" / "run_l3_smoke.py"
VALIDATE_LOCAL_PATH = REPO_ROOT / "scripts" / "contracts" / "validate_local.py"
SMOKE_MANIFEST = REPO_ROOT / "config" / "contracts" / "smoke-v0.1.json"

COMMIT = "a" * 40
PAYLOAD_HASH = "sha256:" + "d" * 64
CONTRACT_VERSION = "0.1.0"
RUN_ID = "l3-smoke-test"

#: 冻结 manifest 预登记的七对 (producer_id, mapping_stage)。
REQUIRED_PAIRS = (
    ("coding.agent.llm_usage", "openai_compat"),
    ("coding.trace.generation_usage", "langfuse_generation"),
    ("coding.benchmark.case_cost", "normal"),
    ("coding.benchmark.case_cost", "environment_error"),
    ("coding.benchmark.case_cost", "error"),
    ("coding.trace.summary", "sdk"),
    ("coding.trace.summary", "clickhouse"),
)

ONE_OF_PRODUCERS = ("coding.trace.summary",)


def _load_module(name: str, path: Path):
    assert path.is_file(), f"缺少被测脚本：{path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver():
    return _load_module("_fc_run_l3_smoke", DRIVER_PATH)


@pytest.fixture(scope="module")
def gate():
    return _load_module("_fc_validate_local_for_l3", VALIDATE_LOCAL_PATH)


# --------------------------------------------------------------------------- #
# 合成证据 fixture（离线业务路径替身）
# --------------------------------------------------------------------------- #

FIXTURE_BUSINESS_TEMPLATE = '''"""合成证据 fixture 业务路径（测试专用；不代表真实业务接线）。"""
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(r"__REPO_ROOT__")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.foundation.evidence_sink import FileEvidenceSink
from agent_core.contracts.enums.evidence import ValidationStatus
from agent_core.contracts.enums.modes import ContractMode
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.cost import Cost, CostSummary
from agent_core.contracts.models.evidence import EvidenceRecord, FoundationObservation
from agent_core.contracts.models.usage import Usage

spec = json.loads((Path(__file__).with_name("spec.json")).read_text(encoding="utf-8"))
run_id = os.environ["AGENT_CONTRACT_RUN_ID"]
commit = os.environ["AGENT_CONTRACT_COMMIT"]
sink = FileEvidenceSink(os.environ["AGENT_CONTRACT_EVIDENCE_DIR"])
counter = {"n": 0}


def event_id():
    counter["n"] += 1
    return "11111111-2222-4333-8444-%012x" % counter["n"]


def observe(producer_id, stage, usage=None, cost=None, components=None):
    record = EvidenceRecord(
        event_id=event_id(),
        run_id=run_id,
        repository="coding",
        repository_commit=commit,
        producer_id=producer_id,
        mapping_stage=stage,
        contract_mode=ContractMode.OBSERVE,
        contract_version=spec["contract_version"],
        contract_payload_hash=spec["payload_hash"],
        timestamp="2026-09-14T01:00:00Z",
        validation_status=ValidationStatus.PASS,
        sanitized_input_facts={"coding.legacy.call_observed": True},
        foundation_output=FoundationObservation(
            usage=usage,
            cost=cost,
            cost_summary=CostSummary.from_costs(components) if components is not None else None,
        ),
    )
    sink.emit(record)


provider_usage = Usage(
    input_tokens=spec["input_tokens"],
    output_tokens=spec["output_tokens"],
    total_tokens=spec["input_tokens"] + spec["output_tokens"],
    source=UsageSource.PROVIDER_REPORTED,
)

for producer_id, stage in spec["pairs"]:
    if stage == "openai_compat":
        observe(producer_id, stage, usage=provider_usage,
                cost=Cost(amount=Decimal("0.12"), currency="USD",
                          source=CostSource.PRICE_TABLE, pricing_version="p1"))
    elif stage == "langfuse_generation":
        observe(producer_id, stage, usage=provider_usage,
                cost=Cost(currency="USD", source=CostSource.UNKNOWN))
    elif producer_id == "coding.benchmark.case_cost":
        observe(producer_id, stage, components=[
            Cost(amount=Decimal("0.5"), currency="USD",
                 source=CostSource.PRICE_TABLE, pricing_version="p1"),
            Cost(currency="USD", source=CostSource.UNKNOWN),
        ])
    else:
        observe(producer_id, stage, components=[])

if spec.get("inject_sink_failure"):
    import unittest.mock as mock
    with mock.patch("os.replace", side_effect=OSError("injected sink failure")):
        try:
            observe("coding.agent.llm_usage", "openai_compat",
                    usage=provider_usage, cost=Cost(currency="USD", source=CostSource.UNKNOWN))
        except Exception:
            pass

sys.exit(int(spec.get("exit_code", 0)))
'''


def write_fixture_business(
    tmp_path: Path,
    *,
    pairs=REQUIRED_PAIRS,
    exit_code: int = 0,
    inject_sink_failure: bool = False,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> Path:
    """写一个合成证据业务脚本（+ 同目录 spec.json），返回脚本路径。"""
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    script = fixture_dir / "fixture_business.py"
    script.write_text(
        FIXTURE_BUSINESS_TEMPLATE.replace("__REPO_ROOT__", str(REPO_ROOT)),
        encoding="utf-8",
    )
    (fixture_dir / "spec.json").write_text(
        json.dumps(
            {
                "pairs": [list(pair) for pair in pairs],
                "contract_version": CONTRACT_VERSION,
                "payload_hash": PAYLOAD_HASH,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "exit_code": exit_code,
                "inject_sink_failure": inject_sink_failure,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return script


def requirement_for(producer_id: str) -> str:
    return "ONE_OF" if producer_id in ONE_OF_PRODUCERS else "ALL_STAGES"


def manifest_payload(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    case_ids=("schedule-99",),
    evidence_root: str | None = None,
    repository_commit: str | None = COMMIT,
    coverage_policy: str = "ALL_STAGES",
    **overrides,
) -> dict:
    """构造与冻结 manifest 键集一致的 run manifest（值可覆盖）。"""
    payload = {
        "manifest_version": "1",
        "run_id": run_id,
        "contract_mode": "observe",
        "contract_version": CONTRACT_VERSION,
        "payload_hash": PAYLOAD_HASH,
        "candidate_commit": None,
        "repository_commit": repository_commit,
        "evidence_root": evidence_root or str(tmp_path / "evidence"),
        "coverage_policy": coverage_policy,
        "required_producer_stages": [
            {
                "producer_id": producer_id,
                "mapping_stage": stage,
                "evidence_requirement": requirement_for(producer_id),
            }
            for producer_id, stage in REQUIRED_PAIRS
        ],
        "model": "mimo-v2.5",
        "provider": "opencode_go",
        "case_ids": list(case_ids),
        "expected_calls": {
            "coding.agent.llm_usage": 1,
            "coding.trace.generation_usage": 1,
            "coding.benchmark.case_cost": 3,
            "coding.trace.summary": 1,
        },
        "max_calls": 12,
        "estimated_cost_cap": {"amount": "1", "currency": "USD"},
        "network_requirement": "provider",
        "side_effect_policy": "read_only",
        "timeout_seconds": 60,
        "duration_cap_seconds": 1800,
    }
    payload.update(overrides)
    return payload


def write_manifest(tmp_path: Path, **kwargs) -> Path:
    payload = manifest_payload(tmp_path, **kwargs)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def set_driver_env(monkeypatch, tmp_path: Path, fixture: Path | None = None, **extra) -> None:
    """把驱动所需的全部环境配置到 monkeypatch（观测模式 + scratch 落 tmp）。"""
    monkeypatch.setenv("AGENT_CONTRACT_MODE", "observe")
    monkeypatch.setenv("AGENT_CONTRACT_RUN_ID", RUN_ID)
    monkeypatch.setenv("KA_L3_SMOKE_SCRATCH_DIR", str(tmp_path / "scratch"))
    if fixture is not None:
        monkeypatch.setenv("KA_L3_SMOKE_BUSINESS_CMD", str(fixture))
    else:
        monkeypatch.delenv("KA_L3_SMOKE_BUSINESS_CMD", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def child_env(tmp_path: Path, fixture: Path, run_id: str = RUN_ID, **extra) -> dict:
    """子进程用环境（driver/gate 都以真实 CLI 方式被驱动）。"""
    env = dict(os.environ)
    env.update(
        {
            "AGENT_CONTRACT_MODE": "observe",
            "AGENT_CONTRACT_RUN_ID": run_id,
            "KA_L3_SMOKE_SCRATCH_DIR": str(tmp_path / "scratch"),
            "KA_L3_SMOKE_BUSINESS_CMD": str(fixture),
        }
    )
    env.pop("AGENT_CONTRACT_COMMIT", None)
    env.update(extra)
    return env


def run_cli(script: Path, args: list[str], env: dict, timeout: int = 300):
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# 1. manifest 校验（与 gate_smoke 同一冻结键集）
# --------------------------------------------------------------------------- #


def test_required_key_set_is_frozen_literal_table(driver) -> None:
    """键集是冻结的：任何增删都必须显式改这张表（与 gate 同步）。"""
    assert driver.RUN_MANIFEST_REQUIRED_KEYS == (
        "manifest_version",
        "run_id",
        "contract_mode",
        "contract_version",
        "payload_hash",
        "repository_commit",
        "evidence_root",
        "coverage_policy",
        "required_producer_stages",
        "expected_calls",
        "max_calls",
        "estimated_cost_cap",
        "network_requirement",
        "side_effect_policy",
        "timeout_seconds",
        "duration_cap_seconds",
        "model",
        "provider",
        "case_ids",
    )
    assert set(driver.RUN_SUMMARY_KEYS) == {
        "sink_failure_count",
        "rejected_records",
        "actual_calls",
        "actual_tokens",
        "duration_seconds",
        "producer_coverage",
        "known_cost_components",
        "unknown_cost_components",
    }
    frozen = json.loads(SMOKE_MANIFEST.read_text(encoding="utf-8"))
    assert set(driver.RUN_MANIFEST_REQUIRED_KEYS) <= set(frozen)


def test_every_frozen_key_is_enforced_by_driver_and_gate(driver, gate, tmp_path) -> None:
    """逐键比对：driver 与 gate 对同一个缺失键都必须拒绝（同键集的证据，而非声明）。"""
    for key in driver.RUN_MANIFEST_REQUIRED_KEYS:
        payload = manifest_payload(tmp_path)
        payload.pop(key)
        with pytest.raises(driver.ToolError) as excinfo:
            driver.validate_run_manifest(payload)
        assert excinfo.value.exit_code == driver.EXIT_USAGE
        assert excinfo.value.marker == driver.MARKER_INCOMPLETE
        assert key in excinfo.value.message

        path = tmp_path / f"missing-{key}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(gate.UsageError):
            gate.gate_smoke(path)


def test_missing_manifest_key_exits_2_with_marker(driver, monkeypatch, tmp_path, capsys) -> None:
    payload = manifest_payload(tmp_path)
    payload.pop("timeout_seconds")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))

    rc = driver.main(["--run-manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == driver.EXIT_USAGE == 2
    assert "SPEC_INCOMPLETE" in err
    assert "Traceback" not in err


def test_run_id_mismatch_exits_2_with_marker(driver, monkeypatch, tmp_path, capsys) -> None:
    manifest = write_manifest(tmp_path)
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))
    monkeypatch.setenv("AGENT_CONTRACT_RUN_ID", "some-other-run")

    rc = driver.main(["--run-manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "SPEC_INCOMPLETE" in err and "EVD-RUN-001" in err
    assert "Traceback" not in err


def test_missing_observe_mode_exits_2_with_marker(driver, monkeypatch, tmp_path, capsys) -> None:
    manifest = write_manifest(tmp_path)
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))
    monkeypatch.setenv("AGENT_CONTRACT_MODE", "strict")

    rc = driver.main(["--run-manifest", str(manifest)])

    assert rc == 2
    assert "SPEC_INCOMPLETE" in capsys.readouterr().err


def test_unresolvable_commit_exits_2_with_marker(driver, monkeypatch, tmp_path, capsys) -> None:
    """`repository_commit: null` 且无 AGENT_CONTRACT_COMMIT → 不推断 HEAD，记配置错误。"""
    manifest = write_manifest(tmp_path, repository_commit=None)
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))
    monkeypatch.delenv("AGENT_CONTRACT_COMMIT", raising=False)

    rc = driver.main(["--run-manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "SPEC_INCOMPLETE" in err and "AGENT_CONTRACT_COMMIT" in err


# --------------------------------------------------------------------------- #
# 2. case 选择（B3）
# --------------------------------------------------------------------------- #


def test_case_id_absent_from_source_exits_2(driver, monkeypatch, tmp_path, capsys) -> None:
    """预登记 id 不在源里 → SPEC_INCOMPLETE，绝不静默换 case。"""
    manifest = write_manifest(tmp_path, case_ids=("no-such-case",))
    empty_source = tmp_path / "empty-cases"
    empty_source.mkdir()
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))

    rc = driver.main(
        ["--run-manifest", str(manifest), "--case-source", str(empty_source)]
    )

    err = capsys.readouterr().err
    assert rc == 2
    assert "SPEC_INCOMPLETE" in err and "no-such-case" in err
    assert not (tmp_path / "evidence" / RUN_ID / "events").exists()
    assert not (tmp_path / "scratch" / RUN_ID / "cases").exists()


def test_real_case_source_resolves_preregistered_id(driver, tmp_path) -> None:
    """冻结 manifest 的 schedule-99 在 benchmark/cases 中真实存在且可被选中。"""
    source = REPO_ROOT / "benchmark" / "cases"
    selected = driver.select_cases(source, ["schedule-99"])

    assert [item.case_id for item in selected] == ["schedule-99"]
    assert selected[0].path.as_posix().endswith("benchmark/cases/feature/schedule-99.json")

    staged = driver.materialize_cases(selected, tmp_path / "staged")
    assert (staged / "feature" / "schedule-99.json").is_file()
    assert (staged / "gold" / "schedule-99.diff").is_file()
    # 只跑预登记 case：staged 目录里不得出现第二个 case。
    assert len(list(staged.glob("*/*.json"))) == 1


def test_business_command_drives_real_benchmark_path(driver, tmp_path) -> None:
    """业务命令必须是真实 benchmark 入口，且 out/workspace 落在 scratch（§11.5）。"""
    command = driver.build_business_command(tmp_path / "cases", tmp_path / "scratch")

    assert Path(command[1]).name == "main.py"
    assert "--benchmark" in command
    assert command[command.index("--cases") + 1] == str(tmp_path / "cases")
    assert command[command.index("--benchmark-out") + 1].startswith(str(tmp_path / "scratch"))
    assert command[command.index("--workspace") + 1].startswith(str(tmp_path / "scratch"))


# --------------------------------------------------------------------------- #
# 3. run-summary 由证据推导（B1）
# --------------------------------------------------------------------------- #


def test_driver_writes_run_summary_with_exact_keys(driver, monkeypatch, tmp_path, capsys) -> None:
    manifest = write_manifest(tmp_path)
    fixture = write_fixture_business(tmp_path)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest), "--json"])

    out = capsys.readouterr().out
    assert rc == 0, out
    report = json.loads(out)
    assert report["fixture_seam"] is True
    assert report["business_returncode"] == 0
    assert report["violations"] == []

    summary_path = tmp_path / "evidence" / RUN_ID / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(summary) == set(driver.RUN_SUMMARY_KEYS)
    assert sorted(map(tuple, summary["producer_coverage"])) == sorted(REQUIRED_PAIRS)
    # 两条 provider_reported usage 证据（openai_compat + langfuse_generation）。
    assert summary["actual_calls"] == 2
    assert summary["actual_tokens"] == {
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    }
    # 1 条 price_table cost + 3 条 case_cost summary 各 1 已知；1 条 unknown cost +
    # 3 条 case_cost summary 各 1 未知。
    assert summary["known_cost_components"] == 4
    assert summary["unknown_cost_components"] == 4
    assert summary["sink_failure_count"] == 0
    assert summary["rejected_records"] == []
    assert summary["duration_seconds"] >= 0.0
    assert not (tmp_path / "evidence" / RUN_ID / "run-summary.json.tmp").exists()


def test_driver_reports_incomplete_coverage_as_conflict(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    """只观测到部分 stage 时：退出码 1 + SPEC_STATUS_CONFLICT，且不伪造覆盖。"""
    manifest = write_manifest(tmp_path)
    fixture = write_fixture_business(
        tmp_path, pairs=(("coding.agent.llm_usage", "openai_compat"),)
    )
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest), "--json"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "SPEC_STATUS_CONFLICT" in captured.err
    assert "coding.benchmark.case_cost" in captured.err
    report = json.loads(captured.out)
    assert report["producer_coverage"] == [["coding.agent.llm_usage", "openai_compat"]]
    summary = json.loads(
        (tmp_path / "evidence" / RUN_ID / "run-summary.json").read_text(encoding="utf-8")
    )
    assert summary["producer_coverage"] == [["coding.agent.llm_usage", "openai_compat"]]


def test_one_of_policy_accepts_single_trace_summary_stage(driver, monkeypatch, tmp_path, capsys) -> None:
    """`coding.trace.summary` 是 ONE_OF：只观测 sdk 也闭合（逐对策略，Spec 13）。"""
    pairs = tuple(pair for pair in REQUIRED_PAIRS if pair[1] != "clickhouse")
    manifest = write_manifest(tmp_path)
    fixture = write_fixture_business(tmp_path, pairs=pairs)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest)])

    assert rc == 0
    assert "SPEC_STATUS_CONFLICT" not in capsys.readouterr().err


def rewrite_requirement(manifest: Path, mapping: dict) -> None:
    """改写冻结形状 manifest 中指定 (producer_id, mapping_stage) 的 evidence_requirement。"""
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for item in payload["required_producer_stages"]:
        key = (item["producer_id"], item["mapping_stage"])
        if key in mapping:
            item["evidence_requirement"] = mapping[key]
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


#: 本轮收敛为 NOT_OBSERVED_ALLOWED 的四对（(A) 规范回归两对 + (B) 授权偏离两对）。
AUTHORIZED_PAIRS = (
    ("coding.benchmark.case_cost", "environment_error"),
    ("coding.benchmark.case_cost", "error"),
    ("coding.trace.summary", "sdk"),
    ("coding.trace.summary", "clickhouse"),
)


def test_not_observed_allowed_pairs_are_named_not_failed(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    """(A)+(B)：`NOT_OBSERVED_ALLOWED` 未观测不使驱动失败，但必须被逐对点名。

    母 Spec §7.3:896「`normal` required；错误 stage 可 `NOT_OBSERVED`」+ §7.3:887
    「'未观察到'不等于失败」。**未观测 ≠ 通过**：这些对既不进 violation，也绝不出现在
    `producer_coverage` 里，只在 `authorized_unobserved` / 报告的"未观测（授权豁免）"段具名。
    """
    manifest = write_manifest(tmp_path)
    rewrite_requirement(
        manifest,
        {pair: driver.REQUIREMENT_NOT_OBSERVED_ALLOWED for pair in AUTHORIZED_PAIRS},
    )
    observed = tuple(pair for pair in REQUIRED_PAIRS if pair not in AUTHORIZED_PAIRS)
    fixture = write_fixture_business(tmp_path, pairs=observed)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest), "--json"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "SPEC_STATUS_CONFLICT" not in captured.err
    report = json.loads(captured.out)
    assert report["violations"] == []
    assert report["authorized_unobserved"] == [
        ["coding.benchmark.case_cost", "environment_error"],
        ["coding.benchmark.case_cost", "error"],
        ["coding.trace.summary", "clickhouse"],
        ["coding.trace.summary", "sdk"],
    ]
    covered = {tuple(item) for item in report["producer_coverage"]}
    assert covered.isdisjoint(AUTHORIZED_PAIRS), "未观测的授权对绝不进 producer_coverage"

    # 人读报告同样点名，不得读成"全部闭合"
    rc_human = driver.main(["--run-manifest", str(manifest)])
    human = capsys.readouterr().out
    assert rc_human == 0
    assert "未观测（§7.3:896 授权豁免" in human
    for producer_id, stage in AUTHORIZED_PAIRS:
        assert f"{producer_id}/{stage}" in human


def test_not_observed_allowed_still_requires_the_other_pairs(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    """豁免只对本对生效：ALL_STAGES 对缺失仍然失败（不弱化门）。"""
    manifest = write_manifest(tmp_path)
    rewrite_requirement(
        manifest,
        {("coding.trace.summary", "sdk"): driver.REQUIREMENT_NOT_OBSERVED_ALLOWED,
         ("coding.trace.summary", "clickhouse"): driver.REQUIREMENT_NOT_OBSERVED_ALLOWED},
    )
    observed = tuple(
        pair
        for pair in REQUIRED_PAIRS
        if pair[1] not in {"sdk", "clickhouse", "environment_error"}
    )
    fixture = write_fixture_business(tmp_path, pairs=observed)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest), "--json"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "SPEC_STATUS_CONFLICT" in err and "environment_error" in err


def test_not_observed_allowed_is_rejected_as_top_level_policy(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    """顶层 `coverage_policy` 不接受 `NOT_OBSERVED_ALLOWED`（整仓放宽会掩盖未观测）。"""
    manifest = write_manifest(tmp_path, coverage_policy=driver.REQUIREMENT_NOT_OBSERVED_ALLOWED)
    set_driver_env(monkeypatch, tmp_path, write_fixture_business(tmp_path))

    rc = driver.main(["--run-manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "SPEC_INCOMPLETE" in err and "coverage_policy" in err


def test_per_pair_vocabulary_is_frozen(driver) -> None:
    """字面量守卫：逐对闭集含 §7.3:896 的授权值，且顶层策略不含它。"""
    assert driver.PER_PAIR_REQUIREMENTS == (
        "ALL_STAGES",
        "ONE_OF",
        "NOT_OBSERVED_ALLOWED",
    )
    assert driver.REQUIREMENT_NOT_OBSERVED_ALLOWED == "NOT_OBSERVED_ALLOWED"


def test_nonzero_business_exit_is_conflict_not_silent_pass(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    manifest = write_manifest(tmp_path)
    fixture = write_fixture_business(tmp_path, exit_code=3)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "SPEC_STATUS_CONFLICT" in captured.err and "退出码非 0" in captured.err
    # 业务失败也要留下可诊断的 run-summary。
    assert (tmp_path / "evidence" / RUN_ID / "run-summary.json").is_file()


def test_sink_failure_is_durable_and_makes_summary_nonzero(
    driver, monkeypatch, tmp_path, capsys
) -> None:
    """子进程内的 sink 失败必须被父进程读到（B1 的 sink_failure_count 来源）。"""
    manifest = write_manifest(tmp_path)
    fixture = write_fixture_business(tmp_path, inject_sink_failure=True)
    set_driver_env(monkeypatch, tmp_path, fixture)

    rc = driver.main(["--run-manifest", str(manifest), "--json"])

    assert rc == 1
    assert "SPEC_STATUS_CONFLICT" in capsys.readouterr().err
    marker = tmp_path / "evidence" / RUN_ID / "sink-failures.jsonl"
    assert marker.is_file()
    summary = json.loads(
        (tmp_path / "evidence" / RUN_ID / "run-summary.json").read_text(encoding="utf-8")
    )
    assert summary["sink_failure_count"] == 1
    assert summary["rejected_records"], "被拒绝的 EvidenceRecord 不得为空"


# --------------------------------------------------------------------------- #
# 4. sink 失败标记的持久承载（evidence_sink 的最小改动）
# --------------------------------------------------------------------------- #


def _sink_module():
    from adapters.foundation import evidence_sink

    return evidence_sink


def _sink_record(event_id: str, run_id: str = RUN_ID):
    from decimal import Decimal

    from agent_core.contracts.enums.evidence import ValidationStatus
    from agent_core.contracts.enums.modes import ContractMode
    from agent_core.contracts.enums.sources import CostSource, UsageSource
    from agent_core.contracts.models.cost import Cost
    from agent_core.contracts.models.evidence import EvidenceRecord, FoundationObservation
    from agent_core.contracts.models.usage import Usage

    return EvidenceRecord(
        event_id=event_id,
        run_id=run_id,
        repository="coding",
        repository_commit=COMMIT,
        producer_id="coding.agent.llm_usage",
        mapping_stage="openai_compat",
        contract_mode=ContractMode.OBSERVE,
        contract_version=CONTRACT_VERSION,
        contract_payload_hash=PAYLOAD_HASH,
        timestamp="2026-09-14T01:00:00Z",
        validation_status=ValidationStatus.PASS,
        sanitized_input_facts={"coding.legacy.call_observed": True},
        foundation_output=FoundationObservation(
            usage=Usage(input_tokens=1, source=UsageSource.PROVIDER_REPORTED),
            cost=Cost(amount=Decimal("0.01"), currency="USD", source=CostSource.PRICE_TABLE,
                      pricing_version="p1"),
        ),
    )


def test_sink_failure_marker_written_on_failed_emit(tmp_path) -> None:
    """失败的 emit 落一行 run 级标记；标记里的绝对路径被掩掉（§11.4）。"""
    from unittest.mock import patch

    from agent_core.contracts.protocols.evidence_sink import SinkFailure

    module = _sink_module()
    sink = module.FileEvidenceSink(tmp_path)
    event_id = "9f1c0c1e-4a3b-4c2d-8e5f-000000000001"

    assert not (tmp_path / RUN_ID / module.FAILURES_FILENAME).exists()
    with patch("os.replace", side_effect=OSError("injected replace failure at C:\\tmp\\x")):
        with pytest.raises(SinkFailure):
            sink.emit(_sink_record(event_id))

    marker = tmp_path / RUN_ID / module.FAILURES_FILENAME
    assert marker.is_file()
    lines = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["code"] == "evidence.sink_write_failed"
    assert lines[0]["event_id"] == event_id
    assert lines[0]["run_id"] == RUN_ID
    assert "C:\\tmp" not in lines[0]["detail"]
    assert sink.sink_failure_count == 1


def test_sink_failure_marker_absent_without_failure(tmp_path) -> None:
    """没有失败就没有标记文件：缺文件 = 零失败。"""
    module = _sink_module()
    sink = module.FileEvidenceSink(tmp_path)
    sink.emit(_sink_record("9f1c0c1e-4a3b-4c2d-8e5f-000000000002"))

    assert sink.sink_failure_count == 0
    assert not (tmp_path / RUN_ID / module.FAILURES_FILENAME).exists()


def test_sink_failure_marker_refuses_unsafe_run_id(tmp_path) -> None:
    """run_id 不安全时无法承载 run 级标记：不写、不抛额外异常（绝不改变业务返回）。"""
    from types import SimpleNamespace

    from agent_core.contracts.protocols.evidence_sink import SinkFailure

    module = _sink_module()
    sink = module.FileEvidenceSink(tmp_path)
    assert sink.failures_path("../escape") is None

    rogue = SimpleNamespace(run_id="../escape", event_id="9f1c0c1e-4a3b-4c2d-8e5f-000000000003")
    with pytest.raises(SinkFailure) as excinfo:
        sink.emit(rogue)  # type: ignore[arg-type]
    assert excinfo.value.code == "evidence.invalid_run_id"
    assert list(tmp_path.rglob(module.FAILURES_FILENAME)) == []
    assert not (tmp_path.parent / "escape").exists()


# --------------------------------------------------------------------------- #
# 5. 端到端：driver 写出的 run-summary 让 `--gate smoke` 通过；扰动后失败
# --------------------------------------------------------------------------- #


def test_gate_smoke_passes_on_driver_output(tmp_path) -> None:
    """synthetic fixture → 真实 sink → driver 汇总 → gate smoke 闭合（离线）。"""
    fixture = write_fixture_business(tmp_path)
    manifest = write_manifest(tmp_path)
    env = child_env(tmp_path, fixture)

    driven = run_cli(DRIVER_PATH, ["--run-manifest", str(manifest)], env)
    assert driven.returncode == 0, driven.stdout + driven.stderr
    assert "WARNING" in driven.stdout and "fixture seam" in driven.stdout

    gated = run_cli(
        VALIDATE_LOCAL_PATH,
        ["--gate", "smoke", "--run-manifest", str(manifest)],
        env,
    )
    assert gated.returncode == 0, gated.stdout + gated.stderr
    assert "gate smoke PASSED" in gated.stdout
    assert gated.stdout.count("] OK") == 8


@pytest.mark.parametrize(
    ("mutation", "expect"),
    [
        ({"sink_failure_count": 1}, "EVD-SINK-003"),
        ({"rejected_records": ["9f1c0c1e-4a3b-4c2d-8e5f-0000000000ff"]}, "被拒绝的 EvidenceRecord"),
        ({"duration_seconds": 99999}, "超过预算"),
    ],
)
def test_perturbed_run_summary_fails_gate(tmp_path, mutation: dict, expect: str) -> None:
    fixture = write_fixture_business(tmp_path)
    manifest = write_manifest(tmp_path)
    env = child_env(tmp_path, fixture)

    driven = run_cli(DRIVER_PATH, ["--run-manifest", str(manifest)], env)
    assert driven.returncode == 0, driven.stdout + driven.stderr

    summary_path = tmp_path / "evidence" / RUN_ID / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(mutation)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    gated = run_cli(
        VALIDATE_LOCAL_PATH,
        ["--gate", "smoke", "--run-manifest", str(manifest)],
        env,
    )
    assert gated.returncode == 1, gated.stdout + gated.stderr
    assert expect in gated.stderr


def test_missing_run_summary_fails_gate(tmp_path) -> None:
    """B1 的核心：没有 run-summary.json 时 gate 必然失败（driver 落地之前的状态）。

    evidence_root 取仓库内 `output/contract-validation/run-scratch/...`（gitignored、
    不被 `--gate pr` 的发布扫描覆盖，也不属于 staging）：这样 gate 的失败信息能正常
    相对化，而不是撞上 `Path.relative_to` 的 ValueError。
    """
    import shutil
    import uuid

    scratch = REPO_ROOT / "output" / "contract-validation" / "run-scratch" / (
        "l3-missing-summary-" + uuid.uuid4().hex
    )
    try:
        fixture = write_fixture_business(tmp_path)
        manifest = write_manifest(tmp_path, evidence_root=str(scratch / "evidence"))
        env = child_env(tmp_path, fixture)
        env["KA_L3_SMOKE_SCRATCH_DIR"] = str(scratch / "business")

        driven = run_cli(DRIVER_PATH, ["--run-manifest", str(manifest)], env)
        assert driven.returncode == 0, driven.stdout + driven.stderr
        (scratch / "evidence" / RUN_ID / "run-summary.json").unlink()

        gated = run_cli(
            VALIDATE_LOCAL_PATH,
            ["--gate", "smoke", "--run-manifest", str(manifest)],
            env,
        )
        assert gated.returncode == 1
        assert "run-summary" in (gated.stderr or "")
        assert "SPEC_INCOMPLETE（G-04）" in (gated.stderr or "")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
