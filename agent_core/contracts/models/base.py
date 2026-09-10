"""Foundation 契约的统一基类与 Extension Boundary 实现。

本模块承载两类规范职责：

1. **统一模型配置**：所有 Foundation 模型默认 ``extra="forbid"``（Master Spec §4），
   禁止任意额外字段。
2. **Extension Boundary**：``extensions`` 是受限逃生口，不是第二套自由 Schema
   （Master Spec §5，FND-EXT-001 至 FND-EXT-006）。

Capacity 与深度的口径（与 Master Spec §5 逐字对应）：

```text
extensions 根映射不计层；
extension value 的第一层 container 记作 depth 1；最大 depth 为 4；
对 extensions 做 canonical JSON UTF-8 序列化后不得超过 16 KiB。
```

敏感内容采用两层防线（FND-EXT-005）：

- **敏感 key 名单 → 硬拒绝**。生产者不得把 raw prompt / response / reasoning /
  traceback / 凭据写进 extension。
- **内容启发式 → 只告警 + 计数**。scanner 只是辅助防线，生产者承担首要责任，
  因此内容命中不阻断合法扩展。

canonical JSON 口径与 Master Spec §12.3 一致；Spec 03 的 payload 工具链复用同一实现，
避免出现第二套规范化。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# JSON 值域
# --------------------------------------------------------------------------- #

# 使用 PEP 695 具名递归类型别名：pydantic 需要具名递归类型才能构建 Schema
# （隐式递归别名会触发 RecursionError）。JsonValue 也是 Extension Boundary 的
# 值域定义（FND-EXT-002）：递归 JSON，禁止 Decimal / datetime / Path / bytes 等。
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_JSON_SCALAR_TYPES: Final[tuple[type, ...]] = (str, int, float, bool, type(None))

# --------------------------------------------------------------------------- #
# Extension Boundary 常量
# --------------------------------------------------------------------------- #

#: 合法 key 格式：小写字母开头，点分多段（Master Spec §5）。
DOTTED_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
)

#: 项目 Adapter 可写的第一段。
EXTENSION_PROJECT_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"wiki", "coding", "provider", "adapter"}
)

#: 为 Foundation 自身保留的第一段；项目 Adapter **禁止**写入。
EXTENSION_RESERVED_NAMESPACES: Final[frozenset[str]] = frozenset({"foundation", "shared"})

#: 全部已注册第一段。
EXTENSION_KNOWN_NAMESPACES: Final[frozenset[str]] = (
    EXTENSION_PROJECT_NAMESPACES | EXTENSION_RESERVED_NAMESPACES
)

#: canonical JSON UTF-8 后的容量上限。
EXTENSIONS_MAX_BYTES: Final[int] = 16 * 1024

#: extension value 的最大 container 深度（根映射不计层）。
EXTENSIONS_MAX_DEPTH: Final[int] = 4

# --------------------------------------------------------------------------- #
# 敏感内容
# --------------------------------------------------------------------------- #

#: 命中即**拒绝**的敏感 key（按点 / 下划线 / 短横切段后逐段比对）。
SENSITIVE_KEY_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "raw_prompt",
        "prompt_text",
        "response",
        "raw_response",
        "response_body",
        "response_text",
        "reasoning",
        "reasoning_content",
        "chain_of_thought",
        "traceback",
        "stack_trace",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "bearer_token",
        "private_key",
        "credential",
        "credentials",
    }
)

#: 命中即**拒绝**的敏感 key 后缀（覆盖 ``<prefix>_prompt`` 这类变体）。
SENSITIVE_KEY_SUFFIXES: Final[tuple[str, ...]] = (
    "_prompt",
    "_secret",
    "_password",
    "_token",
    "_api_key",
    "_private_key",
    "_credential",
    "_traceback",
)

#: 按 ``_`` 切 token 后命中即拒绝的敏感 token（**不含** prompt / response，
#: 它们另走下面的"内容前缀 + 指标豁免"判定，避免误伤 ``prompt_tokens``）。
SENSITIVE_UNDERSCORE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "apikey",
        "traceback",
        "reasoning",
    }
)

#: 承载原始内容的 token 前缀。
SENSITIVE_CONTENT_TOKEN_PREFIXES: Final[frozenset[str]] = frozenset({"prompt", "response"})

#: 紧跟内容前缀时表示"指标名"而非内容的 token；此时不判定为敏感。
SENSITIVE_METRIC_SUFFIX_TOKENS: Final[frozenset[str]] = frozenset(
    {"token", "tokens", "count", "length", "chars", "size", "bytes"}
)

#: 内容启发式：**只告警**，不拒绝。
CONTENT_HEURISTICS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("windows_absolute_path", re.compile(r"[A-Za-z]:[\\/](?:[^\\/\s\"']+[\\/])+")),
    ("posix_absolute_path", re.compile(r"(?<![\w.])/(?:Users|home|usr|etc|var|opt|root|tmp|mnt)/")),
    ("bearer_credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")),
    ("pem_block", re.compile(r"-----BEGIN [A-Z ]+-----")),
    ("long_opaque_token", re.compile(r"(?=[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-]))(?=[^\s]*[0-9])[A-Za-z0-9+/=_-]{40,}")),
)

# --------------------------------------------------------------------------- #
# canonical JSON（与 Master Spec §12.3 同一口径）
# --------------------------------------------------------------------------- #


def canonical_json_dumps(value: object) -> str:
    """按契约口径序列化为 canonical JSON 文本。

    ``sort_keys=True``、compact separators、``ensure_ascii=False``；
    编码为 UTF-8 时固定不带 BOM。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 校验异常
