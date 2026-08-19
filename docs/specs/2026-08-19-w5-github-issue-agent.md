# W5 GitHub Issue Agent 设计规格

> 日期：2026-08-19
> 状态：草稿（待用户批准后进入 writing-plans）
> 关联：`docs/roadmap.md` W5 窗口、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 5 阶段
> 说明：本规格为窗口准备产物，起草于 main 分支；正式开发时在 worktree `feature/w5-github-issue` 中执行

## 1. 背景与目标

W1-W3 已具备：文件工具 + shell/test + git 只读三件套 + Docker 沙箱（一个任务一个沙箱，含创建期 clone）+ LangGraph 显式编排（plan → decide ⇄ execute → verify → reflect → finalize）。

W5 是项目的**最终 MVP**：补齐 GitHub API 与 git 写操作，打通全链路：

```text
Get Issue → Clone Repo → Create Branch → Agent Work → Run Tests
→ Git Diff → Review → Commit → Push → Create PR
```

输入：仓库 URL + Issue 编号；输出：可审查的 PR（或人工确认的模拟 PR）。

## 2. 验收标准（源自 roadmap）

1. 输入仓库 URL + Issue 编号，Agent 完成全部步骤；
2. 产出可审查的 PR（真实仓库）或 dry-run 停在 diff/commit 阶段（模拟 PR，人工确认）。

## 3. 设计决策（brainstorming 已确认）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | GitHub API 访问 | **`gh` CLI**（`gh api` / `gh pr create` 等，JSON 输出解析），不引入 PyGithub | 零新 Python 依赖；`gh` 统一处理认证（已登录或 GH_TOKEN）；与 AGENTS.md「GitHub 相关任务一律用 gh 命令」一致 |
| D2 | 凭据边界 | **GitHub/git 写操作（clone/branch/commit/push/PR）全部在宿主执行**；沙箱容器内保持只读 git 三件套 | 延续 W3 秘密隔离决策：凭据不进容器 |
| D3 | 代码修改执行 | 复用 W3 沙箱：代码修改与测试在容器内（git 状态经挂载同步可见） | 一个任务一个沙箱机制已就绪；W3 遗留（超时 124/OOM 判别）不受影响 |
| D4 | 全链路编排形态 | **复用 LangGraph**：前置阶段（fetch issue → clone → branch）在图外顺序执行，decide/execute/verify/reflect 循环复用，后置阶段（diff → review → commit → push → PR）图外执行 | 核心循环已验证；编排阶段无 LLM 决策，不需要图节点 |
| D5 | 安全阀 | `--dry-run` 停在 push 前（产出 diff + commit + 审查结论，不推远端）；默认 dry-run=True，`--push` 才真推 | W8 之前以人工确认为准（需求文档第 14 节：PR 属高风险写操作） |
| D6 | 审查 | 新增轻量 **Review 步骤**：LLM 对 diff 做中文总结 + 通过/需修改结论（不引入独立 Reviewer Agent，留 W8） | 满足链路中的 Review 环节且不越窗 |

## 4. 架构与组件

```
tools/github_tools.py     # 新增：gh CLI 封装（get_issue / get_repository / create_branch / commit / push / create_pull_request / comment_issue）
tools/shell_tools.py      # 修改：git 只读三件套不变；宿主 git 写操作经 github_tools 直调（不进容器）
agent/issue.py            # 新增：IssueAgent 全链路编排（run_issue_agent）
agent/graph.py            # 不变：decide/execute/verify/reflect 循环复用
main.py                   # 修改：--issue 模式（repo URL + issue 号）与 --push/--dry-run
```

### 4.1 GitHub 工具（seam 1：github_tools）

```python
@dataclass
class GitHubIssue:
    number: int
    title: str
    body: str
    labels: list[str]
    state: str

@dataclass
class GitHubRepo:
    full_name: str
    clone_url: str
    default_branch: str
    language: str | None

def get_issue(repo: str, number: int) -> GitHubIssue: ...          # gh api repos/{repo}/issues/{n}
def get_repository(repo: str) -> GitHubRepo: ...                    # gh api repos/{repo}
def create_branch(repo_dir: str, branch: str, base: str) -> None: ...  # 宿主 git checkout -b
def commit_changes(repo_dir: str, message: str) -> None: ...        # 宿主 git add -A + commit
def push_branch(repo_dir: str, branch: str) -> None: ...            # 宿主 git push -u origin
def create_pull_request(repo: str, head: str, base: str, title: str, body: str) -> str: ...  # gh pr create → PR URL
def comment_issue(repo: str, number: int, body: str) -> None: ...   # gh api POST（演示辅助，可选）
```

- 全部以 subprocess 调 `gh`（宿主），`--json` 输出解析；`gh` 未安装/未认证 → `ToolError`
- git 写操作（branch/commit/push）不经过 `run_command`（避免进容器），在 github_tools 内部以宿主 subprocess 直调

### 4.2 编排（seam 2：agent/issue.py）

