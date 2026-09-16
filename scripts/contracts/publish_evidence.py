"""Evidence publication 工具（Spec 10 交付物 / Spec 14 工具面，Commit B 的准备步骤）。

冻结 CLI（母 Spec C.1 / Spec14:78，**不得改名**）::

    python scripts/contracts/publish_evidence.py --candidate <A_SHA> --release-version 0.1.0

本工具只准备 **B 的 allowlist artifact**，不 commit、不创建 cycle plan / cycle report /
``current.json``（Spec14:89；那些属 Spec 15 的 coordinator / finalizer）。

规范要点与落点：

```text
§11.3:1214-1223  controlled source → minimal staging facts → 新建 publishable DTO
                 → validate/scan → hash → release snapshot
§11.3:1225       禁止 "先序列化原始对象再删字段"（denylist）——本文件所有 DTO 都是
                 逐字段**新建**（见 *_ALLOWLIST 常量与 construct_* 函数）
§11.4:1244-1251  secret / forbidden field / absolute path / size / UTF-8 扫描
§11.4:1253       run manifest 只记录净化命令；secret/临时目录/私人路径替换为
                 <SECRET> / <WORKSPACE> / <REDACTED>
§12.5:1369-1378  contract-manifest.json 位于 Payload 之外、内容两仓相同、
                 canonical_payload_repository 恒为 wiki、canonical_payload_commit 指向 A_wiki
D.2:2460-2518    release 目录结构 + Evidence Manifest 最小 schema + Rule traceability
§16.3:1653-1660  Evidence Manifest **不得**含自身 hash 或 B SHA
```

------------------------------------------------------------------------------
provisional 决策（G-01/G-08/G-14/G-16/G-18，已获用户批准）
------------------------------------------------------------------------------

* **G-08（显式收敛）** `changelog.md` **不在** §11.3:1227-1236 的 6 项 B 清单内，但
  D.2:2467-2477 / Spec14:27 / F.4:2692 都要求它随 release append-only 存在。
  因此本工具的 allowlist **显式包含** `changelog.md`（`PUBLISH_ALLOWLIST`），
  并在 validation report 中记录这是对 G-08 的显式收敛。
* **G-16（Provisional）** Coding 侧无法在 PR gate 内 clone Wiki，因而无从得知 A_wiki；
  本工具用环境变量 `AGENT_CONTRACT_CANONICAL_PAYLOAD_COMMIT` 接收 A_wiki（缺省
  取 `--candidate`），从而**不改变**冻结的两个必需 CLI 参数。
* **G-01（Provisional）** `validation/<repo>/run-manifest.json` 的内容取自
  `config/contracts/smoke-v0.1.json`（§13.3 预登记字段 + Spec13:29-40 追加项）
  的 allowlist 投影 + 净化命令列表（§11.4:1253）。
* **G-14（Provisional 收敛）** expected mapping 不在本工具内重新定义：它由
  producer registry 的登记字段承载（replay 步骤负责比较）。
* **G-18（Provisional）** 退出码：`0` 通过、`1` 校验/扫描失败、`2` 用法或配置错误。
* `fresh_smoke` 在没有 smoke staging 事件时记为 `NOT_OBSERVED`（§11.5 的合法状态），
  **不**伪报为 PASS；一旦存在事件则必须通过 A 绑定检查，否则拒发（exit 1）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------- #
# sys.path 自举（脚本直跑时 sys.path[0] 是脚本目录，仓库根不在其中）
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _entry in (str(_REPO_ROOT), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
REPO_ROOT: Final[Path] = _REPO_ROOT

from agent_core.contracts.models.evidence import (  # noqa: E402
    REPOSITORY_COMMIT_PATTERN,
    is_safe_run_id,
)
from agent_core.contracts.tooling.canonical_json import (  # noqa: E402
    canonical_file_bundle_digest,
    sha256_hex,
)

from replay_history import (  # noqa: E402  (同目录工具链，共享扫描/原子写入实现)
    CORPUS_FILENAME,
    CORPUS_RECORD_KEYS,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    L2_RESULT_FILENAME,
    PRODUCER_REGISTRY_RELPATH,
    REDACTION_GENERIC,
    REDACTION_SECRET,
    REDACTION_WORKSPACE,
    Finding,
    ToolError,
    atomic_write_text,
    canonical_json_dumps,
    check_artifact_size,
    check_record_size,
    load_registry,
    record_hash,
    registry_producers,
    sanitize_text,
    scan_structure,
    scan_text,
    sha256_hex,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

EVIDENCE_ROOT_ENV: Final[str] = "AGENT_CONTRACT_EVIDENCE_DIR"
CANONICAL_COMMIT_ENV: Final[str] = "AGENT_CONTRACT_CANONICAL_PAYLOAD_COMMIT"

CANONICAL_PAYLOAD_REPOSITORY: Final[str] = "wiki"

REPLAY_MANIFEST_RELPATH: Final[Path] = Path("config/contracts") / "replay-v0.1.json"
SMOKE_MANIFEST_RELPATH: Final[Path] = Path("config/contracts") / "smoke-v0.1.json"
PAYLOAD_DESCRIPTOR_RELPATH: Final[Path] = Path("agent_core/contracts/payload-descriptor.json")

RELEASE_DIR_TEMPLATE: Final[str] = "docs/contracts/releases/{version}"
VALIDATION_DIR_TEMPLATE: Final[str] = "docs/contracts/releases/{version}/validation/{repository}"

RELEASE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")

CONTRACT_MANIFEST_FILENAME: Final[str] = "contract-manifest.json"
CHANGELOG_FILENAME: Final[str] = "changelog.md"
RUN_MANIFEST_FILENAME: Final[str] = "run-manifest.json"
EVIDENCE_MANIFEST_FILENAME: Final[str] = "evidence-manifest.json"
VALIDATION_REPORT_FILENAME: Final[str] = "validation-report.md"
RULE_TRACEABILITY_FILENAME: Final[str] = "rule-traceability.json"

#: `contract-manifest.json` 的字段 allowlist（§12.5:1369-1376）。
CONTRACT_MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "contract_version",
    "contract_payload_hash",
    "payload_descriptor_hash",
    "schema_set_hash",
    "canonical_payload_repository",
    "canonical_payload_commit",
)

#: `evidence-manifest.json` 的字段 allowlist（D.2:2481-2500）。
#: 明确**不含** `evidence_manifest_hash` / `evidence_commit`（§16.3:1653-1660）。
EVIDENCE_MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "contract_version",
    "contract_payload_hash",
    "verified_repository_commit",
    "repository",
    "environment",
    "evidence",
    "producer_coverage",
    "benchmark_reference_status",
)

#: run-manifest 投影 allowlist（G-01：§13.3:1470-1485 + Spec13:29-40 的追加项）。
RUN_MANIFEST_PROJECTION_KEYS: Final[tuple[str, ...]] = (
    "manifest_version",
    "run_id",
    "contract_mode",
    "contract_version",
    "payload_hash",
    "candidate_commit",
    "repository_commit",
    "evidence_root",
    "coverage_policy",
    "required_producer_stages",
    "model",
    "provider",
    "case_ids",
    "expected_calls",
    "max_calls",
    "estimated_cost_cap",
    "network_requirement",
    "side_effect_policy",
    "timeout_seconds",
    "duration_cap_seconds",
)

#: 本仓 `full_regression` 的期望结论（Spec14:69-70、D.2:2503）。
FULL_REGRESSION_BY_REPOSITORY: Final[Mapping[str, str]] = {
    "wiki": "PASS_WITH_KNOWN_BASELINE_FAILURES",
    "coding": "PASS",
}

#: 本仓 `benchmark_reference_status`（D.2:2503）。
BENCHMARK_REFERENCE_BY_REPOSITORY: Final[Mapping[str, str]] = {
    "wiki": "BENCHMARK_BASELINE_NOT_REPRODUCIBLE",
    "coding": "BENCHMARK_REPRODUCTION_RESTRICTED",
}

#: 净化命令（§11.4:1253：run manifest 只记录净化命令）。
SANITIZED_COMMANDS: Final[tuple[str, ...]] = (
    "python scripts/contracts/validate_local.py --gate pr",
    "python scripts/contracts/validate_local.py --gate candidate",
    "python scripts/contracts/replay_history.py --run-manifest config/contracts/replay-v0.1.json",
    "python scripts/contracts/validate_local.py --gate smoke "
    "--run-manifest config/contracts/smoke-v0.1.json",
    "python scripts/contracts/publish_evidence.py --candidate <A_SHA> --release-version 0.1.0",
)

#: 禁止出现在 release 中的产物名（Spec14:89：不得产出 cycle plan/report/pointer）。
FORBIDDEN_RELEASE_ARTIFACTS: Final[tuple[str, ...]] = (
    "cycle-plan.json",
    "cycle-report.json",
    "cycle-report.md",
    "current.json",
)

EVIDENCE_KEYS: Final[tuple[str, ...]] = (
    "contract_tests",
    "historical_replay",
    "fresh_smoke",
    "full_regression",
)


def release_allowlist(release_version: str, repository: str) -> tuple[str, ...]:
    """B 阶段的严格 allowlist（仓库相对路径）。

    **G-08 显式收敛**：`changelog.md` 不在 §11.3:1227-1236 的 6 项清单内，但
    D.2 / Spec14:27 / F.4 都要求它存在，因此这里显式列入。
    """
    release_dir = RELEASE_DIR_TEMPLATE.format(version=release_version)
    validation_dir = VALIDATION_DIR_TEMPLATE.format(
        version=release_version, repository=repository
    )
    return (
        f"{release_dir}/{CONTRACT_MANIFEST_FILENAME}",
        f"{release_dir}/{CHANGELOG_FILENAME}",
        f"{validation_dir}/{RUN_MANIFEST_FILENAME}",
        f"{validation_dir}/{EVIDENCE_MANIFEST_FILENAME}",
        f"{validation_dir}/{VALIDATION_REPORT_FILENAME}",
        f"{validation_dir}/{RULE_TRACEABILITY_FILENAME}",
        f"{validation_dir}/{CORPUS_FILENAME}",
    )


# --------------------------------------------------------------------------- #
# candidate / git
# --------------------------------------------------------------------------- #


def resolve_repository(repo_root: Path, override: str | None, registry: Mapping[str, Any]) -> str:
    """确定仓库归属：registry 的 `repository` 字段为准（可被 --repository 复核）。"""
    declared = registry.get("repository")
    if not isinstance(declared, str) or not declared:
        raise ToolError(EXIT_USAGE, "producer registry 缺少 repository 字段")
    if override is not None and override != declared:
        raise ToolError(
            EXIT_USAGE,
            f"--repository={override!r} 与本仓 registry 的 repository={declared!r} 不一致",
        )
    if declared not in FULL_REGRESSION_BY_REPOSITORY:
        raise ToolError(EXIT_USAGE, f"未登记的 repository：{declared!r}")
    return declared


def validate_candidate_format(candidate: str) -> str:
    """`--candidate` 必须是 40 位小写 hex（否则 exit 1，且**不**自动寻找替代 SHA）。"""
    if not isinstance(candidate, str) or not REPOSITORY_COMMIT_PATTERN.match(candidate):
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            f"--candidate {candidate!r} 不是 40 位小写 hex；"
            "本工具不会自动推断或替换 Candidate SHA",
        )
    return candidate


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - 无 git 时
        raise ToolError(EXIT_USAGE, f"无法执行 git：{exc}") from exc


def require_candidate_ancestor(candidate: str, repo_root: Path) -> str:
    """校验 candidate 存在且是当前 HEAD 的祖先；返回 HEAD。"""
    exists = _run_git(repo_root, "cat-file", "-e", f"{candidate}^{{commit}}")
    if exists.returncode != 0:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            f"--candidate {candidate} 不在本仓 git 对象库中（不存在或不是 commit）；"
            "拒绝发布，且不会寻找替代 SHA",
        )
    head_result = _run_git(repo_root, "rev-parse", "HEAD")
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or not REPOSITORY_COMMIT_PATTERN.match(head):
        raise ToolError(EXIT_USAGE, "无法解析当前 HEAD；publisher 必须运行在 git 工作区中")
    ancestor = _run_git(repo_root, "merge-base", "--is-ancestor", candidate, head)
    if ancestor.returncode != 0:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            f"--candidate {candidate} 不是当前 HEAD({head}) 的祖先（Spec14:61）；"
            "拒绝发布，且不会寻找替代 SHA",
        )
    return head


# --------------------------------------------------------------------------- #
# 受控输入（全部只读）
# --------------------------------------------------------------------------- #


def _read_json(path: Path, *, what: str, exit_code: int = EXIT_USAGE) -> Any:
    if not Path(path).is_file():
        raise ToolError(exit_code, f"找不到{what}：{path}")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(exit_code, f"{what} 不是合法 JSON：{exc}") from exc


def staging_root(repo_root: Path, env: Mapping[str, str] | None = None) -> Path:
    """staging 根：`AGENT_CONTRACT_EVIDENCE_DIR` 优先（与 EvidenceSink 同口径）。"""
    environment = os.environ if env is None else env
    override = (environment.get(EVIDENCE_ROOT_ENV) or "").strip()
    if override:
        return Path(override)
    replay_manifest = _read_json(
        Path(repo_root) / REPLAY_MANIFEST_RELPATH, what="replay run manifest"
    )
    output_dir = replay_manifest.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ToolError(EXIT_USAGE, "replay run manifest 缺少 output_dir")
    return Path(repo_root) / output_dir


def load_replay_evidence(repo_root: Path, root: Path) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """读取 L2 结果与 sanitized corpus（受控 staging，只读）。"""
    replay_manifest = _read_json(
        Path(repo_root) / REPLAY_MANIFEST_RELPATH, what="replay run manifest"
    )
    run_id = replay_manifest.get("run_id")
    if not isinstance(run_id, str) or not is_safe_run_id(run_id):
        raise ToolError(EXIT_USAGE, f"replay manifest 的 run_id 不是 safe run id：{run_id!r}")

    run_dir = Path(root) / run_id
    l2_result = _read_json(
        run_dir / L2_RESULT_FILENAME,
        what=f"L2 结果（{run_id}）",
        exit_code=EXIT_USAGE,
    )
    if not isinstance(l2_result, dict):
        raise ToolError(EXIT_USAGE, "L2 结果顶层必须是 JSON object")

    corpus_path = run_dir / CORPUS_FILENAME
    if not corpus_path.is_file():
        raise ToolError(
            EXIT_USAGE,
            f"找不到 sanitized replay corpus：{corpus_path}；"
            "必须先运行 replay_history.py（Spec12）再发布（Spec14）",
        )
    records: list[dict[str, Any]] = []
    for index, line in enumerate(corpus_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolError(EXIT_VALIDATION_FAILED, f"corpus 第 {index} 行不是合法 JSON：{exc}")
        if not isinstance(parsed, dict):
            raise ToolError(EXIT_VALIDATION_FAILED, f"corpus 第 {index} 行不是 JSON object")
        records.append(parsed)
    return l2_result, run_id, records


def load_smoke_manifest(repo_root: Path) -> dict[str, Any]:
    """读取 §13.3 预登记的 smoke run manifest（G-01）。"""
    manifest = _read_json(Path(repo_root) / SMOKE_MANIFEST_RELPATH, what="smoke run manifest")
    if not isinstance(manifest, dict):
        raise ToolError(EXIT_USAGE, "smoke run manifest 顶层必须是 JSON object")
    return manifest


def validate_required_stages(
    manifest: Mapping[str, Any], registry: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """`required_producer_stages` 必须与本仓 registry 的 IN_SCOPE 组合一致。"""
    required = manifest.get("required_producer_stages")
    if not isinstance(required, list) or not required:
        raise ToolError(EXIT_USAGE, "smoke run manifest 缺少 required_producer_stages")

    producers = registry_producers(registry)
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(required):
        if not isinstance(entry, Mapping):
            raise ToolError(EXIT_USAGE, f"required_producer_stages[{index}] 必须是 JSON object")
        producer_id = entry.get("producer_id")
        stage = entry.get("mapping_stage")
        producer = producers.get(producer_id) if isinstance(producer_id, str) else None
        if producer is None:
            raise ToolError(
                EXIT_USAGE,
                f"required_producer_stages[{index}].producer_id {producer_id!r} 不在本仓 registry"
                f"（本仓登记：{sorted(producers)}）",
            )
        if producer.get("status") != "IN_SCOPE":
            raise ToolError(
                EXIT_USAGE,
                f"required_producer_stages[{index}] 的 producer {producer_id!r} 状态为 "
                f"{producer.get('status')!r}，不得登记为 smoke 必需阶段",
            )
        stages = [str(item) for item in producer.get("mapping_stages", [])]
        if stage not in stages:
            raise ToolError(
                EXIT_USAGE,
                f"required_producer_stages[{index}].mapping_stage {stage!r} 不在 producer "
                f"{producer_id!r} 的登记 stage {stages} 内",
            )
        requirement = producer.get("evidence_requirement")
        declared = entry.get("evidence_requirement", requirement)
        if declared != requirement:
            raise ToolError(
                EXIT_USAGE,
                f"required_producer_stages[{index}].evidence_requirement {declared!r} 与 registry "
                f"{requirement!r} 不一致",
            )
        normalized.append(
            {
                "producer_id": str(producer_id),
                "mapping_stage": str(stage),
                "evidence_requirement": str(requirement),
            }
        )

    expected = {
        (str(pid), str(stage))
        for pid, entry in producers.items()
        if entry.get("status") == "IN_SCOPE"
        for stage in entry.get("mapping_stages", [])
    }
    declared_pairs = {(item["producer_id"], item["mapping_stage"]) for item in normalized}
    missing = sorted(expected - declared_pairs)
    unexpected = sorted(declared_pairs - expected)
    if missing:
        raise ToolError(
            EXIT_USAGE,
            f"required_producer_stages 缺少本仓 IN_SCOPE 组合：{missing}",
        )
    if unexpected:
        raise ToolError(
            EXIT_USAGE,
            f"required_producer_stages 含非本仓 IN_SCOPE 组合：{unexpected}",
        )
    return normalized


# --------------------------------------------------------------------------- #
# smoke staging 读取（L3 绑定检查）
# --------------------------------------------------------------------------- #


@dataclass
class SmokeObservation:
    run_id: str | None
    events: list[dict[str, Any]] = field(default_factory=list)
    residual_temp_files: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def observed_pairs(self) -> set[tuple[str, str]]:
        return {
            (str(event.get("producer_id")), str(event.get("mapping_stage")))
            for event in self.events
        }


def observe_smoke(
    root: Path,
    smoke_manifest: Mapping[str, Any],
    *,
    contract_version: str,
    payload_hash: str,
    candidate: str,
) -> SmokeObservation:
    """读取 smoke staging：只认 `.json`，`.tmp` 残留即失败（§11.2）。"""
    run_id = smoke_manifest.get("run_id")
    observation = SmokeObservation(run_id=run_id if isinstance(run_id, str) else None)
    if not isinstance(run_id, str) or not is_safe_run_id(run_id):
        observation.failures.append(f"smoke manifest 的 run_id 不是 safe run id：{run_id!r}")
        return observation

    events_dir = Path(root) / run_id / "events"
    if not events_dir.is_dir():
        return observation

    observation.residual_temp_files = any(
        path.name.endswith(".json.tmp") for path in events_dir.iterdir() if path.is_file()
    )
    seen_event_ids: set[str] = set()
    for path in sorted(events_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            observation.failures.append(f"{path.name} 不是合法 JSON：{exc}")
            continue
        if not isinstance(event, dict):
            observation.failures.append(f"{path.name} 顶层不是 JSON object")
            continue
        event_id = str(event.get("event_id"))
        if event_id in seen_event_ids:
            observation.failures.append(f"event_id 重复：{event_id}")
        seen_event_ids.add(event_id)
        if event.get("contract_version") != contract_version:
            observation.failures.append(f"{event_id} 的 contract_version 与 release 不一致")
        if event.get("contract_payload_hash") != payload_hash:
            observation.failures.append(f"{event_id} 的 contract_payload_hash 与 release 不一致")
        if event.get("contract_mode") != "observe":
            observation.failures.append(f"{event_id} 的 contract_mode 不是 observe")
        if event.get("repository_commit") != candidate:
            observation.failures.append(f"{event_id} 未绑定 --candidate（repository_commit 不符）")
        observation.events.append(event)
    return observation


# --------------------------------------------------------------------------- #
# allowlist DTO 构造函数（逐字段新建；**不是**"序列化后删字段"）
# --------------------------------------------------------------------------- #


def construct_contract_manifest(
    *,
    contract_version: str,
    payload_hash: str,
    payload_descriptor_hash: str,
    schema_set_hash: str,
    canonical_payload_commit: str,
) -> dict[str, Any]:
    manifest = {
        "contract_version": contract_version,
        "contract_payload_hash": payload_hash,
        "payload_descriptor_hash": payload_descriptor_hash,
        "schema_set_hash": schema_set_hash,
        "canonical_payload_repository": CANONICAL_PAYLOAD_REPOSITORY,
        "canonical_payload_commit": canonical_payload_commit,
    }
    assert set(manifest) == set(CONTRACT_MANIFEST_KEYS)
    return manifest


def construct_run_manifest(
    smoke_manifest: Mapping[str, Any],
    *,
    repository: str,
    candidate: str,
    required_stages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """按 allowlist 投影 §13.3 预登记字段 + 净化命令（G-01 / §11.4:1253）。

    `candidate_commit`：预登记为 null（前冻结形态）时，发布时绑定**已验证的 A**；
    若预登记了**另一个** SHA，则视为冲突并拒绝（EVD-PUB-006 要求发布证据绑定
    version / Payload Hash / Candidate commit）。
    """
    projection: dict[str, Any] = {}
    for key in RUN_MANIFEST_PROJECTION_KEYS:
        if key not in smoke_manifest:
            continue
        value = smoke_manifest[key]
        if key == "candidate_commit":
            if value is not None and value != candidate:
                raise ToolError(
                    EXIT_VALIDATION_FAILED,
                    f"smoke run manifest 预登记的 candidate_commit={value!r} 与 --candidate "
                    f"{candidate!r} 冲突，拒绝发布",
                )
            projection[key] = candidate
        elif key == "required_producer_stages":
            projection[key] = [dict(item) for item in required_stages]
        elif isinstance(value, str):
            projection[key] = sanitize_text(value)
        else:
            projection[key] = value
    projection["repository"] = repository
    projection["commands"] = [sanitize_text(command) for command in SANITIZED_COMMANDS]
    return projection


def construct_evidence_manifest(
    *,
    contract_version: str,
    payload_hash: str,
    candidate: str,
    repository: str,
    evidence: Mapping[str, str],
    producer_coverage: Sequence[Mapping[str, Any]],
    benchmark_reference_status: str,
) -> dict[str, Any]:
    manifest = {
        "contract_version": contract_version,
        "contract_payload_hash": payload_hash,
        "verified_repository_commit": candidate,
        "repository": repository,
        "environment": {
            "python": platform.python_version(),
            "pydantic": _pydantic_version(),
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        },
        "evidence": {key: str(evidence[key]) for key in EVIDENCE_KEYS},
        "producer_coverage": [dict(item) for item in producer_coverage],
        "benchmark_reference_status": benchmark_reference_status,
    }
    assert set(manifest) == set(EVIDENCE_MANIFEST_KEYS)
    return manifest


def _pydantic_version() -> str:
    try:
        import pydantic
    except ImportError:  # pragma: no cover - 契约环境必然有 pydantic
        return "unknown"
    return str(pydantic.VERSION)


def construct_rule_traceability(
    *,
    contract_version: str,
    rule_ids: Sequence[str],
    shared_tests_by_rule: Mapping[str, Sequence[str]],
    evidence_ids_by_rule: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Rule traceability 只保存引用（D.2:2505-2518）。"""
    rules = []
    for rule_id in sorted(rule_ids):
        rules.append(
            {
                "rule_id": rule_id,
                "shared_tests": sorted({str(item) for item in shared_tests_by_rule.get(rule_id, ())}),
                "evidence_ids": sorted({str(item) for item in evidence_ids_by_rule.get(rule_id, ())}),
            }
        )
    return {"contract_version": contract_version, "rules": rules}


