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
