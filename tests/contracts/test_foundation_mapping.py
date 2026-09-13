"""Coding presence-aware facts / mapping / runtime / sidecar 的契约测试（Spec 06）。

覆盖 Spec 06 要求的 fixture：

```text
provider usage absent / present、显式零、unknown USD cost
partial calls before failure、no-call environment error
cursor isolation、off no-allocation、concurrent client isolation
```

以及验收项：

```text
sidecar 无磁盘持久化、无跨项目依赖、无 public contract exposure
空 price table 导致 Foundation unknown，不导致 Legacy 数值变化
case slice 不串入前一 case components
off 模式与未安装 Adapter 前的 state/return 等价
已调用后失败与根本未调用可产生不同 Foundation Summary 语义
```

本文件不接任何 producer，也不触碰 Agent / Sandbox / Benchmark 业务文件。
"""
from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_core.contracts.enums.evidence import ValidationStatus
from agent_core.contracts.enums.modes import ContractMode, ContractModeConfigError
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.evidence import EvidenceRecord
from agent_core.contracts.protocols.evidence_sink import SinkFailure

from adapters.foundation import facts as facts_mod
from adapters.foundation import mapping as mapping_mod
from adapters.foundation.observation_ledger import ComponentLedger, ComponentRecord
from adapters.foundation.runtime import (
    CodingFoundationRuntime,
    ContractConfigurationError,
    ContractValidationError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_DIR = REPO_ROOT / "adapters" / "foundation"

COMMIT = "b" * 40
PAYLOAD_HASH = "sha256:" + "e" * 64
RUN_ID = "run-spec06"

#: 当前 Coding 的真实状态：空单价表。
EMPTY_TABLE = facts_mod.CodingPriceTable.from_mapping({})
#: 带一条可用单价表的对照（用于验证 price_table 分支）。
FILLED_TABLE = facts_mod.CodingPriceTable.from_mapping({"mimo-v2.5": 0.004})


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def emit(self, record: EvidenceRecord) -> None:
        self.records.append(record)


class ExplodingSink:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, record: EvidenceRecord) -> None:
        self.calls += 1
        raise SinkFailure("evidence.sink_write_failed", "injected sink failure")


class _Usage:
    def __init__(self, **fields: object) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, usage: object | None) -> None:
        if usage is not None:
            self.usage = usage


def make_runtime(
    sink: object | None = None,
    *,
    mode: ContractMode | str = ContractMode.OBSERVE,
    run_id: str | None = RUN_ID,
    table: facts_mod.CodingPriceTable | None = EMPTY_TABLE,
    repository_commit: str | None = COMMIT,
    **kwargs: object,
) -> CodingFoundationRuntime:
    return CodingFoundationRuntime(
        sink=sink,  # type: ignore[arg-type]
        mode=mode,
        run_id=run_id,
        repository_commit=repository_commit,
        price_table=table,
        payload_hash=PAYLOAD_HASH,
        **kwargs,  # type: ignore[arg-type]
    )


def code_only(path: Path) -> str:
    """剥掉注释与字符串字面量后的代码文本（源码扫描用）。"""
    source = path.read_text(encoding="utf-8")
    pieces: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if token.type == tokenize.ENDMARKER:
            break
        pieces.append(token.string)
    return " ".join(pieces)


# --------------------------------------------------------------------------- #
# usage / cost facts
# --------------------------------------------------------------------------- #


def test_usage_absent_is_unknown_not_zero() -> None:
    """provider usage absent → 全 null + unknown。"""
    usage_facts = facts_mod.extract_usage_from_response(None)
    assert usage_facts.call_observed is False
    usage = mapping_mod.map_usage(usage_facts)
    assert usage.source is UsageSource.UNKNOWN
    assert usage.input_tokens is None and usage.output_tokens is None


def test_call_observed_but_usage_missing_is_still_unknown() -> None:
    """调用发生但 provider 未回 usage：unknown（且与"未调用"可区分）。"""
    usage_facts = facts_mod.extract_usage_from_response(_Response(None))
    assert usage_facts.call_observed is True
    assert usage_facts.usage_object_present is False
    assert mapping_mod.map_usage(usage_facts).source is UsageSource.UNKNOWN