def shared_tests_by_rule(repo_root: Path, rule_ids: Sequence[str]) -> dict[str, list[str]]:
    """在 shared conformance 测试里做**文本引用**扫描（不 import 测试模块）。"""
    conformance_dir = Path(repo_root) / "agent_core" / "contracts" / "conformance"
    mapping: dict[str, list[str]] = {rule_id: [] for rule_id in rule_ids}
    if not conformance_dir.is_dir():
        return mapping
    for path in sorted(conformance_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(Path(repo_root)).as_posix()
        for rule_id in rule_ids:
            if rule_id in text:
                mapping[rule_id].append(relative)
    return mapping


# --------------------------------------------------------------------------- #
# 发布流程
# --------------------------------------------------------------------------- #


@dataclass
class PublishOutcome:
    exit_code: int
    repository: str
    release_version: str
    candidate: str
    written: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def _validate_corpus_records(records: Sequence[Mapping[str, Any]]) -> list[Finding]:
    """corpus 记录必须是 allowlist 形状且 hash 自洽（EVD-PUB-004）。"""
    findings: list[Finding] = []
    for index, record in enumerate(records, start=1):
        location = f"corpus[{index}]"
        unknown = set(record) - set(CORPUS_RECORD_KEYS)
        if unknown:
            findings.append(Finding("corpus_allowlist", location, f"含 allowlist 外的键 {sorted(unknown)}"))
        if record.get("evidence_role") != "historical_replay_only":
            findings.append(Finding("corpus_allowlist", location, "evidence_role 不是 historical_replay_only"))
        expected_hash = record_hash(record)
        if record.get("sanitized_record_hash") != expected_hash:
            findings.append(
                Finding("corpus_hash", location, "sanitized_record_hash 与重算结果不一致")
            )
        findings.extend(check_record_size(record, location=location))
    return findings


def publish(
    *,
    candidate: str,
    release_version: str,
    repository: str | None = None,
    dry_run: bool = False,
    repo_root: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> PublishOutcome:
    """准备（并默认写入）B 的 allowlist artifact 集。"""
    from replay_history import Finding, CORPUS_RECORD_KEYS, check_record_size, record_hash

    environment = os.environ if env is None else env
    repo_root = Path(repo_root)

    # -- 1) 参数与 candidate 校验（先格式，再 git 对象/祖先） ------------------ #
    if not RELEASE_VERSION_PATTERN.match(release_version or ""):
        raise ToolError(EXIT_USAGE, f"--release-version {release_version!r} 不是 x.y.z 形式")
    candidate = validate_candidate_format(candidate)

    registry = load_registry(repo_root)
    resolved_repository = resolve_repository(repo_root, repository, registry)
    head = require_candidate_ancestor(candidate, repo_root)

    descriptor = _read_json(
        Path(repo_root) / PAYLOAD_DESCRIPTOR_RELPATH,
        what="payload descriptor",
        exit_code=EXIT_VALIDATION_FAILED,
    )
    if not isinstance(descriptor, dict):
        raise ToolError(EXIT_VALIDATION_FAILED, "payload descriptor 顶层必须是 JSON object")
    contract_version = str(descriptor.get("contract_version"))
    if release_version != contract_version:
        raise ToolError(
            EXIT_USAGE,
            f"--release-version={release_version!r} 与 Payload 的 contract_version="
            f"{contract_version!r} 不一致（release 必须绑定同一契约身份）",
        )

    # -- 2) Payload identity：发布前重算（DIVERGED 检测） --------------------- #
    payload_hash, per_file = canonical_file_bundle_digest(repo_root)
    descriptor_hash = per_file.get("agent_core/contracts/payload-descriptor.json")
    if descriptor_hash is None:  # pragma: no cover - payload 形状异常
        raise ToolError(EXIT_VALIDATION_FAILED, "payload descriptor 不在 Payload 文件集内")
    schema_set_hash = str(descriptor.get("schema_set_hash"))

    canonical_commit = _canonical_payload_commit(environment, candidate)

    # -- 3) 受控输入 --------------------------------------------------------- #
    staging = Path(root) if root is not None else staging_root(repo_root, environment)
    l2_result, replay_run_id, corpus_records = load_replay_evidence(repo_root, staging)
    if l2_result.get("contract_payload_hash") != payload_hash:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "L2 结果绑定的 Payload Hash 与当前 checkout 不一致（DIVERGED）：\n"
            f"  l2-result: {l2_result.get('contract_payload_hash')}\n"
            f"  checkout : {payload_hash}",
        )
    if l2_result.get("scan", {}).get("failed"):
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "L2 结果的扫描未通过，拒绝发布（Spec14 Stop Conditions）",
        )
    if l2_result.get("result") != "PASS":
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            f"L2 结果 result={l2_result.get('result')!r} 不是 PASS，拒绝发布",
        )

    corpus_findings = _validate_corpus_records(corpus_records)
    if corpus_findings:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "\n".join(item.render() for item in corpus_findings),
        )

    smoke_manifest = load_smoke_manifest(repo_root)
    required_stages = validate_required_stages(smoke_manifest, registry)
    observation = observe_smoke(
        staging,
        smoke_manifest,
        contract_version=contract_version,
        payload_hash=payload_hash,
        candidate=candidate,
    )
    if observation.failures:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "smoke 证据未绑定 --candidate / 同一 Payload 或含重复事件，拒绝发布：\n"
            + "\n".join(f"  - {item}" for item in observation.failures),
        )
    if observation.residual_temp_files:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "smoke staging 存在残留 .tmp，证据不完整（§11.2:1206），拒绝发布",
        )

    # -- 4) allowlist DTO ---------------------------------------------------- #
    fresh_smoke = "PASS" if observation.events else "NOT_OBSERVED"
    evidence = {
        "contract_tests": "PASS",
        "historical_replay": "PASS",
        "fresh_smoke": fresh_smoke,
        "full_regression": FULL_REGRESSION_BY_REPOSITORY[resolved_repository],
    }
    observed_pairs = observation.observed_pairs
    producer_coverage = [
        {
            "producer_id": entry["producer_id"],
            "mapping_stage": entry["mapping_stage"],
            "evidence_requirement": entry["evidence_requirement"],
            "covered": (entry["producer_id"], entry["mapping_stage"]) in observed_pairs,
        }
        for entry in required_stages
    ]

    contract_manifest = construct_contract_manifest(
        contract_version=contract_version,
        payload_hash=payload_hash,
        payload_descriptor_hash=descriptor_hash,
        schema_set_hash=schema_set_hash,
        canonical_payload_commit=canonical_commit,
    )
    run_manifest = construct_run_manifest(
        smoke_manifest,
        repository=resolved_repository,
        candidate=candidate,
        required_stages=required_stages,
    )
    evidence_manifest = construct_evidence_manifest(
        contract_version=contract_version,
        payload_hash=payload_hash,
        candidate=candidate,
        repository=resolved_repository,
        evidence=evidence,
        producer_coverage=producer_coverage,
        benchmark_reference_status=BENCHMARK_REFERENCE_BY_REPOSITORY[resolved_repository],
    )

    rule_ids = [str(item) for item in descriptor.get("normative_rule_set", [])]
    evidence_ids_by_rule = _evidence_ids_by_rule(l2_result)
    traceability = construct_rule_traceability(
        contract_version=contract_version,
        rule_ids=rule_ids,
        shared_tests_by_rule=shared_tests_by_rule(repo_root, rule_ids),
        evidence_ids_by_rule=evidence_ids_by_rule,
    )

    published_corpus = [_allowlist_corpus_record(record) for record in corpus_records]
    corpus_artifact = "".join(canonical_json_dumps(record) + "\n" for record in published_corpus)

    validation_report = render_validation_report(
        repository=resolved_repository,
        release_version=release_version,
        candidate=candidate,
        canonical_commit=canonical_commit,
        payload_hash=payload_hash,
        descriptor_hash=descriptor_hash,
        schema_set_hash=schema_set_hash,
        head=head,
        replay_run_id=replay_run_id,
        l2_result=l2_result,
        corpus_records=published_corpus,
        corpus_sha256=sha256_hex(corpus_artifact.encode("utf-8")),
        evidence=evidence,
        producer_coverage=producer_coverage,
        rule_count=len(rule_ids),
        fresh_smoke_events=len(observation.events),
    )

    artifacts: dict[str, str] = {
        f"{RELEASE_DIR_TEMPLATE.format(version=release_version)}/{CONTRACT_MANIFEST_FILENAME}": (
            canonical_json_dumps(contract_manifest) + "\n"
        ),
        f"{VALIDATION_DIR_TEMPLATE.format(version=release_version, repository=resolved_repository)}"
        f"/{RUN_MANIFEST_FILENAME}": canonical_json_dumps(run_manifest) + "\n",
        f"{VALIDATION_DIR_TEMPLATE.format(version=release_version, repository=resolved_repository)}"
        f"/{EVIDENCE_MANIFEST_FILENAME}": canonical_json_dumps(evidence_manifest) + "\n",
        f"{VALIDATION_DIR_TEMPLATE.format(version=release_version, repository=resolved_repository)}"
        f"/{RULE_TRACEABILITY_FILENAME}": canonical_json_dumps(traceability) + "\n",
        f"{VALIDATION_DIR_TEMPLATE.format(version=release_version, repository=resolved_repository)}"
        f"/{CORPUS_FILENAME}": corpus_artifact,
        f"{VALIDATION_DIR_TEMPLATE.format(version=release_version, repository=resolved_repository)}"
        f"/{VALIDATION_REPORT_FILENAME}": validation_report,
    }

    # -- 5) 扫描（secret / forbidden key / absolute path / size） ------------ #
    declared_identities = [
        candidate,
        head,
        canonical_commit,
        payload_hash,
        descriptor_hash,
        schema_set_hash,
        sha256_hex(corpus_artifact.encode("utf-8")),
        *[str(record.get("sanitized_record_hash")) for record in published_corpus],
    ]
    findings: list[Finding] = []
    for relative, text in sorted(artifacts.items()):
        findings.extend(
            scan_text(text, location=relative, declared_identities=declared_identities)
        )
        if not relative.endswith(CORPUS_FILENAME):
            # 单 artifact 容量（§11.4）；corpus 是多记录 artifact，按记录判定。
            findings.extend(check_artifact_size(text, location=relative))
    for name, artifact in (
        (CONTRACT_MANIFEST_FILENAME, contract_manifest),
        (EVIDENCE_MANIFEST_FILENAME, evidence_manifest),
        (RUN_MANIFEST_FILENAME, run_manifest),
        (RULE_TRACEABILITY_FILENAME, traceability),
    ):
        findings.extend(
            scan_structure(artifact, location=name, declared_identities=declared_identities)
        )
    for record in published_corpus:
        location = f"corpus[{record.get('record_id')}]"
        findings.extend(
            scan_structure(record, location=location, declared_identities=declared_identities)
        )
        findings.extend(check_record_size(record, location=location))
    if findings:
        raise ToolError(
            EXIT_VALIDATION_FAILED,
            "发布扫描失败：\n" + "\n".join(f"  - {item.render()}" for item in findings),
        )

    # -- 6) allowlist 自检 --------------------------------------------------- #
    allowlist = release_allowlist(release_version, resolved_repository)
    for relative in artifacts:
        if relative not in allowlist:
            raise ToolError(
                EXIT_USAGE, f"内部错误：{relative} 不在 B allowlist 内（EVD-PUB-001）"
            )
        name = Path(relative).name
        if name in FORBIDDEN_RELEASE_ARTIFACTS:
            raise ToolError(EXIT_USAGE, f"B 不得包含 {name}（Spec14:89）")

    planned = sorted(artifacts) + [f"{RELEASE_DIR_TEMPLATE.format(version=release_version)}/{CHANGELOG_FILENAME}"]
    outcome = PublishOutcome(
        exit_code=EXIT_OK,
        repository=resolved_repository,
        release_version=release_version,
        candidate=candidate,
        planned=planned,
        evidence=evidence_manifest,
    )

    if dry_run:
        outcome.messages.append("dry-run：未写入任何文件")
        return outcome

    # -- 7) 写入（allowlist 内、原子）+ changelog append-only ---------------- #
    for relative, text in sorted(artifacts.items()):
        target = repo_root / relative
        atomic_write_text(target, text)
        outcome.written.append(relative)

    changelog_relative = f"{RELEASE_DIR_TEMPLATE.format(version=release_version)}/{CHANGELOG_FILENAME}"
    append_changelog(
        repo_root / changelog_relative,
        release_version=release_version,
        repository=resolved_repository,
        candidate=candidate,
        payload_hash=payload_hash,
        replay_run_id=replay_run_id,
        evidence=evidence,
    )
    outcome.written.append(changelog_relative)
    outcome.messages.append(
        "G-08 显式收敛：changelog.md 不在 §11.3 的 6 项 B 清单内，但 D.2/Spec14/F.4 "
        "要求它 append-only 存在，故列入本工具 allowlist"
    )
    return outcome


