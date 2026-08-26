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
    ws.mkdir()
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
    assert result.verify_rounds == 1
    assert len(result.test_results) == 1


def test_graph_success_path_verify(tmp_path):
    """成功路径（Task 5 起 decide 无工具调用后强制 verify）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
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
    ws.mkdir()
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
    ws.mkdir()
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
    assert result.iteration_count == 21


def test_graph_decide_can_call_run_tests(tmp_path):
    """decide 可调用 run_tests 工具（回归 spec 工具集缺口）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    script = [
        _plan_msg(["运行测试"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="t1", name="run_tests", arguments={})]),
        LLMMessage(role="assistant", content="测试通过"),
    ]
    result = run_agent_graph("运行测试", MockLLMClient(script), workspace_root=str(ws))
    assert result.stopped_by_limit is False
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "run_tests"
    tr = result.steps[0].result
    assert tr.passed == 1


def test_graph_decide_can_call_run_command(tmp_path):
    """decide 可调用 run_command 工具。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        _plan_msg(["执行命令"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="run_command",
                     arguments={"command": "python -c \"print('hi')\""})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("执行命令", MockLLMClient(script), workspace_root=str(ws))
    assert len(result.steps) == 1
    cr = result.steps[0].result
    assert cr.exit_code == 0
    assert "hi" in cr.stdout


def test_graph_run_command_missing_arg_returns_toolerror(tmp_path):
    """畸形 run_command 调用（缺 command）返回 ToolError，不中止图。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        _plan_msg(["执行"]),
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="c1", name="run_command", arguments={})]),
        LLMMessage(role="assistant", content="完成"),
    ]
    result = run_agent_graph("执行", MockLLMClient(script), workspace_root=str(ws))
    from tools.file_tools import ToolError
    assert isinstance(result.steps[0].result, ToolError)
    assert result.final_answer == "完成"


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


def test_graph_nodes_are_traced(monkeypatch):
    """7 个图节点函数均被 traced 包装（name 前缀 graph.，decide/reflect 为 generation）。"""
    import agent.graph as graph_mod
    wrapped: list[tuple[str, str]] = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        def deco(func):
            wrapped.append((name, as_type))
            return func
        return deco

    monkeypatch.setattr(graph_mod, "traced", fake_traced)
    graph_mod.build_graph(MockLLMClient([LLMMessage(role="assistant", content="[]")]))
    names = [n for n, _ in wrapped]
    assert "graph.plan" in names
    assert "graph.decide" in names and "graph.execute" in names
    assert "graph.verify" in names and "graph.reflect" in names
    assert "graph.finalize" in names and "graph.finalize_limited" in names
    types = dict(wrapped)
    assert types["graph.decide"] == "generation"
    assert types["graph.reflect"] == "generation"
