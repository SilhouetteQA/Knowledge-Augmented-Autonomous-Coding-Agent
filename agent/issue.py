"""GitHub Issue Agent 全链路编排。

Get Issue → Clone → Branch → Work（LangGraph 沙箱）→ Diff → Review（独立 Reviewer）→ 生成审批单并停止；
推送唯一入口为 main.py --approve（人工审批门禁，W8）。
凭据边界：gh/git 写操作在宿主与 --approve 阶段执行（沙箱容器内无凭据）。
"""
import glob
import os
from dataclasses import dataclass

from agent.graph import run_agent_graph
from agent.llm import LLMClient
from agent.loop import AgentStep
from tools.approval import create_approval, save_approval
from tools.file_tools import ToolError
from tools.shell_tools import TestResult
from tools.github_tools import (
    GitHubIssue,
    clone_repository,
    create_branch,
    get_issue,
    get_repository,
    git_diff_since,
    run_host,      # 宿主只读执行器（统一编码/超时/ToolError 语义，A3 复用；公开 API）
    sync_repository,
)
from tools.tracing import traced

# Review 提示词（独立审查角色）：审查输入 = Issue + diff + 验证轮数 + 工作树事实，
# 不见工作过程。工作树事实（_review_context 产物）为 Reviewer 的文本证据面：
# 变更/删除/新增清单与关键路径存在性，防止"只看 diff 文本"产生的缺失幻觉。
REVIEW_PROMPT = (
    "你是独立代码审查者（Reviewer Agent），独立于实施该变更的 Coder，审查以下变更：\n"
    "1. 变更是否解决 Issue 描述的问题；\n"
    "2. 是否有明显错误或遗漏（语法/逻辑/回归风险）；\n"
    "3. 是否混入与 Issue 无关的改动（格式调整、文档顺手修改等无关变更应明确指出，"
    "此类改动不得进入 PR）；\n"
    "4. 测试是否已并入仓库现有测试套件（若发现独立验证脚本而非正式测试，应指出并要求并入）。"
    "注意：位于仓库测试目录（tests/、<包>/tests/ 等）内的新增测试文件会被全量测试自动收集运行，"
    "属于已并入正式套件，不得仅以「未并入」为由判 FAIL（以工作树事实中的测试验证证据为准）。\n"
    "5. 域数据操作（extractions/seed/index 等数据文件）的删除条目须有证据可核：三条件齐备——"
    "无来源锚点（source_records/line_range/原文出现）∧ 无事件参与 ∧ 无结构化引用；"
    "存在元数据/日志等与任务无关的改动（如 _meta.generated_at、cost/日志文件）应明确指出；\n"
    "6. 检查工作树是否存在脚本/中间产物类残留（新增文件清单由工作树事实提供），"
    "若发现 scripts/、output/ 下新增的分析脚本或中间产物文件应指出，视为需要清理的无关改动。\n"
    "输出格式：第一行结论（PASS 或 FAIL），后续为中文要点列表；"
    "需要人工特别关注的事项以「人工关注」开头单独列出。"
)


@dataclass
class IssueTask:
    """Issue 任务输入。"""
    repository: str              # owner/name
    issue_number: int
    workspace_root: str          # 仓库克隆父目录（如 workspace/repos）
    approval_dir: str | None = None   # 非 None：review 后生成审批单并停止（等待人工审批）
    max_iterations: int = 20
    issue_snapshot: GitHubIssue | None = None   # 非 None 时离线模式：跳过 get_issue


@dataclass
class IssueAgentResult:
    """Issue 任务结果。"""
    issue: GitHubIssue
    steps: list[AgentStep]
    final_answer: str
    branch: str
    diff: str
    review: str
    pr_url: str | None            # 恒为 None（推送唯一入口为 --approve，兼容旧字段）
    stopped_by_limit: bool
    iteration_count: int
    verify_rounds: int
    retry_count: int = 0          # FAIL 重试发生次数（0/1）
    approval_path: str | None = None   # approval_dir 非 None 时生成的审批单路径


def repo_dir_name(repository: str) -> str:
    """owner/name → owner__name（目录安全）。"""
    return repository.replace("/", "__")


