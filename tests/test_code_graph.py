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
    assert graph.query_calls("create_entity") == ["app.mod.main"]


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
    assert query_code_graph(graph, "calls", "create_entity") == ["app.mod.main"]
    result = query_code_graph(graph, "bogus", "x")
    assert isinstance(result, ToolError)


def test_query_code_graph_empty_hint():
    graph = CodeGraph(_metadata())
    result = query_code_graph(graph, "calls", "nobody")
    assert len(result) == 1 and "未找到" in result[0] and "search_code" in result[0]


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