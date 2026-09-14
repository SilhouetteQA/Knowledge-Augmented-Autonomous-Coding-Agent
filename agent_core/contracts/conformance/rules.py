"""规范规则标识（Contract Rule ID）的元数据机制。

Rule ID 采用 ``<FAMILY>-<SUBDOMAIN>-<NNN>`` 形状（Master Spec §0.3），
编号发布后**永不复用**；废弃规则保留编号并标记 ``DEPRECATED``。

共享一致性测试必须使用机器可读 metadata 关联 Rule ID：

```python
@contract_rule("FND-COST-001")
def test_unknown_cost_is_not_zero():
    ...
```

测试函数名只是人类可读信息，**不能**代替 Rule ID。
"""
from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from types import ModuleType
from typing import Final, TypeVar

#: ``<FAMILY>-<SUBDOMAIN>-<NNN>``，例如 ``FND-COST-001`` / ``EVD-PUB-004``。
RULE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9]+-\d{3}$")

#: 测试函数上承载 Rule ID 的 metadata 属性名。
RULE_ATTRIBUTE: Final[str] = "contract_rules"

F = TypeVar("F", bound=Callable[..., object])


def is_contract_rule_id(value: str) -> bool:
    """判断字符串是否为合法 Rule ID 形状。"""
    return isinstance(value, str) and bool(RULE_ID_PATTERN.match(value))


def contract_rule(*rule_ids: str) -> Callable[[F], F]:
    """把规范 Rule ID 绑定到测试函数上。

    :param rule_ids: 一个或多个 Rule ID；允许一条规则有多个测试，
        也允许一个测试关联紧密相关的多条规则。
    :raises ValueError: 未给出 Rule ID，或某个 ID 形状非法。
    """
    if not rule_ids:
        raise ValueError("contract_rule 至少需要一个 Rule ID")

    invalid = [rid for rid in rule_ids if not is_contract_rule_id(rid)]
    if invalid:
        raise ValueError(
            f"非法的 Rule ID {invalid}；期望形状 <FAMILY>-<SUBDOMAIN>-<NNN>，例如 FND-COST-001"
        )
    normalized = tuple(dict.fromkeys(rule_ids))

    def decorate(func: F) -> F:
        existing = tuple(getattr(func, RULE_ATTRIBUTE, ()))
        merged = existing + tuple(rid for rid in normalized if rid not in existing)
        setattr(func, RULE_ATTRIBUTE, merged)
        return func

    return decorate


def rules_of(target: object) -> tuple[str, ...]:
    """读取绑定在某个对象上的 Rule ID。"""
    return tuple(getattr(target, RULE_ATTRIBUTE, ()))


def collect_rule_usages(modules: Iterable[ModuleType]) -> dict[str, list[str]]:
    """扫描模块内的顶层函数，返回 ``rule_id -> [测试函数名, ...]``。

    用于生成 Rule traceability 预览；某条规则没有任何测试时不会出现在结果中，
    调用方据此发现覆盖缺口（Spec 09）。
    """
    usages: dict[str, list[str]] = {}
    for module in modules:
        for name, member in vars(module).items():
            if not inspect.isfunction(member):
                continue
            for rule_id in rules_of(member):
                usages.setdefault(rule_id, []).append(f"{module.__name__}::{name}")
    for names in usages.values():
        names.sort()
    return usages


def unimplemented_rules(declared: Iterable[str], modules: Iterable[ModuleType]) -> list[str]:
    """返回已声明但当前没有任何测试落点的 Rule ID。"""
    covered = set(collect_rule_usages(modules))
    return sorted(rid for rid in set(declared) if rid not in covered)


# --------------------------------------------------------------------------- #
# 规则 metadata（Spec 09）
# --------------------------------------------------------------------------- #

