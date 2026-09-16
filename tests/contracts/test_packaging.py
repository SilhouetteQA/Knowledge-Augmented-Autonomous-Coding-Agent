"""Spec 10 — Packaging 与发行包 smoke（项目契约测试）。

规范规则落点：

- ``FND-PKG-003``：Built project distributions MUST expose ``agent_core.contracts``
  and packaged schemas. —— 本 Spec 解除该规则的 deferral，落点在本文件（项目层，
  因为"发行包"是仓库特有的 packaging 事实）。
- ``FND-PKG-001`` / ``FND-PKG-004`` 的规范性落点在 payload conformance
  （``test_versioning.py``）。此处只补充**发行包侧**断言：wheel 中出现的
  ``agent_core`` 内容同样不得越出 Phase 1 白名单。

本文件不 import 任何项目业务模块（``agent`` / ``benchmark`` / ``tools``）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from importlib.resources import files
from pathlib import Path

import pydantic
import pytest

from agent_core.contracts.conformance.rules import contract_rule

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: v0.1 契约身份（lockstep Contract Set）。
CONTRACT_VERSION = "0.1.0"
CANONICALIZATION_VERSION = "1"
PINNED_PYDANTIC = "2.13.4"
EXPECTED_MODELS = frozenset(
    {
        "Usage",
        "Cost",
        "CostSummary",
        "ErrorEnvelope",
        "FoundationObservation",
        "EvidenceRecord",
    }
)
EXPECTED_RULE_COUNT = 68

#: 随 wheel 发布的 payload 非 Python 文件。
PAYLOAD_SCHEMA_FILES = (
    "cost-summary.schema.json",
    "cost.schema.json",
    "error-envelope.schema.json",
    "evidence-record.schema.json",
    "foundation-observation.schema.json",
    "usage.schema.json",
)
PAYLOAD_NON_PYTHON_FILES = ("contract.md", "payload-descriptor.json")

#: 本仓库发行包必须包含的顶层包。
PROJECT_PACKAGES = ("adapters", "agent", "agent_core", "benchmark", "tools")
PROJECT_DISCOVERY_PATTERNS = ("agent*", "adapters*", "agent_core*", "benchmark*", "tools*")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _descriptor() -> dict:
    return json.loads(
        (files("agent_core.contracts") / "payload-descriptor.json").read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------- #
# packaging 声明
# --------------------------------------------------------------------------- #


def test_build_backend_is_declared() -> None:
    """必须有显式 [build-system]，不得依赖环境里恰好存在的 setuptools。"""
    build_system = _pyproject().get("build-system")
    assert build_system, "pyproject.toml 缺少 [build-system]"
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any(req.startswith("setuptools") for req in build_system["requires"])


def test_pydantic_is_pinned_exactly() -> None:
    """主依赖必须精确固定 pydantic==2.13.4，且与 payload descriptor 一致。"""
    dependencies = _pyproject()["project"]["dependencies"]
    pins = [dep for dep in dependencies if dep.replace(" ", "").startswith("pydantic==")]
    assert pins == [f"pydantic=={PINNED_PYDANTIC}"], f"pydantic 未精确固定：{dependencies}"
    assert _descriptor()["pydantic_version"] == PINNED_PYDANTIC
    assert pydantic.VERSION == PINNED_PYDANTIC, (
        f"环境 pydantic {pydantic.VERSION} 与契约固定值 {PINNED_PYDANTIC} 不一致"
    )


def test_package_discovery_is_explicit_and_covers_every_package() -> None:
    """不得依赖 setuptools flat-layout 自动发现（它会排除 tools / benchmark）。"""
    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    for pattern in PROJECT_DISCOVERY_PATTERNS:
        assert pattern in include, f"package discovery 缺少 {pattern}：{include}"
    for package in PROJECT_PACKAGES:
        assert (REPO_ROOT / package / "__init__.py").is_file(), f"{package}/ 不是包"


def test_payload_non_python_files_are_registered_as_package_data() -> None:
    """contract.md / descriptor / schemas 必须注册为 package data。"""
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["agent_core.contracts"]
    for name in PAYLOAD_NON_PYTHON_FILES:
        assert name in package_data, f"{name} 未注册为 package data"
    assert "schemas/*.json" in package_data, "schemas/*.json 未注册为 package data"


# --------------------------------------------------------------------------- #
# importlib.resources（不依赖 cwd）
# --------------------------------------------------------------------------- #


def test_importlib_resources_reads_payload_from_any_cwd(tmp_path, monkeypatch) -> None:
    """切到无关 cwd 后仍能读到全部 payload 非 Python 文件。"""
    monkeypatch.chdir(tmp_path)
    root = files("agent_core.contracts")
    assert len((root / "contract.md").read_text(encoding="utf-8")) > 0
    for name in PAYLOAD_SCHEMA_FILES:
        text = (root / "schemas" / name).read_text(encoding="utf-8")
        json.loads(text)


def test_descriptor_identity_is_v0_1() -> None:
    """descriptor 身份与 v0.1 契约一致（版本 / canonicalization / schema 集合 / 规则数）。"""
    descriptor = _descriptor()
    assert descriptor["contract_version"] == CONTRACT_VERSION
    assert descriptor["canonicalization_version"] == CANONICALIZATION_VERSION
    assert descriptor["families"] == ["foundation"]
    assert set(descriptor["schema_hashes"]) == EXPECTED_MODELS
    rules = descriptor["normative_rule_set"]
    assert len(rules) == EXPECTED_RULE_COUNT
    assert len(set(rules)) == EXPECTED_RULE_COUNT, "normative_rule_set 存在重复 Rule ID"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor["schema_set_hash"])
    for model, digest in descriptor["schema_hashes"].items():
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), f"{model} 的 schema hash 形状非法"


# --------------------------------------------------------------------------- #
# wheel smoke（FND-PKG-003 真实落点）
# --------------------------------------------------------------------------- #


def _build_wheel(outdir: Path) -> Path:
    """在 outdir 构建 wheel（--no-isolation 保证离线可跑），并清理源树残留。"""
    outdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(outdir),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        "`python -m build --wheel` 失败：\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"期望恰好 1 个 wheel，实际 {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def wheel_entries(tmp_path_factory) -> list[str]:
    """构建一次 wheel 并返回其全部 entry 名（构建后清理源树 build/ 残留）。"""
    outdir = tmp_path_factory.mktemp("wheelhouse")
    try:
        wheel = _build_wheel(outdir)
        with zipfile.ZipFile(wheel) as archive:
            return archive.namelist()
    finally:
        shutil.rmtree(REPO_ROOT / "build", ignore_errors=True)


@contract_rule("FND-PKG-003")
def test_wheel_exposes_agent_core_contracts_and_packaged_schemas(wheel_entries: list[str]) -> None:
    """FND-PKG-003：发行包必须暴露 agent_core.contracts 与随包 schemas。"""
    required = [
        "agent_core/__init__.py",
        "agent_core/contracts/__init__.py",
        "agent_core/contracts/version.py",
    ]
    required += [f"agent_core/contracts/{name}" for name in PAYLOAD_NON_PYTHON_FILES]
    required += [f"agent_core/contracts/schemas/{name}" for name in PAYLOAD_SCHEMA_FILES]
    required += [
        "agent_core/contracts/conformance/rules.py",
        "agent_core/contracts/tooling/generate_schemas.py",
        "agent_core/contracts/tooling/verify_payload.py",
        "agent_core/contracts/tooling/bundle.py",
    ]
    missing = sorted(set(required) - set(wheel_entries))
    assert not missing, f"wheel 缺少 payload 文件：{missing}"


@contract_rule("FND-PKG-003")
def test_wheel_ships_every_project_package(wheel_entries: list[str]) -> None:
    """显式声明 package discovery 后，每个项目包都必须真的进入 wheel。"""
    missing = sorted(
        f"{package}/__init__.py"
        for package in PROJECT_PACKAGES
        if f"{package}/__init__.py" not in wheel_entries
    )
    assert not missing, f"wheel 缺少项目包：{missing}"
    tops = {entry.split("/")[0] for entry in wheel_entries}
    allowed_tops = set(PROJECT_PACKAGES)
    unexpected = {
        top for top in tops if top not in allowed_tops and not top.endswith(".dist-info")
    }
    assert not unexpected, f"wheel 夹带了未声明的顶层内容：{sorted(unexpected)}"


@contract_rule("FND-PKG-001")
def test_wheel_agent_core_tree_respects_phase1_allowlist(wheel_entries: list[str]) -> None:
    """发行包内的 agent_core 同样不得越出 Phase 1 白名单（根 __init__ + contracts/**）。"""
    agent_core_entries = [e for e in wheel_entries if e.startswith("agent_core/")]
    assert agent_core_entries, "wheel 中没有任何 agent_core 内容"
    outside = [
        entry
        for entry in agent_core_entries
        if entry != "agent_core/__init__.py" and not entry.startswith("agent_core/contracts/")
    ]
    assert not outside, f"wheel 中 agent_core 越出白名单：{sorted(outside)}"
    assert not [e for e in wheel_entries if "__pycache__" in e or e.endswith(".pyc")]
