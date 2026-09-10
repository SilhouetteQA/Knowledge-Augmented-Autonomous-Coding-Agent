"""``Usage``：一次边界活动的 token 用量。

核心语义（Master Spec §4.2，FND-USAGE-001 至 FND-USAGE-003）：

```text
0    = 明确观察到零
null = 未知或未报告
```

- 所有非空 token 字段必须是非负整数。
- provider 返回的 ``total_tokens`` **原样保留**，即使与 input/output 之和不一致也不改写。
- provider 未返回 ``total_tokens`` 时保持 ``null``，Adapter **禁止**自动相加。
- ``source`` 是 object-level 的共同来源；缺失字段为 ``null``，不会单独降低来源等级。
- v0.1 **不做** per-field provenance；真实路径出现混合来源需求时由 L2/L3 驱动 v0.2。
"""
from __future__ import annotations

import math
from typing import Final

from pydantic import Field, ValidationInfo, field_validator

from agent_core.contracts.enums.sources import UsageSource
from agent_core.contracts.models.base import FoundationModel, JsonValue

#: Usage 的 token 字段集合。
TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


class Usage(FoundationModel):
    """token 用量值对象。

    ``total_tokens`` 没有任何自动推导语义：它要么是 provider 报告的原始值，
    要么是 ``null``。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    source: UsageSource
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator(*TOKEN_FIELDS, mode="before")
    @classmethod
    def _validate_token_count(cls, value: object, info: ValidationInfo) -> object:
        """校验 token 计数：非负整数，或 ``None``。"""
        field = info.field_name
        if value is None:
            return None

        if isinstance(value, bool):
            raise ValueError(f"FND-USAGE-001: {field} 不得为布尔值")

        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"FND-USAGE-001: {field} 不得为 NaN/Infinity")
            if not value.is_integer():
                # 拒绝非整数浮点，避免静默截断（不引入二进制误差）
                raise ValueError(
                    f"FND-USAGE-001: {field} 必须是非负整数，实际为 {value!r}"
                )
            value = int(value)

        if not isinstance(value, int):
            raise ValueError(
                f"FND-USAGE-001: {field} 必须是非负整数或 null，"
                f"实际为 {type(value).__name__}"
            )

        if value < 0:
            raise ValueError(f"FND-USAGE-001: {field} 不得为负（实际 {value}）")

        return value


__all__ = ["Usage", "TOKEN_FIELDS"]
