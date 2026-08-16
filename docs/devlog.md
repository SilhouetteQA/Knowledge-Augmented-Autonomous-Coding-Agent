# Devlog — 开发日志

本文件记录项目的开发过程、架构决策、关键指标与遗留问题。新会话进入前先读本文件与 `readme.md`、`docs/roadmap.md`。

## W0 项目初始化（2026-08-15）

### 完成内容

- 读取需求文档《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》，确认项目定位：领域知识增强的自主编程 Agent，目标为 GitHub Issue → PR 闭环。
- 编写 `agents.md`：Superpowers 全套流程（brainstorming → writing-plans → TDD 实施 → code-review → systematic-debugging）、窗口任务制（W0-W8，一个窗口 = 一个 worktree = 一个 feature 分支）、Git 规则（与兄弟项目 Arknights LLM Wiki 保持一致）、工程约束与安全边界。
- 编写 `readme.md`：项目定位、能力等级（Level 1-4）、最终架构、技术栈、目录结构、实现路径总览。
- 编写 `docs/roadmap.md`：W0-W8 窗口路线图（任务清单、验收标准、依赖关系）。
- 脚手架：`.gitignore`、`pyproject.toml`（Python 3.12+、pytest）、目录骨架（agent/ tools/ workspace/ tests/ config/ docs/）。
- git 初始化：默认分支 main，身份配置与兄弟项目一致（Silhouette <silhouette@mrfz.dev>），Conventional Commits 中文提交。

### 架构决策

- **窗口任务制**：按实现路径（需求文档第 12 节）划分 W0-W8 窗口，每个窗口一个 worktree（`.worktrees/`，已 gitignore）+ feature 分支，与兄弟项目的多会话管理经验保持一致。
- **文档结构**：`docs/specs/`（设计规格）、`docs/plans/`（实施计划）、`docs/roadmap.md`（路线图）、`docs/devlog.md`（本日志）——沿用兄弟项目 docs 结构约定。
- **工作流**：全部开发走 Superpowers 流程，HARD-GATE：设计未经用户批准不写实现代码。
- **第一版范围**：不做复杂 Multi-Agent / Browser Agent / Kubernetes Sandbox / 复杂 Code KG，聚焦 Issue → PR 主链路（需求文档第 14 节）。

### 遗留问题

- GitHub remote 尚未配置（等待仓库地址确认后 `git remote add origin <url>`）。
- `workspace/demo-project` 将在 W1 窗口创建。
- LLM API 配置（模型 / Key 管理）将在 W1 的 spec 阶段确定。

### 下一步

- 进入 W1 窗口（本地 Workspace）：创建 worktree `feature/w1-local-workspace`，先 brainstorm 澄清设计，产出 spec 后经用户批准再实施。

## W1 本地 Workspace（2026-08-15）

### 完成内容

- **窗口**：worktree `.worktrees/w1-local-workspace`，分支 `feature/w1-local-workspace`。
- **设计**：brainstorming 澄清（交付形态=工具+最小 ReAct 循环；LLM=opencode go 的 deepseek-v4-flash；search_code=ripgrep；demo=用户提供真实项目）→ spec → plan（8 任务，全部 TDD）。
- **实现**（7 个实施任务，每任务独立 review）：
  - `tools/file_tools.py`：list_files / read_file（500KB 上限、带行号）/ search_code（ripgrep --json，RIPGREP_BIN 可覆盖）/ write_file，全部带 workspace 路径安全校验，错误结构化返回 ToolError。
  - `agent/llm.py`：LLMClient 协议 + OpenAICompatClient（env: opencode_go_api / OPENCODE_GO_BASE_URL / OPENCODE_GO_MODEL）+ MockLLMClient。
  - `agent/loop.py`：最小 ReAct 循环 run_agent（10 轮上限、OpenAI tool-call 消息回注、错误注入不中断）。
  - `main.py` CLI + `.env.example` 模板。
- **测试**：23 项全绿（14 工具 + 3 LLM + 4 循环 + 2 CLI）。
- **真实演示**：`D:\AI project\camera man` 复制到 workspace/demo-project；真实 LLM（deepseek-v4-flash）闭环：list_files → read_file → write_file → 总结，在 storage.py 的 EventStore.record() 正确添加中文注释。

### 关键决策与经验

- **模型 ID 修正**：用户口述的 `deepseek-4-flash` 实际为 opencode go 的 **`deepseek-v4-flash`**（端点返回 ModelError 后经网络资料确认）。
- **沙箱环境限制（重要）**：
  - DSH 沙箱（workspace-write）将 0o700 目录锁死 → 常规 `python -m venv` 不可用；workaround：.venv 内置 sitecustomize 补丁（tempfile 重定向 .venv_tmp），pip 从基础解释器复制。**重建 venv 会破坏测试环境，后续窗口沿用现有 .venv。**
  - 沙箱阻断出站网络 → openai/python-dotenv 由 Task 5 从基础解释器复制进 venv（非 pip 规范安装，联网环境可重装）。
  - 沙箱子进程 PATH 无 rg → 测试/演示命令需 `$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"`。
- **计划文档缺陷**（实施中发现并已在代码修正，计划原文保留）：Task 2/3/4/6 测试字面量缺 `ws.mkdir()`；Task 2 size 断言在 Windows CRLF 下不成立（改 read_bytes 独立计算）；Task 6 `_result_to_text` 的 `asdict(list)` 会抛 TypeError（补 list 分支）。
- 项目统一使用 worktree 内 `.venv`；`.venv_tmp/` 为运行时临时目录（gitignored 语义，未提交）。

### 遗留问题

- demo-project（camera man 副本）含 `.gitignore`，但 Agent 演示未涉及 git 操作（W2 引入）。
- 联网环境建议执行 `pip install -e ".[dev]"` 使依赖规范可复现。
- W2 起 LangGraph 引入时机待 W2 spec 阶段决策。
