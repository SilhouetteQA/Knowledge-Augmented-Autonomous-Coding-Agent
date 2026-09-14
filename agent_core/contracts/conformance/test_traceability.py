"""规则 traceability 检查（Spec 09）。

验证三件事，且**不 import 任何项目模块**（Spec 09 硬性约束）：

1. ``payload-descriptor.json.normative_rule_set`` 的每条规则都在 ``RULE_REGISTRY``
   里有 machine-readable statement。
2. conformance 测试的 ``@contract_rule`` 引用不超出 ``RULE_REGISTRY``（无孤儿引用）。
3. payload 规则全集 = conformance 落点 + 项目层声明落点（``PROJECT_SCOPED_RULES``）
   + deferral 白名单（``DEFERRED_PAYLOAD_RULES``），缺一即失败。

项目层规则的落点（FND-MAP / EVD-SINK-002..004 / FND-MODE-004）由项目测试负责，
conformance 层无法 import 项目模块去验证它们，因此以 ``PROJECT_SCOPED_RULES``
声明，并由两仓各自的 ``tests/contracts/test_test_baseline.py`` 反向核验真实落点。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from agent_core.contracts.conformance.rules import (
    DEFERRED_PAYLOAD_RULES,
    RULE_REGISTRY,
    collect_rule_usages,
)

CONFORMANCE_DIR = Path(__file__).resolve().parent
DESCRIPTOR_PATH = CONFORMANCE_DIR.parent / "payload-descriptor.json"

#: 项目层负责落点的 payload 规则（conformance 层无法 import 项目验证，故声明）。
PROJECT_SCOPED_RULES = frozenset(
    {
        "FND-MAP-001",
        "FND-MAP-002",
        "FND-MODE-004",
        "EVD-SINK-002",
        "EVD-SINK-003",
        "EVD-SINK-004",
    }
)


def _payload_rule_ids() -> frozenset[str]:
    data = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    return frozenset(data["normative_rule_set"])


def _conformance_rule_usages() -> dict[str, list[str]]:
    modules = [
        f"agent_core.contracts.conformance.{p.stem}"
        for p in sorted(CONFORMANCE_DIR.glob("test_*.py"))
        if p.stem != "test_traceability"
    ]
    return collect_rule_usages([importlib.import_module(m) for m in modules])


def test_registry_covers_every_payload_rule_with_statement() -> None:
    """descriptor 的每条 payload 规则在 RULE_REGISTRY 里有 statement。"""
    payload = _payload_rule_ids()
    assert payload <= frozenset(RULE_REGISTRY), (
        f"payload 规则缺少 statement：{sorted(payload - frozenset(RULE_REGISTRY))}"
    )


def test_registry_has_no_rule_outside_payload_and_gates() -> None:
    """RULE_REGISTRY 只含 payload 规则 + 门禁/协调规则（FND-REG/FND-REL），无杂项。"""
    payload = _payload_rule_ids()
    allowed_extra = {rid for rid in RULE_REGISTRY if rid.startswith(("FND-REG-", "FND-REL-"))}
    assert frozenset(RULE_REGISTRY) == payload | allowed_extra


def test_conformance_has_no_orphan_rule_reference() -> None:
    """conformance 测试引用的每个 Rule ID 都在 RULE_REGISTRY 里登记。"""
    usages = _conformance_rule_usages()
    orphan = set(usages) - frozenset(RULE_REGISTRY)
    assert not orphan, f"conformance 引用了未登记规则：{sorted(orphan)}"


def test_conformance_only_references_payload_rules() -> None:
    """conformance 层只引用 payload 规则，不引用门禁/协调规则。"""
    usages = _conformance_rule_usages()
    gate_rules = {rid for rid in usages if rid.startswith(("FND-REG-", "FND-REL-"))}
    assert not gate_rules, f"conformance 越界引用门禁规则：{sorted(gate_rules)}"


def test_payload_rules_all_have_a_landing() -> None:
    """每条 payload 规则至少一个落点（conformance 或项目声明或 deferral）。"""
    payload = _payload_rule_ids()
    conformance_covered = set(_conformance_rule_usages())
    required = payload - set(DEFERRED_PAYLOAD_RULES) - PROJECT_SCOPED_RULES
    missing = required - conformance_covered
    assert not missing, f"payload 规则无落点：{sorted(missing)}"


def test_deferral_and_project_scopes_partition_payload() -> None:
    """deferral 与项目声明与 conformance 覆盖互不重叠。"""
    conformance_covered = set(_conformance_rule_usages())
    overlap = conformance_covered & (set(DEFERRED_PAYLOAD_RULES) | PROJECT_SCOPED_RULES)
    assert not overlap, f"规则同时被 conformance 覆盖又被声明/defer：{sorted(overlap)}"
