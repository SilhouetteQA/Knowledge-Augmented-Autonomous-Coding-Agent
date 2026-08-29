# W2 Shell + Test 实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施本计划。步骤用 checkbox（`- [ ]`）跟踪。
> 关联 Spec：`docs/specs/2026-08-15-w2-shell-test.md`（已批准）

**Goal:** 交付 run_command / run_tests / git 只读三工具 + LangGraph 显式阶段编排（plan → decide ⇄ execute → verify 强制测试 → reflect 重试），Agent 能自主完成「定位 → 修改 → 测试 → 失败 → 分析 → 再修 → 通过」闭环。

**Architecture:** `tools/shell_tools.py`（subprocess 封装，workspace 路径安全 + 超时 + 截断 + 结构化结果）→ `agent/graph.py`（LangGraph StateGraph 六节点 + 条件路由 + 双上限）→ W1 的 `run_agent` 保留回归。

**Tech Stack:** Python 3.12 / pytest 9.1.1 / langgraph 1.2.6（vendoring）/ ripgrep 14.1.1 / subprocess

## Global Constraints

- 环境：Windows；所有命令在 worktree 根（`.worktrees/w2-shell-test`）执行（PowerShell）
- venv：从 W1 残留 `.worktrees/w1-local-workspace/.venv` **复制**重建（沙箱禁止 python -m venv）；sitecustomize 补丁随复制保留；测试命令统一 `.venv\Scripts\python.exe -m pytest ...`；search_code 相关测试需 `$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"`
- 测试 seam（已确认）：`run_command` / `run_tests` / `git_status` / `git_diff` / `git_log` + `run_agent_graph(task)`；red 先行、green 后收
- 错误不崩溃：ToolError 结构化返回；run_command 超时返回 CommandResult(timeout=True) 不抛异常
- 命令 cwd 必须位于 workspace 根内（复用 `resolve_workspace_path`）
- 代码中文注释、UTF-8、无 emoji、不写 fallback
- 提交：Conventional Commits 中文 message；每任务结束一次 commit
- langgraph 依赖声明同步进 `pyproject.toml`（vendoring 环境无法 pip 验证，声明保持真值）

---

### Task 1: run_command 工具（含环境重建）

**Files:**
- Create: `tools/shell_tools.py`（本任务只含 CommandResult / MAX_COMMAND_OUTPUT / run_command）
- Test: `tests/test_shell_tools.py`（本任务只含 run_command 用例）

**Interfaces:**
- Consumes: `tools.file_tools.ToolError` / `resolve_workspace_path`（W1）
- Produces: `CommandResult(timeout: bool, stdout: str, stderr: str, exit_code: int, duration: float)`；`MAX_COMMAND_OUTPUT = 100 * 1024`；`run_command(command: str, cwd: str | None = None, timeout: int = 60, workspace_root: str | None = None) -> CommandResult | ToolError`

- [ ] **Step 1: 重建 venv（从 W1 残留复制）**

在 worktree 根执行：

```powershell
Copy-Item "D:\AI project\Knowledge-Augmented Autonomous Coding Agent\.worktrees\w1-local-workspace\.venv" ".venv" -Recurse -Force -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m pytest --version
```

Expected: pytest 9.1.1 可用（sitecustomize 补丁随目录复制保留，`.venv_tmp` 会自动重建）。若复制不完整（个别包缺失），从 `D:\CodexPython312\Lib\site-packages` 补复制缺失包（pytest / openai / python-dotenv / pluggy / iniconfig / packaging / pygments / exceptiongroup / colorama，含 dist-info）。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_shell_tools.py`：

```python
# tests/test_shell_tools.py
"""Shell 工具测试：run_command 成功/失败/超时/越界/截断"""
from tools.file_tools import ToolError
from tools.shell_tools import MAX_COMMAND_OUTPUT, run_command


