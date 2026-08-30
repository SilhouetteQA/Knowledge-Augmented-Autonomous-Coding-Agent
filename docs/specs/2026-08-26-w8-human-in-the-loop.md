# W8 Human-in-the-loop 设计规格

> 日期：2026-08-26
> 状态：设计已批准（2026-08-26，brainstorming 五问五答）；待 spec 复核后进入 writing-plans
> 关联：`docs/roadmap.md` W8 窗口、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 8 阶段
> 说明：本规格起草于 worktree `feature/w8-human-in-the-loop`，与实施代码同分支演进，合并回 main 时一并带回

## 1. 背景与目标

W0-W7 已完成：GitHub Issue → PR 全链路（W5 `run_issue_agent`，dry-run 安全阀）、Docker 沙箱、双知识检索、Evaluation（W6，真实 Resolution Rate 20%）、Observability（W7，全链路 Trace）。

W5 现状：`run_issue_agent` 在 review 后以 `task.push` 布尔开关决定是否 commit+push+PR；Review 为**内联轻量 LLM 审查**（`_review_diff`，PASS/FAIL 首行，FAIL 重试 1 轮）。即已有「LLM 轻量审查 + CLI 开关门禁」，但**没有独立 Reviewer 角色、没有人工审批环节**。

W8 目标（roadmap）：在 review 与 push 之间插入**人工审批门禁**——Reviewer Agent（独立于 Coder 的审查角色）+ Diff 展示与人工审批流程；审批通过后才 Commit → Push → Create PR；最后评估完全自动化的可行性（基于 W6 指标）。

联动（用户决策，2026-08-25 冻结事项）：知识纠错第二阶段（LLM 事实核查 → 写回 → `--apply`）依赖 W8 门禁机制解冻——本窗口把审批机制设计为**通用审批单**（`action_type` 扩展点），`knowledge_apply` 作为预留类型，解冻后复用同一通道。

```
输入：--issue owner/name#N（干跑）→ 人工审阅审批单 → --approve <审批单> --decision approve
输出：PR（或干净拒绝，零远端副作用）
```

## 2. 验收标准（源自 roadmap）

1. Reviewer Agent（独立于 Coder 的审查角色）；
2. Diff 展示与人工审批界面/流程；
3. 审批通过后才 Commit → Push → Create PR；
4. 评估完全自动化的可行性（基于 W6 指标）；
5. TDD + Review + 合并回 main。

**验收标准**：Agent 完成工作后停下等待审批；审批后自动完成 PR；演示完整闭环。

## 3. 设计决策（brainstorming 已确认，2026-08-26）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | 审批流程形态 | **两段式**：`--issue` 干跑产出审批单并停止 → 人工审阅（diff + 审查报告，可对照外部工具）→ `--approve` 审批推送 | 用户选择；异步灵活、可中断、与现 CLI 架构契合度最高 |
| D2 | Reviewer 角色 | **独立审查角色**：独立 system prompt + 独立 LLM 调用 + 独立 trace span；输入 = Issue + diff + verify_rounds，**不见工作过程**（防"自己审自己"锚定） | 用户选择；满足 roadmap「独立于 Coder」，且不踩第一版禁止清单（复杂 Multi-Agent 不做，独立调用而非独立进程） |
| D3 | 门禁后 act 范围 | 批准 → 自动 commit+push+PR；**拒绝 → 干净停止、零远端副作用**（审查报告与审批单留盘供人工处理） | 用户选择；最小闭环，YAGNI |
| D4 | 门禁通用性 | **通用审批单机制**：JSON 审批单带 `action_type` 字段；本期实现 `pr_push`，预留 `knowledge_apply`（知识纠错 --apply 复用同一通道，解冻后另排期） | 用户选择；兑现"W8 门禁就绪后知识纠错第二阶段可解冻" |
| D5 | 验收演示 | **三者都要**：mock+本地 git 远程自动化验收（主路径，不依赖外部仓库）→ 真实 dry-run 停等演示（dbader/schedule#646 产单供人工审阅）→ 真实 PR 演示（前置验证宿主 push 通道，用户提供有写权限测试仓库） | 用户选择 |
| D6 | `--push` 语义 | **废弃自动推送语义**：推送唯一入口为 `--approve`；评估/benchmark 模式不产审批单（`approval_dir=None` 保持 W5 dry-run 行为，W6 评测链路零影响） | 任何"跳过人工门禁的自动推送"都违背 W8 目的；评测本就不 push |

