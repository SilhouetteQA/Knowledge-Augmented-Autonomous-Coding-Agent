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

## W2 Shell + Test（2026-08-15）

### 完成内容

- **窗口**：worktree `.worktrees/w2-shell-test`，分支 `feature/w2-shell-test`（6 任务 + 1 补缺，全部 TDD + 双审查）。
- **新工具**（`tools/shell_tools.py`）：
  - `run_command`：任意命令 + workspace cwd 约束 + 60s 超时（Win32 进程树终止，Windows 下 shell=True 孤儿进程问题的必要偏离）+ 100KB 截断。
  - `run_tests`：pytest 封装 → 结构化 TestResult（passed/failed/error/total/duration/failures）。
  - git 只读三件套：`git_status` / `git_diff` / `git_log`。
- **LangGraph 编排**（`agent/graph.py`）：显式阶段图 plan → decide ⇄ execute → **verify（强制测试）** → reflect（失败分析）→ finalize；迭代上限 20、验证轮上限 3；`run_agent_graph` 入口。
- **工具注册补齐**（Task 6.5）：演示发现 decide 只暴露 W1 四工具，补齐 8 工具（run_command/run_tests/git 三件套入 schema 与 dispatch）。
- **测试**：47 项全绿（W1 27 + W2 20）。
- **真实演示**（bilibili defect-repo）：三轮运行后完整闭环——Agent 定位根因（conftest.py 缺失 → pytest sys.path 不含项目根 → 79 测试全挂）→ 最小修复 → verify 79 passed；最终运行中 Agent 主动做因果实验（.bak 复现 8 failed → 恢复 → 8 passed）。

### 关键决策与经验

- **LangGraph 引入**（用户拍板）：W2 用 StateGraph 显式编排，plan/verify/reflect 节点化，满足需求 U-06 显式规划；W1 的 run_agent 保留回归。
- **verify 强制闭环**：Agent 声称完成后图自动跑测试，失败进 reflect 重试——测试不是可选项。
- **迭代上限语义**：brief 实现 verbatim 下 `iteration_count` 计 decide 决策次数（20 次执行 + 1 次 guard = 21）；功能上执行轮数上限 20 保持。
- **真实发现（W2 限制，W4 方向）**：
  1. 迭代上限 20 对复杂真实任务偏紧（Agent 探索阶段消耗大）。
  2. Agent 探索策略低效（第一轮读 77 个文件，大量无关）——W4 Repository Intelligence 代码索引可改善。
  3. plan 节点只做流程拆解不做根因识别；根因分析依赖 decide 循环 LLM 自由探索（效率与稳定性受模型影响）。
- **环境**：venv 复制重建 + langgraph 1.2.6/fastapi 等 vendoring（断网）；rg 在本会话 PATH 可用；`GIT_CEILING_DIRECTORIES` 用于非仓库测试场景。

### 遗留问题

- **bilibili 业务缺陷未深入**（用户反馈"AI 日报生成问题很大"）：项目会话记录显示真实失败历史（图片 JSON 50% / 网络超时 30% / 重启 / HTTP 500；A/B/C 三个已知未修根因：lifespan 内存不重置、轮询不处理部分完成、图片 JSON 容错）。测试全绿不覆盖真实链路（LLM/公众号 API/cron/网络）。待用户确认是否需要继续排查。
- 修复方案 conftest.py 仅在 defect-repo 副本，原仓库 `D:\AI project\bilibili` 未动（待用户决定是否应用）。
- defect-repo 演示副本不入库（同 W1 决策），收尾时清理。
- `.venv_tmp/`、`demo-w2-*.txt` 等演示残留待清理。

### 会话归档与新会话指引

- 完整会话归档：`docs/sessions/2026-08-15-w1w2-session.md`（时间线、决策、指标、环境备忘、W3 入口指引——新会话先读它）。
- **LLM 配置**（.env 重建）：`opencode_go_api`（User 环境变量）+ `OPENCODE_GO_BASE_URL=https://opencode.ai/zen/go/v1` + `OPENCODE_GO_MODEL=deepseek-v4-flash`。
- **W3 前置**：Docker 未安装（2026-08-15 检查），需先装 Docker Desktop。

