# W6 Evaluation 设计规格

> 日期：2026-08-25
> 状态：草稿（用户已批准设计，2026-08-25；待 spec 复核后进入 writing-plans）
> 关联：`docs/roadmap.md` W6 窗口、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 6 阶段
> 说明：本规格起草于 worktree `feature/w6-evaluation`，与实施代码同分支演进，合并回 main 时一并带回

## 1. 背景与目标

W0-W5 已完成：文件/Shell/Test/Git 工具、Docker 沙箱、双知识检索（Code Graph + Arknights KG）、GitHub Issue → PR 全链路（`run_issue_agent`，dry-run 安全阀）；知识抽查（`--correct`）已有知识质量指标雏形。

W6 的目标是建立**可复现的评估体系**：固定基准（真实 Issue 快照 + gold patch）、一键评测运行器、核心指标 Issue Resolution Rate、以及版本演进证据链（V1 → V2 → ... 对比），使"任意 Agent 版本变更可在同一基准上重跑并对比指标"成为可能。

```
输入：基准任务集 + 当前 Agent 版本
输出：JSON 报告 + Markdown 报告（含 run metadata，多轮可对比）
```

## 2. 验收标准（源自 roadmap）

1. Issue 基准库：`benchmark/cases/` 下 bug / feature / test / refactor / domain 五类任务（首次 6-10 个）；
2. 指标采集：Issue Resolution Rate（核心）/ Test Pass Rate / Patch Acceptance Rate / Tool Success Rate / Iteration Count / Latency / Cost；
3. 版本对比机制：每次运行记录 run metadata，多轮运行可在同一基准上对比（→ V1 32% → V2 47% 演进证据链）；
4. LLM-as-a-Judge 与回归测试辅助判定；
5. TDD + Review + 合并回 main。

## 3. 设计决策（brainstorming 已确认）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | 窗口推进 | **串行，先 W6 后 W7**；W7 worktree/分支已建（`.worktrees/w7-observability` / `feature/w7-observability`），W6 合并后启动 | 用户选择；避免两窗口同时改动埋点/指标相关代码造成冲突 |
| D2 | 基准任务形态 | **真实 Issue 快照 + gold patch**：每任务 JSON 含 issue 标题/正文快照 + 社区已合并 PR 的 diff；五类各 1-2 个，首次 6-10 个 | 静态可控、离线可重跑；gold patch 作为 LLM Judge 对照基准 |
| D3 | 运行时指标采集 | **直接接入 Langfuse**（兄弟项目 Docker 部署经验直接套用），不造轻量采集器；LLM usage 本地累计作为兜底备用 | 用户选择；LANGFUSE 三键缺失时自动关闭、零开销 |
| D4 | Resolution 判定 | **测试全绿 + LLM Judge 对比 gold** 双判定：测试判定（diff 非空 + 应用后 must_pass 测试通过）+ Judge 等价性判定（PASS/FAIL + 理由） | 测试客观兜底（防假修复），Judge 兜功能等价（feature 类无预写测试）；两者结合 |
| D5 | 版本对比 | **run metadata**：git commit / 分支 / 模型 / 时间戳 / 执行器 / 关键参数写入每轮报告的头部；`--compare` 合并多轮 JSON 产出演进表 | 满足"任意版本重跑对比"且实现轻量 |
| D6 | 任务规模 | 首次 **6-10 个**（dbader/schedule#646 必选 + 小型 Python 库已关闭 Issue 中挑选） | 平衡工作量与五类覆盖；15+ 留后续扩充 |
| D7 | Langfuse 形态 | 新模块 `tools/tracing.py`：懒加载 client + `traced()` 可开关装饰器，**独立实现**（照搬兄弟项目模式但依赖隔离，同 W4 knowledge_client 决策） | 不引用兄弟项目代码路径（跨仓库耦合）；关闭态零网络 |

## 4. 架构与组件