def test_usage_present_maps_to_provider_reported() -> None:
    """usage present → 精确保留，source=provider_reported。"""
    usage_facts = facts_mod.extract_usage_from_response(
        _Response(_Usage(prompt_tokens=11, completion_tokens=22, total_tokens=33)),
        model="mimo-v2.5",
    )
    usage = mapping_mod.map_usage(usage_facts)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (11, 22, 33)
    assert usage.source is UsageSource.PROVIDER_REPORTED


def test_explicit_zero_is_distinct_from_unknown() -> None:
    """显式零 → 0；缺失 → null。"""
    explicit = facts_mod.extract_usage_from_counts(0, 0, model="mimo-v2.5")
    usage = mapping_mod.map_usage(explicit)
    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert usage.source is UsageSource.PROVIDER_REPORTED

    missing = mapping_mod.map_usage(facts_mod.extract_usage_from_counts(None, None))
    assert missing.input_tokens is None
    assert missing.source is UsageSource.UNKNOWN


def test_provider_total_is_never_recomputed() -> None:
    """provider total 原样保留，不与 prompt+completion 求和。"""
    # extract_usage_from_counts 不携带 total：必须是 null 而不是 30
    usage = mapping_mod.map_usage(facts_mod.extract_usage_from_counts(10, 20, model="mimo-v2.5"))
    assert usage.input_tokens == 10 and usage.output_tokens == 20
    assert usage.total_tokens is None

    with_total = mapping_mod.map_usage(
        facts_mod.extract_usage_from_response(
            _Response(_Usage(prompt_tokens=10, completion_tokens=20, total_tokens=999))
        )
    )
    assert with_total.total_tokens == 999


def test_empty_price_table_yields_unknown_usd_not_true_zero() -> None:
    """空价格表 → amount null + source unknown + USD 上下文（unknown 不是零）。"""
    cost_facts = facts_mod.extract_cost_facts(
        EMPTY_TABLE, model="mimo-v2.5", legacy_cost_usd=0.0, legacy_cost_is_default=True
    )
    assert cost_facts.price_table_size == 0
    assert cost_facts.price_entry_present is False

    cost = mapping_mod.map_cost(cost_facts)
    assert cost.source is CostSource.UNKNOWN
    assert cost.amount is None
    assert cost.currency == "USD"


def test_usable_price_table_yields_price_table_source() -> None:
    """有可用单价表且已有金额 → source=price_table，并带上表身份。"""
    cost_facts = facts_mod.extract_cost_facts(
        FILLED_TABLE, model="mimo-v2.5", legacy_cost_usd=0.004, legacy_cost_is_default=False
    )
    cost = mapping_mod.map_cost(cost_facts)
    assert cost.source is CostSource.PRICE_TABLE
    assert str(cost.amount) == "0.004"
    assert cost.pricing_version == FILLED_TABLE.table_hash


def test_price_table_identity_is_content_addressed() -> None:
    """价格表身份由内容决定；空表与有内容的表身份不同。"""
    assert EMPTY_TABLE.table_hash != FILLED_TABLE.table_hash
    again = facts_mod.CodingPriceTable.from_mapping({"mimo-v2.5": 0.004})
    assert again.table_hash == FILLED_TABLE.table_hash
    assert facts_mod.CodingPriceTable.from_mapping(
        {"b": 2.0, "a": 1.0}
    ).table_hash == facts_mod.CodingPriceTable.from_mapping({"a": 1.0, "b": 2.0}).table_hash


def test_provider_reported_cost_is_preferred() -> None:
    """provider 明确报告货币成本 → source=provider_reported。"""
    cost_facts = facts_mod.extract_cost_facts(
        EMPTY_TABLE,
        model="mimo-v2.5",
        legacy_cost_usd=0.02,
        provider_reported_cost=True,
        provider_reported_currency="USD",
    )
    cost = mapping_mod.map_cost(cost_facts)
    assert cost.source is CostSource.PROVIDER_REPORTED
    assert str(cost.amount) == "0.02"


# --------------------------------------------------------------------------- #
# sidecar：游标与隔离
# --------------------------------------------------------------------------- #


