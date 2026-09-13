"""Coding facts → Foundation Usage / Cost / CostSummary 映射（项目本地实现）。

与 Wiki 侧语义一致但代码独立（Spec 06 step 4）。映射表见母 Spec §6.1–§6.3：

```text
usage object absent            → token 全 null，source=unknown
字段明确为 0 / 字段缺失        → 该字段 0 / null
provider total present/absent  → 精确保留 / null（禁止重算）

price entry absent / 空价格表  → amount null、source=unknown、保留 USD 上下文
有效价格                       → Decimal amount、source=price_table、pricing_version 必填
provider 明确报告货币成本      → source=provider_reported
只有 legacy 0.0（含默认占位）   → 不足以证明真实零 → unknown
```

**Legacy 数值一律不变**：本模块只产生 Foundation 侧的 DTO 与证据，
`tokens_total`、`CaseResult.cost_usd`、Benchmark 判定都不受本模块影响。
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from agent_core.contracts.enums.errors import (
    ERROR_CODE_CATEGORY,
    FOUNDATION_CURRENCY_MISMATCH,
    FOUNDATION_INVALID_COST,
    FOUNDATION_INVALID_COST_SUMMARY,
    FOUNDATION_INVALID_USAGE,
)
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.cost import Cost, CostSummary, CurrencyMismatchError
from agent_core.contracts.models.error import ErrorEnvelope
from agent_core.contracts.models.evidence import FoundationObservation
from agent_core.contracts.models.usage import Usage

from adapters.foundation.facts import (
    CURRENCY_CONTEXT,
    CodingLegacyCostFacts,
    CodingLegacyUsageFacts,
)

#: 用作 `source=price_table` 时 `pricing_version` 的来源（价格表内容身份）。
PRICING_VERSION_SOURCE: Final[str] = "price_table_hash"


class MappingFailure(RuntimeError):
    """facts → Foundation 映射失败；携带稳定 :class:`ErrorEnvelope`。"""

    def __init__(self, envelope: ErrorEnvelope) -> None:
        super().__init__(f"{envelope.code}: {envelope.message}")
        self.envelope = envelope


def make_envelope(code: str, message: str) -> ErrorEnvelope:
    """按登记分类构造 Envelope。"""
    category = ERROR_CODE_CATEGORY.get(code)
    if category is None:  # pragma: no cover - 仅未登记错误码时触发
        raise ValueError(f"未登记的错误码：{code}")
    return ErrorEnvelope(category=category, code=code, message=message)


def map_usage(facts: CodingLegacyUsageFacts) -> Usage:
    """usage facts → :class:`Usage`；unknown（null）与显式零（0）严格区分。"""
    if not facts.call_observed or not facts.usage_object_present:
        return Usage(source=UsageSource.UNKNOWN)
    if not facts.any_value_present:
        return Usage(source=UsageSource.UNKNOWN)
    try:
        return Usage(
            input_tokens=facts.prompt_tokens,
            output_tokens=facts.completion_tokens,
            total_tokens=facts.total_tokens,
            source=UsageSource.PROVIDER_REPORTED,
        )
    except ValidationError as exc:
        raise MappingFailure(
            make_envelope(FOUNDATION_INVALID_USAGE, f"usage facts 不满足契约：{_short(exc)}")
        ) from exc


def map_cost(facts: CodingLegacyCostFacts) -> Cost:
    """cost facts → :class:`Cost`。

    空价格表（当前 Coding 的实际情况）与未知模型都得到 ``amount=null`` +
    ``source=unknown``，并保留 USD 上下文。
    """
    currency = facts.provider_reported_currency or facts.currency_context or CURRENCY_CONTEXT

    if facts.provider_reported_cost and facts.legacy_cost_usd is not None:
        return _build_cost(
            facts,
            amount=_decimal(facts.legacy_cost_usd),
            currency=currency,
            source=CostSource.PROVIDER_REPORTED,
            with_pricing_version=False,
        )

    if not facts.price_entry_present or not facts.pricing_value_present:
        # 空表 / 模型不在表内：未知，不是零。
        return _build_cost(
            facts,
            amount=None,
            currency=currency,
            source=CostSource.UNKNOWN,
            with_pricing_version=False,
        )

    if facts.legacy_cost_usd is None:
        return _build_cost(
            facts,
            amount=None,
            currency=currency,
            source=CostSource.UNKNOWN,
            with_pricing_version=False,
        )

    return _build_cost(
        facts,
        amount=_decimal(facts.legacy_cost_usd),
        currency=currency,
        source=CostSource.PRICE_TABLE,
        with_pricing_version=True,
    )


def _decimal(value: float) -> Decimal:
    """Legacy float → Decimal，先经 ``str`` 避免二进制误差扩散。"""
    return Decimal(str(value))


def _build_cost(
    facts: CodingLegacyCostFacts,
    *,
    amount: Decimal | None,
    currency: str | None,
    source: CostSource,
    with_pricing_version: bool,
) -> Cost:
    try:
        return Cost(
            amount=amount,
            currency=currency,
            source=source,
            pricing_version=facts.price_table_hash if with_pricing_version else None,
        )
    except ValidationError as exc:
        raise MappingFailure(
            make_envelope(FOUNDATION_INVALID_COST, f"cost facts 不满足契约：{_short(exc)}")
        ) from exc


def map_summary(components: Sequence[CodingLegacyCostFacts]) -> CostSummary:
    """由被观察到的组成项构建 :class:`CostSummary`。

    「无调用」与「调用后失败」在这里自然产生不同语义：前者 ``component_count=0``，
    后者保留已观察到的组成项（Spec 06 step 8）。
    """
    costs = [map_cost(component) for component in components]
    try:
        return CostSummary.from_costs(costs)
    except CurrencyMismatchError as exc:
        raise MappingFailure(
            make_envelope(FOUNDATION_CURRENCY_MISMATCH, f"聚合范围内币种不一致：{exc}")
        ) from exc
    except ValidationError as exc:
        raise MappingFailure(
            make_envelope(FOUNDATION_INVALID_COST_SUMMARY, f"summary 不满足契约：{_short(exc)}")
        ) from exc


def map_observation(
    usage_facts: CodingLegacyUsageFacts | None = None,
    cost_facts: CodingLegacyCostFacts | None = None,
    components: Sequence[CodingLegacyCostFacts] | None = None,
) -> FoundationObservation:
    """组合成 :class:`FoundationObservation`（至少一项）。"""
    usage = map_usage(usage_facts) if usage_facts is not None else None
    cost = map_cost(cost_facts) if cost_facts is not None else None
    summary = map_summary(components) if components is not None else None
    try:
        return FoundationObservation(usage=usage, cost=cost, cost_summary=summary)
    except ValidationError as exc:
        raise MappingFailure(
            make_envelope(FOUNDATION_INVALID_USAGE, f"observation 为空：{_short(exc)}")
        ) from exc


def _short(exc: Exception, limit: int = 240) -> str:
    return " ".join(str(exc).split())[:limit]


__all__ = [
    "PRICING_VERSION_SOURCE",
    "MappingFailure",
    "make_envelope",
    "map_usage",
    "map_cost",
    "map_summary",
    "map_observation",
]
