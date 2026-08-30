# 技术栈、知识内容与 Docker 部署指南

> 生成日期：2026-08-30。基线：main @ 69554cd。
> 配套文档：《[项目全景梳理](2026-08-30-project-overview.md)》《[代码审查报告](2026-08-30-code-review-report.md)》。

---

## 一、技术栈总表

| 层级 | 技术 | 说明 |
|------|------|------|
| 语言/运行时 | Python 3.12+ | 全部中文注释，UTF-8；Windows 宿主 + Linux 容器双环境 |
| Agent 编排 | LangGraph ≥1.2（StateGraph） | 显式阶段图 plan→decide⇄execute→verify→reflect→finalize |
| LLM 接入 | openai ≥1.40（OpenAI 兼容协议） | 双供应商：opencode_go（默认 mimo-v2.5）/ deepseek（deepseek-v4-flash）；超时/重试 SDK 原生参数 |
| 配置 | python-dotenv ≥1.0 + 环境变量 | 全部 `KA_*` 前缀，`.env` 不入库 |
| 数据模型/校验 | dataclasses + 手写 schema 校验 | LLM 输出经结构解析后进入系统；案例 JSON 逐键校验 |
| 代码理解 | Python AST（stdlib）+ ripgrep | Tree-sitter 调研后否决（重/依赖深）；`RIPGREP_BIN` 可覆盖 |
| 隔离运行时 | Docker（Docker Desktop/WSL2） | 一个任务一个沙箱，六维资源限制 |
| GitHub 通道 | gh CLI（宿主执行）+ git | 凭据不进沙箱；`GH_TOKEN` 或 `gh auth login` |
| 知识检索 | MCP（stdio 子进程） | 经兄弟项目 Arknights LLM Wiki 的 MCP 客户端 |
| 评测 | 自定义 benchmark 包 + LLM-as-a-Judge + 规则判定器 | 五类案例；双口径 Resolution Rate |
| 可观测 | Langfuse v4 + OpenTelemetry + ClickHouse | SDK 4.14.4 经 OTLP 上报；events_only 模式落 events_core |
| 观测存储栈 | docker compose 六容器 | langfuse-web/worker + postgres + clickhouse + redis + minio |
| 测试 | pytest ≥8.0（TDD，red→green） | 标记：docker / mcp / kg 条件跳过 |

### 1.1 依赖清单（pyproject.toml）

```toml
dependencies = ["openai>=1.40", "python-dotenv>=1.0", "langgraph>=1.2"]
[project.optional-dependencies]
dev  = ["pytest>=8.0"]                      # 开发/测试
eval = ["langfuse>=4.0", "clickhouse-connect>=1.7"]   # 评测与 trace 导出
```

安装：`pip install -e ".[dev]"`（评测/导出另需 `.[eval]`）。

### 1.2 代码地图（约 5400 行源码）

```text
main.py (293)            CLI：--graph/--issue/--approve/--correct/--benchmark/--compare/--trace-report 七模式分发
agent/
  graph.py (505)         LangGraph 状态图（核心）：节点/降级/上下文压缩/预算纪律/红线规则注入
  issue.py (451)         Issue 全链路编排：红线拦截/Reviewer/审批单生成/条件自动放行
  loop.py (266)          最小 ReAct 循环（W1 保留回归）
  llm.py (194)           LLM 客户端（双供应商/埋点/Mock）
  correct.py (150)       知识抽查编排（纯规则层）
tools/
  shell_tools.py (403)   命令/测试/git 工具 + Executor 协议双轨 + 破坏性 git 拦截
  docker_sandbox.py (289) 沙箱生命周期与六维限制
  knowledge_audit.py (267) 知识抽查规则层
  file_tools.py (247)    文件五工具 + workspace 路径守门
  report_trace.py (248)  trace 导出（SDK 优先 + ClickHouse 回退）
  approval.py (207)      审批单 schema/状态机/漂移检查/执行
  github_tools.py (164)  gh CLI 封装 + git 写操作
  code_parser.py (149)   AST 解析
  knowledge_client.py (106) MCP 子进程适配
  code_graph.py (105)    内存代码图五类查询
  tracing.py (96)        Langfuse 可开关埋点
benchmark/
  domain_checks.py (463) 域案例规则判定器（deletions/bridge）
  runner.py (365)        评测运行器（基线预检/双判定/指标）
  report.py (199)        报告与版本对比
  loader.py (147)        案例加载与 schema 校验
  judge.py (48)          LLM-as-a-Judge
  cases/                 5 个基准案例 JSON + gold/（社区已合并 PR diff）
scripts/fetch_node.py (67) 镜像构建用 node tar 并行分块下载
Dockerfile               沙箱基础镜像 ka-sandbox:py312-v1
docker/langfuse/docker-compose.yml  Langfuse v4 六容器观测栈
```

