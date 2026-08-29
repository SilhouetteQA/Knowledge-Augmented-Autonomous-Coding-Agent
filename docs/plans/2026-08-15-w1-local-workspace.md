# W1 本地 Workspace 实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施本计划。步骤用 checkbox（`- [ ]`）跟踪。
> 关联 Spec：`docs/specs/2026-08-15-w1-local-workspace.md`（已批准）

**Goal:** 交付 4 个文件工具（list_files / read_file / search_code / write_file）+ 最小 ReAct 循环（CLI 入口），Agent 能对真实 Python 项目完成「读取 → 理解 → 修改 → 确认」闭环。

**Architecture:** 工具层（`tools/file_tools.py`，全部以 workspace 根为安全边界，错误返回结构化 `ToolError` 不抛异常）→ 可插拔 `LLMClient` 协议（OpenAI 兼容实现 + MockLLMClient）→ `run_agent` 最小 ReAct 循环（LLM 决策工具调用，结果回注 messages，上限 10 轮）→ `main.py` CLI。

**Tech Stack:** Python 3.12+ / pytest / ripgrep 14.1.1（`%LOCALAPPDATA%\Programs\rg`，PATH 已配置）/ openai SDK / python-dotenv

## Global Constraints

- 环境：Windows；Python >= 3.12（当前 3.12.10）；ripgrep 14.1.1 已装（`RIPGREP_BIN` 环境变量可覆盖路径）
- 测试 seam（已与用户确认）：四个工具公共函数 + `run_agent(task)`；不测 LLM 内部
- 所有工具路径必须限制在 workspace 根内，越界返回 `ToolError`
- 错误不崩溃：`ToolError` 结构化回注，Agent 可观察可纠正
- `read_file` 大小上限 500KB；循环上限 10 轮
- 代码中文注释、UTF-8、无 emoji、不写 fallback
- 提交：Conventional Commits，中文 message；每任务结束一次 commit（G-02 小步提交）
- 所有命令在仓库根（worktree `.worktrees/w1-local-workspace`）执行；测试命令统一 `.venv\Scripts\python.exe -m pytest`

---

### Task 1: 路径安全与 list_files 工具

**Files:**
- Create: `tools/__init__.py`
- Create: `tools/file_tools.py`
- Modify: `pyproject.toml`（pytest 配置加 `pythonpath = ["."]`）
- Test: `tests/test_file_tools.py`（本任务只含 list_files 与路径安全用例）

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `resolve_workspace_path(path: str, workspace_root: str | None) -> str | ToolError`；`list_files(root: str | None = None, workspace_root: str | None = None) -> list[FileEntry] | ToolError`；`ToolError(message: str)`；`FileEntry(path: str, is_dir: bool, size: int)`

- [ ] **Step 1: 初始化环境（venv + 依赖 + pytest 路径配置）**

修改 `pyproject.toml`：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

运行：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected: 安装成功无报错；`.venv` 目录生成（已被 .gitignore 忽略）。

- [ ] **Step 2: 写失败测试（list_files 忽略规则 + 工作区不存在 + 越界）**

创建 `tests/test_file_tools.py`：

```python
# tests/test_file_tools.py
"""list_files 与路径安全测试"""
from tools.file_tools import FileEntry, ToolError, list_files, resolve_workspace_path


def test_list_files_ignores_special_dirs(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.py").write_text("print(1)", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / "__pycache__").mkdir()
    (ws / "sub" / "b.txt").write_text("x", encoding="utf-8")
    result = list_files(workspace_root=str(ws))
    assert not isinstance(result, ToolError)
    assert [e.path for e in result] == ["a.py", "sub", "sub/b.txt"]
    assert {e.path for e in result if not e.is_dir} == {"a.py", "sub/b.txt"}


def test_list_files_missing_workspace(tmp_path):
    result = list_files(workspace_root=str(tmp_path / "nope"))
    assert isinstance(result, ToolError)
    assert "工作区不存在" in result.message


def test_resolve_rejects_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = resolve_workspace_path("../outside.py", str(ws))
    assert isinstance(result, ToolError)
    assert "越界" in result.message
```

