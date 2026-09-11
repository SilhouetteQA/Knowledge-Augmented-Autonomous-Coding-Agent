"""Coding 项目本地的 Foundation Adapter 层。

与 Wiki 仓的同名子包**各自独立实现**（Spec 04：共享 DTO / Protocol，不共享 I/O）；
共享的只有 `agent_core.contracts` 定义的模型、枚举、Protocol 与生成的 Schema。

本层只负责把项目事实搬进契约形状，不改变任何既有业务行为。
"""