# --------------------------------------------------------------------------- #


class ExtensionValidationError(ValueError):
    """``extensions`` 违反 Extension Boundary 规则。

    ``rule_id`` 是规范规则标识，便于 Adapter 在边界映射为合适的
    :class:`ErrorEnvelope`（本层不产生 Envelope，也不定义错误码映射）。
    """

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(f"{rule_id}: {message}")
        self.rule_id = rule_id
        self.message = message


# --------------------------------------------------------------------------- #
# 结构校验
# --------------------------------------------------------------------------- #


def _ensure_json_value(value: object, path: str, depth: int) -> None:
    """递归校验 JSON 兼容性与 container 深度（FND-EXT-002 / FND-EXT-004）。"""
    if isinstance(value, _JSON_SCALAR_TYPES):
        return

    if depth > EXTENSIONS_MAX_DEPTH:
        raise ExtensionValidationError(
            "FND-EXT-004",
            f"{path} 的 container 深度 {depth} 超过上限 {EXTENSIONS_MAX_DEPTH}",
        )

    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_value(item, f"{path}[{index}]", depth + 1)
        return

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExtensionValidationError(
                    "FND-EXT-002", f"{path} 的映射键必须是字符串，实际为 {type(key).__name__}"
                )
            _ensure_json_value(item, f"{path}.{key}", depth + 1)
        return

    raise ExtensionValidationError(
        "FND-EXT-002",
        f"{path} 不是 JSON 兼容值（类型 {type(value).__name__}）；"
        "禁止 Decimal / datetime / Path / bytes / Exception / Pydantic 实例 / dataclass",
    )


def _container_depth(value: object) -> int:
    """返回 extension value 的 container 深度（标量记 0，根映射不计层）。"""
    if isinstance(value, list):
        if not value:
            return 1
        return 1 + max(_container_depth(item) for item in value)

    if isinstance(value, Mapping):
        if not value:
            return 1
        return 1 + max(_container_depth(item) for item in value.values())

    return 0


def _sensitive_reason(key: str) -> str | None:
    """返回 key 命中敏感名单的原因；未命中返回 ``None``。

    判定顺序（三级，全部针对 key 名，不涉及值）：

    1. **点段精确命中** —— 任一点分段整体等于敏感名（如 ``adapter.raw_prompt``）。
    2. **后缀命中** —— 整条 key 以敏感后缀结尾（如 ``adapter.foo_password``）。
    3. **下划线 token 判定** —— 末段按 ``_`` 切 token：
       命中 :data:`SENSITIVE_UNDERSCORE_TOKENS` 直接拒绝；
       ``prompt`` / ``response`` 这类**内容前缀**只有在紧跟
       :data:`SENSITIVE_METRIC_SUFFIX_TOKENS`（token / count / chars …）
       时才豁免，因此 ``prompt_tokens`` 合法而 ``system_prompt_text`` 被拒。
    """
    lowered = key.lower()
    parts = lowered.split(".")

    for part in parts:
        if part in SENSITIVE_KEY_SEGMENTS:
            return f"点段 {part!r} 属于敏感字段名"

    for suffix in SENSITIVE_KEY_SUFFIXES:
        if lowered.endswith(suffix):
            return f"key 后缀 {suffix!r} 属于敏感字段名"

    tokens = parts[-1].split("_")
    for index, token in enumerate(tokens):
        if token in SENSITIVE_UNDERSCORE_TOKENS:
            return f"token {token!r} 属于敏感字段名"
        if token in SENSITIVE_CONTENT_TOKEN_PREFIXES:
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following in SENSITIVE_METRIC_SUFFIX_TOKENS:
                continue
            return f"token {token!r} 指向原始内容而非指标"

    return None


