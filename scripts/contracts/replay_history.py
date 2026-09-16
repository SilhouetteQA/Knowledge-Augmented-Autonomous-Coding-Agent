"""L2 historical replay 与 sanitized replay corpus（Spec 10 交付物 / Spec 12 工具面）。

母 Spec C.1（唯一写死的 CLI）::

    python scripts/contracts/replay_history.py --run-manifest config/contracts/replay-v0.1.json

前置（母 Spec C.1:2378-2380、Spec 12:45）::

    AGENT_CONTRACT_MODE = strict
    AGENT_CONTRACT_RUN_ID = <显式 safe run id，必须与 manifest.run_id 一致>

本脚本是 **工具**，不是契约实现：它只读已存在的历史产物，不修改任何 Adapter /
Producer / Schema / config / test。规范要求逐条对应：

```text
Spec12:35-40  只读 allowlist 内的历史来源；每条记录 source domain / runtime adapter
              status / evidence role；allowlist extraction（不做"先全量序列化再删字段"）
Spec12:46     sanitizer 不得执行 contract mapping  ← 本文件中 sanitize_* 不触碰 adapter
Spec12:47-48  用 Candidate A Adapter 得到 actual mapping，与预登记 expected mapping
              比较，逐 Rule/Semantic case 统计真实状态
Spec12:49     corpus 每条只含 record_id / source_class / 最小 legacy facts /
              expected / actual / sanitized record hash
Spec12:50     secret / path / forbidden-field / size / Schema 扫描
Spec12:51     raw 业务证据留在受控位置，不复制进 contract staging 或 Git
```

------------------------------------------------------------------------------
provisional 决策（G-01/G-02/G-14/G-18，已获用户批准；见 output/
spec10-normative-extraction-report.md）
------------------------------------------------------------------------------

* **G-02（Provisional）** `config/contracts/replay-v0.1.json` 的字段结构在规范中
  **完全缺失**（Spec12:35-40 只描述来源选择原则）。本实现使用"每个键都能追溯到文档
  概念"的最小 schema：`manifest_version / run_id / contract_mode / contract_version /
  payload_hash / repository_commit / output_dir / sources[] / max_records /
  reproduction_restriction`，`sources[]` 键为
  `source_id / source_class / path / producer_id / mapping_stage /
  runtime_adapter_status / evidence_role`。这些概念分别来自 Spec12:35-40、
  §11.1、§13.2、Appendix B:2354。**不改契约语义、不新增公共 artifact 形状**。
* **G-14（Provisional 收敛）** "预登记 expected mapping" 的承载位置规范未定义。
  本实现**不新增 manifest 键**，而是把 `config/contracts/producer-registry.json`
  中已登记的 `producer_id / mapping_stage / foundation_objects /
  evidence_requirement` 直接当作预登记 expected mapping（registry 是 FND-REG-*
  的受控 artifact）。actual mapping 由 Adapter 在 sanitized facts 上产生，两者比较。
* **G-18（Provisional）** 退出码在规范中未定义。全项目统一为
  `0 = 通过`、`1 = 校验/扫描失败`、`2 = 用法或配置错误`。
* `DEFERRED` 来源允许参与 replay，但记录必须携带
  `runtime_adapter_status="DEFERRED"` 与 `evidence_role="historical_replay_only"`
  （Appendix B:2354），且**不得**被解释为 runtime wiring（Spec12:85）。
* corpus 记录的形状是严格 allowlist（EVD-PUB-004）：除必需键外不写入任何内容。
  分类状态折叠在 `actual_mapping.status`，DEFERRED 标注作为顶层两个必需键存在；
  这样既满足 Spec12:49 的"每条只含"约束，又承载 Spec12:48 的状态语义。
* `reproduction_restriction` 是 run 级声明（Spec12:73-77、EVD-PUB-005）。当其为
  `REPRODUCTION_RESTRICTED` 时，可映射记录的状态被标为
  `REPRODUCTION_RESTRICTED`，但底层 `mapping_outcome`
  （`OBSERVED` / `NOT_OBSERVED` / `LEGACY_DATA_INSUFFICIENT`）仍然保留，避免用
  受限标签掩盖真实结论。
* `repository_commit` 在 manifest 中可以（也应当）是 `null`：§13.3 的预登记字段清单
  里**没有** `repository_commit`，运行 commit 属"运行后记录"；A 冻结前也无法知道
  Candidate SHA。`null` 的语义是"运行期由 `AGENT_CONTRACT_COMMIT` 解析"，脚本
  **不自动推断** HEAD 代替它（与 `validate_local.py` 的同一约定对齐）。

本仓（coding）差异（Spec10:39，项目文件不进 Payload，故两仓脚本允许不同）：

* Adapter 位于 ``adapters/foundation/{facts,mapping}.py``（本仓没有 ``arknights_wiki``
  包，也没有 Wiki 的 ``pricing.json`` 快照；价格表是 ``benchmark/runner.py`` 的
  ``MODEL_PRICE_USD_PER_1K``，v0.1 当前为空表）。
* 受控历史来源目录是 ``benchmark/cases``（benchmark artifact）。
* registry 的 producer_id 为 ``coding.*``。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------- #
# sys.path 自举
# --------------------------------------------------------------------------- #
# 以 `python scripts/contracts/replay_history.py` 方式直跑时 sys.path[0] 是脚本所在
# 目录（scripts/contracts），仓库根不在其中；只有 `python -m` 才会加入 CWD。
# 因此必须在任何项目 import 之前把仓库根插入 sys.path。

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
REPO_ROOT: Final[Path] = _REPO_ROOT

from agent_core.contracts.enums.modes import (  # noqa: E402
    CONTRACT_MODE_ENV,
    ContractMode,
    parse_contract_mode,
)
from agent_core.contracts.models.base import canonical_json_dumps  # noqa: E402
from agent_core.contracts.models.evidence import (  # noqa: E402
    EVIDENCE_RECORD_MAX_BYTES,
    PAYLOAD_HASH_PATTERN,
    REPOSITORY_COMMIT_PATTERN,
    is_safe_run_id,
)
from agent_core.contracts.tooling.canonical_json import (  # noqa: E402
    canonical_file_bundle_digest,
    sha256_hex,
)

# --------------------------------------------------------------------------- #
# 退出码与常量
# --------------------------------------------------------------------------- #

EXIT_OK: Final[int] = 0
EXIT_VALIDATION_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2

#: 别名：manifest / 用法 / 配置错误（provisional，G-18）。
EXIT_CONFIG_ERROR: Final[int] = EXIT_USAGE

#: 显式 run id 环境变量（母 Spec §11.1）。
RUN_ID_ENV: Final[str] = "AGENT_CONTRACT_RUN_ID"

#: 运行期 commit 环境变量（§11.1）。manifest `repository_commit: null` 时由它解析。
COMMIT_ENV: Final[str] = "AGENT_CONTRACT_COMMIT"

REPOSITORY: Final[str] = "coding"

PRODUCER_REGISTRY_RELPATH: Final[Path] = Path("config/contracts") / "producer-registry.json"

#: 缺省 staging 根（§11.1:1167）；manifest 的 `output_dir` 必须指向它。
DEFAULT_OUTPUT_DIR: Final[str] = "output/contract-validation/staging"

#: 禁止写入的目录（Master §11.1、Spec12:51）。
FORBIDDEN_OUTPUT_ROOTS: Final[tuple[str, ...]] = (
    "output/contract-validation/raw",
    "private",
)

L2_RESULT_FILENAME: Final[str] = "l2-result.json"
CORPUS_FILENAME: Final[str] = "sanitized-replay-corpus.jsonl"

TEMP_SUFFIX: Final[str] = ".tmp"

#: 本仓受控历史来源允许出现的目录前缀（allowlist，Spec12:35-40）。
SOURCE_ROOTS: Final[tuple[str, ...]] = ("benchmark/cases",)

MANIFEST_VERSION: Final[str] = "1"

SOURCE_CLASSES: Final[tuple[str, ...]] = ("cost_log", "trace", "benchmark", "other")
RUNTIME_ADAPTER_STATUSES: Final[tuple[str, ...]] = ("IN_SCOPE", "DEFERRED")
REPRODUCTION_RESTRICTIONS: Final[tuple[str, ...]] = ("NONE", "REPRODUCTION_RESTRICTED")
EVIDENCE_ROLE: Final[str] = "historical_replay_only"

CLASSIFICATIONS: Final[tuple[str, ...]] = (
    "OBSERVED",
    "NOT_OBSERVED",
    "LEGACY_DATA_INSUFFICIENT",
    "REPRODUCTION_RESTRICTED",
)

#: Spec12:84 的差异分类闭集。
DIFFERENCE_CLASSES: Final[tuple[str, ...]] = (
    "NONE",
    "Adapter defect",
    "Contract feedback",
    "legacy insufficiency",
    "expected semantic correction",
)

#: Spec12:109 要求 replay 覆盖的 Rule ID（本步骤的真实落点）。
REPLAY_RULES: Final[tuple[str, ...]] = (
    "EVD-PUB-001",
    "EVD-PUB-004",
    "EVD-PUB-005",
    "EVD-PUB-007",
    "EVD-RUN-001",
    "EVD-RUN-002",
    "EVD-DATA-001",
    "FND-MAP-001",
    "FND-MAP-002",
)

#: Rule ID → 证据覆盖面（本脚本内的透明映射；不新增公共语义）。
RULE_EVIDENCE_SCOPE: Final[Mapping[str, str]] = {
    "EVD-PUB-001": "corpus_allowlist",
    "EVD-PUB-004": "corpus_allowlist",
    "EVD-PUB-005": "restricted",
    "EVD-PUB-007": "run",
    "EVD-RUN-001": "run",
    "EVD-RUN-002": "run",
    "EVD-DATA-001": "corpus_allowlist",
    "FND-MAP-001": "mapping",
    "FND-MAP-002": "mapping",
}

#: corpus 记录的唯一 allowlist（Spec12:49 + Appendix B:2354 的两项标注）。
CORPUS_RECORD_KEYS: Final[tuple[str, ...]] = (
    "record_id",
    "source_class",
    "legacy_facts",
    "expected_mapping",
    "actual_mapping",
    "runtime_adapter_status",
    "evidence_role",
    "sanitized_record_hash",
)

#: 最小 legacy usage/cost facts 的 allowlist（presence + provenance + pricing 证据）。
LEGACY_FACT_KEYS: Final[tuple[str, ...]] = (
    "model",
    "call_observed",
    "usage_object_present",
    "field_presence",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_present",
    "legacy_cost_amount",
    "legacy_cost_is_default",
    "currency",
)

#: canonical Foundation token 字段 → legacy 记录中的候选键（按序取第一个存在者）。
#:
#: 与 Adapter 的 presence 原则一致：**不补零**，缺键即"未观察"。
LEGACY_FIELD_CANDIDATES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("input_tokens", ("prompt_tokens", "tokens_in", "input_tokens")),
    ("output_tokens", ("completion_tokens", "tokens_out", "output_tokens")),
    ("total_tokens", ("tokens_total", "total_tokens")),
)

#: legacy 成本键候选（Coding 为 `cost_usd`，Wiki 为 `cost`）。
LEGACY_COST_KEYS: Final[tuple[str, ...]] = ("cost_usd", "cost", "cost_cny", "total_cost")

#: 单条字符串事实的最大长度（裁剪；只影响 corpus，不影响 mapping）。
MAX_FACT_STRING_LENGTH: Final[int] = 200

# --------------------------------------------------------------------------- #
# 扫描器（secret / forbidden key / absolute path / size / UTF-8）
# --------------------------------------------------------------------------- #

#: secret 内容模式（§11.4:1244-1251）。
SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("openai_key_prefix", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}")),
    ("bearer_credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{12,}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{16,}\b")),
    ("pem_block", re.compile(r"-----BEGIN [A-Z ]+-----")),
    ("long_opaque_hex", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])")),
)

#: 绝对路径模式（Windows / Linux）。匹配**整段路径**，以便 sanitizer 完整替换。
PATH_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("windows_absolute_path", re.compile(r"[A-Za-z]:[\\/][^\s\"'|<>]*")),
    (
        "posix_absolute_path",
        re.compile(r"(?<![\w.])/(?:Users|home|usr|etc|var|opt|root|tmp|mnt|srv|app)/[^\s\"']*"),
    ),
)

#: 禁止出现的 key（§11.4:1244-1251）。匹配是**键名精确/点段精确**，
#: 因此 `input_tokens` / `total_tokens` 这类已登记的 legacy 字段不会被误判。
FORBIDDEN_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "response_body",
        "reasoning",
        "traceback",
        "authorization",
        "api_key",
        "token",
        "password",
    }
)

#: 契约自身的身份值（canonical hash / commit）是声明的 identity，不是 opaque secret。
_DECLARED_IDENTITY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^sha256:[0-9a-f]{64}$"),
    re.compile(r"^[0-9a-f]{40}$"),
)

REDACTION_SECRET: Final[str] = "<SECRET>"
REDACTION_WORKSPACE: Final[str] = "<WORKSPACE>"
REDACTION_GENERIC: Final[str] = "<REDACTED>"


class ToolError(RuntimeError):
    """带退出码的干净失败（绝不打印 traceback）。"""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


@dataclass(frozen=True)
class Finding:
    """一条扫描命中。"""

    kind: str
    location: str
    detail: str

    def render(self) -> str:
        return f"{self.kind}: {self.location}: {self.detail}"


def _is_declared_identity(text: str) -> bool:
    return any(pattern.match(text) for pattern in _DECLARED_IDENTITY_PATTERNS)


def _mask_declared(text: str, declared_identities: Sequence[str]) -> str:
    """把本 artifact 自身声明过的身份值替换为占位符。

    这样做的目的是让"长 hex"检测只针对**未声明**的 opaque token：契约发布物必然
    携带 canonical hash 与 commit（`sha256:<64hex>` / 40 位 hex），把它们一律判为
    secret 会让扫描失去意义；而未声明的长 hex 仍然会被判为命中。
    """
    masked = text
    for value in declared_identities:
        if value:
            masked = masked.replace(value, "DECLARED_IDENTITY")
    return masked


def scan_text(
    text: str,
    *,
    location: str = "$",
    declared_identities: Sequence[str] = (),
) -> list[Finding]:
    """扫描单个字符串：secret 模式 + 绝对路径。

    完全等于 canonical hash / commit 的字符串是**声明的 identity**，不判为 opaque
    token；此外 ``declared_identities`` 里显式声明的值也不会被判为 secret（允许它们
    出现在报告等散文中）。

    容量不在这里判定：§11.4 的容量约束是**单 record / 单 artifact**，由
    :func:`check_record_size`（单记录）与 :func:`check_artifact_size`（单 artifact）
    在各自的边界上执行，避免把多记录 artifact 的整段文本误判为超限。
    """
    findings: list[Finding] = []
    masked = _mask_declared(text, declared_identities)
    if _is_declared_identity(text):
        masked = "DECLARED_IDENTITY"

    for name, pattern in SECRET_PATTERNS:
        match = pattern.search(masked)
        if match is not None:
            findings.append(Finding("secret_pattern", location, f"{name} 命中 {match.group(0)[:16]!r}…"))
    for name, pattern in PATH_PATTERNS:
        match = pattern.search(masked)
        if match is not None:
            findings.append(Finding("absolute_path", location, f"{name} 命中 {match.group(0)!r}"))
    return findings


def check_artifact_size(text: str, *, location: str = "$") -> list[Finding]:
    """单个 artifact 的 UTF-8 字节数不得超过 64 KiB（§11.4 容量约束）。"""
    size = len(text.encode("utf-8"))
    if size > EVIDENCE_RECORD_MAX_BYTES:
        return [
            Finding(
                "oversize_artifact",
                location,
                f"{size} 字节超过上限 {EVIDENCE_RECORD_MAX_BYTES} 字节",
            )
        ]
    return []


def forbidden_key_reason(key: object) -> str | None:
    """判断 key 是否命中禁止名单（精确键名或点段）。"""
    if not isinstance(key, str):
        return "非字符串 key"
    lowered = key.lower()
    if lowered in FORBIDDEN_KEY_NAMES:
        return f"key {key!r} 属于禁止字段名"
    for segment in lowered.split("."):
        if segment in FORBIDDEN_KEY_NAMES:
            return f"key {key!r} 的点段 {segment!r} 属于禁止字段名"
    return None


def scan_structure(
    value: object,
    *,
    location: str = "$",
    declared_identities: Sequence[str] = (),
) -> list[Finding]:
    """递归扫描 JSON 兼容结构：key 名单 + 字符串内容。"""
    findings: list[Finding] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, str):
            findings.extend(
                scan_text(node, location=path, declared_identities=declared_identities)
            )
            return
        if isinstance(node, bool) or node is None or isinstance(node, (int, float)):
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                reason = forbidden_key_reason(key)
                if reason is not None:
                    findings.append(Finding("forbidden_key", f"{path}.{key}", reason))
                walk(item, f"{path}.{key}")
            return
        if isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        findings.append(Finding("non_json_value", path, f"类型 {type(node).__name__} 不是 JSON 值"))

    walk(value, location)
    return findings


def check_record_size(record: object, *, location: str = "$") -> list[Finding]:
    """单条记录 canonical JSON 不得超过 64 KiB（EVD-DATA-001）。"""
    size = len(canonical_json_dumps(record).encode("utf-8"))
    if size > EVIDENCE_RECORD_MAX_BYTES:
        return [
            Finding(
                "oversize_record",
                location,
                f"{size} 字节超过上限 {EVIDENCE_RECORD_MAX_BYTES} 字节",
            )
        ]
    return []


def scan_serialized(
    text: str, *, location: str = "$", declared_identities: Sequence[str] = ()
) -> list[Finding]:
    """扫描即将落盘的文本（BOM / 非法 UTF-8 已在 decode 阶段处理）。"""
    return scan_text(text, location=location, declared_identities=declared_identities)


def decode_utf8_strict(raw: bytes, *, location: str = "$") -> tuple[str | None, list[Finding]]:
    """严格 UTF-8 解码：拒绝 BOM 与非法字节（§11.4）。"""
    findings: list[Finding] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        findings.append(Finding("bom", location, "文件以 UTF-8 BOM 开头"))
        return None, findings
    if b"\x00" in raw:
        findings.append(Finding("nul_byte", location, "文件包含 NUL 字节，疑似二进制"))
        return None, findings
    try:
        return raw.decode("utf-8"), findings
    except UnicodeDecodeError as exc:
        findings.append(Finding("invalid_utf8", location, f"{exc}"))
        return None, findings


def read_text_strict(path: Path) -> str:
    """读取受控来源/artifact；BOM 或非法编码即扫描失败。"""
    raw = Path(path).read_bytes()
    text, findings = decode_utf8_strict(raw, location=str(path))
    if findings:
        raise ToolError(EXIT_VALIDATION_FAILED, render_findings(findings))
    assert text is not None  # decode_utf8_strict 在 findings 为空时必定返回文本
    return text


def render_findings(findings: Sequence[Finding]) -> str:
    lines = [f"扫描失败：{len(findings)} 项命中"]
    lines.extend(f"  - {item.render()}" for item in findings)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 原子写入（§11.1 的同一序列：同目录 .tmp → fsync → os.replace）
# --------------------------------------------------------------------------- #


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入：同目录临时文件 → flush/fsync → ``os.replace``。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + TEMP_SUFFIX)
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# sanitize（**绝不**调用 adapter mapping —— Spec12:46）
# --------------------------------------------------------------------------- #


def sanitize_text(value: str) -> str:
    """裁剪 + 掩码：只做字符串级净化，不做任何 contract mapping。"""
    text = value
    for _name, pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTION_SECRET, text)
    for _name, pattern in PATH_PATTERNS:
        text = pattern.sub(REDACTION_WORKSPACE, text)
    if len(text) > MAX_FACT_STRING_LENGTH:
        text = text[:MAX_FACT_STRING_LENGTH] + REDACTION_GENERIC
    return text


def sanitize_legacy_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """按 ``LEGACY_FACT_KEYS`` 重建最小 facts；**不调用任何 Adapter**。"""
    sanitized: dict[str, Any] = {}
    for key in LEGACY_FACT_KEYS:
        if key not in facts:
            continue
        value = facts[key]
        if isinstance(value, str):
            sanitized[key] = sanitize_text(value)
        elif isinstance(value, Mapping):
            sanitized[key] = {
                str(inner_key): bool(inner_value) for inner_key, inner_value in value.items()
            }
        else:
            sanitized[key] = value
    return sanitized


# --------------------------------------------------------------------------- #
# legacy facts 提取（presence-aware；不调用 Adapter）
# --------------------------------------------------------------------------- #


def _as_int(value: object) -> int | None:
    """计数转 int；无法转换即未知，**不补零**。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_present(record: Mapping[str, Any], keys: Iterable[str]) -> tuple[str | None, object]:
    """返回第一个**存在且非 None**的候选键及其原文。"""
    for key in keys:
        if key in record and record[key] is not None:
            return key, record[key]
    return None, None