```
benchmark/
├── cases/                 # 基准任务定义（五类目录 + gold/ 存放 gold patch diff）
│   ├── bug/schedule-646.json
│   ├── feature/...
│   ├── test/...
│   ├── refactor/...
│   └── domain/...
├── loader.py              # 任务加载与校验（benchmark case schema）
├── runner.py              # 评测运行器（遍历 cases → 执行 → 判定 → 汇总）
├── judge.py               # 判定逻辑（测试全绿 + LLM Judge 等价性）
└── report.py              # run metadata + JSON/Markdown 报告 + --compare 版本对比
tools/tracing.py           # Langfuse 懒加载 client + traced() 装饰器（新增）
agent/llm.py               # chat_completion 埋 generation（usage/tokens）（修改）
agent/issue.py             # run_issue_agent 关键阶段埋 span（修改，最小侵入）
main.py                    # --benchmark 模式入口（修改）
docker/langfuse/           # Langfuse v4 Docker 部署（从兄弟项目复制 compose）
output/benchmark/<run_id>/ # 运行产物（gitignore）
```

### 4.1 基准任务 schema（seam 1：benchmark/loader.py）

```json
{
  "id": "schedule-646",
  "category": "bug",
  "repository": "dbader/schedule",
  "issue": {"title": "...", "body": "..."},
  "gold_patch": "gold/schedule-646.diff",
  "must_pass": ["test_schedule.py"],
  "max_iterations": 30,
  "notes": "单元守卫 __repr__ 崩溃，社区 PR #652 同方案"
}
```

- `loader.load_cases(cases_dir) -> list[BenchmarkCase]`：读取 + **schema 校验**（id 唯一 / category 合法 / gold_patch 存在且可读 / must_pass 非空）；校验失败 → `CaseError` 明确报错（不静默跳过）。
- `BenchmarkCase` dataclass：id / category / repository / issue / gold_patch / must_pass / max_iterations / notes。

### 4.2 评测运行器（seam 2：benchmark/runner.py）

```python
@dataclass
class CaseResult:
    case_id: str
    category: str
    status: str                # resolved / not_resolved / error
    resolution: bool
    test_pass: bool
    patch_acceptance: bool
    judge_verdict: str         # PASS / FAIL / SKIP（judge 不可用时）
    judge_reason: str
    tool_success_rate: float
    iteration_count: int
    latency_s: float
    tokens_total: int
    cost_usd: float
    diff: str
    errors: list[str]

@dataclass
class BenchmarkReport:
    run_id: str
    metadata: RunMetadata     # D5
    total: int
    resolved: int
    resolution_rate: float
    results: list[CaseResult]
```

运行流程（每 case）：
1. `run_issue_agent(IssueTask(repository, issue_number=<真实编号>, issue_snapshot=case.issue, ...))` —— issue 正文直接用快照的 title/body，**不依赖 GitHub 在线获取**：W5 的 `run_issue_agent` 以 `get_issue` 开头，需提供**离线快照注入 seam**（见 §4.3）；issue_number 保留真实编号（仅跳过在线 get_issue 调用，其余行为不变）；
2. 收集 diff（result.diff 非空？）+ 应用 patch 后跑 `must_pass` 测试（复用 run_tests / 沙箱）→ `test_pass`；
3. LLM Judge：`judge_patch(agent_diff, gold_patch, issue) -> PASS/FAIL + 理由` → `patch_acceptance`；
4. 汇总：resolution = test_pass and patch_acceptance（Judge 不可用或 diff 空时 SKIP 判定，resolution=False 并记录原因）。

### 4.3 run_issue_agent 离线快照 seam（agent/issue.py 最小修改）

W5 的 `get_issue` 走 gh 在线获取。W6 需要离线可重跑：在 `IssueTask` 增加 `issue_snapshot: GitHubIssue | None`，非 None 时跳过 `get_issue`（直接使用快照）；`get_repository`/clone 仍需在线（目标仓库克隆无法离线），clone 用 `--depth 1` 降低带宽（W5 遗留优化）。