# 工作树关键路径探针（Reviewer 证据面：路径存在/缺失为正交事实，防只看 diff 的幻觉）
REVIEW_CONTEXT_PROBES = [
    "config/identity_map.json",
    "data/entity_source_map.json",
    "data/*.txt",
]


def _probe_review_context(repo_dir: str) -> list[str]:
    """关键路径存在性探针：具体路径用 os.path.isfile，通配路径按存在文件数标计。"""
    verdicts = []
    for path in REVIEW_CONTEXT_PROBES:
        abs_path = os.path.join(repo_dir, path)
        if "*" in path:
            found = glob.glob(abs_path)
            verdicts.append(f"{path}: 存在（{len(found)} 个文件）" if found
                            else f"{path}: 缺失")
        else:
            verdicts.append(f"{path}: 存在" if os.path.isfile(abs_path)
                            else f"{path}: 缺失")
    return verdicts


def _review_context(repo_dir: str, base_branch: str,
                    extra_facts: list[str] | None = None) -> str:
    """收集工作树事实摘要，供 Reviewer 核对论据（防只看 diff 文本的幻觉）。

    事实 = 变更清单（git status --porcelain）+ 删除文件（git diff --name-status
    相对 base）+ 新增文件（status 未跟踪 ?? 项）+ 关键路径存在性（文件系统探针）
    + 额外事实（extra_facts：调用方追加的审计事实，如红线还原清单；None/空不追加）。
    目录必须自身就是 git 工作树（含 .git，覆盖完整克隆与 linked worktree 的
    .git 文件形式）：缺失时降级——否则 git 会向上回溯命中外围仓库，把外层仓库
    的事实误报成 repo_dir 的事实（正是本函数要防的幻觉来源）。
    全部为宿主只读命令；任一只读命令失败（非 git 仓库/命令错误）→ 降级为简短
    提示而非抛错——审查输入缺失事实时明确告知 Reviewer 优于中断（架构决定的
    降级语义：明确告知是正确行为，与"不写 fallback"不冲突）。
    """
    if not os.path.isdir(repo_dir):
        return "（工作树事实不可用: 仓库目录不存在）"
    if not os.path.exists(os.path.join(repo_dir, ".git")):
        return "（工作树事实不可用: 目录不是 git 工作树）"
    status_out = run_host(["git", "status", "--porcelain"], cwd=repo_dir)
    if isinstance(status_out, ToolError):
        return f"（工作树事实不可用: {status_out.message.splitlines()[0]}）"
    name_status = run_host(["git", "diff", "--name-status", base_branch], cwd=repo_dir)
    if isinstance(name_status, ToolError):
        return f"（工作树事实不可用: {name_status.message.splitlines()[0]}）"

    changed = [line for line in status_out.splitlines() if line.strip()]
    untracked = [line[3:] for line in changed if line.startswith("??")]
    deleted = [line.split("\t")[-1] for line in name_status.splitlines()
               if line.startswith("D")]

    out = []
    if changed:
        out.append(f"变更清单（{len(changed)} 项）:")
        out += [f"  {line}" for line in changed]
    else:
        out.append("变更清单: （无变更）")
    if deleted:
        out.append("删除文件:")
        out += [f"  {line}" for line in deleted]
    else:
        out.append("删除文件: （无）")
    if untracked:
        out.append("新增文件:")
        out += [f"  {line}" for line in untracked]
    else:
        out.append("新增文件: （无）")
    out.append("关键路径存在性:")
    out += [f"  {line}" for line in _probe_review_context(repo_dir)]
    if extra_facts:
        out.append("额外事实:")
        out += [f"  {line}" for line in extra_facts]
    return "\n".join(out)


# 红线路径模式（重测 #2 暴露：A2 提示词约束不够硬，需执行层拦截）：
# 命中模式的文件被 _enforce_red_lines 从工作树还原，不得进入 diff/审查/审批单。
# cost_log：output/eval/cost_log.jsonl 类运行成本日志（运行兄弟项目脚本的副作用）；
# generated_at：v3_seed 等数据文件 _meta.generated_at 行的误改。
RED_LINE_PATTERNS = ("cost_log", "generated_at")


