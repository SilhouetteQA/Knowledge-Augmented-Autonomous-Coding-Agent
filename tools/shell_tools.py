# tools/shell_tools.py
"""Shell 工具：run_command / run_tests / git 只读三件套。

所有命令以 workspace 根为安全边界（cwd 必须位于工作区内）；错误返回 ToolError。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Protocol

from tools.file_tools import ToolError, resolve_workspace_path
from tools.tracing import traced

# 单次命令输出上限（防止大输出撑爆 LLM 上下文）
MAX_COMMAND_OUTPUT = 100 * 1024

# 破坏性 git 子命令黑名单（rich#3299 实测：Agent 用 run_command 跑 git stash 自伤，
# 之后数轮耗在恢复现场）。仓库分支/提交/同步由编排层（宿主 git 函数）管理，
# Agent 内只需只读 git 工具；检出/回滚类需求用 write_file/edit_file 满足。
GIT_DESTRUCTIVE_SUBCOMMANDS = (
    "stash", "reset", "rebase", "merge", "checkout", "clean", "restore",
    "commit", "push", "pull", "fetch", "cherry-pick", "revert", "am", "apply",
)
_GIT_DESTRUCTIVE_RE = re.compile(
    r"(?:^|[;&|]\s*)\s*git\s+"
    r"(?:(?:-{1,2}[\w-]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|[\w./@+-]+))?\s+)*"
    r"(?P<sub>" + "|".join(GIT_DESTRUCTIVE_SUBCOMMANDS) + r")\b"
)


def check_destructive_git(command: str) -> ToolError | None:
    """拦截 run_command 中的破坏性 git 子命令；命中返回 ToolError，放行返回 None。

    按命令词法匹配（git 后跟可选全局选项再跟子命令），不解析引号嵌套——
    这是防 Agent 自伤的护栏而非安全边界（沙箱负责真正的隔离）。
    """
    match = _GIT_DESTRUCTIVE_RE.search(command)
    if match:
        return ToolError(
            f"禁止执行破坏性 git 子命令: git {match.group('sub')}"
            "（分支/提交/同步由系统编排层管理；查看变更用 git_status/git_diff/git_log，"
            "撤销修改用 edit_file 反向编辑或重新 write_file）")
    return None


def _test_timeout_s() -> int:
    """run_tests 超时秒数：KA_TEST_TIMEOUT_S 覆盖，缺省 120。非法/非正值回退默认。"""
    raw = os.environ.get("KA_TEST_TIMEOUT_S", "").strip()
    if not raw:
        return 120
    try:
        value = int(float(raw))
    except ValueError:
        return 120
    return value if value > 0 else 120


def _truncate(text: str) -> str:
    """截断过长的命令输出，尾部附加标记。"""
    if len(text) <= MAX_COMMAND_OUTPUT:
        return text
    return text[:MAX_COMMAND_OUTPUT] + "\n...[输出截断]"


@dataclass
class CommandResult:
    """命令执行结果：timeout 标记超时终止，oom 标记内存超限被杀，duration 为耗时（秒）。"""
    timeout: bool
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    oom: bool = False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """终止进程及其整棵子进程树。

    Windows 下 shell=True 会生成「cmd.exe -> cmd.exe -> ... -> 命令子进程」的多级树；
    仅 kill 顶层 cmd 会留下孤儿子进程继续占住 stdout/stderr 管道，导致后续 communicate
    阻塞至其自然结束（超时命令拖着不返回）。故需递归枚举并终止全部后代。
    """
    if os.name != "nt":
        proc.kill()
        return
    import ctypes
    from ctypes import wintypes

    PROCESS_TERMINATE = 0x0001
    TH32CS_SNAPPROCESS = 0x00000002
    kernel32 = ctypes.windll.kernel32

    class _PROCESSENTRY32(ctypes.Structure):
        """进程快照项；仅使用其中的数值字段（忽略最大路径名）。"""
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    def _snapshot() -> list[tuple[int, int]]:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return []
        entries: list[tuple[int, int]] = []
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            entries.append((pe.th32ProcessID, pe.th32ParentProcessID))
            ok = kernel32.Process32Next(snap, ctypes.byref(pe))
        kernel32.CloseHandle(snap)
        return entries

    def _terminate(pid: int) -> None:
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)

    # 先取快照并算出全部后代，再终止进程。若先 kill 顶层 cmd，Windows 会把
    # 孤儿进程重新挂到 PID 4，按 ppid 追溯时后代集合为空而漏杀，故必须在
    # proc.kill() 之前完成快照与后代枚举。
    children_by_parent: dict[int, list[int]] = {}
    for pid, ppid in _snapshot():
        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    stack = [proc.pid]
    while stack:
        current = stack.pop()
        for child in children_by_parent.get(current, []):
            descendants.append(child)
            stack.append(child)

    # 先终止后代，再终止顶层进程本身（含 proc.pid）。
    for child in descendants:
        _terminate(child)
    proc.kill()


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


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
    workspace_root: str | None = None,
) -> CommandResult | ToolError:
    """在 workspace 内 cwd 执行 shell 命令；超时终止进程树置 timeout=True；输出截断。"""
    guard = check_destructive_git(command)
    if guard is not None:
        return guard
    base = resolve_workspace_path(cwd or "", workspace_root)
    if isinstance(base, ToolError):
        return base
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    return executor.run_command(command, cwd=base, timeout=timeout,
                                workspace_root=workspace_root)


@dataclass
class TestFailure:
    """单个测试失败：test 为 路径::用例，message 为失败原因摘要。"""
    test: str
    message: str


@dataclass
class TestResult:
    """pytest 运行结果：failures 为 FAILED 行解析出的失败列表。"""
    passed: int
    failed: int
    error: int
    total: int
    duration: float
    failures: list[TestFailure]


def _test_metadata(args, kwargs, result) -> dict:
    """test.run span metadata：TestResult 摘要（asdict）或错误信息。"""
    if isinstance(result, TestResult):
        return asdict(result)
    return {"error": str(result)}


def _parse_pytest_output(out: str) -> TestResult:
    """解析 `pytest -q --tb=no` 输出：计数行 + FAILED 摘要行。"""
    passed = failed = error = 0
    duration = 0.0
    failures: list[TestFailure] = []
    for line in out.splitlines():
        m = re.search(r"(\d+) passed", line)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", line)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) error", line)
        if m:
            error = int(m.group(1))
        m = re.search(r"in (\d+\.?\d*)s", line)
        if m:
            duration = float(m.group(1))
        fm = re.match(r"FAILED (.+) - (.+)$", line)
        if fm:
            failures.append(TestFailure(test=fm.group(1), message=fm.group(2)))
    return TestResult(
        passed=passed, failed=failed, error=error,
        total=passed + failed + error, duration=duration, failures=failures,
    )


@traced("test.run", as_type="span", metadata_fn=_test_metadata)
def run_tests(path: str | None = None, workspace_root: str | None = None) -> TestResult | ToolError:
    """在 workspace 根运行 pytest；path 可指定子路径（相对 workspace 根）。"""
    executor = get_executor()
    if isinstance(executor, ToolError):
        return executor
    return executor.run_tests(path=path, workspace_root=workspace_root)


@dataclass
class GitStatus:
    """git status --short 解析结果。"""
    clean: bool
    changes: list[str]


@dataclass
class GitDiff:
    """git diff 结果：stat 为变更统计，diff 为完整差异（截断）。"""
    stat: str
    diff: str


@dataclass
class GitLog:
    """git log --oneline 结果。"""
    entries: list[str]


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
