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
