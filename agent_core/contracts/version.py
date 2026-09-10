"""Contract Set 版本与工具链版本常量。

本模块只承载**版本事实**，不承载治理成熟度（``EXPERIMENTAL`` / ``CYCLING`` /
``ELIGIBLE`` / ...）。成熟度属于治理 artifact，不进入 Contract Payload，
也不改变契约身份（Master Spec §12.1）。

版本语义（Master Spec §12.6，lockstep Contract Set Versioning）：

```text
0.1.0  Foundation Cycle 1
0.2.0  真实 L2/L3 反馈导致的非文档语义修订
0.x.y  不改变 Schema / 不变量 / 映射 / 行为的修正
1.0.0  首批成熟 family 已抽入 agent-core 并承诺稳定公共 API
```

整个 ``agent_core/contracts`` 载荷只使用这一个版本号；各 family 不维护独立 SemVer。
"""
from __future__ import annotations

from typing import Final

#: 契约族标识（Phase 1 只有 foundation 一族进入 CYCLING）。
CONTRACT_FAMILY: Final[str] = "foundation"

#: Phase 1 唯一的锁步 Contract Set 版本。
CONTRACT_VERSION: Final[str] = "0.1.0"

#: canonical JSON / 文件束规范化算法的版本（Master Spec §12.3–§12.4）。
CANONICALIZATION_VERSION: Final[str] = "1"

#: Schema 生成器版本，写入 payload-descriptor.json。
SCHEMA_TOOLING_VERSION: Final[str] = "1"

#: Payload 哈希与 transport bundle 工具版本。
PAYLOAD_TOOLING_VERSION: Final[str] = "1"

#: v0.1 必须精确固定的 Pydantic 版本（Master Spec §1.1）。
#: 升级 Pydantic 属于显式的 contract/tooling change，必须走版本决策。
PYDANTIC_PIN: Final[str] = "2.13.4"

__all__ = [
    "CONTRACT_FAMILY",
    "CONTRACT_VERSION",
    "CANONICALIZATION_VERSION",
    "SCHEMA_TOOLING_VERSION",
    "PAYLOAD_TOOLING_VERSION",
    "PYDANTIC_PIN",
]
