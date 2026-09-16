"""Spec 10 — `scripts/contracts/validate_local.py` 工具自测（两仓同构）。

只测**纯逻辑**与用法门：安全扫描、junit→nodeid 映射、失败指纹衍生、判定分支、
回归子集发现、退出码。端到端的 `--gate pr` 由 Spec 10 的权威命令单独跑（它要
构建 wheel、跑全量回归，不适合放在单元测试里）。

`scripts/contracts/` 不是包，因此按**文件路径**加载被测模块（与 Spec 09 的
baseline comparator 同一模式）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE_LOCAL = REPO_ROOT / "scripts" / "contracts" / "validate_local.py"


def _load_validate_local():
    assert VALIDATE_LOCAL.is_file(), f"缺少被测脚本：{VALIDATE_LOCAL}"
    spec = importlib.util.spec_from_file_location("_fc_validate_local", VALIDATE_LOCAL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_fc_validate_local"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vl():
    return _load_validate_local()


# --------------------------------------------------------------------------- #
# 安全扫描（§11.4）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "key = sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer abcdefghijklmnopqrst",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
        'api_key = "abcdefghijklmnop"',
        r"C:\Users\someone\secret.txt",
        r"D:\\AI project\\private\\x.json",
        "/home/deploy/.ssh/id_rsa.pem",
    ],
)
def test_scan_text_detects_secrets_and_absolute_paths(vl, text: str) -> None:
    assert vl._scan_text(text), f"未检出：{text}"


def test_scan_text_flags_utf8_bom(vl) -> None:
    assert any("BOM" in hit for hit in vl._scan_text("\ufeffhello"))


def test_scan_text_accepts_clean_text(vl) -> None:
    assert vl._scan_text("payload_hash: sha256:64049830 in output/contract-validation") == []


def test_scan_object_detects_forbidden_keys_recursively(vl) -> None:
    payload = {
        "sanitized_input_facts": {
            "provider": {"api_key": "abcdefghijklmnop"},
            "nested": [{"reasoning": "chain of thought"}],
        }
    }
    hits = vl._scan_object(payload, "record")
    assert any("api_key" in hit for hit in hits)
    assert any("reasoning" in hit for hit in hits)


def test_scan_object_accepts_allowed_fact_keys(vl) -> None:
    assert vl._scan_object({"usage": {"input_tokens": 12, "total_tokens": None}}, "record") == []


def test_scan_paths_flags_oversize_record_and_non_utf8(vl, tmp_path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    (events / "event.json").write_bytes(b"{}" + b" " * (vl.MAX_RECORD_BYTES + 1))
    (tmp_path / "bad.json").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    violations, scanned = vl._scan_paths([tmp_path])
    assert scanned == 3
    assert any("超过 64 KiB" in item for item in violations)
    assert any("非 UTF-8" in item for item in violations)


def test_scan_paths_applies_record_cap_per_jsonl_line(vl, tmp_path) -> None:
    """64 KiB 是**单条记录**上限：大语料文件本身不违规，超长单行才违规。"""
    corpus = tmp_path / "sanitized-replay-corpus.jsonl"
    corpus.write_text("\n".join(['{"record_id": "a"}'] * 4000) + "\n", encoding="utf-8")
    violations, _ = vl._scan_paths([tmp_path])
    assert corpus.stat().st_size > vl.MAX_RECORD_BYTES, "语料文件应当大于 64 KiB"
    assert not [item for item in violations if "64 KiB" in item]

    corpus.write_text('{"record_id": "' + "x" * (vl.MAX_RECORD_BYTES + 10) + '"}\n', encoding="utf-8")
    violations, _ = vl._scan_paths([tmp_path])
    assert any("单条记录" in item and "64 KiB" in item for item in violations)


def test_scan_paths_does_not_size_gate_reports(vl, tmp_path) -> None:
    report = tmp_path / "validation-report.md"
    report.write_text("# report\n" + "line\n" * 40000, encoding="utf-8")
    assert report.stat().st_size > vl.MAX_RECORD_BYTES
    violations, _ = vl._scan_paths([tmp_path])
    assert not [item for item in violations if "64 KiB" in item]


def test_scan_paths_accepts_clean_tree(vl, tmp_path) -> None:
    (tmp_path / "evidence-manifest.json").write_text(
        json.dumps({"repository": "wiki", "contract_version": "0.1.0"}), encoding="utf-8"
    )
    violations, scanned = vl._scan_paths([tmp_path])
    assert (violations, scanned) == ([], 1)


# --------------------------------------------------------------------------- #
# junit → pytest nodeid 映射与失败指纹衍生
# --------------------------------------------------------------------------- #


def test_junit_nodeid_maps_existing_test_file(vl) -> None:
    nodeid = vl._junit_nodeid("tests.contracts.test_test_baseline", "test_baseline_self_consistency")
    assert nodeid == "tests/contracts/test_test_baseline.py::test_baseline_self_consistency"


def test_junit_nodeid_keeps_class_segments(vl) -> None:
    # 用两仓都存在且确定包含类分段的形态：模块路径 + 类 + 方法。
    nodeid = vl._junit_nodeid("tests.contracts.test_test_baseline.SomeClass", "test_x")
    assert nodeid == "tests/contracts/test_test_baseline.py::SomeClass::test_x"


def test_derive_locus_uses_last_frame_of_module(vl) -> None:
    traceback_text = (
        'Traceback (most recent call last):\n'
        f'  File "{REPO_ROOT / "scripts" / "contracts" / "validate_local.py"}", line 42, in _get_raw_data\n'
        "    return payload.get('nodes')\n"
        "AttributeError: 'list' object has no attribute 'get'\n"
    )
    locus = vl._derive_locus(traceback_text)
    assert locus.endswith("._get_raw_data")
    assert "validate_local" in locus


def test_derive_locus_is_unknown_without_frames(vl) -> None:
    assert vl._derive_locus("AssertionError: nope") == "unknown"


def test_normalize_signature_is_stable_and_strips_volatiles(vl) -> None:
    a = vl._normalize_signature("AttributeError", "AttributeError: 'list' object has no attribute 'get' at 0xdeadbeef")
    b = vl._normalize_signature("AttributeError", "AttributeError: 'list' object has no attribute 'get' at 0x1234abcd")
    assert a == b
    assert "0x" not in a
    assert a.startswith("attributeerror:")


def test_parse_junit_classifies_pass_skip_fail(vl, tmp_path) -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
  <testcase classname="tests.contracts.test_test_baseline" name="test_baseline_self_consistency" time="0.1"/>
  <testcase classname="tests.contracts.test_evidence_sink" name="test_deepeval_gated" time="0.0">
    <skipped type="pytest.skip" message="deepeval 未安装">skipped</skipped>
  </testcase>
  <testcase classname="tests.contracts.test_evidence_sink" name="test_boom" time="0.2">
    <failure type="AssertionError" message="AssertionError: boom">AssertionError: boom</failure>
  </testcase>
</testsuite></testsuites>
"""
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    results = {nodeid: outcome for nodeid, outcome, _ in vl._parse_junit(path)}
    assert results["tests/contracts/test_test_baseline.py::test_baseline_self_consistency"] == "PASS"
    assert results["tests/contracts/test_evidence_sink.py::test_deepeval_gated"] == "SKIP"
    assert results["tests/contracts/test_evidence_sink.py::test_boom"] == "FAIL"

    failures = [item for item in vl._parse_junit(path) if item[1] == "FAIL"]
    assert failures and failures[0][2]["exception_type"] == "AssertionError"
    assert failures[0][2]["normalized_error_signature"].startswith("assertionerror:")


