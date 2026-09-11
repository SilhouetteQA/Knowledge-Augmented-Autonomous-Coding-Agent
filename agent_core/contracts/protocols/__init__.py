"""共享能力边界。

本子包只放 **Protocol**：它们描述"符合实现必须能做什么"，不提供任何实现。
文件 I/O、目录策略、rotation 与 cleanup 属各项目本地实现，禁止进入共享 payload
（Spec 04 Forbidden Changes）。

约定与 :mod:`agent_core.contracts.models` 一致：``__init__`` 只写子包说明，
不 re-export，稳定 import 必须使用完整路径 ``agent_core.contracts.protocols.<module>``。
"""
