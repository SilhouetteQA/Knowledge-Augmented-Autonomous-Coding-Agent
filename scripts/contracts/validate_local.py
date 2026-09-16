#!/usr/bin/env python
"""Spec 10 — Local Contract Gate：`pr` / `candidate` / `smoke`。

规范来源
--------
- Master §15.1（PR / Candidate Gate 的保序步骤组合、禁止事项）
- Master §11.2 / Spec 13（Smoke Evidence Gate 的八项闭合检查、run manifest 预登记）
- Master §11.4（发布安全扫描）
- Master §14.1–§14.2（非侵入不变性、回归门禁与失败指纹判定）
- Master Appendix C.1 / C.3 / C.4、Spec 10 Development Validation Commands

规范缺口与 provisional 决策
---------------------------
用户已批准："按最小可行语义实现 + 显式标注 provisional，入账本 `SPEC_INCOMPLETE`
事件，留 Spec 11 Stage 0 校准"。

- **G-05**（PR gate 的 "contract-related regression subset" 未定义）
  本实现取：从 `config/contracts/producer-registry.json` 的 **IN_SCOPE** producer
  `source_locations` 导出模块名，再静态匹配 `tests/**/*.py` 中引用了这些模块的测试
  文件（排除已被 step 5 覆盖的 `tests/contracts/`）。若无任何匹配，退化为
  `tests/` 全量（排除 `tests/contracts/`）并在输出中注明退化依据。
- **G-04**（`sink_failure_count == 0` 与"被拒绝的 EvidenceRecord"在进程内存中、
  规范未定义承载 artifact）
  本实现定义运行后汇总文件 `<evidence_root>/<run_id>/run-summary.json`
  （键见 ``RUN_SUMMARY_KEYS``）。**缺失即 gate 失败**——无法验证不等于通过。
- **G-01**（`smoke-v0.1.json` 只有 YAML 伪字段、无 JSON 键名）
  键名按 §13.3 的字段概念直译。另有一条集成约定：`repository_commit` 允许为
  `null`，表示"运行期由 `AGENT_CONTRACT_COMMIT` 解析"——因为该字段不在 §13.3 的
  预登记清单内，而 Candidate A 的 SHA 在 Spec 11 冻结前不可知，Spec 13/14 又禁止
  修改 config。详见 ``_resolve_manifest_commit``。
- **G-13**（`--gate smoke` 是"跑业务"还是"只校验"未定义）
  本实现只校验**已完成的 run**，不驱动业务路径；业务运行由受控手动命令完成。
- **G-15 / G-16**（expected payload hash 来源未定义）
  不自动推断、不 clone 另一仓；仅在显式传入 ``--expect-payload-hash`` 时比较。
- **G-18**（退出码全文未定义）
  约定 ``0`` = 通过；``1`` = gate 失败；``2`` = 用法或配置错误。
- **G-12**（Spec 09 的 baseline comparator 位于无 ``__init__.py`` 的 ``tests/contracts/``）
  按文件路径加载，而非包路径 import。

本脚本**不修改任何文件**（除 `python -m build` 产生的 `build/` 与临时目录，均在
结束后清理）。不访问另一仓、不调用真实模型、不 dump 环境变量或凭据。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 直接以脚本方式运行时 ``sys.path[0]`` 是脚本所在目录，仓库根不在其中
#: （只有 ``python -m`` 才把 CWD 放进 sys.path）。Spec 10 的权威命令形如
#: ``python scripts/contracts/validate_local.py ...``，因此必须自行自举，
#: 否则 ``import agent_core`` 会 ModuleNotFoundError。不得依赖 editable install
#: 的副作用（两仓都提供顶层 ``agent_core``，同一解释器里无法同时可编辑安装）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REPO_ROOT = _REPO_ROOT

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2

CONTRACT_VERSION = "0.1.0"
MAX_RECORD_BYTES = 64 * 1024
RELEASE_ROOT = Path("docs") / "contracts" / "releases"
STAGING_RELATIVE = Path("output") / "contract-validation" / "staging"

PAYLOAD_NON_PYTHON_FILES = ("contract.md", "payload-descriptor.json")
PAYLOAD_SCHEMA_FILES = (
    "cost-summary.schema.json",
    "cost.schema.json",
    "error-envelope.schema.json",
    "evidence-record.schema.json",
    "foundation-observation.schema.json",
    "usage.schema.json",
)

#: G-04 provisional：运行后汇总 artifact 的键（对应 §13.3:1487 的"运行后记录"概念）。
RUN_SUMMARY_KEYS = (
    "sink_failure_count",
    "rejected_records",
    "actual_calls",
    "actual_tokens",
    "duration_seconds",
    "producer_coverage",
    "known_cost_components",
    "unknown_cost_components",
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|credential)s?\b\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{12,}"
    ),
)

FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "raw_prompt",
        "response",
        "response_body",
        "raw_response",
        "reasoning",
        "reasoning_content",
        "traceback",
        "stack_trace",
        "code_body",
        "diff",
        "raw_diff",
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "access_token",
        "credentials",
    }
)

ABSOLUTE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z]:[\\/]{1,2}"),
    re.compile(r"/(?:home|Users|root|mnt|var/folders)/[A-Za-z0-9._\-]+/"),
)

#: 回归子集发现：IN_SCOPE producer 的源码来源（G-05 provisional）。
PRODUCER_REGISTRY = Path("config") / "contracts" / "producer-registry.json"


class UsageError(RuntimeError):
    """用法或配置错误（退出码 2）。"""


class GateFailure(RuntimeError):
    """Gate 判定失败（退出码 1）。"""


@dataclass
class Step:
    index: int
    name: str
    detail: str

    def render(self) -> str:
        return f"[{self.index}] OK   {self.name} — {self.detail}"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def repository_name() -> str:
    """本仓的 EvidenceRepository 取值。"""
    return "wiki" if (REPO_ROOT / "arknights_wiki").is_dir() else "coding"


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        list(cmd),
        cwd=str(cwd or REPO_ROOT),
        env=merged,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _pytest(targets: Sequence[str], *, junit: Path | None = None, timeout: int = 1800) -> Step:
    cmd = [sys.executable, "-m", "pytest", *targets, "-q", "--tb=short", "-p", "no:cacheprovider"]
    if junit is not None:
        cmd.append(f"--junit-xml={junit}")
    proc = _run(cmd, timeout=timeout)
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "(no output)"
    if proc.returncode != 0:
        raise GateFailure(
            f"pytest {' '.join(targets)} 失败（exit {proc.returncode}）：{summary}\n"
            f"--- stdout tail ---\n" + "\n".join(tail[-25:])
        )
    return Step(0, f"pytest {' '.join(targets)}", summary)


def _require_payload_hash(explicit: str | None) -> str | None:
    """只接受显式传入的 expected hash（G-15/G-16：不自动推断、不读另一仓）。"""
    if explicit is None:
        return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", explicit):
        raise UsageError(f"--expect-payload-hash 形状非法：{explicit!r}")
    return explicit


# --------------------------------------------------------------------------- #
# step 1–3：payload 白名单 / schema 漂移 / payload hash
# --------------------------------------------------------------------------- #


def _step_payload_allowlist(index: int) -> Step:
    from agent_core.contracts.tooling import verify_payload as vp

    errors = vp.check_package_layout(REPO_ROOT) + vp.check_file_allowlist(REPO_ROOT)
    if errors:
        raise GateFailure("payload 白名单校验失败：\n  " + "\n  ".join(errors))
    return Step(index, "payload allowlist（FND-PKG-001/002）", "allowlist OK")


def _step_schema_diff(index: int) -> Step:
    from agent_core.contracts.tooling import generate_schemas as gs

    drifts = gs.check(REPO_ROOT)
    if drifts:
        raise GateFailure(
            "Schema 漂移：\n  " + "\n  ".join(f"{d.path}: {d.detail}" for d in drifts)
        )
    return Step(index, "Schema regeneration diff", f"6 schemas 一致（contract_version={CONTRACT_VERSION}）")


def _step_payload_verify(index: int, expected: str | None) -> Step:
    from agent_core.contracts.tooling import verify_payload as vp

    report = vp.verify_tree(REPO_ROOT, expected_payload_hash=expected)
    if not report.ok:
        raise GateFailure("payload 校验失败：\n  " + "\n  ".join(report.errors))
    if report.diverged:
        raise GateFailure(f"payload DIVERGED：{report.payload_hash} != {expected}")
    return Step(
        index,
        "descriptor / payload hash",
        f"{report.file_count} files, payload_hash={report.payload_hash[:23]}…",
    )


# --------------------------------------------------------------------------- #
# step 6：契约相关回归子集（G-05 provisional）
# --------------------------------------------------------------------------- #


def _in_scope_modules() -> list[str]:
    """从 producer registry 的 IN_SCOPE 来源导出模块名（G-05 provisional）。"""
    path = REPO_ROOT / PRODUCER_REGISTRY
    if not path.is_file():
        raise UsageError(f"缺少 producer registry：{path.relative_to(REPO_ROOT).as_posix()}")
    registry = json.loads(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for producer in registry.get("producers", []):
        if producer.get("status") != "IN_SCOPE":
            continue
        for location in producer.get("source_locations", []):
            source = str(location).split("::", 1)[0]
            if not source.endswith(".py"):
                continue
            dotted = source[: -len(".py")].replace("/", ".")
            if dotted not in modules:
                modules.append(dotted)
    return modules


def discover_regression_subset() -> tuple[list[str], str]:
    """静态发现契约相关回归子集（排除 tests/contracts/，它由 step 5 覆盖）。"""
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        return [], "无 tests/ 目录"
    candidates = [
        path
        for path in sorted(tests_dir.rglob("test_*.py"))
        if not path.relative_to(REPO_ROOT).as_posix().startswith("tests/contracts/")
    ]
    modules = _in_scope_modules()
    matched = [
        path
        for path in candidates
        if any(module in path.read_text(encoding="utf-8", errors="replace") for module in modules)
    ]
    if matched:
        basis = f"IN_SCOPE producer 模块匹配（{len(modules)} 个模块）"
        return [path.relative_to(REPO_ROOT).as_posix() for path in matched], basis
    basis = "fallback：无模块引用匹配，退化为 tests/ 全量（排除 tests/contracts/）"
    return [path.relative_to(REPO_ROOT).as_posix() for path in candidates], basis


# --------------------------------------------------------------------------- #
# step 7：发布安全扫描（§11.4）
# --------------------------------------------------------------------------- #


def _scan_text(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(f"疑似密钥（pattern {pattern.pattern[:32]}…）")
    for pattern in ABSOLUTE_PATH_PATTERNS:
        if pattern.search(text):
            hits.append(f"绝对路径（pattern {pattern.pattern[:32]}…）")
    if text.startswith("\ufeff"):
        hits.append("UTF-8 BOM")
    return hits


def _scan_object(value: Any, where: str, *, depth: int = 0) -> list[str]:
    hits: list[str] = []
    if depth > 12:
        return hits
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.append(f"{where}: 禁止字段 {key!r}")
            hits.extend(_scan_object(item, f"{where}.{key}", depth=depth + 1))
    elif isinstance(value, list):
        for position, item in enumerate(value):
            hits.extend(_scan_object(item, f"{where}[{position}]", depth=depth + 1))
    elif isinstance(value, str):
        hits.extend(f"{where}: {hit}" for hit in _scan_text(value))
    return hits


def _scan_paths(roots: Iterable[Path]) -> tuple[list[str], int]:
    violations: list[str] = []
    scanned = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            scanned += 1
            try:
                relative = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                # 扫描根可能位于仓库之外（工具自测用 tmp_path）；此时保留原路径，
                # 不因为"无法相对化"而跳过扫描。
                relative = path.as_posix()
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                violations.append(f"{relative}: 非 UTF-8（{exc}）")
                continue
            # 64 KiB 上限的规范含义是**单条 Evidence 记录**（EVD-DATA-001 / D.4
            # "canonical event JSON ≤ 64 KiB"），不是整个 artifact 文件：语料/报告类
            # 文件天然是多条记录的聚合。因此逐类判定：
            #   - `.jsonl`：逐行判定（每行是一条记录）
            #   - `events/` 下的 `.json`：整文件判定（一个事件一个文件）
            #   - 其它文件：不设尺寸门（仍走密钥/路径/UTF-8/JSON 扫描）
            if path.suffix == ".jsonl":
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                        violations.append(
                            f"{relative}:{line_no}: 单条记录 {len(line.encode('utf-8'))} 字节超过 64 KiB"
                        )
            elif path.suffix == ".json" and "events" in path.parts:
                if len(raw) > MAX_RECORD_BYTES:
                    violations.append(
                        f"{relative}: 单条 Evidence 记录 {len(raw)} 字节超过 64 KiB"
                    )
            violations.extend(f"{relative}: {hit}" for hit in _scan_text(text))
            if path.suffix == ".json":
                try:
                    violations.extend(
                        f"{relative}: {hit}" for hit in _scan_object(json.loads(text), relative)
                    )
                except json.JSONDecodeError as exc:
                    violations.append(f"{relative}: JSON 非法（{exc}）")
    return violations, scanned


def _step_publication_safety(index: int) -> Step:
    roots = (REPO_ROOT / RELEASE_ROOT, REPO_ROOT / STAGING_RELATIVE)
    violations, scanned = _scan_paths(roots)
    if violations:
        raise GateFailure("发布安全扫描命中：\n  " + "\n  ".join(sorted(set(violations))[:40]))
    if scanned == 0:
        return Step(index, "evidence publication safety scan", "无待扫描产物（vacuous）")
    return Step(index, "evidence publication safety scan", f"{scanned} 文件无命中")


# --------------------------------------------------------------------------- #
# step 8：package smoke（clean environment，不依赖 cwd）
# --------------------------------------------------------------------------- #

_IMPORT_PROBE = """
import json, sys, zipfile
from importlib.resources import files
prefix = sys.argv[1]
sys.path.insert(0, prefix)
import agent_core
assert agent_core.__file__.startswith(prefix), f"agent_core 来自 {agent_core.__file__}，未使用目标安装树"
from agent_core.contracts.models.usage import Usage
from agent_core.contracts.models.cost import Cost
from agent_core.contracts.models.evidence import EvidenceRecord
root = files("agent_core.contracts")
descriptor = json.loads((root / "payload-descriptor.json").read_text(encoding="utf-8"))
assert descriptor["contract_version"] == "0.1.0", descriptor["contract_version"]
for name in (
    "cost-summary.schema.json", "cost.schema.json", "error-envelope.schema.json",
    "evidence-record.schema.json", "foundation-observation.schema.json", "usage.schema.json",
):
    json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
