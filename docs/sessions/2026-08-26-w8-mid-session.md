# W8 + 评估修复 + Agent 修复 中途会话归档（2026-08-26）

> 会话状态：**已暂停（用户要求），随时可恢复**。恢复顺序见第四节。
> 本归档是恢复入口；各 worktree 内 `.superpowers/sdd/progress.md` 为任务级账本（含全部 Minor 台账），三份账本分别为：
> - `.worktrees/w8-human-in-the-loop/.superpowers/sdd/progress.md`
> - `.worktrees/w8-eval-fixes/.superpowers/sdd/progress.md`
> - `.worktrees/w8-agent-fixes/.superpowers/sdd/progress.md`

## 一、分支地图（当前 HEAD）

| worktree | 分支 | HEAD | 内容 |
|----------|------|------|------|
| `.worktrees/w8-human-in-the-loop` | `feature/w8-human-in-the-loop` | `5d79fef` | W8 主线：审批门禁代码 + spec/plan + LLM 供应商可选项 |
| `.worktrees/w8-eval-fixes` | `feature/w8-eval-fixes` | `40e4384` | 评估问题修复 E1-E10 全部完成（基于 W8 分支） |
| `.worktrees/w8-agent-fixes` | `feature/w8-agent-fixes` | `3892a65`（A3 待落） | Agent 侧修复 A1/A2 完成，A3 实施中（基于 eval-fixes） |

合并顺序（用户定）：main ← W8 ← eval-fixes ← agent-fixes，全部验证后才合并。

## 二、已完成工作

### W8 主线（feature/w8-human-in-the-loop）
- 完整 Superpowers 流程：brainstorming 五问五答 → spec（4ab7bac）→ plan（5657817，6 任务）→ SDD 实施。
- T1 审批单核心 / T2 审批执行（漂移检查+防重放）/ T3 产单串联+Reviewer 升级 / T4 CLI（--approve）——**4/4 双审查 Approved**。
- 计划矛盾裁决：T3 移除 push 字段波及 main.py 与 test_benchmark_runner.py，用户批准方案 A（最小连带）。
- 真实 dry-run：dbader/schedule#646 产单停等（独立 Reviewer 抓到无关改动+测试未并入——W5 盲区实证修复）。
- 真实演示：兄弟项目 Issue #1（知识纠错试点）干跑 31 迭代触顶 → **审批=拒绝**（删除判定依据不足 + 交付报告与实际 diff 不符）→ 防重放拦截实测通过。
- push 通道验证：openssl 后端下直连 github.com 与 ghfast.top 镜像均可达（HEAD 838ba4c）；批准阶段方案=临时 unset 全局 insteadOf 走直连。
- **LLM 供应商可选项**（用户要求）：`KA_LLM_PROVIDER=opencode_go|deepseek`；deepseek 供应商 key=`deepseek_api`（机器用户环境变量）、端点 https://api.deepseek.com、模型默认 **deepseek-v4-flash**（端点实测列表：deepseek-v4-flash / -vision-exp / deepseek-v4-pro；用户口述的 deepseek-4-flash 实际带 v）；工具调用冒烟通过。当前 `.env` 已切 `KA_LLM_PROVIDER=deepseek`（三个 worktree 同步）；任务全结束回切 opencode_go/mimo-v2.5（改 .env 一行即可，代码默认已保持 opencode_go）。

### 评估修复（feature/w8-eval-fixes，E1-E10 全 Approved，回归 326 passed / 14 skipped / 1 已知环境性失败）
- E1 case 级 setup_commands（P0-1b）｜E2 Dockerfile 预装 pytz（P0-1a）｜E3 基线预检 environment-error（P0-2，setup 前置重构）｜E4 test_pass 失败摘要（P1-3）｜E5 LLM timeout/max_retries（P1-4，SDK 原生，KA_LLM_TIMEOUT_S/KA_LLM_MAX_RETRIES）｜E6 resolution 双口径（P2-6，adjusted 排除 error/env_error 分母）｜E7 开放型收敛 task_type+默认迭代上限（P2-7）｜E8 case 级 span + 无效 trace_id exit 1（P2-8/9）｜E9 域案例判定模型（domain_checks：deletions/bridge 三态检查器，替代功能等价 Judge）｜E10 交付一致性核查（expected_actions vs diff 删除数，不改变 resolution）。

### Agent 修复（feature/w8-agent-fixes）
- A1 verify 强制兜底（6b398de）：迭代触顶强制测试验证（新节点 finalize_iter_limited，复用 _execute_verify_tests；verify 触顶路径既有断言保持）——Approved。
- A2 域操作规则注入（3892a65）：decide/review 提示词追加知识数据删除三条件（无来源锚点∧无事件参与∧无结构化引用，任一满足不得删除）+ 元数据/日志红线——Approved。
- A3 Reviewer 证据面扩展（工作树事实上下文，防幻觉论据）：**实施中，落账后待审查**。

