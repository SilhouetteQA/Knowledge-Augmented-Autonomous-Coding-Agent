# W7 Observability 设计规格

> 日期：2026-08-25
> 状态：草稿（brainstorming 三项决策已确认；待 W6 合并后完善并进入 writing-plans）
> 关联：`docs/roadmap.md` W7 窗口、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 7 阶段
> 说明：W6（Evaluation）先行（用户决策，串行）；W7 worktree `.worktrees/w7-observability` / 分支 `feature/w7-observability` 已就绪。W6 已完成 `tools/tracing.py`（Langfuse 懒加载 + traced 装饰器）、LLM generation 埋点、issue/benchmark span——W7 在此基础上补齐全链路。

## 1. 背景与目标

W6 已落地 Langfuse 基础埋点（`tools/tracing.py` 可开关 traced 装饰器 + LLM chat generation + usage 累计 + issue/benchmark 顶层 span）。W7 的目标是把可观测性补全为**全链路 Trace**：

```text
Issue → Planner → Tool Call → LLM → Test → Failure → Retry → Review → PR
```

每条链路记录：Latency / Tokens / Cost / Tool Calls / Errors / Retries / Test Results，且一次 Agent 任务执行后可导出完整 Trace 与本地成本报告。

## 2. 验收标准（源自 roadmap）

1. 全链路 Trace 采集：Issue → Planner → Tool Call → LLM → Test → Failure → Retry → Review → PR 各环节可见（Langfuse UI trace 树）；
2. 指标记录：Latency / Tokens / Cost / Tool Calls / Errors / Retries / Test Results；
3. 集成 Langfuse + OpenTelemetry（OTLP 导出验证）；
4. 一次 Agent 任务执行后可导出完整 Trace 与成本报告；
5. TDD + Review + 合并回 main。

## 3. 设计决策（brainstorming 已确认）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | OTel 落地形态 | **Langfuse 为主**：埋点层统一走 `langfuse.observe`（SDK 底层即 OTel）；验收时做一次 OTLP → Langfuse 导出验证（trace 树在 UI 可见即双达标） | 不引入第二套埋点 API（与 W6 tracing.py 重复）；Langfuse 即 OTel 后端 |
| D2 | 埋点覆盖层 | **完整链路埋点**：graph 节点（plan / decide / execute / verify / reflect / finalize）+ 工具 dispatch + run_tests 结果 + review + retry 次数 + issue 阶段（W6 已有 LLM/issue/benchmark 基础，W7 补齐其余） | 满足 roadmap「Issue → Planner → Tool Call → LLM → Test → Failure → Retry → Review → PR」全链路可见 |
| D3 | 报告形态 | **本地汇总报告 + Langfuse UI**：trace 导出脚本从 Langfuse API 拉取某任务 trace → JSON/Markdown 汇总报告（与 W6 评测报告格式对齐）；UI 用于交互式复盘 | 本地可归档/对比成本，UI 用于可视化；不引入 ClickHouse 直查（YAGNI） |

## 4. 架构与组件

```
tools/tracing.py             # 已有（W6）：is_enabled/get_client/flush/traced/record_usage
agent/graph.py               # 修改：节点函数（plan/decide/execute/verify/reflect/finalize）traced 包装
agent/loop.py                # 修改（如适用）：decide 环节工具调用计数
tools/shell_tools.py         # 修改：dispatch/executor 层 span（工具名/参数摘要/latency/error）
tools/report_trace.py        # 新增：trace 导出脚本（Langfuse API → JSON/Markdown 报告）
main.py                      # 修改：--trace-report <trace_id>（导出单任务 trace 汇总）
docker/langfuse/             # 已有（W6 Task 10 部署）：compose + .env
tests/                       # 新增 test_trace_report.py 等
```

### 4.1 埋点清单（Trace 结构，完整链路）

