"""``Cost`` 与 ``CostSummary``：不可变金额与聚合状态。

语义边界（Master Spec §4.3–§4.4，FND-COST-* / FND-CSUM-*）：

```text
amount=0 只表示有 presence/provenance 证明的真实零成本
source=unknown 时 amount 必须为 null —— unknown 不能用 0 表示
Contract 层不做汇率换算，也不做默认币种替换
CostSummary 不发明统一 source / pricing_version
```

币种兼容性**先于**完整性计算：聚合范围内出现多个非空币种时显式失败，
不允许返回"混合币种 + complete=false"的伪造结果（FND-COST-002 / FND-CSUM-007）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import Field, SerializationInfo, ValidationInfo, field_serializer, field_validator, model_validator

from agent_core.contracts.enums.errors import FOUNDATION_CURRENCY_MISMATCH
from agent_core.contracts.enums.sources import CostSource
from agent_core.contracts.models.base import FoundationModel, JsonValue

#: 币种：大写三字母 ISO 4217 形状；刻意不使用封闭 Enum（FND-COST-004）。
CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{3}$")

#: 参与计数校验的 CostSummary 计数字段。
COUNT_FIELDS: Final[tuple[str, ...]] = (
    "component_count",
    "known_component_count",
    "unknown_component_count",
)


class CostSummaryError(ValueError):
    """Cost / CostSummary 聚合语义失败。

    ``code`` 用于 Adapter 在边界映射为 :class:`ErrorEnvelope`；本层不产生 Envelope。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CurrencyMismatchError(CostSummaryError):
    """聚合范围内出现多个非空币种（FND-COST-002 / FND-CSUM-007）。"""

    def __init__(self, currencies: Iterable[str]) -> None:
        ordered = sorted(set(currencies))
        super().__init__(
            FOUNDATION_CURRENCY_MISMATCH,
            f"聚合范围出现多个币种 {ordered}；Contract 层不做隐式汇率换算",
        )
        self.currencies = tuple(ordered)


def _coerce_decimal(value: object, field: str) -> Decimal | None:
    """把输入规范化为 :class:`Decimal`；拒绝 float，避免二进制误差（FND-COST-003）。"""
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError(f"FND-COST-003: {field} 不得为布尔值")

    if isinstance(value, float):
        raise ValueError(
            f"FND-COST-003: {field} 禁止使用 float 输入（会引入二进制误差）；"
            "请显式使用 Decimal(str(value))"
        )

    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"FND-COST-003: {field} 不是合法十进制字符串: {value!r}") from exc
    else:
        raise ValueError(
            f"FND-COST-003: {field} 必须是 Decimal / 十进制字符串 / null，"
            f"实际为 {type(value).__name__}"
        )

    if not decimal_value.is_finite():
        raise ValueError(f"FND-COST-003: {field} 必须是有限十进制数，实际为 {decimal_value}")
    if decimal_value < 0:
        raise ValueError(f"FND-COST-003: {field} 不得为负（实际 {decimal_value}）")

    return decimal_value