# --------------------------------------------------------------------------- #
# 判定分支（§14.2 / Appendix E.2）
# --------------------------------------------------------------------------- #


def _stub_comparator(status_verdict: dict, fingerprint_verdict: str = "ALLOWED_BASELINE_FAILURE"):
    class _Stub:
        def classify_status(self, nodeid: str, status: str, baseline: dict) -> str:
            return status_verdict[(status, nodeid)]

        def classify_failure(self, nodeid: str, failure: dict, baseline: dict) -> str:
            return fingerprint_verdict

    return _Stub()


BASELINE = {"known_failures": [], "existing_pass_nodeids": [], "existing_skip_nodeids": []}


@pytest.mark.parametrize(
    ("outcome", "verdict", "allowed", "review"),
    [
        ("PASS", "PASS_OK", True, False),
        ("PASS", "RESOLVED_UNEXPECTEDLY", True, True),
        ("SKIP", "SKIP_OK", True, False),
        ("SKIP", "SKIP_REGRESSION", False, False),
        ("FAIL", "NEW_FAILURE", False, False),
        ("FAIL", "BASELINE_PASS_NOW_FAIL", False, False),
        ("FAIL", "BASELINE_SKIP_NOW_FAIL", False, False),
    ],
)
def test_judge_verdict_table(vl, outcome: str, verdict: str, allowed: bool, review: bool) -> None:
    module = _stub_comparator({(outcome, "node"): verdict})
    assert vl._judge("node", outcome, {}, BASELINE, module) == (verdict, allowed, review)


