# W8 并行窗口：评估问题修复（eval-fixes）实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> 窗口：`feature/w8-eval-fixes`（基于 feature/w8-human-in-the-loop@22f4064，合并顺序 main ← W8 ← eval-fixes）
> 规格依据：`docs/sessions/2026-08-26-w6w7-close-session.md` 第三节问题清单（P0/P1/P2 已诊断，无需新 spec；范围经用户批准 2026-08-26）
> 跳过项：P2-10/11（被 W8 独立 Reviewer 覆盖，W8 真实干跑已实证）；P2-5（待用户提供 mimo-v2.5 单价）；P2-12（容器内 TLS 环境项，留档）

## Global Constraints

- Python 3.12+；中文注释；无 emoji；UTF-8；TDD red 先行；精准修改。
- 本窗口基于 W8 分支：`benchmark/runner.py` 已无 `push=False`（IssueTask 无 push 字段）；`agent/issue.py` 有 `approval_dir`/`repo_dir_name`；`agent/llm.py` 仍为 W1 形态（OpenAICompatClient，无超时/重试）。
- 沙箱限制（本会话）：docker npipe 不可用 → Dockerfile/容器级集成验证留档（单元 TDD 必须）；`test_sync_repository_fetch_and_reset` 为已知环境性失败。
- 全量回归基线（W8 后，本 worktree 应为）：240 passed / 14 skipped / 1 已知失败。
- 每个任务独立提交（Conventional Commits 中文）。

## 任务

### E1: case 级 setup_commands（P0-1b）
- `benchmark/loader.py`：case schema 加 `setup_commands: list[str] = []`（校验：必须 list[str]）；`BenchmarkCase` 加字段。
- `benchmark/runner.py`：每 case 在应用 patch 前执行 `setup_commands`（复用 run_command/沙箱上下文；失败 → case error 并记录）。
- 测试：loader 校验（非 list / 非 str 元素报错）、runner 执行顺序（fake run_command 记录）、失败 → error 标注。
- 目录因子：setup 命令在 repo_dir 执行。

### E2: Dockerfile 预装 pytz（P0-1a）
- `Dockerfile`：`pip install pytz` 加入镜像构建（schedule 时区测试群依赖）。
- 测试：无法容器验证；留档说明（docker 集成留非沙箱复测）；单元无。

### E3: 基线测试预检 environment-error（P0-2）
- `benchmark/runner.py`：每 case 先对**未修改的原仓库**（base 分支）跑 must_pass 测试；基线失败（依赖缺失等）→ case 标注 `status=environment_error`、reason 记失败摘要、**不再运行 Agent**（跳过该 case 的 Agent 执行），resolution=False。
- 基线通过 → 正常链路（Agent → patch → 测试 → judge）。
- 报告（report.py）：分类统计含 environment_error 计数；markdown 标注。
- 测试：fake run_tests 返回失败 → environment_error；基线通过 → 正常；基线失败时断言 run_issue_agent 未被调用。

### E4: test_pass 失败摘要（P1-3）
- `benchmark/runner.py` `_case_result`/采集：CaseResult 加 `test_error_summary: str`（run_tests 的 failures 摘要前 3 条 + 总数）。
- `benchmark/report.py`：markdown 明细列补失败摘要（截断）。
- 测试：fake TestResult 带 failures → 摘要入报告；JSON 字段存在。

### E5: LLM timeout/max_retries（P1-4）
- `agent/llm.py` OpenAICompatClient：`timeout`（默认 120s）与 `max_retries`（默认 2）参数 + 环境变量 `KA_LLM_TIMEOUT_S` / `KA_LLM_MAX_RETRIES`；chat 调用以 retry 循环包裹（指数退避 1s/2s，捕获 APITimeoutError/APIError 重试，超限抛原件）。
- 测试：fake client 抛 N 次后成功 → 重试生效；超限抛错；env 解析。
- 注意：opencode 端点为 OpenAI 兼容；openai SDK 自带重试（max_retries 参数）——优先用 SDK 参数（timeout=..., max_retries=... 传给 OpenAI()），不加自造循环（DRY）。测试断言构造时传参正确 + 超时异常透传。

