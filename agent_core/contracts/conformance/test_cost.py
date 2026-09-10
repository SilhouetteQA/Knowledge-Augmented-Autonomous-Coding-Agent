"""``Cost`` 的一致性证明。

对应 Master Appendix A.2 的 FND-COST-001 至 FND-COST-013。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.sources import CostSource
from agent_core.contracts.models.cost import Cost

PRICE_TABLE = CostSource.PRICE_TABLE
ESTIMATED = CostSource.ESTIMATED
PROVIDER = CostSource.PROVIDER_REPORTED
UNKNOWN = CostSource.UNKNOWN


@contract_rule("FND-COST-001", "FND-COST-012")
def test_unknown_cost_is_not_zero() -> None:
    """未知成本必须是 ``amount=null, source=unknown``，不得用 0 表示。"""
    unknown = Cost(source=UNKNOWN)
    assert unknown.amount is None
    assert unknown.source is UNKNOWN

    # 单个 legacy 数值 0 不足以证明真实零：unknown + amount 必须构造失败
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("0"), source=UNKNOWN)


@contract_rule("FND-COST-001")
def test_unknown_cannot_serialize_as_zero_unknown() -> None:
    """验收要求：未知 Cost 无法序列化为 ``amount="0", source="unknown"``。"""
    serialized = Cost(source=UNKNOWN).model_dump(mode="json")
    assert serialized["amount"] is None
    assert serialized["source"] == "unknown"
    assert (serialized["amount"], serialized["source"]) != ("0", "unknown")


@contract_rule("FND-COST-007")
def test_explicit_zero_with_provenance_is_allowed() -> None:
    """有明确来源证明的真实零成本是合法已知值。"""
    free = Cost(amount=Decimal("0"), currency="USD", source=PROVIDER)
    assert free.amount == Decimal("0")
    assert free.model_dump(mode="json")["amount"] == "0"


@contract_rule("FND-COST-003")
@pytest.mark.parametrize("raw", [0.1, 1.0, -0.5, float("nan")])
def test_float_amount_rejected(raw: float) -> None:
    """float 金额输入必须被拒绝，避免二进制误差跨边界扩散。"""
    with pytest.raises(ValidationError):
        Cost(amount=raw, currency="USD", source=ESTIMATED)


@contract_rule("FND-COST-003")
def test_decimal_serializes_as_decimal_string() -> None:
    cost = Cost(amount=Decimal("0.1200"), currency="USD", source=ESTIMATED)
    dumped = cost.model_dump(mode="json")

    assert dumped["amount"] == "0.1200"
    assert isinstance(dumped["amount"], str)
    assert not isinstance(dumped["amount"], float)
    # Python 模式仍保留 Decimal，供项目内部继续精确计算
    assert isinstance(cost.model_dump()["amount"], Decimal)


@contract_rule("FND-COST-003")
def test_decimal_precision_is_not_normalized() -> None:
    """契约层不改写小数位：``0.10`` 与 ``0.100`` 原样保留。"""
    assert Cost(amount=Decimal("0.10"), currency="USD", source=ESTIMATED).model_dump(mode="json")["amount"] == "0.10"
    assert Cost(amount=Decimal("0.100"), currency="USD", source=ESTIMATED).model_dump(mode="json")["amount"] == "0.100"
    assert Decimal("0.10") == Decimal("0.100")


@contract_rule("FND-COST-003")
def test_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("-0.01"), currency="USD", source=ESTIMATED)


@contract_rule("FND-COST-004")
@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "12A", "U S D", ""])
def test_invalid_currency_code_rejected(currency: str) -> None:
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("1"), currency=currency, source=ESTIMATED)


@contract_rule("FND-COST-004")
@pytest.mark.parametrize("currency", ["USD", "CNY", "EUR", "JPY"])
def test_valid_uppercase_currency_accepted(currency: str) -> None:
    assert Cost(amount=Decimal("1"), currency=currency, source=ESTIMATED).currency == currency


@contract_rule("FND-COST-005")
def test_source_matrix_known_amount_requires_currency() -> None:
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("1"), source=ESTIMATED)


@contract_rule("FND-COST-006", "FND-COST-013")
def test_price_table_requires_pricing_version() -> None:
    """价格表推导出的已知金额必须引用可复现的 pricing snapshot 标识。"""
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("1"), currency="USD", source=PRICE_TABLE)

    with pytest.raises(ValidationError):
        Cost(amount=Decimal("1"), currency="USD", source=PRICE_TABLE, pricing_version="")

    ok = Cost(
        amount=Decimal("1"),
        currency="USD",
        source=PRICE_TABLE,
        pricing_version="sha256:0f1e2d",
    )
    assert ok.pricing_version == "sha256:0f1e2d"


@contract_rule("FND-COST-008")
def test_unknown_amount_may_keep_currency_context() -> None:
    """unknown 金额可以保留已知币种上下文，但不得因此变成已知成本。"""
    cost = Cost(currency="CNY", source=UNKNOWN)
    assert cost.currency == "CNY"
    assert cost.amount is None


@contract_rule("FND-COST-009")
def test_raw_currency_is_preserved_without_conversion() -> None:
    """原始币种原样保留；契约层不做汇率换算，也不做默认币种替换。"""
    usd = Cost(amount=Decimal("1"), currency="USD", source=ESTIMATED)
    cny = Cost(amount=Decimal("1"), currency="CNY", source=ESTIMATED)

    assert usd.currency == "USD" and cny.currency == "CNY"
    # 相同数值不同币种必须序列化不同，绝不归一化
    assert usd.model_dump(mode="json") != cny.model_dump(mode="json")


@contract_rule("FND-COST-010")
def test_cost_has_no_scope_fields() -> None:
    """Cost 只表达金额本身；task / span / report 范围属于消费方 family。"""
    fields = set(Cost.model_fields)
    assert fields == {"amount", "currency", "source", "pricing_version", "extensions"}
    for forbidden in ("task_id", "span_id", "run_id", "report_id", "scope"):
        assert forbidden not in fields


@contract_rule("FND-COST-011")
def test_source_is_required_and_epistemic() -> None:
    """来源是必填的认识论维度，不是"数据存放位置"的可选标注。"""
    with pytest.raises(ValidationError):
        Cost(amount=Decimal("1"), currency="USD")  # type: ignore[call-arg]

    estimated = Cost(amount=Decimal("1"), currency="USD", source=ESTIMATED)
    reported = Cost(amount=Decimal("1"), currency="USD", source=PROVIDER)
    # 同样金额、同样币种，可信来源不同 → 契约对象必须可区分
    assert estimated.source is not reported.source
    assert estimated.model_dump(mode="json")["source"] == "estimated"
    assert reported.model_dump(mode="json")["source"] == "provider_reported"
