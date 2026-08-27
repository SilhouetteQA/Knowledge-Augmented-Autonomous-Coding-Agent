# W8 Agent 侧修复窗口（agent-fixes）实施计划

> 窗口：`feature/w8-agent-fixes`（基于 feature/w8-eval-fixes@40e4384；合并顺序 main ← W8 ← eval-fixes ← agent-fixes）
> 背景：兄弟项目 #1 真实演示暴露三类 Agent 侧问题（用户 2026-08-26 定序：eval-fix 做完 → 本窗口修 Agent → 同题新数据重测 W8 → 批准后真实远程修复 → 全部验证后收尾）
> 问题源：① verify_rounds=0（Agent 全程未声称完成，触顶即停，图从未强制跑测试）② 删除判定把「入度 0」粗筛当结论（无领域规则约束）③ Reviewer 只看 diff 文本，幻觉论据（identity_map 缺失误判）

## Global Constraints

- Python 3.12+；中文注释；无 emoji；UTF-8；TDD red 先行；精准修改（agent/graph.py 与 agent/issue.py 为本窗口范围，不动 benchmark/ 与 tools/ 其他模块）。
- 本窗口基于 eval-fixes 分支：llm.py 含供应商表（KA_LLM_PROVIDER，.env 已切 deepseek）；issue.py 有 approval_dir 产单 + repo_dir_name；runner 有域判定/一致性核查（本窗口不依赖，但重测时会被用到）。
- 全量回归基线（eval-fixes 收口值，见 eval 账本）：312+ passed / 少量 skipped / 1 已知环境性失败（test_sync_repository_fetch_and_reset）。
- 每个任务独立提交（Conventional Commits 中文）。

## 任务

### A1: verify 强制兜底（迭代触顶也要有测试证据）
- 现象：#1 演示 verify_rounds=0——Agent 31 轮探索从未声称完成，finalize_limited 直接收尾，仓库测试从未跑过。
- `agent/graph.py`：`run_agent_graph` 的迭代上限路径（stopped_by_limit / finalize_limited）：**在收尾前强制执行一次测试验证**（复用 verify 节点的测试执行逻辑——读 graph.py 现状，verify 节点如何调用 run_tests/沙箱；把该执行抽成可复用函数或直接在同路径调用）；结果记入 `AgentGraphResult`（verify_rounds 增加 1；test_results 追加本次结果——读 AgentGraphResult 现有字段语义，与 verify 路径一致）。
- 语义：触顶强制验证 ≠ 声称完成（不改变 final_answer/stopped_by_limit 语义；仅在结果里补测试证据，供 Reviewer/审批人使用）。
- 测试：fake 图运行触顶路径（MockLLMClient 脚本耗尽/上限内不声称完成）→ verify_rounds ≥ 1 且 test_results 非空；正常声称完成路径行为不变（既有断言保持）。
- 注意沙箱：强制验证的测试执行与 verify 节点同一执行器上下文（KA_EXECUTOR），不额外造容器语义。

### A2: 域操作判定规则注入（decide + review 提示词）
- 现象：#1 演示 Agent 把「入度 0」粗筛当删除充分条件（保全派驻/卫戍协议含来源也被删）；还改动了 `_meta.generated_at` 与 cost_log（无关副作用）。
- `agent/graph.py` `_decide_system` 追加「域操作规则」段（中文，追加不重写）：
  1. 知识数据（extractions/seed/index）删除/修改属于高风险操作：删除条目必须三条件齐备——无来源锚点（source_records/line_range/原文出现）∧ 无事件参与 ∧ 无结构化引用；**任一条件满足即不得删除，应保留并考虑补引用**；
  2. 不得修改元数据/日志等与任务无关的文件（如 `_meta.generated_at`、cost/日志文件）；
  3. 修改类操作（如命名 bridge）必须附变更清单（条目+依据），并运行仓库既有测试/构建脚本验证。
