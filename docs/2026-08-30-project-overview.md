# 项目全景梳理：开发过程 / 功能 / 进度

> 生成日期：2026-08-30。基线：main @ 69554cd（171 commits，工作树干净，无活动 worktree）。
> 信息来源：readme.md、docs/roadmap.md、docs/devlog.md、docs/analysis/、docs/sessions/、docs/specs/、docs/plans/ 与全量源码走读。
> 配套文档：《[技术栈与部署指南](2026-08-30-tech-stack-and-deployment.md)》《[代码审查报告](2026-08-30-code-review-report.md)》。

---

## 一、项目定位

**领域知识增强的自主编程 Agent（Autonomous Software Engineering Agent）**：基于《明日方舟》全量剧情结构化知识库、Knowledge Graph 与 LangGraph ReAct Agent，构建能够**自主完成真实 GitHub Issue** 的编程 Agent。

目标链路（已全部打通并经真实实验验证）：

```text
GitHub Issue → Repository Analysis → Domain Knowledge Retrieval → Task Planning
→ Code Modification → Test → Failure Analysis → Iterative Repair
→ Code Review → Human Approval → GitHub Pull Request
```

项目核心不是"AI 写代码工具"，而是证明 Agent 能在真实软件工程环境中自主完成端到端任务：理解真实代码仓库、调用工具、操作隔离环境、执行代码、观察结果、根据反馈迭代修复。

**能力等级目标（Level 1-4）已全部达成**：

| 等级 | 名称 | 状态 | 达成证据 |
|------|------|------|----------|
| L1 | Code Tool Agent（文件四件套 + 命令 + 测试） | 达成 | W1：真实 LLM 在 demo-project 完成"读取-理解-修改-确认"闭环 |
| L2 | Autonomous Coding Loop（显式规划/重试/反思） | 达成 | W2：bilibili defect-repo 79 测试全绿闭环（含失败重试与因果验证） |
| L3 | Sandbox + Repository（Docker 沙箱/独立工作区/资源限制） | 达成 | W3：camera man 全命令容器化，容器内 51 测试全绿，无残留 |
| L4 | GitHub Issue → PR 全链路 | 达成 | W5-W8：schedule#646 修复、真实 PR #3、五类基准评测 |

**核心特色：领域知识增强（与普通 Coding Agent 的关键区别）**——Agent 同时调用代码知识与领域知识：`query_code_graph()`（AST 代码图）+ `search_knowledge()`（Arknights KG），得到 Code Context + Domain Context 后再决策。最自然的落地：把兄弟项目 Arknights LLM Wiki 本身作为被操作的真实仓库，让 Agent 完成领域逻辑类 Issue（如知识数据纠错）。

---

## 二、功能全景

### 2.1 Agent 核心（agent/）

| 模块 | 功能 |
|------|------|
| `graph.py` | LangGraph 显式状态图：`plan → decide ⇄ execute → verify（强制测试）→ reflect → finalize`，两个触顶终态变体（`finalize_limited`/`finalize_iter_limited`）。11 个注册工具（文件四件套 + edit_file + run_command/run_tests + git 只读三件套 + query_code_graph + search_knowledge）。含 LLM 故障三处优雅降级（plan/decide/reflect）、上下文滚动压缩（送 LLM 视图保留最近 30 条、旧工具结果截 1200 字符，state 内历史无损）、阶段预算纪律（过 2/3 预算且零写操作时注入提醒）、域操作红线规则注入、环境声明防幻觉 |
| `loop.py` | W1 最小 ReAct 循环（保留回归用）：tool-call 消息按 OpenAI 格式回注、reasoning_content 回传（G8）、LLM 失败降级、沙箱上下文包裹 |
| `issue.py` | GitHub Issue 全链路编排：Get Issue → clone/sync（幂等）→ 建分支（-B）→ 跑图 → **红线机械拦截**（路径/diff 内容双通道，命中即还原工作树；untracked 红线文件直接删除）→ 独立 Reviewer 审查（输入 = Issue + diff + 工作树事实 + 测试证据，不见工作过程）→ FAIL 携意见重跑一轮（上限 1）→ **生成审批单停等**；`auto_approve_if_eligible` 条件自动放行（Reviewer PASS + 红线 0 + 非删除型 + pending） |
| `llm.py` | LLM 客户端：OpenAI 兼容协议 + 双供应商（opencode_go / deepseek，`KA_LLM_PROVIDER` 切换）、超时/重试可配、token 累计、Langfuse generation 埋点、Mock 实现 |
| `correct.py` | 知识抽查编排（纯规则层、零 LLM、零写回）：load_inventory → 2% 分层抽样 → 可靠性/实用性检查 → md + jsonl 报告 |

