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

## 三、台账余项（按前置条件分层）——本轮全部处置

**设计决策**
- [x] G4 verify 空洞通过：口径定为"不翻转 pass 语义（无测试仓库合法），0 收集对 Reviewer 可见化"（F4c：verify 未收集 → 审查证据面显式声明"diff 未经测试验证"）
- [x] E6-1/B11 --compare adjusted 口径：compare_reports 新增 "Resolution Rate (adjusted)" 行（分母排除 error/environment_error，就地计算不依赖报告字段）
- [x] B14 mount_sync flake：定性为 Docker Desktop bind-mount 瞬时抖动（复现 3 连败后自愈、main 同测通过），维持观测

**架构排期**
- [x] G5 上下文压缩：_compact_messages 滚动压缩落地（保留最近 KA_CONTEXT_KEEP_RECENT=30 条，更早工具结果截 KA_CONTEXT_TOOL_LIMIT=1200 字符；短对话零影响 + 单测）
- [ ] G2 双 ReAct 循环归一：需默认行为切换（--graph 缺省化），影响 W1 模式 UX 与 main 测试面，留独立小窗口
- [x] D1 条件自动放行档：auto_approve_if_eligible（PASS + 红线 0 + 非删除型 + pending）+ main --auto-approve + 4 测试（删除型永久人工终审）
- [x] G7 容器 root 残留：destroy 前 exec 清理 __pycache__/.pytest_cache/.pytest_tmp（尽力而为，销毁不受影响）

**待外部输入**
- [ ] C1/P2-5 单价表：待用户提供 mimo-v2.5 / deepseek-v4-flash 单价
- [ ] D3 PR #3 审阅/合并 + Issue #4 续跑策略：qa-account 决定

**随任务顺手清偿**
- [x] B12 A7-2 task-A7-brief.md 档案补全（docs/analysis/2026-08-29-a7-brief-archive.md）
- [x] A13 定性：兄弟项目测试在容器内触发 pytest capture 内部错误（收集期 stdout 关闭）——兄弟项目侧 backlog，非本仓库问题（Agent 侧影响已被 F4c 可视化）
- [x] M-1：11 份历史计划补"已完成"头注
- [ ] 测试 conftest.py 抽取 _load_probe 重复（M1）；手写 env save/restore → monkeypatch（M3）——纯卫生，留顺手清偿
- [ ] E9/E10 台账余项（EVENT_REF_FIELDS 死代码、测试改名等）——纯卫生，留顺手清偿

## 四、当日已闭合 but 建议复验

- [x] A12 预算 nudge：**实战复证通过**——arknights v4（deepseek，local，43/50 迭代未触顶）按期交付结论，无探索失控；G5/G8 同 run 验证（43 轮无超时崩溃、无 400）
- [ ] 嵌套复用沙箱在真实 benchmark（docker 执行器全量 5 case）复跑验证（需 mimo 周限恢复后执行）
