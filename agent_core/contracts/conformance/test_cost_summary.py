"""``CostSummary`` 的一致性证明。

对应 Master Appendix A.3 的 FND-CSUM-001 至 FND-CSUM-011，
以及币种兼容性规则 FND-COST-002。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.errors import FOUNDATION_CURRENCY_MISMATCH
from agent_core.contracts.enums.sources import CostSource
from agent_core.contracts.models.cost import Cost, CostSummary, CurrencyMismatchError

ESTIMATED = CostSource.ESTIMATED
UNKNOWN = CostSource.UNKNOWN


def _known(amount: str, currency: str = "USD") -> Cost:
    return Cost(amount=Decimal(amount), currency=currency, source=ESTIMATED)


def _unknown(currency: str | None = None) -> Cost:
    return Cost(currency=currency, source=UNKNOWN)


@contract_rule("FND-CSUM-009")
def test_empty_aggregation() -> None:
    """空集合固定为 0/0/0、金额与币种为 null、complete=true。"""
    summary = CostSummary.from_costs([])
    assert summary.component_count == 0
    assert summary.known_component_count == 0
    assert summary.unknown_component_count == 0
    assert summary.known_amount is None
    assert summary.currency is None
    assert summary.complete is True


@contract_rule("FND-CSUM-002", "FND-CSUM-003")
def test_counts_reconcile_and_complete_definition() -> None:
    summary = CostSummary.from_costs([_known("1.5"), _unknown()])
    assert summary.component_count == 2
    assert summary.known_component_count == 1
    assert summary.unknown_component_count == 1
    assert summary.component_count == (
        summary.known_component_count + summary.unknown_component_count
    )
    assert summary.complete is (summary.unknown_component_count == 0)


@contract_rule("FND-CSUM-002")
def test_count_reconciliation_violation_rejected() -> None:
    with pytest.raises(ValidationError):
        CostSummary(
            complete=True,
            component_count=5,
            known_component_count=1,
            unknown_component_count=0,
            known_amount=Decimal("1"),
            currency="USD",
        )


@contract_rule("FND-CSUM-003")
def test_complete_must_match_unknown_count() -> None:
    # unknown_component_count > 0 却声明 complete
    with pytest.raises(ValidationError):
        CostSummary(
            complete=True,
            component_count=1,
            known_component_count=0,
            unknown_component_count=1,
        )
    # unknown_component_count == 0 却声明 incomplete
    with pytest.raises(ValidationError):
        CostSummary(
            complete=False,
            component_count=1,
            known_component_count=1,
            unknown_component_count=0,
            known_amount=Decimal("1"),
            currency="USD",
        )


@contract_rule("FND-CSUM-001")
@pytest.mark.parametrize("field", ["component_count", "known_component_count", "unknown_component_count"])
def test_negative_counts_rejected(field: str) -> None:
    base = {
        "complete": True,
        "component_count": 0,
        "known_component_count": 0,
        "unknown_component_count": 0,
    }
    base[field] = -1
    with pytest.raises(ValidationError):
        CostSummary(**base)


@contract_rule("FND-CSUM-004")
def test_zero_known_components_require_null_amount() -> None:
    with pytest.raises(ValidationError):
        CostSummary(
            complete=True,
            component_count=0,
            known_component_count=0,
            unknown_component_count=0,
            known_amount=Decimal("0"),
            currency="USD",
        )

    all_unknown = CostSummary.from_costs([_unknown(), _unknown()])
    assert all_unknown.known_component_count == 0
    assert all_unknown.known_amount is None
    assert all_unknown.complete is False


@contract_rule("FND-CSUM-005")
def test_known_components_require_amount_and_currency() -> None:
    with pytest.raises(ValidationError):
        CostSummary(
            complete=True,
            component_count=1,
            known_component_count=1,
            unknown_component_count=0,
            currency="USD",
        )
    with pytest.raises(ValidationError):
        CostSummary(
            complete=True,
            component_count=1,
            known_component_count=1,
            unknown_component_count=0,
            known_amount=Decimal("1"),
        )


@contract_rule("FND-CSUM-006")
def test_true_zero_is_a_known_component() -> None:
    summary = CostSummary.from_costs([_known("0"), _known("2.5")])
    assert summary.known_component_count == 2
    assert summary.unknown_component_count == 0
    assert summary.known_amount == Decimal("2.5")
    assert summary.complete is True


@contract_rule("FND-CSUM-007", "FND-COST-002")
def test_mixed_currencies_fail_explicitly() -> None:
    """币种冲突必须先于完整性判断显式失败，不得返回伪造的 complete=false 混合金额。"""
    with pytest.raises(CurrencyMismatchError) as excinfo:
        CostSummary.from_costs([_known("1", "USD"), _known("2", "CNY")])

    assert excinfo.value.code == FOUNDATION_CURRENCY_MISMATCH
    assert excinfo.value.currencies == ("CNY", "USD")


@contract_rule("FND-CSUM-007")
def test_known_usd_with_unknown_cny_still_mismatches() -> None:
    """已知金额 USD 与未知金额 CNY 仍然是 CurrencyMismatch。"""
    with pytest.raises(CurrencyMismatchError):
        CostSummary.from_costs([_known("1", "USD"), _unknown("CNY")])


@contract_rule("FND-CSUM-008")
def test_null_currency_does_not_create_mismatch_but_stays_incomplete() -> None:
    """currency 缺失本身不产生 mismatch，但对应未知金额仍令 summary incomplete。"""
    summary = CostSummary.from_costs([_known("1", "USD"), _unknown(None)])
    assert summary.currency == "USD"
    assert summary.known_amount == Decimal("1")
    assert summary.unknown_component_count == 1
    assert summary.complete is False


@contract_rule("FND-CSUM-010")
def test_summary_declares_no_source_or_pricing_version() -> None:
    fields = set(CostSummary.model_fields)
    assert fields == {
        "known_amount",
        "currency",
        "complete",
        "component_count",
        "known_component_count",
        "unknown_component_count",
        "extensions",
    }
    for forbidden in ("source", "pricing_version"):
        assert forbidden not in fields


@contract_rule("FND-CSUM-011")
def test_summary_built_from_observed_components() -> None:
    """Summary 必须由被观察到的组成项构建，不得从 legacy total 反推。

    ``from_costs`` 只接受组成项；不存在"只传一个 total 数字"的构造入口，
    并且已知小计等于各已知组成项之和。
    """
    summary = CostSummary.from_costs([_known("0.1"), _known("0.2")])
    assert summary.known_amount == Decimal("0.3")
    assert summary.component_count == 2

    # 构造入口不存在 total 语义参数
    import inspect

    parameters = set(inspect.signature(CostSummary.from_costs).parameters)
    assert "total" not in parameters
    assert "legacy_total" not in parameters


@contract_rule("FND-CSUM-009")
def test_summary_serialization_uses_decimal_strings() -> None:
    summary = CostSummary.from_costs([_known("1.50")])
    dumped = summary.model_dump(mode="json")
    assert dumped["known_amount"] == "1.50"
    assert dumped["currency"] == "USD"
    assert dumped["complete"] is True