```python
@dataclass
class IssueTask:
    repository: str          # owner/name
    issue_number: int
    workspace_root: str
    push: bool = False       # True 才 push + 建 PR；False 停在 commit（dry-run）
    max_iterations: int = 20

@dataclass
class IssueAgentResult:
    issue: GitHubIssue
    steps: list[AgentStep]
    final_answer: str
    branch: str
    diff: str
    review: str
    pr_url: str | None
    stopped_by_limit: bool

def run_issue_agent(task: IssueTask, llm: LLMClient) -> IssueAgentResult: ...
```

流程：

1. `get_issue` / `get_repository` → 生成任务描述（issue 标题 + body）
2. 宿主 `gh repo clone <repo> <workspace_root>/<repo>`（已存在则 fetch + reset）
3. 宿主 `create_branch`：`fix/issue-<number>`（自 `default_branch`）
4. `run_agent_graph` 在沙箱内执行（任务描述 = issue 内容；工具集复用 W4 之后的完整集）
5. 宿主 `git diff default_branch...HEAD`（或工作区 diff）→ 汇总变更
6. Review：LLM 读 diff 输出中文审查结论（通过/需修改 + 要点）；失败时把审查意见回注为下一轮任务描述重新执行（上限 1 次）
7. `commit_changes`（Conventional Commits：`fix: 修复 #<n> <标题>`，dry-run 时仅展示 diff 不提交）
8. `push` 时：`push_branch` + `create_pull_request`（body 含测试结果与审查结论）

### 4.3 输入与状态

- 复用现有 `AgentState` 与 `run_agent_graph`（不新增图节点）；阶段间数据（issue / branch / diff）由 `IssueTask` + 结果 dataclass 携带
- 沙箱挂载：workspace_root 为 `<workspace_root>/<repo>`，创建期容器自动安装 requirements（W3 已有）

## 5. 测试策略（TDD，seam = github_tools 函数 + run_issue_agent）

1. **github_tools 单元测试**：FakeGitHub（monkeypatch subprocess / 假 gh 脚本）——解析 gh JSON、错误路径（gh 缺失、未认证、404）
2. **git 写操作测试**：本地临时仓库（git init）验证 create_branch / commit / diff 行为（不触网）
3. **编排测试**（Mock LLM + 本地 mock 仓库）：
   - 全链路 dry-run：fetch → clone（本地路径模拟）→ branch → work（Mock 修改文件）→ diff → review → 停在 commit
   - push 模式：Fake gh 断言 push/pr create 调用参数（不真推远端）
4. **真实演示**（验收）：真实仓库 + 真实 Issue + `--push` 到测试仓库（或 `--dry-run` 展示 diff + commit + 审查），双模式各一次

## 6. 配置

| env | 默认 | 说明 |
|-----|------|------|
| `GH_TOKEN` | 无（用 gh 已登录态） | 认证令牌；不写代码、不入库 |
| `KA_ISSUE_PUSH` | `0` | 默认 dry-run；=1 时允许 push + PR |
| `KA_GITHUB_BRANCH_PREFIX` | `fix/issue-` | 分支前缀 |

## 7. 错误处理与返回语义

| 场景 | 返回 |
|------|------|
| `gh` 未安装 / 未认证 | `ToolError`（提示 gh auth login 或设置 GH_TOKEN） |
| Issue/仓库不存在或无权访问 | `ToolError`（gh 错误原文） |
| clone 失败（网络/权限） | `ToolError`，清理已创建的半成品目录 |
| 沙箱内测试失败且反复修复不通过（verify 轮上限） | 流程继续至 diff/commit，Review 阶段标注「测试未全绿」，PR body 明示 |
| push 失败 | 停在本地 commit，返回可手工 push 的命令 |
| dry-run 模式 | 产出 diff + commit 内容展示 + 审查结论，不产生远端任何副作用 |

## 8. 非目标（YAGNI，第一版不做）

- 不做独立 Reviewer Agent 与人工审批门禁（W8）
- 不做 Issue 评论回复与多轮对话
- 不做 PR 创建后的更新迭代（reviewer 评论修复循环留 W8）
- 不做多个 Issue 并行 / 队列
- 不做复杂 Code Knowledge Graph（W4 简单版即可，需求文档第 14 节）

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| gh CLI 未安装/未认证 | 前置检查（github_tools 初始化时探测），明确报错 |
| 真实仓库沙箱内依赖安装失败 | 复用 W3 创建期 pip 安装；失败时测试结果如实上报，Review 标注 |
| 公网 clone 慢/失败 | 复用 W3 网络经验（创建期有网）；失败重试 1 次后报错 |
| 沙箱内 git 只读 vs 宿主 git 写操作的状态同步 | 修改发生在挂载目录，宿主 git diff 直接可见；测试在容器内、commit 在宿主 |
| push 覆盖风险 | 分支名含 issue 号 + 默认 dry-run + `--push` 显式开启 |

## 10. 依赖

- 新增 Python 依赖：无（subprocess / json / dataclasses）
- 外部：`gh` CLI（GitHub CLI，需已认证或 GH_TOKEN）