print("clean-env import + package data OK")
"""


def _step_package_smoke(index: int) -> Step:
    import contextlib

    work = Path(tempfile.mkdtemp(prefix="fc-package-smoke-"))
    try:
        wheelhouse = work / "wheelhouse"
        wheelhouse.mkdir(parents=True, exist_ok=True)
        env = {"PYTHONPATH": ""}
        build = _run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheelhouse),
            ],
            cwd=REPO_ROOT,
            env=env,
            timeout=900,
        )
        if build.returncode != 0:
            raise GateFailure(
                "`python -m build --wheel` 失败（G-19：需先安装 build 模块）：\n"
                + (build.stdout or "")[-1500:]
                + (build.stderr or "")[-1500:]
            )
        wheels = sorted(wheelhouse.glob("*.whl"))
        if len(wheels) != 1:
            raise GateFailure(f"期望恰好 1 个 wheel，实际 {wheels}")
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            entries = set(archive.namelist())
        required = {
            "agent_core/__init__.py",
            "agent_core/contracts/__init__.py",
            "agent_core/contracts/version.py",
            *(f"agent_core/contracts/{name}" for name in PAYLOAD_NON_PYTHON_FILES),
            *(f"agent_core/contracts/schemas/{name}" for name in PAYLOAD_SCHEMA_FILES),
        }
        missing = sorted(required - entries)
        if missing:
            raise GateFailure(f"wheel 缺少 payload 文件：{missing}")

        target = work / "site"
        install = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-deps",
                "--target",
                str(target),
                str(wheel),
            ],
            cwd=work,
            env={"PYTHONPATH": ""},
            timeout=900,
        )
        if install.returncode != 0:
            raise GateFailure("wheel 安装到临时 target 失败：\n" + (install.stderr or "")[-1500:])
        probe = _run(
            [sys.executable, "-c", _IMPORT_PROBE, str(target)],
            cwd=work,
            env={"PYTHONPATH": ""},
            timeout=300,
        )
        if probe.returncode != 0:
            raise GateFailure(
                "clean-environment import / package-data 读取失败：\n"
                + (probe.stdout or "")[-1500:]
                + (probe.stderr or "")[-1500:]
            )
        return Step(
            index,
            "clean wheel install / import / resource smoke",
            f"{wheel.name}，{len(entries)} entries，clean-env 通过",
        )
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(REPO_ROOT / "build", ignore_errors=True)


# --------------------------------------------------------------------------- #
# gate: pr
# --------------------------------------------------------------------------- #


def gate_pr(expected_hash: str | None) -> list[Step]:
    steps: list[Step] = []
    steps.append(_step_payload_allowlist(1))
    steps.append(_step_schema_diff(2))
    steps.append(_step_payload_verify(3, _require_payload_hash(expected_hash)))

    steps.append(_relabel(_pytest(["agent_core/contracts/conformance"]), 4))
    steps.append(_relabel(_pytest(["tests/contracts"]), 5))

    subset, subset_basis = discover_regression_subset()
    if not subset:
        raise GateFailure(
            "SPEC_INCOMPLETE（G-05）：contract-related regression subset 为空；"
            "本仓没有任何测试引用 IN_SCOPE producer 的源码模块"
        )
    subset_step = _relabel(_pytest(subset), 6)
    subset_step.detail = f"{subset_step.detail}；{subset_basis}，{len(subset)} 文件"
    steps.append(subset_step)

    steps.append(_step_publication_safety(7))
    steps.append(_step_package_smoke(8))
    return steps


def _relabel(step: Step, index: int) -> Step:
    step.index = index
    return step


# --------------------------------------------------------------------------- #
# gate: candidate
# --------------------------------------------------------------------------- #


def _load_baseline_module():
    """按文件路径加载 Spec 09 的 baseline comparator（G-12：tests/contracts 不是包）。"""
    path = REPO_ROOT / "tests" / "contracts" / "test_test_baseline.py"
    if not path.is_file():
        raise UsageError(f"缺少 baseline comparator：{path}")
    spec = importlib.util.spec_from_file_location("_fc_test_baseline", path)
    if spec is None or spec.loader is None:
        raise UsageError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_fc_test_baseline"] = module
    spec.loader.exec_module(module)
    return module


def _junit_nodeid(classname: str, name: str) -> str:
    parts = [part for part in classname.split(".") if part]
    for cut in range(len(parts), 0, -1):
        candidate = REPO_ROOT.joinpath(*parts[:cut]).with_suffix(".py")
        if candidate.is_file():
            head = "/".join(parts[:cut]) + ".py"
            return "::".join([head, *parts[cut:], name])
    return "::".join([classname, name]) if classname else name


def _derive_locus(text: str) -> str:
    frames = re.findall(r'File "([^"]+)", line \d+, in ([^\s]+)', text)
    if not frames:
        return "unknown"
    raw_path, function = frames[-1]
    try:
        relative = Path(raw_path).resolve().relative_to(REPO_ROOT)
    except ValueError:
        return function
    module = ".".join(relative.with_suffix("").parts)
    return f"{module}.{function}"


def _normalize_signature(exception_type: str, text: str) -> str:
    """provisional：Appendix E.1 只规定"normalized semantic signature"概念，未给算法。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    message = lines[-1] if lines else ""
    message = re.sub(r"0x[0-9a-fA-F]+", "<hex>", message)
    message = re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<path>", message)
    message = re.sub(r"\d+", "<n>", message)
    message = re.sub(r"[^0-9A-Za-z<>=]+", "_", message).strip("_").lower()
    return f"{exception_type.lower()}:{message[:120]}"