def extract_legacy_facts(record: Mapping[str, Any]) -> dict[str, Any]:
    """从一条历史记录提取最小 presence-aware legacy facts。

    这是 **allowlist extraction**（§11.3:1214-1223）：只读已登记的 legacy 字段，
    presence 与 provenance 一并保留，禁止用 ``or 0`` 之类的默认值抹掉"未观察"。
    """
    presence: dict[str, bool] = {}
    values: dict[str, int | None] = {}
    for field_name, candidates in LEGACY_FIELD_CANDIDATES:
        source_key, raw = _first_present(record, candidates)
        presence[field_name] = source_key is not None
        values[field_name] = _as_int(raw)

    cost_key, raw_cost = _first_present(record, LEGACY_COST_KEYS)
    cost_amount = _as_float(raw_cost)
    model_key, raw_model = _first_present(record, ("model",))

    return {
        "model": raw_model if isinstance(raw_model, str) else None,
        "call_observed": True,
        "usage_object_present": any(presence.values()),
        "field_presence": presence,
        "input_tokens": values["input_tokens"],
        "output_tokens": values["output_tokens"],
        "total_tokens": values["total_tokens"],
        "cost_present": cost_key is not None,
        "legacy_cost_amount": cost_amount,
        "legacy_cost_is_default": bool(record.get("estimate")) if cost_key else False,
        "currency": currency_context(),
    }


