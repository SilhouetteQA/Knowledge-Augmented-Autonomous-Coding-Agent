"""Coding 项目本地的 Foundation Adapter 层。

与 Wiki 仓的同名子包**各自独立实现**（Spec 06：逻辑与共享契约一致，代码项目本地）；
共享的只有 `agent_core.contracts` 定义的模型、枚举、Protocol 与生成的 Schema。

子包构成：

```text
facts.py               presence-aware facts 提取（价格表由调用方注入，不 import 业务模块）
mapping.py             facts → Foundation DTO，边界处映射 ErrorEnvelope
runtime.py             Contract Mode 策略、证据组装、sink 注入、sidecar 生命周期
observation_ledger.py  client-local component provenance sidecar
evidence_sink.py       项目本地 FileEvidenceSink（原子写）
```

边界：本层不承载 Agent、Sandbox、GitHub 或审批逻辑，也不改变任何既有业务行为。
"""
