# W8 真实远程修复里程碑 + 会话总结（2026-08-28）

> 状态：**真实 PR 已交付**（https://github.com/qa-account/Arknights-LLM-wiki/pull/3）——W8 Human-in-the-loop 全链路「干跑产单 → 人工审批 → 真实远程修复」首次完整走通。本归档为**新会话继续优化的入口**。
> 前序归档：`docs/sessions/2026-08-26-w8-mid-session.md`（分支地图/裁决/环境备忘仍有效）。
> 任务级账本（各 worktree `.superpowers/sdd/progress.md`）：W8 主线 / eval-fixes / agent-fixes 三份，含全部任务状态与 Minor 台账。

## 一、本次里程碑（2026-08-28）

1. **真实远程修复闭环**：兄弟项目 Issue #2（知识纠错试点二）→ Agent 干跑（mimo，61 迭代触顶）→ 真实删除 2 条已核验死数据（玉门望烽节、阮先生（画家）：seed JSON 条目 + md 双删）→ Reviewer 审查（FAIL：交付不完整）→ **人工批准** → commit → 直连 push（沙箱 sh 管道限制下手工补完，已如实记录）→ **真实 PR #3**（OPEN，main 为目标分支，3 文件纯删除）→ 远端核验通过。
2. **修复链在实战中逐项验证**：
   - A1 verify 兜底：每轮均 verify_rounds≥1；
   - A3 Reviewer 证据面：审查引用工作树事实，无幻觉论据（未再误判 identity_map）；
   - A4 红线机械拦截：运行期 generated_at/cost_log 副作用被还原并记入审批单（red_line_reverts=2）；
   - A5/A6 产物与预算纪律：无分析脚本残留、零红线违规；
   - A7 结论交付：空 diff + 结论审查模式上线（第四轮起试验）。
3. **模型实验结论（重要，供后续选型）**：deepseek-v4-flash @ opencode_go 端点在**多轮**工具调用下会把工具调用以 DSML 文本返回（final_answer 含 `<DSML> invoke` 原文）→ 第 5 迭代假完成早停；单轮冒烟正常。**判定：mimo-v2.5 为稳定默认（.env 已恢复）**；deepseek 实验数据与证据留存（round3/4 审批单 final_answer）。
4. **第五轮迭代环境坑**：verify 后克隆内残留 `.pytest_tmp/`（0o700 沙箱锁）→ 下次 run 的 sync clean -fd 崩溃（现场手工清理 1 次，danger-full-access）。非产品缺陷（W1 已知沙箱行为），但新会话注意：**每轮 run 前检查克隆根 `.pytest_tmp/`/`.venv_tmp/`**。

## 二、当前分支状态

| worktree | 分支 | HEAD | 说明 |
|----------|------|------|------|
| w8-human-in-the-loop | feature/w8-human-in-the-loop | 5d79fef | W8 主线（审批门禁 + LLM 供应商选项） |
| w8-eval-fixes | feature/w8-eval-fixes | 40e4384 | E1-E10 全 Approved（评估修复） |
| w8-agent-fixes | feature/w8-agent-fixes | a886bbe | A1-A7 全 Approved（Agent 修复）+ 审计产物/计划文档 |

合并顺序（用户定）：main ← W8 ← eval-fixes ← agent-fixes，全部验证后再合并。

## 三、新会话待办（继续优化）

1. **PR #3 收尾**：审阅/合并 PR（qa-account 决定）；剩余 3 条候选（万顷研究/忘水坪（取江峰）/神农（历史人物））核验结论 + bridge 生效验证 + 回归文档 → 新 issue 续跑（建议题面带「前序已删除 2 条」上下文并复用 v4 候选论证）。
2. **W8 Task 6（未做）**：D5 完全自动化可行性评估（素材：五轮真实运行数据——正确执行率/红线违反率/A4 拦截次数/审批裁决记录）+ roadmap W8 [x] 完成记录 + devlog W8 全量条目 + readme 状态行。
3. **合并收尾**：全量回归（agent-fixes 基线 345 passed，A6/A7 后 +11 预计 ≈356）→ 双轴全分支审查（含三份账本 Minor 台账三选）→ 依次合并三分支 → 删 worktree。
4. **知识纠错第二阶段解冻**：`--apply` 写回经审批单通道（action_type=knowledge_apply 预留），排期待定。
5. **评估遗留**：P2-5 单价表（待用户提供 mimo-v2.5 单价）；E3-a 真实 docker 环境验证（environment_error 计数）；A7-2 补 task-A7-brief.md 档（追责/复现用）。
6. **环境备忘**：gh 登录（qa-account, repo scope）；git 镜像 insteadOf=ghfast.top（clone 用，push 需临时 unset 走直连——**沙箱无法写 ~/.gitconfig**，直连 push 需 danger-full-access）；沙箱需 GIT_CONFIG_* sslBackend=openssl 注入；`.pytest_tmp` 坑见一-4；模型 = mimo-v2.5（default）。
7. **审批单/产物位置**：output/approvals/ 共 4 份（schedule-646 pending 待收口建议拒绝、#1 rejected、#2 R2/R3/R4 rejected、#2 R5 approved+pr_url）；全量审计产物 W8 worktree output/correction_full/（11072 条，死数据 3199，死+不可靠 concept 1058，v3_seed 死+不可靠 134——新候选池）。

## 四、关键规则与记录

- 所有外部写操作（gh issue/PR/push）均经用户确认；PR 已如实携带 FAIL 审查结论与人工批准注记。
- 空 diff + Reviewer PASS 的审批尾部边界：approve 时 commit 必然 'nothing to commit'（语义正确），devlog 记录。
- push 手工补完偏差已记录于审批单 decision_comment（沙箱环境限制非流程缺陷）。
- deepseek 实验与 DSML 证据在 round3/4 审批单与本题归档中，避免后续会话重复实验。