def facts_are_insufficient(facts: Mapping[str, Any]) -> bool:
    """legacy 数据是否不足以支撑任何 Foundation 语义（Spec12:48）。"""
    presence = facts.get("field_presence") or {}
    has_tokens = any(bool(value) for value in presence.values()) or any(
        facts.get(key) is not None for key in ("input_tokens", "output_tokens", "total_tokens")
    )
    return not has_tokens and not bool(facts.get("cost_present"))


# --------------------------------------------------------------------------- #
# Adapter bridge（唯一调用 Adapter 的地方）
# --------------------------------------------------------------------------- #


def load_adapter() -> tuple[Any, Any]:
    """返回本仓 Adapter 的 ``(facts, mapping)`` 模块。

    Coding 没有 ``arknights_wiki`` 包，Adapter 位于仓库根下的 ``adapters/foundation``。
    单独抽出是为了让测试可以证明 sanitizer / extractor **没有**触碰 Adapter。
    """
    from adapters.foundation import facts as facts_module
    from adapters.foundation import mapping as mapping_module

    return facts_module, mapping_module


def currency_context() -> str:
    """本仓成本记账币种（来自 Adapter facts 模块的登记常量；Coding 为 USD）。"""
    facts_module, _ = load_adapter()
    return str(facts_module.CURRENCY_CONTEXT)


