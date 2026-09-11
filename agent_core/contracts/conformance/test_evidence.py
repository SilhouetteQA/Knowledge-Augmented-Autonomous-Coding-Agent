"""Evidence 契约的一致性测试（Spec 04）。

覆盖 Rule：``EVD-RUN-001``、``EVD-RUN-002``、``EVD-SINK-001``、``EVD-DATA-001``、
``EVD-PUB-001``、``EVD-PUB-005``，以及共享载荷的仓库隔离（``FND-PKG-002``）。

刻意**不覆盖**的部分（按文件计划落在别处）：

```text
EVD-SINK-002 / 003  项目本地 sink 与 smoke closure（Spec 05/06/09）
EVD-SINK-004        文件发布的原子性与并发安全（Spec 09 项目 sink 测试）
EVD-PUB-002 / 003   发布前脱敏与 secret 扫描（Spec 10/14）
EVD-PUB-004 / 006 / 007  语料、manifest 绑定与保留决策（Spec 12/14）
```

共享载荷内没有文件 I/O，因此本文件不测试任何目录、命名或 rotation 行为。
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.evidence import (
    EVIDENCE_INFRASTRUCTURE_CODES,
    EvidenceArtifactKind,
    EvidenceRepository,
    EvidenceStatus,
    ValidationStatus,
)
from agent_core.contracts.enums.modes import ContractMode
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.cost import Cost, CostSummary
from agent_core.contracts.models.error import ErrorEnvelope
from agent_core.contracts.models.evidence import (
    EVIDENCE_FACT_MAX_KEYS,
    EVIDENCE_RECORD_MAX_BYTES,
    EvidenceFactsValidationError,
    EvidenceRecord,
    FoundationObservation,
    is_canonical_event_id,
    is_path_safe_identifier,
    is_safe_run_id,
    normalize_event_id,
    validate_evidence_facts,
)
from agent_core.contracts.models.usage import Usage
from agent_core.contracts.protocols.evidence_sink import (
    EVIDENCE_SINK_METHODS,
    EvidenceSink,
    SinkFailure,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PAYLOAD_ROOT = REPO_ROOT / "agent_core"

USAGE = Usage(input_tokens=10, output_tokens=5, source=UsageSource.PROVIDER_REPORTED)
COST = Cost(
    amount=Decimal("0.12"),
    currency="USD",
    source=CostSource.PRICE_TABLE,
    pricing_version="pricing-2026-09",
)
SUMMARY = CostSummary(
    known_amount=Decimal("0.12"),
    currency="USD",
    complete=True,
    component_count=1,
    known_component_count=1,
    unknown_component_count=0,
)
ENVELOPE = ErrorEnvelope(
    category="validation", code="foundation.invalid_cost", message="bad cost"
)

UUID_EVENT = "9f1c0c1e-4a3b-4c2d-8e5f-0a1b2c3d4e5f"
ULID_EVENT = "01JZ5C3W7A9KQ2M4N6P8R0T2V4"
COMMIT = "b" * 40
PAYLOAD_HASH = "sha256:" + "c" * 64

#: 共享载荷不得 import 的项目顶层模块。
PROJECT_MODULE_ROOTS = frozenset(
    {"arknights_wiki", "adapters", "benchmark", "scripts", "tools"}
)


def make_record(**overrides: object) -> EvidenceRecord:
    """构造一条合法 PASS 记录，按需覆盖字段。"""
    payload: dict[str, object] = {
        "event_id": UUID_EVENT,
        "run_id": "run-2026-09-11_a",
        "repository": EvidenceRepository.WIKI,
        "repository_commit": COMMIT,
        "producer_id": "wiki.agent.llm_usage",
        "mapping_stage": "chat_completion",
        "contract_mode": ContractMode.OBSERVE,
        "contract_version": "0.1.0",
        "contract_payload_hash": PAYLOAD_HASH,
        "timestamp": "2026-09-11T00:40:00Z",
        "validation_status": ValidationStatus.PASS,
        "sanitized_input_facts": {"wiki.legacy.fields_present": ["input_tokens"]},
        "foundation_output": FoundationObservation(usage=USAGE, cost=COST),
        "error_envelope": None,
    }
    payload.update(overrides)
    return EvidenceRecord(**payload)


# --------------------------------------------------------------------------- #
# FoundationObservation
# --------------------------------------------------------------------------- #


@contract_rule("EVD-DATA-001")
def test_observation_requires_at_least_one_fact() -> None:
    """空观测不是合法的 zero：三项全空必须被拒绝。"""
    with pytest.raises(ValidationError):
        FoundationObservation()


@contract_rule("EVD-DATA-001")
def test_observation_accepts_each_single_fact() -> None:
    """任一项单独存在都合法；三项并存也合法。"""
    assert FoundationObservation(usage=USAGE).usage is USAGE
    assert FoundationObservation(cost=COST).cost is COST
    assert FoundationObservation(cost_summary=SUMMARY).cost_summary is SUMMARY
    combined = FoundationObservation(usage=USAGE, cost=COST, cost_summary=SUMMARY)
    assert combined.usage is USAGE and combined.cost_summary is SUMMARY


@contract_rule("EVD-DATA-001")
def test_observation_is_a_dto_not_a_result_or_exception() -> None:
    """FoundationObservation 不是业务 Result，也不是异常类型。"""
    assert not issubclass(FoundationObservation, BaseException)
    with pytest.raises(TypeError):
        raise FoundationObservation(usage=USAGE)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# EvidenceRecord：状态不变量
# --------------------------------------------------------------------------- #


@contract_rule("EVD-DATA-001")
def test_pass_requires_output_and_no_error() -> None:
    """PASS → foundation_output 存在且 error_envelope 为 null。"""
    record = make_record()
    assert record.validation_status is ValidationStatus.PASS
    assert record.foundation_output is not None
    assert record.error_envelope is None

    with pytest.raises(ValidationError):
        make_record(foundation_output=None)
    with pytest.raises(ValidationError):
        make_record(error_envelope=ENVELOPE)


@contract_rule("EVD-DATA-001")
def test_fail_requires_error_and_allows_output() -> None:
    """FAIL → error_envelope 存在；foundation_output 可空也可是存在的。"""
    without_output = make_record(
        validation_status=ValidationStatus.FAIL,
        foundation_output=None,
        error_envelope=ENVELOPE,
    )
    assert without_output.error_envelope is ENVELOPE

    with_output = make_record(
        validation_status=ValidationStatus.FAIL,
        error_envelope=ENVELOPE,
        foundation_output=FoundationObservation(usage=USAGE),
    )
    assert with_output.foundation_output is not None

    with pytest.raises(ValidationError):
        make_record(validation_status=ValidationStatus.FAIL, foundation_output=None)


# --------------------------------------------------------------------------- #
# EvidenceRecord：契约绑定与标识
# --------------------------------------------------------------------------- #


@contract_rule("EVD-RUN-002")
def test_record_binds_contract_version_hash_and_commit() -> None:
    """记录必须绑定 contract version、Payload Hash 与 40 位提交号。"""
    record = make_record()
    assert record.contract_version == "0.1.0"
    assert record.contract_payload_hash == PAYLOAD_HASH
    assert record.repository_commit == COMMIT

    for bad in ("0.1", "v0.1.0", "1", ""):
        with pytest.raises(ValidationError):
            make_record(contract_version=bad)
    for bad in ("c" * 64, "sha256:" + "C" * 64, "sha256:" + "c" * 63, ""):
        with pytest.raises(ValidationError):
            make_record(contract_payload_hash=bad)
    for bad in ("b" * 39, "B" * 40, "z" * 40, ""):
        with pytest.raises(ValidationError):
            make_record(repository_commit=bad)


@contract_rule("EVD-RUN-001")
def test_run_id_shape_and_path_safety() -> None:
    """run_id 只允许 [A-Za-z0-9_-]+，且必须路径安全。"""
    assert make_record(run_id="run-2026-09-11_a").run_id == "run-2026-09-11_a"
    for bad in ("", "a b", "a/b", "a\\b", "a:b", "../x", "..", ".", "a..b", "run\nid"):
        with pytest.raises(ValidationError):
            make_record(run_id=bad)
        assert not is_safe_run_id(bad)
    assert is_safe_run_id("ckpt_01")


@contract_rule("EVD-DATA-001")
def test_event_id_accepts_uuid_or_ulid_and_normalizes_case() -> None:
    """event_id 允许 canonical UUID 或 ULID；两者都规范化为小写。"""
    assert is_canonical_event_id(UUID_EVENT)
    assert is_canonical_event_id(UUID_EVENT.upper())
    assert is_canonical_event_id(ULID_EVENT)
    assert make_record(event_id=UUID_EVENT.upper()).event_id == UUID_EVENT
    assert make_record(event_id=ULID_EVENT).event_id == ULID_EVENT.lower()
    assert normalize_event_id(ULID_EVENT) == ULID_EVENT.lower()

    # ULID 字母表排除 I / L / O / U
    assert not is_canonical_event_id("01JZ5C3W7A9KQ2M4N6P8R0T2UI")
    for bad in ("not-an-id", "", "../../etc/passwd", UUID_EVENT[:-1]):
        with pytest.raises(ValidationError):
            make_record(event_id=bad)
        assert not is_canonical_event_id(bad)


@contract_rule("EVD-RUN-001")
def test_path_unsafe_identifiers_are_never_path_safe() -> None:
    """任一标识都不得携带 / \\ .. : 参与路径构造。"""
    for bad in ("a/b", "a\\b", "..", "./x", "a:b", "", ".", "a..b", "a\x00b"):
        assert not is_path_safe_identifier(bad)
    for good in ("run-1", "01JZ5C3W7A9KQ2M4N6P8R0T2V4", "abc_123"):
        assert is_path_safe_identifier(good)

    # 路径安全 ≠ 字符集合法：空格对路径无害（Windows 文件名允许），
    # 但仍会被 run_id 的形状规则拒绝，两层判定各司其职。
    assert is_path_safe_identifier("a b")
    assert not is_safe_run_id("a b")


@contract_rule("EVD-DATA-001")
def test_only_observe_and_strict_produce_evidence() -> None:
    """off 模式不接入，也就不产出证据；其余模式值必须明确失败。"""
    assert make_record(contract_mode=ContractMode.OBSERVE).contract_mode is ContractMode.OBSERVE
    assert make_record(contract_mode=ContractMode.STRICT).contract_mode is ContractMode.STRICT
    with pytest.raises(ValidationError):
        make_record(contract_mode=ContractMode.OFF)
    with pytest.raises(ValidationError):
        make_record(contract_mode="verbose")


@contract_rule("EVD-DATA-001")
def test_repository_enum_is_closed() -> None:
    """repository 只有 wiki / coding 两个值。"""
    assert make_record(repository="coding").repository is EvidenceRepository.CODING
    with pytest.raises(ValidationError):
        make_record(repository="other")


# --------------------------------------------------------------------------- #
# EvidenceRecord：facts 与容量
# --------------------------------------------------------------------------- #


@contract_rule("EVD-DATA-001")
def test_facts_namespace_allowlist_rejects_unregistered_and_reserved() -> None:
    """fact key 必须点分合法且首段属于项目命名空间。"""
    validate_evidence_facts({"wiki.a": 1, "coding.b.c": 2, "provider.d": 3, "adapter.e": 4})

    for bad in ("foundation.internal", "shared.x", "unknown.y", "wiki", "Wiki.legacy", ""):
        with pytest.raises(EvidenceFactsValidationError):
            validate_evidence_facts({bad: 1})


@contract_rule("EVD-DATA-001")
def test_facts_reject_sensitive_keys_but_not_metric_names() -> None:
    """敏感 key 硬拒绝；指标名不得被误伤。"""
    for bad in (
        "wiki.raw_prompt",
        "wiki.response_body",
        "wiki.reasoning",
        "wiki.traceback",
        "wiki.openai_api_key",
        "adapter.foo_password",
        "adapter.system_prompt_text",
        "provider.client_secret",
    ):
        with pytest.raises(EvidenceFactsValidationError):
            validate_evidence_facts({bad: 1})

    for good in (
        "wiki.prompt_tokens",
        "wiki.response_chars",
        "wiki.cache_read_tokens",
        "adapter.token_count",
    ):
        validate_evidence_facts({good: 1})


@contract_rule("EVD-DATA-001")
def test_facts_are_json_only() -> None:
    """facts 值域是递归 JSON；Decimal / datetime / Path / bytes 一律拒绝。"""
    import datetime as dt

    for bad in (Decimal("1.5"), dt.datetime.now(), Path("x"), b"bytes", object()):
        with pytest.raises(EvidenceFactsValidationError):
            validate_evidence_facts({"wiki.value": bad})

    for good in (1, 1.5, True, None, "text", [1, "a"], {"k": [{"n": None}]}):
        validate_evidence_facts({"wiki.value": good})

    with pytest.raises(ValidationError):
        make_record(sanitized_input_facts={"wiki.amount": Decimal("1.5")})


@contract_rule("EVD-DATA-001")
def test_facts_key_count_is_bounded() -> None:
    """键数上限防止用海量微型键绕过 64 KiB 边界。"""
    validate_evidence_facts(
        {f"adapter.k{index}": index for index in range(EVIDENCE_FACT_MAX_KEYS)}
    )
    with pytest.raises(EvidenceFactsValidationError):
        validate_evidence_facts(
            {f"adapter.k{index}": index for index in range(EVIDENCE_FACT_MAX_KEYS + 1)}
        )


@contract_rule("EVD-DATA-001")
def test_record_size_is_capped_at_64_kib() -> None:
    """单条记录 canonical JSON UTF-8 不得超过 64 KiB。"""
    assert EVIDENCE_RECORD_MAX_BYTES == 64 * 1024

    near_limit = make_record(sanitized_input_facts={"adapter.blob": "x" * 60000})
    assert len(near_limit.sanitized_input_facts["adapter.blob"]) == 60000

    with pytest.raises(ValidationError):
        make_record(sanitized_input_facts={"adapter.blob": "x" * 70000})
    with pytest.raises(ValidationError):
        make_record(
            sanitized_input_facts={"adapter.a": "x" * 60000, "adapter.b": "y" * 6000}
        )


@contract_rule("EVD-DATA-001")
def test_record_fields_are_closed_and_round_trip() -> None:
    """字段集合封闭（禁止额外字段，也不接受 extensions 冒充），且可无损往返。"""
    record = make_record()
    assert EvidenceRecord(**record.model_dump(mode="json")).event_id == record.event_id

    with pytest.raises(ValidationError):
        make_record(unexpected_field=1)
    with pytest.raises(ValidationError):
        make_record(extensions={"wiki.x": 1})


@contract_rule("EVD-DATA-001")
def test_timestamp_must_be_rfc3339_with_timezone() -> None:
    """时间戳必须带时区偏移。"""
    make_record(timestamp="2026-09-11T08:40:00+08:00")
    make_record(timestamp="2026-09-11T00:40:00.123Z")
    for bad in ("2026-09-11T00:40:00", "2026-09-11", "", "now"):
        with pytest.raises(ValidationError):
            make_record(timestamp=bad)


# --------------------------------------------------------------------------- #
# EvidenceSink Protocol
# --------------------------------------------------------------------------- #


@contract_rule("EVD-SINK-001")
def test_sink_protocol_is_narrow() -> None:
    """Protocol 只要求 ``emit``，不定义目录、命名、rotation 或 cleanup。"""
    assert EVIDENCE_SINK_METHODS == ("emit",)
    assert hasattr(EvidenceSink, "emit")

    # Protocol 不携带任何实现；共享载荷也不得出现文件 I/O 依赖
    source = (PAYLOAD_ROOT / "contracts" / "protocols" / "evidence_sink.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("shutil", "os.replace", "open(", "zipfile", "tempfile"):
        assert forbidden not in source, f"共享 Protocol 不得包含 I/O 实现：{forbidden}"


@contract_rule("EVD-SINK-001")
def test_sink_failure_is_not_a_semantic_error() -> None:
    """失败上报是基础设施异常，与 ErrorEnvelope / foundation.* 互不继承。"""
    assert not issubclass(SinkFailure, ErrorEnvelope)
    assert not issubclass(ErrorEnvelope, SinkFailure)

    failure = SinkFailure("evidence.sink_write_failed", "disk full", event_id=UUID_EVENT)
    assert failure.code in EVIDENCE_INFRASTRUCTURE_CODES
    assert failure.event_id == UUID_EVENT
    assert failure.code.startswith("evidence.")

    with pytest.raises(ValueError):
        SinkFailure("foundation.invalid_cost", "wrong namespace")


@contract_rule("EVD-SINK-001")
def test_sink_failure_codes_are_a_closed_set() -> None:
    """sink 失败只能使用三个已登记的基础设施码。"""
    assert EVIDENCE_INFRASTRUCTURE_CODES == {
        "evidence.sink_write_failed",
        "evidence.invalid_run_id",
        "evidence.artifact_unavailable",
    }
    for code in EVIDENCE_INFRASTRUCTURE_CODES:
        assert not code.startswith("foundation.")


# --------------------------------------------------------------------------- #
# 发布 allowlist 基础与共享载荷隔离
# --------------------------------------------------------------------------- #


@contract_rule("EVD-PUB-001")
def test_publishable_artifact_allowlist_is_explicit_and_closed() -> None:
    """可发布 artifact 是显式闭集；cycle-report 只在 Finalization C 写入。"""
    kinds = {kind.value for kind in EvidenceArtifactKind}
    assert kinds == {
        "run-manifest.json",
        "contract-manifest.json",
        "evidence-manifest.json",
        "validation-report.md",
        "rule-traceability.json",
        "sanitized-replay-corpus.jsonl",
    }
    assert "cycle-report.json" not in kinds
    assert "cycle-report.md" not in kinds


@contract_rule("EVD-PUB-005")
def test_reproduction_restricted_is_not_fail() -> None:
    """受限复现是有证据支持的状态，不得混淆为 FAIL。"""
    assert EvidenceStatus.REPRODUCTION_RESTRICTED in set(EvidenceStatus)
    assert EvidenceStatus.REPRODUCTION_RESTRICTED is not EvidenceStatus.FAIL
    assert len(set(EvidenceStatus)) == 10


@contract_rule("FND-PKG-002")
def test_shared_payload_does_not_import_any_project() -> None:
    """共享载荷不得 import 任一项目模块（项目代码也不得位于 agent_core 之下）。"""
    offenders: list[str] = []
    for path in sorted(PAYLOAD_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if root in PROJECT_MODULE_ROOTS:
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}")
    assert not offenders, f"共享载荷出现项目依赖：{offenders}"