### 2.2 工具层（tools/）

| 模块 | 功能 |
|------|------|
| `file_tools.py` | list_files / read_file（500KB 上限、带行号）/ search_code（ripgrep --json）/ write_file / edit_file（局部编辑），统一 `resolve_workspace_path` 路径守门（resolve + is_relative_to，拦 `../`、符号链接、盘符越界） |
| `shell_tools.py` | run_command（timeout/100KB 截断/Windows 进程树终止）/ run_tests（pytest 解析为结构化 TestResult + test.run 埋点）/ git 只读三件套；Executor 协议（Local/Docker 双轨，ContextVar 切换）；破坏性 git 命令黑名单拦截 |
| `docker_sandbox.py` | 一个任务一个沙箱：SandboxConfig 六维限制（CPU/内存+禁 swap/PID 上限/网络 opt-in/tmpfs/秘密隔离）、SandboxManager（create→exec→destroy，创建失败亦销毁）、DockerExecutor、`sandbox_executor` 上下文（嵌套同 workspace 复用外层容器） |
| `github_tools.py` | gh CLI 封装（get_issue/get_repository/clone/sync/create_pull_request/comment_issue）+ git 写操作（create_branch -B/commit/push），全部宿主执行、凭据不进沙箱 |
| `approval.py` | 审批单核心：schema 校验 + pending→approved/rejected 终态状态机（防重放）+ 漂移检查（分支存在 + diff sha256 指纹 + untracked 红线文件存在即拒）+ approve 推送建 PR / reject 零副作用；预留 knowledge_apply 动作类型 |
| `code_parser.py` + `code_graph.py` | Python AST 解析（module/class/function/import/calls）→ 内存 CodeGraph → 五类查询（calls/inheritance/imports/module_of/symbols），空结果附自导航提示 |
| `knowledge_client.py` | 经兄弟项目 stdio MCP 客户端子进程适配 Arknights KG（argv 传参无 shell、UTF-8 reconfigure、零新增依赖） |
| `knowledge_audit.py` | 知识抽查规则层：统一条目模型（实测 11072 条）、分层可复现抽样、来源可靠性三锚点（line_range/原文出现/source_records）、实用性入度判死数据 |
| `report_trace.py` + `tracing.py` | trace 导出（SDK 优先 + ClickHouse events_core 直查回退 → JSON+MD 报告）；Langfuse 可开关埋点（关闭态零开销直通） |

### 2.3 GitHub Issue 全链路与 Human-in-the-loop（W5+W8）

两段式工作流（**推送唯一入口是 `--approve`**，run 内从不含 push）：

```text
python main.py "owner/name#123" --issue          # 干跑产单停等（远端零副作用）
python main.py --approve output/approvals/<单>.json --decision approve   # 人工批准 → commit+push+建 PR
python main.py --approve <单>.json --decision reject     # 拒绝 → 零远端副作用
python main.py --approve <单>.json --decision approve --auto-approve-pass   # 条件自动放行档
```

Reviewer 为独立审查角色（四要点：正确性/域操作/产物残留/测试并入），证据面含工作树事实清单、路径探针、红线还原记录、测试验证事实（含 total=0 与异常形态的显式告知）。

### 2.4 评测子系统（benchmark/，W6）

- **案例库**：`benchmark/cases/` 五类（bug×2 / feature / test / refactor，domain 类规则判定器就绪但案例待补），gold patch 取自社区已合并 PR。
- **流水线**：loader（schema 逐键校验）→ runner（repo 就绪 → case 级 setup_commands → **基线测试预检**（失败标 environment_error 与 Agent 能力分离）→ Agent → 判定）→ 双判定（must_pass 测试 + LLM-as-a-Judge 功能等价 / 域案例规则检查器 deletions/bridge）→ report（JSON+MD+`--compare` 版本对比）。
- **指标**：Issue Resolution Rate（核心，raw/adjusted 双口径）、Test Pass Rate、Patch Acceptance Rate、Tool Success Rate、Iteration Count、Latency、Cost（单价表待填，恒 0）。
- **实测量化**：docker 执行器真实评测 Resolution Rate 20%（1/5，schedule-608 resolved；另 2 例 Judge 确认修复等价但基线缺 pytz 致 test_pass=False——该缺口已由镜像预装修复）。