## 4. 架构与组件

```
agent/issue.py        # Review 升级独立角色 + IssueTask.approval_dir + 产单（修改）
tools/approval.py     # 审批单核心：schema/load/save/状态机/漂移检查/执行（新增）
main.py               # --approve CLI（修改）
tools/github_tools.py # 复用 commit_changes/push_branch/create_pull_request（不改）
tests/test_approval.py# 新增
tests/test_approval.py    # 新增
tests/test_issue_agent.py # 补充 approval_dir 产单用例（既有测试无需改）
output/approvals/     # 审批单产物（gitignore）
```

### 4.1 审批单 schema（seam 1：tools/approval.py）

`output/approvals/<repo__issue>-<runid>.json`（runid = `YYYYMMDD-HHMMSS`，同 benchmark 风格）：

```json
{
  "approval_id": "dbader__schedule-646-20260826-183000",
  "action_type": "pr_push",
  "status": "pending",
  "repository": "dbader/schedule",
  "issue_number": 646,
  "branch": "fix/issue-646",
  "commit_message": "fix: 修复 #646 <title>",
  "diff": "<git diff 全文>",
  "diff_sha256": "<sha256(diff)>",
  "review": "PASS ...（结论文本）",
  "verify_rounds": 2,
  "retry_count": 0,
  "created_at": "2026-08-26T18:30:00+08:00",
  "decided_at": null,
  "decision_comment": null,
  "pr_url": null
}
```

- `ApprovalRequest` dataclass + `load_approval(path)` / `save_approval(req, dir)` / `validate_approval(req)`；
- **状态机**：`pending → approved | rejected`（终态）；非 pending 拒绝任何再审批（防重放）；
- `diff_sha256`：审批执行前重新 `git_diff_since` 并比对，防"审批后仓库被外部改动 → push 错内容"。

### 4.2 审批执行（seam 2：approve_request）

```python
def approve_request(approval: ApprovalRequest, decision: str,
                    comment: str | None, workspace_root: str) -> ApprovalRequest
```

流程：
1. **状态检查**：`status != "pending"` → 拒绝执行并报当前状态（防重放）；
2. **漂移检查**：重新计算 `git_diff_since(repository, base)` 的 sha256 与 `diff_sha256` 一致 + 分支存在（`git rev-parse --verify`）→ 不一致/缺失则 Abort（提示重新 `--issue`，审批单保持 pending）；
3. `decision == "approve"`：`commit_changes` → `push_branch` → `create_pull_request`（PR body 含验证轮数与审查结论，同 W5）→ 审批单置 `approved` + `pr_url` + `decided_at`；
4. `decision == "reject"`：置 `rejected` + `decision_comment`，**零远端副作用**。

错误语义：审批单文件缺失/schema 非法 → 明确报错退出；approve 中途失败（如 push 网络）→ 报错、审批单保持 pending 可重试；已 commit 未 push 时提示人工处理。

### 4.3 run_issue_agent 变更（W5 基础上的精准修改）

- `IssueTask` 新增 `approval_dir: str | None = None`（CLI 设为 `output/approvals`；评估/测试不传）；
- review（FAIL 重试 1 轮逻辑保留）之后：`approval_dir` 非 None → 生成审批单（diff / review / verify_rounds / retry_count）→ 保存 → 打印审批单路径与"等待人工审批，审阅后执行 `--approve`"→ **停止，不 push**；
- `approval_dir is None` → 行为与 W5 dry-run 完全一致（review 后停止，不产单）——评估（`benchmark/runner.py` 不传 approval_dir）与既有测试零影响；
- **`IssueTask.push` 字段与 `--push` CLI 开关、`KA_ISSUE_PUSH` 环境变量一并移除**（不保留死字段，杜绝任何绕过人工门禁的自动推送入口）；`benchmark/runner.py` 同步删除 `push=False` 实参（一行，属于本窗口语义变更的连带调整）；既有测试无 push 断言（已核），无需改测试。

