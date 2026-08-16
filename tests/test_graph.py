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