---

## 二、配置面（.env.example 全变量）

复制 `.env.example` 为 `.env` 填写。分组如下：

| 分组 | 变量 | 默认/说明 |
|------|------|-----------|
| LLM 基础 | `opencode_go_api`（小写键，历史约定） | API Key（必填） |
| | `OPENCODE_GO_BASE_URL` / `OPENCODE_GO_MODEL` | https://opencode.ai/zen/go/v1 / mimo-v2.5 |
| W8 供应商 | `KA_LLM_PROVIDER` | opencode_go \| deepseek；deepseek 需 `deepseek_api` + `DEEPSEEK_BASE_URL` + `DEEPSEEK_MODEL` |
| LLM 行为 | `KA_LLM_TIMEOUT_S` / `KA_LLM_MAX_RETRIES` | **部署必须显式设 timeout（建议 120）**——未设时 SDK 默认 600s，长任务单次请求挂起会拖垮整轮迭代 |
| W3 沙箱 | `KA_EXECUTOR` | local（默认）/ docker |
| | `KA_SANDBOX_IMAGE` / `_CPUS` / `_MEMORY` / `_PIDS` / `_NETWORK` / `_REPO_URL` | ka-sandbox:py312-v1 / 2 / 1g / 512 / 0（任务级 opt-in）/ 空 |
| W4 知识 | `ARKNIGHTS_USE_MCP` / `ARKNIGHTS_WIKI_DIR` / `KA_CODE_INDEX` | 启用域知识检索 / 兄弟项目根目录 / 任务启动时构建代码索引 |
| W5 GitHub | `GH_TOKEN` / `KA_GITHUB_BRANCH_PREFIX` / `KA_APPROVAL_DIR` | 或 gh auth login / fix/issue- / output/approvals |
| W5 抽查 | `KA_CORRECT_OUT` | output/correction（--audit-out 覆盖） |
| W8 补强 | `KA_ISSUE_SETUP_COMMANDS` | 沙箱创建后逐条执行的环境准备（&& 分隔） |
| | `KA_TEST_TIMEOUT_S` / `KA_REDLINE_PATTERNS` | run_tests 超时（大套件调大）/ 红线模式覆盖（逗号分隔） |
| 评测 | `KA_SETUP_TIMEOUT_S` | case.setup_commands 每条超时（默认 60） |
| W6/W7 可观测 | `KA_TRACING` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | 1 启用；三键齐备才启用（或兄弟项目 .env 的 LANGFUSE_INIT_PROJECT_* 运行时映射） |
| | `CLICKHOUSE_PASSWORD` | `--trace-report` 回退直查 events_core 用 |

---

## 三、知识内容体系

### 3.1 双知识检索（项目核心特色）

Agent 在 decide 阶段同时持有两类知识工具：

1. **代码知识（Code KG，本仓库内构建）**：`main.py --graph` 启动时经 `KA_CODE_INDEX` 构建内存 CodeGraph——Python AST 遍历 workspace（排除 IGNORED_DIRS，语法错误跳过告警，60s 限时部分返回）→ 模块/类/函数节点 + import/inherits/calls 边 → 五类查询（calls/inheritance/imports/module_of/symbols）。查询空结果附自导航提示（引导转 symbols 模糊查询或 search_code 全文搜索）。
2. **领域知识（Arknights KG，兄弟项目经 MCP）**：`search_knowledge(kind, query)` → `ARKNIGHTS_USE_MCP=1` + `ARKNIGHTS_WIKI_DIR` 指向兄弟项目根 → 每次查询一个 stdio MCP 子进程（固定 `-c` 脚本 + argv JSON 传参，无 shell 防注入；UTF-8 reconfigure 解决 GBK 乱码）。kind 映射五类：character/event/concept/faction/location。