## 三、关键用户裁决（本会话）

1. W8 设计五问：两段式 --approve / 独立 Reviewer / 批准执行·拒绝停止 / 通用审批单（预留 knowledge_apply）/ 演示三者都要。
2. T3 计划矛盾 → 方案 A（最小连带修复）。
3. eval-fixes 范围：P0+P1+P2 可做项全排；E9/E10 为演示后追加（域判定模型 + 交付一致性）。
4. LLM：加 deepseek 可选项；先切 deepseek 跑通 W8 收尾；任务结束回切 opencode_go/mimo 默认；deepseek 端点=官方 api.deepseek.com。
5. 兄弟项目 #1 审批=**拒绝**：删除判定问题（无事件参与但有多项来源的条目不应删除——粗筛信号非结论）；命名映射接线（build_entity_index 复用既有 identity_map）认可。
6. 后续流程：eval-fix 做完 → 开新分支修 agent → 同题新数据重测 W8 → 尝试到接受（批准）→ 完整远程修复（真实 PR）→ agent 与评估器都验证无问题再收尾。

## 四、恢复指引（下次会话按此顺序）

1. 读本归档 + 三份账本（含 Minor 台账，最终全分支审查时三选）。
2. **A3 收尾**：若 A3 已落账（见 agent-fixes 账本/`git log`）：生成审查包 → 任务审查 → Approved 后 ledger 登记。
3. **agent-fixes 全量回归**：`$env:RIPGREP_BIN=...; .venv\Scripts\python.exe -m pytest tests/ -q`（期望 330+ passed / 14 skipped / 1 已知环境性失败 test_sync_repository_fetch_and_reset）。
4. **重测（同题新数据）**：用户已同意用兄弟项目「知识纠错」同题、**换新数据**再跑。方案：在兄弟项目建新 Issue（或复用 #1 已拒绝的教训改写 scope——删除目标改为真正的《三山谈》类三条件全满足条目 + bridge 补全 + 交付清单），跑 `python main.py "qa-account/Arknights-LLM-wiki#N" --issue --max-iterations 30`（deepseek 模型下），整理审批单给用户 → **批准** → 临时 unset 全局 insteadOf 后 `--approve --decision approve` → 真实 PR → 远端验证。
   - 注意：批准前检查 Agent 是否还改元数据/日志（A2 红线）与交付清单（E10 口径）；A1 后触顶也应有 verify 证据。
5. **收尾（全部验证后）**：W8 分支 Task 6（D5 自动化可行性评估写入 roadmap/devlog + readme 状态行）→ 依次合并 W8 → eval-fixes → agent-fixes（从主仓库 main 执行，`git merge feature/xxx`，删分支与 worktree）→ 更新主仓库 docs/roadmap.md / docs/devlog.md / readme.md。
6. **遗留提醒**：知识纠错第二阶段（--apply 写回）已具备门禁通道（action_type=knowledge_apply 预留）待排期；评估 P2-5 单价表待用户提供 mimo-v2.5 单价；E3-a 建议真实 docker 运行验证 environment_error 计数。

## 五、环境备忘

- gh 已登录（qa-account，repo scope）；git 全局 insteadOf=ghfast.top 镜像（clone 通道）；沙箱内需 `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.sslBackend GIT_CONFIG_VALUE_0=openssl` 注入（schannel 沙箱限制）。
- `.env`（三个 worktree）：`KA_LLM_PROVIDER=deepseek` + `NO_PROXY=opencode.ai,api.deepseek.com`；deepseek_api 为机器用户环境变量（勿写入任何文件/提交）。
- 本会话沙箱：docker npipe 不可用（容器级验证留非沙箱）；`test_sync_repository_fetch_and_reset` 已知环境性失败。
- 兄弟项目克隆：`.worktrees/w8-human-in-the-loop/workspace/qa-account__Arknights-LLM-wiki`（#1 干跑工作树保留，含 agent 分析产物 data/*.txt 与 scripts/analyze_*.py 供人工查证）。

## 六、审批单与产物

- `output/approvals/dbader__schedule-646-20260826-154553.json`（pending，schedule 无写权限建议拒绝收口）
- `output/approvals/qa-account__Arknights-LLM-wiki-1-20260826-171612.json`（rejected + 原因，防重放已实测）
- eval 报告产物在 eval-fixes worktree `output/benchmark/`（如有）
- 审查包：各 worktree `.superpowers/sdd/reviews/*.txt`；简报/报告：`.superpowers/sdd/briefs|reports/`