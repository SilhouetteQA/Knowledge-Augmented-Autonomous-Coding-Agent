# 未完结项全量台账 v2（2026-08-30，全量代码审查修复日汇总）

> 取代 2026-08-29 版（原文件保留作历史；其 12 项过时状态在本文件第二节对账）。
> 来源：2026-08-30 全量代码审查（2C/18IM/35Min）+ 逐项用户裁决（一项项询问确认）+ 历轮遗留。
> 当日战果：产品代码 21 项修复 + 历轮 4 项处置 + C 组用户项 5 项落地，7 个 fix/feat commits，全量回归 440 passed / 4 skipped / 0 failed。

## 一、本轮审查发现（全部清偿）

| # | 项 | 处置 | commit |
|---|----|------|--------|
| CR-1 | untracked 新文件绕过审批直达 PR | ✅ worktree_full_diff 三处共用（产单/Reviewer/漂移检查），untracked 内容入指纹 | ed4f908 |
| CR-2 | --issue 默认 local 执行器威胁模型矛盾 | ✅ 未显式配置时默认 docker，显式 local 放行并警示 | 8bf3ea7 |
| IM-1 | auto-approve 不核验测试证据 | ✅ 审批单 test_summary 字段 + total>0 全绿硬条件 | 8f99bc0 |
| IM-2 | 红线内容通道漏删除行 | ✅ +/- 行同查 | 8f99bc0 |
| IM-3 | 测试基础设施失败静默 | ✅ 证据面「测试未能运行」事实 + test_summary 异常形态 | 8f99bc0 |
| IM-4 | --approve 裸路径 makedirs 崩溃 | ✅ 空目录回退 + abspath 归一 | 8f99bc0 |
| IM-5 | run_command 超时非硬上界 | ✅ Windows 收尾硬预算（_POST_KILL_GRACE_S）；POSIX 孤儿树→台账 | 839f5f0 |
| IM-6 | run_tests 忽略 pytest 退出码 | ✅ 双执行器无汇总行→ToolError；exit 5 保持 G4 | 839f5f0 |
| IM-7 | docker exec 300s 一刀切 | ✅ _cli_budget 自适应（max(300, t+60)）+ exec 返回 ToolError 契约 | 839f5f0 |
| IM-8 | 嵌套沙箱原始字符串比较 | ✅ abspath 归一比较 | 839f5f0 |
| IM-9 | run_tests workspace_root=None 崩溃 | ✅ 结构化 ToolError | 839f5f0 |
| IM-10 | edit_file Windows 行尾双向问题 | ✅ 匹配归一 + 写回按原文件行尾物化 | 3e380c0 |
| IM-11 | 最小循环无工具防护 | ✅ 与 graph 同型守卫 | 3e380c0 |
| IM-12 | 畸形 tool arguments 终局崩溃 | ✅ 哨兵保留原文 + dispatch 结构化错误回注重试 | 3e380c0 |
| IM-13 | git 拦截绕过形态未记录 | ✅ docstring 补记 4 实测形态（取舍不变） | 3e380c0 |
| IM-14 | report_trace SDK 双计 | ✅ 同口径 max-min | f181ac7 |
| IM-15 | code_parser 不识 async def | ✅ AsyncFunctionDef 同收集 | f181ac7 |
| IM-16 | knowledge_audit 畸形 line_range 崩溃 | ✅ 类型校验降级无锚点 | f181ac7 |
| IM-17 | generation 不写对话内容 | ✅ 有意取舍注记（llm.py + .env.example；用户未答按保守默认） | f181ac7 |
| IM-18 | list_files stat 裸崩 | ✅ 遍历容错跳过 | 9599f2b |
| BM-1 | 域判定器 deletions 无基线（引用剥离可绕过） | ✅ HEAD 基线对比 + 剩余数据为空按基线核验 | 9599f2b |
| BM-2 | domain case 被要求 gold 文件 | ✅ 域 case 放宽校验 | 9599f2b |

## 二、历轮项处置

