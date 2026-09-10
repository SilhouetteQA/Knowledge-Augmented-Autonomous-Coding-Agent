"""``Usage`` 的一致性证明。

对应 Master Appendix A.2 的 FND-USAGE-001 至 FND-USAGE-003。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.sources import UsageSource
from agent_core.contracts.models.usage import TOKEN_FIELDS, Usage

PROVIDER = UsageSource.PROVIDER_REPORTED


@contract_rule("FND-USAGE-001")
def test_unknown_is_not_zero() -> None:
    """未知（null）与显式零（0）必须是两个可区分的状态。"""
    unknown = Usage(source=UsageSource.UNKNOWN)
    explicit_zero = Usage(input_tokens=0, output_tokens=0, source=PROVIDER)

    assert unknown.input_tokens is None
    assert unknown.output_tokens is None
    assert explicit_zero.input_tokens == 0
    assert explicit_zero.output_tokens == 0
    # 两者序列化结果必须不同，否则"未知被零掩盖"的语义债务会跨边界传播
    assert unknown.model_dump(mode="json") != explicit_zero.model_dump(mode="json")


@contract_rule("FND-USAGE-001")
@pytest.mark.parametrize("field", list(TOKEN_FIELDS))
def test_negative_token_count_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        Usage(source=PROVIDER, **{field: -1})


@contract_rule("FND-USAGE-001")
@pytest.mark.parametrize("bad", [True, False, 1.5, -0.5, "12", [], {}])
def test_non_integer_token_count_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        Usage(source=PROVIDER, input_tokens=bad)


@contract_rule("FND-USAGE-001")
@pytest.mark.parametrize("ok", [0, 7, 2.0])
def test_integral_values_accepted(ok: object) -> None:
    """整数与等值整数浮点可接受；非等值浮点在上一个测试中被拒绝（不做静默截断）。"""
    assert Usage(source=PROVIDER, input_tokens=ok).input_tokens == int(ok)


@contract_rule("FND-USAGE-002")
def test_provider_total_preserved_exactly() -> None:
    """provider 报告的 total 原样保留，即使与可见组成项之和不一致。"""
    usage = Usage(input_tokens=10, output_tokens=2, total_tokens=99, source=PROVIDER)
    assert usage.total_tokens == 99

    dumped = usage.model_dump(mode="json")
    assert dumped["total_tokens"] == 99
    assert dumped["input_tokens"] + dumped["output_tokens"] != dumped["total_tokens"]


@contract_rule("FND-USAGE-003")
def test_missing_total_not_synthesized() -> None:
    """缺失的 total 必须保持 null；Adapter 禁止自动相加 input + output。"""
    usage = Usage(input_tokens=10, output_tokens=2, source=PROVIDER)
    assert usage.total_tokens is None
    assert usage.model_dump(mode="json")["total_tokens"] is None


@contract_rule("FND-USAGE-001")
def test_source_is_required() -> None:
    """object-level source 是必填；无法凭默认值猜测来源。"""
    with pytest.raises(ValidationError):
        Usage(input_tokens=1)  # type: ignore[call-arg]


@contract_rule("FND-USAGE-001")
def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Usage(source=PROVIDER, task_id="t-1")  # type: ignore[call-arg]


@contract_rule("FND-USAGE-001")
def test_cache_fields_are_nullable_and_distinct() -> None:
    usage = Usage(source=PROVIDER, cache_read_tokens=0)
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens is None