### 3.2 Arknights 知识库（兄弟项目数据）

- **数据规模**：三次提取产物统一条目模型实测 **11072 条**——event 4131 / character 2253 / concept 2170 / faction 1438 / location 1031 / timeline 49。
- **数据布局**（domain_checks 判定器依赖）：
  - `data/extractions/v3_seed_db_v2.json` / `v3_seed_db_v3_final.json`（concepts/factions/locations/timeline_events，含 related_*、source_records、aliases）
  - `data/extractions/v1_events/{main,side,special}/*.json`（events participants/location、条目 line_range 锚点）
  - `data/entity_source_map.json`（实体 → type/source_files/related_* 索引）
  - `data/extractions/v3_wiki/concepts/<名>.md`（条目 md，文件名即规范名）
- **抽查结论（W5 真实验收，ratio=0.02 seed=42，抽 224 条）**：来源可靠性 **96.88%**（217/217/7）；内容实用性 **70.98%**（159 used / 65 dead：concept 37 / faction 17 / location 8 / character 3）。
- **人工复核两发现**：① "dead" 是粗筛信号非结论——两类假阳性：有戏份但未被结构化引用；**代号↔真名命名脱节**（红松林事件 participants 用真名"玛莉娅"，章节清单用代号"瑕光"，别名无 bridge 导致整名匹配不命中）。② 组织名粒度（莱茵生命 9 事件文本提及，participants 只写"莱茵研究员"变体）。
- **域判定器**（E9）：域知识任务无 gold 可比，改用确定性规则——deletions（删除条目须同时满足：无来源锚点 ∧ 无事件参与 ∧ 结构化引用入度 0；删索引/种子文件判破坏性 FAIL）与 bridge（引用入度改前 git HEAD / 改后工作树对比，0→>0 为 bridge 建立 PASS）。判据与 knowledge_audit 的 W5 审计定义对齐。

### 3.3 基准案例知识（benchmark/cases/）

5 个案例全部来自真实仓库 dbader/schedule 的真实 Issue，gold patch 取自社区已合并 PR：

| case | 类别 | Issue | gold | 要点 |
|------|------|-------|------|------|
| schedule-608 | bug | #608 daylight saving bug | PR #623 | 唯一真实 resolved 案例（25 迭代） |
| schedule-646 | bug | #646 __repr__ unit=None 崩溃 | PR #653 | W5 演示锚点；Agent 方案与社区 PR #652 一致 |
| schedule-99 | feature | #99 workday/weekend 调度 | PR #656 | |
| schedule-602 | test | #602 补充时区测试 | PR #602 本身 | 开放型（默认 20 迭代收敛） |
| schedule-622 | refactor | #622 去 mock 依赖 | PR #622 本身 | |

domain 类案例**待补**（规则判定器 deletions/bridge 已就绪，可复用兄弟项目 Issue #4 题面）。

---

## 四、Docker 部署指南

本项目 Docker 有两套独立用途：**A. 沙箱执行镜像**（Agent 工作与测试的隔离环境，W3）；**B. Langfuse 观测栈**（trace 存储与 UI，W6/W7）。

### 4.1 A：沙箱镜像 ka-sandbox:py312-v1

**构建**（仓库根目录）：

```bash
docker build -t ka-sandbox:py312-v1 .
```

**Dockerfile 分层解析**：

| 层 | 内容 | 原因 |
|----|------|------|
| 基础 | python:3.12-slim | 浮动 tag（已知遗留：建议锁定 digest） |
| apt 换源 | deb.debian.org → mirrors.aliyun.com | 国内构建加速（用户批准偏离） |
| 系统包 | git curl xz-utils（--no-install-recommends） | 沙箱内 git 操作与下载工具 |
| Node.js 22 | `scripts/fetch_node.py` 8 连接 Range 并行分块下载 npmmirror CDN tar → 解压 /usr/local | 本机网络单连接限速 ~250KB/s 的 workaround（原生命周期管理：仅构建期需要） |
| Python 包 | pytest + pytz==2023.3（清华 PyPI 源） | pytz 为 schedule 时区测试群依赖（E1/P0-1a 修复——此前缺它致评测 test_pass 恒 False） |
| git 配置 | `core.filemode false`（--system） | 消除 Windows/Linux 文件权限位差异，容器内 git 行为与宿主一致 |
| WORKDIR | /workspace | 挂载点 |

