"""基准案例加载与 schema 校验。

案例目录结构：
    cases/<category>/<id>.json   （category ∈ bug/feature/test/refactor/domain）
    cases/gold/<id>.diff         （社区已合并 PR 的 diff，gold patch）

T3 可选锁定：frozen/<version>/cases.lock.json（或 KA_BENCHMARK_CASES_LOCK 指定的
路径）以 {case_id: repository_commit} 钉住被测仓库提交。文件不存在即视为未锁定
（完全向后兼容）；存在时锁定值优先于 case 内联的 repository_commit。
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from tools.github_tools import GitHubIssue

CATEGORIES = ("bug", "feature", "test", "refactor", "domain")

# T3：可选锁定文件（不要求存在）。路径优先级：load_cases 显式参数 >
# KA_BENCHMARK_CASES_LOCK > <cases_dir 同级>/frozen/<version>/cases.lock.json
LOCK_ENV_VAR = "KA_BENCHMARK_CASES_LOCK"
FROZEN_VERSION_ENV_VAR = "KA_BENCHMARK_FROZEN_VERSION"
FROZEN_VERSION_DEFAULT = "v1"

# E7（P2-7）：case 未显式配 max_iterations 时按 task_type 补默认——开放型任务
# （test/refactor/domain）迭代上限更紧（20，逼小步收敛），bug/feature 保持 30。
DEFAULT_MAX_ITERATIONS = {"bug": 30, "feature": 30,
                          "test": 20, "refactor": 20, "domain": 20}

class CaseError(Exception):
    """案例 schema 非法时抛出（含文件名与原因）。"""


@dataclass
class BenchmarkCase:
    """单个基准任务。"""
    id: str
    category: str
    repository: str
    issue: GitHubIssue
    gold_patch: str
    must_pass: list[str]
    max_iterations: int = 30
    task_type: str = ""
    notes: str = ""
    setup_commands: list[str] = field(default_factory=list)
    # E10：交付一致性核查配置——期望动作（如 {"deletions": 5}），缺省空 dict 不核查
    expected_actions: dict = field(default_factory=dict)
    # T3：被测仓库钉住的提交（case 内联 repository_commit 或锁定文件给出）；
    # 缺省空串 = 未钉住（沿用仓库默认分支 HEAD，与既有行为一致）
    repository_commit: str = ""


def _validate(data: dict, path: Path,
              lock: dict[str, str] | None = None) -> BenchmarkCase:
    case_id = data.get("id")
    category = data.get("category")
    if not isinstance(case_id, str) or not case_id:
        raise CaseError(f"{path.name}: id 缺失或非字符串")
    if category not in CATEGORIES:
        raise CaseError(f"{path.name}: category '{category}' 非法（应为 {CATEGORIES}）")
    # E7（P2-7）：task_type 可选，与 category 同域（bug/feature/test/refactor/domain），
    # 缺省取 category；非法取值（含非字符串）报错并列出允许集。
    task_type = data.get("task_type", category)
    if task_type not in CATEGORIES:
        raise CaseError(f"{path.name}: task_type '{task_type}' 非法（应为 {CATEGORIES}）")
    repo = data.get("repository")
    if not isinstance(repo, str) or "/" not in repo:
        raise CaseError(f"{path.name}: repository 缺失或非法（应为 owner/name）")
    issue = data.get("issue")
    # issue 字段逐键类型校验（IMP-1）：labels 为字符串时若只做粗粒度
    # isinstance 放行，后续 list("open") 会静默拆成单字符列表；逐键精确
    # 校验并在任一不符时抛 CaseError（含文件名与原因），不抛裸 TypeError。
    if not isinstance(issue, dict):
        raise CaseError(f"{path.name}: issue 缺失或非对象")
    if any(k not in issue for k in ("number", "title", "body", "labels", "state")):
        raise CaseError(f"{path.name}: issue 字段缺失（须含 number/title/body/labels/state）")
    number, title, body, labels, state = (issue["number"], issue["title"],
                                          issue["body"], issue["labels"],
                                          issue["state"])
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise CaseError(f"{path.name}: issue.number 须为正整数（got {number!r}）")
    for key in ("title", "body", "state"):
        if not isinstance(issue[key], str):
            raise CaseError(f"{path.name}: issue.{key} 须为字符串（got {issue[key]!r}）")
    if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
        raise CaseError(f"{path.name}: issue.labels 须为字符串列表（got {labels!r}）")
    gold = data.get("gold_patch")
    gold_full = path.parent.parent / gold if isinstance(gold, str) and gold else None
    # BM-2：domain 类案例（知识服务侧规则判定，无 gold 可比）不要求 gold 文件存在；
    # 非域 case 维持必须存在。
    if category != "domain" and (gold_full is None or not gold_full.is_file()):
        raise CaseError(f"{path.name}: gold_patch '{gold}' 文件不存在")
    must_pass = data.get("must_pass")
    if not isinstance(must_pass, list) or not must_pass or not all(
            isinstance(m, str) for m in must_pass):
        raise CaseError(f"{path.name}: must_pass 须为非空字符串列表")
    # E7（P2-7）：显式 max_iterations 永远优先（存在即用，显式 null 视同非法
    # 走正整数校验）；未配置时按 task_type 补默认（bug/feature=30，开放型=20）。
    if "max_iterations" in data:
        max_iter = data["max_iterations"]
    else:
        max_iter = DEFAULT_MAX_ITERATIONS[task_type]
    if not isinstance(max_iter, int) or max_iter <= 0:
        raise CaseError(f"{path.name}: max_iterations 须为正整数")
    setup = data.get("setup_commands", [])
    if not isinstance(setup, list):
        raise CaseError(f"{path.name}: setup_commands 须为列表")
    for i, cmd in enumerate(setup):
        if not isinstance(cmd, str):
            raise CaseError(f"{path.name}: setup_commands[{i}] 须为字符串（got {cmd!r}）")
    # E10：expected_actions 可选 dict（如 {"deletions": 5}），缺省 {}；非 dict →
    # CaseError；键 deletions 存在时值须为非负 int（bool 为 int 子类，一并拒绝）。
    expected_actions = data.get("expected_actions", {})
    if not isinstance(expected_actions, dict):
        raise CaseError(f"{path.name}: expected_actions 须为对象"
                        f"（got {expected_actions!r}）")
    if "deletions" in expected_actions:
        del_n = expected_actions["deletions"]
        if not isinstance(del_n, int) or isinstance(del_n, bool) or del_n < 0:
            raise CaseError(f"{path.name}: expected_actions.deletions 须为非负整数"
                            f"（got {del_n!r}）")
    # T3：repository_commit 可选——锁定文件优先（冻结基线不被 case 内联值覆盖），
    # 其次 case 内联字段；两者皆无 → 空串（不钉住）。非字符串（null 除外）报错。
    inline_commit = data.get("repository_commit", "")
    if inline_commit is None:
        inline_commit = ""
    if not isinstance(inline_commit, str):
        raise CaseError(f"{path.name}: repository_commit 须为字符串"
                        f"（got {inline_commit!r}）")
    pinned_commit = (lock or {}).get(case_id, "") or inline_commit
    return BenchmarkCase(
        id=case_id, category=category, repository=repo,
        issue=GitHubIssue(number=issue["number"], title=issue["title"],
                          body=issue["body"], labels=list(issue["labels"]),
                          state=issue["state"]),
        gold_patch=str(gold) if isinstance(gold, str) else "",
        must_pass=list(must_pass),
        max_iterations=max_iter, task_type=task_type, notes=data.get("notes", ""),
        setup_commands=list(setup),
        expected_actions=dict(expected_actions),
        repository_commit=pinned_commit,
    )


def default_lock_path(cases_dir: str, version: str | None = None) -> str:
    """默认锁定文件路径（T3）：<cases_dir 同级>/frozen/<version>/cases.lock.json。"""
    ver = (version or os.environ.get(FROZEN_VERSION_ENV_VAR, "")
           or FROZEN_VERSION_DEFAULT)
    return str(Path(cases_dir).parent / "frozen" / ver / "cases.lock.json")


def resolve_lock_path(cases_dir: str, lock_path: str | None = None) -> str:
    """锁定文件路径解析（T3）：显式参数 > KA_BENCHMARK_CASES_LOCK > 默认路径。"""
    if lock_path:
        return lock_path
    env_path = (os.environ.get(LOCK_ENV_VAR) or "").strip()
    return env_path or default_lock_path(cases_dir)


def load_lock_file(lock_path: str) -> dict[str, str]:
    """读取锁定文件 → {case_id: repository_commit}；文件不存在 → {}（可选）。

    存在但非法（JSON 无法解析 / 顶层非对象 / 值非非空字符串）→ CaseError：
    锁定文件损坏必须显式失败，不能静默降级为“未锁定”。
    兼容 {"cases": {...}} 包裹形态与裸映射 {"id": "commit"} 两种写法。
    """
    p = Path(lock_path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CaseError(f"锁定文件 {lock_path}: JSON 解析失败: {e}") from e
    if isinstance(data, dict) and isinstance(data.get("cases"), dict):
        data = data["cases"]
    if not isinstance(data, dict):
        raise CaseError(f"锁定文件 {lock_path}: 顶层须为对象（case_id -> commit）")
    lock: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value:
            raise CaseError(
                f"锁定文件 {lock_path}: {key} 的 repository_commit 须为非空字符串"
                f"（got {value!r}）")
        lock[str(key)] = value
    return lock


def load_cases(cases_dir: str, lock_path: str | None = None) -> list[BenchmarkCase]:
    """加载全部案例（五类目录递归扫描）；任一非法 → CaseError。

    lock_path 可选（T3，见模块 docstring）：缺省由 resolve_lock_path 解析；
    锁定文件不存在即未锁定，行为与历史完全一致。
    """
    root = Path(cases_dir)
    if not root.is_dir():
        raise CaseError(f"案例目录不存在: {cases_dir}")
    # T3：可选锁定文件（不存在 → {}，与未锁定行为一致）
    lock = load_lock_file(resolve_lock_path(cases_dir, lock_path))
    cases: list[BenchmarkCase] = []
    for category_dir in sorted(p for p in root.iterdir()
                               if p.is_dir() and p.name in CATEGORIES):
        for json_file in sorted(category_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise CaseError(f"{json_file.name}: JSON 解析失败: {e}") from e
            c = _validate(data, json_file, lock)
            if any(c.id == other.id for other in cases):
                raise CaseError(f"{json_file.name}: id '{c.id}' 重复")
            cases.append(c)
    return sorted(cases, key=lambda c: c.id)