| 节点 | 类型 | 位置 | 记录字段 |
|------|------|------|----------|
| `issue.run`（已有） | agent | agent/issue.py run_issue_agent | 顶层 span |
| `graph.plan` | span | agent/graph.py plan 节点 | 计划文本 |
| `graph.decide` | span+generation | decide 节点（LLM 调用） | iteration, model, tokens, tool_calls 数 |
| `graph.execute` | span | execute 节点 | 工具名/参数摘要/结果摘要 |
| `graph.verify` | span+generation | verify 节点 | 测试结果（passed/failed/error/total）, verify_round |
| `graph.reflect` | span+generation | reflect 节点 | 失败原因 |
| `graph.finalize` | span | finalize 节点 | 最终回答摘要 |
| `tool.<name>` | span | 工具执行（dispatch / executor） | 名称、参数摘要（截断）、latency、error |
| `test.run` | span | run_tests | passed/failed/error/total/duration、test 路径 |
| `review` | span+generation | issue.py _review_diff | 结论（PASS/FAIL）、diff 摘要 |
| `retry` | span（metadata） | issue.py FAIL 重绕 / reflect 重试 | retry_count（span metadata 记录次数） |

- 埋点全部经 `tools.tracing.traced()`（关闭态零开销，不侵入行为）；
- metadata 摘要统一截断（≥1000 字符截断，防敏感/大文本——沿用兄弟项目经验）；
- **retry 记录机制**：在 decide/verify 循环的 `iteration` 计数之上，graph 已有 verify_rounds（W2 设计）；issue.py 有 FAIL 重试 1 轮。W7 在对应 span metadata 记录 `verify_round` / `retry_count`，从已有计数器读取（不新增状态）。

### 4.2 工具与测试埋点（seam：dispatch 层）

- 现有工具调用路径：`agent/loop.py` `_invoke`（或 graph 的 execute 节点）→ `dispatch` → executor。W7 在该处包 `traced("tool.execute")`，metadata 含工具名/参数摘要；
- `run_tests`（tools/shell_tools.py）包 `traced("test.run")`，metadata 含 TestResult 各字段；
- 注意：execute 已作为节点存在时，工具级 span 作为其子级（Langfuse 自动嵌套——observe 在异步/同步调用栈内自动挂父子）。

### 4.3 Trace 导出与汇总报告（seam：tools/report_trace.py）

```python
@dataclass
class TraceSummary:
    trace_id: str
    task: str                       # 顶层 span 名/输入摘要
    total_latency_s: float
    tokens_prompt: int
    tokens_completion: int
    cost_usd: float
    tool_calls: int
    errors: list[str]
    retries: int
    test_results: list[dict]        # {test: path, passed/failed/error/total, duration}
    steps: list[dict]               # 节点名 → 子 trace 摘要

def fetch_trace(client, trace_id: str) -> TraceSummary: ...   # Langfuse API（client.trace.get / observe 查询）
def save_trace_report(summary: TraceSummary, out_dir: str) -> str:  # JSON + Markdown
def trace_report_md(summary: TraceSummary) -> str: ...
```

- Langfuse SDK 提供 trace 查询（`langfuse.client.trace.get(id)` / `client.trace.list`），按 trace_id 取 `observations` 汇总；
- 输出：`output/trace/<trace_id>/` 下 report.json + report.md（与 W6 风格一致）；含时间/commit metadata（可选复用 W6 `RunMetadata` 思路，独立简化版）；
- CLI：`python main.py --trace-report <trace_id> [--trace-out output/trace]`；
- 关闭态/无 trace_id → 明确报错退出（不静默）。

### 4.4 OTLP 导出验证

- Langfuse SDK v4 底层即 OTel（observe 生成的 span 经 OTLP 上报 langfuse 后端）；验收验证：真实任务跑一次 → Langfuse UI 可见完整 trace 树（即 OTLP 链路贯通证明）；
- 若 SDK 有显式 OTLP exporter 配置开关（`LANGFUSE_OTLP_ENDPOINT` 等），文档记录用法；不额外引入 `opentelemetry-*` 包（YAGNI，W6 依赖 langfuse>=4.0 已含）。