def _parse_junit(path: Path) -> list[tuple[str, str, dict[str, Any]]]:
    tree = ET.parse(path)
    results: list[tuple[str, str, dict[str, Any]]] = []
    for case in tree.iter("testcase"):
        nodeid = _junit_nodeid(case.get("classname", ""), case.get("name", ""))
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None or error is not None:
            node = failure if failure is not None else error
            assert node is not None
            body = f"{node.get('message') or ''}\n{node.text or ''}"
            exception_type = node.get("type") or "Exception"
            results.append(
                (
                    nodeid,
                    "FAIL",
                    {
                        "nodeid": nodeid,
                        "exception_type": exception_type,
                        "symbolic_failure_locus": _derive_locus(body),
                        "normalized_error_signature": _normalize_signature(exception_type, body),
                        "scope": "DEFERRED",
                    },
                )
            )
        elif skipped is not None:
            results.append((nodeid, "SKIP", {}))
        else:
            results.append((nodeid, "PASS", {}))
    return results


def _judge(
    nodeid: str, outcome: str, failure: dict[str, Any], baseline: dict, module
) -> tuple[str, bool, bool]:
    """返回 ``(判定, 是否放行, 是否需要 review)``。"""
    if outcome == "PASS":
        verdict = module.classify_status(nodeid, "PASS", baseline)
        return verdict, verdict in {"PASS_OK", "RESOLVED_UNEXPECTEDLY"}, verdict == "RESOLVED_UNEXPECTEDLY"
    if outcome == "SKIP":
        verdict = module.classify_status(nodeid, "SKIP", baseline)
        return verdict, verdict == "SKIP_OK", False
    verdict = module.classify_status(nodeid, "FAIL", baseline)
    if verdict == "KNOWN_FAILURE_PRESENT":
        fingerprint = module.classify_failure(nodeid, failure, baseline)
        return fingerprint, fingerprint == "ALLOWED_BASELINE_FAILURE", False
    return verdict, False, False