```python
@dataclass
class IssueTask:
    repository: str
    issue_number: int
    issue_snapshot: GitHubIssue | None = None   # 新增：非 None 时离线模式
    workspace_root: str = "workspace"
    push: bool = False
    max_iterations: int = 30
```

### 4.4 LLM-as-a-Judge（seam 3：benchmark/judge.py）

```python
def judge_patch(llm: LLMClient, agent_diff: str, gold_patch: str, issue: GitHubIssue) -> JudgeResult:
    """对照 gold patch 判定 agent diff 的功能等价性。JudgeResult = verdict + reason。"""
```

- 提示词 `JUDGE_PROMPT`：输入 issue 描述 + agent diff + gold patch → 输出**首行 PASS/FAIL** + 中文理由（复刻 W5 REVIEW_PROMPT 的轻量模式）；
- LLM 不可用（无 key）→ `JudgeResult(SKIP, "LLM 不可用")`，resolution 判定回退为仅 test_pass 并记录；
- Judge 输出非 PASS/FAIL → 按 FAIL 处理并记录解析警告（LLM 输出校验，AGENTS.md 八-5）。

### 4.5 指标采集

| 指标 | 来源 |
|------|------|
| Issue Resolution Rate | judge + runner 汇总（核心） |
| Test Pass Rate | runner 应用 patch 后跑 must_pass |
| Patch Acceptance Rate | LLM Judge 等价性通过数 / 判定任务数 |
| Tool Success Rate | `IssueAgentResult.steps` 中成功工具调用 / 总调用（本地统计） |
| Iteration Count | `IssueAgentResult`（stopped_by_limit / iteration 计数） |
| Latency | 每 case 起止时长（time.monotonic）；LLM 调用时长经 Langfuse trace 可见 |
| Tokens / Cost | LLM usage 本地累计（prompt+completion tokens；cost 按模型单价常量换算）；Langfuse 记录原始 usage |

- **ModelConfig 单价表**：`benchmark/cost.py`（或 report.py 内常量）：`mimo-v2.5` 单价常量（按用户提供的计费口径；未知模型单价 → cost=0 且报告标注"单价未知"）。

### 4.6 Langfuse 接入（tools/tracing.py）

照搬兄弟项目模式（独立实现）：
- `get_tracing()`：懒加载；`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` 三键齐备才初始化，否则返回关闭态 stub（零开销、零网络）；
- `traced(name, as_type="span"|"generation", metadata_fn=None)` 装饰器：开启态包装 `langfuse.observe`，关闭态直通原函数；
- 埋点 seam：
  - `agent/llm.py` `chat_completion` → generation（input/output + usage）；
  - `agent/issue.py` `run_issue_agent` → 顶层 span + 阶段 span（fetch/clone/work/diff/review）；
  - `benchmark/runner.py` → 每 case 一个 span（case_id metadata）；
- 部署：`docker/langfuse/docker-compose.yml` + `.env.example` 从兄弟项目 `docker/langfuse/` 复制（Langfuse v4：web+worker+postgres17+clickhouse25.12+redis7+minio），`.env`（含密钥）不入库。

### 4.7 报告（benchmark/report.py）

- `save_json(report)` → `output/benchmark/<run_id>/report.json`（完整结构化，供 --compare 与外部消费）；
- `save_markdown(report)` → `output/benchmark/<run_id>/report.md`：
  - 头部 = run metadata（git commit / 分支 / 模型 / 时间戳 / 执行器 / 参数）；
  - 核心：Resolution Rate 统计 + 五类分组表；
  - 明细：case × 指标表（resolution / test / acceptance / iteration / latency / cost）；
- `compare_reports(run_ids)` → Markdown 演进对比表：行=指标（Resolution Rate / Test Pass / Patch Acceptance / 平均迭代 / 平均耗时 / 总成本），列=各 run_id → `output/benchmark/compare_<ts>.md`。

## 5. 测试策略（TDD，seam = loader / runner / judge / report / tracing）

