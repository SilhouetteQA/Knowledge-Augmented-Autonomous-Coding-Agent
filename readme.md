# Knowledge-Augmented Autonomous Coding Agent

> 领域知识增强的自主编程 Agent
>
> 基于 LangGraph、MCP、Knowledge Graph、Docker Sandbox 与 GitHub Workflow 的 Autonomous Software Engineering Agent

基于现有的《明日方舟》全量剧情结构化知识库、Knowledge Graph 与 LangGraph ReAct Agent，进一步构建能够**自主完成真实 GitHub Issue** 的编程 Agent：理解真实代码仓库、调用工具、操作隔离环境、执行代码、观察结果、根据反馈迭代修复，最终产出 GitHub Pull Request。

> 项目状态：W0 项目初始化、W1 本地 Workspace 均已完成（23 项测试通过，真实项目演示闭环成功）。等待进入 W2（Shell + Test）。
> 开发规范见 [agents.md](agents.md)，窗口路线图见 [docs/roadmap.md](docs/roadmap.md)。

---

## 最终目标

```text
GitHub Issue
→ Repository Analysis
→ Domain Knowledge Retrieval
→ Task Planning
→ Code Modification
→ Test
→ Failure Analysis
→ Iterative Repair
→ Code Review
→ GitHub Pull Request
```

核心不是做"AI 写代码工具"，而是证明 Agent 能在真实软件工程环境中自主完成端到端任务。

## 能力等级（目标：做到第四阶段）

| 等级 | 名称 | 能力 | 完成标志 |
|------|------|------|----------|
| Level 1 | Code Tool Agent | list_files / read_file / search_code / write_file / run_command / run_tests | 读取 → 理解 → 修改代码 → 执行测试 |
| Level 2 | Autonomous Coding Loop | Planner / Agent Loop / Observation / Retry / Reflection / Git Diff | Task → Analyze → Plan → Tool → Observe → Test → Debug → Review |
| Level 3 | Sandbox + Repository | Docker Sandbox / Git Clone / 独立 Workspace / 资源限制 | Agent → Tool → Docker Sandbox → Repository → Code / Test / Git |
| Level 4 | GitHub Issue → PR | GitHub API 全链路 | Issue → Clone → Branch → 修改 → 测试 → Review → Commit → Push → PR |

## 核心特色：领域知识增强

与普通 Coding Agent 的关键区别——同时调用代码知识与领域知识：

```text
GitHub Issue
     │
     ↓
  Planner
     │
 ┌───┴───┐
 ↓       ↓
Repository Agent   Domain Knowledge Agent
 ↓       ↓
Code Knowledge     Arknights KG
 └───┬───┘
     ↓
 Task Context
     ↓
   Coder
```

Agent 同时调用 `search_code()` 与 `search_knowledge()`，得到 Code Context + Domain Context 后再决定如何修改。最自然的落地方式：**把现有 Arknights LLM Wiki 项目本身作为真实代码仓库**，让 Agent 完成领域逻辑类 Issue（如实体去重）。

## 最终架构

```mermaid
graph TD
    GH[GitHub] --> IO[Issue / Repository]
    IO --> API[API / Agent UI]
    API --> AO[Agent Orchestrator]
    AO --> P[Planner]
    AO --> R[Researcher]
    AO --> C[Coder]
    P --> TE[Tool Executor]
    R --> TE
    C --> TE
    TE --> FT[File Tools]
    TE --> GT[Git Tools]
    TE --> ST[Shell Tools]
    FT --> SB[Docker Sandbox]
    GT --> SB
    ST --> SB
    SB --> REPO[Repository]
    REPO --> CODE[Code]
    REPO --> TST[Tests]
    CODE --> RES[Results]
    TST --> RES
    RES --> CR[Critic]
    CR --> RT[Retry]
    CR --> OK[Pass]
    RT --> TE
    OK --> RV[Review]
    RV --> HA[Human Approval]
    HA --> PR[GitHub PR]
```

## 技术栈

