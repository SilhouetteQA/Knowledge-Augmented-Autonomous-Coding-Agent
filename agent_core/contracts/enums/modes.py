"""Contract Mode：旁路契约验证的统一运行策略。

三个模式必须共用同一份 facts extractor、Adapter 和 validation 逻辑，
只改变**失败策略**（Master Spec §10，FND-MODE-001/002）：

```text
off     完全跳过 facts / Adapter / Evidence 分支
observe 正常映射并记录 PASS/FAIL 证据；mapping 或 sink 失败不影响业务
strict  使用完全相同路径；任何契约失败令验证命令失败
```

``observe`` 不是 fallback，也不存在 Foundation 主路径。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

#: 唯一合法的配置入口（FND-MODE-003）。
CONTRACT_MODE_ENV: Final[str] = "AGENT_CONTRACT_MODE"


class ContractMode(StrEnum):
    """契约运行模式。"""

    OFF = "off"
    OBSERVE = "observe"
    STRICT = "strict"


class ContractModeConfigError(ValueError):
    """``AGENT_CONTRACT_MODE`` 取值非法。

    注意：它是普通配置错误，不是 :class:`ErrorEnvelope` 的替代品
    （FND-ERR-001）；ErrorEnvelope 只在序列化 / API / IPC / MCP /
    Adapter / artifact 边界由 Adapter 产生。
    """


def parse_contract_mode(raw: str | None) -> ContractMode:
    """把环境变量原文解析为 :class:`ContractMode`。

    未设置、空白或空串 → :attr:`ContractMode.OFF`（默认不介入业务）。
    任何其它非法值 → 明确抛 :class:`ContractModeConfigError`，
    **禁止**静默降级为 ``off``（FND-MODE-003）。
    """
    if raw is None:
        return ContractMode.OFF

    text = raw.strip()
    if not text:
        return ContractMode.OFF

    try:
        return ContractMode(text.lower())
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ContractMode)
        raise ContractModeConfigError(
            f"{CONTRACT_MODE_ENV} 取值非法: {raw!r}；允许值: {allowed}"
        ) from exc


def resolve_contract_mode(env: Mapping[str, str] | None = None) -> ContractMode:
    """从环境解析当前 Contract Mode。

    :param env: 可注入的映射（便于测试）；为 ``None`` 时读取 ``os.environ``。
    """
    source = os.environ if env is None else env
    return parse_contract_mode(source.get(CONTRACT_MODE_ENV))


__all__ = [
    "CONTRACT_MODE_ENV",
    "ContractMode",
    "ContractModeConfigError",
    "parse_contract_mode",
    "resolve_contract_mode",
]
