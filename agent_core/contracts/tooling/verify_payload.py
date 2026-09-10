"""Payload 自洽校验：白名单、descriptor 派生一致性、无自引用、Payload Hash。

校验项对应 Master Spec Appendix A.1：

```text
FND-PKG-001  agent_core 只含根 __init__.py 与 contracts/**
FND-PKG-002  项目 Adapter 不在 agent_core 之下
FND-VER-002  Payload 不含自己的 contract_payload_hash
FND-VER-003  descriptor 可在 payload 哈希之前完全派生
FND-VER-004  contract-manifest.json 位于它所标识的 Payload 之外
FND-VER-006  治理状态不改变 payload 身份
```

同一函数 :func:`verify_tree` 既用于 Wiki canonical 端自检，也用于 Coding 消费端
在原子替换前验证解包结果（Master Spec §12.7）。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pydantic

from agent_core.contracts.tooling import generate_schemas as gs
from agent_core.contracts.tooling.canonical_json import (
    EXCLUDED_DIR_NAMES,
    PAYLOAD_CONTRACTS_PREFIX,
    PAYLOAD_PACKAGE,
    PAYLOAD_TOP_LEVEL_INIT,
    canonical_file_bundle_digest,
    canonical_json_dumps,
    iter_payload_files,
    normalize_relative_path,
    read_text,
    schema_set_hash,
    sha256_hex,
)
from agent_core.contracts.version import (
    CANONICALIZATION_VERSION,
    CONTRACT_VERSION,
    PYDANTIC_PIN,
)

#: 治理成熟度键：出现即违反 FND-VER-006。
GOVERNANCE_KEYS: Final[frozenset[str]] = frozenset(
    {"maturity", "governance", "lifecycle", "state", "status"}
)

#: 自引用键：出现即违反 FND-VER-002。
SELF_REFERENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"contract_payload_hash", "payload_hash", "self_hash"}
)


@dataclass
class VerifyReport:
    """校验结果。``errors`` 为空即通过。"""

    repo_root: Path
    payload_hash: str = ""
    descriptor_hash: str = ""
    schema_set_hash: str = ""
    file_count: int = 0
    per_file_hashes: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    diverged: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "diverged": self.diverged,
            "payload_hash": self.payload_hash,
            "descriptor_hash": self.descriptor_hash,
            "schema_set_hash": self.schema_set_hash,
            "file_count": self.file_count,
            "errors": self.errors,
        }


def check_package_layout(repo_root: Path) -> list[str]:
    """FND-PKG-001 / FND-PKG-002：`agent_core` 顶层白名单。"""
    errors: list[str] = []
    package_dir = repo_root / PAYLOAD_PACKAGE
    if not package_dir.is_dir():
        return [f"FND-PKG-001: 找不到 payload 根包 {PAYLOAD_PACKAGE}/"]

    for child in sorted(package_dir.iterdir()):
        if child.name in EXCLUDED_DIR_NAMES:
            continue
        if child.is_dir():
            if child.name == "contracts":
                continue
            errors.append(f"FND-PKG-001: agent_core 下出现白名单外的子包 {child.name}/")
        elif child.name != "__init__.py":
            errors.append(f"FND-PKG-001: agent_core 根下出现白名单外的文件 {child.name}")
    return errors


def check_file_allowlist(repo_root: Path) -> list[str]:
    """FND-PKG-001：payload 内每个文件的路径都在白名单内。"""
    errors: list[str] = []
    for path in iter_payload_files(repo_root):
        rel = normalize_relative_path(path, repo_root)
        if rel == PAYLOAD_TOP_LEVEL_INIT or rel.startswith(PAYLOAD_CONTRACTS_PREFIX):
            continue
        errors.append(f"FND-PKG-001: payload 白名单外文件 {rel}")
    return errors


def check_no_manifest_inside_payload(repo_root: Path) -> list[str]:
    """FND-VER-004：`contract-manifest.json` 必须在 payload 之外。"""
    for path in iter_payload_files(repo_root):
        if path.name == "contract-manifest.json":
            rel = normalize_relative_path(path, repo_root)
            return [f"FND-VER-004: contract-manifest.json 出现在 payload 内（{rel}）"]
    return []


def _load_descriptor(repo_root: Path) -> tuple[dict | None, list[str]]:
    target = repo_root / gs.DESCRIPTOR_PATH
    if not target.exists():
        return None, [f"缺少 payload descriptor：{gs.DESCRIPTOR_PATH}"]
    try:
        return json.loads(read_text(target)), []
    except json.JSONDecodeError as exc:
        return None, [f"descriptor 不是合法 JSON：{exc}"]


def check_descriptor(repo_root: Path) -> tuple[dict | None, list[str]]:
    """descriptor 的自引用、治理键、版本与派生一致性。"""
    descriptor, errors = _load_descriptor(repo_root)
    if descriptor is None:
        return None, errors

    keys = set(descriptor)
    self_ref = sorted(keys & SELF_REFERENCE_KEYS)
    if self_ref:
        errors.append(f"FND-VER-002: descriptor 包含自引用键 {self_ref}")

    governance = sorted(keys & GOVERNANCE_KEYS)
    if governance:
        errors.append(f"FND-VER-006: descriptor 包含治理状态键 {governance}")

    if descriptor.get("contract_version") != CONTRACT_VERSION:
        errors.append(
            f"版本不一致：descriptor={descriptor.get('contract_version')!r} "
            f"vs version.py={CONTRACT_VERSION!r}"
        )
    if descriptor.get("canonicalization_version") != CANONICALIZATION_VERSION:
        errors.append(
            f"canonicalization_version 不一致：{descriptor.get('canonicalization_version')!r} "
            f"vs {CANONICALIZATION_VERSION!r}"
        )
    if descriptor.get("pydantic_version") != pydantic.VERSION:
        errors.append(
            f"pydantic_version 不一致：{descriptor.get('pydantic_version')!r} "
            f"vs 运行时 {pydantic.VERSION!r}"
        )
    if descriptor.get("pydantic_version") != PYDANTIC_PIN:
        errors.append(
            f"FND-VER-001: 运行时 Pydantic 与 v0.1 固定版本不符："
            f"{pydantic.VERSION!r} vs PYDANTIC_PIN={PYDANTIC_PIN!r}"
        )

    schema_hashes = descriptor.get("schema_hashes") or {}
    if not isinstance(schema_hashes, dict) or not schema_hashes:
        errors.append("descriptor.schema_hashes 缺失或为空")
    else:
        recomputed_set = schema_set_hash(schema_hashes)
        if descriptor.get("schema_set_hash") != recomputed_set:
            errors.append("descriptor.schema_set_hash 与 schema_hashes 不自洽")

    declared = gs.parse_declared_rules(repo_root)
    rule_set = descriptor.get("normative_rule_set")
    if rule_set != declared:
        missing = sorted(set(declared) - set(rule_set or []))
        extra = sorted(set(rule_set or []) - set(declared))
        errors.append(
            f"FND-VER-003: descriptor.normative_rule_set 与 contract.md 不一致；"
            f"缺失={missing} 多余={extra}"
        )

    return descriptor, errors


def verify_tree(repo_root: Path, expected_payload_hash: str | None = None) -> VerifyReport:
    """对一个 payload 树做完整校验。

    :param expected_payload_hash: 消费端传入生产端声明的 Payload Hash；
        不一致即标记 ``DIVERGED``（同版本不同载荷）并计入 errors。
    """
    report = VerifyReport(repo_root=repo_root)

    report.errors.extend(check_package_layout(repo_root))
    report.errors.extend(check_file_allowlist(repo_root))
    report.errors.extend(check_no_manifest_inside_payload(repo_root))

    descriptor, descriptor_errors = check_descriptor(repo_root)
    report.errors.extend(descriptor_errors)

    payload_hash, per_file = canonical_file_bundle_digest(repo_root)
    report.payload_hash = payload_hash
    report.per_file_hashes = per_file
    report.file_count = len(per_file)

    if descriptor is not None:
        report.descriptor_hash = sha256_hex(canonical_json_dumps(descriptor).encode("utf-8"))
        report.schema_set_hash = str(descriptor.get("schema_set_hash", ""))

    # Schema 快照必须能由模型确定性重放
    for drift in gs.check(repo_root):
        report.errors.append(f"Schema 漂移：{drift.path} — {drift.detail}")

    if expected_payload_hash is not None and expected_payload_hash != payload_hash:
        report.errors.append(
            f"DIVERGED/FAILED: 期望 Payload Hash {expected_payload_hash} "
            f"与实际 {payload_hash} 不一致（相同 contract version 下的载荷分歧）"
        )
        report.diverged = True

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 Foundation Contract Payload 自洽性")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--expect-payload-hash", default=None)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    repo_root = args.repo_root or gs.find_repo_root()
    report = verify_tree(repo_root, expected_payload_hash=args.expect_payload_hash)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    elif report.ok:
        print("payload verify OK")
        print(f"  contract_version : {CONTRACT_VERSION}")
        print(f"  files            : {report.file_count}")
        print(f"  payload_hash     : {report.payload_hash}")
        print(f"  descriptor_hash  : {report.descriptor_hash}")
        print(f"  schema_set_hash  : {report.schema_set_hash}")
    else:
        print("payload verify FAILED:", file=sys.stderr)
        for error in report.errors:
            print(f"  {error}", file=sys.stderr)

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