### 2.5 可观测性（W7）

- 全链路 Trace：graph 七节点（plan/decide/execute/verify/reflect/finalize + 边路径）+ tool.execute + test.run + issue.run（verify_rounds/retry_count 指标）+ benchmark.run/evaluation.case。
- 指标：Latency / Tokens / Cost / Tool Calls / Errors / Retries / Test Results。
- 栈：Langfuse v4（SDK 4.14.4 经 OpenTelemetry/OTLP 上报，events_only 模式落 ClickHouse events_core）+ `--trace-report` 导出（SDK 读口优先、ClickHouse 直查回退，两条路径均实测）。

### 2.6 知识抽查扩展（W5 内，知识纠错第一阶段）

`main.py --correct`：对兄弟项目三次提取产物（v1_events 106 章 + v3_seed）做 2% 分层抽样只读审计——**来源可靠性 96.88%（217/7）、内容实用性 70.98%（65 条疑似死数据）**；人工复核定位"代号↔真名命名脱节"（如瑕光↔玛莉娅无 bridge）与组织名粒度两类假阳性。第二阶段（LLM 事实核查 + `--apply` 写回）已解冻待排期（审批通道 action_type=knowledge_apply 已预留）。

---

## 三、开发过程

### 3.1 方法论

- **Superpowers 全套流程**：brainstorming（澄清意图 → Spec 用户批准）→ writing-plans（任务分解 2-5 分钟/步，无占位符）→ 实施（subagent-driven + TDD，red→green→refactor，垂直切片）→ code-review（双轴：Standards + Spec）→ systematic-debugging → commit（Conventional Commits 中文）。
- **窗口任务制**：按需求文档第 12 节实现路径划分 W0-W8 共 9 个窗口，一个窗口 = 一个 worktree = 一个 feature 分支 = 一个实现路径阶段；main 只接受已 review 的合并。依赖链：W1 → W2 → W3 → W5 → W6/W7 → W8，W4 与 W5 可并行。
- **硬门禁**：设计未经用户批准不写实现代码；高风险写操作（push/PR/知识数据修改）必须 Human-in-the-loop。

### 3.2 完整时间线（2026-08-15 → 2026-08-30，共 16 天）