def test_judge_known_failure_requires_fingerprint_match(vl) -> None:
    matching = _stub_comparator(
        {("FAIL", "node"): "KNOWN_FAILURE_PRESENT"}, fingerprint_verdict="ALLOWED_BASELINE_FAILURE"
    )
    assert vl._judge("node", "FAIL", {}, BASELINE, matching) == (
        "ALLOWED_BASELINE_FAILURE",
        True,
        False,
    )

    drifted = _stub_comparator(
        {("FAIL", "node"): "KNOWN_FAILURE_PRESENT"}, fingerprint_verdict="FINGERPRINT_MISMATCH"
    )
    assert vl._judge("node", "FAIL", {}, BASELINE, drifted) == ("FINGERPRINT_MISMATCH", False, False)


# --------------------------------------------------------------------------- #
# 回归子集发现（G-05 provisional）
# --------------------------------------------------------------------------- #


def test_regression_subset_is_non_empty_and_excludes_contracts(vl) -> None:
    subset, basis = vl.discover_regression_subset()
    assert subset, "contract-related regression subset 不应为空"
    assert basis
    assert not [item for item in subset if item.startswith("tests/contracts/")]


def test_in_scope_modules_are_derived_from_registry(vl) -> None:
    modules = vl._in_scope_modules()
    assert modules, "producer registry 的 IN_SCOPE 来源不应为空"
    assert all(isinstance(item, str) and "." in item for item in modules)


def test_repository_name_matches_layout(vl) -> None:
    expected = "wiki" if (REPO_ROOT / "arknights_wiki").is_dir() else "coding"
    assert vl.repository_name() == expected


# --------------------------------------------------------------------------- #
# 用法门与退出码（G-18 provisional）
# --------------------------------------------------------------------------- #


def test_missing_run_manifest_is_usage_error(vl, capsys) -> None:
    assert vl.main(["--gate", "smoke"]) == vl.EXIT_USAGE
    assert "SPEC_INCOMPLETE" in capsys.readouterr().err


def test_malformed_expected_payload_hash_is_usage_error(vl, capsys) -> None:
    assert vl.main(["--gate", "pr", "--expect-payload-hash", "not-a-hash"]) == vl.EXIT_USAGE
    assert "USAGE" in capsys.readouterr().err


def test_require_payload_hash_accepts_none(vl) -> None:
    assert vl._require_payload_hash(None) is None


def test_require_payload_hash_accepts_well_formed(vl) -> None:
    digest = "sha256:" + "a" * 64
    assert vl._require_payload_hash(digest) == digest


# --------------------------------------------------------------------------- #
# run manifest 的运行 commit 解析（G-01 provisional 约定）
# --------------------------------------------------------------------------- #


def test_resolve_manifest_commit_prefers_declared_value(vl) -> None:
    declared = "a" * 40
    assert vl._resolve_manifest_commit({"repository_commit": declared}) == declared


