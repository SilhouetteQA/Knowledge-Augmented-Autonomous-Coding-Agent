# tools/shell_tools.py
"""Shell 工具：run_command / run_tests / git 只读三件套。

所有命令以 workspace 根为安全边界（cwd 必须位于工作区内）；错误返回 ToolError。
"""
import os
import subprocess
import time
from dataclasses import dataclass

from tools.file_tools import ToolError, resolve_workspace_path

# 单次命令输出上限（防止大输出撑爆 LLM 上下文）
MAX_COMMAND_OUTPUT = 100 * 1024


def _truncate(text: str) -> str:
    """截断过长的命令输出，尾部附加标记。"""
    if len(text) <= MAX_COMMAND_OUTPUT:
        return text
    return text[:MAX_COMMAND_OUTPUT] + "\n...[输出截断]"


@dataclass
class CommandResult:
    """命令执行结果：timeout 标记超时终止，duration 为耗时（秒）。"""
    timeout: bool
    stdout: str
    stderr: str
    exit_code: int
    duration: float


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
    start = time.monotonic()
    timeout_hit = False
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=base, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timeout_hit = True
        try:
            _kill_process_tree(proc)
        except Exception:
            # 树杀除异常不崩溃：退化为超时终止，仍尝试排空输出。
            pass
        out, err = proc.communicate()
    except OSError as e:
        # 覆盖 Popen 启动失败（如 %COMSPEC% 损坏导致 OSError）与 communicate 的 OSError。
        return ToolError(f"命令执行失败: {e}")
    return CommandResult(
        timeout=timeout_hit,
        stdout=_truncate(out or ""),
        stderr=_truncate(err or ""),
        exit_code=-1 if timeout_hit else proc.returncode,
        duration=round(time.monotonic() - start, 3),
    )
