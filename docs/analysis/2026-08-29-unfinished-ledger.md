# 未完结项全量台账（2026-08-29，两轮实测 + 历轮遗留汇总）

> 用途：项目审查与后续计划的输入。状态：已修 / 待修（小）/ 设计决策 / 排期项 / 用户项。

## A. 本轮 + 第一轮实测暴露（产品代码）

| # | 项 | 状态 | 说明 |
|---|----|------|------|
| A1 | 环境声明提示词 | ✅ 已修 | 第一轮 F1 |
| A2 | edit_file 局部编辑工具 | ✅ 已修 | F2，mdit#415 实战验证 |
| A3 | run_command 破坏性 git 拦截 | ✅ 已修 | F3 |
| A4 | Reviewer 测试证据面 | ✅ 已修 | F4 + F4b（异常形态） |
| A5 | G1 LLM 异常优雅降级 | ✅ 已修 | arknights v1 崩溃 |
| A6 | G3 KA_ISSUE_SETUP_COMMANDS | ✅ 已修 | dateutil src 布局 |
| A7 | G8 reasoning_content 回传 | ✅ 已修 | deepseek thinking 400 |
| A8 | G4 verify 空洞通过（total=0 全绿即 pass） | 待设计决策 | 0 收集 = 无证据；一刀切判失败会伤"无测试仓库"，需语义确认 |
| A9 | G5 上下文膨胀（50 轮无压缩 → 超时/慢） | 待设计 | 架构项：历史压缩/摘要策略，本轮唯一未修的架构级缺口 |
| A10 | G6 run_tests 超时 | ✅ 已修 | KA_TEST_TIMEOUT_S |
| A11 | G7 容器 root 文件残留清理 | 部分 | root 容器清理可工作；可考虑 destroy 钩子自动化（小） |
| A12 | 模型预算纪律 | 部分：执行层 nudge 已落地（过 2/3 无写操作注入提醒 + 单测）；真实 run 复证待做 |
| A13 | 兄弟项目测试容器内 2 errors | 待查 | 容器内基线不绿（0 passed/2 error），回归验收语义受损 |

## B. 历轮 Minor 台账（已清偿 + 剩余）

| # | 项 | 状态 |
|---|----|------|
| B1 | approval_id 同秒撞 id | ✅ 随机后缀 |
| B2 | approval_id docstring runid→时间戳 | ✅ |
| B3 | load 校验 approval_id 字符集 | ✅ |
| B4 | _run 私有跨模块导入 | ✅ run_host |
| B5 | E5-1 超时透传测试 | ✅ |
| B6 | 报告 ✓/✗ 擦边 emoji | ✅ 中文 |
| B7 | approve push 成功建 PR 失败的重放窗口 | ✅ 已修：落 approved 终态（pr_url=None+注记），拒绝重放 |
| B8 | E1-M2 case.setup_commands 60s 默认不可配 | ✅ 已修：KA_SETUP_TIMEOUT_S |
| B9 | E9 runner 对域 case 读 gold | ✅ 已修：域 case 跳过 gold 读取 |
| B10 | E4-1/2 md 摘要转义与测试 | ✅ 已修：失败摘要换行转义 + 一致列测试更新 |
| B11 | E6-1 --compare 不回填 adjusted | 待设计（口径统一） |
| B12 | A7-2 task-A7-brief.md 档案补全 | 待做（文档） |
| B13 | E3-a docker setup environment_error 真实验证 | 部分（本轮 docker 实测覆盖主链路；setup 失败形态待专项） |
| B14 | mount_sync 测试 flake（本轮 3 连败后自愈） | 待查（环境类，观测） |

## C. 评估遗留（P0-P2 清单余量）

| # | 项 | 状态 |
|---|----|------|
| C1 | P2-5 单价表 MODEL_PRICE_USD_PER_1K | 用户项：待提供 mimo/deepseek 单价 |
| C2 | domain 类 benchmark 案例缺位 | 排期：补 Arknights 域 case（可复用 #4 题面） |
| C3 | 评测口径 adjusted 回填 --compare | 同 B11 |

## D. 窗口级大项

| # | 项 | 状态 |
|---|----|------|
| D1 | D5 条件自动放行档（Reviewer PASS + 红线 0 + 测试绿 + 非删除型） | 排期：小任务，判定字段齐备 |
| D2 | 知识纠错第二阶段 --apply（action_type=knowledge_apply） | 冻结中：等 D1 落地后解冻排期 |
| D3 | PR #3 审阅/合并 + 剩余候选续跑 | 用户项：SilhouetteQA 决定；#4 运行结果出来后一并处理 |
| D4 | 模型选型：mimo 周限 / deepseek thinking+预算纪律问题 | 待评估：G8 修复后 deepseek 复测；预算纪律执行层强制是前置 |
