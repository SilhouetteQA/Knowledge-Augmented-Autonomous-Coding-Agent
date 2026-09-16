#!/usr/bin/env python
"""Spec 13 — L3 Fresh Smoke 受控驱动（Coding 仓）。

母 Spec §13.3 / Spec 13 `Validation Commands` 只给出**校验**命令
（`validate_local.py --gate smoke`），没有任何命令真正驱动业务路径 —— 于是
`<evidence_root>/<run_id>/run-summary.json` 永不存在、预登记 `case_ids` 无法被选中、
L3 无法闭合。本脚本是那个缺失的驱动，本身**不是** workflow、不是契约实现：

```text
python scripts/contracts/run_l3_smoke.py --run-manifest config/contracts/smoke-v0.1.json
python scripts/contracts/run_l3_smoke.py --run-manifest <manifest> --case-source <dir> --json
```

它做四件事，全部以**已落盘证据**为准，不伪造、不补写：

1. 用与 `validate_local.gate_smoke()` **同一**必需键集校验 run manifest（冻结，测试逐一比对）。
2. 把 manifest 的 `case_ids` 解析到真实存在的 case 源（`benchmark/cases/**` 或
   `--case-source`），缺任一预登记 id 即以 `SPEC_INCOMPLETE`（退出码 2）中止 ——
   绝不静默换成别的 case。
3. 在 `AGENT_CONTRACT_MODE=observe` 下以子进程驱动**真实业务路径**
   （`python main.py --benchmark --cases <staged> ...`），遵守 manifest 的
   `timeout_seconds`，量测 wall-clock。
4. 独立读回 `<evidence_root>/<run_id>/events/`，据此写出
   `<evidence_root>/<run_id>/run-summary.json`（键集 = `RUN_SUMMARY_KEYS`，值全部由
   证据推导），并按预登记策略判定覆盖闭合。

产物位置（Master §11.5：原始业务记录**不得**进入 staging）
----------------------------------------------------------

```text
<evidence_root>/<run_id>/events/<event_id>.json   ← sink 产出（manifest.evidence_root）
<evidence_root>/<run_id>/run-summary.json         ← 本脚本产出（gate 的消费契约）
<evidence_root>/<run_id>/sink-failures.jsonl      ← sink 失败标记（缺文件 = 零失败）
<scratch_root>/<run_id>/cases/                    ← 预登记 case 的 staging 选择副本
<scratch_root>/<run_id>/benchmark/                ← --benchmark-out
<scratch_root>/<run_id>/workspace/                ← --workspace（克隆/测试工作区）
```

`scratch_root` 缺省 `output/contract-validation/run-scratch`（可用
`KA_L3_SMOKE_SCRATCH_DIR` 覆盖），**刻意不放在 staging 下**：benchmark report 含
`diff` 字段与模型自由文本，而 `validate_local.py --gate pr` 的发布扫描对整个 staging
目录禁止 `diff` 键与绝对路径（§11.4），业务产物落进去会让 L1 gate 失败。

provisional / 已知缺口（G-18 退出码，与 Spec 10 同口径）
------------------------------------------------------

```text
0 = 通过（证据闭合）
1 = 校验失败          → stderr 含 SPEC_STATUS_CONFLICT
2 = 用法或配置错误    → stderr 含 SPEC_INCOMPLETE
```

* 与 `gate_smoke` 的唯一**有意**差异，已在测试中固定：运行 commit 不可解析
  （`repository_commit: null` 且无 `AGENT_CONTRACT_COMMIT`）在本脚本记为配置错误（2）：
  业务尚未运行、无证据可校验；gate 对同一条件记 1。
* 覆盖判定与 `gate_smoke` 第 7 步**同款逐对**语义（`ALL_STAGES` 逐对、`ONE_OF` 同 producer
  成组、`NOT_OBSERVED_ALLOWED` 豁免但必须点名）。本脚本不修改 gate，只如实报告未观测项。
* `KA_L3_SMOKE_BUSINESS_CMD` 是**测试/fixture seam**：指向一个 Python 脚本即可替换
  真实业务命令（用于无网络、无密钥的合成证据端到端验证）。真实 L3 不设置它；
  设置时脚本会在 stdout 与 JSON 里显式标注 `fixture_seam=true`。

覆盖可达性：两类不同的改动，不得混同（母 Spec §7.3 / N-04）
--------------------------------------------------------

`config/contracts/smoke-v0.1.json`（冻结）预登记的七对 stage，在当前 Candidate A 接线下的
可达性并不相同 —— 本驱动**如实报告未观测项**，不补写、不伪造：

```text
coding.agent.llm_usage / openai_compat            可达：agent/llm.py::chat 每次 provider 调用
coding.trace.generation_usage / langfuse_generation  可达：tools/tracing.py::record_usage（同一调用，独立事件；不依赖 Langfuse 凭据）
coding.benchmark.case_cost / normal               可达：benchmark/runner.py 的正常分支
coding.benchmark.case_cost / environment_error|error  **每 case 恰好一个**（同一 runner 的三个互斥分支）
coding.trace.summary / sdk|clickhouse              benchmark 路径**不可达**：唯一 producer 在
                                                  tools/report_trace.py，只经 `main.py --trace-report`
                                                  （Langfuse SDK 或 ClickHouse 127.0.0.1:8123）触达，
                                                  且每次 fetch 只走其中一条（SDK 优先、CH 回退）
```

冻结 manifest 只预登记一个 case（`schedule-99`），因此 `case_cost` 的三个 stage 在一次 run
内不可能同时出现；`trace.summary` 的两条路径在 `--benchmark` 下都不可达。据此，本轮把相关
逐对 `evidence_requirement` 收敛为 `NOT_OBSERVED_ALLOWED`，但**两类改动性质不同**：

* **(A) 规范回归（不是偏离）**：`case_cost` / `environment_error` / `error`。母 Spec §7.3:896
  逐字写着"`normal` required；错误 stage 可 `NOT_OBSERVED`"；冻结 manifest 曾把三条都登记为
  `ALL_STAGES`，与 §7.3 直接矛盾，属实现缺陷，本轮修回规范。
* **(B) 用户授权偏离（依 N-04）**：`trace.summary` / `sdk` + `clickhouse`。§7.3:897 的 L3 硬
  要求是 `ONE_OF(sdk, clickhouse)`，但本机无 Langfuse 凭据、无 ClickHouse 实例，无法观测；
  用户已授权标记为 `NOT_OBSERVED_ALLOWED` 并把偏离记录在
  `docs/plans/2026-09-16-foundation-contract-spec11-stage0-calibration.md` §10 (B2)。
  恢复条件：提供 Langfuse 凭据或 ClickHouse 实例，然后按 §7.3 恢复 `ONE_OF`。

无论 (A) 还是 (B)，`NOT_OBSERVED_ALLOWED` 都**不是通过**：它们不进 violation 列表
（不使退出码变 1），但会出现在 `authorized_unobserved` 与报告的"未观测（授权豁免）"段，
既不算 covered 也不许说成已覆盖。跨仓约定见 §10.3。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Sequence

#: 直接以脚本方式运行时 ``sys.path[0]`` 是脚本目录，仓库根不在其中（与
#: `validate_local.py` / `replay_history.py` 同一个自举约定）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REPO_ROOT: Final[Path] = _REPO_ROOT

# --------------------------------------------------------------------------- #
# 退出码与 greppable marker（G-18 provisional）
# --------------------------------------------------------------------------- #

EXIT_OK: Final[int] = 0
EXIT_VALIDATION_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2

MARKER_STATUS_CONFLICT: Final[str] = "SPEC_STATUS_CONFLICT"
MARKER_INCOMPLETE: Final[str] = "SPEC_INCOMPLETE"

# --------------------------------------------------------------------------- #
# 环境变量与常量
# --------------------------------------------------------------------------- #

MODE_ENV: Final[str] = "AGENT_CONTRACT_MODE"
RUN_ID_ENV: Final[str] = "AGENT_CONTRACT_RUN_ID"
COMMIT_ENV: Final[str] = "AGENT_CONTRACT_COMMIT"
EVIDENCE_DIR_ENV: Final[str] = "AGENT_CONTRACT_EVIDENCE_DIR"
SCRATCH_DIR_ENV: Final[str] = "KA_L3_SMOKE_SCRATCH_DIR"
BUSINESS_COMMAND_ENV: Final[str] = "KA_L3_SMOKE_BUSINESS_CMD"

OBSERVE_MODE: Final[str] = "observe"

RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")

DEFAULT_CASE_SOURCE: Final[str] = "benchmark/cases"
DEFAULT_SCRATCH_ROOT: Final[Path] = Path("output") / "contract-validation" / "run-scratch"

#: benchmark 案例目录的类别名（与 `benchmark/loader.py::CATEGORIES` 一致）。
CASE_CATEGORIES: Final[tuple[str, ...]] = ("bug", "feature", "test", "refactor", "domain")

MAIN_RELPATH: Final[Path] = Path("main.py")
RUN_SUMMARY_FILENAME: Final[str] = "run-summary.json"
EVENTS_DIRNAME: Final[str] = "events"
CASES_DIRNAME: Final[str] = "cases"
BENCHMARK_OUT_DIRNAME: Final[str] = "benchmark"
WORKSPACE_DIRNAME: Final[str] = "workspace"
TEMP_SUFFIX: Final[str] = ".tmp"

#: `validate_local.gate_smoke()` 的必需键集（冻结；不得增删）。
#: 测试对每一项逐键比对 gate 的行为（删键后两边都必须拒绝）。
RUN_MANIFEST_REQUIRED_KEYS: Final[tuple[str, ...]] = (
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
)

#: `validate_local.RUN_SUMMARY_KEYS` 的同一冻结键集；本脚本只写这些键。
RUN_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "sink_failure_count",
    "rejected_records",
    "actual_calls",
    "actual_tokens",
    "duration_seconds",
    "producer_coverage",
    "known_cost_components",
    "unknown_cost_components",
)

#: token 字段名（`agent_core.contracts.models.usage::TOKEN_FIELDS` 的汇总子集）。
TOKEN_FIELDS: Final[tuple[str, ...]] = ("input_tokens", "output_tokens", "total_tokens")

REQUIREMENT_ALL_STAGES: Final[str] = "ALL_STAGES"
REQUIREMENT_ONE_OF: Final[str] = "ONE_OF"
#: 母 Spec §7.3:896「`normal` required；错误 stage 可 `NOT_OBSERVED`」的**逐对**形态。
#: 该对未被观测到不构成闭合失败（§7.3:887"未观察到"不等于失败），但必须被逐对点名 ——
#: 绝不静默通过、绝不伪报覆盖。顶层 `coverage_policy` **不接受**该值（整仓放宽会掩盖未观测）。
REQUIREMENT_NOT_OBSERVED_ALLOWED: Final[str] = "NOT_OBSERVED_ALLOWED"
PER_PAIR_REQUIREMENTS: Final[tuple[str, ...]] = (
    REQUIREMENT_ALL_STAGES,
    REQUIREMENT_ONE_OF,
    REQUIREMENT_NOT_OBSERVED_ALLOWED,
)


class ToolError(RuntimeError):
    """带退出码与 stderr marker 的干净失败（绝不打印 traceback）。"""

    def __init__(self, exit_code: int, marker: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.marker = marker
        self.message = message


def _incomplete(message: str) -> ToolError:
    """用法/配置错误（退出码 2）。"""
    return ToolError(EXIT_USAGE, MARKER_INCOMPLETE, message)


def _conflict(message: str) -> ToolError:
    """校验失败（退出码 1）。"""
    return ToolError(EXIT_VALIDATION_FAILED, MARKER_STATUS_CONFLICT, message)


def _force_utf8_streams() -> None:
    """stdout/stderr 强制 UTF-8（Windows runner 默认 cp1252，中文会 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 非文本流或已关闭的流
            pass


