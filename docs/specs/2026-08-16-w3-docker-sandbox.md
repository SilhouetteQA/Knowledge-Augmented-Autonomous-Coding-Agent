# W3 Docker Sandbox 设计规格

> 日期：2026-08-16
> 状态：已批准（brainstorming 产出，用户批准后进入 writing-plans）
> 关联：`docs/roadmap.md` W3 窗口、`docs/sessions/2026-08-15-w1w2-session.md` W3 入口指引

## 1. 背景与目标

W1/W2 的命令执行（`run_command` / `run_tests` / git 三件套）均以 subprocess 在宿主机 Windows 上运行，存在：无隔离（命令以用户身份可读写任意路径）、无资源限制（仅 timeout 杀进程树，W2 已踩 Windows 孤儿进程坑）、环境不可复现（Windows 语义）等问题。

W3 目标：**用 Docker 容器替换 subprocess 执行**，一个 Task 一个独立 Sandbox，实现 timeout / CPU / memory / network / filesystem / secrets 六项隔离与限制，为 W5（GitHub Issue Agent 对接真实 Linux 仓库）铺路。

## 2. 验收标准（源自 roadmap）

1. 同一任务的全部命令（run_command / run_tests / git 三件套）在容器内执行
2. 超时/超内存被正确终止并返回结构化错误
3. 容器销毁后无残留（`docker ps -a` 无该任务容器）

## 3. 设计决策（brainstorming 已确认）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | 接入方式 | 执行器抽象 + 配置切换（LocalExecutor / DockerExecutor） | Agent 任务走容器满足验收；既有 48 项测试保持 LocalExecutor 下全绿 |
| D2 | 镜像策略 | 仓库内 Dockerfile 自建镜像（python:3.12-slim + git + nodejs + npm + pytest） | 一次构建、环境统一、可入库复现 |
| D3 | 网络策略 | 创建阶段开网（装依赖/克隆），运行阶段 `--network none`，任务级 opt-in | 依赖一次性装好，运行期隔离防外泄 |
| D4 | git 工具 | git 三件套也进容器；创建时注入 `git config --global core.filemode=false` | 字面满足验收；消除 Windows/Linux 文件权限位差异噪音 |

## 4. 架构与组件

```
tools/docker_sandbox.py        # 新增：SandboxConfig / SandboxManager（生命周期）/ DockerExecutor / sandbox_executor 上下文
tools/shell_tools.py           # 修改：抽出 Executor 接口，run_command/run_tests/git 经执行器分发
tools/file_tools.py            # 不变：文件工具仍在宿主机操作 workspace（bind mount 实时同步）
agent/loop.py                  # 微调：run_agent 以 sandbox_executor 上下文包裹循环
agent/graph.py                 # 微调：run_agent_graph 以 sandbox_executor 上下文包裹 invoke
main.py                        # 新增：--executor 参数（透传 KA_EXECUTOR）
Dockerfile                     # 新增：沙箱基础镜像
```

执行器在任务内由 `sandbox_executor(workspace_root)` 上下文提供（docker 时创建/销毁沙箱，
local 时直接返回 LocalExecutor），`shell_tools` 内部经 ContextVar 获取当前执行器——
`_dispatch` / `_graph_dispatch` 无需改动。

### 4.1 Executor 接口（seam）

```python
class Executor(Protocol):
    def run_command(command: str, cwd: str | None, timeout: int,
                    workspace_root: str | None) -> CommandResult | ToolError: ...
    def run_tests(path: str | None, workspace_root: str | None) -> TestResult | ToolError: ...
    def run_git(args: list[str], workspace_root: str | None) -> str | ToolError: ...
```

- `LocalExecutor`：现有 subprocess 实现原样迁移，行为不变
- `DockerExecutor`：容器 exec 实现
- git 三件套经执行器的 `run_git` 实现（容器内 git），LocalExecutor 复用现有 `_run_git`（list 参数、无 shell，避免引号问题），DockerExecutor 以 shlex 拼接后容器内执行
- `CommandResult` 新增 `oom: bool = False` 字段（OOM 语义标记，默认值保持向后兼容）

### 4.2 执行器选择

- 环境变量 `KA_EXECUTOR=local|docker`（默认 `local`）
- `main.py` 增加 `--executor` CLI 参数覆盖
- 选择逻辑在 `shell_tools.py` 的模块级工厂（`get_executor()`），`run_command` / `run_tests` / git 工具内部经工厂获取执行器

## 5. Sandbox 生命周期（一个 Task 一个 Sandbox）

### 5.1 Create（任务启动）

1. 检查镜像是否存在，不存在则 `docker build`（Dockerfile 在仓库根，tag `ka-sandbox:py312-v1`，可用 `KA_SANDBOX_IMAGE` 覆盖）
2. `docker run -d` 创建容器：
   - 名称：`ka-sandbox-<workspace-basename>-<sha1(workspace路径)[:8]>`
   - 挂载：workspace 根 → `/workspace`（rw）
   - `--tmpfs /tmp`
   - 资源限制见第 6 节
   - 创建阶段网络开