## W3 前置：Docker 安装完成（2026-08-16）

### 完成内容

- **Docker Desktop 4.86.0 安装到 `D:\Docker`**（`--installation-dir=D:\Docker --backend=wsl-2 --quiet --accept-license`），验证通过：`docker --version`（Client/Server 29.7.2）、`docker info`（12 CPU / 8GB 内存 / Linux/WSL2 引擎）、`docker run hello-world` 成功。
- **WSL2 环境修复**：本机原本缺 WSL2 内核且 VirtualMachinePlatform 未启用；已启用 `Microsoft-Windows-Subsystem-Linux` + `VirtualMachinePlatform`（dism，无重启需求），安装 WSL2 内核（wsl_update_x64.msi，内核 6.6.87.2-1），WSL 2.6.3.0。Ubuntu（WSL2）发行版实测可用。
- **坑 1（孤儿注册表项）**：4 月曾安装过 Docker Desktop 4.68.0 但目录已删除（当时无 WSL2 内核，从未正常运行），残留 `HKLM\...\Uninstall\Docker Desktop` 注册项导致新安装器 4.86 直接退出 -5（0xFFFFFFFB，检测到已安装）。删除残留项后安装成功。
- **坑 2（沙箱网络限制）**：DSH 沙箱阻断出站网络，且 Docker CDN 单连接仅 ~250KB/s。解法：提权（danger-full-access）后 curl 6 连接分块并行下载（`-r` Range 分段，~5MB/s），个别分块 SSL 中断用 `--retry-all-errors` 重试后合并（SHA256 校验一致）。
- **坑 3（WSL 数据迁移，重要）**：Docker Desktop 4.86 的 WSL 数据目录设置键为 **`CustomWslDistroDir`**（settings-store.json）。直接改配置文件写 `vm.resources.wslDataFolder` / `dataFolder` / `DataFolder` 均无效（后端日志标记 unknown 或被默认值覆盖）；**唯一可靠方式是通过 GUI**：Settings → Resources → Advanced → Disk image location → 选目标目录 → Apply & Restart（确认 Move disk image?）。迁移后：程序在 `D:\Docker`，数据在 `D:\Docker\data\DockerDesktopWSL`（WSL 注册 BasePath 指向 D 盘，hello-world 镜像完整保留）。
- **W3 窗口已就绪**：worktree `.worktrees/w3-docker-sandbox`（分支 `feature/w3-docker-sandbox`）已创建；`.env` 已重建；venv 从 W1 残留 worktree 复制 + 从 `D:\CodexPython312` vendoring langgraph 1.2.6 家族（含 langsmith/requests 等传递依赖，jsonpatch 为单文件模块需单独复制）；**48 项测试全绿**。
- 安装脚本与日志留存在 `D:\DockerInstall\`（prepare-wsl.ps1 / install-docker3.ps1 / 各 .log）。

### 下一步

- 进入 W3 窗口流程：brainstorming 澄清 Sandbox 生命周期与资源限制设计 → spec → 用户批准 → plan → TDD 实施。

## W3 Docker Sandbox 完成（2026-08-17）

### 完成内容

- **窗口**：worktree `.worktrees/w3-docker-sandbox`，分支 `feature/w3-docker-sandbox`（13 commits），合并回 main（b8fae3c，88 测试全绿）。
- **设计**：brainstorming 澄清 4 项决策（执行器抽象+配置切换 / 自建镜像 / 创建期开网运行时禁网 / git 进容器）→ spec → plan（7 任务，全代码 TDD）→ subagent-driven 实施（每任务独立实现+双轴审查，共 14 个子代理轮次）。
- **实现**：
  - `tools/shell_tools.py`：Executor 协议 + LocalExecutor（原 subprocess 行为不变）+ get_executor（ContextVar + KA_EXECUTOR）+ CommandResult.oom
  - `tools/docker_sandbox.py`：SandboxConfig/get_sandbox_config、SandboxManager（create→exec→destroy，资源限制全参数）、DockerExecutor、sandbox_executor 上下文（一个任务一个沙箱，创建失败亦销毁）
  - `agent/loop.py`/`agent/graph.py`：任务包进沙箱上下文；`main.py` 新增 `--executor` 与 `--graph` 开关（--graph 接入 9 工具编排，W3 验收缺口修复）
  - `Dockerfile` + `scripts/fetch_node.py` + `.dockerignore`；`tools/file_tools.py` ToolError 改 Exception 子类（用户裁决）
- **测试**：88 项（单元 64 + 集成 10 + 回归）；集成测试 @pytest.mark.docker 真实容器 10/10（生命周期/超时 124/OOM 137/网络隔离/挂载同步/git 一致/双执行器一致/创建期 pip+clone/无残留）。
- **真实演示**（camera man，容器化闭环）：Agent 全部命令在容器内执行；缺 numpy/cv2/mediapipe 依赖时自主编写纯 Python 降级模块 + conftest 修复 → 容器内 51 测试全绿 → verify 门禁通过 → 中文总结闭环；无残留。

### 关键决策与经验

- **执行器抽象**：LocalExecutor/DockerExecutor 双轨，KA_EXECUTOR 切换；docker 模式无 fallback（不可用即 ToolError）。
- **网络策略实现**：`--network none` 在创建期即生效会断掉 pip/clone → 改为「默认桥接创建 → setup 完成后 `docker network disconnect bridge`」，network=True 时不切断（opt-in）。
- **超时判定**：GNU `timeout -s KILL` 恒返 137（与 OOM 同码且永不判中）→ 改用 `timeout -k 5s`（TERM→5s 宽限 KILL），超时返 124、OOM 返 137；TERM 免疫进程被宽限 KILL 仍 137 为已知边角（spec §7 已注，W5 可用 docker inspect OOMKilled 判别）。
- **本网络单连接限速 ~250KB/s**（apt/pip 单连接下载 150MB 需 30-40 分钟且停滞）→ 镜像构建全面并行化：apt 只装小包 + node 官方 tar 用 `scripts/fetch_node.py` 8 连接 Range 分块下载 + 清华 pip 源；全镜像构建 3.9 分钟。
- **沙箱环境限制复盘**：DSH 沙箱阻断出站网络/命名管道；venv 复制重建（mypyc 编译包需连 .pyd 一起复制）；docker CLI 不在默认 PATH（需刷新 Machine PATH）；集成测试在 danger-full-access 下可正常跑。
- **main.py 历史缺口**：W1 起连 loop（4 文件工具），graph（9 工具）从未接 CLI——W3 演示暴露后加 `--graph` 开关（用户批准）。
- **最终审查发现并修复**：sandbox_executor 的 create() 原在 try/finally 外，创建期失败会泄漏容器且确定性容器名导致下个任务 --name 冲突（已修复 + 回归测试）。
- 子代理执行要点：实施/审查/修复均子代理化（简报文件传递、审查包 diff 文件、账本 .superpowers/sdd/progress.md 跟踪）；简报缺陷（ToolError 非异常、timeout -s KILL、--network none、测试 env 泄漏等）由实施者发现 → 控制器裁决 → 用户批准。

### 遗留问题

- 沙箱镜像未锁定版本（python:3.12-slim 浮动 tag + apt/pip 未固定）——重建镜像可能漂移；W5 前建议锁定。
- `fetch_node.py` 无 sha256 校验（tar 解压兜底）；失败时 part 文件未清理（容器内瞬态）。
- 集成测试 2 项依赖公网（pip six / GitHub Hello-World 克隆）——易碎外部依赖。
- git filemode 一致性测试未真正加压（建议 chmod +x 变体）。
- demo 副本与日志（workspace/demo-project、graph-demo-*.log）未入库、未清理残留于主仓库 workspace 之外——已随 worktree 删除；主仓库 workspace 仅 .gitkeep。
- 主仓库 `.venv` 已从 W3 worktree 复制完整版（原残缺），后续窗口沿用。

### 下一步

- W4（Repository Intelligence，可与 W5 并行）或 W5（GitHub Issue Agent，依赖 W3 已满足）。

## W4 Repository Intelligence 完成（2026-08-19）

### 完成内容

- **窗口**：worktree `.worktrees/w4-repo-intelligence`，分支 `feature/w4-repo-intelligence`（10 commits），合并回 main（113 测试全绿，其中 MCP 集成 3 项实跑）。
- **设计**：brainstorming 澄清（Python AST 选型（Tree-sitter 重/依赖深，否决）/ 内存 CodeGraph / 双知识源注入图）/ spec → plan（9 任务，全 TDD）→ 实施（Task 5 依赖倒置提前于 Task 4）。
- **实现**：
  - `tools/code_parser.py`：parse_python_file（AST）+ build_metadata（全工作区遍历，排除 IGNORED_DIRS，语法错误/读取失败跳过并告警，60s 限时返回部分结果）
  - `tools/code_graph.py`：CodeGraph 内存图（module/class/function 节点，import/inherits/calls 边）+ `query_code_graph` 分发五类查询（calls / inheritance / imports / module_of / symbols），空结果附自导航提示
  - `tools/knowledge_client.py`：KnowledgeClient 族（Mock 独立 + ArknightsMCPClient 子进程适配 + get_knowledge_client 工厂，`ARKNIGHTS_USE_MCP`/`ARKNIGHTS_WIKI_DIR` 环境开关；`_KIND_TOOL` 映射与兄弟项目 mcp_server 工具参数逐一核验；无 venv 回退 PATH python；子进程 stdout reconfigure(utf-8) 系统解决 GBK 乱码）
  - `agent/graph.py`：注入 code_graph/knowledge_client 两 ToolSpec（search/knowledge 双知识工具），`_decide_system` 提示词显式引用可用查询工具；`agent/loop.py` `_result_to_text` 兼容 str/list
  - `main.py --graph` 一键构建双知识源（`KA_CODE_INDEX`，构建失败静默回退 None→工具调用时 ToolError 不中止图）；`pyproject.toml` 加 `mcp` marker；`.env.example` 配置注释
- **测试**：113 项全绿（新增 code_parser 8 / code_graph 11 / knowledge_client 7 / graph 3 / MCP 集成 3；`@pytest.mark.mcp` 条件跳过，配置 env 时实跑）。
- **真实演示**（`workspace/w4-demo` 兄弟项目副本）：Agent 依次使用 query_code_graph（symbols → calls → imports）与 search_knowledge（阿米娅），阅读 entity_repository.py / character_aggregator.py / seed.py / identity_map.json 后给出结论——同一角色跨章节不会重复创建（多层去重：Seed 主键 + resolve_name/get_by_name 四层判重 + seed NPC 前置检查 + normalize_and_merge（id_map + 干员名 + 模糊匹配 ≤0.6）+ build_entity_index._ensure + EntityIndexStore O(1) lookup）；Pass 1 事件按章节存储为设计而非重复。

### 关键决策与经验

- **解析选型**：Python AST（stdlib，无重依赖）+ ripgrep 已覆盖需求；Tree-sitter 调研后否决。
- **双知识源注入**：build_graph 闭包注入，未动既有节点逻辑；main.py 仅 11 行增量。
- **MCP 适配**：不直接复用兄弟项目 client 代码（依赖隔离），改为每次查询一个子进程（python -c 固定脚本 + argv JSON 传参，无 shell，防注入）。
- **审查后修复（needs-fixes → approve）**：① `_decide_system` 增加可用查询工具一行（spec §4.4 验收标准 1 核心）；② `query_code_graph` 空结果附「未找到…可用 symbols 模糊查询或 search_code 全文搜索」自导航提示（spec §7）；③ `_invoke`/build_metadata 补 OSError 兜底（工具错误返回结构化错误串/跳过文件，不抛异常）；④ 图内工具 docstring 8→11。
- **规格用词偏差（记录）**：spec §4.3「懒加载单例」实为工厂非单例（main.py 仅调用一次并注入，无实际影响）；未知 kind 静默按 entity 处理；MCP 运行时子进程失败返回字符串错误（图可观察，未做 ToolError 结构区分）——均为 v1 有意取舍。

### 遗留问题

- 演示产物 `workspace/w4-demo/`（兄弟项目副本，157 py）与 `w4-demo-run.log` 未入库，收尾时清理或加入忽略。
- 知识库查询未引入缓存（同一查询重复走子进程）；W5 若发现频繁可加进程级 lru_cache。
- Code KG 为内存态，无持久化/增量更新；大仓库（>2k 文件）构建耗时待 W6 Benchmark 评估。

### 下一步

- W5（GitHub Issue Agent）：worktree `.worktrees/w5-github-issue`（分支 `feature/w5-github-issue`，计划已入库）执行规划 7 大任务，目标完成 GitHub Issue → PR 全链路闭环（MVP 交付）。

## W5 GitHub Issue Agent 实施进行中（2026-08-25）

### 完成内容

- **窗口**：worktree `.worktrees/w5-github-issue`，分支 `feature/w5-github-issue`（Task 1-6 已提交 + 重复运行幂等修复，共 9 commits 待合并）。
- **实现**（按计划 7 大任务，TDD）：
  - `tools/github_tools.py`：gh CLI 封装（get_issue / get_repository / clone_repository / create_branch（-B 幂等）/ git_diff_since / commit_changes / push_branch / create_pull_request / comment_issue）+ `sync_repository`（仓库已存在时 fetch + checkout base + reset --hard origin/base + clean -fd，spec §4.2）；全部宿主执行，凭据不进沙箱。
  - `agent/issue.py`：`IssueTask` / `IssueAgentResult` / `run_issue_agent` 全链路编排（fetch issue → clone/sync → branch → run_agent_graph（沙箱工作）→ diff → LLM Review（PASS/FAIL 首行）→ FAIL 重试 1 轮 → push 门禁（`task.push` 才 commit+push+PR））；Review 提示词 REVIEW_PROMPT。
  - `main.py`：`--issue`（`<owner/name>#<number>` 格式）与 `--push` 开关 + `_run_issue_mode`（dry-run 无远端副作用）。
  - `agent/graph.py` 不变：`run_agent_graph` 内部 `sandbox_executor` 包裹，KA_EXECUTOR=docker 时工作与测试自动容器化。
