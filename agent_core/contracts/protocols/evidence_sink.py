"""``EvidenceSink`` Protocol 与失败上报形状。

Protocol 只定义**行为边界**，不定义目录、命名、rotation 或 cleanup
（Master Spec §4.7；Spec 04 Required Invariants）：

```text
Shared DTO / Protocol != shared I/O implementation
Sink failure != business failure
Sink failure = evidence gate failure
partial .tmp != valid evidence
```

失败语义（Master Spec Appendix D.4）：

- 符合实现**必须**接收合法记录；无法持久化时**显式报告失败**，不得静默丢弃
  （EVD-SINK-001）。
- 失败只允许使用 ``evidence.*`` 基础设施诊断码写结构化应用日志与内存 counter，
  **不得**污染 Foundation semantic error（``ErrorEnvelope`` / ``foundation.*``）。
  因此 :class:`SinkFailure` 与 ``ErrorEnvelope`` 是互不相关的类型。
- 失败时**禁止**递归地再次调用同一个 sink。
"""
from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from agent_core.contracts.enums.evidence import EVIDENCE_INFRASTRUCTURE_CODES
from agent_core.contracts.models.evidence import EvidenceRecord


class SinkFailure(RuntimeError):
    """证据无法持久化时抛出的**基础设施**异常。

    它不是 :class:`~agent_core.contracts.models.error.ErrorEnvelope` 的替代品，
    也不与任何 ``foundation.*`` 语义错误互相继承：sink 失败只让该 run 的证据失效
    （EVD-SINK-003），在 observe 模式下**不得**改变业务返回（EVD-SINK-002）。
    """

    def __init__(self, code: str, message: str, *, event_id: str | None = None) -> None:
        if code not in EVIDENCE_INFRASTRUCTURE_CODES:
            raise ValueError(
                f"非法的 evidence 基础设施码 {code!r}；"
                f"允许集合: {sorted(EVIDENCE_INFRASTRUCTURE_CODES)}"
            )
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.event_id = event_id


@runtime_checkable
class EvidenceSink(Protocol):
    """证据持久化能力的**窄接口**。

    只要求一件事：接受一条合法记录，并在持久化失败时抛出 :class:`SinkFailure`。

    刻意不定义的部分（留给项目本地实现，且各仓独立）：

    ```text
    目录布局、文件命名、staging 根、rotation、cleanup、retention
    ```

    刻意不要求的返回值：``emit`` 返回 ``None``。证据是否成功写入由"异常与否"
    表达，调用方不得依赖返回值判断；需要计数时使用 sink 自身的 failure counter。
    """

    def emit(self, record: EvidenceRecord) -> None:
        """持久化一条记录。

        :raises SinkFailure: 无法持久化。实现必须已把结构化诊断写入应用日志并
            递增内存 failure counter；不得静默丢弃记录。
        """
        ...


#: Protocol 只暴露 ``emit``（用于一致性测试断言接口宽度）。
EVIDENCE_SINK_METHODS: Final[tuple[str, ...]] = ("emit",)

__all__ = ["EvidenceSink", "SinkFailure", "EVIDENCE_SINK_METHODS"]