def _red_line_patterns() -> tuple[str, ...]:
    """红线模式集：默认常量；KA_REDLINE_PATTERNS（逗号分隔子串）覆盖，空/未设 → 默认。"""
    raw = os.environ.get("KA_REDLINE_PATTERNS", "").strip()
    if not raw:
        return RED_LINE_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _enforce_red_lines(repo_dir: str, base_branch: str) -> list[str]:
    """执行层红线拦截：还原工作树中命中红线模式的文件，返回被还原文件清单。

    以 `git diff --name-only <base>` 取变更文件；命中判定为双通道——
    文件路径含模式（cost_log 类），或该文件相对 base 的 diff 新增行含模式
    （generated_at 类：v3_seed_db_v2.json 路径不含模式，靠 _meta.generated_at
    改动行命中）。还原用 `git checkout -- <path>`（宿主执行，cwd=repo_dir）；
    run 内从不暂存，还原只影响工作树（index 与 base 一致）。
    git 命令失败 → 冒泡 ToolError：拦截是门禁不是证据，执行层拦截失败必须
    显式报错、不得静默放行（与 _review_context 的「审查信息降级」语义相反，
    后者缺事实时降级告知，前者放行 = 红线改动进入 PR）。
    """
    name_only = run_host(["git", "diff", "--name-only", base_branch], cwd=repo_dir)
    if isinstance(name_only, ToolError):
        raise ToolError(f"红线检查失败（读取变更清单）: {name_only.message}")
    patterns = _red_line_patterns()
    reverted: list[str] = []
    changed = [p.strip() for p in name_only.splitlines() if p.strip()]
    # untracked 纳入红线（Arch-C2）：`git diff` 不含未跟踪文件，Agent 新建的
    # cost_log/output 类文件此前会静默绕过门禁、进 diff 盲区、随 approve 被提交
    others = run_host(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo_dir)
    if isinstance(others, ToolError):
        raise ToolError(f"红线检查失败（读取未跟踪清单）: {others.message}")
    untracked = [p.strip() for p in others.splitlines() if p.strip()]
    for path in changed:
        if not _is_red_line_path(repo_dir, base_branch, path, patterns):
            continue
        reset = run_host(["git", "checkout", "--", path], cwd=repo_dir)
        if isinstance(reset, ToolError):
            raise ToolError(f"红线还原失败（{path}）: {reset.message}")
        reverted.append(path)
    for path in untracked:
        if not any(p in path for p in patterns):
            continue
        try:
            os.remove(os.path.join(repo_dir, path))
        except OSError as e:
            raise ToolError(f"红线清除失败（未跟踪 {path}）: {e}") from e
        reverted.append(path)
    return reverted


def _is_red_line_path(repo_dir: str, base_branch: str, path: str,
                      patterns: tuple[str, ...]) -> bool:
    """红线命中判定：路径含模式，或该文件 diff 新增行含模式（内容级通道）。"""
    if any(p in path for p in patterns):
        return True
    frag = run_host(["git", "diff", base_branch, "--", path], cwd=repo_dir)
    if isinstance(frag, ToolError):
        raise ToolError(f"红线检查失败（读取 {path} 的 diff）: {frag.message}")
    return any(line.startswith("+") and not line.startswith("+++")
               and any(p in line for p in patterns)
               for line in frag.splitlines())


def _red_line_facts(reverts: list[str]) -> list[str]:
    """红线还原事实行（追加进 _review_context，Reviewer 可见）；空清单 → 无事实。"""
    if not reverts:
        return []
    return [f"红线还原（{len(reverts)} 个文件）: {', '.join(reverts)}"]


def _test_evidence_facts(test_results: list) -> list[str]:
    """测试验证事实（Reviewer 判「测试已并入套件」的证据）：取最后一次全量 verify 结果。

    total>0：报通过/失败明细；total=0 且 error>0（收集/执行失败，dateutil 实测
    环境依赖遮蔽仓库源码即此形态）：必须报异常——此时 diff 无法被测试验证，
    缺证据会让 Reviewer 误判 PASS。两者皆无（空洞收集）不产出事实。
    """
    if not test_results:
        return []
    tr = test_results[-1]
    if not isinstance(tr, TestResult):
        return []
    if tr.total == 0 and tr.error == 0:
        return []
    if tr.total == 0:
        return [f"测试验证异常（verify 全量运行未收集到测试，error={tr.error}）: "
                "diff 未经过任何测试验证，请人工核验"]
    return [f"测试验证（verify 全量运行）: {tr.passed} passed / {tr.failed} failed / "
            f"{tr.error} error（共 {tr.total} 项）"]