| # | 项 | 处置 |
|---|----|------|
| 容器硬化（M20） | ✅ --cap-drop ALL + no-new-privileges（read-only 有意不做：破坏创建期装依赖） | 5518f5d |
| M23 红线常量分层倒置 | ✅ 下沉 tools/redlines.py，approval 反向依赖消除 | 5518f5d |
| B13 docker setup 失败形态验证 | ✅ scripts/b13_probe.py docker 实跑：setup 失败→error(exit=3 透传)、基线失败→environment_error(含失败摘要)；**探针顺带暴露并修复 runner 相对路径嵌套缺陷**（repo_dir 绝对化） | 5518f5d |
| B14 mount_sync flake | 观测中（本轮未复现） |
| POSIX 孤儿进程树（IM-5 拆出） | 记录：LocalExecutor POSIX 分支只 kill 顶层不杀树（start_new_session+killpg 缺）；当前无 Linux/macOS 宿主部署场景，风险 0；部署 Linux 前修复 |
| A8/G4 | ✅ 已决策（total=0 不翻转 pass + Reviewer 显式告知），runner 基线口径独立 |
| A9/G5 上下文压缩 | ✅ 滚动压缩 + 43 轮实战复证（08-29 已闭环） |
| A11/G7 root 残留 | ✅ destroy 容器侧清理 |
| A12 预算纪律 | ✅ 执行层 nudge + 实战复证 |
| A13 兄弟项目测试不可跑 | 兄弟侧问题（pytest 0 collected + SystemExit），域案例 arknights-4 的基线预检将标 environment_error 直至修复 |
| 08-29 台账对账 | B7/B8/B9/B10/B11、B12、G4 决策、G5、G7、D1、A12 共 12 项当时实际已完成（原文件状态滞后） |

## 三、C/D 组用户项（2026-08-30 用户逐项裁决）

| # | 项 | 裁决与状态 |
|---|----|-----------|
| C1 | 单价表 MODEL_PRICE_USD_PER_1K | 用户裁决：暂不填，成本列保持 0，CLI 已有提示；待用户提供单价 |
| C2 | domain 基准案例缺位 | ✅ 已补 benchmark/cases/domain/arknights-4.json（兄弟项目真实 Issue #4 题面，deletions 判定器；A13 前提注记） | 
| D1 | 条件自动放行 | ✅ 早已落地（--auto-approve），本轮 IM-1 补测试证据硬条件 |
| D2 | 知识纠错第二阶段 | 已解冻启动：用户裁决**先做事实核查报告**（65 条死数据 LLM 对照 stories 原文，三态判定，零写回）；spec 见 docs/specs/2026-08-30-fact-check-stage2.md，**待实施** |
| D3 | PR #3 / Issue #4 | ✅ 用户授权：PR #3 已合并（mergedAt 2026-08-30T07:52Z），Issue #4 已带「保留 0 删除」结论关闭 |
| D4 | 模型分流 | ✅ 用户裁决：默认 mimo-v2.5 + 文档化建议（长预算任务显式切 deepseek-v4-flash） |

## 四、剩余待办（下会话入口）

1. **D2-a 事实核查报告实施**（spec 已批，TDD）：知识核查对象=审计报告 65 条死数据 + 人工复核 bridge 假阳性清单；形态沿用 --correct 管道（tools/knowledge_audit 扩展 / agent/correct 新函数 / main.py 子命令）；输出 md/jsonl 三态报告，零写回。
2. Minor 35 项（审查报告第四节）按窗口节奏清理；优先建议：M17（--issue 默认迭代上限三处不一致）、M6（review 首行 startswith 严格性）、M15（CLI 退出码全 0）。
3. A13：兄弟项目测试套件修复（pytest 0 collected）——解锁域案例 arknights-4 真实评测。
4. 环境备注：主仓 .venv 为非 editable vendoring 副本（benchmark 等 src 改动不反映到 .venv 内 import），跑测试/校验用全局 `python -m pytest`（pythonpath=.）或重建 venv。