### E6: resolution 分母口径（P2-6）
- `benchmark/report.py`：`resolution_rate` 保持全案例分母（raw）；新增 `resolution_rate_adjusted` = resolved / (total - errors - environment_error)；markdown 头部同时展示两口径并注释。
- runner 汇总同步。
- 测试：构造含 error 的 fake 报告 → 两口径数值正确。

### E7: 开放型任务收敛引导（P2-7）
- `benchmark/cases/*.json`：case 级 `max_iterations` 已存在；新增可选 `task_type: str`（bug/feature/test/refactor/domain 与 category 对应，缺省取 category）。
- `benchmark/runner.py`：开放型类别（test/refactor/domain）默认迭代上限映射 `open_ended_default`（20）vs bug/feature（30）；`agent/graph.py` decide 提示词追加收敛引导一句（小步验证、优先最小变更）。
- 测试：case 无 max_iterations 时按 task_type 取默认；task_type 非法报错（loader）。

### E8: case 级 span + 无效 trace_id exit 1（P2-8/9）
- `benchmark/runner.py`：每 case 包裹 `traced("evaluation.case", metadata={case_id, category})` span。
- `main.py --trace-report`：fetch_trace 无数据（steps 空）→ 打印「无数据」并 return 1（现 rc=0）。
- 测试：span 注册 metadata 正确（沿用 test_issue_agent 的 probe 模式）；report_trace 空摘要 → main rc=1。

### E9: 域案例判定模型（用户 2026-08-26 追加）
- 背景：兄弟项目 #1 真实演示暴露域知识任务（删除/bridge）无 gold 可比，LLM Judge「功能等价」模型不匹配。
- `benchmark/loader.py`：case schema 可选 `domain_check: str`（`"deletions"` / `"bridge"`，缺省无）；校验：非法值报错。
- `benchmark/runner.py` + 新 `benchmark/domain_checks.py`：
  - `deletions` 检查器：解析 agent diff 的删除文件集 → 对每个删除条目核验数据三字段（source_records/锚点 空 ∧ 事件参与 空 ∧ 结构化引用 0——实现为读 data 文件/索引的规则函数，判据常量集中）→ 全部满足 = PASS，任一不满足 = FAIL（附条目与字段证据）；diff 无删除 = SKIP。
  - `bridge` 检查器：先统计受影响规范名的引用入度（改前/改后，从工作树数据重建索引或读固定索引文件）→ 入度从 0 变 >0 = PASS；无变化 = FAIL（附证据）。
  - 判定结果并入 CaseResult（judge_verdict 用 DOMAIN_PASS/DOMAIN_FAIL/DOMAIN_SKIP，reason 含证据）；resolution = test_pass ∧ domain 判定 PASS。
- 测试：loader 校验；deletions 检查器（构造含违规删除的 fake diff → FAIL；合规 → PASS）；bridge 检查器（fake 入度变化 → PASS/FAIL）。

### E10: 交付一致性核查（用户 2026-08-26 追加）
- 背景：#1 演示 fix_report 声称删除 17 条而实际 5 条（deliverable 与 diff 不符）。
- `benchmark/loader.py`：case 可选 `expected_actions: dict`（如 `{"deletions": 5}`，缺省 {}）；校验类型。
- `benchmark/runner.py`：run 后按 expected_actions 核对 diff 实际删除文件数；相符 → 记录 consistency=True；不符 → False + 差异详情入 CaseResult.errors/新字段 `consistency_note`，resolution 不受影响但在报告标注「交付报告与 diff 不一致风险」。
- 测试：相符/不符两分支 + loader 校验。

## 收尾

- 全量回归：240 passed 基线 + 新用例；双轴审查；合并 main（在 W8 合并之后）；roadmap W8 遗留节 & devlog 更新；删除 worktree。