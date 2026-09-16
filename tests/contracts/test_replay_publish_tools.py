"""Spec 10 工具开发验证：`replay_history.py` / `publish_evidence.py`（Coding 仓）。

本文件只做**工具开发验证**，不是 Spec 12/14 的正式 Evidence 落点：

* 不为任何测试函数加 ``contract_rule`` 装饰器（`EVD-PUB-*` 的正式 traceability
  归属 Spec 14），避免制造误导性的 rule 覆盖。
* 所有文件系统操作都在 ``tmp_path`` 内完成，**绝不**写真实
  ``output/contract-validation/`` 或 ``docs/contracts/``。
* 覆盖范围：manifest 校验退出码、扫描器五类命中、sanitizer 不调用 Adapter、
  corpus 记录字段 allowlist、publisher 的 allowlist 构造 / Evidence Manifest 无自引用 /
  changelog append-only / 非法 candidate 失败。

本仓差异：Adapter 在 ``adapters/foundation``；受控历史来源目录是 ``benchmark/cases``；
币种为 ``USD``（``adapters/foundation/facts.py`` 的 ``CURRENCY_CONTEXT``）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "contracts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import publish_evidence as pe  # noqa: E402
import replay_history as rh  # noqa: E402

COMMIT = "a" * 40
HEAD = "f" * 40
RUN_ID = "test-run-1"

#: Coding 的受控来源根（benchmark artifact）。
SOURCE_DIR = Path("benchmark") / "cases"


# --------------------------------------------------------------------------- #
# 夹具：在 tmp_path 内造一个最小但自洽的仓库
# --------------------------------------------------------------------------- #


def _registry(repository: str) -> dict:
    return {
        "registry_version": "1",
        "contract_version": "0.1.0",
        "repository": repository,
        "producers": [
            {
                "producer_id": f"{repository}.benchmark.case_cost",
                "mapping_stages": ["normal"],
                "status": "IN_SCOPE",
                "source_locations": [],
                "legacy_fields": ["cost_usd", "prompt_tokens", "completion_tokens"],
                "foundation_objects": ["Usage", "Cost"],
                "evidence_requirement": "ALL_STAGES",
            },
            {
                "producer_id": f"{repository}.sandbox",
                "mapping_stages": [],
                "status": "DEFERRED",
                "source_locations": [],
                "legacy_fields": [],
                "foundation_objects": ["Usage", "Cost"],
                "evidence_requirement": "NOT_APPLICABLE",
                "reason": "sandbox_safety_boundary_out_of_v0.1_scope",
            },
        ],
    }


def _write_payload(root: Path) -> None:
    package = root / "agent_core"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    contracts = package / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "canonicalization_version": "1",
        "contract_version": "0.1.0",
        "families": ["foundation"],
        "normative_rule_set": ["EVD-PUB-001", "EVD-PUB-004", "FND-MAP-001", "FND-MAP-002"],
        "pydantic_version": "2.13.4",
        "schema_hashes": {},
        "schema_set_hash": "sha256:" + "b" * 64,
        "schema_tooling_version": "1",
    }
    (contracts / "payload-descriptor.json").write_text(
        json.dumps(descriptor), encoding="utf-8"
    )


def _write_sources(root: Path, repository: str) -> None:
    """写入本仓受控来源：`benchmark/cases/**`（真实 benchmark artifact 形态）。"""
    cases = root / SOURCE_DIR
    cases.mkdir(parents=True, exist_ok=True)
    (cases / "case-a.json").write_text(
        json.dumps(
            {
                "id": "case-a",
                "category": "feature",
                "prompt_tokens": 631,
                "completion_tokens": 775,
                "cost_usd": 0.0006543,
            }
        ),
        encoding="utf-8",
    )
    # 只带成本、无 token：覆盖 presence-aware 分支
    (cases / "case-b.json").write_text(
        json.dumps({"id": "case-b", "cost_usd": 0.5}), encoding="utf-8"
    )
    # 既无 token 也无 cost：必须判 LEGACY_DATA_INSUFFICIENT
    (cases / "case-c.json").write_text(
        json.dumps({"id": "case-c", "issue": {"title": "no usage facts"}}), encoding="utf-8"
    )
    # 第三个来源用 JSON 数组形态，覆盖 `_iter_records` 的分支
    (cases / "cases.json").write_text(
        json.dumps(
            [{"id": "case-d", "prompt_text": "raw", "prompt_tokens": 7, "cost_usd": 0.01}]
        ),
        encoding="utf-8",
    )


def _replay_manifest(root: Path, repository: str, payload_hash: str) -> dict:
    return {
        "manifest_version": "1",
        "run_id": RUN_ID,
        "contract_mode": "strict",
        "contract_version": "0.1.0",
        "payload_hash": payload_hash,
        "repository_commit": None,
        "output_dir": "output/contract-validation/staging",
        "sources": [
            {
                "source_id": "src-case-a",
                "source_class": "benchmark",
                "path": "benchmark/cases/case-a.json",
                "producer_id": f"{repository}.benchmark.case_cost",
                "mapping_stage": "normal",
                "runtime_adapter_status": "IN_SCOPE",
                "evidence_role": "historical_replay_only",
            },
            {
                "source_id": "src-case-c",
                "source_class": "benchmark",
                "path": "benchmark/cases/case-c.json",
                "producer_id": f"{repository}.benchmark.case_cost",
                "mapping_stage": "normal",
                "runtime_adapter_status": "IN_SCOPE",
                "evidence_role": "historical_replay_only",
            },
            {
                "source_id": "src-deferred-json",
                "source_class": "other",
                "path": "benchmark/cases/cases.json",
                "producer_id": f"{repository}.sandbox",
                "mapping_stage": None,
                "runtime_adapter_status": "DEFERRED",
                "evidence_role": "historical_replay_only",
            },
        ],
        "max_records": 10,
        "reproduction_restriction": "NONE",
    }


def _smoke_manifest(repository: str, payload_hash: str) -> dict:
    return {
        "manifest_version": "1",
        "run_id": "test-smoke-1",
        "contract_mode": "observe",
        "contract_version": "0.1.0",
        "payload_hash": payload_hash,
        "candidate_commit": None,
        "repository_commit": None,
        "evidence_root": "output/contract-validation/staging",
        "coverage_policy": "ALL_STAGES",
        "required_producer_stages": [
            {
                "producer_id": f"{repository}.benchmark.case_cost",
                "mapping_stage": "normal",
                "evidence_requirement": "ALL_STAGES",
            }
        ],
        "model": "model-x",
        "provider": "provider-x",
        "case_ids": ["case-a"],
        "expected_calls": {f"{repository}.benchmark.case_cost": 1},
        "max_calls": 4,
        "estimated_cost_cap": {"amount": "1", "currency": "USD"},
        "network_requirement": "provider",
        "side_effect_policy": "read_only",
        "timeout_seconds": 60,
        "duration_cap_seconds": 120,
    }


def _build_repo(tmp_path: Path, repository: str = "coding") -> dict:
    """在 tmp_path 内构造 payload + registry + 两个 config + 真实来源文件。"""
    _write_payload(tmp_path)
    _write_sources(tmp_path, repository)

    config_dir = tmp_path / "config" / "contracts"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "producer-registry.json").write_text(
        json.dumps(_registry(repository)), encoding="utf-8"
    )

    payload_hash = rh.canonical_file_bundle_digest(tmp_path)[0]
    manifest = _replay_manifest(tmp_path, repository, payload_hash)
    manifest_path = config_dir / "replay-v0.1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (config_dir / "smoke-v0.1.json").write_text(
        json.dumps(_smoke_manifest(repository, payload_hash)), encoding="utf-8"
    )
    return {
        "manifest_path": manifest_path,
        "manifest": manifest,
        "payload_hash": payload_hash,
        "registry": _registry(repository),
        "staging": tmp_path / "output" / "contract-validation" / "staging",
        "env": {
            "AGENT_CONTRACT_MODE": "strict",
            "AGENT_CONTRACT_RUN_ID": RUN_ID,
            "AGENT_CONTRACT_COMMIT": COMMIT,
        },
    }


def _run(tmp_path: Path, repo: dict, **overrides):
    kwargs = {"verify_payload": True, "verify_head": True}
    kwargs.update(overrides)
    return rh.run_replay(
        manifest_path=repo["manifest_path"], repo_root=tmp_path, env=repo["env"], **kwargs
    )


# --------------------------------------------------------------------------- #
# 1) manifest 校验与退出码
# --------------------------------------------------------------------------- #


def test_manifest_valid_returns_ok_and_writes_only_tmp_staging(tmp_path):
    repo = _build_repo(tmp_path)
    outcome = _run(tmp_path, repo)

    assert outcome.exit_code == rh.EXIT_OK
    assert outcome.result == "PASS"
    staging = repo["staging"] / RUN_ID
    assert (staging / "l2-result.json").is_file()
    assert (staging / "sanitized-replay-corpus.jsonl").is_file()
    assert not (tmp_path / "docs" / "contracts").exists()


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        (lambda m: m.pop("contract_version"), rh.EXIT_USAGE),
        (lambda m: m.pop("sources"), rh.EXIT_USAGE),
        (lambda m: m.update({"max_records": "many"}), rh.EXIT_USAGE),
        (lambda m: m.update({"contract_mode": "observe"}), rh.EXIT_USAGE),
        (lambda m: m.update({"reproduction_restriction": "MAYBE"}), rh.EXIT_USAGE),
        (lambda m: m.update({"payload_hash": "deadbeef"}), rh.EXIT_USAGE),
        (lambda m: m.update({"output_dir": "output/contract-validation/raw"}), rh.EXIT_USAGE),
        (lambda m: m.update({"repository_commit": "not-a-sha"}), rh.EXIT_USAGE),
    ],
)
def test_manifest_field_violations_are_usage_errors(tmp_path, mutation, expected_code):
    repo = _build_repo(tmp_path)
    manifest = json.loads(repo["manifest_path"].read_text(encoding="utf-8"))
    mutation(manifest)
    repo["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(rh.ToolError) as excinfo:
        _run(tmp_path, repo)
    assert excinfo.value.exit_code == expected_code


def test_unknown_producer_and_stage_mismatch_are_usage_errors(tmp_path):
    repo = _build_repo(tmp_path)
    pristine = repo["manifest_path"].read_text(encoding="utf-8")

    for source_index, mutate, fragment in (
        (
            0,
            lambda s: s.update({"producer_id": "coding.ghost.producer"}),
            "不在本仓 producer registry",
        ),
        (0, lambda s: s.update({"mapping_stage": "environment_error"}), "登记 stage"),
        (2, lambda s: s.update({"runtime_adapter_status": "IN_SCOPE"}), "runtime_adapter_status"),
    ):
        repo["manifest_path"].write_text(pristine, encoding="utf-8")
        manifest = json.loads(pristine)
        mutate(manifest["sources"][source_index])
        repo["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(rh.ToolError) as excinfo:
            _run(tmp_path, repo)
        assert excinfo.value.exit_code == rh.EXIT_USAGE
        assert fragment in excinfo.value.message


def test_deferred_source_requires_deferred_status_and_null_stage(tmp_path):
    repo = _build_repo(tmp_path)
    manifest = json.loads(repo["manifest_path"].read_text(encoding="utf-8"))
    manifest["sources"][2]["mapping_stage"] = "normal"
    repo["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(rh.ToolError) as excinfo:
        _run(tmp_path, repo)
    assert excinfo.value.exit_code == rh.EXIT_USAGE
    assert "DEFERRED producer" in excinfo.value.message


def test_payload_hash_divergence_is_usage_error(tmp_path):
    repo = _build_repo(tmp_path)
    manifest = json.loads(repo["manifest_path"].read_text(encoding="utf-8"))
    manifest["payload_hash"] = "sha256:" + "c" * 64
    repo["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(rh.ToolError) as excinfo:
        _run(tmp_path, repo)
    assert excinfo.value.exit_code == rh.EXIT_USAGE
    assert "DIVERGED" in excinfo.value.message


def test_runtime_commit_null_requires_explicit_env(tmp_path):
    repo = _build_repo(tmp_path)
    env = {k: v for k, v in repo["env"].items() if k != "AGENT_CONTRACT_COMMIT"}
    with pytest.raises(rh.ToolError) as excinfo:
        rh.run_replay(
            manifest_path=repo["manifest_path"], repo_root=tmp_path, env=env
        )
    assert excinfo.value.exit_code == rh.EXIT_USAGE
    assert rh.COMMIT_ENV in excinfo.value.message

    env["AGENT_CONTRACT_COMMIT"] = "not-hex"
    with pytest.raises(rh.ToolError) as excinfo:
        rh.run_replay(manifest_path=repo["manifest_path"], repo_root=tmp_path, env=env)
    assert excinfo.value.exit_code == rh.EXIT_USAGE


def test_main_cli_exit_codes_for_env_and_usage(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    # CLI 直跑（repo_root 缺省 = REPO_ROOT）：把 REPO_ROOT 指向 tmp_path，确保
    # 测试**绝不**触碰真实 output/ 目录。
    monkeypatch.setattr(rh, "REPO_ROOT", tmp_path)
    argv = ["--run-manifest", str(repo["manifest_path"])]

    monkeypatch.delenv(rh.CONTRACT_MODE_ENV, raising=False)
    monkeypatch.delenv(rh.RUN_ID_ENV, raising=False)
    assert rh.main(argv) == rh.EXIT_USAGE

    monkeypatch.setenv(rh.CONTRACT_MODE_ENV, "observe")
    monkeypatch.setenv(rh.RUN_ID_ENV, RUN_ID)
    assert rh.main(argv) == rh.EXIT_USAGE

    monkeypatch.setenv(rh.CONTRACT_MODE_ENV, "strict")
    monkeypatch.setenv(rh.COMMIT_ENV, COMMIT)
    assert rh.main(argv) == rh.EXIT_OK
    assert (repo["staging"] / RUN_ID / "l2-result.json").is_file()

    monkeypatch.setenv(rh.RUN_ID_ENV, "different-run")
    assert rh.main(argv) == rh.EXIT_USAGE

    with pytest.raises(SystemExit) as excinfo:
        rh.main(["--run-manifest", str(repo["manifest_path"]), "--extra", "x"])
    assert excinfo.value.code == rh.EXIT_USAGE


def test_replay_cli_exposes_only_run_manifest_parameter():
    parser = rh.build_parser()
    dests = {action.dest for action in parser._actions} - {"help"}
    assert dests == {"run_manifest"}


# --------------------------------------------------------------------------- #
# 2) 扫描器
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        "key=sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer abcdefgh12345678",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----",
        "opaque " + "0123456789abcdef" * 3,
    ],
)
def test_scanner_flags_secret_patterns(payload):
    findings = rh.scan_text(payload, location="unit")
    assert any(item.kind == "secret_pattern" for item in findings), findings


@pytest.mark.parametrize(
    "payload",
    [
        r"C:\Users\alice\private\cost_log.jsonl",
        "/home/alice/private/cost_log.jsonl",
        "/opt/app/secret.json",
    ],
)
def test_scanner_flags_absolute_paths(payload):
    findings = rh.scan_text(payload, location="unit")
    assert any(item.kind == "absolute_path" for item in findings), findings


def test_scanner_declared_identities_are_not_secrets():
    digest = "sha256:" + "a" * 64
    commit = "b" * 40
    assert rh.scan_text(digest) == []
    assert rh.scan_text(commit) == []
    # 散文中未声明的 commit 仍被判为 opaque token
    prose = f"head is {commit} in this sentence"
    assert any(item.kind == "secret_pattern" for item in rh.scan_text(prose))
    # 显式声明后不再命中
    assert rh.scan_text(prose, declared_identities=[commit]) == []


def test_scanner_flags_forbidden_keys_without_false_positives():
    findings = rh.scan_structure(
        {
            "prompt": "raw",
            "nested": {"api_key": "x", "traceback": "y"},
            "input_tokens": 10,
            "total_tokens": 20,
        },
        location="unit",
    )
    kinds = {(item.kind, item.location) for item in findings}
    assert ("forbidden_key", "unit.prompt") in kinds
    assert ("forbidden_key", "unit.nested.api_key") in kinds
    assert ("forbidden_key", "unit.nested.traceback") in kinds
    assert all("tokens" not in item.location for item in findings)


def test_scanner_flags_oversize_record():
    record = {"record_id": "r-1", "blob": "x" * (64 * 1024)}
    findings = rh.check_record_size(record, location="corpus[r-1]")
    assert [item.kind for item in findings] == ["oversize_record"]


def test_scanner_flags_bom_and_invalid_utf8():
    text, findings = rh.decode_utf8_strict(b"\xef\xbb\xbf{}\n")
    assert text is None
    assert [item.kind for item in findings] == ["bom"]

    text, findings = rh.decode_utf8_strict(b"\xff\xfe\x00bad")
    assert text is None
    assert findings and findings[0].kind in {"invalid_utf8", "nul_byte"}


def test_source_file_with_bom_fails_with_exit_code_1(tmp_path):
    repo = _build_repo(tmp_path)
    case = tmp_path / "benchmark" / "cases" / "case-a.json"
    case.write_bytes(b"\xef\xbb\xbf" + case.read_bytes())
    with pytest.raises(rh.ToolError) as excinfo:
        _run(tmp_path, repo)
    assert excinfo.value.exit_code == rh.EXIT_VALIDATION_FAILED


# --------------------------------------------------------------------------- #
# 3) sanitizer 不执行 contract mapping，且 corpus 记录是严格 allowlist
# --------------------------------------------------------------------------- #


def test_sanitizer_never_calls_adapter_mapping(monkeypatch):
    record = {"model": "m", "tokens_in": 1, "tokens_out": 2, "cost": 0.5}
    facts = rh.extract_legacy_facts(record)  # 允许触碰 Adapter（币种上下文）

    def _forbidden(*args, **kwargs):  # pragma: no cover - 命中即失败
        raise AssertionError("sanitizer / corpus 构造不得调用 Adapter mapping")

    monkeypatch.setattr(rh, "map_to_foundation", _forbidden)
    monkeypatch.setattr(rh, "load_adapter", _forbidden)

    sanitized = rh.sanitize_legacy_facts(facts)
    assert sanitized["input_tokens"] == 1
    corpus = rh.build_corpus_record(
        source_id="src",
        source_class="cost_log",
        ordinal=1,
        legacy_facts=sanitized,
        expected_mapping={"producer_id": "p", "mapping_stage": "s", "foundation_objects": []},
        actual_mapping={"status": "OBSERVED", "mapping_outcome": "OBSERVED"},
        runtime_adapter_status="IN_SCOPE",
    )
    assert set(corpus) == set(rh.CORPUS_RECORD_KEYS)
    assert corpus["sanitized_record_hash"] == rh.record_hash(corpus)


def test_sanitizer_masks_secrets_and_paths():
    assert "<SECRET>" in rh.sanitize_text("key sk-abcdefghijklmnop")
    assert "<WORKSPACE>" in rh.sanitize_text(r"C:\Users\alice\x.json")
    assert rh.sanitize_legacy_facts({"prompt": "raw"}) == {}


def test_corpus_records_are_allowlisted_and_free_of_raw_content(tmp_path):
    repo = _build_repo(tmp_path)
    outcome = _run(tmp_path, repo)

    assert outcome.corpus_records
    for record in outcome.corpus_records:
        assert set(record) == set(rh.CORPUS_RECORD_KEYS)
        assert record["evidence_role"] == "historical_replay_only"
        assert record["sanitized_record_hash"] == rh.record_hash(record)
        blob = json.dumps(record)
        for forbidden in ("prompt_text", "answer", "reasoning", "response_body"):
            assert forbidden not in blob

    statuses = {record["actual_mapping"]["status"] for record in outcome.corpus_records}
    assert statuses == {"OBSERVED", "LEGACY_DATA_INSUFFICIENT", "NOT_OBSERVED"}

    deferred = [r for r in outcome.corpus_records if r["runtime_adapter_status"] == "DEFERRED"]
    assert deferred and all(r["actual_mapping"]["status"] == "NOT_OBSERVED" for r in deferred)


def test_reproduction_restriction_labels_records_but_keeps_outcome(tmp_path):
    repo = _build_repo(tmp_path)
    manifest = json.loads(repo["manifest_path"].read_text(encoding="utf-8"))
    manifest["reproduction_restriction"] = "REPRODUCTION_RESTRICTED"
    repo["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

    outcome = _run(tmp_path, repo)
    observed = [
        r
        for r in outcome.corpus_records
        if r["actual_mapping"]["mapping_outcome"] == "OBSERVED"
    ]
    assert observed
    assert all(
        r["actual_mapping"]["status"] == "REPRODUCTION_RESTRICTED" for r in observed
    )


def test_l2_result_contains_coverage_matrix_and_ancestor_note(tmp_path):
    repo = _build_repo(tmp_path)
    outcome = _run(tmp_path, repo)
    l2 = outcome.l2_result

    assert l2["ancestor_check"]["applicable"] is False
    rule_ids = {entry["rule_id"] for entry in l2["coverage_matrix"]["rules"]}
    assert rule_ids == set(rh.REPLAY_RULES)
    assert l2["raw_retention"]["copied_into_git"] is False
    assert l2["scan"]["failed"] == 0
    for source in l2["sources"]:
        assert set(source["by_status"]) == set(rh.CLASSIFICATIONS)
        # 来源 artifact 必须按内容绑定（可审计、可复现）
        assert source["sha256"].startswith("sha256:")
        assert source["sha256"] == rh.sha256_hex((tmp_path / source["path"]).read_bytes())
    assert len(l2["raw_retention"]["source_sha256"]) >= 1
    for record in l2["records"]:
        assert set(record) >= {"record_id", "source_id", "status", "mapping_outcome"}


def test_mapping_failure_is_recorded_per_record_not_fatal(tmp_path, monkeypatch):
    """strict 模式下 mapping 失败只作为该记录的 FAIL/Adapter defect，不中断 run。"""
    repo = _build_repo(tmp_path)
    real_map = rh.map_to_foundation

    def _defective(source, facts, *, pricing_snapshot=None):
        result = real_map(source, facts, pricing_snapshot=pricing_snapshot)
        return rh.MappingResult(
            mapping_outcome="NOT_OBSERVED",
            foundation_objects=[],
            usage_source=None,
            cost_source=None,
            cost_amount_present=False,
            error_code="foundation.invalid_usage",
            difference_class="Adapter defect",
        )

    monkeypatch.setattr(rh, "map_to_foundation", _defective)
    outcome = _run(tmp_path, repo)

    # mapping 失败不产生 traceback；run 级判定为 CONTRACT_FEEDBACK（strict 语义）
    assert outcome.exit_code == rh.EXIT_VALIDATION_FAILED
    assert outcome.l2_result["totals"]["adapter_defects"] >= 1
    assert outcome.result == "CONTRACT_FEEDBACK"
    assert len(outcome.corpus_records) == 3


# --------------------------------------------------------------------------- #
# 4) publisher
# --------------------------------------------------------------------------- #


def _publish(tmp_path, monkeypatch, *, repository="coding", dry_run=False, candidate=COMMIT, **kwargs):
    repo = _build_repo(tmp_path, repository)
    run = _run(tmp_path, repo)
    assert run.exit_code == rh.EXIT_OK
    monkeypatch.setattr(pe, "require_candidate_ancestor", lambda cand, root: HEAD)
    outcome = pe.publish(
        candidate=candidate,
        release_version="0.1.0",
        repository=repository,
        dry_run=dry_run,
        repo_root=tmp_path,
        env={},
        root=repo["staging"],
        **kwargs,
    )
    return repo, outcome


def test_allowlist_is_an_explicit_allowlist_including_changelog():
    allowlist = pe.release_allowlist("0.1.0", "coding")
    assert "docs/contracts/releases/0.1.0/changelog.md" in allowlist  # G-08 显式收敛
    assert "docs/contracts/releases/0.1.0/validation/coding/evidence-manifest.json" in allowlist
    for forbidden in pe.FORBIDDEN_RELEASE_ARTIFACTS:
        assert not any(path.endswith(forbidden) for path in allowlist)


def test_contract_manifest_fields_are_exactly_the_documented_set():
    manifest = pe.construct_contract_manifest(
        contract_version="0.1.0",
        payload_hash="sha256:" + "a" * 64,
        payload_descriptor_hash="sha256:" + "b" * 64,
        schema_set_hash="sha256:" + "c" * 64,
        canonical_payload_commit=COMMIT,
    )
    assert set(manifest) == set(pe.CONTRACT_MANIFEST_KEYS)
    assert manifest["canonical_payload_repository"] == "wiki"


def test_evidence_manifest_has_no_self_hash_and_no_b_sha():
    payload_hash = "sha256:" + "9" * 64
    manifest = pe.construct_evidence_manifest(
        contract_version="0.1.0",
        payload_hash=payload_hash,
        candidate=COMMIT,
        repository="coding",
        evidence={
            "contract_tests": "PASS",
            "historical_replay": "PASS",
            "fresh_smoke": "NOT_OBSERVED",
            "full_regression": "PASS",
        },
        producer_coverage=[{"producer_id": "p", "mapping_stage": "s"}],
        benchmark_reference_status="BENCHMARK_BASELINE_NOT_REPRODUCIBLE",
    )
    assert set(manifest) == set(pe.EVIDENCE_MANIFEST_KEYS)
    assert "evidence_manifest_hash" not in manifest
    assert "evidence_commit" not in manifest
    blob = json.dumps(manifest)
    assert blob.count(COMMIT) == 1  # 只允许出现在 verified_repository_commit
    assert manifest["verified_repository_commit"] == COMMIT


def test_publish_writes_only_allowlisted_paths_under_tmp(tmp_path, monkeypatch):
    _, outcome = _publish(tmp_path, monkeypatch)

    assert outcome.exit_code == pe.EXIT_OK
    release_dir = tmp_path / "docs" / "contracts" / "releases" / "0.1.0"
    written = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in release_dir.rglob("*")
        if path.is_file()
    )
    assert written == sorted(pe.release_allowlist("0.1.0", "coding"))

    evidence_manifest = json.loads(
        (release_dir / "validation" / "coding" / "evidence-manifest.json").read_text(encoding="utf-8")
    )
    assert evidence_manifest["repository"] == "coding"
    assert evidence_manifest["evidence"]["fresh_smoke"] == "NOT_OBSERVED"
    assert "evidence_manifest_hash" not in evidence_manifest

    run_manifest = json.loads(
        (release_dir / "validation" / "coding" / "run-manifest.json").read_text(encoding="utf-8")
    )
    assert run_manifest["candidate_commit"] == COMMIT
    assert run_manifest["commands"]

    corpus_lines = (
        release_dir / "validation" / "coding" / "sanitized-replay-corpus.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert corpus_lines
    for line in corpus_lines:
        assert set(json.loads(line)) == set(rh.CORPUS_RECORD_KEYS)

    report = (release_dir / "validation" / "coding" / "validation-report.md").read_text(
        encoding="utf-8"
    )
    assert "A is ancestor of B" in report
    # 绝不产出 Spec 15 的产物
    assert not (release_dir / "cycle-report.json").exists()
    assert not (tmp_path / "docs" / "contracts" / "current.json").exists()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _, outcome = _publish(tmp_path, monkeypatch, dry_run=True)
    assert outcome.planned
    assert not (tmp_path / "docs").exists()


def test_changelog_is_appended_not_overwritten(tmp_path, monkeypatch):
    repo, _ = _publish(tmp_path, monkeypatch)
    changelog = tmp_path / "docs" / "contracts" / "releases" / "0.1.0" / "changelog.md"
    first = changelog.read_text(encoding="utf-8")

    monkeypatch.setattr(pe, "require_candidate_ancestor", lambda cand, root: HEAD)
    pe.publish(
        candidate=COMMIT,
        release_version="0.1.0",
        repository="coding",
        repo_root=tmp_path,
        env={},
        root=repo["staging"],
    )
    second = changelog.read_text(encoding="utf-8")
    assert second.startswith(first)
    assert len(second) > len(first)
    assert second.count("evidence publication") >= 2


def test_invalid_candidate_format_fails_with_exit_code_1(tmp_path, monkeypatch):
    repo = _build_repo(tmp_path)
    for bad in ("zzz", "0" * 39, "0" * 41, "A" * 40):
        with pytest.raises(pe.ToolError) as excinfo:
            pe.publish(
                candidate=bad,
                release_version="0.1.0",
                repo_root=tmp_path,
                env={},
                root=repo["staging"],
            )
        assert excinfo.value.exit_code == pe.EXIT_VALIDATION_FAILED


def test_non_ancestor_and_unknown_object_fail_with_exit_code_1(tmp_path, monkeypatch):
    def _unknown(repo_root, *args):
        return subprocess.CompletedProcess(args, 1, "", "fatal: Not a valid object name")

    monkeypatch.setattr(pe, "_run_git", _unknown)
    with pytest.raises(pe.ToolError) as excinfo:
        pe.require_candidate_ancestor("0" * 40, tmp_path)
    assert excinfo.value.exit_code == pe.EXIT_VALIDATION_FAILED
    assert "不存在或不是 commit" in excinfo.value.message

    def _not_ancestor(repo_root, *args):
        if args[0] == "cat-file":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, HEAD + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(pe, "_run_git", _not_ancestor)
    with pytest.raises(pe.ToolError) as excinfo:
        pe.require_candidate_ancestor(COMMIT, tmp_path)
    assert excinfo.value.exit_code == pe.EXIT_VALIDATION_FAILED
    assert "不是当前 HEAD" in excinfo.value.message


def test_publish_cli_keeps_frozen_parameters():
    parser = pe.build_parser()
    actions = {action.dest: action for action in parser._actions}
    assert {"candidate", "release_version"} <= set(actions)
    assert actions["candidate"].required
    assert actions["release_version"].required
    assert actions["repository"].required is False
    assert actions["dry_run"].required is False


def test_require_stages_must_match_registry(tmp_path):
    repo = _build_repo(tmp_path)
    smoke = json.loads(
        (tmp_path / "config" / "contracts" / "smoke-v0.1.json").read_text(encoding="utf-8")
    )
    smoke["required_producer_stages"][0]["mapping_stage"] = "judge"
    with pytest.raises(pe.ToolError) as excinfo:
        pe.validate_required_stages(smoke, repo["registry"])
    assert excinfo.value.exit_code == pe.EXIT_USAGE

    smoke["required_producer_stages"][0]["mapping_stage"] = "chat_completion"
    smoke["required_producer_stages"].append(
        {"producer_id": "coding.ghost", "mapping_stage": "x", "evidence_requirement": "ALL_STAGES"}
    )
    with pytest.raises(pe.ToolError):
        pe.validate_required_stages(smoke, repo["registry"])


# --------------------------------------------------------------------------- #
# 4b) `evidence_requirement` 的下限语义（母 Spec §7.3:887 / §7.3:896 / §7.3:897）
# --------------------------------------------------------------------------- #
# registry 的 producer 级值是**授权下限**：`NOT_OBSERVED_ALLOWED` 是 §7.3:896
# 「错误 stage 可 `NOT_OBSERVED`」的逐对形态，或用户依 N-04 授权的偏离（Wiki `scoring`、
# Coding `trace.summary`）。manifest 的逐对声明可以更严（同 producer 的 `normal`/`runner`
# 仍为 `ALL_STAGES`），但**不得更松** —— 放宽必须同时改 registry。
# 缺失该字段一律拒绝：不得为缺失项默认一个 required 值。


def _shipped_smoke(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "config" / "contracts" / "smoke-v0.1.json").read_text(encoding="utf-8")
    )


def _registry_with_floor(repo: dict, floor: str) -> dict:
    registry = json.loads(json.dumps(repo["registry"]))
    for producer in registry["producers"]:
        if producer["status"] == "IN_SCOPE":
            producer["evidence_requirement"] = floor
    return registry


def test_evidence_requirement_may_be_tightened_above_registry_floor(tmp_path):
    """逐对声明比 registry 下限更严 → 接受，并按**逐对声明**投影进 run-manifest。"""
    repo = _build_repo(tmp_path)
    smoke = _shipped_smoke(tmp_path)

    normalized = pe.validate_required_stages(
        smoke, _registry_with_floor(repo, "NOT_OBSERVED_ALLOWED")
    )

    assert [item["evidence_requirement"] for item in normalized] == ["ALL_STAGES"]
    projection = pe.construct_run_manifest(
        smoke,
        repository=repo["registry"]["repository"],
        candidate=COMMIT,
        required_stages=normalized,
    )
    assert projection["required_producer_stages"][0]["evidence_requirement"] == "ALL_STAGES"


def test_evidence_requirement_equal_to_registry_floor_is_projected_verbatim(tmp_path):
    """与 registry 下限同值的逐对声明（本轮锁定的偏离形态）原样进入 run-manifest 投影。"""
    repo = _build_repo(tmp_path)
    smoke = _shipped_smoke(tmp_path)
    smoke["required_producer_stages"][0]["evidence_requirement"] = "NOT_OBSERVED_ALLOWED"

    normalized = pe.validate_required_stages(
        smoke, _registry_with_floor(repo, "NOT_OBSERVED_ALLOWED")
    )

    assert [item["evidence_requirement"] for item in normalized] == ["NOT_OBSERVED_ALLOWED"]
    projection = pe.construct_run_manifest(
        smoke,
        repository=repo["registry"]["repository"],
        candidate=COMMIT,
        required_stages=normalized,
    )
    assert (
        projection["required_producer_stages"][0]["evidence_requirement"]
        == "NOT_OBSERVED_ALLOWED"
    )


def test_evidence_requirement_may_not_be_relaxed_below_registry(tmp_path):
    """逐对声明弱于 registry（registry 仍是 ALL_STAGES）→ exit 2，不得单方面放宽。"""
    repo = _build_repo(tmp_path)
    smoke = _shipped_smoke(tmp_path)
    smoke["required_producer_stages"][0]["evidence_requirement"] = "NOT_OBSERVED_ALLOWED"

    with pytest.raises(pe.ToolError) as excinfo:
        pe.validate_required_stages(smoke, repo["registry"])

    assert excinfo.value.exit_code == pe.EXIT_USAGE
    assert "registry" in excinfo.value.message


def test_missing_evidence_requirement_is_not_defaulted_to_required(tmp_path):
    """缺失该字段 → exit 2（绝不为缺失项默认一个 required 值）。"""
    repo = _build_repo(tmp_path)
    smoke = _shipped_smoke(tmp_path)
    smoke["required_producer_stages"][0].pop("evidence_requirement")

    with pytest.raises(pe.ToolError) as excinfo:
        pe.validate_required_stages(smoke, repo["registry"])

    assert excinfo.value.exit_code == pe.EXIT_USAGE
    assert "缺少 evidence_requirement" in excinfo.value.message


def test_unknown_evidence_requirement_is_rejected(tmp_path):
    """闭集之外的取值（`OPTIONAL` 之类的温和命名）不得静默通过。"""
    repo = _build_repo(tmp_path)
    smoke = _shipped_smoke(tmp_path)
    smoke["required_producer_stages"][0]["evidence_requirement"] = "OPTIONAL"

    with pytest.raises(pe.ToolError) as excinfo:
        pe.validate_required_stages(smoke, repo["registry"])

    assert excinfo.value.exit_code == pe.EXIT_USAGE
    assert "OPTIONAL" in excinfo.value.message


def test_not_observed_allowed_pair_is_reported_uncovered(tmp_path, monkeypatch):
    """(B) 诚实报告：未观测的授权对记 `covered=false` 并在报告里单列，绝不伪报覆盖。"""
    repo = _build_repo(tmp_path)
    config_dir = tmp_path / "config" / "contracts"
    (config_dir / "producer-registry.json").write_text(
        json.dumps(_registry_with_floor(repo, "NOT_OBSERVED_ALLOWED")), encoding="utf-8"
    )
    smoke = _shipped_smoke(tmp_path)
    smoke["required_producer_stages"][0]["evidence_requirement"] = "NOT_OBSERVED_ALLOWED"
    (config_dir / "smoke-v0.1.json").write_text(json.dumps(smoke), encoding="utf-8")

    run = _run(tmp_path, repo)
    assert run.exit_code == rh.EXIT_OK
    repository = repo["registry"]["repository"]
    monkeypatch.setattr(pe, "require_candidate_ancestor", lambda cand, root: HEAD)
    outcome = pe.publish(
        candidate=COMMIT,
        release_version="0.1.0",
        repository=repository,
        repo_root=tmp_path,
        env={},
        root=repo["staging"],
    )

    coverage = outcome.evidence["producer_coverage"]
    assert coverage
    assert all(item["evidence_requirement"] == "NOT_OBSERVED_ALLOWED" for item in coverage)
    assert all(item["covered"] is False for item in coverage), "未观测不得伪报为 covered"
    report = (
        tmp_path
        / "docs"
        / "contracts"
        / "releases"
        / "0.1.0"
        / "validation"
        / repository
        / "validation-report.md"
    ).read_text(encoding="utf-8")
    assert "NOT_OBSERVED_ALLOWED | false" in report
    assert "Authorized `NOT_OBSERVED_ALLOWED` pairs" in report


def test_canonical_payload_commit_env_override(tmp_path):
    assert pe._canonical_payload_commit({}, COMMIT) == COMMIT
    assert pe._canonical_payload_commit({pe.CANONICAL_COMMIT_ENV: HEAD}, COMMIT) == HEAD
    with pytest.raises(pe.ToolError) as excinfo:
        pe._canonical_payload_commit({pe.CANONICAL_COMMIT_ENV: "nope"}, COMMIT)
    assert excinfo.value.exit_code == pe.EXIT_USAGE


# --------------------------------------------------------------------------- #
# 5) 自检：落盘的 config 必须与**本仓** registry / Adapter 自洽
# --------------------------------------------------------------------------- #

#: 本仓 registry 中 `status == "IN_SCOPE"` 的 producer / stage 组合数（Coding: 4 / 7）。
IN_SCOPE_PRODUCERS = 4
IN_SCOPE_STAGES = 7


def test_shipped_manifests_match_this_repo_registry():
    """自检 5 项：producer/stage 归属、expected_calls 键、run_id 形态、source 登记、币种。"""
    registry = rh.load_registry(REPO_ROOT)
    repository = registry["repository"]
    assert repository == rh.REPOSITORY

    smoke = rh.load_manifest(REPO_ROOT / pe.SMOKE_MANIFEST_RELPATH)
    replay = rh.load_manifest(REPO_ROOT / pe.REPLAY_MANIFEST_RELPATH)

    # 1) smoke 的 required (producer, stage) 必须恰好等于本仓 IN_SCOPE 组合
    required = pe.validate_required_stages(smoke, registry)
    in_scope = set(rh.in_scope_stages(registry))
    assert {(item["producer_id"], item["mapping_stage"]) for item in required} == in_scope
    assert len({pid for pid, _ in in_scope}) == IN_SCOPE_PRODUCERS
    assert len(in_scope) == IN_SCOPE_STAGES

    # 2) expected_calls 的键必须是本仓 IN_SCOPE producer id
    assert set(smoke["expected_calls"]) == {pid for pid, _ in in_scope}
    assert smoke["max_calls"] >= sum(smoke["expected_calls"].values())
    assert all(isinstance(count, int) and count > 0 for count in smoke["expected_calls"].values())

    # 3) run_id 形态必须与本仓一致
    assert smoke["run_id"] == f"foundation-0_1_0-c1-{repository}-smoke"
    assert replay["run_id"] == f"foundation-0_1_0-c1-{repository}-replay"

    # 4) replay 的每个 producer_id 都必须登记过，且 (producer, stage) 与 registry 自洽
    sources = rh.validate_manifest(replay, registry, REPO_ROOT)
    assert sources
    for source in sources:
        assert source["producer_id"].startswith(f"{repository}.")
        assert source["evidence_role"] == rh.EVIDENCE_ROLE

    # 5) 币种必须等于本仓 Adapter 的 CURRENCY_CONTEXT
    assert smoke["estimated_cost_cap"]["currency"] == rh.currency_context()
    assert smoke["estimated_cost_cap"]["amount"] != ""

    # 额外的身份一致性（预登记字段）
    assert smoke["payload_hash"] == replay["payload_hash"]
    assert smoke["contract_version"] == replay["contract_version"] == "0.1.0"
    assert smoke["contract_mode"] == "observe"
    assert replay["contract_mode"] == "strict"
    assert smoke["candidate_commit"] is None
    assert smoke["repository_commit"] is None and replay["repository_commit"] is None
    assert replay["reproduction_restriction"] == "REPRODUCTION_RESTRICTED"
    assert smoke["coverage_policy"] in {"ALL_STAGES", "ONE_OF"}


# --------------------------------------------------------------------------- #
# 冻结表面：run manifest 键集（Spec 11 Stage 0 校准，G-01 / G-02）
# --------------------------------------------------------------------------- #


def test_manifest_key_sets_are_frozen() -> None:
    """Spec 13/14 禁止修改 ``config/``，所以两个 run manifest 的键名必须在 A 冻结前定死。

    Wiki 侧同一断言在 ``tests/contracts/test_status_ledger.py::TestFrozenSurface``；
    Coding 没有 ``status_ledger.py``，故在此独立钉住（两仓键集当前逐字相同）。

    逐对 ``evidence_requirement`` 的值同样钉住，但**性质必须逐条分清**（不得混同）：

    * **§7.3 规范硬要求**（L3 列逐字）→ ``ALL_STAGES``（``case_cost`` 的 ``normal`` 等）。
    * **(A) 规范回归**：母 Spec §7.3:896 逐字写明"``normal`` required；错误 stage 可
      ``NOT_OBSERVED``"。冻结 manifest 曾把 ``case_cost`` 的 ``environment_error`` /
      ``error`` 错标 ``ALL_STAGES``（与 §7.3 矛盾；且 ``benchmark/runner.py::_run_one_case``
      三分支互斥、``case_ids`` 冻结为单一 case，三者不可能在一次 run 内同现），本轮回归为
      ``NOT_OBSERVED_ALLOWED`` —— 这是**修缺陷**，不是偏离。
    * **(B) 用户授权偏离**（依 N-04）：``coding.trace.summary`` 的 ``sdk`` / ``clickhouse``
      被 §7.3:897 硬要求为 ``ONE_OF(sdk, clickhouse)``，但 ``--benchmark`` 路径上无任何
      producer 可达（唯一 producer 在 ``tools/report_trace.py``，只经 ``main.py
      --trace-report``，需 Langfuse 凭据或 127.0.0.1:8123 的 ClickHouse），因此记
      ``NOT_OBSERVED_ALLOWED``。记录见
      ``docs/plans/2026-09-16-foundation-contract-spec11-stage0-calibration.md`` §10 (B2)。

    校准记录：``docs/plans/2026-09-16-foundation-contract-spec11-stage0-calibration.md``
    """
    expected_smoke_keys = {
        "manifest_version",
        "run_id",
        "contract_mode",
        "contract_version",
        "payload_hash",
        "candidate_commit",
        "repository_commit",
        "evidence_root",
        "coverage_policy",
        "required_producer_stages",
        "model",
        "provider",
        "case_ids",
        "expected_calls",
        "max_calls",
        "estimated_cost_cap",
        "network_requirement",
        "side_effect_policy",
        "timeout_seconds",
        "duration_cap_seconds",
    }
    expected_replay_keys = {
        "manifest_version",
        "run_id",
        "contract_mode",
        "contract_version",
        "payload_hash",
        "repository_commit",
        "output_dir",
        "sources",
        "max_records",
        "reproduction_restriction",
    }
    expected_stage_keys = {"producer_id", "mapping_stage", "evidence_requirement"}
    #: 冻结的逐对覆盖要求（键名/嵌套不变；值 = §7.3 硬要求 + (A) 回归 + (B) 授权偏离）。
    expected_stage_requirements = {
        ("coding.agent.llm_usage", "openai_compat"): "ALL_STAGES",
        ("coding.trace.generation_usage", "langfuse_generation"): "ALL_STAGES",
        ("coding.benchmark.case_cost", "normal"): "ALL_STAGES",
        # (A) 规范回归：§7.3:896「错误 stage 可 NOT_OBSERVED」
        ("coding.benchmark.case_cost", "environment_error"): "NOT_OBSERVED_ALLOWED",
        ("coding.benchmark.case_cost", "error"): "NOT_OBSERVED_ALLOWED",
        # (B) N-04 授权偏离：--benchmark 路径上无 trace.summary producer 可达
        ("coding.trace.summary", "sdk"): "NOT_OBSERVED_ALLOWED",
        ("coding.trace.summary", "clickhouse"): "NOT_OBSERVED_ALLOWED",
    }
    #: registry 的 producer 级值是**授权下限**（manifest 逐对可更严，不可更松）。
    expected_registry_requirements = {
        "coding.agent.llm_usage": "ALL_STAGES",
        "coding.trace.generation_usage": "ALL_STAGES",
        "coding.benchmark.case_cost": "NOT_OBSERVED_ALLOWED",  # (A) 规范回归
        "coding.trace.summary": "NOT_OBSERVED_ALLOWED",  # (B) 授权下限
    }
    expected_source_keys = {
        "source_id",
        "source_class",
        "path",
        "producer_id",
        "mapping_stage",
        "runtime_adapter_status",
        "evidence_role",
    }

    config_dir = REPO_ROOT / "config" / "contracts"
    smoke = json.loads((config_dir / "smoke-v0.1.json").read_text(encoding="utf-8"))
    replay = json.loads((config_dir / "replay-v0.1.json").read_text(encoding="utf-8"))
    registry = json.loads((config_dir / "producer-registry.json").read_text(encoding="utf-8"))

    assert set(smoke) == expected_smoke_keys, sorted(set(smoke) ^ expected_smoke_keys)
    assert set(replay) == expected_replay_keys, sorted(set(replay) ^ expected_replay_keys)
    actual_stage_requirements = {}
    for item in smoke["required_producer_stages"]:
        assert set(item) == expected_stage_keys
        actual_stage_requirements[(item["producer_id"], item["mapping_stage"])] = item[
            "evidence_requirement"
        ]
    assert actual_stage_requirements == expected_stage_requirements
    actual_registry_requirements = {
        producer["producer_id"]: producer["evidence_requirement"]
        for producer in registry["producers"]
        if producer["status"] == "IN_SCOPE"
    }
    assert actual_registry_requirements == expected_registry_requirements
    assert replay["sources"], "sources 必须非空（空 sources 是显式 exit 2，不得静默）"
    for source in replay["sources"]:
        assert set(source) == expected_source_keys
