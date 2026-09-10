"""``ErrorEnvelope`` 的一致性证明。

对应 Master Appendix A.4 的 FND-ERR-001 至 FND-ERR-005。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.errors import (
    ERROR_CODE_CATEGORY,
    FOUNDATION_INVALID_COST,
    FOUNDATION_INVALID_COST_SUMMARY,
    FOUNDATION_INVALID_USAGE,
    FOUNDATION_SCHEMA_VERSION_MISMATCH,
    FOUNDATION_V01_ERROR_CODES,
    ErrorCategory,
)
from agent_core.contracts.models.error import ERROR_MESSAGE_MAX_LENGTH, ErrorEnvelope


@contract_rule("FND-ERR-001", "FND-ERR-005")
def test_error_envelope_is_a_boundary_dto_not_an_exception() -> None:
    """它是 Pydantic DTO，不是可用于 raise / except 的异常类型。"""
    assert issubclass(ErrorEnvelope, BaseModel)
    assert not issubclass(ErrorEnvelope, BaseException)

    with pytest.raises(TypeError):
        raise ErrorEnvelope(  # type: ignore[misc]  # noqa: TRY301
            category=ErrorCategory.VALIDATION,
            code=FOUNDATION_INVALID_USAGE,
            message="not raisable",
        )


@contract_rule("FND-ERR-002")
def test_code_is_the_machine_branching_key() -> None:
    """机器逻辑依据 code：相同 code、不同 message 必须是同一个机器分支。"""
    first = ErrorEnvelope(
        category=ErrorCategory.VALIDATION,
        code=FOUNDATION_INVALID_COST,
        message="amount 缺失",
    )
    second = ErrorEnvelope(
        category=ErrorCategory.VALIDATION,
        code=FOUNDATION_INVALID_COST,
        message="totally different human text",
    )
    assert first.code == second.code
    assert first.message != second.message


@contract_rule("FND-ERR-002")
@pytest.mark.parametrize(
    "code",
    ["BadCode", "foundation", "foundation.", ".foundation.x", "foundation.9x", "FOUNDATION.X", "foundation.x-y"],
)
def test_invalid_code_shape_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(category=ErrorCategory.VALIDATION, code=code, message="x")


@contract_rule("FND-ERR-003")
def test_v01_foundation_errors_are_not_retryable() -> None:
    """v0.1 全部 Foundation 错误固定 retryable=false。"""
    for code in sorted(FOUNDATION_V01_ERROR_CODES):
        envelope = ErrorEnvelope(
            category=ERROR_CODE_CATEGORY[code], code=code, message="x"
        )
        assert envelope.retryable is False


@contract_rule("FND-ERR-003")
def test_retryable_true_rejected() -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            category=ErrorCategory.VALIDATION,
            code=FOUNDATION_INVALID_USAGE,
            message="x",
            retryable=True,
        )


@contract_rule("FND-ERR-003")
def test_v01_error_code_set_is_closed() -> None:
    assert FOUNDATION_V01_ERROR_CODES == {
        FOUNDATION_INVALID_USAGE,
        FOUNDATION_INVALID_COST,
        FOUNDATION_INVALID_COST_SUMMARY,
        FOUNDATION_SCHEMA_VERSION_MISMATCH,
        "foundation.currency_mismatch",
    }
    assert set(ERROR_CODE_CATEGORY) == set(FOUNDATION_V01_ERROR_CODES)
    assert all(isinstance(category, ErrorCategory) for category in ERROR_CODE_CATEGORY.values())


@contract_rule("FND-ERR-004")
def test_message_is_bounded() -> None:
    ok = ErrorEnvelope(
        category=ErrorCategory.VALIDATION,
        code=FOUNDATION_INVALID_COST,
        message="x" * ERROR_MESSAGE_MAX_LENGTH,
    )
    assert len(ok.message) == ERROR_MESSAGE_MAX_LENGTH

    with pytest.raises(ValidationError):
        ErrorEnvelope(
            category=ErrorCategory.VALIDATION,
            code=FOUNDATION_INVALID_COST,
            message="x" * (ERROR_MESSAGE_MAX_LENGTH + 1),
        )


@contract_rule("FND-ERR-004")
@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_empty_message_rejected(message: str) -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            category=ErrorCategory.VALIDATION, code=FOUNDATION_INVALID_COST, message=message
        )


@contract_rule("FND-ERR-004")
def test_exception_objects_are_not_accepted_as_message() -> None:
    """异常对象与堆栈不得直接进入 Envelope；细节必须结构化、已净化。"""
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            category=ErrorCategory.VALIDATION,
            code=FOUNDATION_INVALID_COST,
            message=ValueError("boom"),  # type: ignore[arg-type]
        )


@contract_rule("FND-ERR-004")
def test_details_belong_in_structured_extensions() -> None:
    envelope = ErrorEnvelope(
        category=ErrorCategory.AGGREGATION,
        code="foundation.currency_mismatch",
        message="multiple currencies observed",
        extensions={"adapter.currencies": ["CNY", "USD"], "adapter.count": 2},
    )
    assert envelope.extensions["adapter.currencies"] == ["CNY", "USD"]


@contract_rule("FND-ERR-001")
def test_error_envelope_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(
            category=ErrorCategory.VALIDATION,
            code=FOUNDATION_INVALID_COST,
            message="x",
            cause_code="foundation.invalid_usage",  # type: ignore[call-arg]
        )


@contract_rule("FND-ERR-001")
def test_category_is_required_and_closed() -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(code=FOUNDATION_INVALID_COST, message="x")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ErrorEnvelope(category="unknown_category", code=FOUNDATION_INVALID_COST, message="x")

    assert {c.value for c in ErrorCategory} == {"validation", "compatibility", "aggregation"}
