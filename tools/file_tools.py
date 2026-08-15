# tools/file_tools.py
"""文件工具：list_files / read_file / search_code / write_file。

所有工具以 workspace 根为安全边界；错误返回 ToolError（结构化、面向 LLM），不抛异常。
"""
import json
import os
import subprocess
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


# 单文件读取大小上限（防止大文件撑爆 LLM 上下文）
MAX_READ_SIZE = 500 * 1024


@dataclass
class FileContent:
    """文件内容：content 为带行号全文，lines 为原始行列表。"""
    path: str
    content: str
    lines: list[str]
    size: int


def read_file(path: str, workspace_root: str | None = None) -> FileContent | ToolError:
    """读取工作区内文本文件，返回带行号内容；大小超限/不存在/是目录/越界时返回 ToolError。"""
    target = resolve_workspace_path(path, workspace_root)
    if isinstance(target, ToolError):
        return target
    p = Path(target)
    if not p.exists():
        return ToolError(f"文件不存在: {path}")
    if p.is_dir():
        return ToolError(f"{path} 是目录，不是文件")
    size = p.stat().st_size
    if size > MAX_READ_SIZE:
        return ToolError(f"文件过大: {path}（{size} 字节，上限 {MAX_READ_SIZE} 字节）")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    content = "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(lines))
    return FileContent(path=path, content=content, lines=lines, size=size)


@dataclass
class SearchResult:
    """搜索结果：path 相对 workspace 根，line/column 为 1-based。"""
    path: str
    line: int
    column: int
    text: str


def _rg_path() -> str:
    """ripgrep 可执行文件路径：默认 rg，可用 RIPGREP_BIN 覆盖（沙箱场景）。"""
    return os.environ.get("RIPGREP_BIN", "rg")


def search_code(
    query: str,
    root: str | None = None,
    ignore_case: bool = False,
    workspace_root: str | None = None,
) -> list[SearchResult] | ToolError:
    """调用 ripgrep 搜索工作区代码，返回命中列表；rg 缺失/超时/失败返回 ToolError。"""
    base = resolve_workspace_path(root or "", workspace_root)
    if isinstance(base, ToolError):
        return base
    cmd = [_rg_path(), "--json", "--column"]
    if ignore_case:
        cmd.append("-i")
    cmd += ["--", query, base]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except FileNotFoundError:
        return ToolError(f"ripgrep 不可用: 未找到 {_rg_path()}（请安装或设置 RIPGREP_BIN）")
    except subprocess.TimeoutExpired:
        return ToolError("搜索超时（30 秒）")
    if proc.returncode not in (0, 1):
        return ToolError(f"ripgrep 执行失败: {proc.stderr.strip()}")
    results: list[SearchResult] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        rel = os.path.relpath(data["path"]["text"], base).replace("\\", "/")
        submatch = data["submatches"][0]
        results.append(SearchResult(
            path=rel,
            line=data["line_number"],
            column=submatch["start"] + 1,
            text=data["lines"]["text"].rstrip("\n"),
        ))
    results.sort(key=lambda r: (r.path, r.line))
    return results
