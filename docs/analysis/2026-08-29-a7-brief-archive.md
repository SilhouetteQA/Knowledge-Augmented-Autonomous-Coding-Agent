# A7 任务简报归档（task-A7-brief 补档，2026-08-29）

> 依据：w8-agent-fixes 账本 A7-2（"task-A7-brief.md 未抽取（审批以约束摘要为权威——恢复时补档）"）。
> A7 审批以当时会话内约束摘要为权威，本档为其可复现记录。

## A7：结论交付——审批单带 final_answer + 空 diff 结论审查

- **触发**：第三轮重测（deepseek-v4-flash，60 迭代）暴露"假完成早停"——final_answer 含 DSML 工具调用原文、空交付无核验结论可审。
- **用户批准**：A7 + 换模（模型切回 mimo-v2.5，--max-iterations 60）。
- **约束摘要**：
  1. 审批单新增 `final_answer` 字段（Agent 图 run 的最终结论，缺省空串）；
  2. 空 diff + final_answer 无实质内容 → Reviewer 硬短路 FAIL（"无代码变更"），不调 LLM；
  3. 空 diff + final_answer 有实质内容 → **结论审查模式**：Reviewer 核验结论的合理性/证据充分性/是否满足验收，输出 PASS/FAIL + 要点；
  4. 边界记录：空 diff + Reviewer PASS → approve 阶段 commit 必然 "nothing to commit"（语义正确：零变更无交付物），错误提示引导"无代码变更"。
- **实现落点**：`tools/approval.py`（create_approval final_answer 参数）+ `agent/issue.py`（_review_diff 空 diff 分支）+ 测试（test_approval/test_issue_agent）。
- **实战验证**：第四轮重测空 diff 阶段触发结论审查模式；R5（mimo）真实 PR #3 全链路带 final_answer 审批。