- [ ] **Step 3: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py -v`

Expected: FAILED x3 —— `ModuleNotFoundError: No module named 'tools'`（模块尚未创建）。

- [ ] **Step 4: 最小实现**

创建 `tools/__init__.py`（空文件）。

创建 `tools/file_tools.py`：

```python
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
```

- [ ] **Step 5: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py -v`

Expected: `3 passed`。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tools/__init__.py tools/file_tools.py tests/test_file_tools.py
git commit -m "feat(tools): 新增 list_files 工具与路径安全校验"
```

---

### Task 2: read_file 工具

**Files:**
- Modify: `tools/file_tools.py`（追加 read_file 与 MAX_READ_SIZE）
- Test: `tests/test_file_tools.py`（追加 read_file 用例）

**Interfaces:**
- Consumes: `resolve_workspace_path` / `ToolError`（Task 1）
- Produces: `MAX_READ_SIZE = 500 * 1024`；`read_file(path: str, workspace_root: str | None = None) -> FileContent | ToolError`；`FileContent(path: str, content: str, lines: list[str], size: int)`（content 为带行号全文）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_file_tools.py`：

```python
def test_read_file_with_line_numbers(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("x=1\ny=2\n", encoding="utf-8")
    fc = read_file("a.py", workspace_root=str(ws))
    assert not isinstance(fc, ToolError)
    assert fc.lines == ["x=1", "y=2"]
    assert "1 | x=1" in fc.content and "2 | y=2" in fc.content
    assert fc.size == 8


def test_read_file_missing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = read_file("nope.py", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "不存在" in r.message


def test_read_file_directory(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    r = read_file("sub", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "目录" in r.message


def test_read_file_too_large(tmp_path, monkeypatch):
    import tools.file_tools as ft

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.py").write_text("a" * 600, encoding="utf-8")
    monkeypatch.setattr(ft, "MAX_READ_SIZE", 500)
    r = read_file("big.py", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "过大" in r.message
```

并更新文件头部 import：

```python
from tools.file_tools import (
    FileContent,
    FileEntry,
    ToolError,
    list_files,
    read_file,
    resolve_workspace_path,
)
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py::test_read_file_with_line_numbers -v`

Expected: FAILED —— `ImportError: cannot import name 'read_file'`。

- [ ] **Step 3: 最小实现**

追加到 `tools/file_tools.py`：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py -v`

Expected: `7 passed`。

- [ ] **Step 5: Commit**

```bash
git add tools/file_tools.py tests/test_file_tools.py
git commit -m "feat(tools): 新增 read_file 工具（带行号，500KB 上限）"
```

---

### Task 3: search_code 工具（ripgrep 后端）

**Files:**
- Modify: `tools/file_tools.py`（追加 search_code 与 SearchResult）
- Test: `tests/test_file_tools.py`（追加 search_code 用例）

**Interfaces:**
- Consumes: `resolve_workspace_path` / `ToolError`（Task 1）
- Produces: `search_code(query: str, root: str | None = None, ignore_case: bool = False, workspace_root: str | None = None) -> list[SearchResult] | ToolError`；`SearchResult(path: str, line: int, column: int, text: str)`（line/column 为 1-based）；rg 路径取自环境变量 `RIPGREP_BIN`（缺省 `rg`）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_file_tools.py`：

```python
def test_search_code_finds_match(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ws / "b.py").write_text("x = hello()\n", encoding="utf-8")
    results = search_code("hello", workspace_root=str(ws))
    assert not isinstance(results, ToolError)
    assert len(results) == 2
    first = results[0]
    assert first.path == "a.py"
    assert first.line == 1
    assert first.column == 5
    assert "def hello" in first.text


def test_search_code_no_match(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("x=1\n", encoding="utf-8")
    results = search_code("zzz_nope", workspace_root=str(ws))
    assert results == []


def test_search_code_ignore_case(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("Hello\n", encoding="utf-8")
    assert search_code("hello", workspace_root=str(ws)) == []
    hits = search_code("hello", ignore_case=True, workspace_root=str(ws))
    assert len(hits) == 1


def test_search_code_rg_missing(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_BIN", str(tmp_path / "no-rg.exe"))
    r = search_code("x", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "ripgrep 不可用" in r.message
```

