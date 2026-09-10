"""Extension Boundary 的一致性证明。

对应 Master Appendix A.4 的 FND-EXT-001 至 FND-EXT-006。
本文件只依赖 ``agent_core.contracts``，不 import 任何项目模块。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.contracts.conformance.rules import contract_rule
from agent_core.contracts.enums.sources import CostSource, UsageSource
from agent_core.contracts.models.base import (
    EXTENSIONS_MAX_BYTES,
    EXTENSIONS_MAX_DEPTH,
    canonical_json_dumps,
    content_scan_warning_total,
    reset_content_scan_warning_total,
    scan_extension_content,
)
from agent_core.contracts.models.cost import Cost
from agent_core.contracts.models.usage import Usage

UNKNOWN = CostSource.UNKNOWN


def _cost(**extensions: object) -> Cost:
    """构造带 extensions 的 Cost，便于参数化测试。"""
    return Cost(source=UNKNOWN, extensions=extensions)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# FND-EXT-001 / 002：命名空间与 JSON 值域
# --------------------------------------------------------------------------- #


@contract_rule("FND-EXT-001")
@pytest.mark.parametrize(
    "key",
    [
        "wiki.eval_cost",
        "coding.benchmark.stage",
        "provider.openai.request_id",
        "adapter.wiki.source",
        "foundation.reserved_slot",
        "shared.common",
    ],
)
def test_registered_namespaces_accepted(key: str) -> None:
    host, _, _ = key.partition(".")
    _cost(**{key: 1})
    assert host in {"wiki", "coding", "provider", "adapter", "foundation", "shared"}


@contract_rule("FND-EXT-001")
@pytest.mark.parametrize(
    "key",
    [
        "unknown_ns.field",
        "other.field",
        "wiki",  # 单段，缺少子域
        ".wiki.field",  # 空首段
        "Wiki.field",  # 大写
        "wiki..field",  # 空段
        "wiki.9field",  # 段首为数字
        "wiki.field-name",  # 含短横
        "wiki.field name",  # 含空格
        "1wiki.field",
    ],
)
def test_invalid_extension_keys_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        _cost(**{key: 1})


@contract_rule("FND-EXT-002")
@pytest.mark.parametrize(
    "value",
    [
        Decimal("1"),
        dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc),
        dt.date(2026, 9, 11),
        Path("C:/tmp/x"),
        b"bytes",
        {"nested"},
        ("tuple",),
        ValueError("boom"),
    ],
)
def test_non_json_values_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        _cost(**{"wiki.bad": value})


@contract_rule("FND-EXT-002")
def test_nested_non_json_value_rejected() -> None:
    with pytest.raises(ValidationError):
        _cost(**{"wiki.bad": {"deep": [Decimal("1")]}})


@contract_rule("FND-EXT-002")
@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -3, 1.5, "text", [], {}, [1, "a", None], {"k": [1, {"j": None}]}],
)
def test_json_values_accepted(value: object) -> None:
    _cost(**{"wiki.ok": value})


@contract_rule("FND-EXT-002")
def test_non_string_mapping_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _cost(**{"wiki.bad": {1: "x"}})


# --------------------------------------------------------------------------- #
# FND-EXT-003 / 004：容量与深度
# --------------------------------------------------------------------------- #


@contract_rule("FND-EXT-003")
def test_capacity_boundary() -> None:
    payload = "x" * 4096
    under = {"wiki.blob": payload}
    assert len(canonical_json_dumps(under).encode("utf-8")) <= EXTENSIONS_MAX_BYTES
    _cost(**under)

    over = {"wiki.blob": "x" * (EXTENSIONS_MAX_BYTES + 1)}
    with pytest.raises(ValidationError):
        _cost(**over)


@contract_rule("FND-EXT-003")
def test_capacity_measured_on_canonical_json() -> None:
    value = {"b": 1, "a": 2}
    assert canonical_json_dumps(value) == '{"a":2,"b":1}'
    assert canonical_json_dumps({"z": "中文"}) == '{"z":"中文"}'


@contract_rule("FND-EXT-004")
def test_depth_boundary() -> None:
    at_limit = {"wiki.deep": {"a": {"b": {"c": {"d": 1}}}}}
    _cost(**at_limit)

    over_limit = {"wiki.deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}}
    with pytest.raises(ValidationError):
        _cost(**over_limit)


@contract_rule("FND-EXT-004")
def test_root_mapping_is_not_counted_as_depth() -> None:
    """根映射不计层，因此单层标量值深度为 0 而被接受。"""
    _cost(**{"wiki.scalar": 1, "wiki.one_level": {"a": 1}})


# --------------------------------------------------------------------------- #
# FND-EXT-005：敏感内容
# --------------------------------------------------------------------------- #


@contract_rule("FND-EXT-005")
@pytest.mark.parametrize(
    "key",
    [
        "wiki.prompt",
        "wiki.raw_prompt",
        "adapter.system_prompt",
        "adapter.response_body",
        "coding.reasoning_content",
        "coding.tool_traceback",
        "provider.authorization",
        "provider.api_key",
        "adapter.openai_api_key",
        "provider.access_token",
        "provider.client_secret",
        "provider.private_key",
        "provider.password",
    ],
)
def test_sensitive_keys_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        _cost(**{key: "whatever"})


@contract_rule("FND-EXT-005")
@pytest.mark.parametrize(
    "key",
    [
        "adapter.prompt_tokens",
        "adapter.completion_tokens",
        "adapter.total_prompt_token_count",
        "adapter.tokens",
        "adapter.cache_read_tokens",
        "coding.response_count",
        "coding.response_chars",
    ],
)
def test_metric_like_keys_not_false_positives(key: str) -> None:
    """指标名（prompt_tokens / response_chars）不是原始内容，不得误伤。"""
    _cost(**{key: 12})


@contract_rule("FND-EXT-005")
def test_content_heuristics_warn_but_do_not_reject() -> None:
    """内容启发式只是辅助防线：命中告警并计数，但不阻断合法扩展。"""
    reset_content_scan_warning_total()
    before = content_scan_warning_total()

    risky = Cost(
        source=UNKNOWN,
        extensions={"wiki.debug": "C:/Users/example/secret_dir/file.txt"},
    )
    assert isinstance(risky, Cost)

    after = content_scan_warning_total()
    assert after > before, "内容启发式命中必须计入告警计数"

    report = scan_extension_content({"wiki.debug": "Bearer abcdef0123456789"})
    assert report.count >= 1
    assert any("bearer_credential" in item for item in report.findings)


@contract_rule("FND-EXT-005")
def test_clean_content_produces_no_warning() -> None:
    reset_content_scan_warning_total()
    Cost(source=UNKNOWN, extensions={"wiki.mode": "observe", "wiki.stage": "runner"})
    assert content_scan_warning_total() == 0


# --------------------------------------------------------------------------- #
# FND-EXT-006：opaque 语义
# --------------------------------------------------------------------------- #


@contract_rule("FND-EXT-006")
def test_foundation_treats_extensions_as_opaque() -> None:
    """扩展内容不得改变任何规范性字段的取值或校验结果。"""
    plain = Cost(amount=Decimal("1"), currency="USD", source=CostSource.ESTIMATED)
    decorated = Cost(
        amount=Decimal("1"),
        currency="USD",
        source=CostSource.ESTIMATED,
        extensions={"wiki.something": {"nested": [1, 2, 3]}, "adapter.flag": True},
    )

    normative = ("amount", "currency", "source", "pricing_version")
    for field in normative:
        assert getattr(plain, field) == getattr(decorated, field)

    plain_json = plain.model_dump(mode="json")
    decorated_json = decorated.model_dump(mode="json")
    assert {field: decorated_json[field] for field in normative} == {
        field: plain_json[field] for field in normative
    }


@contract_rule("FND-EXT-006")
def test_extensions_are_serializable_round_trip() -> None:
    extensions = {"wiki.flag": True, "adapter.count": 3, "coding.tags": ["a", "b"]}
    usage = Usage(source=UsageSource.ESTIMATED, output_tokens=5, extensions=extensions)
    assert json.loads(usage.model_dump_json())["extensions"] == extensions
    assert Usage.model_validate(usage.model_dump(mode="json")).extensions == extensions


@contract_rule("FND-EXT-003")
def test_size_limit_constant_is_16_kib() -> None:
    assert EXTENSIONS_MAX_BYTES == 16 * 1024


@contract_rule("FND-EXT-004")
def test_depth_limit_constant_is_4() -> None:
    assert EXTENSIONS_MAX_DEPTH == 4


@contract_rule("FND-EXT-002")
def test_no_project_modules_imported() -> None:
    """共享 conformance 不得依赖任何项目包。"""
    for forbidden in ("arknights_wiki", "benchmark", "tools", "agent"):
        assert forbidden not in sys.modules or forbidden == "agent"