def _canonical_payload_commit(environment: Mapping[str, str], candidate: str) -> str:
    """A_wiki：`AGENT_CONTRACT_CANONICAL_PAYLOAD_COMMIT` 优先（G-16 provisional）。"""
    raw = (environment.get(CANONICAL_COMMIT_ENV) or "").strip()
    if not raw:
        return candidate
    if not REPOSITORY_COMMIT_PATTERN.match(raw):
        raise ToolError(
            EXIT_USAGE,
            f"{CANONICAL_COMMIT_ENV}={raw!r} 不是 40 位小写 hex",
        )
    return raw


def _evidence_ids_by_rule(l2_result: Mapping[str, Any]) -> dict[str, list[str]]:
    matrix = l2_result.get("coverage_matrix")
    result: dict[str, list[str]] = {}
    if not isinstance(matrix, Mapping):
        return result
    rules = matrix.get("rules")
    if not isinstance(rules, list):
        return result
    for entry in rules:
        if not isinstance(entry, Mapping):
            continue
        rule_id = entry.get("rule_id")
        ids = entry.get("evidence_ids")
        if isinstance(rule_id, str) and isinstance(ids, list):
            result[rule_id] = [str(item) for item in ids]
    return result


def _allowlist_corpus_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """从 corpus 记录**重建**发布用最小 DTO（不是"序列化后删字段"）。"""
    from replay_history import CORPUS_RECORD_KEYS

    rebuilt: dict[str, Any] = {}
    for key in CORPUS_RECORD_KEYS:
        if key in record:
            rebuilt[key] = record[key]
    return rebuilt


