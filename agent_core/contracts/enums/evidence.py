"""Evidence 值域与基础设施诊断码。

本模块只放**值域封闭**的枚举与诊断码常量，不含任何 I/O、目录策略或项目配置
（Spec 04 §Allowed Changes；属共享 DTO 层）。

三类常量：

1. 记录层枚举 —— 证据的仓库归属、校验状态（Master Spec §4.6）。
2. 发布层枚举 —— 可发布 artifact 种类与证据状态（Master Spec §11.3、§11.5）。
3. ``evidence.*`` **基础设施诊断码** —— sink 写入失败等运行时诊断，
   **不是** 新的 :class:`~agent_core.contracts.enums.errors.ErrorCategory`，
   也**不属于** ``foundation.*`` 语义错误（FND-ERR 系列不出现在这里）。

段位边界（与 ``enums/errors.py`` 的模块 docstring 一致）：

```text
foundation.*  Foundation 语义错误，进入 ErrorEnvelope
evidence.*    证据基础设施诊断，只进结构化应用日志与内存 failure counter
```
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final


class EvidenceRepository(StrEnum):
    """EvidenceRecord 的仓库归属（Master Spec §4.6）。"""

    WIKI = "wiki"
    CODING = "coding"


class ValidationStatus(StrEnum):
    """单条证据的校验结论（Master Spec §4.6 / Appendix D.4）。

    状态不变量：

    ```text
    PASS → foundation_output 存在 AND error_envelope 为 null
    FAIL → error_envelope 存在；foundation_output 可空
    ```
    """

    PASS = "PASS"
    FAIL = "FAIL"


class EvidenceArtifactKind(StrEnum):
    """允许进入 Evidence Publication Commit B 的 artifact（Master Spec §11.3）。

    该清单是 **allowlist extraction** 的声明面：发布只能由显式 allowlist 构造，
    不得"序列化原始对象后删除已知敏感字段"（EVD-PUB-001）。

    最终 ``cycle-report.json`` / ``cycle-report.md`` 只在 Wiki Finalization C 写入，
    因此**不在**本清单内。
    """

    RUN_MANIFEST = "run-manifest.json"
    CONTRACT_MANIFEST = "contract-manifest.json"
    EVIDENCE_MANIFEST = "evidence-manifest.json"
    VALIDATION_REPORT = "validation-report.md"
    RULE_TRACEABILITY = "rule-traceability.json"
    SANITIZED_REPLAY_CORPUS = "sanitized-replay-corpus.jsonl"


class EvidenceStatus(StrEnum):
    """证据结论状态（Master Spec §11.5）。

    ``REPRODUCTION_RESTRICTED`` 表示结论有真实证据支持，但完整复现依赖不可发布数据
    或受限环境；它**不是** FAIL，也不能伪报为完全可复现（EVD-PUB-005）。
    """

    VERIFIED = "VERIFIED"
    PARTIALLY_OBSERVED = "PARTIALLY_OBSERVED"
    NOT_OBSERVED = "NOT_OBSERVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    LEGACY_DATA_INSUFFICIENT = "LEGACY_DATA_INSUFFICIENT"
    REPRODUCIBLE = "REPRODUCIBLE"
    PARTIALLY_REPRODUCIBLE = "PARTIALLY_REPRODUCIBLE"
    REPRODUCTION_RESTRICTED = "REPRODUCTION_RESTRICTED"
    CONFLICT = "CONFLICT"
    FAIL = "FAIL"


# --------------------------------------------------------------------------- #
# evidence.* 基础设施诊断码
# --------------------------------------------------------------------------- #

#: sink 无法持久化证据（磁盘、权限、注入的 I/O 失败）。
EVIDENCE_SINK_WRITE_FAILED: Final[str] = "evidence.sink_write_failed"

#: 显式 run_id 缺失或不安全（含路径分隔符、上跳、空值）。
EVIDENCE_INVALID_RUN_ID: Final[str] = "evidence.invalid_run_id"

#: 证据 artifact 不可用（缺失、不可读、或只有未完成的过程态文件）。
EVIDENCE_ARTIFACT_UNAVAILABLE: Final[str] = "evidence.artifact_unavailable"

#: v0.1 闭集：sink 失败只能使用这三个诊断码（Master Spec Appendix D.4）。
EVIDENCE_INFRASTRUCTURE_CODES: Final[frozenset[str]] = frozenset(
    {
        EVIDENCE_SINK_WRITE_FAILED,
        EVIDENCE_INVALID_RUN_ID,
        EVIDENCE_ARTIFACT_UNAVAILABLE,
    }
)

__all__ = [
    "EvidenceRepository",
    "ValidationStatus",
    "EvidenceArtifactKind",
    "EvidenceStatus",
    "EVIDENCE_SINK_WRITE_FAILED",
    "EVIDENCE_INVALID_RUN_ID",
    "EVIDENCE_ARTIFACT_UNAVAILABLE",
    "EVIDENCE_INFRASTRUCTURE_CODES",
]
