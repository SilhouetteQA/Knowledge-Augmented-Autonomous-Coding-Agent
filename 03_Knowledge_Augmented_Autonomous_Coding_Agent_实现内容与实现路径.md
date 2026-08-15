# Knowledge-Augmented Autonomous Coding Agent：实现内容与实现路径

## 1. 项目定位

基于现有的《明日方舟》全量剧情结构化知识库、Knowledge Graph、LangGraph ReAct Agent，进一步构建：

> **领域知识增强的 Autonomous Coding Agent**

最终目标：

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

核心不是做一个“AI 写代码工具”，而是证明 Agent 能够理解真实代码仓库、调用工具、操作隔离环境、执行代码、观察结果、根据反馈继续行动，并完成真实 GitHub Issue。

---

## 2. 为什么适合现有 Wiki 项目

现有项目已经具备：

```text
LLM
→ Structured Extraction
→ Knowledge Graph
→ LangGraph
→ ReAct Agent
→ Tool Calling
```

Coding Agent 增加：

```text
Repository
→ Code Understanding
→ File / Shell / Git Tools
→ Sandbox
→ Test
→ Failure Recovery
```

两个项目形成互补：

| 现有 Wiki Agent | 新 Coding Agent |
|---|---|
| 剧情知识 | 代码知识 |
| Knowledge Graph | Code Knowledge Graph |
| RAG | Repository Retrieval |
| ReAct | ReAct / Workflow |
| Tool Calling | File / Shell / Git Tools |
| 问答 | 执行任务 |
| 知识验证 | 测试验证 |

---

# 3. 目标：做到第四阶段

## Level 1：Code Tool Agent

实现：

- [ ] list_files
- [ ] read_file
- [ ] search_code
- [ ] write_file
- [ ] run_command
- [ ] run_tests

目标：

```text
读取代码
→ 理解代码
→ 修改代码
→ 执行测试
```

## Level 2：Autonomous Coding Loop

增加：

- [ ] Planner
- [ ] Agent Loop
- [ ] Observation
- [ ] Retry
- [ ] Reflection
- [ ] Git Diff

流程：

```text
Task
→ Analyze
→ Plan
→ Tool
→ Observe
→ Tool
→ Test
→ Debug
→ Review
```

## Level 3：Sandbox + Repository

增加：

- [ ] Docker Sandbox
- [ ] Git Clone
- [ ] 独立 Workspace
- [ ] Runtime Environment
- [ ] Dependency Installation
- [ ] Command Timeout
- [ ] Resource Limits

形成：

```text
Agent
→ Tool
→ Docker Sandbox
→ Repository
→ Code / Test / Git
```

## Level 4：GitHub Issue → Pull Request

最终目标：

```text
GitHub Issue
→ Clone Repository
→ Create Branch
→ Analyze Repository
→ Search Relevant Code
→ Build Context
→ Plan
→ Modify Code
→ Generate / Update Tests
→ Run Tests
→ Failure? → Analyze → Repair → Test
→ Git Diff
→ Code Review
→ Human Approval
→ Commit
→ Push Branch
→ Create Pull Request
```

---

# 4. 最终架构

```text
                         GitHub
                           │
                 Issue / Repository
                           │
                           ↓
                    API / Agent UI
                           │
                           ↓
                    Agent Orchestrator
                           │
              ┌────────────┼────────────┐
              ↓            ↓            ↓
           Planner      Researcher     Coder
              │            │            │
              └────────────┼────────────┘
                           ↓
                     Tool Executor
                           │
         ┌─────────────────┼─────────────────┐
         ↓                 ↓                 ↓
     File Tools         Git Tools        Shell Tools
         │                 │                 │
         └─────────────────┼─────────────────┘
                           ↓
                     Docker Sandbox
                           │
                           ↓
                       Repository
                           │
                    ┌──────┴──────┐
                    ↓             ↓
                  Code          Tests
                    │             │
                    └──────┬──────┘
                           ↓
                       Results
                           │
                           ↓
                        Critic
                           │
                    ┌──────┴──────┐
                    ↓             ↓
                  Retry         Pass
                    │             │
                    └──────┬──────┘
                           ↓
                        Review
                           ↓
                     Human Approval
                           ↓
                     GitHub PR
```