| 层级 | 方案 |
|------|------|
| Agent | Python 3.12+ / LangGraph / OpenAI 兼容 LLM / Pydantic |
| Code Understanding | Tree-sitter / Python AST / ripgrep |
| Tools | Filesystem / Shell / Git / GitHub API / MCP |
| Runtime | Docker / Redis / PostgreSQL |
| Evaluation | 自定义 Benchmark（GitHub Issue） / LLM-as-a-Judge / 回归测试 |
| Observability | OpenTelemetry / Langfuse |
| Web | FastAPI / React / Next.js |
| 测试 | pytest（TDD，red → green） |

## 目录结构

```
Knowledge-Augmented Autonomous Coding Agent/
├── agents.md                     # Agent 工作规范（Superpowers 流程 / 窗口任务制 / Git 规则）
├── readme.md                     # 本文件
├── docs/
│   ├── roadmap.md                # W0-W8 实现路径路线图（窗口任务制）
│   ├── specs/                    # 设计规格（brainstorming 产出）
│   ├── plans/                    # 实施计划（writing-plans 产出）
│   └── devlog.md                 # 开发日志（架构决策 / 指标 / 遗留问题）
├── agent/                        # Agent 核心（LangGraph 图、节点、状态）
├── tools/                        # 工具层（File / Shell / Git / GitHub / MCP）
├── workspace/                    # 沙箱工作区（demo-project 等被操作仓库）
├── tests/                        # 测试套件
├── config/                       # 配置文件
└── pyproject.toml                # 项目元数据与依赖
```

## 实现路径（窗口路线图）

按《实现内容与实现路径》文档第 12 节划分为 9 个窗口，每个窗口一个独立 worktree 与 feature 分支，完整路线图见 [docs/roadmap.md](docs/roadmap.md)。

| 窗口 | 名称 | 关键交付 |
|------|------|----------|
| W0 | 项目初始化 | git 初始化、AGENTS/README/路线图、脚手架（已完成） |
| W1 | 本地 Workspace | 文件工具四件套：list_files / read_file / search_code / write_file |
| W2 | Shell + Test | run_command / run_tests + Planner / Agent Loop / Retry / Reflection |
| W3 | Docker Sandbox | 容器化执行 + timeout / CPU / 内存 / 网络 / 文件系统限制 |
| W4 | Repository Intelligence | Code Parser → Code Knowledge Graph + 接入 Arknights KG |
| W5 | GitHub Issue Agent | Issue → Clone → Branch → Work → Test → Review → Commit → Push → PR（MVP） |
| W6 | Evaluation | Issue Benchmark 五类 + Issue Resolution Rate 核心指标 |
| W7 | Observability | 全链路 Trace + Langfuse / OpenTelemetry |
| W8 | Human-in-the-loop | Reviewer Agent + 人工审批门禁后自动 PR |

## 推荐的真实 Issue（用于 W5/W6 验证）

1. 普通 Bug：某 API 在特定情况下返回 500
2. 测试补充：为知识抽取模块增加边界条件测试
3. 领域逻辑 Bug：同一角色在不同章节被识别为不同 Entity（体现双知识检索价值）
4. 跨模块任务：修改实体合并逻辑并保证已有 KG 数据兼容
5. 旗舰 Demo：优化三遍 LLM 抽取 Pipeline，降低重复实体生成率并保持数据兼容

## 快速开始

> 已实现 Level 1 前半部分：四个文件工具（list_files / read_file / search_code / write_file）+ 最小 ReAct 循环（CLI）。

```bash
# 环境要求：Python 3.12+、ripgrep（PATH 或 RIPGREP_BIN 指定）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 配置 LLM（复制 .env.example 为 .env 并填写）
# opencode_go_api / OPENCODE_GO_BASE_URL / OPENCODE_GO_MODEL

# 运行测试
.venv\Scripts\python.exe -m pytest tests/

# 运行 Agent（工作区默认 workspace/，可放入任意真实项目）
.venv\Scripts\python.exe main.py "在 demo-project 中定位某函数并添加注释" --workspace workspace
```

## 常用命令

```bash
git status / git diff            # 查看变更
pytest tests/ -v                 # 全量测试
pytest tests/xxx.py -v           # 单文件测试
```

## 关联项目

- **Arknights LLM Wiki**（兄弟项目）：本项目的领域知识来源（Knowledge Graph / LangGraph Agent / MCP 能力），同时作为 Agent 的首个真实操作仓库。