### 4.4 Reviewer 独立审查角色（agent/issue.py）

- 新 `REVIEW_PROMPT`（独立角色，与 `_decide_system` 分离）：输入 = Issue 全文 + diff + verify_rounds（测试验证轮数说明）；
- 审查四要点**：① Issue 满足度（变更是否解决 Issue）；② 明显错误/遗漏；③ **无关改动**（不得混入与 Issue 无关的修改——W5 实证盲区：`every()` docstring 被顺手改坏未被 review 发现）；④ **测试并入现有套件**（W5 实证盲区：Agent 留下独立验证脚本而非写入 test_schedule.py；发现独立脚本应指出并要求并入）；
- 输出格式：首行 PASS/FAIL（兼容 W5 重试判定语义），后续中文要点列表；需要人工关注的事项（依赖缺失、环境差异等）以「人工关注」行标记；
- trace：现有 `review` span（W7 已埋）metadata 扩展 verdict（结论首行，已有 first_line）+ 无关改动/人工关注标记。

### 4.5 CLI

```bash
python main.py --issue dbader/schedule#646                       # 干跑：工作 → review → 产审批单 → 停止
python main.py --approve output/approvals/xxx.json --decision approve
python main.py --approve output/approvals/xxx.json --decision reject --comment "测试未并入现有套件"
```

- `--approve`：文件存在校验 → load/validate → `approve_request`（§4.2）；
- `--issue` 原 `--push` 开关**直接移除**（不留绕过人工门禁的入口）；`KA_ISSUE_PUSH` 环境变量同步废弃；结束提示改为"审批单已生成：<路径>，人工审阅后执行 `--approve`"。

### 4.6 完全自动化可行性评估（roadmap 任务 4，交付物）

写入 `docs/roadmap.md` W8 完成记录 + `docs/devlog.md`（不单开文档，控制文件数）：

- 输入数据：W6 真实指标（Resolution Rate 20%、test_pass 基准缺口=沙箱镜像缺 pytz、Judge PASS 但 test=False 案例）+ W8 门禁运行数据（审批通过/拒绝率、人工关注标记出现率、漂移检查触犯次数）；
- 输出建议：自动化推送的候选条件（如测试全绿 + Judge PASS + 无无关改动 + 无人工关注标记 + 低风险类别），高风险类别（知识写回、外部 API 副作用）保留人工门禁的结论。

## 5. 测试策略（TDD，seam = approval.py / approve_request / run_issue_agent approval_dir / main.py --approve）

1. **approval.py**：load/save 往返、schema 非法（缺字段/坏 JSON）报错、状态机（pending→approved/rejected、非 pending 再审批拒绝、非法 decision 值拒绝）；
2. **approve_request**（mock gh + 本地临时 git 仓库）：
   - approve 路径：commit/push/create PR 调用被触发（Fake gh 断言）+ 审批单置 approved + pr_url 回填；
   - reject 路径：零远端副作用（Fake gh 断言未调用）+ 置 rejected + comment 落盘；
   - 漂移检查：审批后改动仓库文件 → diff sha 不一致 → Abort、审批单保持 pending；
   - 防重放：approved 审批单再次 approve → 拒绝；
3. **run_issue_agent**：`approval_dir` 非 None → 产单（文件存在、字段齐全、无远端副作用）；`None` → 不产单（评估兼容回归）；
4. **main.py**：`--approve` 参数校验（缺文件/缺 --decision/非法 decision）；
5. **真实演示（验收，D5）**：
   - 自动化验收主路径：本地临时 git 远程 + Fake gh 全链路（干跑产单 → --approve → 本地 push → 伪 PR）；
   - 真实 dry-run 停等演示：dbader/schedule#646 实跑 → 产审批单 → 展示 diff + 审查报告 → 人工批准或拒绝（环境：gh 已登录、镜像克隆通道已就绪）；
   - 真实 PR 演示：**前置实测宿主 push 通道**（github.com SNI 阻断；镜像 `insteadOf` 仅验证过 clone，push 可用性待实测；不可用则与用户确定替代通道）→ 用户提供有写权限测试仓库 + open issue → 全链路真实 PR；
