"""项目 FileEvidenceSink 文件行为测试（Spec 09，Spec 04 遗留的正式落地）。

覆盖 EVD-SINK-004（原子写 / 并发安全）及 sink 的非法 ID / 超限 / 注入 I/O 失败
等文件行为。超限与 ID 形状由模型层冗余校验，本测试直击 sink 层的行为与计数。
"""
from __future__ import annotations

import concurrent.futures
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.evidence import ValidationStatus
from agent_core.contracts.enums.modes import ContractMode
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.base import canonical_json_dumps
from agent_core.contracts.models.cost import Cost
from agent_core.contracts.models.evidence import EvidenceRecord, FoundationObservation
from agent_core.contracts.models.usage import Usage
from agent_core.contracts.protocols.evidence_sink import SinkFailure

from adapters.foundation.evidence_sink import (
    EVIDENCE_DIR_ENV,
    FileEvidenceSink,
    default_evidence_root,
    has_residual_temp_files,
    iter_published_events,
)

COMMIT = "a" * 40
PAYLOAD_HASH = "sha256:" + "d" * 64
RUN_ID = "run-sink-01"


def make_record(event_id: str, run_id: str = RUN_ID) -> EvidenceRecord:
    return EvidenceRecord(
        event_id=event_id,
        run_id=run_id,
        repository="coding",
        repository_commit=COMMIT,
        producer_id="coding.agent.llm_usage",
        mapping_stage="openai_compat",
        contract_mode=ContractMode.OBSERVE,
        contract_version="0.1.0",
        contract_payload_hash=PAYLOAD_HASH,
        timestamp="2026-09-14T01:00:00Z",
        validation_status=ValidationStatus.PASS,
        sanitized_input_facts={"coding.legacy.fields_present": ["prompt_tokens"]},
        foundation_output=FoundationObservation(
            usage=Usage(input_tokens=10, source=UsageSource.PROVIDER_REPORTED),
            cost=Cost(
                amount=Decimal("0.12"),
                currency="USD",
                source=CostSource.PRICE_TABLE,
                pricing_version="p1",
            ),
        ),
    )


def eid(index: int) -> str:
    return f"9f1c0c1e-4a3b-4c2d-8e5f-{index:012x}"


def _events_dir(sink: FileEvidenceSink, run_id: str) -> Path:
    """Coding 侧 sink 使用 ``events_dir`` 方法名。"""
    return sink.events_dir(run_id)


@contract_rule("EVD-SINK-004")
def test_legal_write_and_atomic_replace(tmp_path: Path) -> None:
    """合法写入落盘为 formal .json，无 .tmp，内容为 canonical JSON。"""
    sink = FileEvidenceSink(tmp_path)
    record = make_record(eid(1))
    sink.emit(record)

    events_dir = _events_dir(sink, RUN_ID)
    formal = events_dir / f"{record.event_id}.json"
    temp = events_dir / f"{record.event_id}.json.tmp"
    assert formal.is_file()
    assert not temp.exists()
    raw = formal.read_text(encoding="utf-8")
    assert canonical_json_dumps(json.loads(raw)) == raw
    assert not formal.read_bytes().endswith(b"\n")
    assert formal.relative_to(tmp_path).as_posix() == f"{RUN_ID}/events/{record.event_id}.json"
    assert sink.sink_failure_count == 0


@contract_rule("EVD-SINK-004")
def test_concurrent_writes_do_not_overwrite(tmp_path: Path) -> None:
    """并发 8 条不同 event_id 全部落盘、互不覆盖、无 .tmp 残留。"""
    sink = FileEvidenceSink(tmp_path)
    ids = [eid(100 + i) for i in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda e: sink.emit(make_record(e)), ids))

    events_dir = _events_dir(sink, RUN_ID)
    published = [p.name[: -len(".json")] for p in iter_published_events(events_dir)]
    assert len(published) == 8
    assert len(set(published)) == len(published)
    assert not has_residual_temp_files(events_dir)
    assert sink.sink_failure_count == 0


@contract_rule("EVD-SINK-004")
def test_residual_tmp_is_not_read_as_evidence(tmp_path: Path) -> None:
    """残留 .tmp 被识别为未完成过程态，不被 iter_published_events 读作证据。"""
    sink = FileEvidenceSink(tmp_path)
    sink.emit(make_record(eid(1)))
    events_dir = _events_dir(sink, RUN_ID)
    stray = events_dir / "deadbeef-dead-dead-dead-deaddeadbeef.json.tmp"
    stray.write_bytes(b'{"partial":')

    listed = [p.name for p in iter_published_events(events_dir)]
    assert all(n.endswith(".json") for n in listed)
    assert stray.name not in listed
    assert has_residual_temp_files(events_dir)


def test_invalid_ids_rejected(tmp_path: Path) -> None:
    """5 类非法 ID 被 sink 冗余校验拒绝，且不越出 staging 根。"""
    sink = FileEvidenceSink(tmp_path)
    before = sink.sink_failure_count
    for bad_run, bad_event in (
        ("../escape", eid(3)),
        ("a/b", eid(3)),
        ("..", eid(3)),
        (RUN_ID, "../../etc/passwd"),
        (RUN_ID, "not-a-uuid"),
    ):
        rogue = SimpleNamespace(run_id=bad_run, event_id=bad_event)
        with pytest.raises(SinkFailure) as exc:
            sink.emit(rogue)  # type: ignore[arg-type]
        assert exc.value.code == "evidence.invalid_run_id"
    assert sink.sink_failure_count == before + 5
    assert not (tmp_path.parent / "escape").exists()


@contract_rule("EVD-SINK-004")
def test_injected_io_failure_raises_and_counts(tmp_path: Path) -> None:
    """注入 os.replace 失败抛 SinkFailure 并计数，不静默丢弃。"""
    sink = FileEvidenceSink(tmp_path)
    before = sink.sink_failure_count
    injected = eid(500)
    with patch("os.replace", side_effect=OSError("injected replace failure")):
        with pytest.raises(SinkFailure) as exc:
            sink.emit(make_record(injected))
        assert exc.value.code == "evidence.sink_write_failed"
    assert sink.sink_failure_count == before + 1
    assert not (_events_dir(sink, RUN_ID) / f"{injected}.json").exists()


def test_oversize_record_rejected_at_model_layer() -> None:
    """超限（>64 KiB）在模型层被拒绝，sink 无需重复实现。"""
    base = make_record(eid(9)).model_dump(mode="json")
    with pytest.raises(ValidationError):
        EvidenceRecord(**{**base, "sanitized_input_facts": {"adapter.blob": "x" * 70000}})


def test_env_var_override_and_default_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AGENT_CONTRACT_EVIDENCE_DIR 覆盖 staging 根；缺省根固定。"""
    monkeypatch.setenv(EVIDENCE_DIR_ENV, str(tmp_path))
    assert FileEvidenceSink().root == tmp_path
    monkeypatch.delenv(EVIDENCE_DIR_ENV)
    assert default_evidence_root().as_posix() == "output/contract-validation/staging"