def _validate_currency_code(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not CURRENCY_PATTERN.match(value):
        raise ValueError(
            f"FND-COST-004: {field} 必须是大写三字母 ISO 币种码或 null，实际为 {value!r}"
        )
    return value


def _serialize_decimal(value: Decimal | None, info: SerializationInfo) -> Decimal | str | None:
    """跨边界 JSON 输出十进制**字符串**；Python 模式保留 Decimal（Master Spec §4）。"""
    if value is None:
        return None
    return str(value) if info.mode == "json" else value


class Cost(FoundationModel):
    """某个边界的不可变金额，带原币种与认识论来源。"""

    amount: Decimal | None = None
    currency: str | None = None
    source: CostSource
    pricing_version: str | None = None
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> object:
        return _coerce_decimal(value, "amount")

    @field_validator("currency", mode="after")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        return _validate_currency_code(value, "currency")

    @field_serializer("amount")
    def _serialize_amount(
        self, value: Decimal | None, info: SerializationInfo
    ) -> Decimal | str | None:
        return _serialize_decimal(value, info)

    @model_validator(mode="after")
    def _check_source_matrix(self) -> "Cost":
        """来源不变量矩阵（Master Spec §4.3，FND-COST-005）。"""
        if self.source is CostSource.UNKNOWN and self.amount is not None:
            raise ValueError(
                "FND-COST-001: source=unknown 时 amount 必须为 null；"
                "未知成本不得用 0 表示"
            )
        if self.amount is not None and self.currency is None:
            raise ValueError("FND-COST-005: 已知 amount 必须同时给出 currency")
        if self.source is CostSource.PRICE_TABLE and not self.pricing_version:
            raise ValueError("FND-COST-006: source=price_table 时 pricing_version 必填")
        return self


class CostSummary(FoundationModel):
    """同一聚合范围内已知小计 + 完整性状态。

    **不**声明统一 ``source`` 或 ``pricing_version``（FND-CSUM-010）；
    也**不**从 legacy total 反推组成项（FND-CSUM-011）。
    """

    known_amount: Decimal | None = None
    currency: str | None = None
    complete: bool
    component_count: int
    known_component_count: int
    unknown_component_count: int
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("known_amount", mode="before")
    @classmethod
    def _validate_known_amount(cls, value: object) -> object:
        return _coerce_decimal(value, "known_amount")

    @field_validator("currency", mode="after")
    @classmethod
    def _validate_currency(cls, value: str | None) -> str | None:
        return _validate_currency_code(value, "currency")

    @field_validator(*COUNT_FIELDS, mode="before")
    @classmethod
    def _validate_count(cls, value: object, info: ValidationInfo) -> object:
        field = info.field_name
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"FND-CSUM-001: {field} 必须是非负整数")
        if value < 0:
            raise ValueError(f"FND-CSUM-001: {field} 不得为负（实际 {value}）")
        return value

    @field_serializer("known_amount")
    def _serialize_known_amount(
        self, value: Decimal | None, info: SerializationInfo
    ) -> Decimal | str | None:
        return _serialize_decimal(value, info)

    @model_validator(mode="after")
    def _check_summary_invariants(self) -> "CostSummary":
        """计数勾稽、完整性定义与已知小计前置条件（FND-CSUM-002 至 FND-CSUM-005）。"""
        if self.component_count != self.known_component_count + self.unknown_component_count:
            raise ValueError(
                "FND-CSUM-002: component_count 必须等于 "
                "known_component_count + unknown_component_count"
            )
        if self.complete != (self.unknown_component_count == 0):
            raise ValueError(
                "FND-CSUM-003: complete 必须等于 (unknown_component_count == 0)"
            )
        if self.known_component_count == 0 and self.known_amount is not None:
            raise ValueError("FND-CSUM-004: 已知组成项为 0 时 known_amount 必须为 null")
        if self.known_component_count > 0 and self.known_amount is None:
            raise ValueError("FND-CSUM-005: 存在已知组成项时 known_amount 不得为 null")
        if self.known_component_count > 0 and self.currency is None:
            raise ValueError("FND-CSUM-005: 存在已知组成项时 currency 不得为 null")
        return self

    @classmethod
    def from_costs(
        cls,
        components: Iterable["Cost | Mapping[str, object]"],
        *,
        extensions: Mapping[str, JsonValue] | None = None,
    ) -> "CostSummary":
        """由**被观察到的组成项**构建 Summary。

        顺序固定：先做币种兼容性判断，再计算完整性与小计。
        严禁从 legacy total 反推组成项（FND-CSUM-011）。
        """
        costs: list[Cost] = []
        for component in components:
            costs.append(
                component if isinstance(component, Cost) else Cost.model_validate(component)
            )

        # 币种兼容性先于完整性：非空币种（含未知金额保留的上下文币种）必须唯一。
        currencies = {cost.currency for cost in costs if cost.currency is not None}
        if len(currencies) > 1:
            raise CurrencyMismatchError(currencies)
        currency = next(iter(currencies)) if currencies else None

        known_costs = [cost for cost in costs if cost.amount is not None]
        unknown_count = len(costs) - len(known_costs)

        if known_costs and currency is None:
            raise CostSummaryError(
                "FND-CSUM-005",
                "存在已知金额组成项但没有可用币种，无法形成已知小计",
            )

        known_amount = (
            sum((cost.amount for cost in known_costs), Decimal(0)) if known_costs else None
        )

        return cls(
            known_amount=known_amount,
            currency=currency,
            complete=unknown_count == 0,
            component_count=len(costs),
            known_component_count=len(known_costs),
            unknown_component_count=unknown_count,
            extensions=dict(extensions) if extensions else {},
        )


__all__ = [
    "CURRENCY_PATTERN",
    "COUNT_FIELDS",
    "Cost",
    "CostSummary",
    "CostSummaryError",
    "CurrencyMismatchError",
]