def test_cursor_slice_does_not_leak_previous_case_components() -> None:
    """case slice 不串入前一 case 的组成项。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)

    first = runtime.begin_case()
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    runtime.observe_usage(3, 4, model="mimo-v2.5")
    assert len(runtime.case_components(first)) == 2

    second = runtime.begin_case()
    runtime.observe_usage(5, 6, model="mimo-v2.5")
    components = runtime.case_components(second)
    assert len(components) == 1
    assert components[0].usage_facts.prompt_tokens == 5
    # 第二个 case 的切片绝不含第一个 case 的组成项
    assert all(rec.usage_facts.prompt_tokens == 5 for rec in components)


def test_range_slice_is_boundary_stable() -> None:
    """range_slice(start, end) 的边界在取切片前确定，后续追加不会混入。"""
    ledger = ComponentLedger()
    usage = facts_mod.extract_usage_from_counts(1, 1)
    cost = facts_mod.extract_cost_facts(EMPTY_TABLE)

    def append(value: int) -> None:
        ledger.append(
            ComponentRecord(
                usage_facts=facts_mod.extract_usage_from_counts(value, value),
                cost_facts=cost,
            )
        )

    start = ledger.cursor()
    append(1)
    append(2)
    end = ledger.cursor()
    frozen = ledger.range_slice(start, end)

    append(3)  # 之后的追加不得混入
    assert [rec.usage_facts.prompt_tokens for rec in frozen] == [1, 2]
    assert len(ledger.range_slice(start, ledger.cursor())) == 3
    assert ledger.range_slice(0, 0) == ()

    with pytest.raises(ValueError):
        ledger.range_slice(2, 1)


def test_cursor_before_any_call_yields_empty_slice() -> None:
    """未发生调用 → 空切片，而不是"零组成的伪证据"。"""
    runtime = make_runtime(RecordingSink())
    mark = runtime.begin_case()
    assert runtime.case_components(mark) == ()
    assert runtime.case_cost_facts(mark) == ()


@pytest.mark.parametrize("bad", [-1, "0", 1.5, True])
def test_invalid_cursor_mark_is_rejected(bad: object) -> None:
    """非法游标必须被拒绝。"""
    ledger = ComponentLedger()
    with pytest.raises(ValueError):
        ledger.slice(bad)  # type: ignore[arg-type]


def test_ledger_append_and_count() -> None:
    """sidecar 的基本追加与计数。"""
    ledger = ComponentLedger()
    assert ledger.cursor() == 0
    usage = facts_mod.extract_usage_from_counts(1, 2)
    cost = facts_mod.extract_cost_facts(EMPTY_TABLE)
    ledger.append(ComponentRecord(usage_facts=usage, cost_facts=cost))
    assert ledger.component_count == 1
    assert len(ledger) == 1
    assert ledger.slice(0)[0].usage_facts.prompt_tokens == 1


def test_ledger_capacity_is_bounded() -> None:
    """sidecar 有容量上限，长时间运行不会无界增长。"""
    ledger = ComponentLedger(max_components=2)
    usage = facts_mod.extract_usage_from_counts(1, 2)
    cost = facts_mod.extract_cost_facts(EMPTY_TABLE)
    for _ in range(3):
        ledger.append(ComponentRecord(usage_facts=usage, cost_facts=cost))
    assert ledger.component_count == 2
    with pytest.raises(ValueError):
        ComponentLedger(max_components=0)


def test_client_isolation_between_two_runtimes() -> None:
    """并发/并存的多个 client 各自持有 sidecar，互不串扰。"""
    left = make_runtime(RecordingSink(), run_id="run-left")
    right = make_runtime(RecordingSink(), run_id="run-right")

    left_mark = left.begin_case()
    right_mark = right.begin_case()
    interleaved = [(left, 1), (right, 10), (left, 2), (right, 20)]
    for runtime, value in interleaved:
        runtime.observe_usage(value, value, model="mimo-v2.5")

    left_values = [rec.usage_facts.prompt_tokens for rec in left.case_components(left_mark)]
    right_values = [rec.usage_facts.prompt_tokens for rec in right.case_components(right_mark)]
    assert left_values == [1, 2]
    assert right_values == [10, 20]


# --------------------------------------------------------------------------- #
# runtime：mode 与 sidecar 生命周期
# --------------------------------------------------------------------------- #


def test_off_mode_allocates_no_ledger_and_emits_nothing() -> None:
    """off 模式不分配 sidecar、不发射证据；与未安装 Adapter 等价。"""
    sink = RecordingSink()
    runtime = make_runtime(sink, mode=ContractMode.OFF)

    mark = runtime.begin_case()
    runtime.observe_chat_completion(_Response(_Usage(prompt_tokens=5)), model="mimo-v2.5")
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    runtime.observe_case_cost(mark)

    assert runtime.ledger_allocated is False
    assert runtime.case_components(mark) == ()
    assert sink.records == []
    assert runtime.sink_failure_count == 0
    assert runtime.run_is_valid is True


def test_observe_allocates_ledger_only_when_used() -> None:
    """observe 模式下 sidecar 惰性分配。"""
    runtime = make_runtime(RecordingSink())
    assert runtime.ledger_allocated is False
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    assert runtime.ledger_allocated is True


def test_invalid_mode_value_fails_configuration() -> None:
    """非法 AGENT_CONTRACT_MODE 明确失败，不静默降级。"""
    with pytest.raises(ContractModeConfigError):
        make_runtime(RecordingSink(), mode="verbose")
    with pytest.raises(ContractModeConfigError):
        make_runtime(RecordingSink(), mode=None, env={"AGENT_CONTRACT_MODE": "on"})


def test_mode_defaults_to_off_when_env_absent() -> None:
    runtime = make_runtime(RecordingSink(), mode=None, env={})
    assert runtime.mode is ContractMode.OFF


def test_strict_requires_explicit_run_id() -> None:
    with pytest.raises(ContractConfigurationError):
        make_runtime(RecordingSink(), mode=ContractMode.STRICT, run_id=None)


def test_observe_generates_process_run_id_when_absent() -> None:
    runtime = make_runtime(RecordingSink(), run_id=None)
    assert re.fullmatch(r"[0-9a-f-]{36}", runtime.run_id)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a:b", "a b", ""])
def test_unsafe_run_id_is_rejected(bad: str) -> None:
    with pytest.raises(ContractConfigurationError):
        make_runtime(RecordingSink(), run_id=bad)


def test_invalid_repository_commit_is_rejected() -> None:
    with pytest.raises(ContractConfigurationError):
        make_runtime(RecordingSink(), repository_commit="nope")
    with pytest.raises(ContractConfigurationError):
        make_runtime(RecordingSink(), repository_commit=None, env={"AGENT_CONTRACT_COMMIT": "x"})


def test_missing_commit_cannot_forge_a_record() -> None:
    """commit 不可用时无法构造合法记录：observe 标记 run 失效，strict 失败。"""
    sink = RecordingSink()
    runtime = make_runtime(sink, repository_commit=None, env={})
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    assert sink.records == []
    assert runtime.run_is_valid is False
    assert runtime.sink_failure_count == 1

    strict = make_runtime(
        RecordingSink(), mode=ContractMode.STRICT, run_id=RUN_ID, repository_commit=None, env={}
    )
    with pytest.raises(ContractValidationError):
        strict.observe_usage(1, 2, model="mimo-v2.5")


# --------------------------------------------------------------------------- #
# runtime：证据产出与 summary 语义
# --------------------------------------------------------------------------- #


def test_chat_completion_emits_pass_evidence_and_appends_component() -> None:
    """一次 provider 调用产出 PASS 证据并记入 sidecar。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)
    mark = runtime.begin_case()
    runtime.observe_chat_completion(
        _Response(_Usage(prompt_tokens=7, completion_tokens=8, total_tokens=15)),
        model="mimo-v2.5",
        legacy_cost_usd=0.0,
    )

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.validation_status is ValidationStatus.PASS
    assert record.producer_id == "coding.agent.llm_usage"
    assert record.mapping_stage == "openai_compat"
    assert record.repository == "coding"
    assert record.repository_commit == COMMIT
    assert record.contract_payload_hash == PAYLOAD_HASH
    assert all(key.startswith("coding.") for key in record.sanitized_input_facts)
    assert len(runtime.case_components(mark)) == 1