1. **loader**：合法 case 加载、重复 id、category 非法、gold_patch 缺失/不可读 → CaseError；
2. **runner**（Fake/Mock：MockLLMClient + Fake run_issue_agent 注入）：单 case resolved / not_resolved / error 分支；test_pass 与 patch_acceptance 组合判定；Judge SKIP 回退逻辑；
3. **judge**：PASS / FAIL / SKIP / 非标准输出处理；Judge 提示词含完整上下文断言；
4. **report**：JSON 结构、Markdown 头部与表格、compare 演进表生成（2 个 fake run）；
5. **tracing**：关闭态直通（无 LANGFUSE 键时不触发网络/不改变行为）；开启态包装（mock observe）；
6. **issue_snapshot 离线 seam**：`IssueTask(issue_snapshot=...)` 时跳过 get_issue（monkeypatch gh 断言未调用）；
7. **真实演示**（验收）：6-10 个 case 实跑一轮，出 JSON + Markdown 报告；对 schedule-646 断言 resolution=True（历史已证可行）。

## 6. 配置与 CLI

```bash
python main.py --benchmark [--cases benchmark/cases] [--executor local|docker]
                  [--benchmark-out output/benchmark] [--compare <run_id1,run_id2>]
```

| env | 默认 | 说明 |
|-----|------|------|
| `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL` | 无 | 三键齐备才启用 tracing，否则关闭态 |
| `KA_ISSUE_PUSH` | 0 | 评测永不 push（runner 强制 task.push=False） |
| 模型单价常量 | 代码内置 | 未知模型 → cost 0 + 标注 |

## 7. 错误处理与返回语义

| 场景 | 行为 |
|------|------|
| case schema 非法 | CaseError 中止运行（不静默跳过） |
| 单 case 执行异常（clone/依赖/LLM 中断） | 记入 errors，status=error，其余 case 继续，报告标注 |
| Judge LLM 不可用 | SKIP + 回退 test_pass-only，报告标注 |
| clone 超时/网络失败 | 该 case error，记错误原文字段（复用 W5 语义） |
| Langfuse 不可达 | 关闭态直通，不影响运行（LANGFUSE 不在线时进程不崩溃） |

## 8. 非目标（YAGNI，第一版不做）

- 不做 CI/定时调度（本地命令式运行）；
- 不做多模型自动对比矩阵（v1 单模型，手动多轮运行 + --compare）；
- 不扩 15+ 任务（后续按需增量添加 case 文件）；
- 不做 SWE-bench 全量接入（任务格式兼容，但非 v1 范围）；
- Live GitHub 执行/PR 创建（评测一律 dry-run）；
- 完整全链路 Trace（W7 范围，W6 只埋必要 seam）。

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 真实 Issue 快照选择与 gold patch 提取耗时 | 优先复用已验证的 schedule-646；其余选已合并 PR 清晰的小型仓库（如 schedule 其他 issue、标准库周边小型项目） |
| 离线 issue 快照与 online 克隆并存导致结果偏差 | run metadata 记录 clone 时间与 commit；报告注明 |
| Judge 判定与人工结论偏差 | 判定理由全文落盘；真实验收时对 schedule-646 人工复核一致性 |
| Langfuse 部署/兼容性问题 | 沿用兄弟项目验证过的 v4 compose；三键缺失自动关闭不影响评测主链路 |
| 评测耗时（每 case 一次完整 Agent 运行） | 6-10 case × ≤30 迭代设上限；多 case 串行执行 v1，并行执行留后续 |
| W5 遗留：迭代上限 30 探索开销大（devlog W5 实测） | case 级 max_iterations 可配置，报告记录 iteration_count 与 stopped_by_limit |

## 10. 依赖

- 新增 Python 依赖：`langfuse>=4.0`（可选 extra `[eval]`，与兄弟项目一致）——断网环境从基础解释器复制 (W1 经验)；
- 外部：Docker（Langfuse 部署 + 沙箱执行器）、gh CLI（clone 用）、LLM API（opencode_go）。