def test_resolve_manifest_commit_uses_env_when_null(vl, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CONTRACT_COMMIT", "b" * 40)
    assert vl._resolve_manifest_commit({"repository_commit": None}) == "b" * 40


def test_resolve_manifest_commit_refuses_to_infer_without_env(vl, monkeypatch) -> None:
    monkeypatch.delenv("AGENT_CONTRACT_COMMIT", raising=False)
    with pytest.raises(vl.GateFailure):
        vl._resolve_manifest_commit({"repository_commit": None})


def test_resolve_manifest_commit_rejects_malformed_value(vl) -> None:
    with pytest.raises(vl.UsageError):
        vl._resolve_manifest_commit({"repository_commit": "not-a-sha"})


# --------------------------------------------------------------------------- #
# gate: smoke 第 7 步 —— 逐对 evidence_requirement 覆盖（B7 / Spec 13:51）
# --------------------------------------------------------------------------- #
# 合成证据与合成 manifest 全部落在 tmp_path：不经网络、不碰 staging、不写冻结的
# config/contracts/*.json。gate 的 REPO_ROOT 被指向 tmp_path，因此不需要仓库内
# scratch 目录；repository 名与 sink 分支按本仓真实取值保留（两仓同构）。

B7_RUN_ID = "b7-smoke-coverage"
B7_COMMIT = "c" * 40
B7_PAYLOAD_HASH = "sha256:" + "e" * 64


def _b7_manifest(*, policy: str, pairs) -> dict:
    """最小合法 run manifest；``pairs`` = [(producer_id, mapping_stage, requirement|None)]。"""
    stages = []
    for producer_id, stage, requirement in pairs:
        entry = {"producer_id": producer_id, "mapping_stage": stage}
        if requirement is not None:
            entry["evidence_requirement"] = requirement
        stages.append(entry)
    return {
        "manifest_version": "1",
        "run_id": B7_RUN_ID,
        "contract_mode": "observe",
        "contract_version": "0.1.0",
        "payload_hash": B7_PAYLOAD_HASH,
        "repository_commit": B7_COMMIT,
        "evidence_root": "evidence",
        "coverage_policy": policy,
        "required_producer_stages": stages,
        "expected_calls": {},
        "max_calls": 12,
        "estimated_cost_cap": {"amount": "1", "currency": "USD"},
        "network_requirement": "provider",
        "side_effect_policy": "read_only",
        "timeout_seconds": 600,
        "duration_cap_seconds": 1800,
        "model": "synthetic-model",
        "provider": "synthetic-provider",
        "case_ids": ["synthetic-case"],
    }


def _b7_record(producer_id: str, stage: str, repository: str, index: int):
    from agent_core.contracts.enums.evidence import ValidationStatus
    from agent_core.contracts.enums.modes import ContractMode
    from agent_core.contracts.enums.sources import UsageSource
    from agent_core.contracts.models.evidence import EvidenceRecord, FoundationObservation
    from agent_core.contracts.models.usage import Usage

    return EvidenceRecord(
        event_id="7b1c0c1e-4a3b-4c2d-8e5f-%012x" % index,
        run_id=B7_RUN_ID,
        repository=repository,
        repository_commit=B7_COMMIT,
        producer_id=producer_id,
        mapping_stage=stage,
        contract_mode=ContractMode.OBSERVE,
        contract_version="0.1.0",
        contract_payload_hash=B7_PAYLOAD_HASH,
        timestamp="2026-09-14T01:00:00Z",
        validation_status=ValidationStatus.PASS,
        sanitized_input_facts={f"{repository}.legacy.b7_fixture": True},
        foundation_output=FoundationObservation(
            usage=Usage(input_tokens=1, source=UsageSource.PROVIDER_REPORTED)
        ),
    )


def _b7_publish_evidence(tmp_path: Path, repository: str, observed) -> None:
    """写合成事件（经本仓 sink → canonical 落盘）与 run-summary.json。"""
    if repository == "wiki":
        from arknights_wiki.adapters.foundation.evidence_sink import FileEvidenceSink
    else:
        from adapters.foundation.evidence_sink import FileEvidenceSink

    root = tmp_path / "evidence"
    sink = FileEvidenceSink(root)
    for index, (producer_id, stage) in enumerate(observed, start=1):
        sink.emit(_b7_record(producer_id, stage, repository, index))

    summary = {
        "sink_failure_count": 0,
        "rejected_records": [],
        "actual_calls": len(observed),
        "actual_tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "duration_seconds": 0.0,
        "producer_coverage": [[producer, stage] for producer, stage in sorted(observed)],
        "known_cost_components": 0,
        "unknown_cost_components": 0,
    }
    (root / B7_RUN_ID / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def smoke_gate(vl, tmp_path, monkeypatch):
    """把 gate 的 REPO_ROOT 指到 tmp_path；返回 (manifest_path, real_repository)。"""
    real_repository = vl.repository_name()
    monkeypatch.setenv("AGENT_CONTRACT_MODE", "observe")
    monkeypatch.setenv("AGENT_CONTRACT_RUN_ID", B7_RUN_ID)
    monkeypatch.setattr(vl, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(vl, "repository_name", lambda: real_repository)

    def build(*, policy: str, pairs, observed):
        manifest_path = tmp_path / "smoke-manifest.json"
        manifest_path.write_text(
            json.dumps(_b7_manifest(policy=policy, pairs=pairs), ensure_ascii=False),
            encoding="utf-8",
        )
        _b7_publish_evidence(tmp_path, real_repository, observed)
        return manifest_path

    return build


def test_smoke_gate_fails_on_missing_all_stages_pair(vl, smoke_gate) -> None:
    """ALL_STAGES 逐对判定：缺任意一对即失败（原行为不变）。"""
    pairs = [
        ("synthetic.llm_usage", "openai_compat", "ALL_STAGES"),
        ("synthetic.llm_usage", "chat_completion", "ALL_STAGES"),
    ]
    manifest = smoke_gate(
        policy="ALL_STAGES",
        pairs=pairs,
        observed=[("synthetic.llm_usage", "openai_compat")],
    )

    with pytest.raises(vl.GateFailure) as excinfo:
        vl.gate_smoke(manifest)

    message = str(excinfo.value)
    assert "ALL_STAGES" in message and "chat_completion" in message


def test_smoke_gate_missing_coverage_exits_1(vl, smoke_gate, capsys) -> None:
    """闭合失败仍是退出码 1 + 明确报错（不弱化门）。"""
    pairs = [("synthetic.llm_usage", "openai_compat", "ALL_STAGES")]
    # 观测到的是**未预登记**的一对（证据目录非空），判定必须走到第 7 步才失败。
    manifest = smoke_gate(
        policy="ALL_STAGES", pairs=pairs, observed=[("synthetic.trace.summary", "sdk")]
    )

    assert vl.main(["--gate", "smoke", "--run-manifest", str(manifest)]) == 1
    err = capsys.readouterr().err
    assert "GATE FAILED" in err and "openai_compat" in err


def test_smoke_gate_one_of_group_passes_with_single_stage_observed(vl, smoke_gate) -> None:
    """ONE_OF 组内观测到 1/2 即闭合（B7 的核心修复；Coding 的 trace.summary）。

    这里是冻结 Coding manifest 的真实形状：ALL_STAGES 对逐对判、ONE_OF 对成组判。
    """
    pairs = [
        ("synthetic.llm_usage", "openai_compat", "ALL_STAGES"),
        ("synthetic.trace.summary", "sdk", "ONE_OF"),
        ("synthetic.trace.summary", "clickhouse", "ONE_OF"),
    ]
    observed = [
        ("synthetic.llm_usage", "openai_compat"),
        ("synthetic.trace.summary", "sdk"),
    ]
    manifest = smoke_gate(policy="ALL_STAGES", pairs=pairs, observed=observed)

    steps = vl.gate_smoke(manifest)

    assert [step.index for step in steps] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert steps[6].index == 7
    assert "policy=ALL_STAGES" in steps[6].detail
    assert "ALL_STAGES 1/1" in steps[6].detail
    assert "ONE_OF[synthetic.trace.summary] 1/2" in steps[6].detail


def test_smoke_gate_one_of_group_passes_with_both_stages_observed(vl, smoke_gate) -> None:
    """两组都存在时同样闭合，并如实报 2/2。"""
    pairs = [
        ("synthetic.trace.summary", "sdk", "ONE_OF"),
        ("synthetic.trace.summary", "clickhouse", "ONE_OF"),
    ]
    observed = [
        ("synthetic.trace.summary", "sdk"),
        ("synthetic.trace.summary", "clickhouse"),
    ]
    manifest = smoke_gate(policy="ALL_STAGES", pairs=pairs, observed=observed)

    steps = vl.gate_smoke(manifest)

    assert "ONE_OF[synthetic.trace.summary] 2/2" in steps[6].detail


def test_smoke_gate_fails_when_one_of_group_entirely_unobserved(vl, smoke_gate) -> None:
    """ONE_OF 组一对都没观测到 → 失败（fail closed），且不被其他 producer 顶替。"""
    pairs = [
        ("synthetic.llm_usage", "openai_compat", "ALL_STAGES"),
        ("synthetic.trace.summary", "sdk", "ONE_OF"),
        ("synthetic.trace.summary", "clickhouse", "ONE_OF"),
    ]
    manifest = smoke_gate(
        policy="ALL_STAGES",
        pairs=pairs,
        observed=[("synthetic.llm_usage", "openai_compat")],
    )

    with pytest.raises(vl.GateFailure) as excinfo:
        vl.gate_smoke(manifest)

    message = str(excinfo.value)
    assert "ONE_OF" in message and "synthetic.trace.summary" in message


def test_smoke_gate_does_not_criss_cross_one_of_producers(vl, smoke_gate) -> None:
    """不同 producer_id 的 ONE_OF 对各自成组：A 的观测不能顶替 B 的要求。"""
    pairs = [
        ("synthetic.trace.alpha", "sdk", "ONE_OF"),
        ("synthetic.trace.beta", "clickhouse", "ONE_OF"),
    ]
    manifest = smoke_gate(
        policy="ALL_STAGES", pairs=pairs, observed=[("synthetic.trace.alpha", "sdk")]
    )

    with pytest.raises(vl.GateFailure) as excinfo:
        vl.gate_smoke(manifest)

    assert "synthetic.trace.beta" in str(excinfo.value)


def test_smoke_gate_pair_without_requirement_follows_top_level_all_stages(vl, smoke_gate) -> None:
    """无 per-pair 要求 → 回退顶层 ALL_STAGES：每一对都必须观测到。"""
    pairs = [
        ("synthetic.llm_usage", "openai_compat", None),
        ("synthetic.llm_usage", "chat_completion", None),
    ]
    manifest = smoke_gate(
        policy="ALL_STAGES",
        pairs=pairs,
        observed=[("synthetic.llm_usage", "openai_compat")],
    )

    with pytest.raises(vl.GateFailure) as excinfo:
        vl.gate_smoke(manifest)

    assert "chat_completion" in str(excinfo.value)


def test_smoke_gate_pair_without_requirement_follows_top_level_one_of(vl, smoke_gate) -> None:
    """无 per-pair 要求 → 回退顶层 ONE_OF：同 producer 组内 1/2 即闭合。"""
    pairs = [
        ("synthetic.trace.summary", "sdk", None),
        ("synthetic.trace.summary", "clickhouse", None),
    ]
    manifest = smoke_gate(
        policy="ONE_OF", pairs=pairs, observed=[("synthetic.trace.summary", "sdk")]
    )

    steps = vl.gate_smoke(manifest)

    assert "ONE_OF[synthetic.trace.summary] 1/2" in steps[6].detail


# --------------------------------------------------------------------------- #
# gate: smoke 第 7 步 —— ``NOT_OBSERVED_ALLOWED``（母 Spec §7.3:896）
# --------------------------------------------------------------------------- #
# 规范依据：§7.3:896「`normal` required；错误 stage 可 `NOT_OBSERVED`」+ §7.3:887
# 「'未观察到'不等于失败」。该值只管**逐对**：未观测不使 gate 失败，但必须被逐对点名
# （"未观测 ≠ 通过"），且不得作为顶层 `coverage_policy`。


def test_smoke_gate_not_observed_allowed_pair_passes_and_names_it(vl, smoke_gate, capsys) -> None:
    """(A/B) 授权可未观测的一对未观测 → gate **通过**（exit 0），且在文本里被逐对点名。"""
    pairs = [
        ("synthetic.llm_usage", "openai_compat", "ALL_STAGES"),
        ("synthetic.benchmark.case_cost", "normal", "ALL_STAGES"),
        ("synthetic.benchmark.case_cost", "environment_error", "NOT_OBSERVED_ALLOWED"),
        ("synthetic.trace.summary", "sdk", "NOT_OBSERVED_ALLOWED"),
    ]
    observed = [
        ("synthetic.llm_usage", "openai_compat"),
        ("synthetic.benchmark.case_cost", "normal"),
    ]
    manifest = smoke_gate(policy="ALL_STAGES", pairs=pairs, observed=observed)

    assert vl.main(["--gate", "smoke", "--run-manifest", str(manifest)]) == 0
    assert "GATE FAILED" not in capsys.readouterr().err

    steps = vl.gate_smoke(manifest)
    detail = steps[6].detail
    # 具名：不许静默通过，也不许把未观测说成 covered/通过
    assert "synthetic.benchmark.case_cost/environment_error" in detail
    assert "synthetic.trace.summary/sdk" in detail
    assert "未观测" in detail
    assert "NOT_OBSERVED_ALLOWED 0/2" in detail
    # 未被计入 ALL_STAGES / ONE_OF
    assert "ALL_STAGES 2/2" in detail
    assert "ONE_OF" not in detail


def test_smoke_gate_not_observed_allowed_pair_observed_is_not_listed_unobserved(vl, smoke_gate) -> None:
    """该对**已**观测到 → 如实报 1/1 且未观测列表为空（不无中生有）。"""
    pairs = [
        ("synthetic.llm_usage", "openai_compat", "ALL_STAGES"),
        ("synthetic.trace.summary", "sdk", "NOT_OBSERVED_ALLOWED"),
    ]
    observed = [
        ("synthetic.llm_usage", "openai_compat"),
        ("synthetic.trace.summary", "sdk"),
    ]
    manifest = smoke_gate(policy="ALL_STAGES", pairs=pairs, observed=observed)

    detail = vl.gate_smoke(manifest)[6].detail

    assert "NOT_OBSERVED_ALLOWED 1/1" in detail
    assert "未观测：无" in detail


def test_smoke_gate_not_observed_allowed_does_not_rescue_other_pairs(vl, smoke_gate) -> None:
    """豁免只对本对生效：同 producer 的 ALL_STAGES 对仍必须观测到（不弱化门）。"""
    pairs = [
        ("synthetic.benchmark.case_cost", "normal", "ALL_STAGES"),
        ("synthetic.benchmark.case_cost", "error", "NOT_OBSERVED_ALLOWED"),
    ]
    manifest = smoke_gate(
        policy="ALL_STAGES",
        pairs=pairs,
        observed=[("synthetic.trace.summary", "sdk")],
    )

    with pytest.raises(vl.GateFailure) as excinfo:
        vl.gate_smoke(manifest)

    message = str(excinfo.value)
    assert "ALL_STAGES" in message and "normal" in message
    assert "error" not in message  # 被豁免的那对不得出现在 ALL_STAGES 失败清单里


def test_smoke_gate_rejects_not_observed_allowed_as_top_level_policy(vl, smoke_gate) -> None:
    """顶层策略不接受 `NOT_OBSERVED_ALLOWED`（整仓放宽会掩盖未观测）→ 用法错误。"""
    pairs = [("synthetic.llm_usage", "openai_compat", "NOT_OBSERVED_ALLOWED")]
    manifest = smoke_gate(
        policy="NOT_OBSERVED_ALLOWED",
        pairs=pairs,
        observed=[("synthetic.llm_usage", "openai_compat")],
    )

    with pytest.raises(vl.UsageError) as excinfo:
        vl.gate_smoke(manifest)

    assert "coverage_policy" in str(excinfo.value)
    assert vl.main(["--gate", "smoke", "--run-manifest", str(manifest)]) == vl.EXIT_USAGE


def test_smoke_gate_rejects_unknown_evidence_requirement(vl, smoke_gate) -> None:
    """逐对闭集之外的取值（例如温和命名 `OPTIONAL`）一律用法错误，不得静默放行。"""
    pairs = [("synthetic.llm_usage", "openai_compat", "OPTIONAL")]
    manifest = smoke_gate(
        policy="ALL_STAGES",
        pairs=pairs,
        observed=[("synthetic.llm_usage", "openai_compat")],
    )

    with pytest.raises(vl.UsageError) as excinfo:
        vl.gate_smoke(manifest)

    message = str(excinfo.value)
    assert "OPTIONAL" in message and "NOT_OBSERVED_ALLOWED" in message


def test_per_pair_vocabulary_and_policy_sets_are_frozen(vl) -> None:
    """字面量守卫：逐对闭集与顶层策略闭集都不含含糊值（`OPTIONAL` 之类）。"""
    assert vl.PER_PAIR_EVIDENCE_REQUIREMENTS == (
        "ALL_STAGES",
        "ONE_OF",
        "NOT_OBSERVED_ALLOWED",
    )
    assert vl.COVERAGE_POLICIES == ("ALL_STAGES", "ONE_OF")
    assert "NOT_OBSERVED_ALLOWED" not in vl.COVERAGE_POLICIES
