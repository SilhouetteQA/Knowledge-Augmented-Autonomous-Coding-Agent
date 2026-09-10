"""``ContractMode`` 的一致性证明。

对应 Master Appendix A.4 的 FND-MODE-001 至 FND-MODE-004。

``FND-MODE-002``（observe 与 strict 共用同一 extractor / Adapter）与
``FND-MODE-004``（observe 映射失败不改变 Legacy 行为）的完整证明分别在
项目 Adapter 层完成（Spec 05 / 06 / 07 / 08 与 Spec 09 的 invariance harness）；
本文件只证明模式值域、配置入口与失败策略在契约层的可判定性。

本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

from collections.abc import Mapping

import pytest

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.modes import (
    CONTRACT_MODE_ENV,
    ContractMode,
    ContractModeConfigError,
    parse_contract_mode,
    resolve_contract_mode,
)


@contract_rule("FND-MODE-001")
def test_mode_values_are_exactly_specified() -> None:
    """off / observe / strict 三个值的字面量是契约的一部分。"""
    assert {mode.value for mode in ContractMode} == {"off", "observe", "strict"}
    assert ContractMode("off") is ContractMode.OFF
    assert ContractMode("observe") is ContractMode.OBSERVE
    assert ContractMode("strict") is ContractMode.STRICT


@contract_rule("FND-MODE-001")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", ContractMode.OFF),
        ("observe", ContractMode.OBSERVE),
        ("strict", ContractMode.STRICT),
        ("OBSERVE", ContractMode.OBSERVE),
        ("  strict  ", ContractMode.STRICT),
    ],
)
def test_mode_parsing(raw: str, expected: ContractMode) -> None:
    assert parse_contract_mode(raw) is expected


@contract_rule("FND-MODE-001")
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_mode_defaults_to_off(raw: str | None) -> None:
    """未配置时默认 off：不介入任何业务路径，也不产生 Evidence。"""
    assert parse_contract_mode(raw) is ContractMode.OFF


@contract_rule("FND-MODE-003")
@pytest.mark.parametrize(
    "raw",
    ["on", "true", "1", "observ", "strict!", "off,observe", "observe strict", "STRICTX", "none"],
)
def test_invalid_mode_fails_loudly(raw: str) -> None:
    """非法配置必须明确报错，**禁止**静默降级为 off。"""
    with pytest.raises(ContractModeConfigError) as excinfo:
        parse_contract_mode(raw)

    assert CONTRACT_MODE_ENV in str(excinfo.value)
    assert "off" in str(excinfo.value)


@contract_rule("FND-MODE-003")
def test_config_error_is_not_error_envelope() -> None:
    """配置错误是普通异常，不是 ErrorEnvelope 的替代品。"""
    from agent_core.contracts.models.error import ErrorEnvelope

    with pytest.raises(ContractModeConfigError) as excinfo:
        parse_contract_mode("nope")

    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, ErrorEnvelope)


@contract_rule("FND-MODE-001")
def test_resolve_reads_only_the_declared_env_entry() -> None:
    """唯一配置入口是 AGENT_CONTRACT_MODE。"""
    assert resolve_contract_mode({CONTRACT_MODE_ENV: "observe"}) is ContractMode.OBSERVE
    assert resolve_contract_mode({}) is ContractMode.OFF
    # 形状相近的其它变量不得生效
    assert resolve_contract_mode({"AGENT_CONTRACT_MODE ": "strict"}) is ContractMode.OFF
    assert resolve_contract_mode({"CONTRACT_MODE": "strict"}) is ContractMode.OFF


@contract_rule("FND-MODE-001")
def test_resolve_accepts_injected_mapping() -> None:
    env: Mapping[str, str] = {CONTRACT_MODE_ENV: "strict"}
    assert resolve_contract_mode(env) is ContractMode.STRICT


@contract_rule("FND-MODE-002")
def test_observe_and_strict_are_distinct_modes() -> None:
    """两者只差失败策略；模式标识必须可区分，供 Adapter 决定是否 fail-fast。

    同一 extractor / mapping 的共用性由项目 Adapter 层的
    ``FND-MODE-002`` 测试证明（Spec 05–09）。
    """
    assert ContractMode.OBSERVE is not ContractMode.STRICT
    assert ContractMode.OFF not in {ContractMode.OBSERVE, ContractMode.STRICT}


@contract_rule("FND-MODE-001")
def test_mode_is_a_string_enum_for_stable_serialization() -> None:
    assert isinstance(ContractMode.OBSERVE, str)
    assert str(ContractMode.OBSERVE) == "observe"
    assert repr(ContractMode.OFF) in {"<ContractMode.OFF: 'off'>", "ContractMode.OFF"}