## 5. 测试策略（TDD，seam = traced 埋点行为 + report_trace 函数）

1. **埋点开启/关闭行为**：延续 W6 tracing 测试模式——关闭态节点行为与现状一致（直通、无副作用）；开启态 mock observe 断言包装发生；
2. **graph 节点埋点**：Mock LLM 跑 graph，断言节点函数被 traced 包装（monkeypatch tracing.is_enabled=True + FakeObserve 记录调用名单：plan/decide/execute/verify/reflect/finalize 均在列）；
3. **tool/test 埋点**：dispatch 测试断言 span metadata（工具名/参数摘要/TestResult 字段）；
4. **report_trace**：Fake Langfuse client（固定 observations 数据）→ fetch_trace 汇总正确（latency/tokens/cost/errors/retries/test results）；save_trace_report 产出 JSON/Markdown 结构与字段断言；
5. **真实验收**：W6 基准评测（或单 issue 任务）运行中开启 tracing → 在 Langfuse UI 按 trace 检查完整链路；`--trace-report` 导出报告人工核对；
6. 全量回归：关闭态下 `pytest tests/` 全绿（不触网）。

## 6. 配置与 CLI

| env | 默认 | 说明 |
|-----|------|------|
| `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL` | 无 | 三键齐备才启用（W6 已定） |
| `KA_TRACING` | 无 | =0 强制关闭（W6 已定） |
| `TRACE_OUT` | output/trace | 报告输出目录（或 --trace-out） |

```bash
python main.py --trace-report <trace_id> [--trace-out output/trace]
```

## 7. 错误处理与返回语义

| 场景 | 行为 |
|------|------|
| tracing 未启用时运行任务 | 行为与 W6 前一致（零开销） |
| `--trace-report` 传空/无效 trace_id | 明确报错（Langfuse 查询失败原样输出），退出码 1 |
| Langfuse 后端不可达（已启用） | 埋点层静默容错（W6 record_usage 已 try/except），任务不中断 |
| 查询 trace 无 observations | 报告含空步骤列表 + 提示 |

## 8. 非目标（YAGNI，第一版不做）

- 不做自定义 trace 可视化前端（Langfuse UI 为准）；
- 不引入独立 opentelemetry SDK 包/第二套埋点 API；
- 不做 ClickHouse 直查成本统计（兄弟项目模式，W6/W7 暂不需要）；
- 不埋 MCP/知识库子进程内部 span（工具级 span 已覆盖「search_knowledge 调用」）；
- 不自动抓取全部历史 trace（按 trace_id 导出单任务）。

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| W6 已提交的 tracing.py 与 W7 需要扩展冲突 | W7 在 W6 合并后开始（串行决策），且 traced() 设计已预留通用性 |
| graph 节点 wrapped 后行为回归 | 关闭态直通测试保证；Mock LLM 全链路测试覆盖 |
| Langfuse trace 查询 API 版本差异 | 冒烟先行：真实 run 后 fetch 一次确认字段；SDK v4 文档为据 |
| metadata 过大（diff/测试输出） | 统一截断 ≥1000 字符；仅摘要入 span |
| OTLP 验证依赖容器/后端在线 | 验收时 docker compose up；失败则记录并降级为「UI 可用性验收」 |

## 10. 依赖

- 新增 Python 依赖：无（langfuse>=4.0 已由 W6 extra `[eval]` 引入；如需查询 API 已在包内）
- 外部：Docker（Langfuse 后端）、LLM API（opencode_go）。

## 11. 待 W6 合并后完善项

- [ ] 核对 W6 实际交付的 `tools/tracing.py` 接口（traced 签名/metadata_fn 行为）与本 spec §4.1 埋点清单的匹配；
- [ ] 补充 `agent/graph.py` 节点函数名与行号（W6 后代码基线）；
- [ ] 确认 `main.py` CLI 参名称与 W6 风格一致；
- [ ] 与 W6 benchmark 报告的联动（可选：W6 runner span 增补 tool/test 子级后，评测报告可直接引用 trace）。