| 日期 | 里程碑 | 关键内容 |
|------|--------|----------|
| 08-15 | **W0 项目初始化** | AGENTS/README/路线图、git 初始化、脚手架 |
| 08-15 | **W1 本地 Workspace** | 文件四件套 + LLM 客户端 + 最小 ReAct 循环 + CLI；23 测试；真实演示（camera man 注释闭环）；模型定为 opencode go deepseek-v4-flash |
| 08-15 | **W2 Shell + Test** | run_command/run_tests/git 只读三件套 + LangGraph 显式状态图（plan→decide⇄execute→verify→reflect→finalize）；47 测试；真实演示（bilibili defect-repo 79 测试修复闭环） |
| 08-16 | **W3 前置：Docker 环境** | Docker Desktop 4.86 安装至 D:\Docker、WSL2 内核修复启用、数据盘迁移至 D 盘（三踩坑：孤儿注册表项、CDN 限速并行下载、CustomWslDistroDir 仅 GUI 可改） |
| 08-17 | **W3 Docker Sandbox** | SandboxConfig/SandboxManager/DockerExecutor、镜像 ka-sandbox:py312-v1（python3.12-slim + git + node22 + pytest）、六维资源限制、创建期有网运行期断网；88 测试（含 10 项真实容器集成）；camera man 容器化闭环 |
| 08-19 | **W4 Repository Intelligence** | Python AST 选型（Tree-sitter 否决）→ CodeParser → 内存 CodeGraph 五类查询 → MCP 子进程接入 Arknights KG → 双知识工具注入图；113 测试；领域 Issue 定位演示（跨章节实体去重结论） |
| 08-25 | **模型依赖统一迁移** | 收敛回 opencode_go_api 单一入口，模型定为 **mimo-v2.5**（修正下划线/连字符 ID 陷阱），废弃 arkcode 遗留配置 |
| 08-25 | **W5 GitHub Issue Agent** | gh CLI 封装 + 全链路编排 + push 门禁；真实 dry-run 演示 **dbader/schedule#646**（`__repr__` unit=None 守卫，与社区 PR #652 方案一致）；诚实记录 6 项能力边界 |
| 08-25 | **W5 知识抽查扩展** | knowledge_audit/correct/`--correct`；真实验收 11072 条抽 224：可靠性 96.88% / 实用性 70.98%；人工复核定位 bridge 命名脱节；**知识纠错第二阶段冻结（等 W8 门禁）** |
| 08-26 | **W6 Evaluation** | benchmark 包五件套 + 可开关 tracing + 案例库 5 个 + `--benchmark/--compare`；两次环境性失败 run（死代理/沙箱 npipe 限制）留证后修复闭环（git 镜像 ghfast.top + sslBackend=openssl），真实评测 **Resolution Rate 20%**（schedule-608 resolved）；定位 pytz 缺失为基准缺口 |
| 08-26 | **W7 Observability** | graph 七节点 + tool/test + issue.run retry 埋点；`--trace-report`（ClickHouse 回退路径实测）；OTLP 贯通验证（telemetry.sdk.name=opentelemetry 落库证据）；纯 SDK 冒烟 8 事件全节点落库 |
| 08-26→29 | **W8 Human-in-the-loop** | 三线并行：① 主线审批门禁（approval 单核心/漂移检查/防重放/`--approve` 两段式/独立 Reviewer）；② eval-fixes E1-E10（case 级 setup/镜像预装 pytz/基线预检/失败摘要/LLM 超时重试/双口径/收敛/域判定器/一致性核查）；③ agent-fixes A1-A7（verify 兜底/提示词约束/Reviewer 证据面/红线机械拦截/产物纪律/阶段预算/结论交付）。**真实里程碑：五轮远程修复迭代 → 人工批准 → 真实 PR #3**（3 文件纯删除 2 条已核验死数据） |
| 08-29 | **真实 Issue 能力实测（第一轮）** | rich#3299 触顶失败（环境幻觉/write_file 覆盖/git 自伤四瓶颈）；jsonschema#1159 实质解决（27 迭代，Reviewer 误判记录）；结论：轮数不是第一杠杆，每轮质量才是 |
| 08-29 | **第二轮实测 + 三轴全项目审查** | markdown-it-py#415 9 迭代完整解决（edit_file 实战）；dateutil#1545 三轮未收敛（镜像依赖遮蔽/F4 盲点）；三轴审查（架构/测试/卫生）发现 2 Critical + 12 Important，**当日全部修复**（execute_node 异常防护/untracked 红线/破坏性 git 引号误报/KA_TEST_TIMEOUT_S 接线/docker 路径换算等），回归 395 passed / 4 skipped / 0 failed |
| 08-30 | **复验闭环：arknights v4 首次完整通过** | Issue #4 第四轮（deepseek，50 迭代上限）：43 迭代未触顶、Reviewer PASS、3 条候选全部保留 0 删除、逐条证据齐备；A12/G5/G8 实战复证；发现 PR #3 从未合并（阮先生/玉门望烽节双残留）如实报告 |

### 3.3 关键架构决策（精选）

1. **执行器抽象双轨**（W3）：LocalExecutor/DockerExecutor 同一协议，`KA_EXECUTOR` + ContextVar 切换；docker 模式无 fallback（不可用即 ToolError）。
2. **沙箱网络策略**（W3）：`--network none` 会断掉创建期 pip/clone → 改"默认桥接创建 → setup 完成后 `docker network disconnect bridge`"；超时判定用 `timeout -k 5s`（124 超时/137 OOM 可区分）。
3. **LangGraph 显式编排**（W2）：StateGraph 节点化 plan/verify/reflect 满足显式规划需求；**verify 强制闭环**——Agent 声称完成后图自动跑测试，失败进 reflect 重试。
4. **双知识源注入**（W4）：build_graph 闭包注入，未动既有节点逻辑；MCP 适配不复用兄弟项目代码（依赖隔离），每次查询一个子进程（argv JSON 传参，无 shell，防注入）。
5. **凭据边界**（W5）：gh/git 写操作全部宿主执行，凭据不进沙箱；容器内仅只读 git + 工作/测试。
6. **两段式审批**（W8）：`--issue` 产单停等、`--approve` 唯一推送入口；漂移检查（diff sha256）+ 终态防重放 + reject 零副作用；完全自动 PR 经 D5 评估**暂不推荐**（正确执行率 1/5），落"条件自动放行"档。
7. **模型策略**：mimo-v2.5 稳定默认；deepseek-v4-flash 需 G8 reasoning_content 回传修复才可用（DSML 文本致假完成早停），预算纪律弱于 mimo。
8. **YAGNI 纪律**：第一版明确不做 Multi-Agent/Browser Agent/K8s Sandbox/复杂 Code KG；W6 评测环境缺口（pytz）不掩盖为 Agent 能力结论，环境错误单列口径。

