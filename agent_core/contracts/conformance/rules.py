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


__all__ = [
    "RULE_ID_PATTERN",
    "RULE_ATTRIBUTE",
    "contract_rule",
    "is_contract_rule_id",
    "rules_of",
    "collect_rule_usages",
    "unimplemented_rules",
]
