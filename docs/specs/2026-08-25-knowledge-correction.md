# 知识库纠错（Knowledge Correction）设计规格

> 日期：2026-08-25
> 状态：草稿（待用户批准后进入 writing-plans 与实施）
> 关联：`docs/roadmap.md` W5/W6、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》领域知识增强定位、兄弟项目《Arknights LLM Wiki》`arknights_wiki/eval/`
> 说明：本规格是对兄弟项目评估器「只评回答、不评知识内容」短板的补齐——用本项目 Autonomous Coding Agent 能力对知识库内容本身做评估与修复。

## 1. 背景与目标

### 1.1 现状（实证调研，2026-08-25）

- 兄弟项目评测闭环：`benchmarks/arknights_bench`（问题集）→ `arknights_wiki/eval/runner.py` 跑 Agent 回答 → `judge_answer`（LLM-as-judge 六指标）+ `rule_metrics` 评分。
- **评估对象是回答（answer），不是知识库内容**：回答错时无法归因到「知识库里哪条知识是错的」，更无法修复。评测（回答层）与知识内容（数据层）之间存在缺口。
- 知识链路：`data/stories`（2160 个剧情原文，真相源）→ extraction pipeline（LLM 抽取）→ `data/extractions/*.json`（v1_events 按章节 / v3_seed）→ store 聚合去重（`entity_repository` / `seed.run_seed` / identity_map）→ SQLite db + faiss 向量索引 + lorebook → MCP 查询服务（`search_entities` / `query_relationship` 等）。
- 错误知识可能出现在：抽取 JSON（事实错、属性错、时间线错）、聚合去重（实体重复、关系悬空、schema 违例）、索引一致性（db 与抽取产物不一致）。

### 1.2 目标

用本项目的 Autonomous Coding Agent 能力，构建**知识内容级评估与修复**闭环：

```text
知识抽样/规则扫描 → 定位可疑条目 → 对照原文（stories）LLM 事实核查
→ 确认错误清单 → 修复（改 extractions 源头）→ 重建索引 → 回归验证
→ 纠错报告 + diff（dry-run 默认）→ 人工确认后 --apply 写回
```

与兄弟项目评估器互补：评估器管「答得对不对」（输出层），纠错器管「库里的知识对不对」（数据层）。

## 2. 验收标准

1. 规则化校验器在合成小 KG 上能发现：悬空关系、实体重复、schema 违例、时间线冲突（各类型有测试覆盖）。
2. LLM 事实核查：对规则命中项读取相关 stories 原文片段，输出 PASS/FAIL + 原文依据引用；误报能被排除。
3. dry-run：产出错误清单 + 修复 diff + 审查结论，**账面上零写回**（不修改任何知识数据文件）。
4. `--apply`：写回 extractions JSON → 重建索引 → 定向查询前后对比通过 → 兄弟项目核心测试通过；重建失败则中止且不写回。
5. 修复可追溯：每项修复含依据（issue id → 原文引用 → 修改字段前后值）。

## 3. 设计决策（brainstorming 已确认 2026-08-25）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | 纠错输出形态 | **默认 dry-run（报告 + diff + 审查结论），`--apply` 才写回** | 复用 W5 安全阀哲学；高风险写操作人工确认（AGENTS.md 八.4） |
| D2 | 错误发现方式 | **规则化一致性校验先行 + 可疑项抽样 LLM 对照原文核查** | 规则零 LLM 成本且可测；LLM 只核查命中项，v1 成本可控；全量对照（2160 文件）留 v2 |
| D3 | 修复写回目标 | **extractions JSON 源头 + 重建索引** | 源头修复惠及全部查询路径（db/向量索引/lorebook 由重建生成）；直接改 db 会被重建覆盖 |
| D4 | 核查执行形态 | **显式批量 LLM 核查步骤（非全自由 ReAct）** | 成本可控、结果可审计（逐条 PASS/FAIL + 依据）；自由探索型纠错留 v2 |
| D5 | 编排复用 | 复用 W5 的「前置/后置顺序编排 + LLM 步骤」模式，不新增图节点 | 核查/修复/重建/回归均为确定性流程，无需 decide 循环；与 issue.py 同构 |
| D6 | 窗口归属 | 建议并入 **W6 Evaluation**（知识质量属评估范畴）作领域扩展，或独立窗口 | 待用户批准时一并定 |

## 4. 架构与组件

```
tools/knowledge_audit.py      # 新增：加载 extractions，规则化一致性校验（无 LLM）
tools/knowledge_corrector.py  # 新增：LLM 事实核查 + 修复 patch 生成/应用 + 重建触发 + 回归
agent/correct.py              # 新增：run_knowledge_correction 编排（dry-run / --apply 门禁）
main.py                       # 修改：--correct 模式 + --apply 开关
tools/knowledge_client.py     # 不变：search_knowledge 用于修复前/后定向查询回归
tests/test_knowledge_audit.py # 新增：规则命中测试（合成 KG fixture）
tests/test_knowledge_corrector.py # 新增：核查/修复/dry-run/apply/重建失败路径
tests/test_main.py            # 修改：--correct 模式测试
.env.example                  # 修改：纠错配置项说明
```

