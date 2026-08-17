# 实现路径路线图（窗口任务制）

> 依据：《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 12 节"实现路径"。
> 规则：一个窗口 = 一个 worktree = 一个 feature 分支 = 一个实现路径阶段。窗口进出协议见 `agents.md` 第三节。

## 窗口总览

| 窗口 | 名称 | 分支 | 依赖 | 状态 |
|------|------|------|------|------|
| W0 | 项目初始化 | main | - | [x] 已完成 |
| W1 | 本地 Workspace | feature/w1-local-workspace | W0 | [x] 已完成 |
| W2 | Shell + Test | feature/w2-shell-test | W1 | [x] 已完成 |
| W3 | Docker Sandbox | feature/w3-docker-sandbox | W2 | [x] 已完成 |
| W4 | Repository Intelligence | feature/w4-repo-intelligence | W1 | [ ] 待开始 |
| W5 | GitHub Issue Agent | feature/w5-github-issue | W3 | [ ] 待开始 |
| W6 | Evaluation | feature/w6-evaluation | W5 | [ ] 待开始 |
| W7 | Observability | feature/w7-observability | W5 | [ ] 待开始 |
| W8 | Human-in-the-loop | feature/w8-human-in-the-loop | W6, W7 | [ ] 待开始 |

依赖链：W1 → W2 → W3 → W5 → W6/W7 → W8；W4 与 W5 可并行。

---

## W0：项目初始化（已完成）

- [x] 读取需求文档，理解项目定位与实现路径
- [x] 编写 `agents.md`（Superpowers 全套流程 / 窗口任务制 / Git 规则 / 工程约束）
- [x] 编写 `readme.md`（项目定位 / 架构 / 技术栈 / 路线图）
- [x] 编写本路线图 `docs/roadmap.md`
- [x] git 初始化（main 分支，身份与兄弟项目一致）与首次提交
- [x] 脚手架：`.gitignore`、`pyproject.toml`、目录骨架（agent/ tools/ workspace/ tests/ config/ docs/）

---

## W1：本地 Workspace（阶段 1：Code Tool Agent 起步）

**目标**：Agent 能理解并修改真实 Python 项目。

**产出结构**：

```text
coding-agent/
├── agent/          # Agent 核心（LangGraph 图、节点、状态）
├── tools/          # 文件工具
├── workspace/
│   └── demo-project/   # 演示用真实 Python 项目
└── main.py         # 命令行入口
```

**任务清单**：

- [x] 进入窗口：创建 worktree `feature/w1-local-workspace`，读 readme + devlog + 本路线图
- [x] Brainstorming：澄清 W1 设计（工具接口、Agent 循环形态、demo-project 选择）→ `docs/specs/2026-08-15-w1-local-workspace.md`，用户批准
- [x] Writing-plans：产出实施计划 → `docs/plans/2026-08-15-w1-local-workspace.md`
- [x] 实现 `list_files()`（目录遍历，排除忽略项）
- [x] 实现 `read_file(path)`（带行号读取，限制大小）
- [x] 实现 `search_code(query)`（ripgrep 语义搜索，返回文件/行号/摘要）
- [x] 实现 `write_file(path, content)`（写入 + 安全校验：路径在 workspace 内）
- [x] 搭建 demo-project（真实项目：`D:\AI project\camera man` 复制至 workspace/demo-project，7 模块 + 6 测试）
- [x] TDD：每个工具先写失败测试 → 最小实现 → 通过（23 项测试）
- [x] 验证：Agent 能读 demo-project 代码、定位函数、修改、再读取确认（真实 LLM 演示：定位 storage.py 的 EventStore.record() 并添加中文注释，闭环成功）
- [x] Review（requesting-code-review）→ 修复 → 合并回 main → 更新路线图状态

**验收标准**：四个文件工具全部有测试覆盖；CLI 能对 demo-project 完成"读取-理解-修改-确认"闭环。

---