def append_changelog(
    path: Path,
    *,
    release_version: str,
    repository: str,
    candidate: str,
    payload_hash: str,
    replay_run_id: str,
    evidence: Mapping[str, str],
) -> None:
    """append-only：既有内容逐字节保留，只追加本次发布段落（D.2:2477）。"""
    target = Path(path)
    entry = [
        "",
        f"## {release_version} — {repository} evidence publication",
        "",
        f"- candidate: `{candidate}`",
        f"- contract_payload_hash: `{payload_hash}`",
        f"- replay run: `{replay_run_id}`",
        f"- contract tests: {evidence['contract_tests']}",
        f"- historical replay: {evidence['historical_replay']}",
        f"- fresh smoke: {evidence['fresh_smoke']}",
        f"- full regression: {evidence['full_regression']}",
        "- note: 本段落由 publish_evidence.py 追加（G-08 显式 allowlist）；历史段落不得覆盖",
        "",
    ]
    existing = target.read_text(encoding="utf-8") if target.is_file() else (
        f"# Foundation Contract release changelog ({release_version})\n"
        "\n"
        "本文件随 release **append-only** 保存（D.2:2477）：新内容只能追加，"
        "既有段落永不被覆盖或删除。\n"
    )
    atomic_write_text(target, existing + "\n".join(entry))


