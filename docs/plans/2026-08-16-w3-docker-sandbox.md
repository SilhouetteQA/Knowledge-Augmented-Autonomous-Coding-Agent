# W3 Docker Sandbox 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 用 Docker 容器替换 subprocess 命令执行：一个 Task 一个 Sandbox（创建 → 执行 → 销毁），实现 timeout / CPU / memory / network / filesystem / secrets 六项限制，全部命令（run_command / run_tests / git 三件套）在容器内执行。

**Architecture:** `tools/shell_tools.py` 抽出 `Executor` 接口（`LocalExecutor` 保留现有 subprocess 行为，`DockerExecutor` 走容器）；`tools/docker_sandbox.py` 新增 `SandboxManager`（容器生命周期 + 资源限制）与 `sandbox_executor` 任务级上下文（一个 Task 一个 Sandbox）；`agent/loop.py` 与 `agent/graph.py` 用该上下文包裹任务循环；执行器经 ContextVar 与 env（`KA_EXECUTOR`）选择，`_dispatch` / `_graph_dispatch` 零改动。

**Tech Stack:** Python 3.12+ / Docker CLI（Desktop 4.86，引擎 29.7.2）/ 镜像 `ka-sandbox:py312-v1`（python:3.12-slim + git + nodejs + npm + pytest）/ pytest。

## Global Constraints

- 仓库根：`.worktrees/w3-docker-sandbox`；测试命令：`.venv\Scripts\python.exe -m pytest tests/`（工作目录 = 仓库根）
- 中文注释、UTF-8、无 emoji；Conventional Commits 中文提交
- 不写 fallback：`KA_EXECUTOR` 默认 `local`，`docker` 模式下 Docker 不可用必须返回结构化 `ToolError`，绝不静默降级
- 既有 48 项测试必须保持全绿（LocalExecutor 行为不变）
- 集成测试（`@pytest.mark.docker`）需要 Docker Desktop 运行中 + docker CLI 在 PATH
  （本机 docker CLI：`D:\Docker\resources\bin\docker.exe`；执行前刷新 PATH：`$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')`）
- 测试环境沿用现有 `.venv`（不可重建）；venv 内已 vendoring langgraph 1.2.6 家族
- 设计依据：`docs/specs/2026-08-16-w3-docker-sandbox.md`（已批准）

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `tools/shell_tools.py` | 修改 | `Executor` 协议、`LocalExecutor`、`get_executor()`、模块函数委托；`CommandResult` 加 `oom` 字段 |
| `tools/docker_sandbox.py` | 新增 | `SandboxConfig` / `get_sandbox_config` / `SandboxManager` / `DockerExecutor` / `sandbox_executor` 上下文 |
| `Dockerfile` | 新增 | 沙箱基础镜像（python:3.12-slim + git/nodejs/npm + pytest + core.filemode false） |
| `pyproject.toml` | 修改 | pytest `docker` marker 注册 |
| `agent/loop.py` | 修改 | `run_agent` 以 `sandbox_executor` 包裹 |
| `agent/graph.py` | 修改 | `run_agent_graph` 以 `sandbox_executor` 包裹 invoke |
| `main.py` | 修改 | `--executor` 参数（写 `KA_EXECUTOR` env） |
| `.env.example` | 修改 | W3 沙箱配置项说明 |
| `tests/test_shell_tools.py` | 修改 | 执行器工厂测试 |
| `tests/test_docker_sandbox.py` | 新增 | 单元测试（FakeDocker，不依赖真实 Docker） |
| `tests/test_docker_integration.py` | 新增 | 集成测试（真实容器，`@pytest.mark.docker`） |
| `tests/test_main.py` | 修改 | `--executor` 参数测试 |

---

### Task 1: Executor 接口与 LocalExecutor 迁移

**Files:**
- Modify: `tools/shell_tools.py`
- Test: `tests/test_shell_tools.py`

**Interfaces:**
- Consumes: 现有 `CommandResult` / `TestResult` / `GitStatus` / `GitDiff` / `GitLog` / `ToolError` / `resolve_workspace_path`
- Produces: `Executor`（Protocol，run_command/run_tests/run_git）、`LocalExecutor`、`get_executor() -> Executor | ToolError`、`_CURRENT_EXECUTOR: ContextVar`、`CommandResult.oom: bool = False`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_shell_tools.py`）

```python
from tools.shell_tools import (
    LocalExecutor, get_executor,
)


def test_get_executor_default_local(monkeypatch):
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    ex = get_executor()
    assert not isinstance(ex, ToolError)
    assert isinstance(ex, LocalExecutor)


def test_get_executor_env_docker_without_context(monkeypatch):
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "sandbox_executor" in ex.message


def test_get_executor_unknown(monkeypatch):
    monkeypatch.setenv("KA_EXECUTOR", "xxx")
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "未知执行器" in ex.message