def load_price_table() -> tuple[Any, str]:
    """加载本仓价格表并返回 ``(CodingPriceTable, 来源说明)``。

    Coding 没有价格表**文件**：表是 ``benchmark/runner.py`` 的 ``MODEL_PRICE_USD_PER_1K``
    （v0.1 当前为空表）。空表意味着任何 legacy ``0.0`` 都不能证明真实零。
    来源说明会写进 ``l2-result.json``，便于诊断"为什么 amount 是 unknown"。
    """
    facts_module, _ = load_adapter()
    table: Mapping[str, object] = {}
    source = "empty_fallback"
    try:
        from benchmark.runner import MODEL_PRICE_USD_PER_1K

        if isinstance(MODEL_PRICE_USD_PER_1K, Mapping):
            table = MODEL_PRICE_USD_PER_1K
            source = "benchmark.runner.MODEL_PRICE_USD_PER_1K"
    except Exception as exc:  # noqa: BLE001 - 缺依赖时退化为空表并在 l2-result 记录
        source = f"unavailable:{type(exc).__name__}"
    return facts_module.CodingPriceTable.from_mapping(table), source


def _mapping_facts(facts: Mapping[str, Any], table: Any) -> tuple[Any, Any]:
    """把最小 facts 还原成 Adapter facts DTO（presence 保真，**不补零**）。"""
    facts_module, _ = load_adapter()
    presence = facts.get("field_presence") or {}
    model = facts.get("model") if isinstance(facts.get("model"), str) else None
    call_observed = bool(facts.get("call_observed"))

    usage_facts = facts_module.extract_usage_from_counts(
        facts.get("input_tokens") if presence.get("input_tokens") else None,
        facts.get("output_tokens") if presence.get("output_tokens") else None,
        model=model,
        call_observed=call_observed,
    )
    cost_facts = facts_module.extract_cost_facts(
        table,
        model=model,
        legacy_cost_usd=facts.get("legacy_cost_amount") if facts.get("cost_present") else None,
        call_observed=call_observed,
        legacy_cost_is_default=bool(facts.get("legacy_cost_is_default")),
    )
    return usage_facts, cost_facts


@dataclass(frozen=True)
class MappingResult:
    """actual Foundation mapping 的结果摘要。"""

    mapping_outcome: str
    foundation_objects: list[str]
    usage_source: str | None
    cost_source: str | None
    cost_amount_present: bool
    error_code: str | None
    difference_class: str


def map_to_foundation(
    source: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    pricing_snapshot: object | None = None,
) -> MappingResult:
    """用本仓 Adapter 在 sanitized facts 上产生 actual Foundation mapping。

    mapping 失败**不中断** run：它被记录为该记录的 NOT_OBSERVED + ``Adapter defect``
    （Spec12:47；strict 模式在 run 级别把该缺陷变成非零退出码）。
    """
    _facts_module, mapping_module = load_adapter()
    table = pricing_snapshot if pricing_snapshot is not None else load_price_table()[0]
    usage_facts, cost_facts = _mapping_facts(facts, table)

    try:
        observation = mapping_module.map_observation(
            usage_facts=usage_facts, cost_facts=cost_facts
        )
    except mapping_module.MappingFailure as exc:
        return MappingResult(
            mapping_outcome="NOT_OBSERVED",
            foundation_objects=[],
            usage_source=None,
            cost_source=None,
            cost_amount_present=False,
            error_code=str(getattr(exc.envelope, "code", "unknown")),
            difference_class="Adapter defect",
        )

    objects: list[str] = []
    if observation.usage is not None:
        objects.append("Usage")
    if observation.cost is not None:
        objects.append("Cost")
    if observation.cost_summary is not None:
        objects.append("CostSummary")

    usage_source = getattr(observation.usage, "source", None)
    cost_source = getattr(observation.cost, "source", None)
    cost_amount_present = getattr(observation.cost, "amount", None) is not None

    expected_objects = expected_foundation_objects(source)
    legacy_amount = facts.get("legacy_cost_amount")
    if not expected_objects:
        difference = "NONE"
    elif any(item not in objects for item in expected_objects):
        difference = "legacy insufficiency" if facts_are_insufficient(facts) else "Contract feedback"
    elif legacy_amount is not None and not cost_amount_present:
        difference = "expected semantic correction"
    else:
        difference = "NONE"

    return MappingResult(
        mapping_outcome="OBSERVED",
        foundation_objects=objects,
        usage_source=str(usage_source) if usage_source is not None else None,
        cost_source=str(cost_source) if cost_source is not None else None,
        cost_amount_present=cost_amount_present,
        error_code=None,
        difference_class=difference,
    )


# --------------------------------------------------------------------------- #
# registry / manifest 校验
# --------------------------------------------------------------------------- #


