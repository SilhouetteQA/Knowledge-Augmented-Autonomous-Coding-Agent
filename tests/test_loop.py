# tests/test_loop.py
"""ReAct 循环测试：工具调用序列、错误回注、多工具一轮、迭代上限"""
from agent.llm import LLMMessage, MockLLMClient, ToolCall
from agent.loop import _build_tools, _dispatch, run_agent
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


# --- W7 Task 2: 工具调用 span 埋点（tool.execute） ---


def _load_probe(name, rel_path, fake_traced, monkeypatch):
    """以独立模块名重放源码顶层，捕获模块级 traced 装饰注册（避免 reload 副作用）。"""
    import importlib.util
    import pathlib
    import sys
    import tools.tracing as tracing
    monkeypatch.setattr(tracing, "traced", fake_traced)
    src = pathlib.Path(__file__).resolve().parent.parent / rel_path
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解需要模块在 sys.modules 中可见，执行期间注册、完毕移除
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def test_dispatch_traced_registered(monkeypatch):
    """_dispatch 模块级注册 traced("tool.execute") span，且 metadata_fn 已接线。"""
    registry = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        registry.append((name, as_type, metadata_fn))
        return lambda f: f

    _load_probe("agent.loop_probe", "agent/loop.py", fake_traced, monkeypatch)
    assert any(n == "tool.execute" and t == "span" and m is not None
               for n, t, m in registry)


def test_tool_metadata_records_tool_name():
    """_tool_metadata 以 args[0] 记录工具名；空 args 回退空串。"""
    from agent.loop import _tool_metadata
    assert _tool_metadata(("read_file", {"path": "a.py"}, None), {}, "ok") == {
        "tool": "read_file"}
    assert _tool_metadata((), {}, None) == {"tool": ""}


def test_dispatch_disabled_passthrough(tmp_path):
    """关闭态直通：_dispatch 行为不变（正常结果与 ToolError 均原样返回）。"""
    from agent.loop import _dispatch
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    ok = _dispatch("read_file", {"path": "a.py"}, str(ws))
    assert isinstance(ok, FileContent)
    assert ok.path == "a.py"
    err = _dispatch("no_such_tool", {}, str(ws))
    assert isinstance(err, ToolError)


def test_build_tools_includes_edit_file():
    """edit_file 暴露给 LLM（write_file 全量覆盖的互补工具）。"""
    names = [t.name for t in _build_tools()]
    assert "edit_file" in names


def test_dispatch_edit_file(tmp_path):
    """_dispatch 分发 edit_file：精确替换一次。"""
    (tmp_path / "m.py").write_text("a = 1\n", encoding="utf-8")
    result = _dispatch("edit_file", {"path": "m.py", "old_text": "a = 1", "new_text": "a = 2"},
                       str(tmp_path))
    assert not isinstance(result, ToolError)
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "a = 2\n"


def test_loop_tool_exception_becomes_toolerror(monkeypatch, tmp_path):
    """IM-11：工具执行异常转 ToolError 观察值回注（与 graph 防护对齐），不再击穿整任务。"""

    def boom(*args, **kwargs):
        raise OSError("bad symlink")

    monkeypatch.setattr("agent.loop.list_files", boom)
    script = [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id="1", name="list_files", arguments={})]),
        LLMMessage(role="assistant", content="已完成（异常已观察）"),
    ]
    result = run_agent("任务", MockLLMClient(script), workspace_root=str(tmp_path))
    assert result.final_answer == "已完成（异常已观察）"
    assert isinstance(result.steps[0].result, ToolError)
    assert "OSError" in result.steps[0].result.message


def test_dispatch_unparsed_arguments_toolerror(tmp_path):
    """IM-12：畸形 arguments 哨兵 → 结构化错误回注（模型可重试），不静默缺参。"""
    r = _dispatch("read_file", {"__unparsed_arguments__": "{bad json"}, str(tmp_path))
    assert isinstance(r, ToolError)
    assert "JSON" in r.message and "bad json" in r.message