def gate_candidate(expected_hash: str | None) -> list[Step]:
    steps = gate_pr(expected_hash)
    module = _load_baseline_module()
    baseline = module.load_baseline()

    with tempfile.TemporaryDirectory(prefix="fc-candidate-") as work:
        junit = Path(work) / "regression.xml"
        steps.append(_relabel(_pytest(["tests/"], junit=junit, timeout=3600), 9))
        results = _parse_junit(junit)

    failures: list[str] = []
    reviews: list[str] = []
    for nodeid, outcome, failure in results:
        verdict, allowed, needs_review = _judge(nodeid, outcome, failure, baseline, module)
        if not allowed:
            failures.append(f"{verdict}: {nodeid}")
        elif needs_review:
            reviews.append(f"{verdict}: {nodeid}")
    counts = {
        "PASS": sum(1 for _, outcome, _ in results if outcome == "PASS"),
        "SKIP": sum(1 for _, outcome, _ in results if outcome == "SKIP"),
        "FAIL": sum(1 for _, outcome, _ in results if outcome == "FAIL"),
    }
    if failures:
        raise GateFailure(
            "nodeid / fingerprint gate 失败：\n  " + "\n  ".join(failures[:40])
        )
    detail = f"{counts['PASS']} PASS / {counts['SKIP']} SKIP / {counts['FAIL']} FAIL，无新增失败"
    if reviews:
        detail += f"；{len(reviews)} 条 known failure 已 PASS，需 review（不使 gate 失败）"
    steps.append(Step(9, "canonical full regression + nodeid/fingerprint gate", detail))

    steps.append(_step_evidence_verification(10))
    return steps


