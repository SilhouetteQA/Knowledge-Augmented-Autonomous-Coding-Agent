# W5 知识纠错——三次提取产物 2% 抽查验证 实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。
> 关联 spec：`docs/specs/2026-08-25-knowledge-correction.md`（已批准，并入 W5 窗口）

**Goal:** 对兄弟项目三次提取产物做**2% 分层抽样抽查**，逐条评估「来源可靠性」与「内容实用性」，产出可审计报告（可靠率 / 实用率 / 死数据清单），作为知识纠错第一阶段验收。

**问题分解（用户验收要求 → 小问题）：**

1. 三次提取产物长什么样、共多少条？（盘点 inventory）
2. 如何在保证代表性的前提下抽 ≥2%？（分层抽样）
3. 每条样本的来源是否可靠？（来源可靠性：line_range/source_records 与原文对照）
4. 每条样本是否被实际使用？（内容实用性：引用入度 / 死数据判定，《三山谈》类样例）
5. 抽查结果如何输出与复用？（报告 + CLI）
6. 如何证明抽查有效？（验收测试：fixture 单测 + 真实 2% 集成 + 全量回归）

**Data facts（实测，2026-08-25）：**

- Pass1 `v1_events/{main,side,special}/*.json`（106 章）：events[]（event/type/line_range/participants/location）、characters[]（name/type/role_in_chapter）、concepts[]（concept/line_range/discussion_summary/is_substantive）、factions[]/locations[]（line_range/description）——来源锚点 = `chapter + line_range`（对照 `stories/{category}/{chapter}.json` 行数）
- Pass3 `v3_seed_db_v3_final.json`：concepts 1199 / factions 234 / locations 245 / timeline_events 49 —— 来源锚点 = `source_records[]`（{source, source_detail, location, confidence}）；《三山谈》概念 source_records 为空 + 无事件参与（死数据样例）
- Pass2 `v3_seed_db_v2.json`：中间版（1 concept），并入 Pass3 同结构处理

## 文件结构（全部新增/修改于 worktree `feature/w5-github-issue`）

| 文件 | 动作 | 职责 |
|------|------|------|
| `tools/knowledge_audit.py` | 新增 | 盘点 inventory / 分层抽样 / 来源可靠性 / 内容实用性（规则层，无 LLM） |
| `agent/correct.py` | 新增 | `run_audit` 抽查编排 + 报告生成（markdown/jsonl） |
| `main.py` | 修改 | `--correct` 模式（第一阶段 = 抽查 audit；`--apply` 留作写回纠错） |
| `.env.example` | 修改 | KA_CORRECT_* 配置说明 |
| `tests/test_knowledge_audit.py` | 新增 | Task1-4 单元测试（fixture：小型 extractions + stories） |
| `tests/test_correct.py` | 新增 | 抽查编排 + 报告断言（fixture 端到端，零写回） |
| `tests/test_main.py` | 修改 | `--correct` 参数测试 |
| `tests/test_kg_audit_integration.py` | 新增 | 真实兄弟项目 2% 抽查（`@pytest.mark.kg`，env 条件跳过） |

## Task 1: 盘点 inventory（G1）

- [ ] 写失败测试：fixture `tests/fixtures/kg_mini/`（extractions/v1_events/main/二次呼吸-like 1 章 + v3_seed_db_v3_final-like 小样）→ `load_inventory` 断言：条目数正确、kind 分类正确（event/character/concept/faction/location/timeline）、字段抽取正确（name/source_records/line_range/chapter/category/origin_file）
- [ ] 实现 `tools/knowledge_audit.py`：`KnowledgeEntry` dataclass + `load_inventory(extractions_dir)` + `inventory_stats(entries)`
- [ ] 测试通过；提交 `feat(tools): knowledge_audit 产物盘点（三次提取 unified inventory）`

## Task 2: 分层抽样 ≥2%（G2）

- [ ] 写失败测试：`sample_entries(entries, ratio=0.02, seed=42)` 断言：样本数 ≥ ceil(0.02×N)；同 seed 可复现；按 kind 分层比例与总体偏差 ≤ 阈值；meta 返回 seed/ratio/total/sample_count
- [ ] 实现：分层比例抽样（random.Random(seed)）
- [ ] 测试通过；提交 `feat(tools): knowledge_audit 分层抽样（可复现 ≥2%）`

## Task 3: 来源可靠性（G3）

- [ ] 写失败测试（fixture stories 小型目录）：
  - Pass1 条目：chapter 对应 stories/{category}/{chapter}.json 存在与否；line_range 上界 ≤ 文件行数；无 line_range → unreliable
  - Pass3 条目：source_records 非空与否；confidence 字段
  - 两者皆无 → unreliable
- [ ] 实现 `check_reliability(entry, stories_dir) -> ReliabilityReport(has_source, issues, verdict)`
- [ ] 测试通过；提交 `feat(tools): knowledge_audit 来源可靠性检查（原文对照）`

## Task 4: 内容实用性（G4）

- [ ] 写失败测试：构建 `NameIndex`（name+aliases 规范化去《》/空格）；`check_utility(entry, index)` 断言：被 events.participants/event 文本/related_*/characters 引用计入度；入度 0 → dead；《三山谈》fixture 断言 dead
- [ ] 实现 `check_utility` + `NameIndex.build(entries)`
- [ ] 测试通过；提交 `feat(tools): knowledge_audit 内容实用性（引用入度/死数据）`

## Task 5: 抽查编排与报告（G5）

- [ ] 写失败测试：`agent/correct.py::run_audit(wiki_dir, ratio=0.02, seed=42, out_dir)` fixture 端到端：AuditReport（total/sample_count/reliability_rate/utility_rate/dead_list/样例明细）、报告文件生成、**对数据目录零写入断言**
- [ ] 实现 `run_audit` + `write_report`；`main.py --correct`（读 ARKNIGHTS_WIKI_DIR，--ratio/--seed/--out）；`.env.example` 配置项
- [ ] 测试通过；提交 `feat(agent): 知识抽查编排 --correct 模式与报告`

## Task 6: 验收测试与真实运行（G6）

- [ ] `@pytest.mark.kg` 集成测试：真实兄弟项目数据 2% 抽查（ARKNIGHTS_WIKI_DIR 配置时实跑；断言 sample_count ≥ ceil(0.02×total)）
- [ ] 真实运行：`python main.py --correct`（兄弟项目 2% 抽查）→ 产出报告 → 记录结论（可靠率/实用率/死数据清单，《三山谈》样例复核）
- [ ] 全量回归 `pytest -m "not docker"`（W5 既有 133 项 + 新增全绿）
- [ ] 更新 devlog（抽查结论 + 决策修订）；提交 `test(kg): 三次提取 2% 抽查验收测试与真实结论`

## 约束

- 规则层全部无 LLM 调用（确定性可单测）；LLM 一致性核查为第二阶段（apply 纠错）
- 安全：抽查阶段**零写回**（只读 + 输出报告到 output/）
- 测试命令：`.venv\Scripts\python.exe -m pytest tests/ -m "not docker"`