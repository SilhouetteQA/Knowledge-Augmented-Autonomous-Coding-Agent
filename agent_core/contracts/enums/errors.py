"""错误分类与 Foundation v0.1 错误码。

:class:`ErrorEnvelope` 是**边界 DTO**，不是异常类型，也不是内部统一 Result 模式
（FND-ERR-001）。机器逻辑只允许依据 ``code`` 分支，禁止解析 ``message``
（FND-ERR-002）。

段位约定：

```text
foundation.*  v0.1 Foundation 语义错误（本模块常量）
evidence.*    Evidence 基础设施诊断码，不是新的 ErrorCategory，
              也不属于 foundation.* 语义错误（Spec 04 定义其常量）
```
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final


class ErrorCategory(StrEnum):
    """v0.1 只有三个错误大类。"""

    VALIDATION = "validation"
    COMPATIBILITY = "compatibility"
    AGGREGATION = "aggregation"


#: 传入的 Usage facts / 字段组合不满足契约不变量。
FOUNDATION_INVALID_USAGE: Final[str] = "foundation.invalid_usage"

#: 传入的 Cost 不满足来源矩阵或金额语义。
FOUNDATION_INVALID_COST: Final[str] = "foundation.invalid_cost"

#: CostSummary 的计数、完整性或币种组合不合法。
FOUNDATION_INVALID_COST_SUMMARY: Final[str] = "foundation.invalid_cost_summary"

#: 契约载荷版本与运行时不匹配。
FOUNDATION_SCHEMA_VERSION_MISMATCH: Final[str] = "foundation.schema_version_mismatch"

#: 聚合范围内出现多个非空币种；禁止隐式 FX（FND-COST-002）。
FOUNDATION_CURRENCY_MISMATCH: Final[str] = "foundation.currency_mismatch"

#: v0.1 闭集。全部固定 ``retryable=false``（FND-ERR-003）。
FOUNDATION_V01_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        FOUNDATION_INVALID_USAGE,
        FOUNDATION_INVALID_COST,
        FOUNDATION_INVALID_COST_SUMMARY,
        FOUNDATION_SCHEMA_VERSION_MISMATCH,
        FOUNDATION_CURRENCY_MISMATCH,
    }
)

#: 各错误码的默认分类，便于 Adapter 在边界构造 ErrorEnvelope。
ERROR_CODE_CATEGORY: Final[dict[str, ErrorCategory]] = {
    FOUNDATION_INVALID_USAGE: ErrorCategory.VALIDATION,
    FOUNDATION_INVALID_COST: ErrorCategory.VALIDATION,
    FOUNDATION_INVALID_COST_SUMMARY: ErrorCategory.AGGREGATION,
    FOUNDATION_SCHEMA_VERSION_MISMATCH: ErrorCategory.COMPATIBILITY,
    FOUNDATION_CURRENCY_MISMATCH: ErrorCategory.AGGREGATION,
}

__all__ = [
    "ErrorCategory",
    "FOUNDATION_INVALID_USAGE",
    "FOUNDATION_INVALID_COST",
    "FOUNDATION_INVALID_COST_SUMMARY",
    "FOUNDATION_SCHEMA_VERSION_MISMATCH",
    "FOUNDATION_CURRENCY_MISMATCH",
    "FOUNDATION_V01_ERROR_CODES",
    "ERROR_CODE_CATEGORY",
]