def load_registry(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """读取本仓 producer registry（受控 artifact，只读）。"""
    path = Path(repo_root) / PRODUCER_REGISTRY_RELPATH
    if not path.is_file():
        raise ToolError(EXIT_USAGE, f"找不到 producer registry：{path}")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(EXIT_USAGE, f"producer registry 不是合法 JSON：{exc}") from exc
    if not isinstance(registry, dict):
        raise ToolError(EXIT_USAGE, "producer registry 顶层必须是 JSON object")
    return registry


def registry_producers(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    producers = registry.get("producers")
    if not isinstance(producers, list) or not producers:
        raise ToolError(EXIT_USAGE, "producer registry 缺少非空 producers 列表")
    result: dict[str, Mapping[str, Any]] = {}
    for entry in producers:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("producer_id"), str):
            raise ToolError(EXIT_USAGE, "producer registry 含形状非法的条目")
        result[str(entry["producer_id"])] = entry
    return result


def in_scope_stages(registry: Mapping[str, Any]) -> list[tuple[str, str]]:
    """返回本仓 ``status == "IN_SCOPE"`` 的 ``(producer_id, mapping_stage)`` 组合。"""
    pairs: list[tuple[str, str]] = []
    for producer_id, entry in registry_producers(registry).items():
        if entry.get("status") != "IN_SCOPE":
            continue
        stages = entry.get("mapping_stages")
        if not isinstance(stages, list):
            continue
        for stage in stages:
            pairs.append((producer_id, str(stage)))
    return pairs


MANIFEST_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "manifest_version",
    "run_id",
    "contract_mode",
    "contract_version",
    "payload_hash",
    "repository_commit",
    "output_dir",
    "sources",
    "max_records",
    "reproduction_restriction",
)

SOURCE_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "source_id",
    "source_class",
    "path",
    "producer_id",
    "mapping_stage",
    "runtime_adapter_status",
    "evidence_role",
)