#: Appendix A 规范规则全集（Rule ID → normative statement，英文）。
#:
#: 这是 payload descriptor ``normative_rule_set`` 与 ``contract.md`` 规则索引的
#: 机器可读 statement 来源，供 traceability 检查与 Semantic Coverage Matrix 使用。
#: statement 与母 Spec Appendix A 逐项对应；此处只保留 payload 规则（68 条）与
#: 门禁/协调规则（FND-REG-* / FND-REL-*，共 11 条），共 79 条。
RULE_REGISTRY: dict[str, str] = {
    "FND-PKG-001": "Phase 1 agent_core MUST only contain root __init__.py and contracts/**.",
    "FND-PKG-002": "Project Adapter MUST NOT reside under agent_core.",
    "FND-PKG-003": "Built project distributions MUST expose agent_core.contracts and packaged schemas.",
    "FND-PKG-004": "agent_core.__init__ MUST NOT re-export contract/runtime symbols.",
    "FND-VER-001": "Phase 1 MUST use one lockstep Contract Set version; any semantic change requires a minor bump.",
    "FND-VER-002": "Contract Payload MUST NOT contain its own contract_payload_hash.",
    "FND-VER-003": "payload-descriptor.json MUST be fully derivable before payload hashing.",
    "FND-VER-004": "contract-manifest.json MUST reside outside the payload it identifies.",
    "FND-VER-005": "Evidence Manifest MUST bind an existing Candidate commit and Payload Hash.",
    "FND-VER-006": "Governance lifecycle changes MUST NOT change payload identity unless normative content changes.",
    "FND-MAP-001": "Adapter MUST map provable facts, not Legacy convenience defaults.",
    "FND-MAP-002": "Facts extraction MUST preserve presence and provenance and MUST NOT reconstruct missing facts from aggregate defaults.",
    "FND-USAGE-001": "Known token counts MUST be non-negative; explicit zero and unknown null are distinct.",
    "FND-USAGE-002": "Provider-reported total_tokens MUST be preserved exactly even when it differs from visible components.",
    "FND-USAGE-003": "Missing total_tokens MUST remain null; Adapter MUST NOT synthesize input plus output.",
    "FND-COST-001": "Unknown cost MUST be amount=null, source=unknown; it MUST NOT be represented by zero.",
    "FND-COST-002": "Cost aggregation MUST NOT perform implicit FX; multiple non-null currencies MUST raise currency mismatch.",
    "FND-COST-003": "Monetary values MUST use Decimal and serialize as decimal strings; float input is rejected.",
    "FND-COST-004": "Non-null currency MUST be an uppercase three-letter code.",
    "FND-COST-005": "source, amount, currency and pricing_version MUST satisfy the source invariant matrix.",
    "FND-COST-006": "source=price_table MUST include a reproducible pricing_version.",
    "FND-COST-007": "Zero is known only when provider or confirmed pricing evidence explicitly proves zero for an observed call.",
    "FND-COST-008": "Unknown amount MAY preserve a known currency context without becoming known cost.",
    "FND-COST-009": "Raw Cost MUST remain in its source currency and MUST NOT claim converted/normalized semantics.",
    "FND-COST-010": "Cost MUST NOT contain task/span/report scope; scope belongs to the consuming family.",
    "FND-COST-011": "CostSource describes epistemic provenance, not the storage location of pricing data.",
    "FND-COST-012": "Legacy numeric zero alone MUST NOT prove a true zero Cost.",
    "FND-COST-013": "Pricing-derived known amounts MUST reference a canonical pricing snapshot hash.",
    "FND-CSUM-001": "All component counts MUST be non-negative.",
    "FND-CSUM-002": "component_count == known_component_count + unknown_component_count.",
    "FND-CSUM-003": "complete == (unknown_component_count == 0).",
    "FND-CSUM-004": "Zero known components require known_amount=null.",
    "FND-CSUM-005": "One or more known components require non-negative known_amount and non-null currency.",
    "FND-CSUM-006": "A true zero Cost is a known component.",
    "FND-CSUM-007": "All non-null component currencies MUST be identical.",
    "FND-CSUM-008": "Null currency does not itself create mismatch, but an unknown amount still makes the summary incomplete.",
    "FND-CSUM-009": "Empty input MUST yield counts 0/0/0, null amount/currency and complete=true.",
    "FND-CSUM-010": "CostSummary MUST NOT declare a single source or pricing_version.",
    "FND-CSUM-011": "Summary MUST be built from observed components and MUST NOT reverse-engineer them from a Legacy total.",
    "FND-ERR-001": "ErrorEnvelope is a boundary DTO, not an exception class or internal control-flow framework.",
    "FND-ERR-002": "Machine behavior MUST use stable code; message MUST NOT drive logic.",
    "FND-ERR-003": "Foundation v0.1 errors MUST have retryable=false.",
    "FND-ERR-004": "Error details MUST be structured, sanitized and bounded; exception objects and tracebacks are forbidden.",
    "FND-ERR-005": "ErrorEnvelope MAY be produced only at declared serialization, API/IPC/MCP, Adapter or artifact boundaries.",
    "FND-EXT-001": "Every extension key MUST use a registered namespace and valid dotted identifier.",
    "FND-EXT-002": "Extension values MUST be recursively JSON-compatible.",
    "FND-EXT-003": "Canonical serialized extensions MUST be at most 16 KiB.",
    "FND-EXT-004": "Extension value nesting MUST be at most four container levels, excluding the root mapping.",
    "FND-EXT-005": "Secrets, raw prompts/responses, tracebacks, credentials and unredacted paths MUST NOT enter extensions.",
    "FND-EXT-006": "Foundation logic MUST treat project/provider extensions as opaque and MUST NOT change normative behavior from them.",
    "FND-MODE-001": "off, observe and strict have the exact semantics defined in section 10.",
    "FND-MODE-002": "Observe and strict MUST execute the same extractor and Adapter; only failure policy differs.",
    "FND-MODE-003": "Invalid AGENT_CONTRACT_MODE MUST fail configuration validation and MUST NOT silently become off.",
    "FND-MODE-004": "Observe mapping failure MUST preserve Legacy behavior and emit failed validation evidence.",
    "EVD-RUN-001": "Controlled smoke, replay and strict commands MUST receive an explicit safe run_id.",
    "EVD-RUN-002": "Every EvidenceRecord MUST bind contract version, Payload Hash and verified repository commit.",
    "EVD-SINK-001": "EvidenceSink MUST NOT silently discard a valid record.",
    "EVD-SINK-002": "Sink failure in observe MUST NOT change the business result.",
    "EVD-SINK-003": "Any sink failure invalidates that run as Cycle Evidence.",
    "EVD-SINK-004": "File event publication MUST be atomic and concurrent-writer safe.",
    "EVD-DATA-001": "Staging Evidence MUST exclude raw business content and obey allowlist, JSON and 64 KiB constraints.",
    "EVD-PUB-001": "Git release artifacts MUST be constructed from explicit allowlists.",
    "EVD-PUB-002": "Raw prompts, responses, reasoning, code bodies, diffs and raw traces MUST NOT enter releases.",
    "EVD-PUB-003": "Credentials, environment dumps and private endpoints MUST NOT enter releases.",
    "EVD-PUB-004": "Replay corpus MUST contain only fields needed to reproduce contract semantics.",
    "EVD-PUB-005": "Restricted evidence MUST be labeled REPRODUCTION_RESTRICTED.",
    "EVD-PUB-006": "Published evidence MUST bind version, Payload Hash and Candidate commit.",
    "EVD-PUB-007": "Raw temporary evidence MUST have an explicit retention decision.",
    "FND-REL-001": "Phase 1 has one canonical Cycle Report truth source: Wiki.",
    "FND-REL-002": "Coding Evidence B MUST NOT be required to duplicate the final Cycle Report.",
    "FND-REL-003": "Coordination PASS is not Cycle COMPLETE; COMPLETE requires merged canonical finalization.",
    "FND-REL-004": "Finalization C MUST contain only allowlisted coordination artifacts.",
    "FND-REL-005": "Finalization artifacts MUST NOT reference C's own commit SHA.",
    "FND-REL-006": "B_wiki to C_wiki MUST NOT mutate payload, Adapter, tests, business code or published Evidence.",
    "FND-REG-001": "Every previously passing test nodeid MUST remain non-failing and non-skipped.",
    "FND-REG-002": "Only registered failure nodeids with identical normalized fingerprints may remain failing.",
    "FND-REG-003": "A registered failure that becomes PASS is reported for review, not treated as Cycle failure or silently removed.",
    "FND-REG-004": "New shared conformance and project Adapter tests MUST all pass.",
    "FND-REG-005": "Cycle quality claims MUST use deterministic non-intrusion; non-reproducible historical benchmark values are reference only.",
}