## W2：Shell + Test（阶段 2：Autonomous Coding Loop）

**目标**：Agent 能执行代码并观察结果，形成 Analyze → Plan → Tool → Observe → Test → Debug 闭环。

**任务清单**：

- [x] `run_command(command)`：返回 timeout / stdout / stderr / exit_code / duration
- [x] `run_tests()` / `run_test(test_path)`：pytest 封装，输出结构化结果
- [x] Planner：任务拆解为步骤（LangGraph plan 节点）
- [x] Agent Loop：LangGraph 循环（plan → decide ⇄ execute → verify → reflect → finalize）
- [x] Retry / Reflection：测试失败 → 分析失败原因 → 重新规划（verify 失败 → reflect → decide，上限 3 轮）
- [x] Git Diff：记录与展示代码变更（git_status / git_diff / git_log 只读三件套）
- [x] TDD + Review + 合并回 main（47 项测试全绿；真实演示：bilibili defect-repo 79 passed 闭环）

**验收标准达成**：真实缺陷任务（bilibili AI 日报测试全挂）→ Agent 自主"定位（conftest 缺失）→ 修复 → 测试通过（79 passed）"，含失败重试与因果验证。

---

## W3：Docker Sandbox（阶段 3：Sandbox + Repository）

**目标**：用 Docker 容器替换 subprocess，一个 Task 一个独立 Sandbox。

**任务清单**：

- [x] Sandbox 生命周期：Create → Mount/Clone Repository → Execute Command → Return Result → Destroy
- [x] 资源限制：timeout / CPU limit / memory limit / network policy / filesystem restriction / secrets 隔离
- [x] 沙箱内环境：Python / Node.js / Git / pytest / npm / 项目依赖安装
- [x] Git Clone 与独立 Workspace 管理
- [x] TDD + Review + 合并回 main

**完成记录（2026-08-17）**：

- 架构：`tools/docker_sandbox.py`（SandboxConfig / SandboxManager / DockerExecutor / sandbox_executor 上下文）+ `tools/shell_tools.py` Executor 协议（LocalExecutor 保留 subprocess 行为）；`KA_EXECUTOR` 切换；Agent 入口（loop/graph）包进沙箱上下文，一个任务一个沙箱
- 镜像 `ka-sandbox:py312-v1`：python:3.12-slim + git/curl + node 22（官方 tar 并行分块下载，网络限速 workaround）+ pytest；`core.filemode=false` 内置；.dockerignore 瘦身构建上下文（600MB→139B）
- 资源限制：timeout（容器内 GNU timeout -k 5s，124 判定）/ --cpus / --memory 禁 swap / --pids-limit / 创建期有网 + 运行期 `docker network disconnect bridge` / 仅挂载 workspace / 秘密隔离
- 测试：88 项全绿（单元 FakeDocker seam + 集成 10 项 @pytest.mark.docker：生命周期/超时/OOM/网络隔离/挂载同步/git 一致/双执行器一致/创建期 pip+clone/无残留）
- 验收达成：真实演示（camera man）——Agent 全部命令在容器内执行，容器内 51 测试全绿（Agent 自主降级依赖修复 + verify 门禁通过），中文总结闭环，无残留
- 用户批准偏离：国内镜像源（apt/pip）、node 官方 tar 并行下载、ToolError 改 Exception 子类、timeout 124 判定、disconnect 断网方案、main.py 加 --graph 开关（W3 验收缺口）

**验收标准**：同一任务的全部命令在容器内执行；超时/超内存被正确终止并返回结构化错误；容器销毁后无残留。—— 全部达成

---

## W4：Repository Intelligence（阶段 4：Code + Domain 双知识）

**目标**：代码解析 → Code Knowledge Graph，并接入现有 Arknights Knowledge Graph。

**任务清单**：