def test_run_command_success(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('hello')\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0
    assert "hello" in r.stdout
    assert r.timeout is False
    assert r.duration >= 0


def test_run_command_failure(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"import sys; sys.exit(3)\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 3


def test_run_command_timeout(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"import time; time.sleep(5)\"", timeout=1,
                    workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.timeout is True
    assert r.duration < 5


def test_run_command_cwd_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("echo hi", cwd="../../..", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "越界" in r.message


def test_run_command_output_truncated(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('x' * 200000)\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert len(r.stdout) <= MAX_COMMAND_OUTPUT + 20  # 截断标记余量
    assert "截断" in r.stdout
```

- [ ] **Step 3: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py -v`

Expected: FAILED x5 —— `ModuleNotFoundError: No module named 'tools.shell_tools'`。

- [ ] **Step 4: 最小实现**

创建 `tools/shell_tools.py`：

```python
# tools/shell_tools.py
"""Shell 工具：run_command / run_tests / git 只读三件套。

所有命令以 workspace 根为安全边界（cwd 必须位于工作区内）；错误返回 ToolError。
"""
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


def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 60,
    workspace_root: str | None = None,
) -> CommandResult | ToolError:
    """在 workspace 内 cwd 执行 shell 命令；超时 kill 置 timeout=True；输出截断。"""
    base = resolve_workspace_path(cwd or "", workspace_root)
    if isinstance(base, ToolError):
        return base
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=base, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            timeout=True,
            stdout=_truncate(e.stdout or ""),
            stderr=_truncate(e.stderr or ""),
            exit_code=-1,
            duration=round(time.monotonic() - start, 3),
        )
    except OSError as e:
        return ToolError(f"命令执行失败: {e}")
    return CommandResult(
        timeout=False,
        stdout=_truncate(proc.stdout),
        stderr=_truncate(proc.stderr),
        exit_code=proc.returncode,
        duration=round(time.monotonic() - start, 3),
    )
```

- [ ] **Step 5: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py -v`

Expected: `5 passed`。

- [ ] **Step 6: Commit**

```bash
git add tools/shell_tools.py tests/test_shell_tools.py
git commit -m "feat(tools): 新增 run_command 工具（超时/截断/workspace 约束）"
```

---

### Task 2: run_tests 工具（pytest 封装）

**Files:**
- Modify: `tools/shell_tools.py`（追加 TestFailure / TestResult / run_tests）
- Test: `tests/test_shell_tools.py`（追加 run_tests 用例）

**Interfaces:**
- Consumes: `resolve_workspace_path` / `ToolError` / `sys.executable`（Task 1 + W1）
- Produces: `TestFailure(test: str, message: str)`；`TestResult(passed: int, failed: int, error: int, total: int, duration: float, failures: list[TestFailure])`；`run_tests(path: str | None = None, workspace_root: str | None = None) -> TestResult | ToolError`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_shell_tools.py`：

```python
def _write_project(tmp_path, files):
    """测试设施：在 tmp 下按 dict 写项目文件。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for name, content in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ws


def test_run_tests_all_pass(tmp_path):
    ws = _write_project(tmp_path, {
        "test_ok.py": "def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 2 == 2\n",
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 2
    assert r.failed == 0
    assert r.error == 0
    assert r.total == 2
    assert r.failures == []


def test_run_tests_with_failures(tmp_path):
    ws = _write_project(tmp_path, {
        "test_mix.py": (
            "def test_ok():\n    assert 1 == 1\n"
            "def test_bad():\n    assert 1 == 2\n"
        ),
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 1
    assert r.failed == 1
    assert r.total == 2
    assert len(r.failures) == 1
    assert "test_bad" in r.failures[0].test


def test_run_tests_collection_error(tmp_path):
    ws = _write_project(tmp_path, {
        "test_broken.py": "def test_x(:\n    pass\n",  # 语法错误
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.error >= 1


def test_run_tests_subpath(tmp_path):
    ws = _write_project(tmp_path, {
        "sub/test_a.py": "def test_a():\n    assert 1 == 1\n",
        "test_other.py": "def test_other():\n    assert 1 == 1\n",
    })
    r = run_tests(path="sub", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 1
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py::test_run_tests_all_pass -v`

Expected: FAILED —— `ImportError: cannot import name 'run_tests'`。

- [ ] **Step 3: 最小实现**

追加到 `tools/shell_tools.py`（文件头部 import 区补 `import re` 与 `import sys`）：

```python
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


def run_tests(path: str | None = None, workspace_root: str | None = None) -> TestResult | ToolError:
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
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py -v`

Expected: `9 passed`。

- [ ] **Step 5: Commit**

```bash
git add tools/shell_tools.py tests/test_shell_tools.py
git commit -m "feat(tools): 新增 run_tests 工具（pytest 结构化结果）"
```

---

### Task 3: git 只读工具

**Files:**
- Modify: `tools/shell_tools.py`（追加 _run_git / GitStatus / GitDiff / GitLog / git_status / git_diff / git_log）
- Test: `tests/test_shell_tools.py`（追加 git 用例）

**Interfaces:**
- Consumes: `resolve_workspace_path` / `ToolError` / `_truncate`（Task 1）
- Produces: `GitStatus(clean: bool, changes: list[str])`；`GitDiff(stat: str, diff: str)`；`GitLog(entries: list[str])`；`git_status(workspace_root=None) -> GitStatus | ToolError`；`git_diff(workspace_root=None) -> GitDiff | ToolError`；`git_log(count: int = 10, workspace_root=None) -> GitLog | ToolError`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_shell_tools.py`：

```python
import subprocess as _sp


def _make_git_repo(tmp_path):
    """测试设施：初始化 tmp git 仓库并做一次提交。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    _sp.run(["git", "init", "-b", "main"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "config", "user.name", "tester"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "config", "user.email", "t@test.dev"], cwd=ws, capture_output=True, check=True)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True)
    return ws


def test_git_status_clean_and_dirty(tmp_path):
    ws = _make_git_repo(tmp_path)
    s = git_status(workspace_root=str(ws))
    assert not isinstance(s, ToolError)
    assert s.clean is True
    assert s.changes == []
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    s2 = git_status(workspace_root=str(ws))
    assert s2.clean is False
    assert any("a.py" in c for c in s2.changes)


def test_git_diff_shows_change(tmp_path):
    ws = _make_git_repo(tmp_path)
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    d = git_diff(workspace_root=str(ws))
    assert not isinstance(d, ToolError)
    assert "a.py" in d.stat
    assert "+x = 2" in d.diff


def test_git_log_entries(tmp_path):
    ws = _make_git_repo(tmp_path)
    log = git_log(workspace_root=str(ws))
    assert not isinstance(log, ToolError)
    assert len(log.entries) == 1
    assert "init" in log.entries[0]


def test_git_tools_not_a_repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = git_status(workspace_root=str(ws))
    assert isinstance(s, ToolError)
```

（同时更新 `tests/test_shell_tools.py` 头部 import：`from tools.shell_tools import MAX_COMMAND_OUTPUT, git_diff, git_log, git_status, run_command, run_tests`）

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py::test_git_status_clean_and_dirty -v`

Expected: FAILED —— `ImportError: cannot import name 'git_status'`。

- [ ] **Step 3: 最小实现**

追加到 `tools/shell_tools.py`：

```python
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


def _run_git(args: list[str], workspace_root: str | None) -> str | ToolError:
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


def git_status(workspace_root: str | None = None) -> GitStatus | ToolError:
    """查看工作区 git 状态（--short）。"""
    out = _run_git(["status", "--short"], workspace_root)
    if isinstance(out, ToolError):
        return out
    lines = [l for l in out.splitlines() if l.strip()]
    return GitStatus(clean=len(lines) == 0, changes=lines)


def git_diff(workspace_root: str | None = None) -> GitDiff | ToolError:
    """查看未提交变更（--stat + 完整 diff，截断）。"""
    stat = _run_git(["diff", "--stat"], workspace_root)
    if isinstance(stat, ToolError):
        return stat
    diff = _run_git(["diff"], workspace_root)
    if isinstance(diff, ToolError):
        return diff
    return GitDiff(stat=stat.strip(), diff=_truncate(diff))


def git_log(count: int = 10, workspace_root: str | None = None) -> GitLog | ToolError:
    """查看最近提交（--oneline）。"""
    out = _run_git(["log", "--oneline", f"-n{count}"], workspace_root)
    if isinstance(out, ToolError):
        return out
    return GitLog(entries=[l for l in out.splitlines() if l.strip()])
```

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_shell_tools.py -v`

Expected: `13 passed`。

- [ ] **Step 5: Commit**

```bash
git add tools/shell_tools.py tests/test_shell_tools.py
git commit -m "feat(tools): 新增 git 只读三件套（status/diff/log）"
```

---

### Task 4: LangGraph 基础图（plan / decide / execute / finalize）

**Files:**
- Create: `agent/graph.py`（本任务只含 AgentState / PLAN_PROMPT / _decide_system / _parse_plan / plan_node / decide_node / execute_node / finalize_node / finalize_limited_node / build_graph / run_agent_graph，**不含 verify/reflect**——decide 无工具调用时直接 finalize）
- Test: `tests/test_graph.py`（本任务只含成功路径用例）
- Modify: `pyproject.toml`（dependencies 加 `langgraph>=1.2`）

**Interfaces:**
- Consumes: `agent.llm`（LLMClient / LLMMessage / ToolCall）、`agent.loop`（`AgentStep` / `_build_tools` / `_dispatch` / `_result_to_text`，直接导入复用不搬移）
- Produces: `AgentState`（TypedDict）；`AgentGraphResult(plan, steps, final_answer, iteration_count, verify_rounds, stopped_by_limit, test_results)`；`run_agent_graph(task, llm, max_iterations=20, max_verify_rounds=3, workspace_root=None) -> AgentGraphResult`（max_verify_rounds 本任务暂未使用，签名保留）

- [ ] **Step 1: vendoring langgraph 并声明依赖**

```powershell
# 从基础解释器复制 langgraph 全家 + langchain 基础包
$base = "D:\CodexPython312\Lib\site-packages"
$pkgs = @("langgraph", "langgraph_checkpoint", "langgraph_prebuilt", "langgraph_sdk",
          "langchain", "langchain_core", "langchain_protocol")
foreach ($p in $pkgs) {
  Copy-Item "$base\$p" ".venv\Lib\site-packages\" -Recurse -Force
  Copy-Item "$base\$p-*.dist-info" ".venv\Lib\site-packages\" -Recurse -Force -ErrorAction SilentlyContinue
}
.venv\Scripts\python.exe -c "import langgraph; print('langgraph', langgraph.__version__)"
```

Expected: `langgraph 1.2.6`（或同版本）。若 import 报缺依赖，从 `$base` 补复制缺失包并重试。

修改 `pyproject.toml` `[project]`：

```toml
dependencies = [
    "openai>=1.40",
    "python-dotenv>=1.0",
    "langgraph>=1.2",
]
```

- [ ] **Step 2: 写失败测试（成功路径）**

创建 `tests/test_graph.py`：

```python
# tests/test_graph.py
"""LangGraph 编排测试：成功路径 / 失败修复路径 / 双上限"""
import json

from agent.graph import run_agent_graph
from agent.llm import LLMMessage, MockLLMClient, ToolCall


def _plan_msg(steps):
    """plan 节点的 JSON 数组响应。"""
    return LLMMessage(role="assistant", content=json.dumps(steps, ensure_ascii=False))


def test_graph_success_path(tmp_path):
    ws = tmp_path / "ws"
    (ws / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    script = [
        _plan_msg(["读取代码", "完成"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="read_file", arguments={"path": "test_ok.py"})]),
        LLMMessage(role="assistant", content="任务完成"),
    ]
    result = run_agent_graph("查看测试文件", MockLLMClient(script), workspace_root=str(ws))
    assert result.stopped_by_limit is False
    assert result.plan == ["读取代码", "完成"]
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "read_file"
    assert result.final_answer == "任务完成"
```

- [ ] **Step 3: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`

Expected: FAILED —— `ModuleNotFoundError: No module named 'agent.graph'`。

- [ ] **Step 4: 最小实现（基础图，无 verify）**

创建 `agent/graph.py`：

```python
# agent/graph.py
"""LangGraph 显式阶段编排：plan → decide ⇄ execute → (verify) → reflect → finalize。

Task 4 版本：核心循环（plan/decide/execute/finalize），decide 无工具调用即完成；
verify/reflect 节点由 Task 5 加入，届时 decide 无工具调用改为进入强制验证。
"""
import json
import re
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph

from agent.llm import LLMClient, LLMMessage, ToolCall
from agent.loop import AgentStep, _build_tools, _dispatch, _result_to_text

# 计划节点提示词：只输出 JSON 数组
PLAN_PROMPT = (
    "你是编码助手。请把任务拆解为 3-6 个具体步骤。\n"
    "只输出 JSON 数组字符串（如 [\"步骤1\", \"步骤2\"]），不要其他内容。"
)


def _decide_system(plan: list[str]) -> str:
    """decide 节点系统提示词（引用计划）。"""
    plan_text = "\n".join(f"- {p}" for p in plan) or "- （无计划）"
    return (
        "你是编码助手。你可以调用工具查看、修改和执行工作区内的代码。\n"
        f"你的计划：\n{plan_text}\n"
        "规则：只操作工作区内文件；每次工具调用后先观察结果再行动；完成后用中文总结。"
    )


class AgentState(TypedDict):
    task: str
    plan: list[str]
    messages: list[dict]
    steps: list[AgentStep]
    iteration: int
    verify_rounds: int
    test_result: object | None
    test_results: list
    pending_tool_calls: list[ToolCall] | None
    final_answer: str
    status: str


@dataclass
class AgentGraphResult:
    plan: list[str]
    steps: list[AgentStep]
    final_answer: str
    iteration_count: int
    verify_rounds: int
    stopped_by_limit: bool
    test_results: list


def _parse_plan(content: str) -> list[str]:
    """解析 plan 节点输出的 JSON 数组；失败返回占位。"""
    text = content.strip()
    text = re.sub(r"^```(json)?|```$", "", text).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        pass
    return ["（计划生成失败，直接执行）"]


def build_graph(llm: LLMClient, max_iterations: int = 20,
                workspace_root: str | None = None) -> object:
    """构建 LangGraph 图（闭包捕获 llm 与上限参数）。"""

    def plan_node(state: AgentState) -> dict:
        msg = llm.chat(
            [{"role": "system", "content": PLAN_PROMPT},
             {"role": "user", "content": state["task"]}],
            [],
        )
        return {"plan": _parse_plan(msg.content or "")}

    def decide_node(state: AgentState) -> dict:
        system = _decide_system(state["plan"])
        messages = [{"role": "system", "content": system}] + state["messages"]
        msg = llm.chat(messages, _build_tools())
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in msg.tool_calls
            ]
        return {
            "messages": state["messages"] + [
                {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}],
            "pending_tool_calls": msg.tool_calls,
            "iteration": state["iteration"] + 1,
        }

    def execute_node(state: AgentState) -> dict:
        steps = list(state["steps"])
        messages = list(state["messages"])
        for tc in state["pending_tool_calls"] or []:
            result = _dispatch(tc.name, tc.arguments, workspace_root)
            steps.append(AgentStep(tool_name=tc.name, arguments=tc.arguments, result=result))
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _result_to_text(result),
            })
        return {"steps": steps, "messages": messages, "pending_tool_calls": None}

    def finalize_node(state: AgentState) -> dict:
        content = state["messages"][-1].get("content") if state["messages"] else ""
        return {"final_answer": content or "任务完成", "status": "done"}

    def finalize_limited_node(state: AgentState) -> dict:
        return {"final_answer": "已达到上限，任务未完成", "status": "limit"}

    def route_after_decide(state: AgentState) -> str:
        if state["pending_tool_calls"]:
            return "execute" if state["iteration"] <= max_iterations else "finalize_limited"
        return "finalize"

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("decide", decide_node)
    g.add_node("execute", execute_node)
    g.add_node("finalize", finalize_node)
    g.add_node("finalize_limited", finalize_limited_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "decide")
    g.add_conditional_edges("decide", route_after_decide,
                            {"execute": "execute", "finalize": "finalize",
                             "finalize_limited": "finalize_limited"})
    g.add_edge("execute", "decide")
    g.add_edge("finalize", END)
    g.add_edge("finalize_limited", END)
    return g.compile()


def run_agent_graph(task: str, llm: LLMClient, max_iterations: int = 20,
                    max_verify_rounds: int = 3,
                    workspace_root: str | None = None) -> AgentGraphResult:
    """执行任务：LangGraph 图驱动，返回结构化结果。"""
    graph = build_graph(llm, max_iterations, workspace_root)
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

- [ ] **Step 5: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`

Expected: `1 passed`。

- [ ] **Step 6: Commit**

```bash
git add agent/graph.py tests/test_graph.py pyproject.toml
git commit -m "feat(agent): 新增 LangGraph 编排图（plan/decide/execute）"
```

---

### Task 5: verify / reflect 节点（强制验证闭环）与双上限

**Files:**
- Modify: `agent/graph.py`（追加 verify_node / reflect_node / route_after_verify / _reflect_system；`_decide_system` 加验证轮次参数；`route_after_decide` 无工具调用改为进入 verify；注册 verify/reflect 节点与边；imports 补 `asdict` 与 `run_tests`/`TestResult`）
- Test: `tests/test_graph.py`（追加失败修复路径 / 验证上限 / 迭代上限用例；更新成功路径断言 verify_rounds==1 与 test_results 长度）

**Interfaces:**
- Consumes: `run_tests` / `TestResult`（Task 2）；Task 4 的图结构
- Produces: `verify_node`（run_tests 入 state，verify_rounds+1）；`reflect_node`（失败分析注入 messages）；`route_after_verify`（通过→finalize / 超轮→finalize_limited / 失败→reflect）；图新增 verify/reflect 节点与边

- [ ] **Step 1: 写失败测试并更新成功路径断言**

追加到 `tests/test_graph.py`：

```python
def test_graph_success_path_verify(tmp_path):
    """成功路径（Task 5 起 decide 无工具调用后强制 verify）。"""
    ws = tmp_path / "ws"
    (ws / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    script = [
        _plan_msg(["完成"]),
        LLMMessage(role="assistant", content="任务完成"),
    ]
    result = run_agent_graph("查看测试", MockLLMClient(script), workspace_root=str(ws))
    assert result.verify_rounds == 1
    assert len(result.test_results) == 1
    assert result.stopped_by_limit is False


def test_graph_failure_then_fix(tmp_path):
    ws = tmp_path / "ws"
    (ws / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    script = [
        _plan_msg(["修复测试"]),
        LLMMessage(role="assistant", content="完成"),
        LLMMessage(role="assistant", content="失败原因：断言错误，需要修改断言值"),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c2", name="write_file",
                     arguments={"path": "test_bad.py",
                                "content": "def test_bad():\n    assert 1 == 1\n"})]),
        LLMMessage(role="assistant", content="已修复"),
    ]
    result = run_agent_graph("修复失败的测试", MockLLMClient(script),
                             workspace_root=str(ws))
    assert result.stopped_by_limit is False
    assert result.verify_rounds == 2
    assert len(result.test_results) == 2
    first, second = result.test_results
    assert first.failed == 1
    assert second.failed == 0


def test_graph_verify_limit(tmp_path):
    ws = tmp_path / "ws"
    (ws / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    script = [
        _plan_msg(["任务"]),
        LLMMessage(role="assistant", content="完成"),
        LLMMessage(role="assistant", content="分析1"),
        LLMMessage(role="assistant", content="完成"),
        LLMMessage(role="assistant", content="分析2"),
        LLMMessage(role="assistant", content="完成"),
        LLMMessage(role="assistant", content="分析3"),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("修复测试", MockLLMClient(script),
                             max_verify_rounds=3, workspace_root=str(ws))
    assert result.stopped_by_limit is True
    assert result.verify_rounds == 3
    assert result.final_answer == "已达到上限，任务未完成"


def test_graph_iteration_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [_plan_msg(["循环"])] + [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id=f"c{i}", name="list_files", arguments={})])
        for i in range(25)
    ]
    result = run_agent_graph("任务", MockLLMClient(script),
                             max_iterations=20, workspace_root=str(ws))
    assert result.stopped_by_limit is True
    assert result.iteration_count == 20
```

同时将 `test_graph_success_path`（Task 4 用例）追加断言：

```python
    assert result.verify_rounds == 1
    assert len(result.test_results) == 1
```

- [ ] **Step 2: 运行测试确认失败**

运行: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`

Expected: FAILED x4 —— `ValueError: ... node 'verify' not found`（verify 节点未注册）。

- [ ] **Step 3: 最小实现**

修改 `agent/graph.py`：

imports 区更新为：

```python
import json
import re
from dataclasses import asdict, dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph

from agent.llm import LLMClient, LLMMessage, ToolCall
from agent.loop import AgentStep, _build_tools, _dispatch, _result_to_text
from tools.shell_tools import TestResult, run_tests
```

模块级追加 reflect 提示词：

```python
def _reflect_system(details: str) -> str:
    """reflect 节点提示词：携带测试失败详情。"""
    return (
        "测试失败。请分析失败原因，给出下一步修复方向。\n"
        f"失败详情：\n{details}"
    )
```

`_decide_system` 更新为：

```python
def _decide_system(plan: list[str], verify_rounds: int, max_verify_rounds: int) -> str:
    """decide 节点系统提示词（引用计划与验证轮次）。"""
    plan_text = "\n".join(f"- {p}" for p in plan) or "- （无计划）"
    return (
        "你是编码助手。你可以调用工具查看、修改和执行工作区内的代码。\n"
        f"你的计划：\n{plan_text}\n"
        f"验证轮次：已进行 {verify_rounds}/{max_verify_rounds} 次。测试全部通过前不要声称完成；"
        "声称完成后系统会自动运行测试验证。\n"
        "规则：只操作工作区内文件；每次工具调用后先观察结果再行动；完成后用中文总结。"
    )
```

`build_graph` 签名追加 `max_verify_rounds: int = 3`，`decide_node` 调用改为 `_decide_system(state["plan"], state["verify_rounds"], max_verify_rounds)`，并追加节点与路由：

```python
    def verify_node(state: AgentState) -> dict:
        tr = run_tests(workspace_root=workspace_root)
        return {
            "test_result": tr,
            "verify_rounds": state["verify_rounds"] + 1,
            "test_results": state["test_results"] + [tr],
        }

    def reflect_node(state: AgentState) -> dict:
        tr = state["test_result"]
        if isinstance(tr, TestResult):
            details = asdict(tr)
        else:
            details = {"error": getattr(tr, "message", str(tr))}
        msg = llm.chat(
            [{"role": "system",
              "content": _reflect_system(json.dumps(details, ensure_ascii=False))},
             {"role": "user", "content": state["task"]}],
            [],
        )
        return {"messages": state["messages"] + [
            {"role": "assistant", "content": f"[失败分析] {msg.content}"}]}

    def route_after_decide(state: AgentState) -> str:
        if state["pending_tool_calls"]:
            return "execute" if state["iteration"] <= max_iterations else "finalize_limited"
        return "verify"

    def route_after_verify(state: AgentState) -> str:
        tr = state["test_result"]
        passed = isinstance(tr, TestResult) and tr.failed == 0 and tr.error == 0
        if passed:
            return "finalize"
        if state["verify_rounds"] >= max_verify_rounds:
            return "finalize_limited"
        return "reflect"
```

图注册段更新为：

```python
    g.add_node("plan", plan_node)
    g.add_node("decide", decide_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("reflect", reflect_node)
    g.add_node("finalize", finalize_node)
    g.add_node("finalize_limited", finalize_limited_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "decide")
    g.add_conditional_edges("decide", route_after_decide,
                            {"execute": "execute", "verify": "verify",
                             "finalize_limited": "finalize_limited"})
    g.add_edge("execute", "decide")
    g.add_conditional_edges("verify", route_after_verify,
                            {"finalize": "finalize", "finalize_limited": "finalize_limited",
                             "reflect": "reflect"})
    g.add_edge("reflect", "decide")
    g.add_edge("finalize", END)
    g.add_edge("finalize_limited", END)
```

`run_agent_graph` 调用改为 `build_graph(llm, max_iterations, max_verify_rounds, workspace_root)`。

- [ ] **Step 4: 运行测试确认通过**

运行: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`

Expected: `5 passed`（成功路径 + 成功路径 verify + 失败修复 + 验证上限 + 迭代上限）。

- [ ] **Step 5: 全量回归**

运行: `.venv\Scripts\python.exe -m pytest tests/ -v`

Expected: `44 passed`（27 W1 + 13 shell/git + 4 图，W1 27 项保持全绿）。

- [ ] **Step 6: Commit**

```bash
git add agent/graph.py tests/test_graph.py
git commit -m "feat(agent): verify/reflect 节点与双上限（强制测试闭环）"
```

---

### Task 6: 全量验证、缺陷仓库演示与窗口收尾

**Files:**
- Modify: `readme.md`（W2 状态与用法）
- Modify: `docs/devlog.md`（W2 记录）
- Modify: `docs/roadmap.md`（W2 完成）
- Modify: `docs/plans/2026-08-15-w2-shell-test.md`（附录：实施修正记录，若有）

**Interfaces:**
- Consumes: 全部 Task 1-5 交付物

- [ ] **Step 1: 全量测试**

运行: `.venv\Scripts\python.exe -m pytest tests/ -v`

Expected: 全绿（约 44 passed）。

- [ ] **Step 2: 缺陷仓库演示（需要用户参与）**

用户指定本地含缺陷仓库位置 → 复制进 `workspace/`（保持原结构，如 `workspace/defect-repo`），然后执行：

```powershell
.venv\Scripts\python.exe -c "from agent.graph import run_agent_graph; from agent.llm import OpenAICompatClient; import asyncio; r = run_agent_graph('修复 defect-repo 中的缺陷，并确保测试通过', OpenAICompatClient()); print(r)"
```

（若演示需 CLI 入口，先与用户确认后用最小方式在 main.py 接线 `--graph` 参数；默认用上面的一行 Python 调用。）

Expected: Agent 自主闭环：计划 → 搜索/读取 → 修改 → verify 失败 → reflect 分析 → 再修 → verify 通过 → 中文总结。人工核对修改正确、测试真实通过。

- [ ] **Step 3: 更新文档**

- `readme.md`：项目状态 W2 完成；快速开始补充 run_tests 说明与图入口。
- `docs/devlog.md`：W2 决策（LangGraph 引入、verify 强制闭环、vendoring 经验）、演示结果。
- `docs/roadmap.md`：W2 checkbox 全部勾选、状态「已完成」。

- [ ] **Step 4: Commit 文档**

```bash
git add readme.md docs/devlog.md docs/roadmap.md docs/plans/2026-08-15-w2-shell-test.md
git commit -m "docs: W2 完成，更新 README/devlog/路线图"
```

- [ ] **Step 5: Review 后合并（退出窗口）**

- 使用 requesting-code-review 审查全部变更，修复问题后再提交。
- 合并回 main 并清理（在**仓库根**执行）：

```bash
git checkout main
git merge --no-ff feature/w2-shell-test -m "merge: W2 Shell + Test 完成"
git worktree remove --force .worktrees/w2-shell-test
git branch -d feature/w2-shell-test
```

Expected: main 全绿；worktree 已解除登记；W2 窗口关闭。

---

## 已知环境备忘（实施时遵守）

- 沙箱禁止 `python -m venv`（0o700 目录锁）与出站网络：venv 用复制重建；依赖 vendoring 从 `D:\CodexPython312\Lib\site-packages` 复制（含 dist-info）。
- 沙箱子进程 PATH 无 rg：search_code/涉及 rg 的测试命令前设置 `$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"`。
- review package 生成用 UTF-8（`Out-File -Encoding utf8`），避免 UTF-16 问题。
- 测试中 `subprocess` 启动 git 需要 `git config user.name/email`（测试设施内配置）。
- `main.py` 仍调用 W1 `run_agent`；W2 图入口 `run_agent_graph` 的 CLI 接线在 Task 6 与用户确认后以最小方式处理（默认一行 Python 调用，不动 main.py）。

---

## 附录：实施修正记录（2026-08-15，实现与本文档字面量的偏差）

| # | 位置 | 偏差 | 原因与修复 |
|---|------|------|-----------|
| 1 | Task 1 实现 `run_command` | `subprocess.run(shell=True, timeout)` → `Popen + communicate(timeout)` + Win32 进程树终止 | Windows 下 shell=True 超时只杀顶层 cmd，孤儿 python 持有管道致 communicate 阻塞（brief 自身测试 duration<5 无法满足）；Win32 CreateToolhelp32Snapshot 枚举终止后代进程；后经 review 修复异常安全与快照时序（commit a6f1140） |
| 2 | Task 1 测试 | brief 测试字面量在环境内不可行处的最小修复 | 沿用 W1 模式（ws.mkdir() 等）；Task 3 的 `test_git_tools_not_a_repo` 用 GIT_CEILING_DIRECTORIES 锚定（pytest tmp 目录位于本仓库内） |
| 3 | Task 4/5 测试 | 多处补 `ws.mkdir()` | `Path.write_text` 不创建父目录（同 W1 记录模式） |
| 4 | Task 5 测试 `test_graph_iteration_limit` | 断言 `iteration_count == 21`（brief 写 20） | brief 实现 verbatim：decide_node 先 +1 再路由（`iteration <= max_iterations`），20 次执行 + 第 21 次 guard decide = 21；功能上执行轮数上限 20 保持，仅计数器含 guard（review 确认语义正确） |
| 5 | Task 6.5（新增） | 图工具集注册补齐 | 真实演示发现 decide 仅暴露 W1 四工具（spec 要求 8 个）；新增 `_graph_tools` / `_graph_dispatch`（run_command/run_tests/git 三件套），commit 7c5bf7b |
| 6 | 环境备忘修正 | rg 在本会话 PATH 可用（W1 需 RIPGREP_BIN）；venv 复制自 W1 残留 | 与 W1 环境差异，实测记录 |