def test_run_command_oom_field_default(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('hi')\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.oom is False
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py::test_get_executor_default_local -v`
Expected: FAIL（`ImportError: cannot import name 'LocalExecutor'` 或 `AttributeError`）

- [ ] **Step 3: 最小实现**——`tools/shell_tools.py` 修改

在 `from dataclasses import dataclass` 处改为：

```python
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol
```

`CommandResult` 增加字段（末尾，带默认值保持兼容）：

```python
@dataclass
class CommandResult:
    """命令执行结果：timeout 标记超时终止，oom 标记内存超限被杀，duration 为耗时（秒）。"""
    timeout: bool
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    oom: bool = False
```

在 `run_command` 原实现之前插入（把原 `run_command` / `run_tests` / `_run_git` 函数体搬进 `LocalExecutor`，模块函数改为委托）：

```python
class Executor(Protocol):
    """命令执行器接口：宿主机（LocalExecutor）或容器（DockerExecutor）。"""

    def run_command(self, command: str, cwd: str | None = None, timeout: int = 60,
                    workspace_root: str | None = None) -> CommandResult | ToolError: ...
    def run_tests(self, path: str | None = None,
                  workspace_root: str | None = None) -> TestResult | ToolError: ...
    def run_git(self, args: list[str], workspace_root: str | None = None) -> str | ToolError: ...


class LocalExecutor:
    """宿主机执行器：subprocess 直接执行（W1/W2 行为不变）。"""

    def run_command(self, command: str, cwd: str | None = None, timeout: int = 60,
                    workspace_root: str | None = None) -> CommandResult | ToolError:
        """在 cwd（已解析的绝对路径）执行 shell 命令；超时终止进程树置 timeout=True。"""
        start = time.monotonic()
        timeout_hit = False
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timeout_hit = True
            try:
                _kill_process_tree(proc)
            except Exception:
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = (proc.stdout or ""), (proc.stderr or "")
        except OSError as e:
            return ToolError(f"命令执行失败: {e}")
        return CommandResult(
            timeout=timeout_hit,
            stdout=_truncate(out or ""),
            stderr=_truncate(err or ""),
            exit_code=-1 if timeout_hit else proc.returncode,
            duration=round(time.monotonic() - start, 3),
        )

    def run_tests(self, path: str | None = None,
                  workspace_root: str | None = None) -> TestResult | ToolError:
        """在 workspace 根运行 pytest；path 可指定子路径（相对 workspace 根）。"""
        base = resolve_workspace_path("", workspace_root)
        if isinstance(base, ToolError):
            return base
        cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no"]
        if path:
            target = resolve_workspace_path(path, workspace_root)
            if isinstance(target, ToolError):
                return target
            cmd.append(target)
        try:
            proc = subprocess.run(
                cmd, cwd=base, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except subprocess.TimeoutExpired:
            return ToolError("测试超时（120 秒）")
        except OSError as e:
            return ToolError(f"pytest 执行失败: {e}")
        return _parse_pytest_output(proc.stdout)

    def run_git(self, args: list[str], workspace_root: str | None = None) -> str | ToolError:
        """在 workspace 根执行只读 git 命令，返回 stdout；失败返回 ToolError。"""
        base = resolve_workspace_path("", workspace_root)
        if isinstance(base, ToolError):
            return base
        try:
            proc = subprocess.run(
                ["git", *args], cwd=base, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        except FileNotFoundError:
            return ToolError("git 不可用: 未找到 git（请安装 Git）")
        except subprocess.TimeoutExpired:
            return ToolError("git 命令超时（30 秒）")
        if proc.returncode != 0:
            return ToolError(f"git {args[0]} 失败: {proc.stderr.strip()}")
        return proc.stdout


# 当前任务执行器（sandbox_executor 上下文设置；未设置时回退 KA_EXECUTOR env）
_CURRENT_EXECUTOR: ContextVar[Executor | None] = ContextVar("ka_current_executor", default=None)


def get_executor() -> Executor | ToolError:
    """当前执行器：优先任务上下文（sandbox_executor），否则按 KA_EXECUTOR 环境变量。"""
    current = _CURRENT_EXECUTOR.get()
    if current is not None:
        return current
    name = os.environ.get("KA_EXECUTOR", "local")
    if name == "local":
        return LocalExecutor()
    if name == "docker":
        return ToolError(
            "KA_EXECUTOR=docker 时命令必须在 sandbox_executor 上下文中执行"
            "（一个任务一个沙箱，见 agent/loop.py 与 agent/graph.py）")
    return ToolError(f"未知执行器: {name}（可选 local|docker）")
```

模块级函数改为委托（`run_command` 保留路径解析后转发；`run_tests` 直接转发；git 三件套经 `run_git`）：

```python
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
    workspace_root: str | None = None,
) -> CommandResult | ToolError:
    """在 workspace 内 cwd 执行 shell 命令；超时终止进程树置 timeout=True；输出截断。"""
    base = resolve_workspace_path(cwd or "", workspace_root)
    if isinstance(base, ToolError):
        return base
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    return executor.run_command(command, cwd=base, timeout=timeout,
                                workspace_root=workspace_root)


def run_tests(path: str | None = None, workspace_root: str | None = None) -> TestResult | ToolError:
    """在 workspace 根运行 pytest；path 可指定子路径（相对 workspace 根）。"""
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    return executor.run_tests(path=path, workspace_root=workspace_root)
```

删除原 `_run_git` 函数，git 三件套改为：

```python
def git_status(workspace_root: str | None = None) -> GitStatus | ToolError:
    """查看工作区 git 状态（--short）。"""
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    out = executor.run_git(["status", "--short"], workspace_root)
    if isinstance(out, ToolError):
        return out
    lines = [l for l in out.splitlines() if l.strip()]
    return GitStatus(clean=len(lines) == 0, changes=lines)


def git_diff(workspace_root: str | None = None) -> GitDiff | ToolError:
    """查看未提交变更（--stat + 完整 diff，截断）。"""
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    stat = executor.run_git(["diff", "--stat"], workspace_root)
    if isinstance(stat, ToolError):
        return stat
    diff = executor.run_git(["diff"], workspace_root)
    if isinstance(diff, ToolError):
        return diff
    return GitDiff(stat=stat.strip(), diff=_truncate(diff))


def git_log(count: int = 10, workspace_root: str | None = None) -> GitLog | ToolError:
    """查看最近提交（--oneline）。"""
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    out = executor.run_git(["log", "--oneline", f"-n{count}"], workspace_root)
    if isinstance(out, ToolError):
        return out
    return GitLog(entries=[l for l in out.splitlines() if l.strip()])
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py -v`
Expected: 全部 PASS（新增 4 项 + 既有全部）

- [ ] **Step 5: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/`
Expected: 52 passed（48 既有 + 4 新增）

- [ ] **Step 6: Commit**

```bash
git add tools/shell_tools.py tests/test_shell_tools.py
git commit -m "refactor(tools): 抽出 Executor 接口与 LocalExecutor，run_command/run_tests/git 经执行器分发"
```

---

### Task 2: 沙箱镜像 Dockerfile 与 pytest marker

**Files:**
- Create: `Dockerfile`
- Modify: `pyproject.toml`
- Create: `tests/test_docker_integration.py`（仅镜像构建测试，Task 6 扩充）

**Interfaces:**
- Produces: 镜像 `ka-sandbox:py312-v1`（构建一次缓存）；`pytest.mark.docker`；`DEFAULT_IMAGE` 常量（Task 3 使用）

- [ ] **Step 1: 写失败测试**——新建 `tests/test_docker_integration.py`

```python
# tests/test_docker_integration.py
"""Docker 沙箱集成测试：需要 Docker Desktop 运行（无 Docker 时自动跳过）。"""
import shutil
import subprocess

import pytest

from tools.docker_sandbox import DEFAULT_IMAGE


def _docker_available() -> bool:
    """docker CLI 存在且 daemon 可达。"""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker 不可用"),
]


def test_build_sandbox_image():
    """镜像可构建（docker build 幂等，利用缓存）。"""
    r = subprocess.run(
        ["docker", "build", "-t", DEFAULT_IMAGE, "."],
        capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    r2 = subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE],
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_integration.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'tools.docker_sandbox'`）

- [ ] **Step 3: 最小实现**——`pyproject.toml` 的 `[tool.pytest.ini_options]` 追加：

```toml
markers = [
    "docker: 需要 Docker 守护进程的集成测试（无 Docker 时跳过）",
]
```

新建 `Dockerfile`（仓库根）：

```dockerfile
# Dockerfile — W3 沙箱基础镜像
# 构建：docker build -t ka-sandbox:py312-v1 .
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive
# 沙箱内环境：Git / Node.js / npm / curl（网络连通性测试用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        git nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*
# Python 测试框架
RUN pip install --no-cache-dir pytest
# 消除 Windows/Linux 文件权限位差异（W3 spec §5.1，容器内 git 行为与宿主一致）
RUN git config --system core.filemode false
WORKDIR /workspace
```

`tools/docker_sandbox.py` 仅先建一行（Task 3 补全，满足 import）：

```python
# tools/docker_sandbox.py
"""Docker 沙箱：容器生命周期管理 + 容器内命令执行。"""

DEFAULT_IMAGE = "ka-sandbox:py312-v1"
```

- [ ] **Step 4: 运行确认通过**（先刷新 PATH 确保 docker 可用）

Run: `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User'); .venv\Scripts\python.exe -m pytest tests/test_docker_integration.py -v`
Expected: PASS（首次构建约 1-2 分钟；Docker 未运行时显示 SKIPPED）

- [ ] **Step 5: Commit**

```bash
git add Dockerfile pyproject.toml tests/test_docker_integration.py tools/docker_sandbox.py
git commit -m "feat(tools): 新增沙箱基础镜像 Dockerfile 与 docker 集成测试 marker"
```

---

### Task 3: SandboxConfig 与 SandboxManager（容器生命周期 + 资源限制）

**Files:**
- Create: `tools/docker_sandbox.py`（补全）
- Create: `tests/test_docker_sandbox.py`

**Interfaces:**
- Consumes: `CommandResult` / `TestResult` / `_parse_pytest_output` / `_truncate`（shell_tools）、`ToolError` / `resolve_workspace_path`（file_tools）、`DEFAULT_IMAGE`
- Produces: `SandboxConfig`（image/cpus/memory/pids_limit/network/repo_url）、`get_sandbox_config()`、`SandboxManager(config, workspace_root, docker_runner=None)`（create/exec/destroy/上下文协议）、`_container_name`、`_sh_quote`

- [ ] **Step 1: 写失败测试**——新建 `tests/test_docker_sandbox.py`

```python
# tests/test_docker_sandbox.py
"""Docker 沙箱单元测试：FakeDocker 替换 docker CLI，不依赖真实 Docker。"""
import subprocess

import pytest

from tools.docker_sandbox import (
    SandboxConfig, SandboxManager, _container_name, _sh_quote, get_sandbox_config,
)
from tools.file_tools import ToolError


class FakeDocker:
    """记录 docker 调用；plan 中键为子串匹配（如 "bash -lc"），命中即返回。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.plan: dict[str, subprocess.CompletedProcess] = {}

    def runner(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        joined = " ".join(args)
        for key, proc in self.plan.items():
            if key in joined:
                return proc
        return subprocess.CompletedProcess(args, 0, "", "")

    def called(self, *args: str) -> bool:
        return list(args) in self.calls

    def count(self, *prefix: str) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))


def test_container_name_stable():
    assert _container_name(r"C:\ws\demo-project") == _container_name(r"C:\ws\demo-project")
    assert "ka-sandbox-" in _container_name("x")


def test_sh_quote():
    assert _sh_quote("echo hi") == "'echo hi'"
    assert _sh_quote("it's") == "'it'\\''s'"


def test_get_sandbox_config_defaults(monkeypatch):
    for k in ("KA_SANDBOX_IMAGE", "KA_SANDBOX_CPUS", "KA_SANDBOX_MEMORY",
              "KA_SANDBOX_PIDS", "KA_SANDBOX_NETWORK", "KA_SANDBOX_REPO_URL"):
        monkeypatch.delenv(k, raising=False)
    cfg = get_sandbox_config()
    assert cfg.image == "ka-sandbox:py312-v1"
    assert cfg.cpus == 2.0
    assert cfg.memory == "1g"
    assert cfg.pids_limit == 512
    assert cfg.network is False
    assert cfg.repo_url is None


def test_get_sandbox_config_overrides(monkeypatch):
    monkeypatch.setenv("KA_SANDBOX_CPUS", "4")
    monkeypatch.setenv("KA_SANDBOX_MEMORY", "2g")
    monkeypatch.setenv("KA_SANDBOX_NETWORK", "1")
    monkeypatch.setenv("KA_SANDBOX_REPO_URL", "https://example.com/r.git")
    cfg = get_sandbox_config()
    assert cfg.cpus == 4.0
    assert cfg.memory == "2g"
    assert cfg.network is True
    assert cfg.repo_url == "https://example.com/r.git"


def test_create_passes_resource_limits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    run = [c for c in fake.calls if c[:2] == ["docker", "run"]][0]
    assert "--cpus" in run and "2.0" in run
    assert "--memory" in run and "1g" in run
    assert "--memory-swap" in run and "1g" in run
    assert "--pids-limit" in run and "512" in run
    assert "--network" in run and "none" in run
    assert "--tmpfs" in run and "/tmp" in run
    assert "-v" in run and f"{ws}:/workspace" in run
    assert "--name" in run and mgr.name in run
    assert run[-3:] == ["sleep", "infinity"]


def test_create_network_optin(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(network=True), str(ws), docker_runner=fake.runner)
    mgr.create()
    run = [c for c in fake.calls if c[:2] == ["docker", "run"]][0]
    assert "--network" not in run


def test_create_installs_requirements(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    assert fake.called("docker", "exec", mgr.name, "pip", "install",
                       "-r", "/workspace/requirements.txt")


def test_create_clones_repo_when_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(repo_url="https://example.com/r.git"),
                         str(ws), docker_runner=fake.runner)
    mgr.create()
    assert fake.called("docker", "exec", mgr.name, "git", "clone",
                       "https://example.com/r.git", "/workspace")


def test_create_skips_clone_when_not_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(repo_url="https://example.com/r.git"),
                         str(ws), docker_runner=fake.runner)
    mgr.create()
    assert not fake.called("docker", "exec", mgr.name, "git", "clone",
                           "https://example.com/r.git", "/workspace")


def test_create_missing_workspace(tmp_path):
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(tmp_path / "nope"), docker_runner=fake.runner)
    with pytest.raises(ToolError, match="不存在"):
        mgr.create()


def test_exec_before_create(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    r = mgr.exec("echo hi")
    assert isinstance(r, ToolError)
    assert "未创建" in r.message


def test_exec_wraps_timeout_and_cwd(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    r = mgr.exec("echo hi", cwd=str(ws / "sub"), timeout=7)
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0
    exec_call = [c for c in fake.calls if c[:2] == ["docker", "exec"]][-1]
    wrapped = exec_call[-1]
    assert "timeout -s KILL -k 5s 7 bash -lc" in wrapped
    assert "cd /workspace/sub &&" in wrapped


def test_exec_timeout_semantics(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    fake.plan["bash -lc"] = subprocess.CompletedProcess([], 124, "", "timeout")
    r = mgr.exec("sleep 10", timeout=2)
    assert not isinstance(r, ToolError)
    assert r.timeout is True
    assert r.exit_code == -1
    assert r.duration >= 0


def test_exec_oom_semantics(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    fake.plan["bash -lc"] = subprocess.CompletedProcess([], 137, "", "Killed")
    r = mgr.exec("python -c 'x=bytearray(10**9)'")
    assert not isinstance(r, ToolError)
    assert r.oom is True
    assert r.exit_code == 137


def test_destroy_removes_container(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    mgr.destroy()
    assert fake.called("docker", "rm", "-f", mgr.name)
    mgr.destroy()  # 幂等：不重复 rm
    assert fake.count("docker", "rm", "-f") == 1


def test_context_manager_destroys(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    with SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner) as mgr:
        assert mgr._created
    assert not mgr._created
    assert fake.called("docker", "rm", "-f", mgr.name)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_sandbox.py -v`
Expected: FAIL（`ImportError` 或 `AttributeError`）

- [ ] **Step 3: 实现**——`tools/docker_sandbox.py` 补全为：

```python
# tools/docker_sandbox.py
"""Docker 沙箱：容器生命周期管理 + 容器内命令执行。

一个 Task 一个 Sandbox：任务启动时经 sandbox_executor 上下文创建容器
（挂载 workspace、限制资源、安装依赖），任务结束销毁；运行期默认无网络。
"""
import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from tools.file_tools import ToolError
from tools.shell_tools import (
    CommandResult,
    TestResult,
    _parse_pytest_output,
    _truncate,
)

DEFAULT_IMAGE = "ka-sandbox:py312-v1"

# docker CLI 单次调用超时（创建/销毁/exec 都可能长耗时）
DOCKER_CLI_TIMEOUT = 300


@dataclass
class SandboxConfig:
    """沙箱资源配置：创建容器时的限制参数（W3 spec §6）。"""
    image: str = DEFAULT_IMAGE
    cpus: float = 2.0
    memory: str = "1g"
    pids_limit: int = 512
    network: bool = False          # True = 创建时容器带网络（任务级 opt-in）
    repo_url: str | None = None    # workspace 为空时创建阶段克隆的仓库


def get_sandbox_config() -> SandboxConfig:
    """从环境变量解析沙箱配置（KA_SANDBOX_IMAGE/CPUS/MEMORY/PIDS/NETWORK/REPO_URL）。"""

    def _env(name: str, default: str) -> str:
        raw = os.environ.get(name)
        return raw if raw not in (None, "") else default

    return SandboxConfig(
        image=_env("KA_SANDBOX_IMAGE", DEFAULT_IMAGE),
        cpus=float(_env("KA_SANDBOX_CPUS", "2")),
        memory=_env("KA_SANDBOX_MEMORY", "1g"),
        pids_limit=int(_env("KA_SANDBOX_PIDS", "512")),
        network=_env("KA_SANDBOX_NETWORK", "0") == "1",
        repo_url=os.environ.get("KA_SANDBOX_REPO_URL") or None,
    )


def _container_name(workspace_root: str) -> str:
    """容器名：ka-sandbox-<workspace 目录名>-<workspace 路径 sha1[:8]>。"""
    base = os.path.basename(os.path.normpath(workspace_root)) or "ws"
    digest = hashlib.sha1(workspace_root.encode("utf-8")).hexdigest()[:8]
    return f"ka-sandbox-{base}-{digest}"


def _sh_quote(s: str) -> str:
    """POSIX 单引号转义（' -> '\\''），用于把命令安全嵌入 bash -lc。"""
    return "'" + s.replace("'", "'\\''") + "'"


def _run_docker(args: list[str]) -> subprocess.CompletedProcess:
    """调用 docker CLI；启动失败/超时抛 ToolError。"""
    try:
        return subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=DOCKER_CLI_TIMEOUT,
        )
    except FileNotFoundError:
        raise ToolError("docker 不可用: 未找到 docker 命令（请安装 Docker Desktop）")
    except subprocess.TimeoutExpired:
        raise ToolError(f"docker 命令超时（{DOCKER_CLI_TIMEOUT} 秒）: {' '.join(args[:2])}")
    except OSError as e:
        raise ToolError(f"docker 执行失败: {e}")


class SandboxManager:
    """容器生命周期管理：create → exec → destroy。

    docker_runner 为可注入的 docker CLI 调用器（测试用 fake 替换），
    签名为 (args: list[str]) -> subprocess.CompletedProcess。
    """

    def __init__(self, config: SandboxConfig, workspace_root: str,
                 docker_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None):
        self.config = config
        self.workspace_root = os.path.abspath(workspace_root)
        self.name = _container_name(self.workspace_root)
        self._runner = docker_runner or _run_docker
        self._created = False

    # -- 内部辅助 ----------------------------------------------------------

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """执行 docker 命令；check=True 时非零退出抛 ToolError。"""
        proc = self._runner(list(args))
        if check and proc.returncode != 0:
            raise ToolError(
                f"docker {' '.join(args[:2])} 失败: {(proc.stderr or '').strip()[:200]}")
        return proc

    def _ensure_image(self) -> None:
        """镜像缺失时自动构建（Dockerfile 位于当前目录）。"""
        inspect = self._run("docker", "image", "inspect", self.config.image, check=False)
        if inspect.returncode == 0:
            return
        build = self._run("docker", "build", "-t", self.config.image, ".", check=False)
        if build.returncode != 0:
            raise ToolError(
                f"沙箱镜像 {self.config.image} 构建失败: {(build.stderr or '').strip()[:200]}")

    # -- 生命周期 ----------------------------------------------------------

    def create(self) -> None:
        """创建容器：挂载 workspace、限资源、装依赖、可选克隆（W3 spec §5.1）。"""
        if not os.path.isdir(self.workspace_root):
            raise ToolError(f"沙箱工作区不存在: {self.workspace_root}")
        self._ensure_image()
        cmd = [
            "docker", "run", "-d",
            "--name", self.name,
            "--cpus", str(self.config.cpus),
            "--memory", self.config.memory,
            "--memory-swap", self.config.memory,
            "--pids-limit", str(self.config.pids_limit),
            "--tmpfs", "/tmp",
            "-v", f"{self.workspace_root}:/workspace",
            "-w", "/workspace",
        ]
        if not self.config.network:
            cmd += ["--network", "none"]
        cmd += [self.config.image, "sleep", "infinity"]
        self._run(*cmd)
        self._created = True
        # 依赖安装：存在 requirements.txt 时自动安装（创建阶段网络可用）
        if os.path.exists(os.path.join(self.workspace_root, "requirements.txt")):
            self._run("docker", "exec", self.name, "pip", "install",
                      "-r", "/workspace/requirements.txt")
        # 可选克隆：workspace 为空且配置了 repo_url
        if self.config.repo_url and not os.listdir(self.workspace_root):
            self._run("docker", "exec", self.name, "git", "clone",
                      self.config.repo_url, "/workspace")

    def exec(self, command: str, cwd: str | None = None,
             timeout: int = 60) -> CommandResult | ToolError:
        """在容器内执行命令；超时（GNU timeout 124）与 OOM（137）结构化返回。"""
        if not self._created:
            return ToolError(f"沙箱容器未创建: {self.name}")
        container_cwd = "/workspace"
        if cwd:
            rel = os.path.relpath(os.path.abspath(cwd), self.workspace_root)
            if rel.startswith(".."):
                return ToolError(f"路径越界: {cwd}")
            if rel != ".":
                container_cwd = "/workspace/" + rel
        wrapped = f"timeout -s KILL -k 5s {int(timeout)} bash -lc {_sh_quote(command)}"
        if container_cwd != "/workspace":
            wrapped = f"cd {_sh_quote(container_cwd)} && {wrapped}"
        start = time.monotonic()
        proc = self._run("docker", "exec", self.name, "bash", "-lc",
                         wrapped, check=False)
        duration = round(time.monotonic() - start, 3)
        timeout_hit = proc.returncode == 124
        return CommandResult(
            timeout=timeout_hit,
            stdout=_truncate(proc.stdout or ""),
            stderr=_truncate(proc.stderr or ""),
            exit_code=-1 if timeout_hit else proc.returncode,
            duration=duration,
            oom=proc.returncode == 137,
        )

    def destroy(self) -> None:
        """销毁容器（幂等）。"""
        if self._created:
            self._run("docker", "rm", "-f", self.name, check=False)
            self._created = False

    def __enter__(self) -> "SandboxManager":
        self.create()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.destroy()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_sandbox.py -v`
Expected: 全部 PASS（16 项）

- [ ] **Step 5: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/`
Expected: 68 + 集成 1 项（68 为 Task 1 的 52 + 本任务 16；集成测试按 Docker 可用性跳过或通过）

- [ ] **Step 6: Commit**

```bash
git add tools/docker_sandbox.py tests/test_docker_sandbox.py
git commit -m "feat(tools): 新增 SandboxManager 容器生命周期与资源限制（timeout/CPU/内存/网络/文件系统）"
```

---

### Task 4: DockerExecutor 与 sandbox_executor 上下文

**Files:**
- Modify: `tools/docker_sandbox.py`
- Test: `tests/test_docker_sandbox.py`

**Interfaces:**
- Consumes: `SandboxManager`、`get_sandbox_config`、`Executor`/`LocalExecutor`/`_CURRENT_EXECUTOR`（shell_tools）、`resolve_workspace_path`
- Produces: `DockerExecutor(manager)`（run_command/run_tests/run_git）、`sandbox_executor(workspace_root, config=None, docker_runner=None) -> Iterator[Executor]`（contextmanager）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_docker_sandbox.py`）

```python
from tools.docker_sandbox import (
    DockerExecutor, SandboxConfig, SandboxManager, sandbox_executor,
)
from tools.shell_tools import LocalExecutor, get_executor, run_command


def test_docker_executor_run_command_via_manager(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    r = ex.run_command("echo hi", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0


def test_docker_executor_run_tests_parses(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    fake = FakeDocker()
    fake.plan["pytest"] = subprocess.CompletedProcess([], 0, "1 passed in 0.1s", "")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    tr = ex.run_tests(workspace_root=str(ws))
    assert not isinstance(tr, ToolError)
    assert tr.passed == 1
    assert tr.total == 1


def test_docker_executor_run_git(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    fake.plan["git status"] = subprocess.CompletedProcess([], 0, " M a.txt\n", "")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    out = ex.run_git(["status", "--short"], workspace_root=str(ws))
    assert out == " M a.txt\n"


def test_docker_executor_run_git_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    fake.plan["git"] = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    out = ex.run_git(["status"], workspace_root=str(ws))
    assert isinstance(out, ToolError)
    assert "not a git repository" in out.message


def test_sandbox_executor_local_no_container(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    with sandbox_executor(str(ws)) as ex:
        assert isinstance(ex, LocalExecutor)


def test_sandbox_executor_docker_lifecycle(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    fake = FakeDocker()
    with sandbox_executor(str(ws), docker_runner=fake.runner) as ex:
        assert isinstance(ex, DockerExecutor)
        assert any(c[:3] == ["docker", "run", "-d"] for c in fake.calls)
        r = run_command("echo hi", workspace_root=str(ws))  # 上下文内模块级调用走容器
        assert not isinstance(r, ToolError)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)


def test_sandbox_executor_resets_context(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    fake = FakeDocker()
    with sandbox_executor(str(ws), docker_runner=fake.runner):
        pass
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "sandbox_executor" in ex.message
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_sandbox.py -k "executor or sandbox" -v`
Expected: FAIL（`ImportError: cannot import name 'DockerExecutor'`）

- [ ] **Step 3: 实现**——`tools/docker_sandbox.py` 追加（文件头部 import 修改为）：

```python
import shlex
from contextlib import contextmanager
from typing import Iterator

from tools.file_tools import ToolError, resolve_workspace_path
from tools.shell_tools import (
    CommandResult,
    Executor,
    LocalExecutor,
    TestResult,
    _CURRENT_EXECUTOR,
    _parse_pytest_output,
    _truncate,
)
```

（把 Task 3 中 `from tools.file_tools import ToolError` 一行替换为上面的 `resolve_workspace_path` 版本，并补充新 import。）

文件末尾追加：

```python
class DockerExecutor:
    """容器内执行器：run_command / run_tests / run_git 全部在沙箱容器内运行（W3 spec §4.1）。"""

    def __init__(self, manager: SandboxManager):
        self._manager = manager

    def run_command(self, command: str, cwd: str | None = None, timeout: int = 60,
                    workspace_root: str | None = None) -> CommandResult | ToolError:
        """在容器内执行 shell 命令（cwd 为宿主绝对路径，自动换算容器路径）。"""
        return self._manager.exec(command, cwd=cwd, timeout=timeout)

    def run_tests(self, path: str | None = None,
                  workspace_root: str | None = None) -> TestResult | ToolError:
        """在容器内 /workspace 运行 pytest，解析结果（与 LocalExecutor 同格式）。"""
        cmd = "python -m pytest -q --tb=no"
        if path:
            base = resolve_workspace_path("", workspace_root)
            if isinstance(base, ToolError):
                return base
            rel = os.path.relpath(os.path.abspath(path), base)
            if rel.startswith(".."):
                return ToolError(f"路径越界: {path}")
            cmd += f" {_sh_quote('/workspace/' + rel)}"
        r = self._manager.exec(cmd, timeout=120)
        if isinstance(r, ToolError):
            return r
        return _parse_pytest_output(r.stdout)

    def run_git(self, args: list[str], workspace_root: str | None = None) -> str | ToolError:
        """在容器内 /workspace 执行只读 git 命令。"""
        r = self._manager.exec("git " + " ".join(shlex.quote(a) for a in args), timeout=30)
        if isinstance(r, ToolError):
            return r
        if r.exit_code != 0:
            return ToolError(f"git {args[0]} 失败: {r.stderr.strip()}")
        return r.stdout


@contextmanager
def sandbox_executor(workspace_root: str, config: SandboxConfig | None = None,
                     docker_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None
                     ) -> Iterator[Executor]:
    """任务级执行器上下文：KA_EXECUTOR=docker 时创建沙箱容器并在结束时销毁（一个任务一个沙箱）。

    local 时直接返回 LocalExecutor，不创建容器。docker_runner 仅供测试注入。
    """
    if os.environ.get("KA_EXECUTOR", "local") != "docker":
        yield LocalExecutor()
        return
    manager = SandboxManager(config or get_sandbox_config(), workspace_root,
                             docker_runner=docker_runner)
    manager.create()
    executor = DockerExecutor(manager)
    token = _CURRENT_EXECUTOR.set(executor)
    try:
        yield executor
    finally:
        _CURRENT_EXECUTOR.reset(token)
        manager.destroy()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_sandbox.py -v`
Expected: 全部 PASS（16 + 7 = 23 项）

- [ ] **Step 5: Commit**

```bash
git add tools/docker_sandbox.py tests/test_docker_sandbox.py
git commit -m "feat(tools): 新增 DockerExecutor 与 sandbox_executor 任务级沙箱上下文"
```

---

### Task 5: 生命周期接线（loop / graph / main / .env.example）

**Files:**
- Modify: `agent/loop.py`、`agent/graph.py`、`main.py`、`.env.example`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `sandbox_executor(workspace_root)`（Task 4）
- Produces: `run_agent` / `run_agent_graph` 在沙箱上下文中执行（Agent 任务全部命令进容器）；`main.py --executor`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_main.py`）

```python
def test_main_executor_docker_sets_env(monkeypatch, capsys):
    import os
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    rc = main.main(["测试任务", "--executor", "docker"])
    assert rc == 0
    assert os.environ["KA_EXECUTOR"] == "docker"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py::test_main_executor_docker_sets_env -v`
Expected: FAIL（`unrecognized arguments: --executor`）

- [ ] **Step 3: 实现**

`main.py`：`import os` 追加到文件头（`import argparse` 之后），`build_parser` 增加参数：

```python
    parser.add_argument("--executor", choices=["local", "docker"], default=None,
                        help="命令执行器：local（宿主机）或 docker（容器沙箱）；缺省读 KA_EXECUTOR（默认 local）")
```

`main()` 在 `load_dotenv()` 之后插入：

```python
    if args.executor:
        os.environ["KA_EXECUTOR"] = args.executor
```

`agent/loop.py`：import 区追加：

```python
import os

from tools.docker_sandbox import sandbox_executor
```

`run_agent` 函数体改为（整体包进 `with sandbox_executor(...)`，原逻辑不变，仅缩进一层）：

```python
def run_agent(task: str, llm: LLMClient, max_iterations: int = DEFAULT_MAX_ITERATIONS,
              workspace_root: str | None = None) -> AgentResult:
    """执行任务：循环调用 LLM，执行其工具调用并回注结果，直至无工具调用或达上限。

    命令执行位于沙箱上下文内（一个任务一个沙箱，KA_EXECUTOR=docker 时全部命令进容器）。
    """
    with sandbox_executor(workspace_root or os.getcwd()):
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        tools = _build_tools()
        steps: list[AgentStep] = []
        for i in range(max_iterations):
            msg = llm.chat(messages, tools)
            if not msg.tool_calls:
                return AgentResult(
                    steps=steps,
                    final_answer=msg.content or "",
                    iteration_count=i + 1,
                    stopped_by_limit=False,
                )
            results = []
            for tc in msg.tool_calls:
                result = _dispatch(tc.name, tc.arguments, workspace_root)
                steps.append(AgentStep(tool_name=tc.name, arguments=tc.arguments, result=result))
                results.append(result)
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc, result in zip(msg.tool_calls, results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _result_to_text(result),
                })
        return AgentResult(
            steps=steps,
            final_answer="已达到迭代上限，任务未完成",
            iteration_count=max_iterations,
            stopped_by_limit=True,
        )
```

`agent/graph.py`：import 区追加（`import re` 之后加 `import os`，并追加 docker_sandbox import）：

```python
import os

from tools.docker_sandbox import sandbox_executor
```

`run_agent_graph` 改为（invoke 包进上下文）：

```python
def run_agent_graph(task: str, llm: LLMClient, max_iterations: int = 20,
                    max_verify_rounds: int = 3,
                    workspace_root: str | None = None) -> AgentGraphResult:
    """执行任务：LangGraph 图驱动（命令执行在沙箱上下文内，一个任务一个沙箱）。"""
    graph = build_graph(llm, max_iterations, max_verify_rounds, workspace_root)
    with sandbox_executor(workspace_root or os.getcwd()):
        result = graph.invoke({
            "task": task,
            "plan": [],
            "messages": [{"role": "user", "content": task}],
            "steps": [],
            "iteration": 0,
            "verify_rounds": 0,
            "test_result": None,
            "test_results": [],
            "pending_tool_calls": None,
            "final_answer": "",
            "status": "running",
        })
    return AgentGraphResult(
        plan=result["plan"],
        steps=result["steps"],
        final_answer=result["final_answer"],
        iteration_count=result["iteration"],
        verify_rounds=result["verify_rounds"],
        stopped_by_limit=result["status"] == "limit",
        test_results=result["test_results"],
    )
```

`.env.example` 末尾追加：

```
# W3 Docker Sandbox（可选，默认值即注释所示）
# KA_EXECUTOR=local                # 命令执行器: local(宿主机) / docker(容器沙箱)
# KA_SANDBOX_IMAGE=ka-sandbox:py312-v1
# KA_SANDBOX_CPUS=2                # 容器 CPU 核数
# KA_SANDBOX_MEMORY=1g             # 容器内存上限
# KA_SANDBOX_NETWORK=0             # 1 = 创建时容器带网络（任务级 opt-in）
# KA_SANDBOX_REPO_URL=             # workspace 为空时创建阶段克隆的仓库
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py tests/test_loop.py tests/test_graph.py -v`
Expected: 全部 PASS（loop/graph 测试在 local 执行器下行为不变）

- [ ] **Step 5: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/`
Expected: 全绿（既有 48 + 新增全部；集成测试按 Docker 可用性）

- [ ] **Step 6: Commit**

```bash
git add agent/loop.py agent/graph.py main.py .env.example tests/test_main.py
git commit -m "feat(agent): run_agent/run_agent_graph 接入沙箱上下文，main.py 新增 --executor"
```

---

### Task 6: Docker 集成测试套件与验收演示

**Files:**
- Modify: `tests/test_docker_integration.py`

**Interfaces:**
- Consumes: `SandboxManager` / `SandboxConfig` / `DEFAULT_IMAGE`、`LocalExecutor`、`run_command` / `run_tests` / `git_status`
- Produces: W3 验收证据（超时/超内存终止、网络隔离、git 一致、无残留、双执行器一致）

- [ ] **Step 1: 写失败测试**——`tests/test_docker_integration.py` 追加（复用已有 `_docker_available` 与 pytestmark）：

```python
import subprocess
import time

from tools.docker_sandbox import SandboxConfig, SandboxManager
from tools.file_tools import ToolError
from tools.shell_tools import LocalExecutor, git_status


def _new_manager(tmp_path, **cfg):
    """在 tmp workspace 上创建真实沙箱管理器（配置可覆盖）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    return SandboxManager(SandboxConfig(**cfg), str(ws)), ws


def test_lifecycle_exec_and_no_residue(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        r = mgr.exec("echo hello-from-sandbox")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert "hello-from-sandbox" in r.stdout
    # 销毁后无残留
    probe = subprocess.run(["docker", "ps", "-a", "--filter", f"name={mgr.name}", "--format",
                            "{{.Names}}"], capture_output=True, text=True, timeout=30)
    assert mgr.name not in probe.stdout


def test_command_timeout_terminated(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        start = time.monotonic()
        r = mgr.exec("sleep 10", timeout=2)
        elapsed = time.monotonic() - start
        assert not isinstance(r, ToolError)
        assert r.timeout is True
        assert r.exit_code == -1
        assert elapsed < 9  # 2 秒超时 + 5 秒宽限内


def test_memory_oom_terminated(tmp_path):
    mgr, ws = _new_manager(tmp_path, memory="64m")
    with mgr:
        r = mgr.exec("python -c \"x = bytearray(300 * 1024 * 1024)\"")
        assert not isinstance(r, ToolError)
        assert r.oom is True
        assert r.exit_code == 137


def test_network_isolated(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        r = mgr.exec("python -c \"import urllib.request; "
                     "urllib.request.urlopen('https://example.com', timeout=5)\"")
        assert not isinstance(r, ToolError)
        assert r.exit_code != 0  # --network none 下连接失败


def test_workspace_mount_sync(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    (ws / "host.txt").write_text("from-host", encoding="utf-8")
    with mgr:
        r = mgr.exec("cat /workspace/host.txt")
        assert not isinstance(r, ToolError)
        assert "from-host" in r.stdout
        r2 = mgr.exec("echo from-container > /workspace/container.txt")
        assert not isinstance(r2, ToolError) and r2.exit_code == 0
    assert (ws / "container.txt").read_text(encoding="utf-8").strip() == "from-container"


def test_git_in_container_matches_host(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    # 宿主侧初始化仓库并提交
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    (ws / "a.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True)
    (ws / "a.txt").write_text("v2", encoding="utf-8")
    with mgr:
        # 容器内 git_status 与宿主一致（filemode=false 注入后无 mode change 噪音）
        host_status = git_status(workspace_root=str(ws))
        container_out = mgr.exec("git status --short")
        assert not isinstance(container_out, ToolError)
        assert not isinstance(host_status, ToolError)
        assert sorted(container_out.stdout.split()) == sorted(host_status.changes)


def test_executor_consistency(tmp_path):
    """同一命令在 local 与 docker 执行器下输出一致（W3 spec §8.3）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    local = LocalExecutor().run_command("python -c \"print('same')\"", cwd=str(ws),
                                        workspace_root=str(ws))
    mgr = SandboxManager(SandboxConfig(), str(ws))
    with mgr:
        docker = mgr.exec("python -c \"print('same')\"")
    assert not isinstance(local, ToolError) and not isinstance(docker, ToolError)
    assert local.exit_code == docker.exit_code
    assert local.stdout.strip() == docker.stdout.strip()


def test_pip_install_at_create(tmp_path):
    """创建阶段自动安装 requirements.txt（创建期网络可用）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    mgr = SandboxManager(SandboxConfig(), str(ws))
    with mgr:
        r = mgr.exec("python -c \"import six; print(six.__version__)\"")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert "1.16.0" in r.stdout


def test_clone_repo_when_empty(tmp_path):
    """workspace 为空 + repo_url 配置 → 创建阶段克隆（公网小仓库）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SandboxManager(
        SandboxConfig(repo_url="https://github.com/octocat/Hello-World.git"), str(ws))
    with mgr:
        r = mgr.exec("git -C /workspace log --oneline -1")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert r.stdout.strip() != ""
```

- [ ] **Step 2: 运行确认失败**

Run（先刷新 PATH）: `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User'); .venv\Scripts\python.exe -m pytest tests/test_docker_integration.py -v`
Expected: 新测试 FAIL（旧测试可能已随 Task 2 通过；未实现功能失败）

- [ ] **Step 3: 实现**——本任务无新实现代码（全部功能已由 Task 1-5 提供）；若 Step 2 出现 FAIL，按失败原因修复对应模块（预期无修复，纯验证任务）

- [ ] **Step 4: 运行确认通过**

Run: 同上命令
Expected: 全部 PASS（Docker 未运行则 SKIPPED）

- [ ] **Step 5: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add tests/test_docker_integration.py
git commit -m "test(tools): Docker 沙箱集成测试（生命周期/超时/OOM/网络隔离/git 一致/无残留/双执行器一致）"
```

---

### Task 7: 真实演示（窗口验收）

**Files:**
- 无代码变更；使用 `workspace/` 下已有 demo 材料（W1 demo-project 副本已删，演示前重新放置或使用新仓库副本）

**Interfaces:**
- Consumes: `main.py --executor docker`

- [ ] **Step 1: 准备演示工作区**

将真实 Python 项目复制到 `workspace/demo-project`（如 `D:\AI project\camera man`，W1 用过），确认含可运行 pytest 测试。

- [ ] **Step 2: 容器化 Agent 演示**

Run（刷新 PATH 后）:
```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
$env:KA_EXECUTOR = "docker"
.venv\Scripts\python.exe main.py "在 demo-project 中定位某函数并添加中文注释，然后运行测试确认" --workspace workspace
```

Expected: Agent 全部命令（read/search/run_tests 等）在容器内执行；任务结束容器销毁（`docker ps -a` 无 `ka-sandbox-*` 残留）；测试通过。

- [ ] **Step 3: 验证验收标准**

- 同一任务全部命令在容器内执行：演示日志中 run_command/run_tests 均经容器（可在演示期间 `docker ps` 观察容器存在）
- 超时/超内存正确终止并返回结构化错误：Task 6 集成测试已覆盖（timeout=True / exit 137 + oom）
- 容器销毁后无残留：Task 6 `test_lifecycle_exec_and_no_residue` + 演示后 `docker ps -a` 确认

- [ ] **Step 4: 记录演示结果**（供 devlog/roadmap 更新，窗口收尾时统一提交）

```bash
git add -A
git commit -m "docs: W3 演示记录与窗口收尾（roadmap/devlog 更新）"
```