3. 依赖安装（存在 `requirements.txt` 时）：容器内 `pip install -r /workspace/requirements.txt`
4. 可选克隆：workspace 为空且配置了 repo_url 时，容器内 `git clone <repo_url> /workspace`
5. 容器内 git 配置：镜像构建时已注入 `git config --system core.filemode false`（Dockerfile RUN）

### 5.2 Execute

- 每条命令：`docker exec <container> bash -lc '<command>'`
- cwd：命令相对 workspace 根解析，容器内对应 `/workspace/<相对路径>`
- 超时由容器内 GNU `timeout` 包裹（见第 6 节）

### 5.3 Destroy（任务结束）

- try/finally 保证执行 `docker rm -f <container>`
- 清理后断言无残留（集成测试验证）

## 6. 资源限制

| 限制 | 实现 | 默认值 |
|------|------|--------|
| timeout | 容器内 `timeout -s KILL -k 5s <sec> bash -lc '<cmd>'` | 60s（命令级，与 W2 一致） |
| CPU | `--cpus` | 2 |
| 内存 | `--memory` + `--memory-swap`（同值，禁 swap） | 1g |
| 进程数 | `--pids-limit` | 512 |
| 网络 | 运行阶段 `--network none`；创建阶段默认网络 | 禁网 |
| 文件系统 | 仅挂载 workspace（rw）+ tmpfs /tmp；容器内其他写盘随销毁清除 | — |
| 秘密 | 不继承宿主环境变量、不挂载 .env、容器内无宿主凭据 | — |

配置项（env）：`KA_SANDBOX_CPUS`（默认 2）、`KA_SANDBOX_MEMORY`（默认 1g）、`KA_SANDBOX_PIDS`（默认 512）、`KA_SANDBOX_NETWORK`（默认 0；1 = 创建时容器带网络，任务级 opt-in）、`KA_SANDBOX_REPO_URL`（可选；workspace 为空时创建阶段克隆该仓库到 /workspace）、`KA_SANDBOX_IMAGE`（默认 ka-sandbox:py312-v1，镜像缺失时自动 docker build）。

## 7. 错误处理与返回语义

| 场景 | 返回 |
|------|------|
| 命令超时 | `CommandResult(timeout=True, exit_code=-1)`（与 W2 语义一致），含已捕获 stdout/stderr |
| 内存超限（OOM 被杀） | exec 返回 137 → `CommandResult(exit_code=137)` + stderr 原文，标注 oom 语义 |
| Docker 不可用（配置了 docker 但 daemon 未运行） | `ToolError`，明确信息（不自动降级到 LocalExecutor——按工程约束不写 fallback） |
| 容器未创建即调用 exec | `ToolError`（SandboxManager 保证生命周期顺序，防御性校验） |
| 输出超长 | 与 W2 相同的 100KB 截断逻辑 |

## 8. 测试策略（TDD，seam = Executor 接口 + CommandResult）

1. **单元测试**（不依赖真实 Docker）：
   - FakeDocker/MockExecutor：生命周期编排（create→exec→destroy 顺序）、配置解析（env 默认值与覆盖）、错误路径（docker 不可用、容器缺失）
   - LocalExecutor 回归：既有 48 项测试保持全绿（原样迁移）
2. **集成测试**（`@pytest.mark.docker`，模块级 `skipif` Docker 不可用）：
   - 真实容器：创建（含 requirements.txt 自动安装）→ 命令执行 → 销毁无残留
   - 超时：`sleep 10` + timeout=2 → `timeout=True`
   - 内存：`--memory 64m` 容器内分配超限内存 → exit 137
   - 网络隔离：运行阶段容器内 `curl/网络请求` 失败（禁网生效）
   - git：容器内 git status/diff/log 与宿主结果一致（filemode 注入生效）
3. **执行器切换**：同一测试在 local/docker 两执行器下输出一致（核心命令路径）

## 9. 非目标（YAGNI，第一版不做）

- 不做多镜像按任务路由（固定 ka-sandbox 镜像）
- 不做容器内文件工具（文件工具保持宿主，bind mount 同步）
- 不做 git 写操作（commit/push 留 W5）
- 不做 Kubernetes Sandbox（需求文档第 14 节明确排除）
- 不做资源限制的 GUI 配置（env 配置即可）

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Windows 挂载目录在 Linux 容器中的权限/换行差异 | workspace 挂载 rw + `core.filemode=false`；换行差异由仓库自身 .gitattributes 决定 |
| 首次 `docker build` 需网络（apt/pip） | 构建一次长期缓存；本机网络已验证可用 |
| 容器内 pip 安装依赖可能与宿主环境版本冲突 | 依赖只在容器内生效，互不影响；requirements.txt 以仓库为准 |
| `docker exec` 无内建 timeout | 容器内 GNU timeout 包裹（debian 系镜像自带 coreutils） |