并更新 import：

```python
from tools.file_tools import (
    FileContent,
    FileEntry,
    SearchResult,
    ToolError,
    list_files,
    read_file,
    resolve_workspace_path,
    search_code,
)
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py::test_search_code_finds_match -v`

Expected: FAILED —— `ImportError: cannot import name 'search_code'`。

- [ ] **Step 3: 最小实现**

追加到 `tools/file_tools.py`：

```python
import json
import subprocess
```

并将文件头部 import 块更新为：

```python
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
```

再追加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py -v`

Expected: `11 passed`。

- [ ] **Step 5: Commit**

```bash
git add tools/file_tools.py tests/test_file_tools.py
git commit -m "feat(tools): 新增 search_code 工具（ripgrep 后端）"
```

---

### Task 4: write_file 工具

**Files:**
- Modify: `tools/file_tools.py`（追加 write_file 与 WriteResult）
- Test: `tests/test_file_tools.py`（追加 write_file 用例）

**Interfaces:**
- Consumes: `resolve_workspace_path` / `ToolError`（Task 1）
- Produces: `write_file(path: str, content: str, workspace_root: str | None = None) -> WriteResult | ToolError`；`WriteResult(path: str, bytes_written: int, overwritten: bool)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_file_tools.py`：

```python
def test_write_file_creates_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = write_file("sub/dir/new.py", "code", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.path == "sub/dir/new.py"
    assert r.bytes_written == 4
    assert r.overwritten is False
    assert (ws / "sub" / "dir" / "new.py").read_text(encoding="utf-8") == "code"


def test_write_file_overwrites(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("old", encoding="utf-8")
    r = write_file("a.py", "new", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.overwritten is True
    assert (ws / "a.py").read_text(encoding="utf-8") == "new"


def test_write_file_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = write_file("../evil.py", "x", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "越界" in r.message
```

并更新 import：

```python
from tools.file_tools import (
    FileContent,
    FileEntry,
    SearchResult,
    ToolError,
    WriteResult,
    list_files,
    read_file,
    resolve_workspace_path,
    search_code,
    write_file,
)
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py::test_write_file_creates_dirs -v`

Expected: FAILED —— `ImportError: cannot import name 'write_file'`。

- [ ] **Step 3: 最小实现**

追加到 `tools/file_tools.py`：

```python
@dataclass
class WriteResult:
    """写入结果：path 相对 workspace 根，overwritten 标记是否覆盖已有文件。"""
    path: str
    bytes_written: int
    overwritten: bool


def write_file(path: str, content: str, workspace_root: str | None = None) -> WriteResult | ToolError:
    """写入工作区内文件（自动创建父目录，覆盖已有内容）；越界/写入失败返回 ToolError。"""
    target = resolve_workspace_path(path, workspace_root)
    if isinstance(target, ToolError):
        return target
    p = Path(target)
    overwritten = p.exists()
    data = content.encode("utf-8")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    except OSError as e:
        return ToolError(f"写入失败: {path}（{e}）")
    return WriteResult(path=path, bytes_written=len(data), overwritten=overwritten)
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_file_tools.py -v`

Expected: `14 passed`。

- [ ] **Step 5: Commit**

```bash
git add tools/file_tools.py tests/test_file_tools.py
git commit -m "feat(tools): 新增 write_file 工具"
```

---

### Task 5: LLM 接口（协议 + OpenAI 兼容实现 + Mock）

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/llm.py`
- Modify: `pyproject.toml`（project.dependencies 加 openai / python-dotenv）
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: 无（独立于 Task 1-4）
- Produces: `ToolSpec(name, description, parameters)`；`ToolCall(id, name, arguments)`；`LLMMessage(role, content, tool_calls, tool_call_id)`；`LLMClient` Protocol（`chat(messages: list[dict], tools: list[ToolSpec]) -> LLMMessage`）；`OpenAICompatClient(api_key=None, base_url=None, model=None)`（env: `opencode_go_api` / `OPENCODE_GO_BASE_URL` / `OPENCODE_GO_MODEL` 默认 `deepseek-v4-flash`）；`MockLLMClient(script: list[LLMMessage])`（按顺序弹出，记录 calls）

- [ ] **Step 1: 添加依赖并安装**

修改 `pyproject.toml`：

```toml
[project]
# ...（保留原有字段）
dependencies = [
    "openai>=1.40",
    "python-dotenv>=1.0",
]
```

运行: `.venv\Scripts\python.exe -m pip install -e ".[dev]"`

Expected: openai / python-dotenv 安装成功。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_llm.py`：

```python
# tests/test_llm.py
"""LLM 客户端测试：Mock 行为、配置校验、OpenAI 兼容消息转换"""
from types import SimpleNamespace

import pytest

from agent.llm import (
    LLMMessage,
    MockLLMClient,
    OpenAICompatClient,
    ToolCall,
    ToolSpec,
)


def test_mock_llm_pops_script_and_records_calls():
    script = [LLMMessage(role="assistant", content="你好")]
    mock = MockLLMClient(script)
    tools = [ToolSpec(name="list_files", description="列出文件", parameters={})]
    msg = mock.chat([{"role": "user", "content": "hi"}], tools)
    assert msg.content == "你好"
    assert mock.calls == [([{"role": "user", "content": "hi"}], tools)]
    # 脚本耗尽后仍可调用（返回占位文本）
    msg2 = mock.chat([], [])
    assert msg2.content == "（无预设响应）"


def test_openai_client_requires_env(monkeypatch):
    monkeypatch.delenv("opencode_go_api", raising=False)
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="opencode_go_api"):
        OpenAICompatClient()
    monkeypatch.setenv("opencode_go_api", "k")
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="OPENCODE_GO_BASE_URL"):
        OpenAICompatClient()


def test_openai_client_chat_converts_response(monkeypatch):
    # 固定模型默认值，避免本机 OPENCODE_GO_MODEL 环境变量干扰断言
    monkeypatch.delenv("OPENCODE_GO_MODEL", raising=False)
    client = OpenAICompatClient(api_key="test-key", base_url="http://localhost:1")

    class FakeCompletions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            msg = SimpleNamespace(
                content="回答",
                tool_calls=[SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(
                        name="read_file", arguments='{"path": "a.py"}'),
                )],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    tools = [ToolSpec(name="read_file", description="读取文件",
                      parameters={"type": "object", "properties": {}})]
    msg = client.chat([{"role": "user", "content": "hi"}], tools)
    assert msg.content == "回答"
    assert fake.kwargs["model"] == "deepseek-4-flash"
    assert fake.kwargs["tools"][0]["function"]["name"] == "read_file"
    assert msg.tool_calls[0].name == "read_file"
    assert msg.tool_calls[0].arguments == {"path": "a.py"}
```

- [ ] **Step 3: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_llm.py -v`

Expected: FAILED x3 —— `ModuleNotFoundError: No module named 'agent'`。

- [ ] **Step 4: 最小实现**

创建 `agent/__init__.py`（空文件）。

创建 `agent/llm.py`：

```python
# agent/llm.py
"""LLM 客户端：可插拔协议 + OpenAI 兼容实现 + Mock 实现。

真实配置从环境变量读取：opencode_go_api（Key）、OPENCODE_GO_BASE_URL（端点）、
OPENCODE_GO_MODEL（模型，默认 deepseek-4-flash）。
"""
import json
import os
from dataclasses import dataclass
from typing import Protocol

import openai


@dataclass
class ToolSpec:
    """工具描述（OpenAI function calling 格式的简化视图）。"""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """一次工具调用：id 用于回注 tool 结果消息。"""
    id: str
    name: str
    arguments: dict


@dataclass
class LLMMessage:
    """LLM 返回消息：有 tool_calls 表示需要执行工具，否则 content 为最终回答。"""
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class LLMClient(Protocol):
    """LLM 客户端协议：ReAct 循环只依赖此接口。"""

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage: ...


class OpenAICompatClient:
    """OpenAI 兼容实现（opencode go 订阅的 deepseek-4-flash）。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or os.environ.get("opencode_go_api", "")
        self.base_url = base_url or os.environ.get("OPENCODE_GO_BASE_URL", "")
        self.model = model or os.environ.get("OPENCODE_GO_MODEL", "deepseek-4-flash")
        if not self.api_key:
            raise ValueError("缺少 API Key：请设置环境变量 opencode_go_api")
        if not self.base_url:
            raise ValueError("缺少端点地址：请设置环境变量 OPENCODE_GO_BASE_URL")
        self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage:
        params: dict = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        resp = self._client.chat.completions.create(**params)
        msg = resp.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments or "{}"),
                )
                for tc in msg.tool_calls
            ]
        return LLMMessage(role="assistant", content=msg.content, tool_calls=tool_calls)


class MockLLMClient:
    """测试用 Mock：按脚本顺序弹出预设响应，并记录每次调用。"""

    def __init__(self, script: list[LLMMessage]):
        self.script = script
        self.calls: list[tuple[list[dict], list[ToolSpec]]] = []

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage:
        self.calls.append((messages, tools))
        if not self.script:
            return LLMMessage(role="assistant", content="（无预设响应）")
        return self.script.pop(0)
```

- [ ] **Step 5: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_llm.py -v`

Expected: `3 passed`。

- [ ] **Step 6: Commit**

```bash
git add agent/__init__.py agent/llm.py pyproject.toml tests/test_llm.py
git commit -m "feat(agent): 新增可插拔 LLM 客户端（OpenAI 兼容 + Mock）"
```

---

### Task 6: 最小 ReAct 循环

**Files:**
- Create: `agent/loop.py`
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `tools.file_tools` 四个工具；`agent.llm`（ToolSpec / ToolCall / LLMMessage / LLMClient）
- Produces: `run_agent(task: str, llm: LLMClient, max_iterations: int = 10, workspace_root: str | None = None) -> AgentResult`；`AgentStep(tool_name, arguments, result)`；`AgentResult(steps, final_answer, iteration_count, stopped_by_limit)`；工具 JSON schema 固定为 `_build_tools()` 的 4 个描述

- [ ] **Step 1: 写失败测试**

创建 `tests/test_loop.py`：

```python
# tests/test_loop.py
"""ReAct 循环测试：工具调用序列、错误回注、多工具一轮、迭代上限"""
from agent.llm import LLMMessage, MockLLMClient, ToolCall
from agent.loop import run_agent
from tools.file_tools import FileContent, ToolError


def test_agent_read_then_finish(tmp_path):
    ws = tmp_path / "ws"
    (ws / "demo").mkdir(parents=True)
    (ws / "demo" / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={"path": "demo/a.py"})]),
        LLMMessage(role="assistant", content="任务完成"),
    ]
    result = run_agent("读取 a.py", MockLLMClient(script), workspace_root=str(ws))
    assert result.stopped_by_limit is False
    assert result.final_answer == "任务完成"
    assert result.iteration_count == 2
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.tool_name == "read_file"
    assert isinstance(step.result, FileContent)
    assert step.result.path == "demo/a.py"