# --------------------------------------------------------------------------- #
# run manifest：加载与校验
# --------------------------------------------------------------------------- #


def load_run_manifest(path: Path) -> dict[str, Any]:
    """读取 run manifest；不存在/非 JSON/非对象 → 配置错误（2）。"""
    if not path.is_file():
        raise _incomplete(f"run manifest 不存在：{path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _incomplete(f"run manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise _incomplete("run manifest 顶层必须是 JSON object")
    return manifest


def validate_run_manifest(manifest: dict[str, Any]) -> None:
    """与 `gate_smoke()` 同一必需键集 + 驱动自身需要的形状校验。"""
    missing = sorted(set(RUN_MANIFEST_REQUIRED_KEYS) - set(manifest))
    if missing:
        raise _incomplete(
            f"run manifest 缺少字段 {missing}；"
            "SPEC_INCOMPLETE：smoke-v0.1.json 的键名是 Spec 10 provisional 收敛"
        )
    if not isinstance(manifest["run_id"], str) or not RUN_ID_PATTERN.match(manifest["run_id"]):
        raise _incomplete(f"run manifest 的 run_id 不是 safe run id：{manifest['run_id']!r}")
    if manifest["contract_mode"] != OBSERVE_MODE:
        raise _incomplete(
            f"run manifest 的 contract_mode 必须是 {OBSERVE_MODE!r}，实际 "
            f"{manifest['contract_mode']!r}（L3 只允许 observe）"
        )
    policy = manifest["coverage_policy"]
    if policy not in (REQUIREMENT_ALL_STAGES, REQUIREMENT_ONE_OF):
        raise _incomplete(f"coverage_policy 非法：{policy!r}")
    if not isinstance(manifest["evidence_root"], str) or not manifest["evidence_root"]:
        raise _incomplete("evidence_root 必须是非空字符串（仓库相对路径或绝对路径）")
    stages = manifest["required_producer_stages"]
    if not isinstance(stages, list) or not stages:
        raise _incomplete("required_producer_stages 必须是非空列表")
    for index, item in enumerate(stages):
        if not isinstance(item, dict):
            raise _incomplete(f"required_producer_stages[{index}] 必须是 JSON object")
        for key in ("producer_id", "mapping_stage"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise _incomplete(f"required_producer_stages[{index}].{key} 必须是非空字符串")
        requirement = item.get("evidence_requirement", policy)
        if requirement not in PER_PAIR_REQUIREMENTS:
            raise _incomplete(
                f"required_producer_stages[{index}].evidence_requirement 非法：{requirement!r}"
                f"（逐对闭集 {list(PER_PAIR_REQUIREMENTS)}）"
            )
    case_ids = manifest["case_ids"]
    if not isinstance(case_ids, list) or not case_ids or not all(
        isinstance(item, str) and item for item in case_ids
    ):
        raise _incomplete("case_ids 必须是非空字符串列表（预登记、不得事后替换）")
    for key in ("timeout_seconds", "duration_cap_seconds"):
        value = manifest[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise _incomplete(f"{key} 必须是正数（got {value!r}）")


def resolve_run_id(manifest: dict[str, Any], env: dict[str, str]) -> str:
    """显式 run_id 必须与 manifest 一致，且 `AGENT_CONTRACT_MODE == observe`。"""
    env_mode = env.get(MODE_ENV)
    if env_mode != OBSERVE_MODE:
        raise _incomplete(
            f"FND-MODE-001：需要 {MODE_ENV}={OBSERVE_MODE}，实际 {env_mode!r}"
        )
    run_id = manifest["run_id"]
    env_run_id = env.get(RUN_ID_ENV)
    if env_run_id != run_id:
        raise _incomplete(
            f"EVD-RUN-001：显式 run_id 与 manifest 不一致（env={env_run_id!r}, "
            f"manifest={run_id!r}）"
        )
    return run_id


def resolve_run_commit(manifest: dict[str, Any], env: dict[str, str]) -> str:
    """与 `validate_local._resolve_manifest_commit` 同语义：声明值优先，否则读环境。

    `null` = 运行期由 `AGENT_CONTRACT_COMMIT` 解析，**不自动推断 HEAD**（用错 commit
    会伪造证据绑定，EVD-RUN-002）。不可解析记配置错误（2），见模块 docstring。
    """
    declared = manifest.get("repository_commit")
    if declared is None:
        resolved = (env.get(COMMIT_ENV) or "").strip()
        if not resolved:
            raise _incomplete(
                "run manifest 的 repository_commit 为 null，必须由 "
                f"{COMMIT_ENV} 提供运行 commit（G-01 provisional：不自动推断 HEAD）"
            )
        if not COMMIT_PATTERN.match(resolved):
            raise _incomplete(f"{COMMIT_ENV} 不是 40 位小写 hex：{resolved!r}")
        return resolved
    text = str(declared)
    if not COMMIT_PATTERN.match(text):
        raise _incomplete(f"run manifest 的 repository_commit 形状非法：{declared!r}")
    return text


def resolve_evidence_root(manifest: dict[str, Any]) -> Path:
    """evidence_root：绝对路径原样使用，相对路径按仓库根解析（与 gate 同构）。"""
    root = Path(manifest["evidence_root"])
    return root if root.is_absolute() else REPO_ROOT / root


def resolve_scratch_root(env: dict[str, str]) -> Path:
    """业务运行 scratch 根（§11.5：原始业务记录不得进入 staging）。"""
    raw = (env.get(SCRATCH_DIR_ENV) or "").strip()
    root = Path(raw) if raw else DEFAULT_SCRATCH_ROOT
    return root if root.is_absolute() else REPO_ROOT / root


# --------------------------------------------------------------------------- #
# case 选择（B3）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CaseSelection:
    """一个预登记 case 在源目录中的落点。"""

    case_id: str
    path: Path


def resolve_case_source(raw: str | None) -> Path:
    """case 源目录：`--case-source` 优先（相对 CWD），否则仓库内 `benchmark/cases`。"""
    if raw:
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else Path.cwd() / candidate
    return REPO_ROOT / DEFAULT_CASE_SOURCE


def select_cases(case_source: Path, case_ids: Sequence[str]) -> list[CaseSelection]:
    """把预登记的 case id 解析到真实存在的 case 文件。

    选择规则 = `benchmark/loader.load_cases` 的目录约定（`<category>/<id>.json`）。
    任一预登记 id 找不到即 `SPEC_INCOMPLETE`（退出码 2）：绝不静默跑别的 case、
    也绝不"跳过缺失项继续跑"。
    """
    if not case_source.is_dir():
        raise _incomplete(f"case 源目录不存在：{case_source}")

    found: dict[str, Path] = {}
    for category in CASE_CATEGORIES:
        category_dir = case_source / category
        if not category_dir.is_dir():
            continue
        for path in sorted(category_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            case_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(case_id, str) and case_id and case_id not in found:
                found[case_id] = path

    missing = [case_id for case_id in case_ids if case_id not in found]
    if missing:
        raise _incomplete(
            f"预登记 case id 在 case 源中不存在：{missing}（源目录 {case_source}）；"
            "SPEC_INCOMPLETE（B3）：不得替换为其它 case"
        )
    return [CaseSelection(case_id=case_id, path=found[case_id]) for case_id in case_ids]


def materialize_cases(selected: Sequence[CaseSelection], target_root: Path) -> Path:
    """把预登记 case 复制成 staging 选择目录（保持 gold 相对布局）。

    业务 CLI 没有 case 过滤参数（`main.py --benchmark` 跑整个 `--cases` 目录），因此
    "只跑预登记 case" 由**目录内容**表达：只把选中的 case 与其 gold patch 复制进去。
    这样不需要改动业务 CLI，也不会跑到任何未预登记的 case。
    """
    for item in selected:
        data = json.loads(item.path.read_text(encoding="utf-8"))
        category = data.get("category")
        if category not in CASE_CATEGORIES:
            raise _incomplete(
                f"case {item.case_id!r} 的 category 非法：{category!r}"
                f"（应为 {list(CASE_CATEGORIES)}）"
            )
        category_dir = target_root / str(category)
        category_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item.path, category_dir / item.path.name)
        gold = data.get("gold_patch")
        if isinstance(gold, str) and gold and ".." not in Path(gold).parts:
            gold_source = item.path.parent.parent / gold
            if gold_source.is_file():
                gold_target = target_root / gold
                gold_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(gold_source, gold_target)
    return target_root


def build_business_command(staged_cases: Path, scratch_run_dir: Path) -> list[str]:
    """真实业务命令：整条 benchmark 路径（repo 就绪 → 基线 → Agent → 判定 → 报告）。

    `--workspace` / `--benchmark-out` 都落在 scratch（§11.5），不落 staging。
    """
    return [
        sys.executable,
        str(REPO_ROOT / MAIN_RELPATH),
        "--benchmark",
        "--cases",
        str(staged_cases),
        "--benchmark-out",
        str(scratch_run_dir / BENCHMARK_OUT_DIRNAME),
        "--workspace",
        str(scratch_run_dir / WORKSPACE_DIRNAME),
    ]


def resolve_business_command(
    staged_cases: Path, scratch_run_dir: Path, env: dict[str, str]
) -> tuple[list[str], bool]:
    """返回 ``(argv, fixture_seam)``；seam 只用于无网络/无密钥的合成证据测试。"""
    override = (env.get(BUSINESS_COMMAND_ENV) or "").strip()
    if not override:
        return build_business_command(staged_cases, scratch_run_dir), False
    script = Path(override)
    if not script.is_file():
        raise _incomplete(f"{BUSINESS_COMMAND_ENV} 指向的脚本不存在：{script}")
    return [sys.executable, str(script)], True


# --------------------------------------------------------------------------- #
# 驱动子进程
# --------------------------------------------------------------------------- #


@dataclass
class BusinessRun:
    """一次业务子进程的结果（诊断文本只回显，不落 staging）。"""

    command: list[str]
    returncode: int | None
    timed_out: bool
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""


def run_business_path(
    command: Sequence[str], env: dict[str, str], timeout_seconds: float
) -> BusinessRun:
    """在 observe 环境下同步驱动业务路径，量测 wall-clock。"""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        return BusinessRun(
            command=list(command),
            returncode=None,
            timed_out=True,
            duration_seconds=elapsed,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        )
    except OSError as exc:
        elapsed = time.monotonic() - started
        return BusinessRun(
            command=list(command),
            returncode=None,
            timed_out=False,
            duration_seconds=elapsed,
            stderr=f"{type(exc).__name__}: {exc}",
        )
    return BusinessRun(
        command=list(command),
        returncode=proc.returncode,
        timed_out=False,
        duration_seconds=time.monotonic() - started,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --------------------------------------------------------------------------- #
# 证据读回与 run-summary 组装
# --------------------------------------------------------------------------- #


def read_events(events_dir: Path) -> tuple[list[Any], list[str]]:
    """读回已发布事件（`EvidenceRecord`）；返回 ``(records, violations)``。

    只认 `.json`（忽略 `.tmp`，§11.1）；校验失败的记录计入 violations 并跳过。
    """
    from adapters.foundation.evidence_sink import iter_published_events
    from agent_core.contracts.models.evidence import EvidenceRecord

    records: list[Any] = []
    violations: list[str] = []
    if not events_dir.is_dir():
        return records, [f"事件目录不存在：{events_dir}"]
    for path in iter_published_events(events_dir):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            violations.append(f"{path.name}: 事件不可读（{exc}）")
            continue
        try:
            records.append(EvidenceRecord.model_validate(raw))
        except Exception as exc:  # pydantic ValidationError 与自定义校验错误
            violations.append(f"{path.name}: EvidenceRecord 校验失败（{exc}）")
    return records, violations


def read_sink_failures(run_dir: Path) -> list[dict[str, Any]]:
    """读 run 级 sink 失败标记；**缺文件 = 零失败**（G-04 provisional）。"""
    from adapters.foundation.evidence_sink import FAILURES_FILENAME

    path = run_dir / FAILURES_FILENAME
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {"code": "unparsable-marker", "raw": line[:200]}
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def summarize_evidence(records: Sequence[Any], duration_seconds: float) -> dict[str, Any]:
    """由已发布证据推导 run-summary 的六个证据字段（不硬编码、不补零）。

    ```text
    producer_coverage          = 观测到的 (producer_id, mapping_stage) 去重集合
    actual_calls               = usage.source == provider_reported 的观测条数
    actual_tokens              = usage 中非 null 计数的求和（provider total 不重算）
    known/unknown_cost_components = cost / cost_summary 证据里的已知/未知组成项计数
    ```
    """
    coverage: set[tuple[str, str]] = set()
    calls = 0
    tokens: dict[str, int] = {name: 0 for name in TOKEN_FIELDS}
    known = 0
    unknown = 0

    for record in records:
        coverage.add((str(record.producer_id), str(record.mapping_stage)))
        observation = getattr(record, "foundation_output", None)
        if observation is None:
            continue
        usage = getattr(observation, "usage", None)
        if usage is not None:
            if str(usage.source) == "provider_reported":
                calls += 1
            for name in TOKEN_FIELDS:
                value = getattr(usage, name, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens[name] += value
        summary = getattr(observation, "cost_summary", None)
        cost = getattr(observation, "cost", None)
        if summary is not None:
            known += int(getattr(summary, "known_component_count", 0) or 0)
            unknown += int(getattr(summary, "unknown_component_count", 0) or 0)
        elif cost is not None:
            if getattr(cost, "amount", None) is None:
                unknown += 1
            else:
                known += 1

    return {
        "actual_calls": calls,
        "actual_tokens": dict(tokens),
        "duration_seconds": round(float(duration_seconds), 3),
        "producer_coverage": [[producer, stage] for producer, stage in sorted(coverage)],
        "known_cost_components": known,
        "unknown_cost_components": unknown,
    }


def write_run_summary(run_dir: Path, summary: dict[str, Any]) -> Path:
    """原子写出 run-summary.json（键集 = ``RUN_SUMMARY_KEYS``，逐键断言）。"""
    from agent_core.contracts.models.base import canonical_json_dumps

    unexpected = sorted(set(summary) - set(RUN_SUMMARY_KEYS))
    missing = sorted(set(RUN_SUMMARY_KEYS) - set(summary))
    if missing or unexpected:
        raise _conflict(
            f"run-summary 键集与 RUN_SUMMARY_KEYS 不一致（缺 {missing} / 多 {unexpected}）"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / RUN_SUMMARY_FILENAME
    temp = target.with_name(target.name + TEMP_SUFFIX)
    payload = (canonical_json_dumps(summary) + "\n").encode("utf-8")
    with open(temp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return target


# --------------------------------------------------------------------------- #
# 闭合判定（镜像 gate_smoke 的第 2–8 步；不改动 gate）
# --------------------------------------------------------------------------- #


def unobserved_pairs(
    manifest: dict[str, Any], observed: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """按**逐对** `evidence_requirement` 判定**构成闭合失败**的未观测对（Spec 13）。

    `ALL_STAGES`：该 producer 的每个预登记 stage 都要观测到。
    `ONE_OF`：该 producer 的预登记 stage 至少观测到一个。
    `NOT_OBSERVED_ALLOWED`（§7.3:896）：**不计入**失败集 —— 它的未观测项由
    :func:`authorized_unobserved_pairs` 单独点名，绝不与"通过"混为一谈。
    """
    policy = manifest["coverage_policy"]
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for item in manifest["required_producer_stages"]:
        pair = (str(item["producer_id"]), str(item["mapping_stage"]))
        requirement = item.get("evidence_requirement", policy)
        groups.setdefault((str(item["producer_id"]), str(requirement)), []).append(pair)

    missing: list[tuple[str, str]] = []
    for (_producer, requirement), pairs in groups.items():
        if requirement == REQUIREMENT_ONE_OF:
            if not any(pair in observed for pair in pairs):
                missing.extend(sorted(pairs))
        elif requirement == REQUIREMENT_NOT_OBSERVED_ALLOWED:
            continue
        else:
            missing.extend(sorted(pair for pair in pairs if pair not in observed))
    return sorted(set(missing))


def authorized_unobserved_pairs(
    manifest: dict[str, Any], observed: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """`NOT_OBSERVED_ALLOWED` 且本次**未观测到**的逐对清单（§7.3:896 授权豁免）。

    这些对不使驱动失败，但必须被具名报告：报告里它们既不是 violation，也不是 covered。
    """
    allowed = {
        (str(item["producer_id"]), str(item["mapping_stage"]))
        for item in manifest["required_producer_stages"]
        if item.get("evidence_requirement") == REQUIREMENT_NOT_OBSERVED_ALLOWED
    }
    return sorted(allowed - observed)


def closure_violations(
    *,
    manifest: dict[str, Any],
    run_id: str,
    records: Sequence[Any],
    summary: dict[str, Any],
    events_dir: Path,
    business: BusinessRun,
) -> list[str]:
    """业务完成 ≠ 证据闭合（§13.3）：这里给出 gate 同款的闭合失败清单。"""
    from adapters.foundation.evidence_sink import has_residual_temp_files

    violations: list[str] = []
    if business.timed_out:
        violations.append(f"业务路径超时（timeout_seconds={manifest['timeout_seconds']}）")
    elif business.returncode not in (0, None):
        violations.append(f"业务路径退出码非 0：{business.returncode}")
    elif business.returncode is None:
        violations.append("业务路径未能启动")
    if not events_dir.is_dir():
        violations.append(f"事件目录不存在：{events_dir}")
    if has_residual_temp_files(events_dir):
        violations.append("§11.1：事件目录存在残留 .tmp")
    if not records:
        violations.append("§11.2：没有任何已发布事件（目录非空不构成 Smoke 证据）")
    if summary["sink_failure_count"] != 0:
        violations.append(f"EVD-SINK-003：sink_failure_count={summary['sink_failure_count']} != 0")
    if summary["rejected_records"]:
        violations.append(f"§11.2：存在被拒绝的 EvidenceRecord：{summary['rejected_records'][:10]}")
    if summary["duration_seconds"] > manifest["duration_cap_seconds"]:
        violations.append(
            f"§13.3：duration {summary['duration_seconds']}s 超过预算 "
            f"{manifest['duration_cap_seconds']}s"
        )
    for pair in unobserved_pairs(manifest, _observed_pairs(records)):
        violations.append(f"§13.3：required producer/stage 未观测到：{pair}")
    for record in records:
        if str(record.run_id) != run_id:
            violations.append(f"EVD-RUN-001：事件 {record.event_id} 的 run_id 与 manifest 不一致")
        if str(record.contract_mode) != OBSERVE_MODE:
            violations.append(f"§11.2：事件 {record.event_id} 的 mode 非 observe")
    return violations


def _observed_pairs(records: Sequence[Any]) -> set[tuple[str, str]]:
    return {(str(record.producer_id), str(record.mapping_stage)) for record in records}


# --------------------------------------------------------------------------- #
# 顶层流程
# --------------------------------------------------------------------------- #


@dataclass
class SmokeRun:
    """一次 L3 驱动的完整记录（退出码由 ``violations`` 决定）。"""

    run_id: str
    manifest_path: Path
    evidence_root: Path
    run_dir: Path
    events_dir: Path
    scratch_run_dir: Path
    case_ids: list[str]
    staged_cases: Path
    commit: str
    business: BusinessRun
    summary: dict[str, Any]
    summary_path: Path | None
    violations: list[str] = field(default_factory=list)
    #: `NOT_OBSERVED_ALLOWED` 且未观测到的对（§7.3:896 授权豁免；既不是 violation，
    #: 也不是 covered —— 报告必须点名，绝不呈现为通过）。
    authorized_unobserved: list[tuple[str, str]] = field(default_factory=list)
    fixture_seam: bool = False

    @property
    def exit_code(self) -> int:
        return EXIT_OK if not self.violations else EXIT_VALIDATION_FAILED

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "manifest": str(self.manifest_path),
            "repository_commit": self.commit,
            "evidence_root": str(self.evidence_root),
            "run_dir": str(self.run_dir),
            "case_ids": list(self.case_ids),
            "staged_case_root": str(self.staged_cases),
            "business_command": list(self.business.command),
            "business_returncode": self.business.returncode,
            "business_timed_out": self.business.timed_out,
            "fixture_seam": self.fixture_seam,
            "duration_seconds": self.summary["duration_seconds"],
            "summary_path": str(self.summary_path) if self.summary_path else None,
            "producer_coverage": self.summary["producer_coverage"],
            "actual_calls": self.summary["actual_calls"],
            "actual_tokens": self.summary["actual_tokens"],
            "sink_failure_count": self.summary["sink_failure_count"],
            "rejected_records": self.summary["rejected_records"],
            "authorized_unobserved": [[producer, stage] for producer, stage in self.authorized_unobserved],
            "violations": list(self.violations),
            "exit_code": self.exit_code,
        }


def run_l3_smoke(
    manifest_path: Path,
    case_source: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> SmokeRun:
    """执行一次 L3 fresh smoke：选择 case → 驱动业务 → 读回证据 → 写 run-summary。

    用法/配置问题抛 :class:`ToolError`（退出码 2）；业务或证据不闭合只累积到
    ``SmokeRun.violations``（退出码 1），run-summary 仍然写出，便于诊断。
    """
    environment = dict(os.environ if env is None else env)
    manifest = load_run_manifest(manifest_path)
    validate_run_manifest(manifest)
    run_id = resolve_run_id(manifest, environment)
    commit = resolve_run_commit(manifest, environment)

    evidence_root = resolve_evidence_root(manifest)
    run_dir = evidence_root / run_id
    events_dir = run_dir / EVENTS_DIRNAME
    scratch_root = resolve_scratch_root(environment)
    scratch_run_dir = scratch_root / run_id

    source = resolve_case_source(case_source)
    selected = select_cases(source, manifest["case_ids"])
    staged_cases = materialize_cases(selected, scratch_run_dir / CASES_DIRNAME)
    command, fixture_seam = resolve_business_command(staged_cases, scratch_run_dir, environment)

    child_env = dict(environment)
    child_env[MODE_ENV] = OBSERVE_MODE
    child_env[RUN_ID_ENV] = run_id
    child_env[COMMIT_ENV] = commit
    child_env[EVIDENCE_DIR_ENV] = str(evidence_root)

    business = run_business_path(command, child_env, float(manifest["timeout_seconds"]))

    records, violations = read_events(events_dir)
    failures = read_sink_failures(run_dir)
    summary = summarize_evidence(records, business.duration_seconds)
    summary["sink_failure_count"] = len(failures)
    summary["rejected_records"] = sorted(
        {str(entry.get("event_id", "")) for entry in failures if entry.get("event_id")}
    )
    summary_path = write_run_summary(run_dir, summary)
    violations.extend(
        closure_violations(
            manifest=manifest,
            run_id=run_id,
            records=records,
            summary=summary,
            events_dir=events_dir,
            business=business,
        )
    )
    violations = list(dict.fromkeys(violations))
    authorized_unobserved = authorized_unobserved_pairs(manifest, _observed_pairs(records))
    return SmokeRun(
        run_id=run_id,
        manifest_path=manifest_path,
        evidence_root=evidence_root,
        run_dir=run_dir,
        events_dir=events_dir,
        scratch_run_dir=scratch_run_dir,
        case_ids=list(manifest["case_ids"]),
        staged_cases=staged_cases,
        commit=commit,
        business=business,
        summary=summary,
        summary_path=summary_path,
        violations=violations,
        authorized_unobserved=authorized_unobserved,
        fixture_seam=fixture_seam,
    )


def _tail(text: str, limit: int = 20) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def render_human(run: SmokeRun) -> str:
    """人读报告（stdout）；失败明细同份写 stderr 的 greppable marker 行。"""
    lines = [
        f"=== L3 fresh smoke / run_id={run.run_id} ===",
        f"manifest        : {run.manifest_path}",
        f"commit          : {run.commit}",
        f"case_ids        : {', '.join(run.case_ids)}（源已解析 → {run.staged_cases}）",
        f"business        : {' '.join(run.business.command)}",
        (
            f"business result : "
            + ("TIMEOUT" if run.business.timed_out else f"exit={run.business.returncode}")
            + f"，duration={run.business.duration_seconds:.3f}s"
        ),
        f"events          : {len(run.summary['producer_coverage'])} 个 (producer, stage) 对",
        f"run-summary     : {run.summary_path}",
        f"sink_failure_count={run.summary['sink_failure_count']} "
        f"rejected_records={run.summary['rejected_records']}",
        f"actual_calls={run.summary['actual_calls']} "
        f"actual_tokens={run.summary['actual_tokens']} "
        f"cost known/unknown={run.summary['known_cost_components']}/"
        f"{run.summary['unknown_cost_components']}",
    ]
    if run.fixture_seam:
        lines.append(
            f"WARNING: {BUSINESS_COMMAND_ENV} 生效 —— 这是 fixture seam，不是真实业务路径"
        )
    if run.authorized_unobserved:
        lines.append(
            f"未观测（§7.3:896 授权豁免，NOT_OBSERVED_ALLOWED；**不是**通过，不计入覆盖）"
            f" {len(run.authorized_unobserved)} 项："
        )
        lines.extend(
            f"  - {producer_id}/{stage}" for producer_id, stage in run.authorized_unobserved
        )
    if run.violations:
        lines.append(f"闭合失败 {len(run.violations)} 项：")
        lines.extend(f"  - {item}" for item in run.violations)
    else:
        lines.append("闭合：覆盖 / sink / 拒绝记录 / 时长全部满足预登记")
    if run.business.stdout:
        lines.append("--- business stdout tail ---")
        lines.extend(_tail(run.business.stdout))
    if run.business.stderr:
        lines.append("--- business stderr tail ---")
        lines.extend(_tail(run.business.stderr))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：绝不打印 traceback；失败只给 greppable marker。"""
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="Spec 13 L3 Fresh Smoke 受控驱动（Coding）",
    )
    parser.add_argument(
        "--run-manifest",
        type=Path,
        required=True,
        help="预登记 run manifest（如 config/contracts/smoke-v0.1.json）",
    )
    parser.add_argument(
        "--case-source",
        default=None,
        help=f"benchmark case 源目录（缺省仓库内 {DEFAULT_CASE_SOURCE}）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    try:
        run = run_l3_smoke(args.run_manifest, args.case_source)
    except ToolError as exc:
        print(f"{exc.marker}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 — 内部缺陷也不得抛裸 traceback
        print(
            f"{MARKER_STATUS_CONFLICT}: 内部错误 {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_FAILED

    if args.json:
        print(json.dumps(run.to_json(), ensure_ascii=False, indent=2))
    else:
        print(render_human(run))
    if run.violations:
        print(
            f"{MARKER_STATUS_CONFLICT}: L3 证据未闭合（{len(run.violations)} 项）："
            + "；".join(run.violations),
            file=sys.stderr,
        )
    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
