"""Payload、Schema 与版本身份的一致性证明。

对应 Master Appendix A.1 的 FND-PKG-001/002/004 与 FND-VER-001 至 FND-VER-006。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.tooling import generate_schemas as gs
from agent_core.contracts.tooling.canonical_json import (
    PAYLOAD_CONTRACTS_PREFIX,
    PAYLOAD_PACKAGE,
    PAYLOAD_TOP_LEVEL_INIT,
    canonical_file_bundle_digest,
    canonical_json_dumps,
    iter_payload_files,
    normalize_relative_path,
    schema_set_hash,
    sha256_hex,
)
from agent_core.contracts.tooling.verify_payload import GOVERNANCE_KEYS, SELF_REFERENCE_KEYS, verify_tree
from agent_core.contracts.version import (
    CANONICALIZATION_VERSION,
    CONTRACT_FAMILY,
    CONTRACT_VERSION,
    PAYLOAD_TOOLING_VERSION,
    PYDANTIC_PIN,
    SCHEMA_TOOLING_VERSION,
)

REPO_ROOT = gs.find_repo_root()
PROJECT_PACKAGES = ("arknights_wiki", "benchmark", "adapters")


def _payload_relative_paths() -> list[str]:
    return sorted(normalize_relative_path(path, REPO_ROOT) for path in iter_payload_files(REPO_ROOT))


# --------------------------------------------------------------------------- #
# FND-PKG-001 / 002 / 004
# --------------------------------------------------------------------------- #


@contract_rule("FND-PKG-001")
def test_payload_file_allowlist() -> None:
    """Phase 1 的 agent_core 只含根 __init__.py 与 contracts/**。"""
    for rel in _payload_relative_paths():
        assert rel == PAYLOAD_TOP_LEVEL_INIT or rel.startswith(PAYLOAD_CONTRACTS_PREFIX), rel


@contract_rule("FND-PKG-001")
def test_agent_core_top_level_layout() -> None:
    package_dir = REPO_ROOT / PAYLOAD_PACKAGE
    entries = {child.name for child in package_dir.iterdir() if child.name != "__pycache__"}
    assert entries == {"__init__.py", "contracts"}, entries
    assert (package_dir / "contracts").is_dir()


@contract_rule("FND-PKG-002")
def test_no_project_code_inside_payload() -> None:
    """payload 内不得出现项目包导入，也不得出现项目 Adapter 目录。"""
    package_dir = REPO_ROOT / PAYLOAD_PACKAGE
    assert not (package_dir / "adapters").exists()
    assert not (package_dir / "arknights_wiki").exists()

    offenders: list[str] = []
    for path in iter_payload_files(REPO_ROOT):
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in PROJECT_PACKAGES:
                    offenders.append(f"{normalize_relative_path(path, REPO_ROOT)}: {name}")
    assert not offenders, offenders


@contract_rule("FND-PKG-004")
def test_root_init_is_namespace_only() -> None:
    """agent_core/__init__.py 不得 re-export 任何契约或运行时符号。"""
    source = (REPO_ROOT / PAYLOAD_TOP_LEVEL_INIT).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 唯一允许的顶层语句是 docstring；import / 赋值 / __all__ / def / class 都不允许
    for node in tree.body:
        is_docstring = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        assert is_docstring, f"根 __init__ 出现白名单外顶层语句：{type(node).__name__}"

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not imported, imported


# --------------------------------------------------------------------------- #
# FND-VER-001 / 006
# --------------------------------------------------------------------------- #


@contract_rule("FND-VER-001")
def test_single_lockstep_version() -> None:
    assert CONTRACT_VERSION == "0.1.0"
    families = [CONTRACT_FAMILY]
    assert families == ["foundation"]
    for value in (CANONICALIZATION_VERSION, SCHEMA_TOOLING_VERSION, PAYLOAD_TOOLING_VERSION, PYDANTIC_PIN):
        assert isinstance(value, str) and value
    parts = CONTRACT_VERSION.split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts)


@contract_rule("FND-VER-001")
def test_descriptor_version_matches_version_module() -> None:
    descriptor = gs.build_descriptor(REPO_ROOT)
    assert descriptor["contract_version"] == CONTRACT_VERSION
    assert descriptor["canonicalization_version"] == CANONICALIZATION_VERSION
    assert descriptor["families"] == [CONTRACT_FAMILY]


@contract_rule("FND-VER-006")
def test_governance_state_does_not_enter_descriptor() -> None:
    descriptor = gs.build_descriptor(REPO_ROOT)
    assert not (set(descriptor) & GOVERNANCE_KEYS), sorted(set(descriptor) & GOVERNANCE_KEYS)
    assert not (set(descriptor) & SELF_REFERENCE_KEYS)


@contract_rule("FND-VER-006")
def test_payload_identity_is_reproducible() -> None:
    """同一内容重复计算必须得到同一 Payload Hash（治理状态不参与）。"""
    first, _ = canonical_file_bundle_digest(REPO_ROOT)
    second, _ = canonical_file_bundle_digest(REPO_ROOT)
    assert first == second


# --------------------------------------------------------------------------- #
# FND-VER-002 / 003
# --------------------------------------------------------------------------- #


@contract_rule("FND-VER-002")
def test_payload_does_not_contain_its_own_hash() -> None:
    descriptor_path = REPO_ROOT / gs.DESCRIPTOR_PATH
    text = descriptor_path.read_text(encoding="utf-8")
    for key in SELF_REFERENCE_KEYS:
        assert key not in text, f"descriptor 不得包含 {key}"

    payload_hash, _ = canonical_file_bundle_digest(REPO_ROOT)
    assert payload_hash not in text, "payload 文件不得记录自身的 Payload Hash"


@contract_rule("FND-VER-003")
def test_descriptor_is_fully_derivable_before_hashing() -> None:
    """descriptor 必须在不接触 Payload Hash 的前提下完全派生。"""
    import inspect

    signature = inspect.signature(gs.build_descriptor)
    assert list(signature.parameters) == ["repo_root"], signature

    rendered = gs.build_descriptor(REPO_ROOT)
    committed = gs._read_committed_json(REPO_ROOT / gs.DESCRIPTOR_PATH)
    assert canonical_json_dumps(committed) == canonical_json_dumps(rendered)


@contract_rule("FND-VER-003")
def test_schema_set_hash_is_derived_from_schema_hashes() -> None:
    descriptor = gs.build_descriptor(REPO_ROOT)
    assert descriptor["schema_set_hash"] == schema_set_hash(descriptor["schema_hashes"])
    assert descriptor["schema_hashes"], "schema_hashes 不得为空"


@contract_rule("FND-VER-003")
def test_schema_regeneration_is_deterministic() -> None:
    first = gs.render_schemas()
    second = gs.render_schemas()
    for schema_id in first:
        assert canonical_json_dumps(first[schema_id]) == canonical_json_dumps(second[schema_id])


# --------------------------------------------------------------------------- #
# FND-VER-004 / 005
# --------------------------------------------------------------------------- #


@contract_rule("FND-VER-004")
def test_contract_manifest_resides_outside_payload() -> None:
    names = {Path(rel).name for rel in _payload_relative_paths()}
    assert "contract-manifest.json" not in names

    manifest_path = REPO_ROOT / "docs" / "contracts" / "releases" / CONTRACT_VERSION / "contract-manifest.json"
    payload_root = REPO_ROOT / PAYLOAD_PACKAGE
    assert payload_root not in manifest_path.parents


@contract_rule("FND-VER-005")
def test_payload_hash_available_for_evidence_binding() -> None:
    """Evidence Manifest 需要绑定 Candidate commit 与 Payload Hash。

    本步骤只证明 Payload 侧身份可用且稳定；commit 绑定与 Manifest 校验
    属 Spec 14（Evidence Publication）。
    """
    report = verify_tree(REPO_ROOT)
    assert report.ok, report.errors
    assert report.payload_hash.startswith("sha256:")
    assert report.descriptor_hash.startswith("sha256:")
    assert report.schema_set_hash.startswith("sha256:")
    assert report.file_count == len(_payload_relative_paths())


# --------------------------------------------------------------------------- #
# 完整自洽性与分歧检测
# --------------------------------------------------------------------------- #


@contract_rule("FND-VER-003")
def test_canonical_repo_passes_payload_verification() -> None:
    report = verify_tree(REPO_ROOT)
    assert report.ok, report.errors
    assert not report.diverged


@contract_rule("FND-VER-001")
def test_same_version_different_payload_is_detected_as_diverged() -> None:
    """同版本 + 不同 Payload Hash 必须被检测为分歧，而不是静默通过。"""
    report = verify_tree(REPO_ROOT, expected_payload_hash="sha256:" + "0" * 64)
    assert not report.ok
    assert report.diverged


@contract_rule("FND-VER-003")
def test_schema_drift_is_detected(tmp_path: Path) -> None:
    """Schema 快照与模型重放不一致时必须失败。"""
    copied = tmp_path / "payload"
    shutil.copytree(REPO_ROOT / PAYLOAD_PACKAGE, copied / PAYLOAD_PACKAGE)

    assert gs.check(copied) == []

    target = copied / gs.SCHEMAS_DIR / "usage.schema.json"
    target.write_text('{"title":"tampered"}\n', encoding="utf-8")

    drifts = gs.check(copied)
    assert drifts, "被篡改的 Schema 快照必须被检测为漂移"
    assert any("usage.schema.json" in drift.path for drift in drifts)

    report = verify_tree(copied)
    assert not report.ok
    assert any("Schema" in error for error in report.errors)


@contract_rule("FND-VER-003")
def test_missing_descriptor_is_detected(tmp_path: Path) -> None:
    copied = tmp_path / "payload"
    shutil.copytree(REPO_ROOT / PAYLOAD_PACKAGE, copied / PAYLOAD_PACKAGE)
    (copied / gs.DESCRIPTOR_PATH).unlink()

    report = verify_tree(copied)
    assert not report.ok
    assert any("descriptor" in error for error in report.errors)


@contract_rule("FND-PKG-001")
def test_unexpected_file_is_detected(tmp_path: Path) -> None:
    """白名单外文件必须让校验失败。"""
    copied = tmp_path / "payload"
    shutil.copytree(REPO_ROOT / PAYLOAD_PACKAGE, copied / PAYLOAD_PACKAGE)
    stray = copied / PAYLOAD_PACKAGE / "adapters" / "foundation"
    stray.mkdir(parents=True)
    (stray / "runtime.py").write_text("x = 1\n", encoding="utf-8")

    report = verify_tree(copied)
    assert not report.ok
    assert any("FND-PKG-001" in error or "FND-PKG-002" in error for error in report.errors)


@contract_rule("FND-VER-002")
def test_json_entries_are_canonicalized_in_bundle_digest(tmp_path: Path) -> None:
    """JSON 文件在 bundle 前重做 canonical JSON，因此排版变化不改变 Payload Hash。"""
    from agent_core.contracts.tooling.canonical_json import file_canonical_content

    pretty = tmp_path / "a.json"
    compact = tmp_path / "b.json"
    pretty.write_text('{\n  "b": 1,\n  "a": 2\n}\n', encoding="utf-8")
    compact.write_text('{"a":2,"b":1}', encoding="utf-8")

    assert file_canonical_content(pretty) == file_canonical_content(compact)
    assert sha256_hex(file_canonical_content(pretty).encode("utf-8")) == sha256_hex(
        file_canonical_content(compact).encode("utf-8")
    )
