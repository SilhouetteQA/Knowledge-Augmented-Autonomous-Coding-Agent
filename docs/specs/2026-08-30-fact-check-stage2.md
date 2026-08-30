# Spec：知识纠错第二阶段——死数据事实核查报告（2026-08-30）

> 状态：范围已经用户批准（2026-08-30 逐项裁决："先做事实核查报告"）；实施待下会话 TDD。
> 上游：W5 知识抽查（--correct，产出 65 条疑似死数据清单）+ W5 人工复核（代号↔真名 bridge 脱节、组织名粒度两类假阳性）+ D2 解冻决策。

## 1. 目标

对审计报告标记的疑似死数据条目，用 LLM 对照兄弟项目 stories 原文逐条核验，产出三态判定报告：

- **CONFIRMED**：原文确无该条目的实质内容支撑（真死数据，删除候选）；
- **FALSE_POSITIVE**：原文有实质内容但未被结构化引用（假阳性，bridge/引用补全候选）；
- **UNCERTAIN**：证据不足或名称歧义（转人工）。

**零写回**：本阶段只产出报告，不修改兄弟项目任何数据（写回由 --apply / knowledge_apply 审批通道承接，另行排期）。

## 2. 输入

- `output/correction/audit_report_*.jsonl` 的死数据清单（65 条：concept 37 / faction 17 / location 8 / character 3），或 `--wiki-dir` 现场重跑 audit 取 dead 集；
- 人工复核补充的 bridge 假阳性名单（瑕光/野鬃/焰尾/莱茵生命等，作为 FALSE_POSITIVE 先验提示而非结论）；
- stories 原文（`stories/{category}/{chapter}/*.json`）与结构化引用（v1_events / v3_seed / entity_source_map）。

## 3. 处理流程

1. 载入死数据条目（沿用 knowledge_audit 的统一条目模型与名称规范化）；
2. 精确字符串预检（复用 domain_checks 的精确匹配思路）：原文/结构化引用命中的直接 FALSE_POSITIVE（省 LLM）；
3. 逐条 LLM 核查：prompt 携带条目全字段 + 该条目相关章节原文摘录（按 source/章节线索定位），要求输出三态 + 中文依据 + 引用位置；system prompt 含注入防御（与 judge.py 同款）；
4. 汇总报告：md（三态分组 + 逐条依据）+ jsonl（机器可读，供 --apply 阶段消费）。

## 4. 接口草图（实施时按 TDD 打磨）

- `tools/knowledge_audit.py` 或新 `tools/fact_check.py`：条目→证据上下文构建（纯规则）；
- `agent/correct.py` 扩展：`run_fact_check(wiki_dir, llm, ...)` 编排；
- `main.py --fact-check`（复用 --wiki-dir/--audit-out 系参数；LLM 必需）；
- 埋点：fact_check 条目级 span（复用 tracing）。

## 5. 验收标准

1. 65 条全部产出三态判定，无崩溃（畸形 schema 沿用 IM-16 防御思路）；
2. 报告含逐条依据与原文位置引用，人工可抽验；
3. 零写回（兄弟项目 git status 干净）；
4. 抽样人工复核：FALSE_POSITIVE 命中 bridge 假阳性名单的条目比例作为质量信号记录 devlog。

## 6. 边界与不做

- 不修改兄弟项目数据（无 --apply）；
- 不做全库 11072 条核查（仅死数据清单 + 用户指定追加项）；
- LLM 供应商用当前默认（mimo-v2.5），成本可控（65 条 × 短上下文）。