def test_tracing_stage_maps_to_generation_producer() -> None:
    """record_usage 对应的 stage 映射到 coding.trace.generation_usage。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)
    runtime.observe_usage(3, 4, model="mimo-v2.5", legacy_cost_usd=0.0)
    assert sink.records[0].producer_id == "coding.trace.generation_usage"
    assert sink.records[0].mapping_stage == "langfuse_generation"


@pytest.mark.parametrize(
    "stage,producer",
    [
        ("sdk", "coding.trace.summary"),
        ("clickhouse", "coding.trace.summary"),
    ],
)
def test_trace_summary_stages(stage: str, producer: str) -> None:
    """trace summary 的两个 stage 都映射到 coding.trace.summary，且带 CostSummary。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)
    component = facts_mod.extract_cost_facts(EMPTY_TABLE, model="mimo-v2.5", legacy_cost_usd=0.0)
    runtime.observe_trace_summary([component], stage=stage)

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.producer_id == producer
    assert record.mapping_stage == stage
    assert record.foundation_output is not None
    assert record.foundation_output.cost_summary is not None


def test_unknown_stage_is_rejected() -> None:
    runtime = make_runtime(RecordingSink())
    with pytest.raises(ContractConfigurationError):
        runtime.observe_usage(1, 2, model="m", stage="not-registered")