#: 本 Spec 明确 defer 到后续 Spec 的 payload 规则（traceability 白名单，不算“无测试”失败）。
#:
#: - ``FND-PKG-003`` → Spec 10（clean wheel smoke）
#: - ``EVD-PUB-002/003/004/006/007`` → Spec 12（replay corpus）/ Spec 14（publication）
DEFERRED_PAYLOAD_RULES: tuple[str, ...] = (
    "FND-PKG-003",
    "EVD-PUB-002",
    "EVD-PUB-003",
    "EVD-PUB-004",
    "EVD-PUB-006",
    "EVD-PUB-007",
)


def rule_statement(rule_id: str) -> str:
    """返回某 Rule ID 的 normative statement；未知 ID 抛 ``KeyError``。"""
    return RULE_REGISTRY[rule_id]


def registered_rule_ids() -> frozenset[str]:
    """返回 RULE_REGISTRY 中登记的全部 Rule ID。"""
    return frozenset(RULE_REGISTRY)


__all__ = [
    "RULE_ID_PATTERN",
    "RULE_ATTRIBUTE",
    "RULE_REGISTRY",
    "DEFERRED_PAYLOAD_RULES",
    "contract_rule",
    "is_contract_rule_id",
    "rule_statement",
    "registered_rule_ids",
    "rules_of",
    "collect_rule_usages",
    "unimplemented_rules",
]