def validate_extensions(extensions: Mapping[str, JsonValue]) -> None:
    """校验 ``extensions`` 的结构、命名空间、容量、深度与敏感 key。

    任一违规抛出 :class:`ExtensionValidationError`。
    """
    if not isinstance(extensions, Mapping):
        raise ExtensionValidationError(
            "FND-EXT-002", f"extensions 必须是映射，实际为 {type(extensions).__name__}"
        )

    for key, value in extensions.items():
        if not isinstance(key, str) or not DOTTED_IDENTIFIER_PATTERN.match(key):
            raise ExtensionValidationError(
                "FND-EXT-001",
                f"extension key {key!r} 不符合 "
                r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
            )

        namespace = key.split(".", 1)[0]
        if namespace not in EXTENSION_KNOWN_NAMESPACES:
            raise ExtensionValidationError(
                "FND-EXT-001",
                f"extension key {key!r} 的首段 {namespace!r} 未注册；"
                f"已知命名空间: {sorted(EXTENSION_KNOWN_NAMESPACES)}",
            )

        reason = _sensitive_reason(key)
        if reason is not None:
            raise ExtensionValidationError(
                "FND-EXT-005", f"extension key {key!r} 被拒绝：{reason}"
            )

        _ensure_json_value(value, key, 1)
        actual_depth = _container_depth(value)
        if actual_depth > EXTENSIONS_MAX_DEPTH:
            raise ExtensionValidationError(
                "FND-EXT-004",
                f"extension {key!r} 的 container 深度 {actual_depth} "
                f"超过上限 {EXTENSIONS_MAX_DEPTH}",
            )

    encoded = canonical_json_dumps(extensions).encode("utf-8")
    if len(encoded) > EXTENSIONS_MAX_BYTES:
        raise ExtensionValidationError(
            "FND-EXT-003",
            f"canonical extensions 为 {len(encoded)} 字节，"
            f"超过上限 {EXTENSIONS_MAX_BYTES} 字节",
        )


# --------------------------------------------------------------------------- #
# 内容启发式扫描（只告警）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContentScanReport:
    """best-effort 内容扫描结果；**不构成拒绝依据**，只用于告警与趋势审计。"""

    findings: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.findings)

    def __bool__(self) -> bool:  # pragma: no cover - 语义糖
        return bool(self.findings)


_content_scan_warning_total = 0


def content_scan_warning_total() -> int:
    """进程内累计的内容启发式命中次数（用于可观测性与趋势审计）。"""
    return _content_scan_warning_total


def reset_content_scan_warning_total() -> None:
    """重置计数器；仅供测试与受控运行使用。"""
    global _content_scan_warning_total
    _content_scan_warning_total = 0


def _iter_strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_strings(item, f"{path}.{key}")


def scan_extension_content(extensions: Mapping[str, JsonValue]) -> ContentScanReport:
    """对 extension 的字符串值做 best-effort 敏感内容启发式扫描。"""
    findings: list[str] = []
    for key, value in extensions.items():
        for path, text in _iter_strings(value, key):
            for name, pattern in CONTENT_HEURISTICS:
                if pattern.search(text):
                    findings.append(f"{path}: {name}")
    return ContentScanReport(findings=tuple(findings))


def _record_content_scan(extensions: Mapping[str, JsonValue]) -> None:
    global _content_scan_warning_total
    report = scan_extension_content(extensions)
    if not report:
        return
    _content_scan_warning_total += report.count
    logger.warning(
        "extension 内容启发式命中 %d 处（仅告警，不拒绝）；生产者应避免写入敏感内容：%s",
        report.count,
        "; ".join(report.findings),
    )


# --------------------------------------------------------------------------- #
# 统一基类
# --------------------------------------------------------------------------- #


class FoundationModel(BaseModel):
    """全部 Foundation 跨边界 DTO 的基类。

    - 默认 ``extra="forbid"``（Master Spec §4）。
    - 声明了 ``extensions`` 字段的子类自动获得 Extension Boundary 校验。
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("extensions", mode="before", check_fields=False)
    @classmethod
    def _validate_extensions_field(
        cls, value: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        """在**pydantic 强制转换之前**校验 extensions，然后原样放行。

        必须是 ``mode="before"``：``dict[str, JsonValue]`` 在 lax 模式下会把
        ``Decimal`` 之类非法值强转成 ``float``，等到 ``mode="after"`` 时
        原始类型已经丢失，FND-EXT-002 就无法真正拒绝它们。
        """
        validate_extensions(value)
        _record_content_scan(value)
        return value


__all__ = [
    "JsonScalar",
    "JsonValue",
    "DOTTED_IDENTIFIER_PATTERN",
    "EXTENSION_PROJECT_NAMESPACES",
    "EXTENSION_RESERVED_NAMESPACES",
    "EXTENSION_KNOWN_NAMESPACES",
    "EXTENSIONS_MAX_BYTES",
    "EXTENSIONS_MAX_DEPTH",
    "SENSITIVE_KEY_SEGMENTS",
    "SENSITIVE_KEY_SUFFIXES",
    "SENSITIVE_UNDERSCORE_TOKENS",
    "SENSITIVE_CONTENT_TOKEN_PREFIXES",
    "SENSITIVE_METRIC_SUFFIX_TOKENS",
    "CONTENT_HEURISTICS",
    "ContentScanReport",
    "ExtensionValidationError",
    "FoundationModel",
    "canonical_json_dumps",
    "validate_extensions",
    "scan_extension_content",
    "content_scan_warning_total",
    "reset_content_scan_warning_total",
]
