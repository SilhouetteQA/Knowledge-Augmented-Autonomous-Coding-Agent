# W4 Repository Intelligence 实施计划

> 状态：本计划已全部完成（对应窗口已合并 main），进度与验收记录见 docs/roadmap.md 与 docs/devlog.md；文内 checkbox 不再回填。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 为 Agent 增加代码结构认知（Python AST → Code Metadata → CodeGraph 查询工具）与域知识检索（search_knowledge），形成 Code + Domain 双知识检索，解决 W2 暴露的「盲目读文件、探索低效」问题。

**Architecture:** `tools/code_parser.py`（标准库 ast 解析，零依赖）→ `tools/code_graph.py`（内存图 + 五类查询）→ 工具接入 `agent/graph.py`（query_code_graph / search_knowledge 两个新 ToolSpec）；`tools/knowledge_client.py` 以兄弟项目 venv 的 python 子进程调用其 Arknights MCP client（本仓库零新增依赖）。

**Tech Stack:** Python 3.12+ / 标准库 ast / pytest / 兄弟项目《Arknights LLM Wiki》`arknights_wiki.mcp_server`（MCP server 5 工具）

## Global Constraints

- 仓库根：`.worktrees/w4-repo-intelligence`；测试命令：`.venv\Scripts\python.exe -m pytest tests/`（工作目录 = 仓库根）
- 中文注释、UTF-8、无 emoji；Conventional Commits 中文提交
- 不写 fallback：兄弟项目不可用必须返回结构化 `ToolError`，绝不静默降级
- 既有 79 项测试必须保持全绿（非 docker 项）
- 设计依据：`docs/specs/2026-08-19-w4-repository-intelligence.md`（已批准）
- 兄弟项目路径：`<sibling-project-dir>`（含 `.venv\Scripts\python.exe` 与 `arknights_wiki` 包）

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `tools/code_parser.py` | 新增 | CodeMetadata 数据模型 + `parse_python_file`（AST 单文件）+ `build_metadata`（全工作区） |
| `tools/code_graph.py` | 新增 | `CodeGraph` 内存图 + `build_code_graph` + `query_code_graph` 工具分发 |
| `tools/knowledge_client.py` | 新增 | `KnowledgeClient` 协议 + `MockKnowledgeClient` + `ArknightsMCPClient` + `get_knowledge_client` 工厂 |
| `agent/graph.py` | 修改 | `_graph_tools` 加 2 个 ToolSpec；`_graph_dispatch` 加 2 个分支；`build_graph`/`run_agent_graph` 增加 `code_graph`/`knowledge_client` 注入参数 |
| `main.py` | 修改 | `--graph` 分支自动构建代码索引与知识客户端 |
| `pyproject.toml` | 修改 | pytest 注册 `mcp` marker |
| `.env.example` | 修改 | W4 配置项说明 |
| `tests/test_code_parser.py` | 新增 | AST 解析单元测试 |
| `tests/test_code_graph.py` | 新增 | CodeGraph 查询单元测试 |
| `tests/test_knowledge_client.py` | 新增 | 知识客户端单元测试 |
| `tests/test_graph.py` | 修改 | 新工具 dispatch 回归测试 |
| `tests/test_main.py` | 修改 | --graph 分支注入测试 |
| `tests/test_mcp_integration.py` | 新增 | MCP 集成测试（`@pytest.mark.mcp`，条件跳过） |

---

### Task 1: code_parser 数据模型与单文件解析

**Files:**
- Create: `tools/code_parser.py`
- Test: `tests/test_code_parser.py`

**Interfaces:**
- Produces: `FunctionInfo(name, module, class_name, calls)` / `ClassInfo(name, module, bases, methods)` / `ModuleInfo(name, path, imports, classes, functions)` / `CodeMetadata(modules, warnings)` / `parse_python_file(path, module_name) -> ModuleInfo`
- 语义：`calls` 为 AST `Call` 节点静态收集（属性链取末段：`a.b.c()` → `c`，`self.work()` → `work`）；语法错误抛 `SyntaxError`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_code_parser.py
"""代码解析测试：AST 单文件解析 / 全工作区构建 / 语法错误跳过"""
import textwrap

import pytest

from tools.code_parser import build_metadata, parse_python_file
from tools.file_tools import ToolError

SAMPLE = textwrap.dedent('''
    import os
    from pathlib import Path

    class Base:
        def run(self):
            return self.work()

    class Worker(Base):
        def work(self):
            return helper(1)

    def helper(x):
        return x + 1

    def main():
        w = Worker()
        w.run()
        print(os.getcwd())
''')