---

# 5. 如何结合现有 LLM Wiki

核心特色不要只是：

```text
GitHub Repo
→ Coding Agent
```

而是：

```text
Coding Agent
→ Domain Knowledge Agent
→ Existing LLM Wiki
→ Knowledge Graph
```

形成：

```text
                     GitHub Issue
                           │
                           ↓
                       Planner
                           │
               ┌───────────┴───────────┐
               ↓                       ↓
        Repository Agent        Domain Knowledge Agent
               │                       │
               ↓                       ↓
       Code Knowledge            Arknights KG
               │                       │
               └───────────┬───────────┘
                           ↓
                      Task Context
                           ↓
                         Coder
```

最自然的做法是把现有 Wiki 本身作为真实代码仓库。

例如：

```text
Arknights Wiki Project
├── data/
├── knowledge/
├── extraction/
├── graph/
├── retrieval/
├── agents/
└── tests/
```

给 Agent 一个真实 Issue：

> 修改人物关系抽取逻辑，使同一人物在不同章节中不会重复创建 Entity。

Agent 同时调用：

```text
search_code()
+
search_knowledge()
```

得到：

```text
Code Context
+
Domain Context
```

再决定如何修改。

这就是项目区别于普通 Coding Agent 的核心。

---

# 6. 推荐的真实 GitHub Issue

## Issue 1：普通 Bug

> 某 API 在特定情况下返回 500。

流程：

```text
定位 → 修复 → 测试
```

## Issue 2：测试补充

> 为某个知识抽取模块增加边界条件测试。

流程：

```text
分析代码 → 理解函数 → 生成测试 → 执行
```

## Issue 3：领域逻辑 Bug

> 同一个角色在不同章节被错误识别为不同 Entity。

流程：

```text
Issue
→ Code Search
→ Knowledge Retrieval
→ Entity Resolution Logic
→ Modify
→ Regression Test
```

## Issue 4：跨模块任务

> 修改实体合并逻辑，同时保证已有 Knowledge Graph 数据兼容。

流程：

```text
Repository Analysis
→ Dependency Analysis
→ Knowledge Graph Analysis
→ Plan
→ Modify Multiple Modules
→ Migration / Compatibility Test
```

## Issue 5：旗舰 Demo

> 优化三遍 LLM 信息抽取 Pipeline，降低重复实体生成率，同时保持已有数据兼容。

流程：

```text
Issue
→ Architecture Analysis
→ Knowledge Retrieval
→ Code Analysis
→ Plan
→ Modify
→ Unit Tests
→ Integration Tests
→ Knowledge Evaluation
→ Before / After Comparison
→ Review
→ PR
```

---

# 7. 必须实现的 Tools

## 文件工具

```python
list_files()
read_file(path)
write_file(path, content)
search_files(query)
```

## Code Search

```python
search_code(query)
find_definition(symbol)
find_references(symbol)
```

建议进一步研究：

```text
Tree-sitter
Python AST
ripgrep
```

## Shell

```python
run_command(command)
```

返回：

```text
timeout
stdout
stderr
exit_code
duration
```

## Testing

```python
run_tests()
run_test(test_path)
```

## Git

```python
git_status()
git_diff()
git_log()
git_create_branch()
git_commit()
git_push()
```

## GitHub

```python
get_issue()
get_repository()
create_branch()
create_pull_request()
comment_issue()
```

最终实现：

```text
Issue → PR
```

完整闭环。

---

# 8. Sandbox 是什么

不要让：

```text
LLM
→ Shell
→ 你的真实电脑
```

而应该：

```text
LLM
→ Tool
→ Docker Sandbox
→ Repository
```

Sandbox 可以包含：

```text
Python
Node.js
Git
pytest
npm
项目依赖
```

并限制：

- 文件系统
- 网络
- CPU
- 内存
- 执行时间
- 系统权限
- Secrets

推荐生命周期：

