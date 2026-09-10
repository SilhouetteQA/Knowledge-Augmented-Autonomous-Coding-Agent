"""Foundation 跨边界值对象。

本子包只放**跨边界 DTO**：它们可以在序列化、API、IPC、MCP、Adapter 或
artifact 边界之间传递。Agent 内部状态、领域模型、持久化对象都不属于这里。

约定：全部模型继承 :class:`agent_core.contracts.models.base.FoundationModel`，
默认 ``extra="forbid"``；声明 ``extensions`` 的模型自动获得 Extension Boundary 校验。
"""