- `agent/issue.py` `REVIEW_PROMPT` 追加对应审查要点（第 5 点）：域数据操作的删除条目须有上述三条件证据可核；存在元数据/日志等无关改动应指出。
- 测试：_decide_system 输出含三规则关键字；REVIEW_PROMPT 含域审查要点关键字。

### A3: Reviewer 证据面扩展（审查上下文携带工作树状态）
- 现象：#1 演示 Reviewer 幻觉「identity_map.json 缺失」（实际存在且完整）——它只看 diff 文本。
- `agent/issue.py`：
  - 新增 `_review_context(repo_dir, base_branch) -> str`：收集工作树事实摘要——`git status --porcelain`（变更文件清单）、被删除文件清单（从 git diff --name-status 或 status 解析）、关键路径存在性（如 config/identity_map.json 是否存在）、工作树内新增文件清单（如 data/*.txt 报告）。全部宿主只读命令，失败时降级为简短提示（不抛错）。
  - `_review_diff` 增加 context 参数（默认 None 保持向后兼容），非 None 时拼入 user 消息（「工作树事实：...」段）。
  - `run_issue_agent` 两处调用（首轮与重试轮）都传 context。
- 测试：`_review_context` 在临时 git 仓库（删除/新增/修改混合）下输出含三类事实；`_review_diff` 传入 context 后 user 消息含该段；context 为空/失败降级不抛错。
- 注意：Reviewer 依然只读文本（不做工具调用），证据面 = 文本事实，避免引入多 Agent 框架。

### A4: 红线路径工具级拦截（重测 #2 暴露：提示词约束不够硬）
- 现象：#2 重测中 Agent 仍修改 `_meta.generated_at`（v3_seed_db_v2.json）与 `output/eval/cost_log.jsonl`（运行兄弟项目脚本的副作用）。
- `agent/issue.py` 新增 `_enforce_red_lines(repo_dir, base_branch) -> list[str]`：
  - 红线模式集（常量 `RED_LINE_PATTERNS`，可用环境变量 `KA_REDLINE_PATTERNS`（逗号分隔子串）覆盖）：`cost_log`、`generated_at`；
  - 以 `git diff --name-only <base>` 取变更文件，文件名含模式 → 从工作树还原该文件（`git checkout -- <path>`，宿主执行；run 内从不暂存，还原只影响工作树）；
  - 返回被还原文件清单（审计）；
- `run_issue_agent` 产单前调用：还原清单记入审批单（create_approval 新增可选参数 `red_line_reverts: list[str] = []`，既存字段不动）与 `_review_context` 摘要（Reviewer 可见「红线还原」事实）。
- 测试：真实 git 仓库（改动 cost_log.jsonl/generated_at 行 + 合法改动）→ 还原仅命中红线文件、合法改动保留、清单正确；env 覆盖模式；无红线时零动作。

### A5: 域任务收敛与产物纪律（重测 #2 暴露：31 轮烧在分析脚本上）
- `agent/graph.py` `_decide_system` 域操作规则追加两条：
  4. 禁止新增分析/验证脚本与中间产物到工作树（scripts/、output/ 下不留中间文件；分析用一次性命令（run_command 内联 python -c / 既有脚本）；必要验证并入仓库既有测试）；
  5. 交付收敛：迭代预算耗尽或任务完成时必须产出简短结论（做了什么/没做什么/为什么）；不存在满足条件的删除候选时必须显式说明并附分析摘要。
- `agent/issue.py` REVIEW_PROMPT 追加第 6 点：检查工作树残留中间产物（scripts/output 新增文件，证据面 A3 已提供）并指出。
- 测试：关键字断言（新增两条 + 第 6 点）。

## 收尾

- 全量回归；双轴审查；合并（在 eval-fixes 之后）；随后按用户流程：同题重测（复用 Issue #2，已拒绝一轮）→ 人工批准 → 真实远程修复（--approve 建 PR）→ 全部验证后 W8/eval-fixes/agent-fixes 依次合并收尾 + 文档（roadmap/devlog/readme）。