# tools/docker_sandbox.py
"""Docker 沙箱：容器生命周期管理 + 容器内命令执行。

一个 Task 一个 Sandbox：任务启动时经 sandbox_executor 上下文创建容器
（挂载 workspace、限制资源、安装依赖），任务结束销毁；运行期默认无网络。
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
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
    test_timeout_s,
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


def _run_docker(args: list[str],
                timeout: int = DOCKER_CLI_TIMEOUT) -> subprocess.CompletedProcess:
    """调用 docker CLI；启动失败/超时抛 ToolError。

    timeout 可由调用方按命令预算覆盖（IM-7：exec 随命令超时自适应，默认仍 300s）。
    """
    try:
        return subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        raise ToolError("docker 不可用: 未找到 docker 命令（请安装 Docker Desktop）")
    except subprocess.TimeoutExpired:
        raise ToolError(f"docker 命令超时（{timeout} 秒）: {' '.join(args[:2])}")
    except OSError as e:
        raise ToolError(f"docker 执行失败: {e}")


def _cli_budget(command_timeout: int | None) -> int:
    """docker CLI 预算（IM-7）：随命令超时自适应——max(默认 300, 命令超时 + 60s 缓冲）。

    KA_TEST_TIMEOUT_S>300 时容器内测试仍能跑完，docker CLI 不再先行误杀。
    """
    return max(DOCKER_CLI_TIMEOUT, int(command_timeout or 0) + 60)


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

    def _run(self, *args: str, check: bool = True,
             cli_timeout: int | None = None) -> subprocess.CompletedProcess:
        """执行 docker 命令；check=True 时非零退出抛 ToolError。

        cli_timeout 仅对真实 _run_docker 生效（注入的测试 runner 签名固定为 (args)，
        IM-7：exec 按命令超时传预算，build 按构建预算传）。
        """
        if cli_timeout is not None and self._runner is _run_docker:
            proc = _run_docker(list(args), timeout=cli_timeout)
        else:
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
        # IM-7：构建预算按 600s 命令折算（镜像冷构建可超默认 300s，误判为失败）
        build = self._run("docker", "build", "-t", self.config.image, ".", check=False,
                          cli_timeout=_cli_budget(600))
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
            # 容器硬化（历轮 M20）：移除全部 Linux capabilities + 禁提权；
            # read-only 根文件系统不做（会破坏创建期 pip 装依赖）
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{self.workspace_root}:/workspace",
            "-w", "/workspace",
        ]
        # 容器默认桥接网络创建：创建期网络可用（装依赖/克隆需要 DNS 解析）。
        cmd += [self.config.image, "sleep", "infinity"]
        self._run(*cmd)
        self._created = True
        # 依赖安装：存在 requirements.txt 时自动安装（创建阶段网络可用）
        if os.path.exists(os.path.join(self.workspace_root, "requirements.txt")):
            self._run("docker", "exec", self.name, "pip", "install",
                      "-r", "/workspace/requirements.txt")
        # 可选克隆：workspace 为空且配置了 repo_url
        # 注意：若此前部分克隆残留文件，workspace 非空将跳过克隆（可接受，避免覆盖）
        if self.config.repo_url and not os.listdir(self.workspace_root):
            self._run("docker", "exec", self.name, "git", "clone",
                      self.config.repo_url, "/workspace")
        # 创建期依赖/克隆完成后再断网：运行期默认隔离（网络 opt-in）。
        # 容器仍在运行 sleep infinity，disconnect 后 exec 阶段无网络。
        if not self.config.network:
            self._run("docker", "network", "disconnect", "bridge", self.name)

    def exec(self, command: str, cwd: str | None = None,
             timeout: int = 60) -> CommandResult | ToolError:
        """在容器内执行命令；超时（GNU timeout 124）与 OOM（137）结构化返回。

        超时用 `timeout -k 5s {t}`（无 `-s KILL`）：先发 SIGTERM，进程不退出则
        5 秒后 SIGKILL；GNU timeout 文档约定超时返回 124，正常结束透传子进程退出码。
        极端情况：TERM 免疫进程被宽限 KILL 也返 137，与 OOM 同码（罕见可接受）。
        """
        if not self._created:
            return ToolError(f"沙箱容器未创建: {self.name}")
        container_cwd = "/workspace"
        if cwd:
            # 以 workspace_root（挂载点）为基准换算，而非进程 cwd；Windows 反斜杠
            # 归一为 POSIX（I3：run_command(cwd="tests") 在 docker 模式此前必报越界）
            rel = os.path.relpath(os.path.abspath(cwd), os.path.abspath(self.workspace_root))
            if rel.startswith(".."):
                return ToolError(f"路径越界: {cwd}")
            if rel != ".":
                container_cwd = "/workspace/" + Path(rel).as_posix()
        wrapped = f"timeout -k 5s {int(timeout)} bash -lc {_sh_quote(command)}"
        if container_cwd != "/workspace":
            wrapped = f"cd {_sh_quote(container_cwd)} && {wrapped}"
        start = time.monotonic()
        try:
            proc = self._run("docker", "exec", self.name, "bash", "-lc",
                             wrapped, check=False, cli_timeout=_cli_budget(timeout))
        except ToolError as e:
            # IM-7：CLI 层失败（超时/不可用）返回结构化 ToolError 而非异常冒泡
            # （模块契约：exec 的错误形态是返回值，不是异常）
            return ToolError(f"docker exec 失败: {e.message}")
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
        """销毁容器（幂等）。销毁前清理容器侧以 root 写入的宿主可见残留
        （G7：__pycache__/.pytest_cache 在 bind mount 上为 root 属主，宿主难删）。"""
        if self._created:
            try:
                self.exec("find /workspace -name __pycache__ -type d "
                          "-exec rm -rf {} + 2>/dev/null; "
                          "rm -rf /workspace/.pytest_cache /workspace/.pytest_tmp",
                          timeout=30)
            except Exception:  # noqa: BLE001 — 清理尽力而为，销毁不受影响
                pass
            try:
                self._run("docker", "rm", "-f", self.name, check=False)
            except ToolError:
                pass  # IM-7：销毁尽力而为，CLI 层失败（超时等）不再打断任务收尾
            self._created = False

    def __enter__(self) -> "SandboxManager":
        self.create()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.destroy()


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
            # IM-9：workspace_root=None 时按协议签名可能为 None——显式报错而非
            # os.path.abspath(None) TypeError 裸崩（模块契约：错误返回 ToolError）
            if not workspace_root:
                return ToolError(
                    "docker 模式 run_tests(path) 需要 workspace_root（按其换算容器路径）")
            # 与 LocalExecutor 同语义：path 按 workspace 相对解析（I3：此前按进程
            # cwd 解析，docker 模式 run_tests(path=...) 必报越界；反斜杠归一 POSIX）
            target = resolve_workspace_path(path, workspace_root)
            if isinstance(target, ToolError):
                return target
            rel = os.path.relpath(target, os.path.abspath(workspace_root))
            if rel.startswith(".."):
                return ToolError(f"路径越界: {path}")
            cmd += f" {_sh_quote('/workspace/' + Path(rel).as_posix())}"
        r = self._manager.exec(cmd, timeout=test_timeout_s())
        if isinstance(r, ToolError):
            return r
        if r.timeout:
            return ToolError(f"测试超时（{test_timeout_s()} 秒）")
        # IM-6：与 LocalExecutor 同检查——pytest 自身失败（无汇总行）不得返回全 0 结果
        has_summary = re.search(r"\d+ (?:passed|failed|error)|no tests ran", r.stdout or "")
        if r.exit_code not in (0, 5) and not has_summary:
            return ToolError(
                f"pytest 运行失败（容器内 exit={r.exit_code}）: "
                f"{(r.stderr or '').strip()[:300]}")
        return _parse_pytest_output(r.stdout)

    def run_git(self, args: list[str], workspace_root: str | None = None) -> str | ToolError:
        """在容器内 /workspace 执行只读 git 命令。"""
        r = self._manager.exec("git " + " ".join(shlex.quote(a) for a in args), timeout=30)
        if isinstance(r, ToolError):
            return r
        if r.exit_code != 0:
            return ToolError(f"git {args[0] if args else 'git'} 失败: {r.stderr.strip()}")
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
    existing = _CURRENT_EXECUTOR.get()
    # IM-8：归一化路径比较——manager 内已是 abspath，内层传入的相对/带尾分隔符
    # 写法归一后再比，避免同容器被误判为不同 workspace 而重复 run（名称冲突）
    existing_root = getattr(getattr(existing, "_manager", None), "workspace_root", None)
    if existing is not None and existing_root \
            and os.path.abspath(workspace_root) == existing_root:
        # 嵌套复用（审查 I2）：外层上下文持有容器，内层不新建/不销毁——
        # benchmark setup 的环境级安装因此在基线/判定阶段可见（docker 下此前蒸发）
        yield existing
        return
    manager = SandboxManager(config or get_sandbox_config(), workspace_root,
                             docker_runner=docker_runner)
    token = None
    try:
        manager.create()          # 创建失败（含部分失败）也会走到 finally 的 destroy
        executor = DockerExecutor(manager)
        token = _CURRENT_EXECUTOR.set(executor)
        yield executor
    finally:
        if token is not None:
            _CURRENT_EXECUTOR.reset(token)
        manager.destroy()         # destroy 幂等（_created 守卫）：未创建则 no-op
