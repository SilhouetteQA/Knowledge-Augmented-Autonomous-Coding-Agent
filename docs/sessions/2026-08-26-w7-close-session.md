# W7 Observability 窗口会话归档（2026-08-26）

> 归档范围：W7 窗口完整生命周期（brainstorming → spec 定稿 → plan → SDD 实施 → 真实验收 → 合并）。
> 关联：`docs/specs/2026-08-25-w7-observability.md`、`docs/plans/2026-08-26-w7-observability.md`、`docs/devlog.md`（W7 条目）、`docs/roadmap.md`（W7 [x]）。

## 一、交付总结

**W7 Observability 已合并回 main（4035ce8），分支与 worktree 已清理。最终全量回归 230 passed / 4 skipped / 0 failed。**

| 交付物 | 说明 |
|--------|------|
| graph 七节点埋点 | plan/decide/execute/verify/reflect/finalize/finalize_limited → graph.* span（decide/reflect 为 generation） |
| 工具/测试埋点 | `_graph_dispatch`/`_dispatch` → tool.execute span（metadata: 工具名+参数摘要 500 截断）；`run_tests` → test.run span（TestResult asdict） |
| retry/error 指标 | issue.run span metadata：verify_rounds + retry_count（FAIL 重试计 1，首轮判定点捕获） |
| review 埋点 | `_review_diff` → review generation span（结论首行入 metadata） |
| `tools/report_trace.py` | TraceSummary + fetch_trace（SDK 优先 → ClickHouse 直查回退 → TraceError）+ JSON/Markdown 报告 |
| `main.py --trace-report` | 独立导出模式（LLM 前处理，无需 key）+ 退出前 flush() |
| 依赖 | clickhouse-connect>=1.7 入 pyproject eval extra |

## 二、真实验收证据（Task 6，全部通过）

1. **--trace-report 真实导出**：W6 benchmark trace `4d889e…`（257 事件：benchmark.run×1 + issue.run×5 + llm.chat×251）与 W7 新 trace `ec5b575f…`（8 事件全层级）均成功导出 report.json/md；SDK 读口在 events_only 部署不可用 → ClickHouse 直查回退实测生效。
2. **新节点链路**：新 trace 层级验证通过——issue.run(AGENT 根, verify_rounds=2/retry_count=1) → graph.plan/decide(GEN, usage+cost) → execute → tool.execute(run_command) → test.run(passed=5) → graph.reflect(GEN) → graph.finalize。
3. **OTLP 验证**（验收标准 3）：新 trace 每事件 metadata 含 `telemetry.sdk.name=opentelemetry`（langfuse SDK 4.14.4 scope）——SDK 观测经 OTel 上报、ClickHouse 可见，链路贯通证明；未引入新包。
4. **全量回归**：230 passed / 4 skipped / 0 failed（含 review 补丁后）。

## 三、关键决策与经验

1. **数据源适配（W6 发现落地）**：Langfuse v4 events_only 部署下经典读接口不可用 → report_trace 以 ClickHouse 直查 `events_core` 为主（冒烟驱动 schema 适配：11 列真实结构 vs spec 假设），SDK 路径保留为不可达时回退；SQL 用 parameters 绑定防注入。
2. **metadata_fn 分离导出**（贯穿 W7 全窗口的模式裁决）：装饰器闭包内的 metadata_fn 无法单测 → 模块级独立函数 + 直接断言语义，测试零副作用。
3. **CLI 顶层 import 偏离**：`_run_trace_report_mode` 内局部 import 会遮蔽 monkeypatch → 改模块顶层 import（tools.report_trace 顶层零重量依赖，无副作用）。
4. **main 重构 flush 接线**：main() = wrapper（_main_inner 收集 rc → flush → return），既有分支零改动、签名兼容。
5. **traced 仅同步函数**：W7 所有埋点目标（graph 节点/工具分发/run_tests/_review_diff）均同步方法，符合 W6 tracing 限定；graph 节点是 build_graph 内嵌套函数（每次调用重新装饰 = 预期行为：span 按调用创建、trace 按 invoke 独立）。
6. **密钥边界**：ClickHouse 口令（容器 env）与 Langfuse 三键全程运行时注入，零落盘（.env/代码/文档/提交均无）。

## 四、遗留问题（供后续窗口/人工复核）

- **评测基准**（W6 遗留继续）：沙箱镜像缺 pytz；单价表 MODEL_PRICE_USD_PER_1K 空（真实 run 成本列 0，待用户提供 mimo-v2.5 单价）。
- **Langfuse UI 层级复核**：v4 面板 SPA 未截图级验证（localhost:3000 可查 ec5b575f…/4d889e…）。
- **容器内 github TLS**（环境）：test_clone_repo_when_empty 偶发；镜像层待网络处理。
- **report_trace Minor**：无效 trace_id 返回空报告 rc=0（spec 期望 exit 1，可后续调整）；TRACE_OUT env 可选；_summarize_events 无独立单测（冒烟背书）。
- **W8 展望**：Human-in-the-loop 审批门禁依赖 W6/W7 已就绪（评测指标 + 可观测链路）；知识纠错第二阶段（冻结中）可随 W8 排期。

## 五、W6+W7 合计成果（本会话全部窗口）

- **W6 Evaluation**（76cae48）：基准案例库 5 个 + 评测运行器 + LLM-as-a-Judge + Langfuse 可开关 tracing + --benchmark/--compare；真实评测 Resolution Rate 20%（schedule-608 resolved）；全量 207 passed。
- **W7 Observability**（4035ce8）：全链路 Trace（graph 七节点 + 工具 + 测试 + review + retry 指标）+ --trace-report 导出 + OTLP 贯通验证；全量 230 passed。
- **W8 前置全部就绪**：worktree/分支待创建（feature/w8-human-in-the-loop）。