@pytest.mark.parametrize(
    "stage", ["normal", "environment_error", "error"]
)
def test_case_cost_stages_map_to_case_cost_producer(stage: str) -> None:
    """benchmark 的三个 case stage 都映射到 coding.benchmark.case_cost。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)
    mark = runtime.begin_case()
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    runtime.observe_case_cost(mark, stage=stage)

    summary_records = [r for r in sink.records if r.mapping_stage == stage]
    assert len(summary_records) == 1
    assert summary_records[0].producer_id == "coding.benchmark.case_cost"


def test_partial_calls_before_failure_keep_components() -> None:
    """已有调用后失败：保留已观察到的组件，不因最终错误清零。"""
    runtime = make_runtime(RecordingSink())
    mark = runtime.begin_case()
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    runtime.observe_usage(3, 4, model="mimo-v2.5")
    # 模拟 case 以错误结束：不清理 sidecar，只按游标取切片
    components = runtime.case_cost_facts(mark)
    assert len(components) == 2

    summary = mapping_mod.map_summary(components)
    assert summary.component_count == 2
    assert summary.known_component_count == 0
    assert summary.known_amount is None
    assert summary.complete is False


def test_no_call_environment_error_yields_empty_summary() -> None:
    """无调用的环境错误：允许空 component set，语义与"调用后失败"不同。"""
    runtime = make_runtime(RecordingSink())
    mark = runtime.begin_case()
    components = runtime.case_cost_facts(mark)
    assert components == ()

    summary = mapping_mod.map_summary(components)
    assert summary.component_count == 0
    assert summary.known_amount is None
    assert summary.complete is True


def test_called_then_failed_differs_from_never_called() -> None:
    """已调用后失败与根本未调用必须产生不同的 Foundation Summary 语义。"""
    runtime = make_runtime(RecordingSink())

    never = runtime.begin_case()
    never_summary = mapping_mod.map_summary(runtime.case_cost_facts(never))

    called = runtime.begin_case()
    runtime.observe_usage(9, 9, model="mimo-v2.5")
    called_summary = mapping_mod.map_summary(runtime.case_cost_facts(called))

    assert never_summary.component_count == 0
    assert called_summary.component_count == 1
    assert never_summary.complete != called_summary.complete


def test_mapping_failure_in_observe_does_not_change_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """observe 下 mapping 失败产 FAIL 证据，模拟业务返回不变。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)

    def boom(*args: object, **kwargs: object) -> None:
        raise mapping_mod.MappingFailure(
            mapping_mod.make_envelope("foundation.invalid_cost", "injected mapping failure")
        )

    monkeypatch.setattr(mapping_mod, "map_observation", boom)

    def simulate_business_call() -> dict[str, object]:
        result = {"tokens_total": {"prompt": 5, "completion": 6}, "cost_usd": 0.0}
        runtime.observe_usage(5, 6, model="mimo-v2.5")
        return result

    assert simulate_business_call() == {"tokens_total": {"prompt": 5, "completion": 6}, "cost_usd": 0.0}
    assert len(sink.records) == 1
    assert sink.records[0].validation_status is ValidationStatus.FAIL