6. **全量回归**：`pytest tests/`（基线 227 passed / 4 skipped / 1 已知环境性失败 + 新增用例全绿）。

## 6. 配置

| env | 默认 | 说明 |
|-----|------|------|
| `KA_APPROVAL_DIR` | `output/approvals` | 审批单输出目录 |
| `KA_GITHUB_BRANCH_PREFIX` | `fix/issue-` | 分支前缀（沿用 W5） |
| `KA_EXECUTOR` | local | 工作执行器（沙箱），门禁本身宿主执行（凭据隔离延续 W5） |

无新增 Python 依赖；复用 gh CLI（已登录）、git 与现有工具层。

## 7. 错误处理与返回语义

| 场景 | 行为 |
|------|------|
| 审批单不存在 / schema 非法 | `--approve` 明确报错退出 |
| status 非 pending | 拒绝执行并报当前状态（防重放） |
| 漂移检查失败（diff hash 不一致 / 分支缺失） | Abort，提示需重新 `--issue`；审批单保持 pending |
| approve 中途失败（push 网络等） | 报错，审批单保持 pending 可重试；已 commit 未 push 时提示人工处理 |
| reject | 零远端副作用，exit 0 |
| 评测 / 测试模式 | 不产审批单，行为与 W5 dry-run 一致 |

## 8. 非目标（YAGNI，第一版不做）

- **拒绝回注重跑**（人工意见喂回 Agent 重跑出第二版）：后续扩展，本版拒绝=干净停止；
- **Web 审批页**（FastAPI 本地服务）：保持 CLI + 文件展示（终端/编辑器/GitHub 网页对照均可审阅 diff）；
- **knowledge_apply 本期实现**：仅预留 `action_type` 与 dispatch 扩展点，知识纠错第二阶段解冻后另排期实施；
- **复杂 Multi-Agent 框架**（第一版禁止清单）：Reviewer 为独立角色独立调用，非独立 agent 进程/子图；
- **自动审批 / 全自动推送**：本窗口只产出可行性评估文档，不实现自动推送。

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| push 通道被 SNI 阻断（github.com 主站 CONN_RESET；镜像 insteadOf 仅验证过 clone） | 真实 PR 演示前实测宿主 push（直连 vs 镜像）；不可用则与用户确定替代通道或用测试仓库；自动化验收以 mock+本地远程为主路径，不受影响 |
| 审批单漂移（审批后仓库被外部改动 → push 错内容） | `diff_sha256` + 分支存在双校验，不一致 Abort |
| 防重放 / 误操作 | 状态机终态拒绝重复审批；`--decision` 必填校验 |
| `--push` 语义变更影响既有用法/测试 | 评估与测试模式零影响（`approval_dir=None`）；唯一连带 = runner.py 删除 `push=False` 一行；推送入口迁移到 `--approve` 并文档注明 |
| Reviewer 盲区（W5 实证：顺手改动、非正式测试脚本未被发现） | 审查四要点显式入提示词 + 无关改动清单 + 人工关注标记，供审批人重点核对 |
| LLM 稳定性（APITimeoutError 冒泡终止） | 沿用 NO_PROXY 直连；图内异常语义不变（重试机制改进不在 W8 范围，记 W6 P1 评估问题清单） |
| 真实 PR 演示依赖用户提供仓库 | 演示排期弹性：mock 验收先行，真实 PR 在 push 通道验证后与用户约定时间执行 |

## 10. 依赖

- 外部：gh CLI（已登录）、git、LLM API（opencode_go，mimo-v2.5）、（真实演示时）用户有写权限的测试仓库；
- Docker 仅用于工作执行器（KA_EXECUTOR=docker 时），门禁与 push 全程宿主执行。