- [ ] Code Parser 调研与选型（Tree-sitter / Python AST / ripgrep 组合）
- [ ] 代码元数据提取：Class 继承 / Function 调用 / API-Service-Repository 关系
- [ ] Code Knowledge Graph 构建与查询
- [ ] 接入 Arknights LLM Wiki 的 Knowledge Graph（MCP 或 API）
- [ ] 双知识检索：`search_code()` + `search_knowledge()` 联合上下文
- [ ] 验证领域逻辑 Issue：同一角色在不同章节被识别为不同 Entity 的定位与修复演示
- [ ] TDD + Review + 合并回 main

**验收标准**：能回答"哪个函数创建了角色 Entity""该逻辑依赖哪些模块"；领域 Issue 修复演示成功。

---

## W5：GitHub Issue Agent（阶段 5：最终 MVP）

**目标**：GitHub API 全链路闭环，Issue → PR。

**任务清单**：

- [ ] GitHub 工具：get_issue / get_repository / create_branch / create_pull_request / comment_issue
- [ ] Git 工具：git_status / git_diff / git_log / git_create_branch / git_commit / git_push
- [ ] 全链路编排：Get Issue → Clone Repo → Create Branch → Agent Work → Run Tests → Git Diff → Review → Commit → Push → Create PR
- [ ] Agent 输入模型（repository / issue_number / task / runtime）与 AgentState
- [ ] 端到端演示：真实 Issue（先用本地 mock 仓库，再试真实仓库）
- [ ] TDD + Review + 合并回 main

**验收标准**：输入仓库 URL + Issue 编号，Agent 完成全部步骤并产出可审查的 PR（或人工确认的模拟 PR）。

---

## W6：Evaluation（阶段 6：Benchmark）

**目标**：建立固定 Benchmark 与核心指标。

**任务清单**：

- [ ] Issue 基准库：`issues/` 下 bug / feature / test / refactor / domain 五类
- [ ] 指标采集：Issue Resolution Rate（核心）/ Test Pass Rate / Patch Acceptance Rate / Tool Success Rate / Iteration Count / Latency / Cost
- [ ] 版本对比机制（V1 32% → V2 47% → ... 演进证据链）
- [ ] LLM-as-a-Judge 与回归测试辅助
- [ ] TDD + Review + 合并回 main

**验收标准**：任意 Agent 版本变更可在同一基准上重跑并对比指标。

---

## W7：Observability（阶段 7：全链路 Trace）

**目标**：每个任务生成完整 Trace 并记录成本指标。

**任务清单**：

- [ ] Trace 采集：Issue → Planner → Tool Call → LLM → Test → Failure → Retry → Review → PR 全链路
- [ ] 指标记录：Latency / Tokens / Cost / Tool Calls / Errors / Retries / Test Results
- [ ] 集成 Langfuse + OpenTelemetry
- [ ] TDD + Review + 合并回 main

**验收标准**：一次 Agent 任务执行后可导出完整 Trace 与成本报告。

---

## W8：Human-in-the-loop（阶段 8：审批门禁）

**目标**：自动 PR 前的人工确认环节，之后考虑完全自动化。

**任务清单**：

- [ ] Reviewer Agent（独立于 Coder 的审查角色）
- [ ] Diff 展示与人工审批界面/流程
- [ ] 审批通过后才 Commit → Push → Create PR
- [ ] 评估完全自动化的可行性（基于 W6 指标）
- [ ] TDD + Review + 合并回 main

**验收标准**：Agent 完成工作后停下等待审批；审批后自动完成 PR；演示完整闭环。

---

## 最终 Demo 验收（全部窗口完成后）

输入：GitHub Repository + Issue #123。预期输出：

```text
Read Issue → Clone Repository → Create Branch → Analyze Repository
→ Search Code → Query Domain Knowledge → Generate Plan → Modify Code
→ Generate Test → Run Test → (Test Failed → Analyze → Modify → Retry) → All Passed
→ Code Review → Human Approval → Commit → Push → Create PR
```

输出：PR 编号、变更文件数、测试通过数、Agent 迭代次数、成本、耗时。
