"""共享一致性测试（shared conformance suite）。

本子包由 **两个仓库共同执行**，用于证明规范性载体之间语义一致。

硬性约束（Spec 09）：

- 共享测试**禁止** import ``arknights_wiki`` / ``agent`` / ``benchmark`` / ``tools``
  等任何项目模块；它们只接收 model / factory / Protocol 实现。
- 测试函数名只是人类可读信息，不能代替规范规则标识；每条规范性规则必须通过
  :func:`agent_core.contracts.conformance.rules.contract_rule` 绑定 Rule ID。
"""