def test_agent_tool_error_does_not_stop_loop(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={"path": "no_such.py"})]),
        LLMMessage(role="assistant", content="文件不存在，已停止"),
    ]
    result = run_agent("读取文件", MockLLMClient(script), workspace_root=str(ws))
    assert isinstance(result.steps[0].result, ToolError)
    assert result.final_answer == "文件不存在，已停止"
    assert result.iteration_count == 2


def test_agent_multiple_tool_calls_in_one_turn(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a.py").write_text("y=2\n", encoding="utf-8")
    script = [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="list_files", arguments={}),
            ToolCall(id="c2", name="read_file", arguments={"path": "a.py"}),
        ]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent("查看项目", MockLLMClient(script), workspace_root=str(ws))
    assert len(result.steps) == 2
    assert result.steps[0].tool_name == "list_files"
    assert result.steps[1].tool_name == "read_file"


def test_agent_stops_at_iteration_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [LLMMessage(role="assistant", tool_calls=[
        ToolCall(id="c1", name="list_files", arguments={})])] * 5
    result = run_agent("任务", MockLLMClient(script), max_iterations=3,
                       workspace_root=str(ws))
    assert result.stopped_by_limit is True
    assert result.iteration_count == 3
    assert len(result.steps) == 3
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_loop.py -v`

Expected: FAILED x4 —— `ModuleNotFoundError: No module named 'agent.loop'`。

- [ ] **Step 3: 最小实现**

创建 `agent/loop.py`：

```python
# agent/loop.py
"""最小 ReAct 循环：LLM 决策 → 工具调用 → 观察结果 → 循环，上限 max_iterations 轮。"""
import json
from dataclasses import asdict, dataclass