def _review_metadata(args, kwargs, result) -> dict:
    """review span metadata：记录结论首行（PASS/FAIL）与「人工关注」标记。"""
    text = str(result or "").strip()
    first = (text.splitlines() or [""])[0]
    return {
        "first_line": first[:80],
        "has_manual_flag": "人工关注" in text,
    }


@traced("review", as_type="generation", metadata_fn=_review_metadata)
def _review_diff(llm: LLMClient, diff: str, issue_text: str,
                 verify_rounds: int, context: str | None = None,
                 final_answer: str = "") -> str:
    """独立 Reviewer 审查 diff，返回结论文本（首行 PASS/FAIL + 中文要点）。

    context：工作树事实摘要（_review_context 产物），非 None 时插入
    Issue/验证轮数之后、Diff 之前（Reviewer 的文本证据面）。
    final_answer：Agent 本轮最终结论（图 run 产物）。空 diff 分支（A7）：
    final_answer 无实质内容（strip 后为空）→ 硬短路 FAIL 不调 LLM（防止对
    空变更空转审查）；final_answer 有实质内容 → 转入结论审查模式——空 diff
    可能是合法零变更（无修改即正确行为），此时审查 Agent 的核验结论是否
    合理可信（证据充分性/是否满足任务验收），供人工审批参考。
    """
    context_section = f"\n\n工作树事实:\n{context}" if context else ""
    if not diff.strip():
        if not final_answer.strip():
            return "FAIL 无代码变更"
        user_content = (f"Issue:\n{issue_text}\n\n验证轮数: {verify_rounds}"
                        f"{context_section}\n\n无代码变更。Agent 最终结论:\n"
                        f"{final_answer}\n\n请审查该结论是否合理可信"
                        "（核验证据充分性/是否满足任务验收），输出 PASS/FAIL + 要点\n"
                        "（无代码变更时的结论审查模式）")
    else:
        user_content = (f"Issue:\n{issue_text}\n\n验证轮数: {verify_rounds}"
                        f"{context_section}\n\nDiff:\n{diff}")
    msg = llm.chat(
        [{"role": "system", "content": REVIEW_PROMPT},
         {"role": "user", "content": user_content}],
        [],
    )
    return (msg.content or "FAIL 审查无输出").strip()


def _issue_run_metadata(args, kwargs, result) -> dict:
    """issue.run span metadata：记录验证轮数与重试次数。"""
    return {
        "verify_rounds": getattr(result, "verify_rounds", 0),
        "retry_count": getattr(result, "retry_count", 0),
    }


