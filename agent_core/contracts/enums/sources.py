"""用量与成本的来源枚举。

来源描述的是**认识论可信度**，不是"数值被存在哪里"（FND-COST-011）：

```text
Unknown is not Zero
Estimated is not Reported
USD is not CNY
Raw Cost is not Converted Cost
```
"""
from __future__ import annotations

from enum import StrEnum


class UsageSource(StrEnum):
    """已填充 token 字段的共同来源（object-level，v0.1 不做 per-field provenance）。"""

    PROVIDER_REPORTED = "provider_reported"
    LOCALLY_CALCULATED = "locally_calculated"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class CostSource(StrEnum):
    """成本金额的认识论来源。"""

    PROVIDER_REPORTED = "provider_reported"
    PRICE_TABLE = "price_table"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


__all__ = ["UsageSource", "CostSource"]
