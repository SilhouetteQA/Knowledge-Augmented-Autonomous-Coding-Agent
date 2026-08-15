# tools/file_tools.py
"""文件工具：list_files / read_file / search_code / write_file。

所有工具以 workspace 根为安全边界；错误返回 ToolError（结构化、面向 LLM），不抛异常。
"""
import os
from dataclasses import dataclass
from pathlib import Path

# 遍历时跳过的目录（与 .gitignore 保持一致）
IGNORED_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".worktrees", ".pytest_cache", ".cache",
}


@dataclass
class ToolError:
    """工具错误：message 面向 LLM，Agent 可观察并自我纠正。"""
    message: str


@dataclass
class FileEntry:
    """目录项：path 为相对 workspace 根的正斜杠路径。"""
    path: str
    is_dir: bool
    size: int


def resolve_workspace_path(path: str, workspace_root: str | None = None) -> str | ToolError:
    """将相对/绝对路径解析为 workspace 内的绝对路径；越界或工作区缺失返回 ToolError。"""
    root = Path(workspace_root) if workspace_root else (Path.cwd() / "workspace")
    root = root.resolve()
    if not root.exists():
        return ToolError(f"工作区不存在: {root}")
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return ToolError(f"路径越界: {path}（仅允许操作工作区内的文件）")
    return str(target)


def list_files(root: str | None = None, workspace_root: str | None = None) -> list[FileEntry] | ToolError:
    """递归列出工作区（或其中子目录）的文件与目录，跳过 IGNORED_DIRS。"""
    base = resolve_workspace_path(root or "", workspace_root)
    if isinstance(base, ToolError):
        return base
    base_path = Path(base)
    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in dirnames:
            p = Path(dirpath) / name
            entries.append(FileEntry(path=p.relative_to(base_path).as_posix(), is_dir=True, size=0))
        for name in filenames:
            p = Path(dirpath) / name
            size = p.stat().st_size
            entries.append(FileEntry(path=p.relative_to(base_path).as_posix(), is_dir=False, size=size))
    entries.sort(key=lambda e: e.path)
    return entries
