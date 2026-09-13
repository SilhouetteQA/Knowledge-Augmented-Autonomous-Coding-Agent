"""Coding Foundation runtime：mode 策略、证据组装、sink 注入与 sidecar 生命周期。

三个模式共用**同一** extractor 与 mapping，只有失败策略不同（FND-MODE-002）：

```text
off     解析后立即返回；不提取、不映射、不分配 sidecar、不建 sink
observe 提取 → 映射 → 记入 client-local sidecar → 交注入的 sink；失败只产 FAIL 证据
strict  完全相同的路径；任何失败先 emit FAIL 证据，再抛 ContractValidationError
```

sidecar 生命周期（Spec 06 step 7 / 验收项）：

```text
只在 observe / strict 激活：由本 runtime 在 mode 判定通过后惰性创建
off 不创建、不分配、不积累 —— 与未安装 Adapter 之前的 state/return 等价
每个 runtime 实例一份（client-scoped），不是全局单例
```

身份绑定（EVD-RUN-002）：``contract_payload_hash`` 从已安装的 ``agent_core`` 现场复算；
``repository_commit`` 由构造参数注入或读 ``AGENT_CONTRACT_COMMIT``。该字段是
EvidenceRecord 的必填项，因此不可用时**无法构造任何记录**：observe 把该 run 标记为失效
（计数 + 结构化日志，业务不受影响），strict 直接失败 —— 不伪造提交号。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from agent_core.contracts.enums.errors import FOUNDATION_INVALID_USAGE
from agent_core.contracts.enums.evidence import (
    EVIDENCE_ARTIFACT_UNAVAILABLE,
    EVIDENCE_SINK_WRITE_FAILED,
    EvidenceRepository,
    ValidationStatus,
)
from agent_core.contracts.enums.modes import (
    ContractMode,
    parse_contract_mode,
    resolve_contract_mode,
)
from agent_core.contracts.models.base import JsonValue
from agent_core.contracts.models.evidence import (
    EvidenceRecord,
    FoundationObservation,
    REPOSITORY_COMMIT_PATTERN,
    is_safe_run_id,
)
from agent_core.contracts.models.error import ErrorEnvelope
from agent_core.contracts.protocols.evidence_sink import EvidenceSink, SinkFailure
from agent_core.contracts.tooling.canonical_json import canonical_file_bundle_digest
from agent_core.contracts.version import CONTRACT_VERSION

from adapters.foundation import facts as facts_mod
from adapters.foundation import mapping as mapping_mod
from adapters.foundation.facts import (
    CodingLegacyCostFacts,
    CodingLegacyUsageFacts,
    CodingPriceTable,
)
from adapters.foundation.mapping import MappingFailure
from adapters.foundation.observation_ledger import ComponentLedger, ComponentRecord

#: 注入 repository commit 的环境变量。
CONTRACT_COMMIT_ENV: Final[str] = "AGENT_CONTRACT_COMMIT"

#: mapping stage → producer_id（与 Spec 01 的 Coding producer registry 一致）。
STAGE_PRODUCER: Final[Mapping[str, str]] = {
    "openai_compat": "coding.agent.llm_usage",
    "langfuse_generation": "coding.trace.generation_usage",
    "normal": "coding.benchmark.case_cost",
    "environment_error": "coding.benchmark.case_cost",
    "error": "coding.benchmark.case_cost",
    "sdk": "coding.trace.summary",
    "clickhouse": "coding.trace.summary",
}

logger = logging.getLogger(__name__)


class ContractConfigurationError(ValueError):
    """运行前的配置问题：缺失或非法的 run_id / repository commit / stage。"""


class ContractValidationError(RuntimeError):
    """strict 模式下的契约验证失败。调用方必须令验证命令失败。"""


def producer_for_stage(stage: str) -> str:
    """把 mapping stage 映射为已登记的 producer_id。"""
    try:
        return STAGE_PRODUCER[stage]
    except KeyError as exc:
        raise ContractConfigurationError(
            f"未登记的 mapping stage {stage!r}；已知: {sorted(STAGE_PRODUCER)}"
        ) from exc


def resolve_payload_hash() -> str:
    """从已安装的 `agent_core` 现场复算 Contract Payload Hash。"""
    import agent_core

    repo_root = Path(agent_core.__file__).resolve().parent.parent
    payload_hash, _ = canonical_file_bundle_digest(repo_root)
    return payload_hash


def resolve_repository_commit(
    explicit: str | None = None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """解析 repository commit：显式参数优先，否则读环境变量。"""
    source = os.environ if env is None else env
    raw = explicit if explicit is not None else source.get(CONTRACT_COMMIT_ENV)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not REPOSITORY_COMMIT_PATTERN.match(text):
        origin = "构造参数" if explicit is not None else CONTRACT_COMMIT_ENV
        raise ContractConfigurationError(f"{origin} 不是 40 位小写 hex 提交号：{text!r}")
    return text


class CodingFoundationRuntime:
    """Coding 侧的统一旁路入口与 client-local sidecar 持有者。"""

    def __init__(
        self,
        *,
        sink: EvidenceSink | None = None,
        mode: ContractMode | str | None = None,
        run_id: str | None = None,
        repository_commit: str | None = None,
        repository: EvidenceRepository | str = EvidenceRepository.CODING,
        contract_version: str = CONTRACT_VERSION,
        price_table: Mapping[str, object] | CodingPriceTable | None = None,
        payload_hash: str | None = None,
        env: Mapping[str, str] | None = None,
        ledger_factory: Callable[[], ComponentLedger] | None = None,
    ) -> None:
        self._mode = self._resolve_mode(mode, env)
        self._sink = sink
        self._repository = EvidenceRepository(repository)
        self._contract_version = contract_version
        self._price_table = (
            price_table
            if isinstance(price_table, CodingPriceTable)
            else CodingPriceTable.from_mapping(price_table)
        )
        self._payload_hash = payload_hash
        self._repository_commit = resolve_repository_commit(repository_commit, env=env)
        self._sink_failure_count = 0
        self._run_valid = True
        self._generated_run_id = str(uuid.uuid4())
        self._run_id = self._resolve_run_id(run_id)
        self._ledger_factory = ledger_factory or ComponentLedger
        self._ledger: ComponentLedger | None = None

    # -- 只读属性 --------------------------------------------------------- #

    @property
    def mode(self) -> ContractMode:
        return self._mode

    @property
    def accepts_observation(self) -> bool:
        """off 模式下为 ``False``；producer 用它做最外层短路（零 I/O、零价格表加载）。"""
        return self._mode is not ContractMode.OFF

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def repository_commit(self) -> str | None:
        return self._repository_commit

    @property
    def sink_failure_count(self) -> int:
        return self._sink_failure_count

    @property
    def run_is_valid(self) -> bool:
        return self._run_valid

    @property
    def price_table(self) -> CodingPriceTable:
        return self._price_table

    @property
    def payload_hash(self) -> str:
        if self._payload_hash is None:
            self._payload_hash = resolve_payload_hash()
        return self._payload_hash

    @property
    def ledger_allocated(self) -> bool:
        """sidecar 是否已被分配；off 模式下必须始终为 ``False``。"""
        return self._ledger is not None

    # -- case 游标 -------------------------------------------------------- #

    def begin_case(self) -> int:
        """标记一个 case 的开始，返回游标。

        off 模式下返回 ``0`` 且**不分配** ledger —— 调用方拿到空切片的等价语义，
        因此"未安装 Adapter"与"off"在 state/return 上一致。
        """
        if self._mode is ContractMode.OFF:
            return 0
        if not self._guard():
            return 0
        return self._ledger_for_observation().cursor()

    def case_components(self, mark: int) -> tuple[ComponentRecord, ...]:
        """返回本 case 的组成项；off 或未分配时为空。"""
        if self._ledger is None:
            return ()
        return self._ledger.slice(mark)

    def case_cost_facts(self, mark: int) -> tuple[CodingLegacyCostFacts, ...]:
        """返回本 case 的 cost facts（供 summary 映射）。"""
        return tuple(record.cost_facts for record in self.case_components(mark))

    # -- 窄 helper API（供 Spec 08 的 seam 调用）--------------------------- #

    def observe_chat_completion(
        self,
        response: object | None,
        *,
        model: str | None = None,
        stage: str = "openai_compat",
        legacy_cost_usd: object = None,
        cost_is_default: bool = True,
        call_id: str | None = None,
    ) -> None:
        """观察一次 provider 调用（`agent/llm.py::OpenAICompatClient.chat`）。

        :param legacy_cost_usd: 调用点传给 `record_usage` 的数值（当前恒为硬编码 0.0）。
        :param cost_is_default: 该数值是否为调用点的默认占位。
        """
        if not self._guard():
            return

        usage_facts = facts_mod.extract_usage_from_response(response, model=model)
        cost_facts = facts_mod.extract_cost_facts(
            self._price_table,
            model=model,
            legacy_cost_usd=legacy_cost_usd,
            call_observed=usage_facts.call_observed,
            legacy_cost_is_default=cost_is_default,
        )
        self._record(
            usage_facts=usage_facts,
            cost_facts=cost_facts,
            model=model,
            call_id=call_id,
            stage=stage,
        )

    def observe_usage(
        self,
        prompt_tokens: object,
        completion_tokens: object,
        *,
        model: str | None = None,
        stage: str = "langfuse_generation",
        legacy_cost_usd: object = None,
        cost_is_default: bool = True,
        call_observed: bool = True,
        call_id: str | None = None,
    ) -> None:
        """观察一次已经把计数取出的调用（`tools/tracing.py::record_usage`）。

        传入 ``None`` 表示计数不可用（未知），**不补零**。
        """
        if not self._guard():
            return

        usage_facts = facts_mod.extract_usage_from_counts(
            prompt_tokens, completion_tokens, model=model, call_observed=call_observed
        )
        cost_facts = facts_mod.extract_cost_facts(
            self._price_table,
            model=model,
            legacy_cost_usd=legacy_cost_usd,
            call_observed=call_observed,
            legacy_cost_is_default=cost_is_default,
        )
        self._record(
            usage_facts=usage_facts,
            cost_facts=cost_facts,
            model=model,
            call_id=call_id,
            stage=stage,
            write_ledger=False,
        )

    def observe_case_cost(self, mark: int, *, stage: str = "normal") -> None:
        """由 case 的 sidecar 切片产出 CostSummary 证据（`benchmark/runner.py`）。

        ``stage`` 取 ``normal`` / ``environment_error`` / ``error``。
        """
        if not self._guard():
            return
        components = self.case_cost_facts(mark)
        payload: dict[str, JsonValue] = {
            "coding.case.component_count": len(components),
            "coding.pricing.table_size": self._price_table.size,
            "coding.currency.context": facts_mod.CURRENCY_CONTEXT,
            "coding.model.name": components[0].model if components else "unknown",
        }
        self._run(
            producer_id=producer_for_stage(stage),
            stage=stage,
            produce=lambda: (mapping_mod.map_observation(components=components), payload),
        )

    def observe_trace_summary(
        self,
        components: Sequence[CodingLegacyCostFacts],
        *,
        usage_facts: CodingLegacyUsageFacts | None = None,
        stage: str = "sdk",
    ) -> None:
        """由显式组成项产出 Trace summary 证据（`tools/report_trace.py`）。

        ``stage`` 取 ``sdk`` / ``clickhouse``。
        """
        if not self._guard():
            return
        payload: dict[str, JsonValue] = {
            "coding.case.component_count": len(components),
            "coding.pricing.table_size": self._price_table.size,
            "coding.currency.context": facts_mod.CURRENCY_CONTEXT,
        }
        self._run(
            producer_id=producer_for_stage(stage),
            stage=stage,
            produce=lambda: (
                mapping_mod.map_observation(usage_facts=usage_facts, components=components),
                payload,
            ),
        )

    # -- 内部 ------------------------------------------------------------- #

    @staticmethod
    def _resolve_mode(
        mode: ContractMode | str | None, env: Mapping[str, str] | None
    ) -> ContractMode:
        if mode is None:
            return resolve_contract_mode(env)
        if isinstance(mode, ContractMode):
            return mode
        return parse_contract_mode(str(mode))

    def _resolve_run_id(self, run_id: str | None) -> str:
        if run_id is None:
            if self._mode is ContractMode.STRICT:
                raise ContractConfigurationError(
                    "strict 模式必须显式提供 run_id（EVD-RUN-001）；"
                    "本地 observe 才会生成进程级 UUID"
                )
            return self._generated_run_id
        if not is_safe_run_id(run_id):
            raise ContractConfigurationError(
                f"run_id 不安全或不合法：{run_id!r}（只允许 [A-Za-z0-9_-]+）"
            )
        return run_id

    def _guard(self) -> bool:
        if self._mode is ContractMode.OFF:
            return False
        if self._repository_commit is None:
            self._invalidate_run(
                f"repository commit 不可用（需要构造参数或 {CONTRACT_COMMIT_ENV}）；"
                "EvidenceRecord 的该字段必填，因此本次观察无法产出证据"
            )
            return False
        return True

    def _ledger_for_observation(self) -> ComponentLedger:
        """惰性分配 sidecar；只有 mode 判定通过后才会被调用。"""
        if self._ledger is None:
            self._ledger = self._ledger_factory()
        return self._ledger

    def _invalidate_run(self, detail: str) -> None:
        self._sink_failure_count += 1
        self._run_valid = False
        logger.error(
            "foundation run invalidated: %s",
            detail,
            extra={
                "evidence_code": EVIDENCE_ARTIFACT_UNAVAILABLE,
                "evidence_run_id": self._run_id,
                "evidence_detail": detail,
            },
        )
        if self._mode is ContractMode.STRICT:
            raise ContractValidationError(detail)

    def _record(
        self,
        *,
        usage_facts: CodingLegacyUsageFacts,
        cost_facts: CodingLegacyCostFacts,
        model: str | None,
        call_id: str | None,
        stage: str,
        write_ledger: bool = True,
    ) -> None:
        """发射（Usage, Cost）证据；``write_ledger`` 决定是否记入 sidecar。

        ``openai_compat``（chat）写 ledger；``langfuse_generation``（record_usage）
        只旁路观察、不写 ledger —— 否则同一调用被两个 stage 各写一次，
        benchmark 的 case summary 会 double-count（母 Spec §9.3）。
        """
        if write_ledger:
            ledger = self._ledger_for_observation()
            ledger.append(
                ComponentRecord(
                    usage_facts=usage_facts,
                    cost_facts=cost_facts,
                    model=model,
                    call_id=call_id,
                )
            )
        component_count = self._ledger.component_count if self._ledger is not None else 0
        payload: dict[str, JsonValue] = {
            "coding.legacy.call_observed": usage_facts.call_observed,
            "coding.legacy.usage_object_present": usage_facts.usage_object_present,
            "coding.legacy.field_presence": dict(usage_facts.field_presence),
            "coding.pricing.entry_present": cost_facts.price_entry_present,
            "coding.pricing.table_size": cost_facts.price_table_size,
            "coding.currency.context": cost_facts.currency_context or facts_mod.CURRENCY_CONTEXT,
            "coding.legacy.cost_is_default": cost_facts.legacy_cost_is_default,
            "coding.model.name": model or "unknown",
            "coding.case.component_count": component_count,
        }
        self._run(
            producer_id=producer_for_stage(stage),
            stage=stage,
            produce=lambda: (
                mapping_mod.map_observation(usage_facts=usage_facts, cost_facts=cost_facts),
                payload,
            ),
        )

    def _run(
        self,
        *,
        producer_id: str,
        stage: str,
        produce: Callable[[], tuple[FoundationObservation, Mapping[str, JsonValue]]],
    ) -> None:
        try:
            observation, fact_payload = produce()
        except MappingFailure as failure:
            self._emit_failure(
                producer_id=producer_id, stage=stage, envelope=failure.envelope, fact_payload={}
            )
            return
        except Exception as exc:  # noqa: BLE001 - 任何提取失败都要变成 FAIL 证据
            self._emit_failure(
                producer_id=producer_id,
                stage=stage,
                envelope=mapping_mod.make_envelope(
                    FOUNDATION_INVALID_USAGE, f"facts 提取失败：{type(exc).__name__}"
                ),
                fact_payload={},
            )
            return

        self._deliver(
            self._build_record(
                producer_id=producer_id,
                stage=stage,
                status=ValidationStatus.PASS,
                observation=observation,
                envelope=None,
                fact_payload=dict(fact_payload),
            )
        )

    def _emit_failure(
        self,
        *,
        producer_id: str,
        stage: str,
        envelope: ErrorEnvelope,
        fact_payload: Mapping[str, JsonValue],
    ) -> None:
        self._deliver(
            self._build_record(
                producer_id=producer_id,
                stage=stage,
                status=ValidationStatus.FAIL,
                observation=None,
                envelope=envelope,
                fact_payload=dict(fact_payload),
            )
        )
        if self._mode is ContractMode.STRICT:
            raise ContractValidationError(f"{envelope.code}: {envelope.message}")

    def _deliver(self, record: EvidenceRecord) -> None:
        """交给 sink；失败只计数，绝不递归、绝不再调 sink。"""
        if self._sink is None:
            self._invalidate_run("未注入 EvidenceSink，证据无接收方")
            return
        try:
            self._sink.emit(record)
        except SinkFailure as exc:
            self._sink_failure_count += 1
            self._run_valid = False
            logger.error(
                "evidence sink rejected a record",
                extra={
                    "evidence_code": exc.code,
                    "evidence_event_id": record.event_id,
                    "evidence_run_id": record.run_id,
                    "evidence_sink_failure_count": self._sink_failure_count,
                },
            )
            if self._mode is ContractMode.STRICT:
                raise ContractValidationError(f"{exc.code}: {exc.message}") from exc
        except Exception as exc:  # noqa: BLE001 - 实现缺陷也不得改变业务返回
            self._sink_failure_count += 1
            self._run_valid = False
            logger.error(
                "evidence sink raised an unexpected error: %s",
                type(exc).__name__,
                extra={
                    "evidence_code": EVIDENCE_SINK_WRITE_FAILED,
                    "evidence_event_id": record.event_id,
                    "evidence_run_id": record.run_id,
                    "evidence_sink_failure_count": self._sink_failure_count,
                },
            )
            if self._mode is ContractMode.STRICT:
                raise ContractValidationError(
                    f"{EVIDENCE_SINK_WRITE_FAILED}: {type(exc).__name__}"
                ) from exc

    def _build_record(
        self,
        *,
        producer_id: str,
        stage: str,
        status: ValidationStatus,
        observation: FoundationObservation | None,
        envelope: ErrorEnvelope | None,
        fact_payload: Mapping[str, JsonValue],
    ) -> EvidenceRecord:
        return EvidenceRecord(
            event_id=str(uuid.uuid4()),
            run_id=self._run_id,
            repository=self._repository,
            repository_commit=self._repository_commit,
            producer_id=producer_id,
            mapping_stage=stage,
            contract_mode=self._mode,
            contract_version=self._contract_version,
            contract_payload_hash=self.payload_hash,
            timestamp=_utc_now(),
            validation_status=status,
            sanitized_input_facts=dict(fact_payload),
            foundation_output=observation,
            error_envelope=envelope,
        )


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------- #
# 进程级访问器与模块级窄 helper（Spec 08 deviation，与 Wiki Spec 07 对称）
# --------------------------------------------------------------------------- #

_commit_cache: str | None = None
_commit_cache_resolved: bool = False


def detect_repository_commit() -> str | None:
    """惰性只读 ``git rev-parse HEAD``；最多探测一次，失败即放弃（返回 ``None``）。

    与 Wiki Spec 07 的 git 回退对称：本地 observe 开箱即用，无需手工 export
    ``AGENT_CONTRACT_COMMIT``。只接受 40 位小写 hex，其余一律视为失败。
    """
    global _commit_cache, _commit_cache_resolved
    if _commit_cache_resolved:
        return _commit_cache
    _commit_cache_resolved = True
    _commit_cache = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    if REPOSITORY_COMMIT_PATTERN.match(text):
        _commit_cache = text
    return _commit_cache


_active_runtime: CodingFoundationRuntime | None = None


def _build_runtime_from_env() -> CodingFoundationRuntime:
    """按环境构造进程级 runtime。

    off 下不构造 sink（零 I/O）；observe/strict 下用项目本地 FileEvidenceSink。
    commit 解析沿用 Wiki Spec 07 已批准的「env 优先 + git 回退」。
    """
    mode = resolve_contract_mode(None)
    sink: EvidenceSink | None = None
    if mode is not ContractMode.OFF:
        from adapters.foundation.evidence_sink import FileEvidenceSink

        sink = FileEvidenceSink()
    commit = resolve_repository_commit(None)
    if commit is None:
        commit = detect_repository_commit()
    return CodingFoundationRuntime(sink=sink, mode=mode, repository_commit=commit)


def get_foundation_runtime() -> CodingFoundationRuntime:
    """返回进程级 runtime（首次调用时按环境构造并缓存）。

    这是四个 producer seam 的唯一入口。``off`` 下只构造一个无 sink 的轻量对象，
    之后每次调用都只是一次属性访问。
    """
    global _active_runtime
    if _active_runtime is None:
        _active_runtime = _build_runtime_from_env()
    return _active_runtime


def set_foundation_runtime(runtime: CodingFoundationRuntime) -> None:
    """受控注入 runtime（测试 / Spec 13 smoke 使用）。"""
    global _active_runtime
    _active_runtime = runtime


def reset_foundation_runtime() -> None:
    """清空进程级缓存（测试隔离用）。"""
    global _active_runtime
    _active_runtime = None


def observe_openai_compat(
    response: object | None,
    *,
    model: str | None = None,
    legacy_cost_usd: object = None,
    cost_is_default: bool = True,
) -> None:
    """`agent/llm.py::chat` 的 openai_compat 旁路观察（emit + 写 ledger）。"""
    runtime = get_foundation_runtime()
    if not runtime.accepts_observation:
        return
    runtime.observe_chat_completion(
        response,
        model=model,
        stage="openai_compat",
        legacy_cost_usd=legacy_cost_usd,
        cost_is_default=cost_is_default,
    )


def observe_langfuse_generation(
    model: str | None,
    tokens_in: object,
    tokens_out: object,
    cost_usd: object,
) -> None:
    """`tools/tracing.py::record_usage` 的 langfuse_generation 旁路观察（emit only）。"""
    runtime = get_foundation_runtime()
    if not runtime.accepts_observation:
        return
    runtime.observe_usage(
        tokens_in,
        tokens_out,
        model=model,
        stage="langfuse_generation",
        legacy_cost_usd=cost_usd,
        cost_is_default=(cost_usd == 0.0),
        call_observed=True,
    )


def observe_case_begin() -> int:
    """`benchmark/runner.py` case 开始：返回游标（off 返回 0）。"""
    return get_foundation_runtime().begin_case()


def observe_case_cost_entry(mark: int, *, stage: str) -> None:
    """`benchmark/runner.py` 三个分支形成 CaseResult 后的 summary 观察。"""
    runtime = get_foundation_runtime()
    if not runtime.accepts_observation:
        return
    runtime.observe_case_cost(mark, stage=stage)


def observe_trace_summary_entry(
    raw_rows: Sequence[Mapping[str, object]],
    *,
    stage: str,
) -> None:
    """`tools/report_trace.py` 两个 summarize 形成旧 TraceSummary 后的 summary 观察。

    ``raw_rows`` 每项含可选键：``model`` / ``input`` / ``output`` / ``cost`` /
    ``cost_present``。runtime 内部用价格表构造 components 并 emit —— 调用方只收集
    原始 presence，不碰 facts 构造（价格表只此一处读取）。
    """
    runtime = get_foundation_runtime()
    if not runtime.accepts_observation:
        return
    components: list[CodingLegacyCostFacts] = []
    for row in raw_rows:
        cost_present = bool(row.get("cost_present"))
        model = row.get("model")
        components.append(
            facts_mod.extract_cost_facts(
                runtime.price_table,
                model=model if isinstance(model, str) else None,
                legacy_cost_usd=row.get("cost") if cost_present else None,
                call_observed=True,
                provider_reported_cost=cost_present,
            )
        )
    runtime.observe_trace_summary(components, stage=stage)


__all__ = [
    "CONTRACT_COMMIT_ENV",
    "STAGE_PRODUCER",
    "ContractConfigurationError",
    "ContractValidationError",
    "CodingFoundationRuntime",
    "detect_repository_commit",
    "get_foundation_runtime",
    "observe_case_begin",
    "observe_case_cost_entry",
    "observe_langfuse_generation",
    "observe_openai_compat",
    "observe_trace_summary_entry",
    "producer_for_stage",
    "reset_foundation_runtime",
    "resolve_payload_hash",
    "resolve_repository_commit",
    "set_foundation_runtime",
]