def load_manifest(path: Path) -> dict[str, Any]:
    """读取 run manifest（G-02：provisional 最小 schema）。"""
    target = Path(path)
    if not target.is_file():
        raise ToolError(EXIT_USAGE, f"找不到 run manifest：{target}")
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(EXIT_USAGE, f"run manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise ToolError(EXIT_USAGE, "run manifest 顶层必须是 JSON object")
    return manifest


def validate_source_path(raw_path: object, repo_root: Path) -> Path:
    """校验 allowlist 内的历史来源路径（存在、相对、位于受控目录下）。"""
    if not isinstance(raw_path, str) or not raw_path:
        raise ToolError(EXIT_USAGE, "sources[].path 必须是非空字符串")
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ToolError(EXIT_USAGE, f"sources[].path 必须是仓库相对路径：{raw_path!r}")
    normalized = candidate.as_posix()
    if ".." in candidate.parts:
        raise ToolError(EXIT_USAGE, f"sources[].path 不得包含上跳：{raw_path!r}")
    if not any(
        normalized == root or normalized.startswith(root + "/") for root in SOURCE_ROOTS
    ):
        raise ToolError(
            EXIT_USAGE,
            f"sources[].path 不在受控来源目录内（允许：{list(SOURCE_ROOTS)}）：{raw_path!r}",
        )
    resolved = Path(repo_root) / candidate
    if not resolved.is_file():
        raise ToolError(EXIT_USAGE, f"sources[].path 指向的历史 artifact 不存在：{raw_path!r}")
    return resolved


def validate_manifest(
    manifest: Mapping[str, Any],
    registry: Mapping[str, Any],
    repo_root: Path = REPO_ROOT,
) -> list[dict[str, Any]]:
    """校验 manifest 与 registry 的一致性；返回规范化后的 sources 列表。

    任何不合法都抛 :class:`ToolError`（退出码 2）：manifest 非法是**唯一**允许
    中断整个 run 的情形。
    """
    for key in MANIFEST_REQUIRED_KEYS:
        if key not in manifest:
            raise ToolError(EXIT_USAGE, f"run manifest 缺少必需字段 {key!r}")

    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise ToolError(EXIT_USAGE, "manifest_version 必须是 \"1\"（provisional，G-02）")

    run_id = manifest["run_id"]
    if not is_safe_run_id(run_id):
        raise ToolError(EXIT_USAGE, f"run manifest 的 run_id 不是 safe run id：{run_id!r}")

    if manifest["contract_mode"] != ContractMode.STRICT.value:
        raise ToolError(EXIT_USAGE, "run manifest 的 contract_mode 必须是 \"strict\"")

    registry_version = registry.get("contract_version")
    if manifest["contract_version"] != registry_version:
        raise ToolError(
            EXIT_USAGE,
            f"manifest contract_version={manifest['contract_version']!r} 与本仓 registry "
            f"{registry_version!r} 不一致",
        )

    if not isinstance(manifest["payload_hash"], str) or not PAYLOAD_HASH_PATTERN.match(
        manifest["payload_hash"]
    ):
        raise ToolError(EXIT_USAGE, "payload_hash 必须是 \"sha256:\" + 64 位小写 hex")

    declared_commit = manifest["repository_commit"]
    if declared_commit is not None and (
        not isinstance(declared_commit, str)
        or not REPOSITORY_COMMIT_PATTERN.match(declared_commit)
    ):
        raise ToolError(
            EXIT_USAGE,
            "repository_commit 必须是 40 位小写 hex 或 null"
            "（null = 运行期由 AGENT_CONTRACT_COMMIT 解析）",
        )

    output_dir = manifest["output_dir"]
    if not isinstance(output_dir, str) or not output_dir:
        raise ToolError(EXIT_USAGE, "output_dir 必须是非空字符串")
    _validate_output_dir(output_dir)

    if not isinstance(manifest["max_records"], int) or isinstance(manifest["max_records"], bool):
        raise ToolError(EXIT_USAGE, "max_records 必须是整数（0 = 不设上限）")
    if manifest["max_records"] < 0:
        raise ToolError(EXIT_USAGE, "max_records 不得为负数")

    if manifest["reproduction_restriction"] not in REPRODUCTION_RESTRICTIONS:
        raise ToolError(
            EXIT_USAGE,
            f"reproduction_restriction 必须是 {list(REPRODUCTION_RESTRICTIONS)} 之一",
        )

    sources = manifest["sources"]
    if not isinstance(sources, list) or not sources:
        raise ToolError(EXIT_USAGE, "sources 必须是至少含一项的列表")

    producers = registry_producers(registry)
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise ToolError(EXIT_USAGE, f"sources[{index}] 必须是 JSON object")
        for key in SOURCE_REQUIRED_KEYS:
            if key not in source:
                raise ToolError(EXIT_USAGE, f"sources[{index}] 缺少必需字段 {key!r}")

        source_id = source["source_id"]
        if not isinstance(source_id, str) or not is_safe_run_id(source_id):
            raise ToolError(EXIT_USAGE, f"sources[{index}].source_id 不是 safe 标识：{source_id!r}")
        if source_id in seen_ids:
            raise ToolError(EXIT_USAGE, f"sources[].source_id 重复：{source_id!r}")
        seen_ids.add(source_id)

        if source["source_class"] not in SOURCE_CLASSES:
            raise ToolError(
                EXIT_USAGE,
                f"sources[{index}].source_class 必须是 {list(SOURCE_CLASSES)} 之一",
            )
        if source["evidence_role"] != EVIDENCE_ROLE:
            raise ToolError(
                EXIT_USAGE,
                f"sources[{index}].evidence_role 必须是 {EVIDENCE_ROLE!r}（Spec12:39）",
            )
        if source["runtime_adapter_status"] not in RUNTIME_ADAPTER_STATUSES:
            raise ToolError(
                EXIT_USAGE,
                f"sources[{index}].runtime_adapter_status 必须是 "
                f"{list(RUNTIME_ADAPTER_STATUSES)} 之一",
            )

        producer = producers.get(source["producer_id"])
        if producer is None:
            raise ToolError(
                EXIT_USAGE,
                f"sources[{index}].producer_id {source['producer_id']!r} 不在本仓 producer "
                f"registry 中（本仓登记：{sorted(producers)}）",
            )

        registry_status = producer.get("status")
        stage = source["mapping_stage"]
        if registry_status == "IN_SCOPE":
            stages = [str(item) for item in producer.get("mapping_stages", [])]
            if source["runtime_adapter_status"] != "IN_SCOPE":
                raise ToolError(
                    EXIT_USAGE,
                    f"sources[{index}] 的 producer {source['producer_id']!r} 在本仓 registry 为 "
                    "IN_SCOPE，runtime_adapter_status 必须是 \"IN_SCOPE\"",
                )
            if stage not in stages:
                raise ToolError(
                    EXIT_USAGE,
                    f"sources[{index}].mapping_stage {stage!r} 不在 producer "
                    f"{source['producer_id']!r} 的登记 stage {stages} 内",
                )
        else:
            if source["runtime_adapter_status"] != "DEFERRED":
                raise ToolError(
                    EXIT_USAGE,
                    f"sources[{index}] 的 producer {source['producer_id']!r} 在本仓 registry 为 "
                    f"{registry_status!r}，runtime_adapter_status 必须是 \"DEFERRED\"",
                )
            if stage is not None:
                raise ToolError(
                    EXIT_USAGE,
                    f"sources[{index}].mapping_stage 对 DEFERRED producer 必须为 null"
                    "（registry 未登记该 producer 的任何 stage）",
                )

        resolved = validate_source_path(source["path"], repo_root)
        entry = dict(source)
        entry["_resolved_path"] = resolved
        normalized.append(entry)

    return normalized


def _validate_output_dir(output_dir: str) -> None:
    candidate = Path(output_dir)
    if candidate.is_absolute():
        raise ToolError(EXIT_USAGE, f"output_dir 必须是仓库相对路径：{output_dir!r}")
    normalized = candidate.as_posix()
    if ".." in candidate.parts:
        raise ToolError(EXIT_USAGE, f"output_dir 不得包含上跳：{output_dir!r}")
    for forbidden in FORBIDDEN_OUTPUT_ROOTS:
        if normalized == forbidden or normalized.startswith(forbidden + "/"):
            raise ToolError(
                EXIT_USAGE,
                f"output_dir 不得指向 {forbidden!r}（Master §11.1；Spec12:51）",
            )


def expected_foundation_objects(source: Mapping[str, Any]) -> list[str]:
    """从 registry 取得预登记的 expected Foundation 对象（G-14 的 provisional 收敛）。"""
    registry = source.get("_registry_entry")
    if isinstance(registry, Mapping):
        objects = registry.get("foundation_objects")
        if isinstance(objects, list):
            return [str(item) for item in objects]
    return []


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #


@dataclass
class SourceRun:
    source: Mapping[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    corpus: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReplayOutcome:
    exit_code: int
    result: str
    l2_result: dict[str, Any]
    corpus_records: list[dict[str, Any]]
    messages: list[str] = field(default_factory=list)


def _iter_records(path: Path, max_records: int) -> list[Mapping[str, Any]]:
    """按 JSONL / JSON 读取受控来源；只保留 JSON object 记录。"""
    text = read_text_strict(path)
    records: list[Mapping[str, Any]] = []

    stripped = text.strip()
    if stripped.startswith("["):
        payload = json.loads(stripped)
        candidates: Sequence[Any] = payload if isinstance(payload, list) else [payload]
    elif stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            candidates = [payload]
        except json.JSONDecodeError:
            candidates = _jsonl_candidates(text)
    else:
        candidates = _jsonl_candidates(text)

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            records.append(candidate)
        if max_records and len(records) >= max_records:
            break
    return records


def _jsonl_candidates(text: str) -> list[Any]:
    candidates: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        candidates.append(json.loads(line))
    return candidates


def _head_commit(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - 无 git 可执行文件时
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head if REPOSITORY_COMMIT_PATTERN.match(head) else None


def record_hash(record: Mapping[str, Any]) -> str:
    """corpus 记录哈希：对**不含自身 hash** 的记录取 canonical JSON SHA256。"""
    payload = {key: value for key, value in record.items() if key != "sanitized_record_hash"}
    return sha256_hex(canonical_json_dumps(payload).encode("utf-8"))


def build_corpus_record(
    *,
    source_id: str,
    source_class: str,
    ordinal: int,
    legacy_facts: Mapping[str, Any],
    expected_mapping: Mapping[str, Any],
    actual_mapping: Mapping[str, Any],
    runtime_adapter_status: str,
) -> dict[str, Any]:
    """按严格 allowlist 构造一条 corpus 记录（EVD-PUB-004）。"""
    record = {
        "record_id": f"{source_id}-{ordinal:06d}",
        "source_class": source_class,
        "legacy_facts": dict(legacy_facts),
        "expected_mapping": dict(expected_mapping),
        "actual_mapping": dict(actual_mapping),
        "runtime_adapter_status": runtime_adapter_status,
        "evidence_role": EVIDENCE_ROLE,
    }
    unknown = set(record) - set(CORPUS_RECORD_KEYS) - {"sanitized_record_hash"}
    if unknown:  # pragma: no cover - 结构性防御
        raise ToolError(EXIT_USAGE, f"corpus 记录出现 allowlist 外的键：{sorted(unknown)}")
    record["sanitized_record_hash"] = record_hash(record)
    return record


def classify(
    *,
    source: Mapping[str, Any],
    facts: Mapping[str, Any],
    mapping: MappingResult | None,
    reproduction_restriction: str,
) -> tuple[str, str]:
    """返回 ``(status, mapping_outcome)``。

    优先级：DEFERRED → 数据不足 → mapping 结果 → run 级受限标注（EVD-PUB-005）。
    """
    if source["runtime_adapter_status"] == "DEFERRED":
        return "NOT_OBSERVED", "NOT_OBSERVED"
    if facts_are_insufficient(facts):
        return "LEGACY_DATA_INSUFFICIENT", "LEGACY_DATA_INSUFFICIENT"
    assert mapping is not None
    if mapping.mapping_outcome != "OBSERVED":
        return "NOT_OBSERVED", mapping.mapping_outcome
    if reproduction_restriction == "REPRODUCTION_RESTRICTED":
        return "REPRODUCTION_RESTRICTED", "OBSERVED"
    return "OBSERVED", "OBSERVED"


def _empty_status_counts() -> dict[str, int]:
    return {name: 0 for name in CLASSIFICATIONS}


def _coverage_matrix(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rule / semantic case 覆盖矩阵（Spec12:48 + Spec12:109）。

    `scope` 是本脚本内的透明归属：
    `corpus_allowlist` = 逐条 corpus 记录；`mapping` = 逐条 actual mapping；
    `restricted` = 受限记录；`run` = run 级绑定/保留决策。
    """
    rules: list[dict[str, Any]] = []
    for rule_id in REPLAY_RULES:
        scope = RULE_EVIDENCE_SCOPE[rule_id]
        counts = _empty_status_counts()
        evidence_ids: list[str] = []

        if scope == "run":
            counts["OBSERVED"] = 1 if records else 0
        else:
            for record in records:
                actual = record["actual_mapping"]
                status = actual["status"]
                counts[status] += 1
                if scope == "corpus_allowlist":
                    evidence_ids.append(record["record_id"])
                elif scope == "restricted":
                    if status == "REPRODUCTION_RESTRICTED":
                        evidence_ids.append(record["record_id"])
                elif actual["mapping_outcome"] in {
                    "OBSERVED",
                    "NOT_OBSERVED",
                    "LEGACY_DATA_INSUFFICIENT",
                }:
                    evidence_ids.append(record["record_id"])

        rules.append(
            {
                "rule_id": rule_id,
                "scope": scope,
                "evidence_count": len(evidence_ids),
                "by_status": counts,
                "evidence_ids": evidence_ids[:50],
            }
        )

    semantic = _empty_status_counts()
    for record in records:
        semantic[record["actual_mapping"]["status"]] += 1
    return {"rules": rules, "semantic_cases": list(CLASSIFICATIONS), "by_status": semantic}


def run_replay(
    *,
    manifest_path: Path,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    verify_payload: bool = True,
    verify_head: bool = True,
    write: bool = True,
) -> ReplayOutcome:
    """执行一次 L2 replay；返回值携带退出码（不直接 ``sys.exit``）。"""
    environment = os.environ if env is None else env
    repo_root = REPO_ROOT if repo_root is None else Path(repo_root)

    # 1) 前置：strict mode + 显式 safe run id（母 Spec C.1:2378-2380）
    raw_mode = environment.get(CONTRACT_MODE_ENV)
    try:
        mode = parse_contract_mode(raw_mode)
    except Exception as exc:  # ContractModeConfigError
        raise ToolError(EXIT_USAGE, str(exc)) from exc
    if mode is not ContractMode.STRICT:
        raise ToolError(
            EXIT_USAGE,
            f"{CONTRACT_MODE_ENV} 必须是 'strict'（实际 {raw_mode!r}）；"
            "replay_history 只允许在 strict 模式下运行",
        )

    run_id = (environment.get(RUN_ID_ENV) or "").strip()
    if not run_id:
        raise ToolError(
            EXIT_USAGE,
            f"缺少显式 {RUN_ID_ENV}（EVD-RUN-001）；replay 必须绑定一个 safe run id",
        )
    if not is_safe_run_id(run_id):
        raise ToolError(EXIT_USAGE, f"{RUN_ID_ENV} 不是 safe run id：{run_id!r}")

    # 2) manifest / registry 校验
    manifest = load_manifest(manifest_path)
    registry = load_registry(repo_root)
    sources = validate_manifest(manifest, registry, repo_root)
    if manifest["run_id"] != run_id:
        raise ToolError(
            EXIT_USAGE,
            f"manifest.run_id={manifest['run_id']!r} 与 {RUN_ID_ENV}={run_id!r} 不一致"
            "（§13.3 要求显式 run_id 与 run manifest 一致）",
        )

    messages: list[str] = []

    # 3) Payload identity
    payload_hash_verified = False
    if verify_payload:
        computed, _per_file = canonical_file_bundle_digest(Path(repo_root))
        if computed != manifest["payload_hash"]:
            raise ToolError(
                EXIT_USAGE,
                "manifest payload_hash 与当前 checkout 的 Payload Hash 不一致（DIVERGED）：\n"
                f"  manifest: {manifest['payload_hash']}\n"
                f"  checkout: {computed}",
            )
        payload_hash_verified = True
    else:  # pragma: no cover - 仅测试注入
        payload_hash_verified = True

    committed = manifest["repository_commit"]
    if committed is None:
        resolved_commit = (environment.get(COMMIT_ENV) or "").strip()
        if not resolved_commit:
            raise ToolError(
                EXIT_USAGE,
                f"manifest repository_commit 为 null 时必须由 {COMMIT_ENV} 提供运行 commit"
                "（预登记不含运行 commit，且不得自动推断 HEAD）",
            )
        if not REPOSITORY_COMMIT_PATTERN.match(resolved_commit):
            raise ToolError(
                EXIT_USAGE, f"{COMMIT_ENV} 不是 40 位小写 hex：{resolved_commit!r}"
            )
        commit_source = COMMIT_ENV
    else:
        resolved_commit = str(committed)
        commit_source = "manifest"

    head = _head_commit(Path(repo_root)) if verify_head else None
    commit_matches_head = head is None or head == resolved_commit
    if head is not None and not commit_matches_head:
        messages.append(
            "repository_commit 与当前 HEAD 不同（仅记录诊断，不改变 manifest 语义）："
            f"resolved={resolved_commit} head={head}"
        )

    producers = registry_producers(registry)
    try:
        pricing_snapshot, price_table_source = load_price_table()
    except Exception as exc:  # pragma: no cover - 价格表不可用时仍可用空事实映射
        messages.append(f"价格表不可用（记录为诊断）：{type(exc).__name__}: {exc}")
        pricing_snapshot = None
        price_table_source = f"unavailable:{type(exc).__name__}"

    # 4) 逐来源 allowlist extraction → mapping → 分类
    all_corpus: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    record_summaries: list[dict[str, Any]] = []
    adapter_defects = 0
    total_by_status = _empty_status_counts()
    total_by_outcome = _empty_status_counts()

    for source in sources:
        resolved: Path = source["_resolved_path"]
        # 受控历史 artifact 自身的哈希：把 L2 证据绑定到**读取时的具体内容**，
        # 而不是只绑定路径（Spec12 Stop Condition："artifact 无法绑定来源"）。
        source_sha256 = sha256_hex(resolved.read_bytes())
        records = _iter_records(resolved, int(manifest["max_records"]))
        registry_entry = producers[str(source["producer_id"])]
        entry = dict(source)
        entry["_registry_entry"] = registry_entry

        expected_mapping = {
            "producer_id": source["producer_id"],
            "mapping_stage": source["mapping_stage"],
            "foundation_objects": [
                str(item) for item in registry_entry.get("foundation_objects", [])
            ],
            "evidence_requirement": registry_entry.get("evidence_requirement"),
        }

        by_status = _empty_status_counts()
        by_outcome = _empty_status_counts()
        source_corpus: list[dict[str, Any]] = []

        for ordinal, raw_record in enumerate(records, start=1):
            raw_facts = extract_legacy_facts(raw_record)
            facts = sanitize_legacy_facts(raw_facts)

            mapping: MappingResult | None = None
            if (
                source["runtime_adapter_status"] != "DEFERRED"
                and not facts_are_insufficient(facts)
            ):
                mapping = map_to_foundation(entry, facts, pricing_snapshot=pricing_snapshot)

            status, outcome = classify(
                source=entry,
                facts=facts,
                mapping=mapping,
                reproduction_restriction=str(manifest["reproduction_restriction"]),
            )
            if mapping is None:
                actual_mapping: dict[str, Any] = {
                    "status": status,
                    "mapping_outcome": outcome,
                    "foundation_objects": [],
                    "usage_source": None,
                    "cost_source": None,
                    "cost_amount_present": False,
                    "error_code": None,
                    "difference_class": "NONE"
                    if source["runtime_adapter_status"] == "DEFERRED"
                    else "legacy insufficiency",
                }
            else:
                actual_mapping = {
                    "status": status,
                    "mapping_outcome": mapping.mapping_outcome,
                    "foundation_objects": list(mapping.foundation_objects),
                    "usage_source": mapping.usage_source,
                    "cost_source": mapping.cost_source,
                    "cost_amount_present": mapping.cost_amount_present,
                    "error_code": mapping.error_code,
                    "difference_class": mapping.difference_class,
                }
                if mapping.difference_class == "Adapter defect":
                    adapter_defects += 1

            corpus_record = build_corpus_record(
                source_id=str(source["source_id"]),
                source_class=str(source["source_class"]),
                ordinal=ordinal,
                legacy_facts=facts,
                expected_mapping=expected_mapping,
                actual_mapping=actual_mapping,
                runtime_adapter_status=str(source["runtime_adapter_status"]),
            )
            extra_keys = set(corpus_record) - set(CORPUS_RECORD_KEYS)
            if extra_keys:  # pragma: no cover - allowlist 防御
                raise ToolError(EXIT_USAGE, f"corpus 记录出现 allowlist 外的键：{sorted(extra_keys)}")

            by_status[status] += 1
            by_outcome[outcome] += 1
            total_by_status[status] += 1
            total_by_outcome[outcome] += 1
            source_corpus.append(corpus_record)
            record_summaries.append(
                {
                    "record_id": corpus_record["record_id"],
                    "source_id": str(source["source_id"]),
                    "status": status,
                    "mapping_outcome": outcome,
                    "difference_class": actual_mapping["difference_class"],
                }
            )

        all_corpus.extend(source_corpus)
        source_summaries.append(
            {
                "source_id": str(source["source_id"]),
                "source_class": str(source["source_class"]),
                "path": str(source["path"]),
                "sha256": source_sha256,
                "producer_id": str(source["producer_id"]),
                "mapping_stage": source["mapping_stage"],
                "runtime_adapter_status": str(source["runtime_adapter_status"]),
                "evidence_role": str(source["evidence_role"]),
                "records": len(source_corpus),
                "by_status": by_status,
                "by_mapping_outcome": by_outcome,
            }
        )

    # 5) 扫描（secret / forbidden key / absolute path / size）
    findings: list[Finding] = []
    declared_for_records = [str(record["sanitized_record_hash"]) for record in all_corpus]
    for record in all_corpus:
        location = f"corpus[{record['record_id']}]"
        findings.extend(
            scan_structure(
                record, location=location, declared_identities=declared_for_records
            )
        )
        findings.extend(check_record_size(record, location=location))

    coverage = _coverage_matrix(all_corpus)

    output_dir = Path(manifest["output_dir"]) / str(manifest["run_id"])
    result = "PASS" if adapter_defects == 0 else "CONTRACT_FEEDBACK"
    exit_code = EXIT_OK if adapter_defects == 0 else EXIT_VALIDATION_FAILED
    publish_corpus = bool(all_corpus)

    l2_result: dict[str, Any] = {
        "tool": "replay_history",
        "tool_version": "1",
        "repository": REPOSITORY,
        "run_id": str(manifest["run_id"]),
        "contract_mode": ContractMode.STRICT.value,
        "contract_version": str(manifest["contract_version"]),
        "contract_payload_hash": str(manifest["payload_hash"]),
        "payload_hash_verified": payload_hash_verified,
        "repository_commit": resolved_commit,
        "repository_commit_source": commit_source,
        "repository_commit_matches_head": commit_matches_head,
        "reproduction_restriction": str(manifest["reproduction_restriction"]),
        "price_table_source": price_table_source,
        "manifest_sha256": sha256_hex(Path(manifest_path).read_bytes()),
        "result": result,
        "totals": {
            "sources": len(source_summaries),
            "records": len(all_corpus),
            "by_status": total_by_status,
            "by_mapping_outcome": total_by_outcome,
            "adapter_defects": adapter_defects,
        },
        "sources": source_summaries,
        "records": record_summaries,
        "coverage_matrix": coverage,
        "scan": {"findings": [item.render() for item in findings], "failed": len(findings)},
        "corpus": {
            "published": publish_corpus,
            "records": len(all_corpus),
            "path": str(output_dir / CORPUS_FILENAME) if publish_corpus else None,
        },
        "raw_retention": {
            "decision": "RAW_BUSINESS_EVIDENCE_RETAINED_IN_CONTROLLED_SOURCE_LOCATION",
            "sources": sorted({str(item["path"]) for item in source_summaries}),
            "source_sha256": sorted({str(item["sha256"]) for item in source_summaries}),
            "copied_into_contract_staging": False,
            "copied_into_git": False,
            "reason": "Spec12:51 / EVD-PUB-007：raw 业务证据只在受控位置保留",
        },
        "ancestor_check": {
            "applicable": False,
            "reason": "L2 步骤只产生 Candidate-bound 证据，不形成 B 提交；"
            "'A is ancestor of B' 由 Spec 14/15 校验",
        },
        "notes": [
            "G-02 provisional：replay manifest 字段结构在规范中缺失，本工具使用最小可追溯 schema。",
            "G-14 provisional：expected mapping 直接取自本仓 producer registry 的登记字段。",
            "G-18 provisional：退出码 0/1/2 = 通过 / 校验失败 / 用法或配置错误。",
            *messages,
        ],
    }
    declared_identities = [
        str(manifest["payload_hash"]),
        resolved_commit,
        str(l2_result["manifest_sha256"]),
        *([head] if head else []),
        *declared_for_records,
    ]
    l2_findings = scan_structure(
        l2_result, location="l2-result.json", declared_identities=declared_identities
    )
    if l2_findings:
        findings.extend(l2_findings)
        l2_result["scan"] = {
            "findings": [item.render() for item in findings],
            "failed": len(findings),
        }

    if findings:
        raise ToolError(EXIT_VALIDATION_FAILED, render_findings(findings))

    if write:
        target_dir = Path(repo_root) / output_dir
        atomic_write_text(
            target_dir / L2_RESULT_FILENAME, canonical_json_dumps(l2_result) + "\n"
        )
        if publish_corpus:
            payload = "".join(
                canonical_json_dumps(record) + "\n" for record in all_corpus
            )
            atomic_write_text(target_dir / CORPUS_FILENAME, payload)

    return ReplayOutcome(
        exit_code=exit_code,
        result=result,
        l2_result=l2_result,
        corpus_records=all_corpus,
        messages=messages,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay_history.py",
        description="L2 历史回放：allowlist extraction → Adapter mapping → sanitized corpus",
    )
    parser.add_argument(
        "--run-manifest",
        required=True,
        metavar="<path>",
        help="replay run manifest（母 Spec C.1 唯一参数），例如 config/contracts/replay-v0.1.json",
    )
    return parser


def _force_utf8_streams() -> None:
    """让诊断文本在 GBK 控制台上仍按 UTF-8 输出（否则中文错误信息不可读）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 非 TextIOWrapper（pytest capture 等）时忽略
            continue


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _force_utf8_streams()
    try:
        outcome = run_replay(manifest_path=Path(args.run_manifest))
    except ToolError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        print("error: 中断", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - 绝不向用户抛 traceback（G-18）
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    totals = outcome.l2_result["totals"]
    print(
        f"replay {outcome.result}: run_id={outcome.l2_result['run_id']} "
        f"records={totals['records']} sources={totals['sources']} "
        f"status={dict(totals['by_status'])} corpus_records={outcome.l2_result['corpus']['records']}"
    )
    print(f"staging: {outcome.l2_result['corpus']['path'] or '(corpus 未发布)'}")
    for message in outcome.messages:
        print(f"note: {message}")
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
