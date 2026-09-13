"""Coding presence-aware facts 提取。

与 Wiki 侧**语义一致、实现独立**（Spec 06 要求两仓各自项目本地）：

```text
Foundation mapping = value + presence + provenance + pricing evidence
Legacy normalized value ≠ Foundation fact
```

本模块只做提取与保留：不构造公共 Pydantic 对象、不写文件、不调用 Langfuse、
**不 import 任何业务模块**（价格表由调用方注入，而不是 import `benchmark.runner`）。

硬约束：

```text
禁止 getattr(obj, name, 0) / dict.get(name, 0) / ... or 0 抹去 presence
禁止重算 provider total
禁止把 legacy cost 0.0 当作真实零
```

Coding 的现状（母 Spec §1.5）：`MODEL_PRICE_USD_PER_1K` 当前为空表，`_cost_usd` 恒返回
`0.0`；`record_usage(model, pt, ct, 0.0)` 也硬编码 `0.0`。因此 v0.1 里任何
`legacy_cost_usd == 0.0` **都不能证明真实零**，一律映射 unknown USD。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent_core.contracts.models.base import canonical_json_dumps

#: Coding 的成本记账货币（美元）。
CURRENCY_CONTEXT: Final[str] = "USD"

#: provider usage 中参与占位统计的字段。
USAGE_FIELDS: Final[tuple[str, ...]] = ("prompt_tokens", "completion_tokens", "total_tokens")


def _sha256_hex(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _as_int(value: object) -> int | None:
    """把计数转成 int；无法转换即"未知"，**不补零**。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _usable_price(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# 价格表身份
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodingPriceTable:
    """`MODEL_PRICE_USD_PER_1K` 的快照视图与稳定身份。

    Coding 没有价格表**文件**（表是 `benchmark/runner.py` 里的字典），因此这里对映射内容
    本身做 canonical JSON + SHA256 作为身份，与 Wiki 的 `pricing.json` 快照口径同构，
    用于在 `source=price_table` 时填充必需的 `pricing_version`。
    """

    entries: Mapping[str, float]
    table_hash: str

    @classmethod
    def from_mapping(cls, table: Mapping[str, object] | None) -> CodingPriceTable:
        source = table or {}
        entries = {
            str(key): float(value)
            for key, value in source.items()
            if _usable_price(value)
        }
        ordered = {key: entries[key] for key in sorted(entries)}
        return cls(
            entries=ordered,
            table_hash=_sha256_hex(canonical_json_dumps(dict(ordered)).encode("utf-8")),
        )

    @property
    def size(self) -> int:
        """表内可用单价条目数；空表是"unknown 而非 true zero"的直接证据。"""
        return len(self.entries)

    def entry_present(self, model: str | None) -> bool:
        return bool(model) and model in self.entries

    def value_present(self, model: str | None) -> bool:
        return self.entry_present(model)


# --------------------------------------------------------------------------- #
# usage facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodingLegacyUsageFacts:
    """一次 `OpenAICompatClient.chat` 调用可观察到的 usage 事实。"""

    call_observed: bool
    usage_object_present: bool
    field_presence: Mapping[str, bool]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    model: str | None = None

    @property
    def any_value_present(self) -> bool:
        return any(
            value is not None
            for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        )


def extract_usage_from_response(
    response: object | None, *, model: str | None = None
) -> CodingLegacyUsageFacts:
    """从 provider 响应对象提取 usage facts。

    注意与 Wiki 的**行为一致但结构独立**：`agent/llm.py` 在 `usage is None` 时既不累加
    `tokens_total` 也不调 `record_usage`，因此调用已发生与 usage 缺失必须分开记录。
    """
    if response is None:
        return CodingLegacyUsageFacts(
            call_observed=False,
            usage_object_present=False,
            field_presence={},
            model=model,
        )

    usage = getattr(response, "usage", None)
    if usage is None:
        return CodingLegacyUsageFacts(
            call_observed=True,
            usage_object_present=False,
            field_presence={},
            model=model,
        )

    presence: dict[str, bool] = {}
    values: dict[str, int | None] = {}
    for name in USAGE_FIELDS:
        raw = getattr(usage, name, None)
        presence[name] = raw is not None
        values[name] = _as_int(raw)

    return CodingLegacyUsageFacts(
        call_observed=True,
        usage_object_present=True,
        field_presence=presence,
        prompt_tokens=values["prompt_tokens"],
        completion_tokens=values["completion_tokens"],
        total_tokens=values["total_tokens"],
        model=model,
    )


def extract_usage_from_counts(
    prompt_tokens: object,
    completion_tokens: object,
    *,
    model: str | None = None,
    call_observed: bool = True,
) -> CodingLegacyUsageFacts:
    """从已经取出的两个计数提取 facts。

    用于 `tracing.record_usage` / `benchmark` 这类调用点：它们手里只有 coercion 之后的
    整数，因此 presence 由"是否给出"决定 —— 传入 ``None`` 即未知。
    """
    prompt_value = _as_int(prompt_tokens)
    completion_value = _as_int(completion_tokens)

    return CodingLegacyUsageFacts(
        call_observed=call_observed,
        usage_object_present=prompt_value is not None or completion_value is not None,
        field_presence={
            "prompt_tokens": prompt_value is not None,
            "completion_tokens": completion_value is not None,
            "total_tokens": False,
        },
        prompt_tokens=prompt_value,
        completion_tokens=completion_value,
        total_tokens=None,
        model=model,
    )


# --------------------------------------------------------------------------- #
# cost facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodingLegacyCostFacts:
    """一条 Coding cost 事实。"""

    call_observed: bool
    model: str | None = None
    price_entry_present: bool = False
    pricing_value_present: bool = False
    price_table_size: int = 0
    price_table_hash: str | None = None
    legacy_cost_usd: float | None = None
    legacy_cost_is_default: bool = False
    currency_context: str | None = CURRENCY_CONTEXT
    provider_reported_cost: bool = False
    provider_reported_currency: str | None = None


def extract_cost_facts(
    table: CodingPriceTable | None,
    *,
    model: str | None = None,
    legacy_cost_usd: object = None,
    call_observed: bool = True,
    legacy_cost_is_default: bool = False,
    provider_reported_cost: bool = False,
    provider_reported_currency: str | None = None,
) -> CodingLegacyCostFacts:
    """从价格表与 Legacy 数值提取 presence-aware cost facts。

    ``legacy_cost_usd`` 是调用点传进来的数值（`record_usage(..., 0.0)` 或
    `_cost_usd` 的返回值）。``legacy_cost_is_default=True`` 表示该值是调用点的默认占位
    （如硬编码 ``0.0``），它**更不能**被当作真实金额。
    """
    amount: float | None = None
    if isinstance(legacy_cost_usd, (int, float)) and not isinstance(legacy_cost_usd, bool):
        amount = float(legacy_cost_usd)

    return CodingLegacyCostFacts(
        call_observed=call_observed,
        model=model,
        price_entry_present=table.entry_present(model) if table else False,
        pricing_value_present=table.value_present(model) if table else False,
        price_table_size=table.size if table else 0,
        price_table_hash=table.table_hash if table else None,
        legacy_cost_usd=amount,
        legacy_cost_is_default=legacy_cost_is_default,
        provider_reported_cost=provider_reported_cost,
        provider_reported_currency=provider_reported_currency,
    )


__all__ = [
    "CURRENCY_CONTEXT",
    "USAGE_FIELDS",
    "CodingPriceTable",
    "CodingLegacyUsageFacts",
    "CodingLegacyCostFacts",
    "extract_usage_from_response",
    "extract_usage_from_counts",
    "extract_cost_facts",
]
