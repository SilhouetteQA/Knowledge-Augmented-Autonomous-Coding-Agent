# tests/test_graph.py
"""LangGraph 编排测试：成功路径 / 失败修复路径 / 双上限"""
import json

from agent.graph import _decide_system, run_agent_graph
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


def test_graph_iteration_limit_forced_verify(tmp_path):
    """迭代触顶路径（脚本耗尽仍未声称完成）强制补一次测试验证：verify_rounds >= 1 且 test_results 非空。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    script = [_plan_msg(["循环"])] + [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id=f"c{i}", name="list_files", arguments={})])
        for i in range(25)
    ]
    result = run_agent_graph("任务", MockLLMClient(script),
                             max_iterations=20, workspace_root=str(ws))
    # 语义不变量：触顶收尾与 final_answer 保持不变
    assert result.stopped_by_limit is True
    assert result.final_answer == "已达到上限，任务未完成"
    # 强制验证证据：真实执行仓库测试，结果记入结果集（与 verify 路径字段语义一致）
    assert result.verify_rounds >= 1
    assert len(result.test_results) >= 1
    tr = result.test_results[0]
    assert tr.passed >= 1 and tr.failed == 0


def test_graph_iteration_limit_forced_verify_reuses_verify_executor(monkeypatch, tmp_path):
    """触顶强制验证复用 verify 节点的测试执行函数（同源执行代码，而非新实现）。"""
    import agent.graph as graph_mod
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    script = [_plan_msg(["循环"])] + [
        LLMMessage(role="assistant", tool_calls=[
            ToolCall(id=f"c{i}", name="list_files", arguments={})])
        for i in range(25)
    ]
    calls: list[str | None] = []
    real = graph_mod._execute_verify_tests

    def spy(workspace_root=None):
        calls.append(workspace_root)
        return real(workspace_root)
    monkeypatch.setattr(graph_mod, "_execute_verify_tests", spy)
    run_agent_graph("任务", MockLLMClient(script),
                    max_iterations=20, workspace_root=str(ws))
    # 全程无 verify 节点触发（每轮都带工具调用），唯一调用来自触顶强制验证
    assert calls == [str(ws)]


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


# --- W7 Task 2: 图内工具调用 span 埋点（tool.execute） ---


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


def test_graph_dispatch_traced_registered(monkeypatch):
    """_graph_dispatch 模块级注册 traced("tool.execute") span，且 metadata_fn 已接线。"""
    registry = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        registry.append((name, as_type, metadata_fn))
        return lambda f: f

    _load_probe("agent.graph_probe", "agent/graph.py", fake_traced, monkeypatch)
    assert any(n == "tool.execute" and t == "span" and m is not None
               for n, t, m in registry)


def test_graph_tool_metadata_records_name_and_args():
    """_graph_tool_metadata 记录工具名与参数摘要。"""
    from agent.graph import _graph_tool_metadata
    meta = _graph_tool_metadata(
        ("run_command", {"command": "echo hi"}, None), {}, None)
    assert meta == {"tool": "run_command", "args": "{'command': 'echo hi'}"}
    assert _graph_tool_metadata((), {}, None) == {"tool": "", "args": ""}


def test_graph_tool_metadata_truncates_long_args():
    """_graph_tool_metadata 参数摘要截断 500 字符。"""
    from agent.graph import _graph_tool_metadata
    long = "x" * 600
    meta = _graph_tool_metadata(("run_command", {"command": long}, None), {}, None)
    assert meta["tool"] == "run_command"
    assert len(meta["args"]) == 500
    assert meta["args"].startswith("{'command': '")
    assert meta["args"].endswith("x")


def test_graph_dispatch_disabled_passthrough(tmp_path):
    """关闭态直通：_graph_dispatch 行为不变（回退 W1 文件工具路径）。"""
    from agent.graph import _graph_dispatch
    from tools.file_tools import FileContent, ToolError
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    ok = _graph_dispatch("read_file", {"path": "a.py"}, str(ws))
    assert isinstance(ok, FileContent)
    assert ok.path == "a.py"
    err = _graph_dispatch("no_such_tool", {}, str(ws))
    assert isinstance(err, ToolError)


# --- E7（P2-7）：decide 提示词收敛引导 ---


def test_decide_system_contains_convergence_guidance():
    """_decide_system 输出含收敛引导句（优先最小变更、小步验证迭代）。"""
    system = _decide_system(["步骤1"], verify_rounds=0, max_verify_rounds=5)
    assert "优先最小变更并尽快验证" in system
    assert "小步验证" in system
    assert "开放型任务" in system


def test_decide_system_contains_domain_operation_rules():
    """_decide_system 输出含域操作规则段：删除三条件 + 无关元数据红线 + 变更清单验证。"""
    system = _decide_system(["步骤1"], verify_rounds=0, max_verify_rounds=5)
    assert "域操作规则" in system
    assert "三条件" in system
    assert "来源锚点" in system
    assert "元数据" in system
    assert "变更清单" in system


def test_decide_system_contains_artifact_discipline_and_delivery_rules():
    """_decide_system 输出含产物纪律与交付收敛：禁止新增分析脚本/中间产物、交付必附结论。"""
    system = _decide_system(["步骤1"], verify_rounds=0, max_verify_rounds=5)
    assert "禁止新增分析" in system
    assert "中间产物" in system
    assert "交付收敛" in system


def test_decide_system_contains_stage_budget_discipline():
    """_decide_system 域操作规则含第 6 条阶段预算纪律：前 1/3 探索核验、后 2/3 执行交付（A6）。"""
    system = _decide_system(["步骤1"], verify_rounds=0, max_verify_rounds=5)
    assert "预算纪律" in system
    assert "前 1/3" in system
    assert "剩余 2/3" in system
    assert "执行" in system
    assert "交付" in system
    assert "重复探索" in system


def test_decide_system_declares_execution_environment():
    """decide 提示词声明执行环境（防 /testbed、/workspace 等 SWE-bench 惯性幻觉）。"""
    from agent.graph import _decide_system
    system = _decide_system(["步骤1"], 0, 3)
    assert "/testbed" in system
    assert "根目录" in system
    assert "python --version" in system