```text
Create Task
→ Create Container
→ Clone Repository
→ Create Branch
→ Install Dependencies
→ Agent Execution
→ Run Tests
→ Generate Diff
→ Human Approval
→ Destroy Container
```

即：

> **一个 Task 对应一个独立 Sandbox。**

---

# 9. Agent 输入

每次任务至少包含：

```json
{
  "repository": "https://github.com/example/project",
  "issue_number": 123,
  "task": "Fix login API 500 error",
  "runtime": {
    "language": "python",
    "version": "3.12",
    "test_command": "pytest"
  }
}
```

此外 Agent Runtime 提供：

```text
Tools
Policies
Sandbox
Memory
Knowledge
```

---

# 10. Agent State

建议 LangGraph State：

```python
class AgentState:
    task
    repository
    issue
    branch
    plan
    messages
    files_inspected
    tool_results
    code_changes
    test_results
    review_results
    iteration
    status
```

重要原则：

> 不要把整个 Repository 塞进 Prompt。

Agent 应通过 Tool 按需读取代码。

---

# 11. LangGraph Workflow

第一版：

```text
START
  ↓
Load Issue
  ↓
Analyze Repository
  ↓
Retrieve Domain Knowledge
  ↓
Plan
  ↓
Execute Tool
  ↓
Observe
  ↓
Need More Action?
 ├── Yes → Execute Tool
 └── No → Run Tests
              ↓
          Test Passed?
          ├── No → Analyze Failure
          │          ↓
          │        Re-plan
          │
          └── Yes → Review
                       ↓
                   Human Approval
                       ↓
                     GitHub PR
                       ↓
                      END
```

---

# 12. 实现路径

## 第 1 阶段：本地 Workspace

```text
coding-agent/
├── agent/
├── tools/
├── workspace/
│   └── demo-project/
└── main.py
```

先实现：

```text
list_files
read_file
search_code
write_file
```

目标：

> Agent 能理解并修改真实 Python 项目。

---

## 第 2 阶段：Shell + Test

加入：

```text
run_command
run_tests
```

目标：

> Agent 能执行代码并观察结果。

形成：

```text
Analyze
→ Plan
→ Tool
→ Observe
→ Test
→ Debug
```

---

## 第 3 阶段：Docker Sandbox

将：

```text
subprocess
```

替换为：

```text
Docker Container
```

实现：

```text
Create Sandbox
→ Mount / Clone Repository
→ Execute Command
→ Return Result
→ Destroy
```

增加：

- timeout
- CPU limit
- memory limit
- network policy
- filesystem restriction

---

## 第 4 阶段：Repository Intelligence

加入：

```text
Code Parser
→ Code Metadata
→ Code Knowledge Graph
```

例如：

```text
Class
 ↓ inherits
Class

Function
 ↓ calls
Function

API
 ↓ invokes
Service

Service
 ↓ accesses
Repository
```

同时接入现有：

```text
Arknights Knowledge Graph
```

形成：

```text
Code Knowledge
+
Domain Knowledge
```

---

## 第 5 阶段：GitHub Issue Agent

加入 GitHub API：

```text
Get Issue
→ Clone Repo
→ Create Branch
→ Agent Work
→ Run Tests
→ Git Diff
→ Review
→ Commit
→ Push
→ Create PR
```

这是项目的最终 MVP。

---

## 第 6 阶段：Evaluation

建立 GitHub Issue Benchmark：

```text
issues/
├── bug/
├── feature/
├── test/
├── refactor/
└── domain/
```

指标：

```text
Issue Resolution Rate
Test Pass Rate
Patch Acceptance Rate
Tool Success Rate
Iteration Count
Latency
Cost
```

核心指标：

> **Issue Resolution Rate**

例如：

```text
V1: 32%
V2: 47%
V3: 61%
V4: 72%
```

---

## 第 7 阶段：Observability

每个任务生成完整 Trace：

```text
Issue
→ Planner
→ Tool Call
→ LLM
→ Tool Call
→ Test
→ Failure
→ Retry
→ Test
→ Review
→ PR
```

记录：

```text
Latency
Tokens
Cost
Tool Calls
Errors
Retries
Test Results
```

推荐：

- Langfuse
- OpenTelemetry