@traced("issue.run", as_type="agent", metadata_fn=_issue_run_metadata)
def run_issue_agent(task: IssueTask, llm: LLMClient,
                    code_graph=None, knowledge_client=None) -> IssueAgentResult:
    """执行 Issue 全链路；review 后生成审批单并停止（approval_dir 非 None 时）。"""
    if task.issue_snapshot is not None:
        issue = task.issue_snapshot
    else:
        issue = get_issue(task.repository, task.issue_number)
        if isinstance(issue, ToolError):
            raise ToolError(f"读取 Issue 失败: {issue.message}")
    repo_info = get_repository(task.repository)
    if isinstance(repo_info, ToolError):
        raise ToolError(f"读取仓库失败: {repo_info.message}")

    repo_dir = os.path.join(task.workspace_root, repo_dir_name(task.repository))
    os.makedirs(task.workspace_root, exist_ok=True)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        err = clone_repository(repo_dir, task.repository)
        if err is not None:
            raise ToolError(f"克隆失败: {err.message}")
    else:
        err = sync_repository(repo_dir, repo_info.default_branch)
        if err is not None:
            raise ToolError(f"同步仓库失败: {err.message}")

    branch = f"{os.environ.get('KA_GITHUB_BRANCH_PREFIX', 'fix/issue-')}{task.issue_number}"
    err = create_branch(repo_dir, branch, repo_info.default_branch)
    if err is not None:
        raise ToolError(f"创建分支失败: {err.message}")

    prompt = (f"仓库: {repo_info.full_name}\n"
              f"Issue #{issue.number}: {issue.title}\n\n{issue.body}")
    result = run_agent_graph(prompt, llm, max_iterations=task.max_iterations,
                             workspace_root=repo_dir, code_graph=code_graph,
                             knowledge_client=knowledge_client)

    # A4 红线拦截（执行层门禁）：每次图运行后、diff 计算前调用——红线改动
    # （cost_log/generated_at 类）从工作树还原，永不进入 diff/审查/审批单；
    # 拦截失败冒泡 ToolError（门禁不是证据，失败必须显式报错）。重试轮的图
    # 运行可能再次产生红线改动，故两轮各自拦截；还原清单跨轮合并去重
    # （审批单审计全部被还原的红线文件）。
    red_line_reverts: list[str] = []
    round_reverts = _enforce_red_lines(repo_dir, repo_info.default_branch)
    red_line_reverts += [p for p in round_reverts if p not in red_line_reverts]
    diff = git_diff_since(repo_dir, repo_info.default_branch)
    if isinstance(diff, ToolError):
        raise ToolError(f"读取 diff 失败: {diff.message}")
    review = _review_diff(llm, diff, prompt, result.verify_rounds,
                          _review_context(repo_dir, repo_info.default_branch,
                                          extra_facts=_red_line_facts(round_reverts)
                                          + _test_evidence_facts(result.test_results)),
                          final_answer=result.final_answer)

    # Review 未通过：以审查意见为任务重跑一轮（上限 1 次）。
    # retried 在首轮判定时捕获（review 后续会被重试后的结论覆盖），
    # 保证 "FAIL→后一轮 PASS" 场景下 retry_count 仍正确记为 1。
    retried = review.startswith("FAIL")
    if retried:
        retry_prompt = f"{prompt}\n\n审查意见（请修复后重新工作）:\n{review}"
        result = run_agent_graph(retry_prompt, llm, max_iterations=task.max_iterations,
                                 workspace_root=repo_dir, code_graph=code_graph,
                                 knowledge_client=knowledge_client)
        round_reverts = _enforce_red_lines(repo_dir, repo_info.default_branch)
        red_line_reverts += [p for p in round_reverts if p not in red_line_reverts]
        diff = git_diff_since(repo_dir, repo_info.default_branch)
        if isinstance(diff, ToolError):
            raise ToolError(f"读取 diff 失败: {diff.message}")
        # 重试轮同样携带工作树事实：diff 已在重试后重新计算，context 也取
        # 重试后的新状态（工作树可能在重试中变化，首轮事实已过时）。
        # 红线还原事实用跨轮合并清单（red_line_reverts）：首轮已还原但重试轮
        # 不再触碰的文件，其红线事件仍须对 Reviewer 可见（审查修复）。
        review = _review_diff(llm, diff, retry_prompt, result.verify_rounds,
                              _review_context(repo_dir, repo_info.default_branch,
                                              extra_facts=_red_line_facts(red_line_reverts)
                                              + _test_evidence_facts(result.test_results)),
                              final_answer=result.final_answer)

    approval_path = None
    if task.approval_dir:
        approval = create_approval(
            action_type="pr_push", repository=task.repository,
            issue_number=issue.number, branch=branch,
            base_branch=repo_info.default_branch,
            commit_message=f"fix: 修复 #{issue.number} {issue.title}",
            diff=diff, review=review,
            verify_rounds=result.verify_rounds, retry_count=1 if retried else 0,
            red_line_reverts=red_line_reverts,
            final_answer=result.final_answer)
        approval_path = save_approval(approval, task.approval_dir)

    return IssueAgentResult(
        issue=issue, steps=result.steps, final_answer=result.final_answer,
        branch=branch, diff=diff, review=review, pr_url=None,
        stopped_by_limit=result.stopped_by_limit,
        iteration_count=result.iteration_count,
        verify_rounds=result.verify_rounds,
        retry_count=1 if retried else 0,
        approval_path=approval_path,
    )