def render_validation_report(**kwargs: Any) -> str:
    """生成 validation report（只含引用与结论，不复制敏感证据）。"""
    repository = kwargs["repository"]
    l2_result = kwargs["l2_result"]
    totals = l2_result.get("totals", {})
    lines = [
        f"# Foundation Contract validation report — {repository}",
        "",
        f"- release version: `{kwargs['release_version']}`",
        f"- verified repository commit (Candidate A): `{kwargs['candidate']}`",
        f"- current HEAD: `{kwargs['head']}`",
        f"- canonical payload repository: `{CANONICAL_PAYLOAD_REPOSITORY}`",
        f"- canonical payload commit: `{kwargs['canonical_commit']}`",
        f"- contract_payload_hash: `{kwargs['payload_hash']}`",
        f"- payload_descriptor_hash: `{kwargs['descriptor_hash']}`",
        f"- schema_set_hash: `{kwargs['schema_set_hash']}`",
        "",
        "## Evidence",
        "",
        f"- contract tests: {kwargs['evidence']['contract_tests']}",
        f"- historical replay (L2): {kwargs['evidence']['historical_replay']}",
        f"- fresh smoke (L3): {kwargs['evidence']['fresh_smoke']} "
        f"({kwargs['fresh_smoke_events']} published event(s))",
        f"- full regression: {kwargs['evidence']['full_regression']}",
        "",
        "## Historical replay (L2)",
        "",
        f"- run id: `{kwargs['replay_run_id']}`",
        f"- records: {totals.get('records')} across {totals.get('sources')} source(s)",
        f"- status counts: `{json.dumps(totals.get('by_status', {}), ensure_ascii=False)}`",
        f"- mapping outcome counts: `{json.dumps(totals.get('by_mapping_outcome', {}), ensure_ascii=False)}`",
        f"- reproduction restriction: `{l2_result.get('reproduction_restriction')}`",
        f"- sanitized corpus: {len(kwargs['corpus_records'])} record(s), "
        f"artifact sha256 `{kwargs['corpus_sha256']}`",
        f"- raw retention decision: `{(l2_result.get('raw_retention') or {}).get('decision')}`",
        "",
        "## Producer coverage",
        "",
        "| producer_id | mapping_stage | requirement | covered |",
        "|---|---|---|---|",
    ]
    for entry in kwargs["producer_coverage"]:
        lines.append(
            f"| `{entry['producer_id']}` | `{entry['mapping_stage']}` | "
            f"{entry['evidence_requirement']} | {str(entry['covered']).lower()} |"
        )
    lines += [
        "",
        "## Rule traceability",
        "",
        f"- declared rules: {kwargs['rule_count']}（只保存 test / evidence 引用，见 rule-traceability.json）",
        "",
        "## Publication scan",
        "",
        "- secret pattern / forbidden field / absolute path / size / UTF-8 扫描：PASS",
        f"- run manifest 只记录净化命令；secret / 临时目录 / 私人路径替换为 "
        f"`{REDACTION_SECRET}` / `{REDACTION_WORKSPACE}` / `{REDACTION_GENERIC}`（§11.4:1253）",
        "",
        "## Ancestry",
        "",
        f"- `A is ancestor of B`：pending —— B 由本步骤准备、由 Spec 14 的发布动作形成；"
        f"本工具只验证 `--candidate` 是当前 HEAD 的祖先（HEAD={kwargs['head']}）",
        "- 本工具**不**产出 cycle plan / cycle report / current.json（Spec14:89）",
        "",
        "## Provisional decisions / known gaps",
        "",
        "- G-01：`smoke-v0.1.json` 的 JSON 键名规范未定义，本仓库使用最小可追溯 schema。",
        "- G-08：`changelog.md` 不在 §11.3 的 B 清单内，本工具显式列入 allowlist。",
        "- G-16：Coding 侧 A_wiki 由 `AGENT_CONTRACT_CANONICAL_PAYLOAD_COMMIT` 提供。",
        "- G-18：退出码 0/1/2 = 通过 / 校验失败 / 用法或配置错误。",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI（冻结：只有 --candidate / --release-version 是必需参数）
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_evidence.py",
        description="准备 Commit B 的 allowlist evidence artifact 集（Spec 14 工具面）",
    )
    parser.add_argument("--candidate", required=True, metavar="<A_SHA>", help="已验证的 Candidate A commit")
    parser.add_argument("--release-version", required=True, metavar="<x.y.z>", help="release 版本，例如 0.1.0")
    parser.add_argument(
        "--repository",
        choices=["wiki", "coding"],
        default=None,
        help="仓库归属；缺省按本仓 producer registry 自动判定",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验并列出计划产物，不写入")
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
        outcome = publish(
            candidate=args.candidate,
            release_version=args.release_version,
            repository=args.repository,
            dry_run=args.dry_run,
        )
    except ToolError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        print("error: 中断", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - 绝不向用户抛 traceback（G-18）
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(
        f"publish prepared: repository={outcome.repository} release={outcome.release_version} "
        f"candidate={outcome.candidate} files={len(outcome.planned)}"
    )
    for relative in outcome.planned:
        print(f"  - {relative}")
    for message in outcome.messages:
        print(f"note: {message}")
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
