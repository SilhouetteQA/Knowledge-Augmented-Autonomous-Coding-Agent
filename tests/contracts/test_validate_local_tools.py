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
