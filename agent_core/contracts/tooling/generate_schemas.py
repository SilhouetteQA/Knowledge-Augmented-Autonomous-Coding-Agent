"""Schema 生成器与 `payload-descriptor.json` 生成。

生成顺序（Master Spec §12.2）：

```text
1. 从 Pydantic 重新生成 JSON Schema（默认渲染到内存，不写工作区）
2. canonicalize schemas
3. 计算各 schema hash 与 schema_set_hash
4. 生成 payload-descriptor.json
5. 校验 descriptor 与实际文件、Rule ID 和 Schema 一致
```

Schema 模式的选择：使用 ``mode="serialization"``。Master Spec §4 把金额的**跨边界**表示
定义为十进制字符串，且 FND-COST-003 拒绝 float 输入；``mode="validation"`` 的 Schema
会为 ``Decimal`` 生成 ``number | string | null``，等于宣称 JSON number 合法，与契约矛盾。
serialization 模式得到 ``string | null``，与契约一致。

`--check`（默认）只比较、绝不写工作区；只有显式 ``--write`` 才落盘。
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pydantic

from agent_core.contracts.tooling.canonical_json import (
    canonical_json_dumps,
    normalize_text,
    read_text,
    schema_content_hash,
    schema_set_hash,
    sha256_hex,
)
from agent_core.contracts.version import (
    CANONICALIZATION_VERSION,
    CONTRACT_FAMILY,
    CONTRACT_VERSION,
    SCHEMA_TOOLING_VERSION,
)

#: Schema ID → (模块, 模型属性名, 快照文件名)。
#: v0.1 全集共六类（Master Spec §3.1）；Spec 04 补入 Evidence 两类，使集合完整。
SCHEMA_REGISTRY: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("Usage", "agent_core.contracts.models.usage", "Usage", "usage.schema.json"),
    ("Cost", "agent_core.contracts.models.cost", "Cost", "cost.schema.json"),
    ("CostSummary", "agent_core.contracts.models.cost", "CostSummary", "cost-summary.schema.json"),
    ("ErrorEnvelope", "agent_core.contracts.models.error", "ErrorEnvelope", "error-envelope.schema.json"),
    (
        "FoundationObservation",
        "agent_core.contracts.models.evidence",
        "FoundationObservation",
        "foundation-observation.schema.json",
    ),
    (
        "EvidenceRecord",
        "agent_core.contracts.models.evidence",
        "EvidenceRecord",
        "evidence-record.schema.json",
    ),
)

SCHEMA_MODE: Final[str] = "serialization"

CONTRACTS_DIR: Final[str] = "agent_core/contracts"
SCHEMAS_DIR: Final[str] = "agent_core/contracts/schemas"
DESCRIPTOR_PATH: Final[str] = "agent_core/contracts/payload-descriptor.json"
CONTRACT_MD_PATH: Final[str] = "agent_core/contracts/contract.md"

#: 规则索引行前缀。``contract.md`` 的规则索引表同时声明 foundation 与 evidence 两族；
#: 只认 ``FND-`` 会让 ``EVD-*`` 静默缺席 descriptor 的 ``normative_rule_set``。
_RULE_LINE_PREFIXES: Final[tuple[str, ...]] = ("| FND-", "| EVD-")


def find_repo_root() -> Path:
    """定位仓库根（``agent_core`` 的父目录）。"""
    return Path(__file__).resolve().parents[3]


def load_model(module_name: str, attribute: str) -> type[pydantic.BaseModel]:
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def render_schemas() -> dict[str, dict]:
    """渲染全部 Schema，返回 ``schema_id -> schema``。"""
    return {
        schema_id: load_model(module_name, attribute).model_json_schema(mode=SCHEMA_MODE)
        for schema_id, module_name, attribute, _ in SCHEMA_REGISTRY
    }


def parse_declared_rules(repo_root: Path) -> list[str]:
    """从 ``contract.md`` 的规则索引表解析已声明 Rule ID。"""
    text = normalize_text(read_text(repo_root / CONTRACT_MD_PATH))
    rules: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(_RULE_LINE_PREFIXES):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0]:
            rules.add(cells[0])
    return sorted(rules)


def build_descriptor(repo_root: Path) -> dict:
    """构造无自引用的 payload descriptor。

    刻意**不**包含：``contract_payload_hash``（自引用）、治理成熟度、仓库 commit、
    Evidence 或时间戳（FND-VER-002 / FND-VER-006）。
    """
    schemas = render_schemas()
    schema_hashes = {schema_id: schema_content_hash(schema) for schema_id, schema in schemas.items()}
    return {
        "contract_version": CONTRACT_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "schema_tooling_version": SCHEMA_TOOLING_VERSION,
        "pydantic_version": pydantic.VERSION,
        "families": [CONTRACT_FAMILY],
        "schema_hashes": schema_hashes,
        "schema_set_hash": schema_set_hash(schema_hashes),
        "normative_rule_set": parse_declared_rules(repo_root),
    }


@dataclass(frozen=True)
class Drift:
    """一处快照漂移。"""

    path: str
    detail: str


def _read_committed_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(normalize_text(read_text(path)))


def check(repo_root: Path) -> list[Drift]:
    """比较渲染结果与已提交快照；返回漂移列表（空表示一致）。"""
    drifts: list[Drift] = []

    for schema_id, _, _, filename in SCHEMA_REGISTRY:
        target = repo_root / SCHEMAS_DIR / filename
        rendered = render_schemas()[schema_id]
        committed = _read_committed_json(target)
        if committed is None:
            drifts.append(Drift(f"{SCHEMAS_DIR}/{filename}", "缺少已提交的 Schema 快照"))
            continue
        if canonical_json_dumps(committed) != canonical_json_dumps(rendered):
            drifts.append(Drift(f"{SCHEMAS_DIR}/{filename}", "Schema 与 Pydantic 重新生成的结果不一致"))

    descriptor_target = repo_root / DESCRIPTOR_PATH
    committed_descriptor = _read_committed_json(descriptor_target)
    if committed_descriptor is None:
        drifts.append(Drift(DESCRIPTOR_PATH, "缺少已提交的 payload descriptor"))
    else:
        rendered_descriptor = build_descriptor(repo_root)
        if canonical_json_dumps(committed_descriptor) != canonical_json_dumps(rendered_descriptor):
            drifts.append(Drift(DESCRIPTOR_PATH, "descriptor 与从模型/contract.md 重新派生的结果不一致"))

    return drifts


def write(repo_root: Path) -> list[str]:
    """显式生成模式：写 Schema 快照与 descriptor。"""
    written: list[str] = []
    schemas_dir = repo_root / SCHEMAS_DIR
    schemas_dir.mkdir(parents=True, exist_ok=True)

    schemas = render_schemas()
    for schema_id, _, _, filename in SCHEMA_REGISTRY:
        target = schemas_dir / filename
        target.write_text(canonical_json_dumps(schemas[schema_id]) + "\n", encoding="utf-8", newline="\n")
        written.append(f"{SCHEMAS_DIR}/{filename}")

    descriptor_target = repo_root / DESCRIPTOR_PATH
    descriptor_target.write_text(
        canonical_json_dumps(build_descriptor(repo_root)) + "\n", encoding="utf-8", newline="\n"
    )
    written.append(DESCRIPTOR_PATH)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 Foundation Contract Schema 与 payload descriptor")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只比较，不写工作区（默认）")
    group.add_argument("--write", action="store_true", help="显式写入快照")
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args(argv)

    repo_root = args.repo_root or find_repo_root()
    if not (repo_root / "agent_core").is_dir():
        print(f"找不到 payload：{repo_root / 'agent_core'}", file=sys.stderr)
        return 2

    if args.write:
        for item in write(repo_root):
            print(f"written  {item}")
        return 0

    drifts = check(repo_root)
    if drifts:
        print("SCHEMA_DRIFT:", file=sys.stderr)
        for drift in drifts:
            print(f"  {drift.path}: {drift.detail}", file=sys.stderr)
        return 1

    descriptor = _read_committed_json(repo_root / DESCRIPTOR_PATH) or {}
    schema_hashes = descriptor.get("schema_hashes", {})
    print(
        f"schema check OK: contract_version={descriptor.get('contract_version')} "
        f"schemas={len(schema_hashes)} "
        f"schema_set_hash={descriptor.get('schema_set_hash', '')[:23]}... "
        f"rules={len(descriptor.get('normative_rule_set', []))}"
    )
    print(f"descriptor sha256: {sha256_hex(canonical_json_dumps(descriptor).encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
