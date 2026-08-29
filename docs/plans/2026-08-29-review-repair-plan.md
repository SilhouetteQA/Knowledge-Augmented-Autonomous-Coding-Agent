# 审查修复计划（brooks-health Dashboard + 三轴审查，2026-08-29）

> 输入：docs/analysis/2026-08-29-brooks-health-dashboard.md（74/100）+ 三轴审查（架构/测试/卫生）+
> docs/analysis/2026-08-29-unfinished-ledger.md。本计划是"针对审查得到的问题进行完整计划修复"的正式产物。
> 状态标记：[x] 当日已修（回归 396 passed / 4 skipped / 0 failed）·[ ] 待办（含前置条件）。

## 一、Dashboard Top-5 与三轴 Critical（当日全部闭合）

- [x] R2/Debt：REVIEW_CONTEXT_PROBES 硬编码 → 父目录自适应 + KA_REVIEW_PROBES 覆盖（agent/issue.py）
- [x] Arch/I2：benchmark setup 跨容器环境蒸发 → sandbox_executor 嵌套复用（workspace 匹配）+ 单 case 共享上下文
- [x] Arch/C1：execute_node 工具异常防护（转 ToolError 回注）+ run_command timeout 上限 600
- [x] Arch/C2：untracked 绕过红线（命中即删并入还原清单）+ approve 纵深防御（红线 untracked 存在即拒绝）
- [x] Test/C1：KA_TEST_TIMEOUT_S 两执行器接线（此前死代码）+ 超时兜底文件对象修复
- [x] Test/C2：破坏性 git 引号段剥离（echo "…; git reset" 误报修复，漏拦形态 docstring 声明）

## 二、三轴 Important（当日已修）

- [x] G3 workspace_root 透传（接线缺陷，经 run_agent_graph 集成断言钉住）
- [x] docker 容器路径换算（workspace 基准 + POSIX 归一；run_tests(path) docker 模式此前必越界）
- [x] tracing is_enabled 移入调用时（.env 配置此前对装饰不可见）
- [x] loop 模式 LLM 异常降级（与 graph G1 同语义）
- [x] main.py compare 句柄/遮蔽导入；correct out_dir 回填；report CLICKHOUSE_PASSWORD 注释
- [x] 红线 git 故障冒泡测试、edit_file 四分支测试、平台无关越界测试、tracing 新语义测试

## 三、台账余项（按前置条件分层）

**设计决策（需用户/下一窗口拍板）**
- [ ] G4 verify 空洞通过语义：0 收集且 0 错误现行判 pass；改为 fail 会伤"无测试仓库"，需口径决定
- [ ] E6-1/B11 --compare 不回填 adjusted 口径：统一出处或注记
- [ ] B14 mount_sync flake：观测（复现时按 Docker Desktop bind-mount 抖动处理）

**架构排期（独立小窗口级任务）**
- [ ] G5 上下文压缩：decide 历史无压缩（50 轮域任务 → 超时崩溃诱因）；方案：工具结果滚动截断 + 阶段摘要注入
- [ ] G2 双 ReAct 循环归一：loop（W1 模式）与 graph 并存，建议 --graph 缺省化后收敛
- [ ] D1 条件自动放行档：Reviewer PASS + 红线 0 + 测试绿 + 非删除型 → 自动 PR（审批单字段齐备）
- [ ] G7 容器 root 残留：destroy 钩子自动清理（现为一次性 root 容器手工方案）

**待外部输入**
- [ ] C1/P2-5 单价表：MODEL_PRICE_USD_PER_1K（待用户提供 mimo-v2.5 / deepseek-v4-flash 单价）
- [ ] D3 PR #3 审阅/合并 + Issue #4 续跑策略（SilhouetteQA 决定；建议 mimo 周限恢复或先落 A12——A12 nudge 已落地待实测复证）

**随任务顺手清偿（低优先）**
- [ ] B12 A7-2 task-A7-brief.md 档案补全
- [ ] 测试 conftest.py 抽取 _load_probe 重复（M1）；手写 env save/restore → monkeypatch（M3）
- [ ] E9 台账余项（EVENT_REF_FIELDS 死代码等 9 项择要）；E10 余项 6 项
- [ ] plans 历史文档"已完成"头注（M-1，11 份）
- [ ] A13 兄弟项目容器内测试 2 errors 根因（0 passed/2 error，待专项排查）

## 四、当日已闭合 but 建议复验

- [ ] A12 预算 nudge 生效性：deepseek 真实 run 复证（nudge 已落地 + 单测）
- [ ] 嵌套复用沙箱在真实 benchmark（docker 执行器全量 5 case）复跑验证