- **测试**：非 docker 全量 **133 项全绿**（github_tools 15 / issue_agent 6 / main 含 issue 模式 2 项；Fake gh monkeypatch 不触网，git 写操作用本地临时仓库；重复运行回归 3 项）。
- **模型依赖**：本窗口已随主线迁移至 opencode_go + mimo-v2.5（见主仓库 devlog 迁移条目）。

### 关键决策与经验

- **凭据边界**：gh/git 写操作宿主执行；沙箱容器内仅只读 git 三件套 + 工作/测试（延续 W3 秘密隔离）。
- **Review 轻量化**：diff → LLM 审查出 PASS/FAIL 首行结论，FAIL 时审查意见回注重跑一轮（上限 1 次）；独立 Reviewer Agent 留 W8。
- **dry-run 安全阀**：默认不 push；`--push` 或 `KA_ISSUE_PUSH=1` 才 commit → push → PR（body 含验证轮数与审查结论）。
- **重复运行幂等**（审查前自检发现）：仓库已存在时 fetch+reset 丢弃残留未提交变更；`checkout -B` 重置同名分支——二次运行不污染（spec §4.2 合规补丁 e1c6a39）。
- **宿主环境检查**：gh CLI 2.92.0 已装但**未登录**（需 `gh auth login` 或 GH_TOKEN）；进程环境已含 opencode_go_api（User 级）。

### 遗留问题与待办（人工测试后继续）

- 真实演示 dry-run（待用户提供目标仓库 + Issue 号；二哥项目若无 GitHub remote 可选小型 Python 仓库）。
- 真实演示 push 模式（需用户有写权限的测试仓库 + open issue）。
- 双轴审查（requesting-code-review）→ 修复 → 全量回归。
- 更新 `docs/roadmap.md` W5 状态 → 合并回 main → 删除 worktree（收尾流程）。