def _step_evidence_verification(index: int) -> Step:
    """L1/L2/L3 证据闭合校验（Spec 11–14 在 Candidate A 冻结后产出）。"""
    release = REPO_ROOT / RELEASE_ROOT / CONTRACT_VERSION
    repo = repository_name()
    manifest_path = release / "validation" / repo / "evidence-manifest.json"
    if not manifest_path.is_file():
        raise GateFailure(
            f"缺少 Evidence Manifest：{manifest_path.relative_to(REPO_ROOT).as_posix()}；"
            "L1/L2/L3 证据由 Spec 11–14 在 Candidate A 冻结后产出"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "contract_version",
        "contract_payload_hash",
        "verified_repository_commit",
        "repository",
        "environment",
        "evidence",
        "producer_coverage",
        "benchmark_reference_status",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise GateFailure(f"Evidence Manifest 缺少字段：{missing}")
    if "evidence_manifest_hash" in manifest or "evidence_commit" in manifest:
        raise GateFailure("Evidence Manifest 不得包含自身 hash 或 B SHA（§16.3）")
    expected_evidence = {"contract_tests", "historical_replay", "fresh_smoke", "full_regression"}
    missing_evidence = sorted(expected_evidence - set(manifest["evidence"]))
    if missing_evidence:
        raise GateFailure(f"Evidence Manifest 缺少证据条目：{missing_evidence}")
    return Step(
        index,
        "L1/L2/L3 evidence verification",
        f"{manifest_path.name} 闭合（repository={manifest['repository']}）",
    )


# --------------------------------------------------------------------------- #
# gate: smoke（§11.2 八项闭合）
# --------------------------------------------------------------------------- #


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError(f"run manifest 不存在：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"run manifest 不是合法 JSON：{exc}") from exc


def _resolve_manifest_commit(manifest: dict[str, Any]) -> str:
    """解析 run manifest 的运行 commit。

    provisional（G-01）：``repository_commit`` **不在** §13.3 的预登记字段清单内
    （该清单只有 contract_version / payload_hash / candidate_commit），而 Candidate A
    的 SHA 在 Spec 11 冻结前不可知；Spec 13/14 又禁止修改 config。因此约定
    ``"repository_commit": null`` = 运行期由 ``AGENT_CONTRACT_COMMIT`` 解析。
    不自动推断 HEAD —— 用错 commit 会伪造证据绑定（``EVD-RUN-002``）。
    """
    declared = manifest.get("repository_commit")
    if declared is None:
        resolved = os.environ.get("AGENT_CONTRACT_COMMIT")
        if not resolved:
            raise GateFailure(
                "run manifest 的 repository_commit 为 null，必须由 AGENT_CONTRACT_COMMIT "
                "提供运行 commit（G-01 provisional：不自动推断 HEAD）"
            )
        return resolved
    if not re.fullmatch(r"[0-9a-f]{40}", str(declared)):
        raise UsageError(f"run manifest 的 repository_commit 形状非法：{declared!r}")
    return str(declared)


def gate_smoke(manifest_path: Path | None) -> list[Step]:
    if manifest_path is None:
        raise UsageError("--gate smoke 必须提供 --run-manifest")
    manifest = _load_manifest(manifest_path)

    required_keys = {
        "manifest_version",
        "run_id",
        "contract_mode",
        "contract_version",
        "payload_hash",
        "repository_commit",
        "evidence_root",
        "coverage_policy",
        "required_producer_stages",
        "expected_calls",
        "max_calls",
        "estimated_cost_cap",
        "network_requirement",
        "side_effect_policy",
        "timeout_seconds",
        "duration_cap_seconds",
        "model",
        "provider",
        "case_ids",
    }
    missing = sorted(required_keys - set(manifest))
    if missing:
        raise UsageError(
            f"run manifest 缺少字段 {missing}；"
            "SPEC_INCOMPLETE（G-01）：smoke-v0.1.json 的键名是 Spec 10 provisional 收敛"
        )

    run_id = manifest["run_id"]
    env_run_id = os.environ.get("AGENT_CONTRACT_RUN_ID")
    env_mode = os.environ.get("AGENT_CONTRACT_MODE")
    steps: list[Step] = []

    if env_mode != "observe":
        raise GateFailure(
            f"FND-MODE-001：--gate smoke 要求 AGENT_CONTRACT_MODE=observe，实际 {env_mode!r}"
        )
    if env_run_id != run_id:
        raise GateFailure(
            f"EVD-RUN-001：显式 run_id 与 manifest 不一致（env={env_run_id!r}, manifest={run_id!r}）"
        )
    steps.append(Step(1, "run_id 与 run manifest 一致 + mode=observe", f"run_id={run_id}"))

    root = REPO_ROOT / manifest["evidence_root"] / run_id
    events_dir = root / "events"
    if not events_dir.is_dir():
        raise GateFailure(f"事件目录不存在：{events_dir.relative_to(REPO_ROOT).as_posix()}")

    from agent_core.contracts.models.evidence import EvidenceRecord

    if repository_name() == "wiki":
        from arknights_wiki.adapters.foundation.evidence_sink import (
            has_residual_temp_files,
            iter_published_events,
        )
    else:
        from adapters.foundation.evidence_sink import (  # type: ignore[no-redef]
            has_residual_temp_files,
            iter_published_events,
        )

    if has_residual_temp_files(events_dir):
        raise GateFailure("§11.1：事件目录存在残留 .tmp，Smoke Evidence Gate 失败")
    steps.append(Step(2, "无 .tmp 残留", "clean"))

    raw_events = [json.loads(path.read_text(encoding="utf-8")) for path in iter_published_events(events_dir)]
    if not raw_events:
        raise GateFailure("§11.2：没有任何已发布事件，目录非空不构成 Smoke 证据")
    records = []
    for raw in raw_events:
        try:
            records.append(EvidenceRecord.model_validate(raw))
        except Exception as exc:  # pydantic ValidationError 与自定义校验错误
            raise GateFailure(f"EvidenceRecord 校验失败：{exc}") from exc
    steps.append(Step(3, "EvidenceRecord 模式校验", f"{len(records)} 条全部通过"))

    event_ids = [record.event_id for record in records]
    if len(set(event_ids)) != len(event_ids):
        duplicates = sorted({value for value in event_ids if event_ids.count(value) > 1})
        raise GateFailure(f"event_id 重复：{duplicates}")
    steps.append(Step(4, "event_id 唯一", f"{len(set(event_ids))} 唯一"))

    expected_identity = {
        "repository": repository_name(),
        "repository_commit": _resolve_manifest_commit(manifest),
        "contract_version": manifest["contract_version"],
        "contract_payload_hash": manifest["payload_hash"],
    }
    for record in records:
        for attribute, expected in expected_identity.items():
            actual = getattr(record, attribute)
            if str(actual) != str(expected):
                raise GateFailure(
                    f"§11.2：事件 {record.event_id} 的 {attribute}={actual!r} 与 manifest 期望 {expected!r} 不一致"
                )
    steps.append(Step(5, "repository / commit / version / payload hash 一致", "全部一致"))

    non_observe = [record.event_id for record in records if str(record.contract_mode) != "observe"]
    if non_observe:
        raise GateFailure(f"§11.2：存在非 observe 事件：{non_observe[:10]}")
    steps.append(Step(6, "全部事件 contract_mode=observe", f"{len(records)} 条"))

    observed = {(str(record.producer_id), str(record.mapping_stage)) for record in records}
    required_pairs = {
        (str(item["producer_id"]), str(item["mapping_stage"]))
        for item in manifest["required_producer_stages"]
    }
    policy = manifest["coverage_policy"]
    if policy == "ALL_STAGES":
        missing_pairs = sorted(required_pairs - observed)
        if missing_pairs:
            raise GateFailure(f"§13.3：required producer/stage 未观测到：{missing_pairs}")
        covered = f"{len(required_pairs)}/{len(required_pairs)}"
    elif policy == "ONE_OF":
        if not (required_pairs & observed):
            raise GateFailure(f"§13.3：ONE_OF 策略下未观测到任何 required stage：{sorted(required_pairs)}")
        covered = f"{len(required_pairs & observed)}/{len(required_pairs)}"
    else:
        raise UsageError(f"coverage_policy 非法：{policy!r}")
    steps.append(Step(7, "producer/stage 覆盖（G-04 provisional）", f"{policy} {covered}"))

    summary_path = root / "run-summary.json"
    if not summary_path.is_file():
        raise GateFailure(
            "SPEC_INCOMPLETE（G-04）：缺少运行后汇总 artifact "
            f"{summary_path.relative_to(REPO_ROOT).as_posix()}；sink_failure_count 与"
            "被拒绝记录在进程内存中，规范未定义承载 artifact，本 Spec provisional 收敛为该文件。"
            "无法验证不等于通过。"
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    missing_summary = sorted(set(RUN_SUMMARY_KEYS) - set(summary))
    if missing_summary:
        raise GateFailure(f"run-summary.json 缺少键：{missing_summary}")
    if summary["sink_failure_count"] != 0:
        raise GateFailure(f"EVD-SINK-003：sink_failure_count={summary['sink_failure_count']} != 0")
    if summary["rejected_records"]:
        raise GateFailure(f"§11.2：存在被拒绝的 EvidenceRecord：{summary['rejected_records'][:10]}")
    if summary["duration_seconds"] > manifest["duration_cap_seconds"]:
        raise GateFailure(
            f"§13.3：duration {summary['duration_seconds']}s 超过预算 {manifest['duration_cap_seconds']}s"
        )
    steps.append(
        Step(8, "run-summary 闭合（sink=0 / 无拒绝 / 时长在预算内）", "闭合")
    )
    return steps


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foundation Contract local gate（Spec 10）")
    parser.add_argument("--gate", choices=("pr", "candidate", "smoke"), required=True)
    parser.add_argument("--run-manifest", type=Path, default=None)
    parser.add_argument(
        "--expect-payload-hash",
        default=None,
        help="显式 expected payload hash（sha256:<64 hex>）；不提供则只做自洽校验",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    try:
        if args.gate == "smoke":
            steps = gate_smoke(args.run_manifest)
        elif args.gate == "candidate":
            steps = gate_candidate(args.expect_payload_hash)
        else:
            steps = gate_pr(args.expect_payload_hash)
    except UsageError as exc:
        print(f"SPEC_INCOMPLETE / USAGE: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GateFailure as exc:
        print(f"GATE FAILED: {exc}", file=sys.stderr)
        return EXIT_GATE_FAILED

    if args.json:
        print(
            json.dumps(
                {
                    "gate": args.gate,
                    "repository": repository_name(),
                    "steps": [{"index": s.index, "name": s.name, "detail": s.detail} for s in steps],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"=== gate {args.gate} / repository={repository_name()} ===")
        for step in steps:
            print(step.render())
        print(f"gate {args.gate} PASSED")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
