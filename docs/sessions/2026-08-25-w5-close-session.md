# 会话归档：2026-08-25 W5 收尾（含知识抽查与冻结决策）

> 归档时间：2026-08-25
> 分支：feature/w5-github-issue（已合并回 main，7fba02d）

## 本会话完成内容（时间线）

1. **模型依赖迁移**：全部模型依赖统一至 opencode_go（`opencode_go_api` Key + `https://opencode.ai/zen/go/v1` + `mimo-v2.5`），废弃火山 arkcode；两端点实测确认模型 ID 为连字符 `mimo-v2.5`（下划线版 401）。
2. **W5 实现收尾**：Task 1-6 全链路（gh CLI 封装 / issue 编排 / review 重试 / push 门禁 / --issue CLI）+ 重复运行幂等（sync_repository / checkout -B）。
3. **真实 dry-run 演示**（dbader/schedule#646）：修复与社区 PR #652 一致，Review PASS；能力边界 6 项实证。
4. **知识纠错扩展（W5 内）**：spec + plan + `tools/knowledge_audit.py` + `agent/correct.py` + `main.py --correct`；三次提取产物 2% 抽样验收：可靠 96.88% / 实用 70.98%（65 条疑似死数据）。
5. **人工复核**：重要内容（莱茵生命/瑕光/野鬃）被标死数据的根因 = 代号↔真名命名脱节 + 组织名粒度（结构化引用整名匹配）；判定为粗筛信号。
6. **冻结决策（用户拍板）**：纠错第二阶段（LLM 核查 / 删除真死数据 / 补全别名引用 / 重建索引 / --apply）**冻结，待 W8 Human-in-the-loop 门禁就绪后继续**；抽查产物（报告/代码/清单）全部保留。
7. **收尾**：合并 main（fast-forward，删分支与 worktree）、全量 154 项测试、roadmap/devlog/readme 更新。

## 关键决策

| 决策 | 结论 |
|------|------|
| 模型 | opencode_go + `mimo-v2.5`（连字符） |
| 知识纠错归属 | 并入 W5 实施；第一阶段=只读抽查验收（已完成）；第二阶段=写回纠错（冻结） |
| 纠错安全阀 | 默认 dry-run 报告；`--apply` 才写回；重建失败回滚（spec §4） |
| 死数据判定 | 结构化引用入度粗筛（非结论）；人工/LLM 复核定性；假阳性=命名 bridge 问题 |
| 下一步 | W6 Evaluation（知识抽查指标可复用；纠错第二阶段依赖 W8） |

## 环境备忘（下会话直接用）

- gh 已登录（SilhouetteQA，scopes read:org/repo/workflow）；`NO_PROXY=opencode.ai` 已在 .env（系统代理 Clash 直连问题）。
- 兄弟项目：`D:\AI project\Arknights LLM Wiki`（无 .venv，PATH python 可用）；kg 集成测试需 `ARKNIGHTS_WIKI_DIR`。
- 抽查产物：`output/correction/audit_report_*.md` + `audit_samples_*.jsonl`（本仓库，gitignore 忽略输出目录）。

## 下会话入口（W6）

1. 读 readme → devlog（本归档 + W5 收尾段）→ roadmap（W6 段入口备注）。
2. 创建 worktree `feature/w6-evaluation`（.worktrees/，gitignore）。
3. W6 任务：Issue 基准库（五类）/ 核心指标 Issue Resolution Rate / 版本对比机制 / LLM-as-judge；知识抽查（--correct）可作为知识质量指标源。
4. 冻结事项：纠错第二阶段需 HITL（W8）——若提前实施，先建人工审批门禁。