**派生镜像经验（ka-sandbox:py312-v1x）**：第二轮实测后产生，pytest 8.3.5 钉版 + 常用测试依赖（pytest-timeout 等）+ setuptools/wheel；关键纪律：**不含与目标仓库同名的包**（dateutil 教训——镜像内预装 python-dateutil 会遮蔽目标仓库 editable install，产生假绿基线）。

**沙箱运行期机制**（docker_sandbox.py，无需人工介入但决定排障思路）：

- 生命周期：`create`（容器 run + 依赖安装 + 可选 clone，创建期有网）→ **`docker network disconnect bridge` 断网**（运行期默认无网；`KA_SANDBOX_NETWORK=1` 任务级 opt-in 保留）→ `exec`（命令包 `timeout -k 5s`：退出码 124=超时、137=OOM）→ `destroy`（幂等 + 容器侧清理 root 属主残留）。
- 六维限制：`--cpus`（KA_SANDBOX_CPUS）/ `--memory` + `--memory-swap` 同值禁 swap / `--pids-limit` / 网络断开 / 仅挂载 workspace + tmpfs /tmp / 秘密隔离（.env 不进容器）。
- 宿主↔容器路径换算：宿主 cwd 以 workspace 基准换算为容器 /workspace 下 POSIX 路径（run_tests(path) docker 模式此前必报越界，已修）。
- `KA_ISSUE_SETUP_COMMANDS`：沙箱创建后逐条执行的任务级环境准备（如 `pip install -e . --no-build-isolation`，dateutil src 布局实测暴露）。

### 4.2 B：Langfuse 观测栈（docker/langfuse/）

**拓扑**（官方 v4 compose，六容器 + 五个命名卷）：

| 容器 | 镜像 | 端口（宿主） | 角色 |
|------|------|--------------|------|
| langfuse-web | langfuse/langfuse:4 | **3000**（全接口） | UI 与摄取 API |
| langfuse-worker | langfuse/langfuse-worker:4 | 127.0.0.1:3030 | 异步摄取 worker |
| clickhouse | clickhouse-server:25.12 | 127.0.0.1:8123/9000 | 事件存储（events_core 为数据真实层） |
| postgres | postgres:17 | 127.0.0.1:5432 | Langfuse 元数据 |
| redis | redis:7 | 127.0.0.1:6379 | 队列（noeviction） |
| minio | chainguard/minio | 9090/127.0.0.1:9091 | 对象存储（events/media 桶） |

**部署步骤**：

```bash
cd docker/langfuse
cp .env.example .env        # 填写全部 # CHANGEME 项（见下）
docker compose up -d
# 健康后访问 http://localhost:3000
```

**必须修改的占位符（# CHANGEME 清单）**：`SALT`、`ENCRYPTION_KEY`（openssl rand -hex 32）、`CLICKHOUSE_PASSWORD`、`REDIS_AUTH`、`MINIO_ROOT_PASSWORD`、`NEXTAUTH_SECRET`、`DATABASE_URL`/`POSTGRES_PASSWORD`、S3 access/secret key。默认值仅限本机体验——**langfuse-web 的 3000 端口绑定在全部接口**，带默认密钥暴露到非本机网络有实际风险。

**与 Agent 的接线（headless 三键）**：本项目 SDK 三键支持两种来源——直接设 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL`，或复用兄弟项目 .env 的 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY/SECRET_KEY`（运行时映射，凭据零落盘）。`KA_TRACING=1` 显式启用（或三键存在即启用、`KA_TRACING=0` 强制禁用）。