### 4.1 规则化校验（seam 1：knowledge_audit）

```python
@dataclass
class KnowledgeIssue:
    id: str                # file:path:rule 定位
    rule: str              # dangling_ref / duplicate_entity / schema_violation / timeline_conflict / source_missing
    severity: str          # error / warning
    location: str          # 文件 + 条目定位（如 v1_events/main/二次呼吸.json::event[3]）
    description: str
    detail: dict           # 结构化上下文（引用实体名、字段、期望值等）
    ref_sources: list[str] # 关联的 stories 原文文件（供核查读取）

def load_knowledge(dir: str) -> list[dict]          # 遍历 data/extractions/*.json 解析条目
def check_consistency(entries: list[dict]) -> list[KnowledgeIssue]
```

规则集（R1-R5，首版）：

| 规则 | 检查内容 | 判定 |
|------|----------|------|
| R1 dangling_ref | 事件 participants / 地点 / 关系端点引用不存在的实体 | 引用名在全量实体名集合（含别名）中缺失 |
| R2 duplicate_entity | 同名/别名高度相似实体跨章节重复建档（不指向同一 identity） | 名字规范化后相等，或相似度 ≥ 阈值（参考兄弟项目模糊匹配 ≤0.6 思路反向用） |
| R3 schema_violation | 必填字段缺失 / 类型错误（实体无 name；事件无 chapter；empty 列表） | 结构校验 |
| R4 timeline_conflict | 同一实体 timeline 条目冲突（出生年份多个/事件时序矛盾） | 同实体年份字段不一致 |
| R5 source_missing | extractions 事件章节在 data/stories 对应文件缺失或为空 | 文件系统对照 |

全部规则无 LLM 调用，确定性、可单测。

### 4.2 LLM 事实核查（seam 2：knowledge_corrector）

```python
@dataclass
class VerifyResult:
    issue_id: str
    verdict: str            # CONFIRMED / FALSE_POSITIVE / UNCERTAIN
    evidence: str           # 原文引用（stories 片段）
    suggested_fix: dict | None  # 建议修改字段与值（CONFIRMED 时）

def verify_issue(llm, issue: KnowledgeIssue, stories_dir: str) -> VerifyResult
def apply_fix(data_dir: str, fix: dict) -> bool      # 写回 extractions JSON（原子：临时文件 + 替换）
def rebuild_and_verify(wiki_dir: str, verify_query: str) -> tuple[bool, str]  # 重建 + search_knowledge 回归
```

- 核查输入：`KnowledgeIssue.detail + ref_sources`（stories 原文片段，优先按章节/实体名定位，截断 1500 字符/文件）+ 第 3 项：现库查询（`search_knowledge`，可选）。
- 提示词要求：结论首行 `CONFIRMED/FALSE_POSITIVE/UNCERTAIN`，后附原文引用与建议修复值；`UNCERTAIN` 不修复、留给人工。
- 成本控制：`KA_CORRECT_AUDIT_LIMIT`（默认 20 条/轮）+ 仅核查规则命中项。

### 4.3 编排（seam 3：agent/correct.py）

```python
@dataclass
class CorrectionTask:
    wiki_dir: str            # 兄弟项目根目录
    apply: bool = False      # dry-run 默认；True 才写回
    audit_limit: int = 20
    max_verify: int = 40     # 单批核查上限

@dataclass
class CorrectionResult:
    issues: list[KnowledgeIssue]
    verified: list[VerifyResult]
    fixes: list[dict]        # 待应用/已应用修复
    diff: str                # 修复 unified diff（演示/审计用）
    review: str              # LLM 审查结论（PASS/FAIL 首行，复用 W5 REVIEW_PROMPT 思路）
    applied: bool
    rebuild_ok: bool | None
    regression: dict | None  # 修复前后定向查询对比

def run_knowledge_correction(task, llm) -> CorrectionResult
```

流程：

1. `load_knowledge` → `check_consistency` → 规则问题清单
2. 取 `audit_limit` 条 error 级问题 → 逐条 `verify_issue`（LLM，采样按 severity + 规则类型）
3. 生成修复 patch：`CONFIRMED` 且 `suggested_fix` 有值 → `{"file":..., "path":..., "old":..., "new":...}`
4. dry-run：`diff = 按 patch 生成的统一 diff 文本` → LLM `review`（审查修复是否恰当，PASS/FAIL）→ 输出报告，**不写回**
5. `apply=True` 且 review PASS：
   - `rebuild_and_verify(wiki_dir, verify_doc)`：备份 → `apply_fix` 逐条 → 调用兄弟项目重建（候选：`seed.run_seed` / `scripts/build_entity_index.py` / `scripts/build_agent_index.py`，实施时先探测确切命令）→ `search_knowledge` 对修复条目定向查询 → 前后对比写入 regression
   - 重建失败：**回滚备份、中止**（保持 dry-run 级报告并明示原因）