from agent.llm import LLMClient, ToolSpec
from tools.file_tools import (
    ToolError,
    list_files,
    read_file,
    search_code,
    write_file,
)

# 系统提示词：约束工具使用范围与行为
SYSTEM_PROMPT = (
    "你是编码助手。你可以调用工具查看和修改工作区内的文件。\n"
    "规则：\n"
    "1. 只操作工作区内的文件，不要尝试访问工作区之外的路径。\n"
    "2. 每次工具调用后先观察结果，再决定下一步。\n"
    "3. 任务完成后，用中文总结你做了什么。"
)

# 最大迭代轮数（防死循环）
DEFAULT_MAX_ITERATIONS = 10


@dataclass
class AgentStep:
    """一次工具调用及其结果。"""
    tool_name: str
    arguments: dict
    result: object


@dataclass
class AgentResult:
    """一次任务的完整结果。"""
    steps: list[AgentStep]
    final_answer: str
    iteration_count: int
    stopped_by_limit: bool


def _build_tools() -> list[ToolSpec]:
    """四个文件工具的 JSON schema 描述（暴露给 LLM）。"""
    return [
        ToolSpec(
            name="list_files",
            description="列出工作区内的文件与目录（跳过 .git/__pycache__ 等）",
            parameters={
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "description": "相对工作区根的目录路径，缺省为工作区根",
                    },
                },
            },
        ),
        ToolSpec(
            name="read_file",
            description="读取工作区内文本文件内容（带行号，上限 500KB）",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区根的文件路径",
                    },
                },
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="search_code",
            description="在代码中搜索关键词（ripgrep，自动遵循 .gitignore）",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "root": {
                        "type": "string",
                        "description": "搜索范围目录，缺省为工作区根",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "是否忽略大小写",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="write_file",
            description="写入工作区内文件（自动创建父目录，覆盖已有内容）",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区根的文件路径",
                    },
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        ),
    ]