**events_only 模式须知**：本部署为 Langfuse v4 events_only——经典读接口 `GET /api/public/traces` 返回 404，SDK 摄取正常；`--trace-report` 优先 SDK 读口、**失败自动回退 ClickHouse 直查 `events_core`**（后者为实测主路径，`CLICKHOUSE_PASSWORD` 需与 compose 一致）。

### 4.3 端到端部署步骤（新机器从零到跑通）

```text
1. Python 3.12+ 与 ripgrep（PATH 或 RIPGREP_BIN 指定）
2. Docker Desktop（WSL2 后端）并确认 docker info 可用
3. python -m venv .venv && .venv\Scripts\python -m pip install -e ".[dev]"   # 评测另装 .[eval]
4. gh CLI 安装并 gh auth login（或 GH_TOKEN）
5. cp .env.example .env —— 填 opencode_go_api、KA_LLM_TIMEOUT_S=120；评测用 KA_EXECUTOR=docker
6. docker build -t ka-sandbox:py312-v1 .                                    # 沙箱镜像
7.（可选观测）docker/langfuse: cp .env.example .env 改 CHANGEME → docker compose up -d
8. .venv\Scripts\python -m pytest tests/                                    # 基线回归
9. 试运行：python main.py "在 demo-project 中定位某函数并添加注释" --workspace workspace
10. Issue 全链路：python main.py "owner/name#123" --issue --max-iterations 40
    → python main.py --approve output/approvals/<审批单>.json --decision approve
```

### 4.4 已知部署注意事项（实战踩坑）

1. **`KA_LLM_TIMEOUT_S` 必须显式设置**（建议 120）：SDK 默认 600s，单次请求挂起会拖垮整轮迭代。
2. **网络代理**：git 全局 http.proxy 指向已停的 Clash 曾致克隆全灭——运行评测前确认代理在线或清除；github.com 主站 SNI 阻断时用镜像重写 `git config --global url."https://ghfast.top/https://github.com/".insteadOf "https://github.com/"`；git schannel 报 SEC_E_NO_CREDENTIALS 时注入 `GIT_CONFIG_*` 环境变量切 `http.sslBackend=openssl`。
3. **LLM 端点直连**：系统代理下 opencode.ai 偶发 TLS 握手超时，`.env` 加 `NO_PROXY=opencode.ai` 解决。
4. **pip/git 加速**：容器内已配清华 PyPI 源与 aliyun apt 源；宿主 pip 走清华镜像。
5. **Windows 执行器**：dbader/schedule 的测试在 Windows 宿主因 `time.tzset()` 崩溃——评测类任务必须 `KA_EXECUTOR=docker`（Linux 容器）。
6. **沙箱镜像无版本锁**：python:3.12-slim 浮动 tag + apt/pip 未固定，重建镜像可能漂移（已知遗留，建议锁定）。
7. **fetch_node.py 无 sha256 校验**（仅总字节数核对）、失败 part 文件残留在容器内（瞬态，已知遗留）。
8. **venv 健康检查**：发现 dist-info 在而模块缺的损坏包用 `pip install --force-reinstall` 修。

---

## 五、常用命令速查

```bash
# 测试
.venv\Scripts\python -m pytest tests/ -v          # 全量（docker/mcp/kg 标记项按环境跳过）
pytest tests/test_graph.py -v                     # 单文件

# Agent 基础
python main.py "任务描述" --workspace workspace   # graph 模式（默认 9+2 工具编排）
python main.py "任务描述" --executor docker       # 强制容器沙箱

# Issue 全链路（两段式，推送唯一入口）
python main.py "owner/name#123" --issue --max-iterations 40          # 干跑产单停等
python main.py --approve output/approvals/<审批单>.json --decision approve   # 人工批准 → push + PR
python main.py --approve <审批单>.json --decision reject                      # 拒绝（零远端副作用）

# 评测 / 对比 / Trace 导出（需 .[eval] 与 docker 执行器）
python main.py --benchmark --executor docker
python main.py --compare <run_id>
python main.py --trace-report <trace_id> --trace-out output/trace

# 知识抽查（纯规则层，无需 LLM Key）
python main.py --correct --wiki-dir "D:\AI project\Arknights LLM Wiki" --audit-ratio 0.02
```
