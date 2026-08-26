"""基准案例加载与 schema 校验。

案例目录结构：
    cases/<category>/<id>.json   （category ∈ bug/feature/test/refactor/domain）
    cases/gold/<id>.diff         （社区已合并 PR 的 diff，gold patch）
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from tools.github_tools import GitHubIssue

CATEGORIES = ("bug", "feature", "test", "refactor", "domain")

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


def _validate(data: dict, path: Path) -> BenchmarkCase:
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
    gold_full = path.parent.parent / gold if isinstance(gold, str) else None
    if gold_full is None or not gold_full.is_file():
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
    return BenchmarkCase(
        id=case_id, category=category, repository=repo,
        issue=GitHubIssue(number=issue["number"], title=issue["title"],
                          body=issue["body"], labels=list(issue["labels"]),
                          state=issue["state"]),
        gold_patch=str(gold), must_pass=list(must_pass),
        max_iterations=max_iter, task_type=task_type, notes=data.get("notes", ""),
        setup_commands=list(setup),
    )


def load_cases(cases_dir: str) -> list[BenchmarkCase]:
    """加载全部案例（五类目录递归扫描）；任一非法 → CaseError。"""
    root = Path(cases_dir)
    if not root.is_dir():
        raise CaseError(f"案例目录不存在: {cases_dir}")
    cases: list[BenchmarkCase] = []
    for category_dir in sorted(p for p in root.iterdir()
                               if p.is_dir() and p.name in CATEGORIES):
        for json_file in sorted(category_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise CaseError(f"{json_file.name}: JSON 解析失败: {e}") from e
            c = _validate(data, json_file)
            if any(c.id == other.id for other in cases):
                raise CaseError(f"{json_file.name}: id '{c.id}' 重复")
            cases.append(c)
    return sorted(cases, key=lambda c: c.id)