def _dispatch(name: str, args: dict, workspace_root: str | None) -> object:
    """工具调用分发：返回成功数据或 ToolError。"""
    if name == "list_files":
        return list_files(root=args.get("root"), workspace_root=workspace_root)
    if name == "read_file":
        return read_file(args["path"], workspace_root=workspace_root)
    if name == "search_code":
        return search_code(
            args["query"],
            root=args.get("root"),
            ignore_case=args.get("ignore_case", False),
            workspace_root=workspace_root,
        )
    if name == "write_file":
        return write_file(args["path"], args["content"], workspace_root=workspace_root)
    return ToolError(f"未知工具: {name}")


def _result_to_text(result: object) -> str:
    """工具结果序列化为回注文本；ToolError 转 {error: ...}。"""
    if isinstance(result, ToolError):
        return json.dumps({"error": result.message}, ensure_ascii=False)
    return json.dumps(asdict(result), ensure_ascii=False)


def run_agent(task: str, llm: LLMClient, max_iterations: int = DEFAULT_MAX_ITERATIONS,
              workspace_root: str | None = None) -> AgentResult:
    """执行任务：循环调用 LLM，执行其工具调用并回注结果，直至无工具调用或达上限。"""
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
        # OpenAI 格式：先回注 assistant 的 tool_calls 消息，再逐条回注 tool 结果
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

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_loop.py -v`

Expected: `4 passed`。

- [ ] **Step 5: 全量回归**

运行: `.venv\Scripts\python.exe -m pytest tests/ -v`

Expected: `21 passed`（14 工具 + 3 LLM + 4 循环）。

- [ ] **Step 6: Commit**

```bash
git add agent/loop.py tests/test_loop.py
git commit -m "feat(agent): 新增最小 ReAct 循环 run_agent"
```

---

### Task 7: CLI 入口 main.py

**Files:**
- Create: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `agent.loop.run_agent` / `agent.llm.OpenAICompatClient`
- Produces: `build_parser() -> argparse.ArgumentParser`（参数：`task` 位置参数、`--workspace` 默认 `workspace`、`--max-iterations` 默认 10）；`main(argv: list[str] | None = None) -> int`；`python main.py "<任务描述>"` 运行入口

- [ ] **Step 1: 写失败测试**

创建 `tests/test_main.py`：

```python
# tests/test_main.py
"""CLI 测试：参数解析与 main 输出（注入 fake，不触发真实 LLM）"""
import main
from agent.loop import AgentResult