def test_parse_extracts_imports(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    assert mod.imports == ["os", "pathlib"]


def test_parse_extracts_classes(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    assert {c.name for c in mod.classes} == {"Base", "Worker"}
    worker = next(c for c in mod.classes if c.name == "Worker")
    assert worker.bases == ["Base"]
    assert worker.methods == ["work"]
    base = next(c for c in mod.classes if c.name == "Base")
    assert base.methods == ["run"]


def test_parse_extracts_functions_and_calls(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    helper = next(x for x in mod.functions if x.name == "helper")
    assert helper.class_name is None
    assert helper.calls == []
    main = next(x for x in mod.functions if x.name == "main")
    assert {"Worker", "run", "print", "getcwd"} <= set(main.calls)
    work = next(x for x in mod.functions if x.name == "work")
    assert work.class_name == "Worker"
    assert work.calls == ["helper"]


def test_parse_syntax_error_raises(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        parse_python_file(str(bad), "bad")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_parser.py -v`
Expected: FAIL（ModuleNotFoundError: tools.code_parser）

- [ ] **Step 3: 最小实现**

```python
# tools/code_parser.py
"""代码解析：Python AST 提取代码元数据（模块/类/函数/导入/调用）。

W4 Repository Intelligence 的解析层：仅标准库静态分析；
语法错误文件跳过并计入 warnings（不中断全量构建）。
"""
import ast
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.file_tools import IGNORED_DIRS, ToolError


@dataclass
class FunctionInfo:
    """函数信息：calls 为 AST 静态收集的调用名（属性链取末段）。"""
    name: str
    module: str
    class_name: str | None = None
    calls: list[str] = field(default_factory=list)


@dataclass
class ClassInfo:
    """类信息：bases 为直接基类名，methods 为本体定义的方法名。"""
    name: str
    module: str
    bases: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


@dataclass
class ModuleInfo:
    """模块信息：imports 为直接导入的模块名（import x / from a.b import c → a.b）。"""
    name: str
    path: str
    imports: list[str] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)


@dataclass
class CodeMetadata:
    """全工作区代码元数据：modules 列表 + 解析警告。"""
    modules: list[ModuleInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _callee_name(node: ast.Call) -> str:
    """提取调用目标名：属性链取末段（a.b.c() → c），简化静态分析。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Call):
        return _callee_name(func)
    return "<dynamic>"


def _base_name(node) -> str:
    """基类名：Name 取 id，Attribute 取末段，其余占位。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "<expr>"


def _module_name_from_path(path: Path, root: Path) -> str:
    """相对根路径推导模块名：foo/bar.py → foo.bar；__init__.py → 所在目录名。"""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "<root>"


def _parse_function(node: ast.FunctionDef, module_name: str,
                    class_name: str | None = None) -> FunctionInfo:
    """解析函数定义：静态收集调用名（ast.walk 覆盖嵌套调用）。"""
    calls = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _callee_name(sub)
            if name not in calls:
                calls.append(name)
    return FunctionInfo(name=node.name, module=module_name,
                        class_name=class_name, calls=calls)


def parse_python_file(path: str, module_name: str) -> ModuleInfo:
    """解析单个 Python 文件为 ModuleInfo；语法错误抛 SyntaxError（由调用方记录）。"""
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    imports: list[str] = []
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            classes.append(ClassInfo(
                name=node.name, module=module_name,
                bases=[_base_name(b) for b in node.bases],
                methods=methods))
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    functions.append(_parse_function(sub, module_name, node.name))
        elif isinstance(node, ast.FunctionDef):
            functions.append(_parse_function(node, module_name))
    return ModuleInfo(name=module_name, path=path, imports=imports,
                      classes=classes, functions=functions)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_parser.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/code_parser.py tests/test_code_parser.py
git commit -m "feat(tools): 新增 code_parser，Python AST 提取模块/类/函数/导入/调用元数据"
```

---

### Task 2: build_metadata 全工作区构建

**Files:**
- Modify: `tools/code_parser.py`
- Test: `tests/test_code_parser.py`（追加）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_code_parser.py`）

```python
def test_build_metadata_full_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ws / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "pkg" / "mod.py").write_text("import os\n\nclass C:\n    pass\n", encoding="utf-8")
    (ws / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    meta = build_metadata(str(ws))
    assert {m.name for m in meta.modules} == {"app", "pkg", "pkg.mod"}
    assert any("broken.py" in w for w in meta.warnings)


def test_build_metadata_skips_ignored_dirs(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".venv").mkdir()
    (ws / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".venv" / "lib.py").write_text("y = 2\n", encoding="utf-8")
    meta = build_metadata(str(ws))
    assert [m.name for m in meta.modules] == ["ok"]


def test_build_metadata_missing_workspace(tmp_path):
    result = build_metadata(str(tmp_path / "nonexistent"))
    assert isinstance(result, ToolError)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_parser.py -v`
Expected: FAIL（build_metadata 未定义）

- [ ] **Step 3: 实现 build_metadata**（追加到 `tools/code_parser.py`）

```python
def build_metadata(workspace_root: str, timeout: float = 60.0) -> CodeMetadata | ToolError:
    """遍历工作区所有 .py 构建元数据；语法错误跳过并记录；超时返回部分结果。

    排除 IGNORED_DIRS（.git / __pycache__ / .venv 等，与 list_files 一致）。
    """
    root = Path(workspace_root)
    if not root.exists():
        return ToolError(f"工作区不存在: {workspace_root}")
    metadata = CodeMetadata()
    start = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if time.monotonic() - start > timeout:
                metadata.warnings.append(f"解析超时（>{timeout}s），结果不完整")
                return metadata
            path = Path(dirpath) / name
            try:
                module_name = _module_name_from_path(path, root)
                metadata.modules.append(parse_python_file(str(path), module_name))
            except SyntaxError as e:
                metadata.warnings.append(
                    f"语法错误跳过: {path.relative_to(root)}（{e.msg} 第 {e.lineno} 行）")
    metadata.modules.sort(key=lambda m: m.name)
    return metadata
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_parser.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/code_parser.py tests/test_code_parser.py
git commit -m "feat(tools): build_metadata 全工作区 AST 构建（排除忽略目录、语法错误跳过、限时）"
```

---

### Task 3: CodeGraph 构建与五类查询

**Files:**
- Create: `tools/code_graph.py`
- Test: `tests/test_code_graph.py`

**Interfaces:**
- Produces: `CodeGraph(metadata)`（`query_calls` / `query_inheritance` / `query_imports` / `query_module_of` / `search_symbols`）、`build_code_graph(workspace_root) -> CodeGraph | ToolError`、`query_code_graph(graph, query, arg) -> list[str] | ToolError`
- 语义：同名符号取首个定义（docstring 标注静态分析限制）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_code_graph.py
"""代码图测试：构建与五类查询"""
from tools.code_graph import CodeGraph, build_code_graph, query_code_graph
from tools.code_parser import ModuleInfo, ClassInfo, FunctionInfo, CodeMetadata
from tools.file_tools import ToolError


def _metadata() -> CodeMetadata:
    """样例元数据：app.mod 定义 create_entity 与 main（main 调用 create_entity）。"""
    return CodeMetadata(modules=[
        ModuleInfo(
            name="app.mod", path="app/mod.py", imports=["os", "store.db"],
            classes=[
                ClassInfo(name="Base", module="app.mod", bases=[], methods=["run"]),
                ClassInfo(name="Worker", module="app.mod", bases=["Base"], methods=["work"]),
            ],
            functions=[
                FunctionInfo(name="create_entity", module="app.mod", calls=["Entity"]),
                FunctionInfo(name="main", module="app.mod", calls=["create_entity", "print"]),
                FunctionInfo(name="work", module="app.mod", class_name="Worker",
                             calls=["helper"]),
            ],
        ),
        ModuleInfo(name="app.other", path="app/other.py", imports=["app.mod"],
                   classes=[], functions=[
                       FunctionInfo(name="create_entity", module="app.other",
                                    calls=["Entity"]),
                   ]),
    ])


def test_query_calls():
    graph = CodeGraph(_metadata())
    assert graph.query_calls("create_entity") == ["app.mod.main", "app.other.create_entity"]


def test_query_calls_unknown():
    graph = CodeGraph(_metadata())
    assert graph.query_calls("nobody") == []


def test_query_inheritance():
    graph = CodeGraph(_metadata())
    assert graph.query_inheritance("Worker") == ["Worker", "Base"]


def test_query_inheritance_unknown():
    graph = CodeGraph(_metadata())
    assert graph.query_inheritance("Ghost") == ["Ghost"]


def test_query_imports():
    graph = CodeGraph(_metadata())
    assert graph.query_imports("app.mod") == ["os", "store.db"]
    assert graph.query_imports("nope") == []


def test_query_module_of():
    graph = CodeGraph(_metadata())
    assert graph.query_module_of("create_entity") == ["app.mod", "app.other"]
    assert graph.query_module_of("Worker") == ["app.mod"]
    assert graph.query_module_of("ghost") == []


def test_search_symbols():
    graph = CodeGraph(_metadata())
    assert graph.search_symbols("entity") == ["create_entity"]
    assert graph.search_symbols("worker") == ["Worker"]


def test_query_code_graph_dispatch():
    graph = CodeGraph(_metadata())
    assert query_code_graph(graph, "calls", "create_entity") == \
        ["app.mod.main", "app.other.create_entity"]
    result = query_code_graph(graph, "bogus", "x")
    assert isinstance(result, ToolError)


def test_build_code_graph_from_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(
        "def create_entity(name):\n    return name\n\ndef main():\n    return create_entity('x')\n",
        encoding="utf-8")
    graph = build_code_graph(str(ws))
    assert graph.query_calls("create_entity") == ["app.main"]
    assert graph.query_module_of("create_entity") == ["app"]


def test_build_code_graph_missing_workspace(tmp_path):
    result = build_code_graph(str(tmp_path / "nope"))
    assert isinstance(result, ToolError)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_graph.py -v`
Expected: FAIL（ModuleNotFoundError: tools.code_graph）

- [ ] **Step 3: 最小实现**

```python
# tools/code_graph.py
"""代码知识图：由 CodeMetadata 构建内存图，提供结构化查询。

查询为静态分析结果，与 search_code 互补（知道符号名时直达代码位置）。
同名符号取首个定义（v1 限制）。
"""
from dataclasses import dataclass

from tools.code_parser import CodeMetadata, build_metadata
from tools.file_tools import ToolError


@dataclass
class CodeGraph:
    """内存图：节点 = 符号（module/class/function），边 = import/inherits/calls/defines。"""

    def __init__(self, metadata: CodeMetadata):
        self._modules: dict[str, dict] = {}
        self._classes: dict[str, dict] = {}
        self._functions: dict[str, list[dict]] = {}
        for m in metadata.modules:
            self._modules[m.name] = {"imports": list(m.imports), "path": m.path}
            for c in m.classes:
                self._classes.setdefault(c.name, {
                    "module": m.name, "bases": list(c.bases), "methods": list(c.methods)})
            for f in m.functions:
                self._functions.setdefault(f.name, []).append(
                    {"module": m.name, "class": f.class_name, "calls": list(f.calls)})

    def query_calls(self, function: str) -> list[str]:
        """谁调用了 function（按 module[.class].function 展示）。"""
        out = []
        for name, defs in self._functions.items():
            for d in defs:
                if function in d["calls"]:
                    prefix = f"{d['module']}.{d['class']}." if d["class"] else f"{d['module']}."
                    out.append(f"{prefix}{name}")
        return sorted(out)

    def query_inheritance(self, class_name: str) -> list[str]:
        """class_name 的继承链（含自身，沿首个基类向上递归）。"""
        chain: list[str] = []
        seen: set[str] = set()
        cur = class_name
        while cur and cur not in seen:
            seen.add(cur)
            info = self._classes.get(cur)
            if info is None:
                break
            chain.append(cur)
            cur = info["bases"][0] if info["bases"] else None
        return chain

    def query_imports(self, module: str) -> list[str]:
        """module 的直接导入列表。"""
        info = self._modules.get(module)
        if info is None:
            return []
        return sorted(info["imports"])

    def query_module_of(self, symbol: str) -> list[str]:
        """定义 symbol 的模块（类或函数）。"""
        out = []
        if symbol in self._classes:
            out.append(self._classes[symbol]["module"])
        for d in self._functions.get(symbol, []):
            if d["module"] not in out:
                out.append(d["module"])
        return sorted(out)

    def search_symbols(self, keyword: str) -> list[str]:
        """符号名模糊搜索（不区分大小写）。"""
        k = keyword.lower()
        out = {n for n in self._classes if k in n.lower()}
        out |= {n for n in self._functions if k in n.lower()}
        return sorted(out)


def build_code_graph(workspace_root: str) -> CodeGraph | ToolError:
    """从工作区构建 CodeGraph；工作区缺失返回 ToolError。"""
    metadata = build_metadata(workspace_root)
    if isinstance(metadata, ToolError):
        return metadata
    return CodeGraph(metadata)


def query_code_graph(graph: CodeGraph, query: str, arg: str) -> list[str] | ToolError:
    """工具分发：query ∈ calls / inheritance / imports / module_of / symbols。"""
    if query == "calls":
        return graph.query_calls(arg)
    if query == "inheritance":
        return graph.query_inheritance(arg)
    if query == "imports":
        return graph.query_imports(arg)
    if query == "module_of":
        return graph.query_module_of(arg)
    if query == "symbols":
        return graph.search_symbols(arg)
    return ToolError(f"未知查询类型: {query}（可用 calls / inheritance / imports / module_of / symbols）")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_code_graph.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/code_graph.py tests/test_code_graph.py
git commit -m "feat(tools): 新增 code_graph，内存图提供五类结构化代码查询"
```

---

### Task 4: query_code_graph 工具接入 LangGraph

**Files:**
- Modify: `agent/graph.py`
- Test: `tests/test_graph.py`（追加）

**Interfaces:**
- Consumes: `CodeGraph` / `query_code_graph`（Task 3）
- Produces: `_graph_dispatch(name, args, workspace_root, code_graph=None, knowledge_client=None)`；`build_graph(..., code_graph=None, knowledge_client=None)`；`run_agent_graph(..., code_graph=None, knowledge_client=None)`（knowledge_client 本任务先透传，Task 6 使用）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_graph.py`）

```python
def test_graph_decide_can_call_query_code_graph(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(
        "def create_entity(name):\n    return name\n\ndef main():\n    return create_entity('x')\n",
        encoding="utf-8")
    from tools.code_graph import build_code_graph
    code_graph = build_code_graph(str(ws))
    script = [
        _plan_msg(["查询代码结构"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="query_code_graph",
                     arguments={"query": "module_of", "arg": "create_entity"})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("查询", MockLLMClient(script), workspace_root=str(ws),
                             code_graph=code_graph)
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "query_code_graph"
    assert result.steps[0].result == ["app"]


def test_graph_query_code_graph_calls(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(
        "def create_entity(name):\n    return name\n\ndef main():\n    return create_entity('x')\n",
        encoding="utf-8")
    from tools.code_graph import build_code_graph
    code_graph = build_code_graph(str(ws))
    script = [
        _plan_msg(["查询"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="query_code_graph",
                     arguments={"query": "calls", "arg": "create_entity"})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("查询", MockLLMClient(script), workspace_root=str(ws),
                             code_graph=code_graph)
    assert result.steps[0].result == ["app.main"]


def test_graph_query_code_graph_without_index(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        _plan_msg(["查询"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="query_code_graph",
                     arguments={"query": "calls", "arg": "x"})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("查询", MockLLMClient(script), workspace_root=str(ws))
    from tools.file_tools import ToolError
    assert isinstance(result.steps[0].result, ToolError)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`
Expected: FAIL（TypeError: run_agent_graph() got an unexpected keyword argument 'code_graph'）

- [ ] **Step 3: 实现**

修改 `agent/graph.py`：

```python
from tools.code_graph import CodeGraph, query_code_graph
from tools.knowledge_client import KnowledgeClient
```

`_graph_tools()` 末尾（git_log ToolSpec 之后）追加两个 ToolSpec（本任务先加 query_code_graph，search_knowledge 在 Task 6 加，此处可一并加入避免二次编辑）：

```python
        ToolSpec(
            name="query_code_graph",
            description="查询代码结构图（静态分析结果）：query=calls(谁调用了某函数) / inheritance(继承链) / imports(模块直接导入) / module_of(符号所在模块) / symbols(符号模糊搜索)，arg 为查询目标",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询类型: calls / inheritance / imports / module_of / symbols"},
                    "arg": {"type": "string", "description": "查询目标（函数名/类名/模块名/关键词）"},
                },
                "required": ["query", "arg"],
            },
        ),
        ToolSpec(
            name="search_knowledge",
            description="查询明日方舟领域知识库（Arknights Wiki 知识图谱）：kind=entity(实体)/event(事件)/relationship(关系)/timeline(时间线)/story(剧情原文)，缺省 entity",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询内容（角色名/概念/关键词）"},
                    "kind": {"type": "string", "description": "检索类型，缺省 entity"},
                },
                "required": ["query"],
            },
        ),
```

`_graph_dispatch` 签名与分支（追加在 git_log 分支之后、`return _dispatch(...)` 之前）：

```python
def _graph_dispatch(name: str, args: dict, workspace_root: str | None,
                    code_graph: CodeGraph | None = None,
                    knowledge_client: KnowledgeClient | None = None) -> object:
    """图内工具分发：W2 shell/git 工具 + W4 双知识工具 + 回退 W1 四文件工具。"""
    ...
    if name == "query_code_graph":
        query = args.get("query")
        arg = args.get("arg")
        if not query or not arg:
            return ToolError("缺少参数: query/arg")
        if code_graph is None:
            return ToolError("代码索引未构建（任务启动时未开启 KA_CODE_INDEX 或构建失败）")
        return query_code_graph(code_graph, query, arg)
    if name == "search_knowledge":
        q = args.get("query")
        if not q:
            return ToolError("缺少参数: query")
        if knowledge_client is None:
            return ToolError("域知识未启用：请设置 ARKNIGHTS_USE_MCP=1 与 ARKNIGHTS_WIKI_DIR")
        return knowledge_client.search(q, args.get("kind"))
    return _dispatch(name, args, workspace_root)
```

`build_graph` 签名与 execute_node（闭包捕获）：

```python
def build_graph(llm: LLMClient, max_iterations: int = 20,
                max_verify_rounds: int = 3,
                workspace_root: str | None = None,
                code_graph: CodeGraph | None = None,
                knowledge_client: KnowledgeClient | None = None) -> object:
    """构建 LangGraph 图（闭包捕获 llm、上限参数与 W4 注入的双知识源）。"""
    ...
    def execute_node(state: AgentState) -> dict:
        ...
        for tc in state["pending_tool_calls"] or []:
            result = _graph_dispatch(tc.name, tc.arguments, workspace_root,
                                     code_graph, knowledge_client)
            ...
```

`run_agent_graph` 签名与透传：

```python
def run_agent_graph(task: str, llm: LLMClient, max_iterations: int = 20,
                    max_verify_rounds: int = 3,
                    workspace_root: str | None = None,
                    code_graph: CodeGraph | None = None,
                    knowledge_client: KnowledgeClient | None = None) -> AgentGraphResult:
    """执行任务：LangGraph 图驱动（命令执行在沙箱上下文内，一个任务一个沙箱）。"""
    graph = build_graph(llm, max_iterations, max_verify_rounds, workspace_root,
                        code_graph, knowledge_client)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`
Expected: PASS（原有 + 新增 3 项全绿）

- [ ] **Step 5: 提交**

```bash
git add agent/graph.py tests/test_graph.py
git commit -m "feat(agent): query_code_graph/search_knowledge 工具接入 LangGraph（code_graph/knowledge_client 注入）"
```

---

### Task 5: KnowledgeClient 协议与 Arknights MCP 适配器

**Files:**
- Create: `tools/knowledge_client.py`
- Test: `tests/test_knowledge_client.py`

**Interfaces:**
- Produces: `KnowledgeClient`（Protocol，`search(query, kind=None) -> str`）、`MockKnowledgeClient(script)`、`ArknightsMCPClient(wiki_dir, python=None, timeout=120)`、`get_knowledge_client() -> KnowledgeClient | ToolError`
- 语义：kind → MCP 工具映射（entity→search_entities / event→search_events / relationship→query_relationship / timeline→query_timeline / story→search_story）；每次查询一次兄弟项目 venv 子进程（依赖与凭据隔离）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_knowledge_client.py
"""知识客户端测试：Mock 行为 / kind 映射 / 子进程调用 / 工厂"""
import json
import types

import pytest

from tools.file_tools import ToolError
from tools.knowledge_client import (
    ArknightsMCPClient,
    MockKnowledgeClient,
    _KIND_TOOL,
    get_knowledge_client,
)


def test_mock_knowledge_client_records_calls():
    mock = MockKnowledgeClient({("entity", "阿米娅"): "实体: 阿米娅"})
    assert mock.search("阿米娅", "entity") == "实体: 阿米娅"
    assert mock.search("罗德岛", "story") == "（无预设响应: story 罗德岛）"
    assert mock.calls == [("entity", "阿米娅"), ("story", "罗德岛")]


def test_kind_mapping():
    assert _KIND_TOOL["entity"][0] == "search_entities"
    assert _KIND_TOOL["event"][0] == "search_events"
    assert _KIND_TOOL["relationship"][0] == "query_relationship"
    assert _KIND_TOOL["timeline"][0] == "query_timeline"
    assert _KIND_TOOL["story"][0] == "search_story"


def test_arknights_client_invoke_ok(monkeypatch):
    client = ArknightsMCPClient("wiki", python="py", timeout=10)

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "py"
        assert cmd[2] == "-c"
        assert json.loads(cmd[4]) == {"query": "阿米娅", "limit": 5}
        return types.SimpleNamespace(returncode=0, stdout="实体结果", stderr="")

    monkeypatch.setattr("tools.knowledge_client.subprocess.run", fake_run)
    assert client.search("阿米娅", "entity") == "实体结果"


def test_arknights_client_invoke_failure(monkeypatch):
    client = ArknightsMCPClient("wiki", python="py", timeout=10)

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("tools.knowledge_client.subprocess.run", fake_run)
    assert "查询失败" in client.search("x")


def test_get_knowledge_client_disabled(monkeypatch):
    monkeypatch.delenv("ARKNIGHTS_USE_MCP", raising=False)
    monkeypatch.delenv("ARKNIGHTS_WIKI_DIR", raising=False)
    assert isinstance(get_knowledge_client(), ToolError)


def test_get_knowledge_client_missing_dir(monkeypatch):
    monkeypatch.setenv("ARKNIGHTS_USE_MCP", "1")
    monkeypatch.setenv("ARKNIGHTS_WIKI_DIR", "C:\\nonexistent")
    assert isinstance(get_knowledge_client(), ToolError)


def test_get_knowledge_client_ok(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "arknights_wiki").mkdir(parents=True)
    monkeypatch.setenv("ARKNIGHTS_USE_MCP", "1")
    monkeypatch.setenv("ARKNIGHTS_WIKI_DIR", str(wiki))
    client = get_knowledge_client()
    assert isinstance(client, ArknightsMCPClient)
    assert client.wiki_dir == str(wiki)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_knowledge_client.py -v`
Expected: FAIL（ModuleNotFoundError: tools.knowledge_client）

- [ ] **Step 3: 最小实现**

```python
# tools/knowledge_client.py
"""域知识客户端：Arknights 知识库 MCP 适配（兄弟项目《Arknights LLM Wiki》）。

以兄弟项目 venv 的 python 一次性子进程调用其 ArknightsMcpClient（stdio MCP），
本仓库零新增依赖，依赖与凭据隔离在兄弟项目侧。
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from tools.file_tools import ToolError

# 兄弟项目内一次性调用脚本：经其 ArknightsMcpClient 同步调用 MCP 工具，stdout 输出文本结果
_CALL_SCRIPT = (
    "import json, sys;"
    "from arknights_wiki.mcp_server.client import ArknightsMcpClient;"
    "c = ArknightsMcpClient();"
    "print(c.call_tool(sys.argv[1], json.loads(sys.argv[2])))"
)

# kind → (MCP 工具名, 参数映射)
_KIND_TOOL = {
    "entity": ("search_entities", lambda q: {"query": q, "limit": 5}),
    "event": ("search_events", lambda q: {"entity": q, "limit": 5}),
    "relationship": ("query_relationship", lambda q: {"entity_name": q}),
    "timeline": ("query_timeline", lambda q: {"query": q, "limit": 10}),
    "story": ("search_story", lambda q: {"query": q, "limit": 5}),
}


class KnowledgeClient(Protocol):
    """域知识检索协议：Agent 工具只依赖此接口。"""

    def search(self, query: str, kind: str | None = None) -> str: ...


class MockKnowledgeClient:
    """测试用：按 (kind, query) 预设响应，并记录每次调用。"""

    def __init__(self, script: dict[tuple[str | None, str], str] | None = None):
        self.script = script or {}
        self.calls: list[tuple[str | None, str]] = []

    def search(self, query: str, kind: str | None = None) -> str:
        self.calls.append((kind, query))
        return self.script.get((kind, query), f"（无预设响应: {kind} {query}）")


def _resolve_sibling_python(wiki_dir: str) -> str:
    """定位兄弟项目 venv python：Windows .venv/Scripts/python.exe，POSIX .venv/bin/python。"""
    venv = Path(wiki_dir) / ".venv"
    for cand in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


class ArknightsMCPClient:
    """兄弟项目 MCP 适配器：每次查询一次子进程（依赖与凭据隔离）。"""

    def __init__(self, wiki_dir: str, python: str | None = None, timeout: int = 120):
        self.wiki_dir = wiki_dir
        self.python = python or _resolve_sibling_python(wiki_dir)
        self.timeout = timeout

    def search(self, query: str, kind: str | None = None) -> str:
        tool, arg_fn = _KIND_TOOL.get(kind or "entity", _KIND_TOOL["entity"])
        return self._invoke(tool, arg_fn(query))

    def _invoke(self, tool: str, args: dict) -> str:
        cmd = [self.python, "-c", _CALL_SCRIPT, tool, json.dumps(args, ensure_ascii=False)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=self.wiki_dir, timeout=self.timeout,
            )
        except FileNotFoundError:
            return f"域知识不可用: 兄弟项目 python 不存在（{self.python}）"
        except subprocess.TimeoutExpired:
            return f"域知识查询超时（{self.timeout}s）"
        if proc.returncode != 0:
            return f"域知识查询失败: {proc.stderr.strip() or proc.stdout.strip()}"
        out = proc.stdout.strip()
        if not out:
            return "域知识查询无结果"
        return out


def get_knowledge_client() -> KnowledgeClient | ToolError:
    """工厂：ARKNIGHTS_USE_MCP=1 且 ARKNIGHTS_WIKI_DIR 可定位时返回适配器，否则 ToolError。"""
    if os.environ.get("ARKNIGHTS_USE_MCP") != "1":
        return ToolError("域知识未启用：请设置 ARKNIGHTS_USE_MCP=1")
    wiki_dir = os.environ.get("ARKNIGHTS_WIKI_DIR", "")
    if not wiki_dir or not (Path(wiki_dir) / "arknights_wiki").exists():
        return ToolError("兄弟项目不可用：请设置 ARKNIGHTS_WIKI_DIR（指向 Arknights LLM Wiki 根目录）")
    return ArknightsMCPClient(wiki_dir)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_knowledge_client.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add tools/knowledge_client.py tests/test_knowledge_client.py
git commit -m "feat(tools): 新增 knowledge_client，兄弟项目 MCP 子进程适配 + Mock + 工厂"
```

---

### Task 6: search_knowledge 工具 dispatch 回归测试

**Files:**
- Modify: `tests/test_graph.py`（追加；`agent/graph.py` 已在 Task 4 完成工具注册）
- Test: `tests/test_graph.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_graph.py`）

```python
def test_graph_decide_can_call_search_knowledge(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    from tools.knowledge_client import MockKnowledgeClient
    knowledge = MockKnowledgeClient({("entity", "阿米娅"): "实体: 阿米娅 (罗德岛)"})
    script = [
        _plan_msg(["查询领域知识"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="k1", name="search_knowledge",
                     arguments={"query": "阿米娅", "kind": "entity"})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("查询", MockLLMClient(script), workspace_root=str(ws),
                             knowledge_client=knowledge)
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "search_knowledge"
    assert result.steps[0].result == "实体: 阿米娅 (罗德岛)"
    assert knowledge.calls == [("entity", "阿米娅")]


def test_graph_search_knowledge_without_client(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        _plan_msg(["查询"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="k1", name="search_knowledge", arguments={"query": "阿米娅"})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("查询", MockLLMClient(script), workspace_root=str(ws))
    from tools.file_tools import ToolError
    assert isinstance(result.steps[0].result, ToolError)
    assert "域知识未启用" in result.steps[0].result.message
```

- [ ] **Step 2: 运行测试确认通过**（Task 4 已实现 dispatch，应直接通过）

Run: `.venv\Scripts\python.exe -m pytest tests/test_graph.py -v`
Expected: PASS（2 新增 + 原有全绿）

- [ ] **Step 3: 提交**

```bash
git add tests/test_graph.py
git commit -m "test(agent): search_knowledge 工具 dispatch 回归测试（注入 Mock 与未启用路径）"
```

---

### Task 7: main.py --graph 自动构建双知识源

**Files:**
- Modify: `main.py`
- Modify: `pyproject.toml`（注册 mcp marker）
- Modify: `.env.example`（W4 配置项）
- Test: `tests/test_main.py`

- [ ] **Step 1: 写失败测试**（更新 `tests/test_main.py` 的 `test_main_graph_flag_uses_graph`）

```python
def test_main_graph_flag_uses_graph(monkeypatch, capsys):
    from agent.graph import AgentGraphResult
    from tools.file_tools import ToolError
    fake = AgentGraphResult(plan=["步骤1"], steps=[], final_answer="搞定", iteration_count=1,
                            verify_rounds=0, stopped_by_limit=False, test_results=[])
    monkeypatch.setattr("main.run_agent_graph", lambda *a, **k: fake)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走 loop")))
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    monkeypatch.setattr("main.build_code_graph", lambda *a, **k: None)
    monkeypatch.setattr("main.get_knowledge_client", lambda: ToolError("未启用"))
    monkeypatch.delenv("KA_CODE_INDEX", raising=False)
    rc = main.main(["测试任务", "--graph"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "搞定" in out
    assert "计划" in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py::test_main_graph_flag_uses_graph -v`
Expected: FAIL（AttributeError: module 'main' has no attribute 'build_code_graph'）

- [ ] **Step 3: 实现**

`main.py` 顶部追加导入：

```python
from tools.code_graph import build_code_graph
from tools.file_tools import ToolError
from tools.knowledge_client import get_knowledge_client
```

`main()` 的 graph 分支改为：

```python
    if args.graph:
        code_graph = None
        if os.environ.get("KA_CODE_INDEX", "1") != "0":
            built = build_code_graph(args.workspace)
            if not isinstance(built, ToolError):
                code_graph = built
        knowledge = get_knowledge_client()
        if isinstance(knowledge, ToolError):
            knowledge = None
        result = run_agent_graph(args.task, llm, max_iterations=args.max_iterations,
                                 workspace_root=args.workspace, code_graph=code_graph,
                                 knowledge_client=knowledge)
```

`pyproject.toml` markers 追加：

```toml
markers = [
    "docker: 需要 Docker 守护进程的集成测试（无 Docker 时跳过）",
    "mcp: 需要兄弟项目 Arknights LLM Wiki（ARKNIGHTS_WIKI_DIR）的集成测试（无配置时跳过）",
]
```

`.env.example` 追加：

```text
# W4 Repository Intelligence（可选）
# ARKNIGHTS_USE_MCP=1                              # 启用域知识检索（Arknights Wiki）
# ARKNIGHTS_WIKI_DIR=<sibling-project-dir>   # 兄弟项目根目录
# KA_CODE_INDEX=1                                 # 任务启动时自动构建代码索引（0 关闭）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py tests/test_graph.py -v`
Expected: PASS（全绿）

- [ ] **Step 5: 提交**

```bash
git add main.py pyproject.toml .env.example tests/test_main.py
git commit -m "feat(main): --graph 自动构建代码索引与域知识客户端（KA_CODE_INDEX 开关）"
```

---

### Task 8: MCP 集成测试（可选运行）

**Files:**
- Create: `tests/test_mcp_integration.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_mcp_integration.py
"""MCP 集成测试：需要兄弟项目《Arknights LLM Wiki》（ARKNIGHTS_WIKI_DIR 指向其根目录）。

跳过条件：ARKNIGHTS_USE_MCP != 1 或 ARKNIGHTS_WIKI_DIR 不可定位。
"""
import os

import pytest

from tools.knowledge_client import ArknightsMCPClient

_WIKI_DIR = os.environ.get("ARKNIGHTS_WIKI_DIR", "")

pytestmark = pytest.mark.mcp


def _skip_reason() -> str | None:
    if os.environ.get("ARKNIGHTS_USE_MCP") != "1":
        return "ARKNIGHTS_USE_MCP != 1"
    if not _WIKI_DIR or not os.path.isdir(_WIKI_DIR):
        return "ARKNIGHTS_WIKI_DIR 未配置"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_entities_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("阿米娅", "entity")
    assert out and "阿米娅" in out


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_query_relationship_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("阿米娅", "relationship")
    assert out and ("阿米娅" in out or "未在索引中找到" in out)


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_story_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("博士", "story")
    assert out
```

- [ ] **Step 2: 条件运行**

Run: `$env:ARKNIGHTS_USE_MCP = "1"; $env:ARKNIGHTS_WIKI_DIR = "<sibling-project-dir>"; .venv\Scripts\python.exe -m pytest tests/test_mcp_integration.py -v`
Expected: PASS（3 passed；未配置时 3 skipped）

- [ ] **Step 3: 提交**

```bash
git add tests/test_mcp_integration.py
git commit -m "test(tools): MCP 集成测试（兄弟项目可用时验证 search_entities/relationship/story）"
```

---

### Task 9: 全量回归 + 真实演示 + 窗口收尾

- [ ] **Step 1: 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/ -m "not docker" -q`
Expected: 全部通过（79 原有 + W4 新增 26 项）

- [ ] **Step 2: 真实演示（领域 Issue 定位）**

准备演示 workspace（兄弟项目代码副本，排除 .git/.venv 等大目录）：

```powershell
robocopy "<sibling-project-dir>" "workspace\w4-demo" /E /XD .git .venv .cache data output /NFL /NDL /NJH /NJS
```

运行：

```powershell
$env:ARKNIGHTS_USE_MCP = "1"
$env:ARKNIGHTS_WIKI_DIR = "<sibling-project-dir>"
.venv\Scripts\python.exe main.py "定位创建角色 Entity 的函数，并确认同一角色在不同章节是否会被重复创建。先使用 query_code_graph 查询 Entity/实体 相关符号与调用关系，再用 search_knowledge 查询具体角色（如阿米娅）的章节信息，最后阅读相关代码给出结论" --workspace workspace/w4-demo --graph
```

Expected：输出中可见 `query_code_graph`（calls/module_of/symbols）与 `search_knowledge`（entity/story/relationship）工具调用，最终回答定位到 Entity 创建/合并逻辑及其章节来源字段。

- [ ] **Step 3: 双轴审查（requesting-code-review）**：Standards（工程约束/注释/无 emoji）+ Spec（双知识检索闭环）；发现问题修复并补测试

- [ ] **Step 4: 更新 `docs/roadmap.md`**：W4 状态改为 `[x] 已完成`，记录验收达成；更新 `docs/devlog.md`：W4 完成记录（实现、测试数、演示结论、决策修订、遗留问题）

- [ ] **Step 5: 合并回 main 并删除 worktree**

```bash
git checkout main
git merge feature/w4-repo-intelligence
git worktree remove ../.worktrees/w4-repo-intelligence
```