def test_observe_sink_failure_does_not_change_business_result() -> None:
    """observe 下 sink 失败不改变业务返回；该 run 明确失效。"""
    sink = ExplodingSink()
    runtime = make_runtime(sink)

    def business() -> str:
        runtime.observe_usage(1, 2, model="mimo-v2.5")
        return "unchanged"

    assert business() == "unchanged"
    assert sink.calls == 1
    assert runtime.sink_failure_count == 1
    assert runtime.run_is_valid is False


def test_strict_sink_failure_raises_without_recursion() -> None:
    sink = ExplodingSink()
    runtime = make_runtime(sink, mode=ContractMode.STRICT, run_id=RUN_ID)
    with pytest.raises(ContractValidationError):
        runtime.observe_usage(1, 2, model="mimo-v2.5")
    assert sink.calls == 1


def test_evidence_records_satisfy_shared_contract() -> None:
    """产出的记录能被共享契约重新校验。"""
    sink = RecordingSink()
    runtime = make_runtime(sink)
    runtime.observe_usage(1, 2, model="mimo-v2.5")
    record = sink.records[0]
    assert EvidenceRecord(**record.model_dump(mode="json")).event_id == record.event_id


# --------------------------------------------------------------------------- #
# 静态验收：sidecar 的边界
# --------------------------------------------------------------------------- #

#: sidecar 内不得出现的持久化 / 跨项目符号。
FORBIDDEN_SIDECAR_PATTERNS = (
    (r"\bopen\s*\(", "文件句柄"),
    (r"\bjson\.dump", "JSON 落盘"),
    (r"\bsqlite3?\b", "SQLite"),
    (r"\bshelve\b", "shelve"),
    (r"\bpickle\b", "pickle"),
    (r"\bagent_core\b", "共享契约依赖"),
    (r"\barknights_wiki\b", "跨项目依赖"),
    (r"\bpathlib\b", "文件路径操作"),
)


@pytest.mark.parametrize("pattern,label", FORBIDDEN_SIDECAR_PATTERNS)
def test_sidecar_has_no_persistence_and_no_cross_project_dependency(
    pattern: str, label: str
) -> None:
    """sidecar 无磁盘持久化、无共享契约暴露、无跨项目依赖。"""
    source = code_only(ADAPTER_DIR / "observation_ledger.py")
    assert not re.search(pattern, source), f"observation_ledger.py 出现 {label}"


def test_sidecar_is_not_a_shared_contract() -> None:
    """sidecar 类型不在 agent_core 里，也不是 Protocol 实现。"""
    assert not hasattr(ComponentLedger, "emit")
    ledger = ComponentLedger()
    assert not hasattr(ledger, "write") and not hasattr(ledger, "flush")


def test_facts_extractor_never_erases_presence() -> None:
    """facts.py 不出现 getattr(..., 0) / dict.get(..., 0) / or 0。"""
    source = code_only(ADAPTER_DIR / "facts.py")
    for pattern, label in (
        (r"getattr\([^)]*,\s*0\s*\)", "getattr(..., 0)"),
        (r"\.get\([^)]*,\s*0\s*\)", "dict.get(..., 0)"),
        (r"\bor\s+0\b", "or 0"),
    ):
        assert not re.search(pattern, source), f"facts.py 出现 {label}"


def test_facts_extractor_does_not_recompute_totals() -> None:
    """facts.py 与 mapping.py 不做 prompt+completion 的 total 重建。"""
    for name in ("facts.py", "mapping.py"):
        source = code_only(ADAPTER_DIR / name)
        assert not re.search(r"prompt_tokens\s*\+\s*\w*completion_tokens", source)
        assert not re.search(r"input_tokens\s*\+\s*\w*output_tokens", source)


def test_adapters_do_not_import_business_modules() -> None:
    """Adapter 层不 import 任何业务模块（价格表由调用方注入）。"""
    forbidden = ("benchmark", "agent.llm", "tools.tracing", "tools.report_trace", "arknights_wiki")
    for name in ("facts.py", "mapping.py", "observation_ledger.py"):
        source = code_only(ADAPTER_DIR / name)
        for needle in forbidden:
            assert needle not in source, f"{name} 依赖业务模块 {needle}"