def test_parser_defaults():
    args = main.build_parser().parse_args(["任务"])
    assert args.max_iterations == 10
    assert args.workspace == "workspace"


def test_main_prints_result(monkeypatch, capsys):
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["测试任务", "--max-iterations", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "测试任务" in out
    assert "搞定" in out
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_main.py -v`

Expected: FAILED x2 —— `ModuleNotFoundError: No module named 'main'`。

- [ ] **Step 3: 最小实现**

创建 `main.py`：

```python
# main.py
"""CLI 入口：python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10]"""
import argparse
import sys

from dotenv import load_dotenv

from agent.llm import OpenAICompatClient
from agent.loop import run_agent


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Knowledge-Augmented Autonomous Coding Agent - W1 本地工作区",
    )
    parser.add_argument("task", help="任务描述，例如：在 demo-project 中定位某函数并修改")
    parser.add_argument("--workspace", default="workspace",
                        help="工作区根目录（默认 workspace）")
    parser.add_argument("--max-iterations", type=int, default=10,
                        help="最大迭代轮数（默认 10）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行一次 Agent 任务并打印过程。"""
    load_dotenv()
    args = build_parser().parse_args(argv)
    llm = OpenAICompatClient()
    result = run_agent(args.task, llm, max_iterations=args.max_iterations,
                       workspace_root=args.workspace)
    print(f"任务: {args.task}")
    print(f"工作区: {args.workspace}")
    for i, step in enumerate(result.steps, 1):
        print(f"[步骤 {i}] {step.tool_name}({step.arguments})")
    print(f"迭代轮数: {result.iteration_count}")
    if result.stopped_by_limit:
        print("提示: 已达到迭代上限")
    print(f"最终回答: {result.final_answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_main.py -v`

Expected: `2 passed`。

- [ ] **Step 5: 创建 .env.example 并提交**

创建 `.env.example`：

```
# LLM 配置模板：复制为 .env 后填写真实值（.env 不入库）
opencode_go_api=你的API_KEY
OPENCODE_GO_BASE_URL=https://你的端点地址
OPENCODE_GO_MODEL=deepseek-4-flash
```

```bash
git add main.py tests/test_main.py .env.example
git commit -m "feat(cli): 新增 main.py 命令行入口与 .env 配置模板"
```

---

### Task 8: 全量验证与窗口收尾

**Files:**
- Modify: `readme.md`（快速开始补充 W1 用法）
- Modify: `docs/devlog.md`（W1 记录）
- Modify: `docs/roadmap.md`（W1 标记完成，W2 待开始）

**Interfaces:**
- Consumes: 全部 Task 1-7 交付物

- [ ] **Step 1: 全量测试**

运行: `.venv\Scripts\python.exe -m pytest tests/ -v`

Expected: `23 passed`（14 + 3 + 4 + 2）。

- [ ] **Step 2: 真实项目演示（需要用户参与）**

请用户将 `D:\AI project` 下一个小型真实项目放入 `workspace/demo-project`，然后执行：

```bash
.venv\Scripts\python.exe main.py "在 demo-project 中定位 XX 函数并添加一行中文注释" --workspace workspace
```

Expected: Agent 依次调用 search_code → read_file → write_file → 中文总结；人工核对修改正确、无越界操作。

- [ ] **Step 3: 更新文档**

- `readme.md`：快速开始补充 `python main.py "<任务描述>"` 用法与 W1 完成状态。
- `docs/devlog.md`：记录 W1 实现要点、测试数、演示结果。
- `docs/roadmap.md`：W1 全部 checkbox 勾选，状态改「已完成」。

- [ ] **Step 4: Commit 文档**

```bash
git add readme.md docs/devlog.md docs/roadmap.md
git commit -m "docs: W1 完成，更新 README/devlog/路线图"
```

- [ ] **Step 5: Review 后合并（退出窗口）**

- 使用 requesting-code-review 审查全部变更，修复问题后再提交。
- 合并回 main 并清理（以下命令在**仓库根** `D:\AI project\Knowledge-Augmented Autonomous Coding Agent` 执行）：

```bash
git checkout main
git merge --no-ff feature/w1-local-workspace -m "merge: W1 本地 Workspace 完成"
git worktree remove .worktrees/w1-local-workspace
git branch -d feature/w1-local-workspace
```

Expected: main 上 `23 passed`；worktree 已删除；W1 窗口关闭。

---

## 附录：实施修正记录（2026-08-15，实现与本文档字面量的偏差）

以下偏差均为实施中发现并已在代码中修复的必要修正，正文保持原样以便对照：

| # | 位置 | 偏差 | 原因与修复 |
|---|------|------|-----------|
| 1 | Task 2 测试 `test_read_file_with_line_numbers` | 补 `ws.mkdir()` | `Path.write_text` 不创建父目录，原字面量必现 FileNotFoundError |
| 2 | Task 2 测试 size 断言 | `fc.size == 8` → `fc.size == len((ws/"a.py").read_bytes())` | Windows 下 write_text 将 LF 转 CRLF，磁盘 size 为 10 而非 8；且经 review 指出原改为 `stat().st_size` 属同义断言（AGENTS.md 禁止），最终以 read_bytes 独立计算 |
| 3 | Task 3 四个 search_code 测试 | 各补 `ws.mkdir()` | 同 #1 |
| 4 | Task 4 测试 `test_write_file_overwrites` | 补 `ws.mkdir()` | 同 #1 |
| 5 | Task 6 测试 `test_agent_multiple_tool_calls_in_one_turn` | 补 `ws.mkdir()` | 同 #1 |
| 6 | Task 6 实现 `_result_to_text` | 新增 list 分支 | `dataclasses.asdict()` 对 list 抛 TypeError；list_files/search_code 返回 list，原字面量必现崩溃（brief 自身测试 3/4 依赖此修复） |
| 7 | Task 5 Interfaces 与代码/文档中的模型默认值 | 模型 ID 修正（deepseek-4-flash → deepseek-v4-flash） | opencode go 实际模型 ID 为 `deepseek-v4-flash`，演示验证；同步修正 `agent/llm.py` 默认值、`.env.example`、`tests/test_llm.py` 断言与 spec §2.1/§4.2 |
