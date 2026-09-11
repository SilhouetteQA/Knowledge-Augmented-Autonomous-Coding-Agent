"""``FoundationObservation`` 与 ``EvidenceRecord``：证据的跨边界数据表示。

两者都是**跨边界 DTO**，不是业务返回类型：

```text
FoundationObservation ≠ 业务 Result
EvidenceRecord        ≠ 日志行、≠ 原始业务记录
```

字段与不变量逐字对应 Master Spec §4.6 与 Appendix D.4。规范要点：

- :class:`FoundationObservation` 的 ``usage`` / ``cost`` / ``cost_summary`` **至少一项存在**，
  但不构成业务结果，也不得替代仓库现有的返回契约。
- :class:`EvidenceRecord` 用 ``validation_status`` 表达结论，并强制：
  ``PASS → foundation_output 存在且 error_envelope 为 null``；
  ``FAIL → error_envelope 存在``（``foundation_output`` 可空）。
- 单条记录 canonical JSON UTF-8 **不超过 64 KiB**（EVD-DATA-001）。
- ``run_id`` 只允许 ``[A-Za-z0-9_-]+``；``event_id`` 为 canonical UUID 或 ULID；
  任一标识都**不得**携带 ``/``、``\\``、``..``、``:`` 参与路径构造。

fact key 采用与 Extension Boundary 相同的两层防线，但归属不同规则：

```text
key 形状 + 命名空间 + 键数上限 + 敏感 key 硬拒绝  → EVD-DATA-001
内容启发式命中                                    → 只告警并计数
```

值域与深度**复用** Extension Boundary 的唯一实现（:func:`_ensure_json_value`），
不另写一套 JSON 兼容性判定，以免出现两份"同一算法"。
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Mapping
from typing import Final

from pydantic import Field, field_validator, model_validator

from agent_core.contracts.enums.evidence import EvidenceRepository, ValidationStatus
from agent_core.contracts.enums.modes import ContractMode
from agent_core.contracts.models.base import (
    DOTTED_IDENTIFIER_PATTERN,
    FoundationModel,
    JsonValue,
    _ensure_json_value,
    _sensitive_reason,
    canonical_json_dumps,
)
from agent_core.contracts.models.cost import Cost, CostSummary
from agent_core.contracts.models.error import ErrorEnvelope
from agent_core.contracts.models.usage import Usage

# --------------------------------------------------------------------------- #
# 容量与形状常量
# --------------------------------------------------------------------------- #

#: 单条 EvidenceRecord canonical JSON UTF-8 字节上限（Master Spec §4.6 / D.4）。
EVIDENCE_RECORD_MAX_BYTES: Final[int] = 64 * 1024

#: ``sanitized_input_facts`` 的键数上限。
#: 与 64 KiB 总量上限共同构成容量边界，防止用海量微型键规避字节限制。
EVIDENCE_FACT_MAX_KEYS: Final[int] = 64

#: fact key 允许的首段。与 Extension Boundary 的项目命名空间一致；
#: ``foundation`` / ``shared`` 保留给 Foundation 自身，项目不得写入。
EVIDENCE_FACT_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"wiki", "coding", "provider", "adapter"}
)

#: 显式 run_id 的形状（Master Spec §4.6）。
RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")

#: canonical UUID（8-4-4-4-12 小写 hex）。
EVENT_ID_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: ULID（26 位 Crockford base32；字母表排除 I / L / O / U，大小写不敏感）。
EVENT_ID_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26}$"
)

#: 40 位小写 hex 提交号。
REPOSITORY_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")

#: ``sha256:`` + 64 位小写 hex。
PAYLOAD_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")

#: 锁步 Contract Set 版本。
CONTRACT_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

#: RFC3339，**必须带时区**（``Z`` 或 ±HH:MM 偏移）。
TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

#: ``producer_id`` / ``mapping_stage``：小写点分标识。
PRODUCER_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
)

#: 参与路径构造时必须拒绝的片段（Master Spec §4.6）。
PATH_UNSAFE_FRAGMENTS: Final[tuple[str, ...]] = ("/", "\\", "..", ":")

#: 只有这两种模式会产出证据：``off`` 不接入，也就不产生记录。
EVIDENCE_PRODUCING_MODES: Final[frozenset[ContractMode]] = frozenset(
    {ContractMode.OBSERVE, ContractMode.STRICT}
)


# --------------------------------------------------------------------------- #
# 路径安全谓词（契约层，供项目 sink 复用）
# --------------------------------------------------------------------------- #


def is_path_safe_identifier(value: object) -> bool:
    """判断标识是否可以安全参与路径构造。

    只做**路径安全**判定，不做字符集合法性判定（后者由 ``run_id`` /
    ``event_id`` 各自的形状规则负责）。以下情形返回 ``False``：

    ```text
    非字符串、空串、"." 与 ".."
    含 "/" 或 "\\"（目录分隔）
    含 ".."（上跳）
    含 ":"（Windows 驱动器与 NTFS 数据流）
    含控制字符
    ```

    与 ``is_safe_run_id`` 的分工：空格这类字符在这里是安全的（Windows 文件名允许），
    但会被 ``run_id`` 的 ``[A-Za-z0-9_-]+`` 规则拒绝。项目 sink 在拼路径前**必须**
    再调一次本函数，与模型层校验互为冗余。
    """
    if not isinstance(value, str) or not value:
        return False
    if value in {".", ".."}:
        return False
    if any(fragment in value for fragment in PATH_UNSAFE_FRAGMENTS):
        return False
    return all(character.isprintable() for character in value)


def is_canonical_event_id(value: object) -> bool:
    """判断是否为 canonical UUID 或 ULID（大小写不敏感）。"""
    if not isinstance(value, str):
        return False
    if EVENT_ID_UUID_PATTERN.match(value.lower()):
        return True
    return bool(EVENT_ID_ULID_PATTERN.match(value))


def is_safe_run_id(value: object) -> bool:
    """判断 run_id 形状合法且路径安全（EVD-RUN-001）。"""
    if not isinstance(value, str) or not RUN_ID_PATTERN.match(value):
        return False
    return is_path_safe_identifier(value)


def normalize_event_id(value: str) -> str:
    """把 event_id 规范化为小写形式。

    UUID 与 ULID 都统一存小写：UUID 的规范形式本就小写，ULID 的 Crockford 字母表
    大小写不敏感，统一小写可避免"同一事件两种拼写"导致 event_id 唯一性判断失效。
    """
    return value.lower()


# --------------------------------------------------------------------------- #
# fact key 校验
# --------------------------------------------------------------------------- #


class EvidenceFactsValidationError(ValueError):
    """``sanitized_input_facts`` 违反 EVD-DATA-001。"""

    rule_id: Final[str] = "EVD-DATA-001"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.rule_id}: {message}")
        self.message = message


def validate_evidence_facts(facts: Mapping[str, JsonValue]) -> None:
    """校验 ``sanitized_input_facts`` 的形状、命名空间、值域与键数。

    值域与深度复用 Extension Boundary 的唯一实现；规则归属改写为 EVD-DATA-001，
    避免把 Evidence 侧的失败误报成 ``FND-EXT-*``。
    """
    if not isinstance(facts, Mapping):
        raise EvidenceFactsValidationError(
            f"sanitized_input_facts 必须是映射，实际为 {type(facts).__name__}"
        )

    if len(facts) > EVIDENCE_FACT_MAX_KEYS:
        raise EvidenceFactsValidationError(
            f"fact 键数 {len(facts)} 超过上限 {EVIDENCE_FACT_MAX_KEYS}"
        )

    for key, value in facts.items():
        if not isinstance(key, str) or not DOTTED_IDENTIFIER_PATTERN.match(key):
            raise EvidenceFactsValidationError(
                f"fact key {key!r} 不符合 "
                r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
            )

        namespace = key.split(".", 1)[0]
        if namespace not in EVIDENCE_FACT_NAMESPACES:
            raise EvidenceFactsValidationError(
                f"fact key {key!r} 的首段 {namespace!r} 未注册；"
                f"允许的命名空间: {sorted(EVIDENCE_FACT_NAMESPACES)}"
            )

        reason = _sensitive_reason(key)
        if reason is not None:
            raise EvidenceFactsValidationError(f"fact key {key!r} 被拒绝：{reason}")

        try:
            _ensure_json_value(value, key, 1)
        except ValueError as exc:
            raise EvidenceFactsValidationError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# FoundationObservation
# --------------------------------------------------------------------------- #


class FoundationObservation(FoundationModel):
    """一次 Producer 观测到的 Foundation 事实集合。

    单次 producer 可能同时形成 Usage 与 Cost，因此这里是一个**显式结构**，
    而不是任意 ``dict``（Master Spec §4.6）。三者**至少一项必须存在**。

    它不是业务 ``Result``：不得作为业务函数返回值，也不得替代仓库现有返回契约。
    """

    usage: Usage | None = None
    cost: Cost | None = None
    cost_summary: CostSummary | None = None

    @model_validator(mode="after")
    def _require_at_least_one_observation(self) -> FoundationObservation:
        if self.usage is None and self.cost is None and self.cost_summary is None:
            raise ValueError(
                "FND-USAGE-001: FoundationObservation 至少需要 usage / cost / cost_summary "
                "之一；空观测不是合法的 zero"
            )
        return self


# --------------------------------------------------------------------------- #
# EvidenceRecord
# --------------------------------------------------------------------------- #


class EvidenceRecord(FoundationModel):
    """一条可校验、可发布的证据记录（Master Spec §4.6 / Appendix D.4）。

    字段集合是**封闭**的（``extra="forbid"``）：它对应一个显式 allowlist 形状，
    而不是"任意 dict 加若干约定键"。发布时只能由本形状构造，不允许从原始对象
    序列化后再删字段。
    """

    event_id: str
    run_id: str
    repository: EvidenceRepository
    repository_commit: str
    producer_id: str
    mapping_stage: str
    contract_mode: ContractMode
    contract_version: str
    contract_payload_hash: str
    timestamp: str
    validation_status: ValidationStatus
    sanitized_input_facts: dict[str, JsonValue] = Field(default_factory=dict)
    foundation_output: FoundationObservation | None = None
    error_envelope: ErrorEnvelope | None = None

    # -- 标识 ------------------------------------------------------------- #

    @field_validator("event_id", mode="after")
    @classmethod
    def _validate_event_id(cls, value: str) -> str:
        if not is_canonical_event_id(value):
            raise ValueError(
                "EVD-DATA-001: event_id 必须是 canonical UUID（8-4-4-4-12）或 "
                f"ULID（26 位 Crockford base32），实际为 {value!r}"
            )
        return normalize_event_id(value)

    @field_validator("run_id", mode="after")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        if not isinstance(value, str) or not RUN_ID_PATTERN.match(value):
            raise ValueError(
                "EVD-RUN-001: run_id 只允许 [A-Za-z0-9_-]+，实际为 "
                f"{value!r}"
            )
        if not is_path_safe_identifier(value):
            raise ValueError(
                f"EVD-RUN-001: run_id {value!r} 不能安全参与路径构造"
            )
        return value

    @field_validator("repository_commit", mode="after")
    @classmethod
    def _validate_repository_commit(cls, value: str) -> str:
        if not isinstance(value, str) or not REPOSITORY_COMMIT_PATTERN.match(value):
            raise ValueError(
                "EVD-RUN-002: repository_commit 必须是 40 位小写 hex，实际为 "
                f"{value!r}"
            )
        return value

    @field_validator("producer_id", "mapping_stage", mode="after")
    @classmethod
    def _validate_producer_identifier(cls, value: str) -> str:
        if not isinstance(value, str) or not PRODUCER_IDENTIFIER_PATTERN.match(value):
            raise ValueError(
                "EVD-DATA-001: producer_id / mapping_stage 必须是小写点分标识，实际为 "
                f"{value!r}"
            )
        return value

    # -- 契约绑定 --------------------------------------------------------- #

    @field_validator("contract_mode", mode="after")
    @classmethod
    def _validate_contract_mode(cls, value: ContractMode) -> ContractMode:
        if value not in EVIDENCE_PRODUCING_MODES:
            raise ValueError(
                "EVD-DATA-001: 只有 observe / strict 模式产出证据，"
                f"实际为 {value!r}"
            )
        return value

    @field_validator("contract_version", mode="after")
    @classmethod
    def _validate_contract_version(cls, value: str) -> str:
        if not isinstance(value, str) or not CONTRACT_VERSION_PATTERN.match(value):
            raise ValueError(
                "EVD-RUN-002: contract_version 必须是锁步 Contract Set 版本，实际为 "
                f"{value!r}"
            )
        return value

    @field_validator("contract_payload_hash", mode="after")
    @classmethod
    def _validate_contract_payload_hash(cls, value: str) -> str:
        if not isinstance(value, str) or not PAYLOAD_HASH_PATTERN.match(value):
            raise ValueError(
                "EVD-RUN-002: contract_payload_hash 必须是 sha256: + 64 位小写 hex，"
                f"实际为 {value!r}"
            )
        return value

    @field_validator("timestamp", mode="after")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        if not isinstance(value, str) or not TIMESTAMP_PATTERN.match(value):
            raise ValueError(
                f"EVD-DATA-001: timestamp 必须是带时区的 RFC3339，实际为 {value!r}"
            )
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:  # pragma: no cover - 正则已挡住绝大多数非法值
            raise ValueError(f"EVD-DATA-001: timestamp 不是合法时间：{value!r}") from exc
        if parsed.tzinfo is None:  # pragma: no cover - 正则强制带偏移
            raise ValueError(f"EVD-DATA-001: timestamp 必须带时区：{value!r}")
        return value

    # -- facts ------------------------------------------------------------ #

    @field_validator("sanitized_input_facts", mode="before")
    @classmethod
    def _validate_facts_field(
        cls, value: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        """在 pydantic 强制转换**之前**校验 facts。

        与 Extension Boundary 同理：``dict[str, JsonValue]`` 在 lax 模式下会把
        ``Decimal`` 之类非法值强转成 ``float``，等到 ``mode="after"`` 时原始类型
        已经丢失，JSON 值域就拦不住。
        """
        validate_evidence_facts(value)
        return value

    # -- 结论不变量与容量 -------------------------------------------------- #

    @model_validator(mode="after")
    def _validate_status_invariant(self) -> EvidenceRecord:
        if self.validation_status is ValidationStatus.PASS:
            if self.foundation_output is None:
                raise ValueError(
                    "EVD-DATA-001: validation_status=PASS 要求 foundation_output 存在"
                )
            if self.error_envelope is not None:
                raise ValueError(
                    "EVD-DATA-001: validation_status=PASS 要求 error_envelope 为 null"
                )
        elif self.error_envelope is None:
            raise ValueError(
                "EVD-DATA-001: validation_status=FAIL 要求 error_envelope 存在"
            )
        return self

    @model_validator(mode="after")
    def _validate_record_size(self) -> EvidenceRecord:
        encoded = canonical_json_dumps(self.model_dump(mode="json")).encode("utf-8")
        if len(encoded) > EVIDENCE_RECORD_MAX_BYTES:
            raise ValueError(
                f"EVD-DATA-001: 单条 EvidenceRecord canonical JSON 为 {len(encoded)} 字节，"
                f"超过上限 {EVIDENCE_RECORD_MAX_BYTES} 字节"
            )
        return self


__all__ = [
    "EVIDENCE_RECORD_MAX_BYTES",
    "EVIDENCE_FACT_MAX_KEYS",
    "EVIDENCE_FACT_NAMESPACES",
    "RUN_ID_PATTERN",
    "EVENT_ID_UUID_PATTERN",
    "EVENT_ID_ULID_PATTERN",
    "REPOSITORY_COMMIT_PATTERN",
    "PAYLOAD_HASH_PATTERN",
    "CONTRACT_VERSION_PATTERN",
    "TIMESTAMP_PATTERN",
    "PRODUCER_IDENTIFIER_PATTERN",
    "PATH_UNSAFE_FRAGMENTS",
    "EVIDENCE_PRODUCING_MODES",
    "EvidenceFactsValidationError",
    "FoundationObservation",
    "EvidenceRecord",
    "is_path_safe_identifier",
    "is_canonical_event_id",
    "is_safe_run_id",
    "normalize_event_id",
    "validate_evidence_facts",
]
