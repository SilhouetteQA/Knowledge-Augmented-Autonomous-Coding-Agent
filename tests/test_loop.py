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
    ws.mkdir()
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