6. 返回报告（控制台打印 + `output/correction_<ts>/report.md`）

### 4.4 main.py

```
python main.py --correct [--wiki-dir D:\AI project\Arknights LLM Wiki] [--apply] [--audit-limit 20]
```

- `--correct` 读 `ARKNIGHTS_WIKI_DIR`（缺省同 knowledge_client）；`--apply` 或 `KA_CORRECT_APPLY=1` 才写回。

## 5. 测试策略（TDD，seam = audit 函数 / verify_issue / run_knowledge_correction）

1. `test_knowledge_audit.py`：合成 KG fixture（tests/fixtures/kg_mini/：2 章节 extractions + 1 个含错误的小 KG）——每条规则 1-2 个命中 - 未命中对照用例
2. `test_knowledge_corrector.py`：Mock LLM 脚本断言：CONFIRMED → patch 生成；FALSE_POSITIVE → 无 patch；UNCERTAIN → 跳过；`apply_fix` 原子写回与损坏输入拒绝；**dry-run 目录零变更断言**；--apply 路径写回断言；`rebuild_and_verify` 失败 → 不写回 + 回滚断言（monkeypatch 重建命令）
3. `test_main.py`：`--correct` 参数解析与 dry-run 输出断言
4. 集成（可选，`@pytest.mark.kg`）：真实兄弟项目副本小样本（audit_limit=5）跑通全流程（env 开关，条件跳过）
5. 回归：本项目非 docker 全量测试保持全绿

## 6. 配置

| env | 默认 | 说明 |
|-----|------|------|
| `ARKNIGHTS_WIKI_DIR` | 无（必配） | 兄弟项目根目录（复用现有变量） |
| `KA_CORRECT_APPLY` | `0` | =1 允许写回（同 `--apply`） |
| `KA_CORRECT_AUDIT_LIMIT` | `20` | 单批 LLM 核查上限 |
| `KA_CORRECT_OUT` | `output/correction` | 报告输出目录 |
| `KA_EXECUTOR` | `local` | 重建命令执行器（沿用；docker 模式需兄弟项目 venv 可挂载） |

## 7. 错误处理与返回语义

| 场景 | 行为 |
|------|------|
| extractions 目录不存在/为空 | ToolError 结构化报错，不进入核查 |
| LLM 核查调用失败（网络/超时） | 单条记为 UNCERTAIN + 原因，流程继续（不中断整批） |
| 重建命令不存在/失败 | 中止 --apply、回滚已写 patch、报告保留 dry-run 形态 |
| 修复后回归查询异常 | 标记 regression 失败并列入报告（人工复核），不回滚（已重建） |
| dry-run 模式 | 零文件写回（含备份文件——备份仅存在于内存/临时目录） |

## 8. 非目标（YAGNI，第一版不做）

- 不做自由 ReAct 式自主纠错（v2；先显式批量核查保证可审计）
- 不做全量 原文↔抽取 逐条对照（2160 文件成本过高，v2 精修轮）
- 不做知识库写入的分布式锁/并发（单机单批）
- 不做对 db/faiss/lorebook 的直接修改（只改源头 + 重建）
- 不做自动推送 PR 到兄弟项目（产出报告 + diff，应用方式由用户定：--apply 本地写回或人工提 PR）

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 误判修复（把正确的知识改错） | 双保险：LLM 核查必须附原文引用；修复后搜索回归 + 人工（dry-run 报告）确认；UNCERTAIN 不修 |
| 兄弟项目重建入口不确定 | 实施第一步先探测确切命令并写测试替身；重建失败自动回滚 |
| 大 KG 核查成本 | 规则先行过滤 + audit_limit 上限 + 仅 error 级条目 |
| 写坏 extractions 文件（json 损坏） | apply_fix 原子替换（临时文件 + os.replace）+ 应用前整体备份；损坏输入拒绝 |
| 与兄弟项目数据格式耦合 | 解析层集中（load_knowledge），格式变更只改一处；fixture 与真实格式双测 |

## 10. 依赖

- 本项目新增依赖：无（复用 LLMClient / file_tools / run_command / search_knowledge）
- 兄弟项目侧：`store.seed.run_seed` / `scripts/build_entity_index.py` / `scripts/build_agent_index.py`（重建触发，argv 探测用法）；其 venv python 或 PATH python

## 11. 相关与展望

- 与评估器联动（v2 提议）：评估器报告某类别回答错误率高 → 引导纠错器优先审计该类别知识 → 修复后重跑评估器，形成「回答错误 → 知识纠错 → 再评估」的闭环证据链。
- 与本项目 W6（Evaluation）合并验收：纠错一轮后，用兄弟项目 benchmark 测回答指标变化。