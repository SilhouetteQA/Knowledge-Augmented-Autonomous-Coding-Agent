"""Foundation Contract 载荷根包。

本包是 Phase 1 的 canonical contract payload 根，内容由 Wiki 仓单独撰写，
再通过受控 bundle 逐字节镜像到 Coding 仓（见 Spec 03）。

约定：

- 本文件只做包标识，不做符号聚合；禁止 ``from .models.usage import Usage`` 之类的
  convenience re-export，避免出现第二套公共导入面。
- 调用方一律使用完整路径导入，例如
  ``from agent_core.contracts.models.cost import Cost``。
"""