---

## 第 8 阶段：Human-in-the-loop

自动 PR 前：

```text
Agent 完成
→ 生成 Diff
→ Reviewer Agent
→ Human Approval
→ Create PR
```

之后再考虑完全自动化。

---

# 13. 推荐技术栈

## Agent

```text
Python
LangGraph
OpenAI-compatible LLM
Pydantic
```

## Code Understanding

```text
Tree-sitter
Python AST
ripgrep
```

## Tools

```text
Filesystem
Shell
Git
GitHub API
MCP
```

## Runtime

```text
Docker
Redis
PostgreSQL
```

## Evaluation

```text
Custom Benchmark
LLM-as-a-Judge
Regression Tests
```

## Observability

```text
OpenTelemetry
Langfuse
```

## Web

```text
FastAPI
React / Next.js
```

---

# 14. 不建议一开始做

第一版不要同时实现：

- 复杂 Multi-Agent
- Browser Agent
- 自我进化 Agent
- Kubernetes Sandbox
- 多模型自动路由
- 复杂 Code Knowledge Graph
- 全自动生产部署

先完成：

```text
Issue
→ Analyze
→ Tool
→ Modify
→ Test
→ Debug
→ Diff
→ PR
```

再扩展。

---

# 15. 最终 Demo

输入：

```text
GitHub Repository
+
Issue #123
```

Agent 自动：

```text
✓ Read Issue
✓ Clone Repository
✓ Create Branch
✓ Analyze Repository
✓ Search Code
✓ Query Domain Knowledge
✓ Generate Plan
✓ Modify Code
✓ Generate Test
✓ Run Test
✗ Test Failed
✓ Analyze Failure
✓ Modify Again
✓ Run Test
✓ All Tests Passed
✓ Code Review
✓ Human Approval
✓ Commit
✓ Push
✓ Create PR
```

最终输出：

```text
PR #456

Title:
Fix entity duplication in story extraction

Files:
3 changed

Tests:
128 passed

Agent Iterations:
4

Cost:
$0.xx

Duration:
xx minutes
```

---

# 16. 最终项目能力矩阵

| 能力 | 现有 Wiki | Coding Agent |
|---|---:|---:|
| LLM | ✅ | ✅ |
| RAG | ✅ | 可选 |
| Knowledge Graph | ✅ | Code KG |
| LangGraph | ✅ | ✅ |
| ReAct | ✅ | ✅ |
| Tool Calling | ✅ | 深化 |
| Planning | 🟡 | 🟢 |
| Code Understanding | - | 🟢 |
| Shell Execution | - | 🟢 |
| Git | - | 🟢 |
| GitHub | - | 🟢 |
| Sandbox | - | 🟢 |
| Testing | - | 🟢 |
| Reflection | 🟡 | 🟢 |
| Failure Recovery | 🟡 | 🟢 |
| MCP | 待补 | 🟢 |
| Evaluation | 待补 | 🟢 |
| Observability | 待补 | 🟢 |
| Human-in-the-loop | 待补 | 🟢 |

---

# 17. 最终项目定位

推荐名称：

> **Knowledge-Augmented Autonomous Coding Agent**

中文：

> **领域知识增强的自主编程 Agent**

副标题：

> 基于 LangGraph、MCP、Knowledge Graph、Docker Sandbox 与 GitHub Workflow 的 Autonomous Software Engineering Agent

最终形成：

```text
                 AI Agent Engineer
                        │
          ┌─────────────┴─────────────┐
          ↓                           ↓
    Domain Agent                 Coding Agent
          │                           │
   Knowledge Graph             Code Intelligence
          │                           │
          └─────────────┬─────────────┘
                        ↓
                    LangGraph
                        ↓
                       MCP
                        ↓
                 Agent Runtime
                        ↓
               Docker Sandbox
                        ↓
              Evaluation / Trace
                        ↓
                    GitHub PR
```

最终要证明的不是“会写代码”，而是：

> **能够让 Agent 在真实软件工程环境中自主理解任务、使用工具、修改代码、执行测试、根据反馈修复，并最终完成 GitHub Issue。**
