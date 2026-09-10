"""``agent_core`` 命名空间预留（namespace reservation）。

Phase 1 只占用 import 命名空间，**不提供任何公共运行时实现**。
当前包内唯一存在的子包是 :mod:`agent_core.contracts`，它承载跨边界契约载荷。

本文件**不得** re-export 任何契约或运行时符号（FND-PKG-004）：
稳定的 import 必须显式写出完整路径，例如 ``agent_core.contracts.models.usage``。
"""