---

## 四、项目进度

### 4.1 窗口状态总览

| 窗口 | 名称 | 分支 | 状态 |
|------|------|------|------|
| W0 | 项目初始化 | main | [x] 已完成（08-15） |
| W1 | 本地 Workspace | feature/w1-local-workspace | [x] 已完成（08-15） |
| W2 | Shell + Test | feature/w2-shell-test | [x] 已完成（08-15） |
| W3 | Docker Sandbox | feature/w3-docker-sandbox | [x] 已完成（08-17） |
| W4 | Repository Intelligence | feature/w4-repo-intelligence | [x] 已完成（08-19） |
| W5 | GitHub Issue Agent + 知识抽查 | feature/w5-github-issue | [x] 已完成（08-25） |
| W6 | Evaluation | feature/w6-evaluation | [x] 已完成（08-26） |
| W7 | Observability | feature/w7-observability | [x] 已完成（08-26） |
| W8 | Human-in-the-loop | feature/w8-human-in-the-loop | [x] 已完成（08-29） |

所有窗口已合并回 main，分支与 worktree 已清理；当前仓库单 main 分支（69554cd），工作树干净。

### 4.2 量化现状

| 维度 | 数值 |
|------|------|
| 提交总量 | 171 commits（main，全中文 Conventional Commits） |
| 源码规模 | 约 5400 行（agent/ tools/ benchmark/ main.py scripts/） |
| 测试规模 | 25 个测试文件 / 382 个测试函数 / 约 6100 行；**最新回归 395 passed / 4 skipped / 0 failed** |
| 设计文档 | 10 份 Spec + 13 份 Plan + 8 份会话归档 + 7 份分析报告，全部入库 |
| 真实 Issue 实测 | 8 个真实仓库 issue + 兄弟项目 Issue #4（详见 4.3） |
| 真实 PR | 1 个（PR #3，人工批准后创建） |

### 4.3 真实验证成果汇总

| 实验 | 结果 | 说明 |
|------|------|------|
| schedule#646（W5 dry-run） | 修复成功 | `__repr__` unit=None 崩溃，方案与社区 PR #652 一致；QA 复核确认 |
| W6 基准评测（docker） | Resolution Rate 20%（1/5） | schedule-608 resolved（25 迭代）；646/99 Judge 确认等价但基线缺 pytz（已修）；602/622 无有效变更 |
| 兄弟项目 Issue #2→PR #3（W8） | 交付真实 PR | 五轮远程修复 → 人工批准 → PR #3（3 文件纯删除 2 条死数据）；rejected 单零副作用实测；**PR 本身尚未合并（待 SilhouetteQA 决定）** |
| jsonschema#1159（第一轮实测） | 实质解决 | 27 迭代，7892 测试全绿 + 独立复现通过；Reviewer 误判 FAIL（修法已记录） |
| rich#3299（第一轮实测） | 触顶失败 | 41 迭代两轮全触顶；暴露四瓶颈（环境幻觉/write_file 覆盖/git 自伤/Reviewer 机械套规则），均已修复 |
| markdown-it-py#415（第二轮实测） | 9 迭代完整解决 | edit_file 实战验证、981 测试全绿、Reviewer PASS |
| dateutil#1545（第二轮实测） | 未收敛 | 暴露镜像依赖遮蔽（假绿基线）、测试证据盲点、setup 钩子缺口（G3 已修） |
| 兄弟项目 Issue #4 试点（四轮） | v4 首次完整通过 | 43 迭代未触顶、Reviewer PASS、3 候选保留 0 删除、证据齐备；G1/G8/G5/A12/A7 全部实战复证 |
| Langfuse 落库 | 验证通过 | events_core 确认 benchmark.run/issue.run/全节点 span 落库，OTLP 贯通 |

### 4.4 当前状态一句话

**W0-W8 全部完成，主链路（Issue → Analyze → Tool → Modify → Test → Debug → Diff → Review → 审批 → PR）经真实实验闭环验证；项目处于"W8 后增强收尾"阶段**——剩余工作为一批已知遗留（详见《代码审查报告》第九节与下文待处理清单）：知识纠错第二阶段（--apply 写回）、domain 类基准案例补位、单价表、PR #3 处置，以及本次全量代码审查新发现的 2 项 Critical 修复。
