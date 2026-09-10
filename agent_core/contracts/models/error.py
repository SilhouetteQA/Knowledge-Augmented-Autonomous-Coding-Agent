"""``ErrorEnvelope``：错误跨边界时的稳定数据表示。

它是**边界 DTO**，不是异常类型，也不是内部统一 Result 模式（FND-ERR-001）：

```text
禁止 raise ErrorEnvelope / except ErrorEnvelope
禁止用它替换仓库现有异常类、传播、重试与恢复逻辑
禁止建立内部 Result[T, ErrorEnvelope] 通用返回模式
```

只在序列化、API / IPC / MCP、跨仓 Adapter 或 Evaluation artifact 边界，
由 Adapter 把内部错误映射为 Envelope。

机器逻辑只允许依据 ``code`` 分支，**禁止**解析 ``message``（FND-ERR-002）。
v0.1 所有 Foundation 错误固定 ``retryable=false``（FND-ERR-003）。
"""
from __future__ import annotations

from typing import Final

from pydantic import Field, field_validator

from agent_core.contracts.enums.errors import ErrorCategory
from agent_core.contracts.models.base import (
    DOTTED_IDENTIFIER_PATTERN,
    FoundationModel,
    JsonValue,
)

#: ``message`` 的容量上限；保证错误细节"结构化、已净化且**有界**"（FND-ERR-004）。
ERROR_MESSAGE_MAX_LENGTH: Final[int] = 2048


class ErrorEnvelope(FoundationModel):
    """跨边界错误表示。

    ``category`` 只有 validation / compatibility / aggregation 三类；
    ``code`` 是机器分支的唯一依据。
    """

    category: ErrorCategory
    code: str
    message: str
    retryable: bool = False
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("code", mode="after")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        if not isinstance(value, str) or not DOTTED_IDENTIFIER_PATTERN.match(value):
            raise ValueError(
                "FND-ERR-002: code 必须是稳定的小写点分标识，"
                r"形状 ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$，"
                f"实际为 {value!r}"
            )
        return value

    @field_validator("message", mode="after")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("FND-ERR-004: message 必须是非空字符串")
        if len(value) > ERROR_MESSAGE_MAX_LENGTH:
            raise ValueError(
                f"FND-ERR-004: message 长度 {len(value)} 超过上限 "
                f"{ERROR_MESSAGE_MAX_LENGTH}；请把细节放进结构化 extensions"
            )
        return value

    @field_validator("retryable", mode="after")
    @classmethod
    def _validate_retryable(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("FND-ERR-003: Foundation v0.1 错误的 retryable 固定为 false")
        return value


__all__ = ["ErrorEnvelope", "ERROR_MESSAGE_MAX_LENGTH"]
