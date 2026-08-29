# agent/graph.py
"""LangGraph 显式阶段编排：plan → decide ⇄ execute → (verify) → reflect → finalize。

Task 5 版本：plan → decide ⇄ execute → verify → reflect ⇄ decide → finalize；
decide 无工具调用进入强制验证（verify/reflect 闭环），verify_rounds 上限 3、迭代上限 20；
迭代触顶（finalize_iter_limited）收尾前强制执行一次测试验证，为 Reviewer/审批人补充测试证据。
"""
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph

from agent.llm import LLMClient, LLMMessage, ToolCall, ToolSpec
from agent.loop import AgentStep, _build_tools, _dispatch, _result_to_text
from tools.code_graph import CodeGraph, query_code_graph
from tools.docker_sandbox import sandbox_executor
from tools.file_tools import ToolError
from tools.knowledge_client import KnowledgeClient
from tools.shell_tools import (
    TestResult,
    git_diff,
    git_log,
    git_status,
    run_command,
    run_tests,
)
from tools.tracing import traced

# 计划节点提示词：只输出 JSON 数组
PLAN_PROMPT = (
    "你是编码助手。请把任务拆解为 3-6 个具体步骤。\n"
    "只输出 JSON 数组字符串（如 [\"步骤1\", \"步骤2\"]），不要其他内容。"
)


def _reflect_system(details: str) -> str:
    """reflect 节点提示词：携带测试失败详情。"""
    return (
        "测试失败。请分析失败原因，给出下一步修复方向。\n"
        f"失败详情：\n{details}"
    )


def _decide_system(plan: list[str], verify_rounds: int, max_verify_rounds: int) -> str:
    """decide 节点系统提示词（引用计划、验证轮次与可用查询工具）。"""
    plan_text = "\n".join(f"- {p}" for p in plan) or "- （无计划）"
    return (
        "你是编码助手。你可以调用工具查看、修改和执行工作区内的代码。\n"
        "执行环境：所有命令在仓库（工作区）根目录内执行，python / git / pytest 可直接调用；"
        "不存在 /testbed、/workspace 等约定目录，禁止 cd 到它们；"
        "首次环境探测用 run_command 跑 pwd 或 python --version，以真实输出为准。\n"
        "修改已有文件优先用 edit_file（局部精确替换），仅新建文件用 write_file（全量覆盖）——"
        "对大文件整文件重写有丢失内容风险。\n"
        f"你的计划：\n{plan_text}\n"
        f"可用查询工具：query_code_graph（调用关系 calls / 导入 imports / 定义位置 "
        "module_of / 符号模糊搜索 symbols）、search_knowledge（领域知识检索）。"
        "定位符号与理解代码时优先使用结构化查询，而非盲目全文搜索。\n"
        f"验证轮次：已进行 {verify_rounds}/{max_verify_rounds} 次。测试全部通过前不要声称完成；"
        "声称完成后系统会自动运行测试验证。\n"
        "规则：只操作工作区内文件；每次工具调用后先观察结果再行动；完成后用中文总结。\n"
        "收敛：优先最小变更并尽快验证，避免大范围探索；开放型任务（补测试/重构/域数据处理）"
        "以小步验证迭代推进。\n"
        "域操作规则：\n"
        "1. 知识数据（extractions/seed/index 等数据文件）的删除/修改属于高风险操作：删除条目必须三条件齐备——"
        "无来源锚点（source_records/line_range/原文出现）∧ 无事件参与 ∧ 无结构化引用；"
        "任一条件满足即不得删除，应保留并考虑补引用；\n"
        "2. 不得修改与任务无关的元数据/日志类文件（如 _meta.generated_at、cost/日志文件）；\n"
        "3. 修改类操作（如命名 bridge）必须产出变更清单（条目+依据），并运行仓库既有测试/构建脚本验证；\n"
        "4. 禁止新增分析/验证脚本与中间产物到工作树：scripts/、output/ 下不留任何中间文件；"
        "分析用一次性命令完成（run_command 内联 python -c，或直接读取既有脚本输出）；"
        "必要验证并入仓库既有测试套件；\n"
        "5. 交付收敛——迭代预算耗尽或任务完成时必须产出简短结论（做了什么/没做什么/为什么）；"
        "不存在满足条件的删除候选时必须显式说明并附分析摘要。\n"
        "6. 预算纪律——迭代预算内前 1/3 用于数据结构了解与候选核验，剩余 2/3 必须进入执行"
        "（变更/删除/构建/验证）并交付结论；禁止把执行阶段消耗在重复探索上。"
    )


def _graph_tools() -> list[ToolSpec]:
    """图内工具集：W1 四文件工具 + W2 shell/git 五工具 + W4 双知识源两工具，共 11 个暴露给 LLM。

    覆盖 W2 spec §3 要求的完整工具集，解决 decide 阶段仅暴露 4 个文件工具、
    无法主动运行测试/命令/git 的缺口。
    """
    return list(_build_tools()) + [
        ToolSpec(
            name="run_command",
            description="在 workspace 内执行 shell 命令（60 秒超时，输出截断）",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录（相对 workspace 根）"},
                    "timeout": {"type": "integer", "description": "超时秒数，缺省 60"},
                },
                "required": ["command"],
            },
        ),
        ToolSpec(
            name="run_tests",
            description="运行 pytest（可选 path 限定子路径），返回结构化测试结果",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "测试子路径（相对 workspace 根）"},
                },
            },
        ),
        ToolSpec(
            name="git_status",
            description="查看工作区 git 状态（只读）",
            parameters={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="git_diff",
            description="查看未提交变更（只读）",
            parameters={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="git_log",
            description="查看最近提交（只读）",
            parameters={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "返回提交条数，缺省 10"},
                },
            },
        ),
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
    ]


def _graph_tool_metadata(args, kwargs, result) -> dict:
    """图内工具 span metadata：记录工具名与参数摘要（截断 500 字符）。"""
    tool = args[0] if args else ""
    summary = str(args[1])[:500] if len(args) > 1 else ""
    return {"tool": tool, "args": summary}


@traced("tool.execute", as_type="span", metadata_fn=_graph_tool_metadata)
def _graph_dispatch(name: str, args: dict, workspace_root: str | None,
                    code_graph: CodeGraph | None = None,
                    knowledge_client: KnowledgeClient | None = None) -> object:
    """图内工具分发：W2 shell/git 工具 + W4 双知识工具 + 回退 W1 四文件工具。"""
    if name == "run_command":
        cmd = args.get("command")
        if not cmd:
            return ToolError("缺少参数: command")
        # timeout 上限 600：执行器层各有硬限（docker CLI 300s），LLM 超传会击穿执行层
        timeout = min(int(args.get("timeout", 60) or 60), 600)
        return run_command(cmd, cwd=args.get("cwd"),
                           timeout=timeout, workspace_root=workspace_root)
    if name == "run_tests":
        return run_tests(path=args.get("path"), workspace_root=workspace_root)
    if name == "git_status":
        return git_status(workspace_root=workspace_root)
    if name == "git_diff":
        return git_diff(workspace_root=workspace_root)
    if name == "git_log":
        return git_log(count=args.get("count", 10), workspace_root=workspace_root)
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


def _execute_verify_tests(workspace_root: str | None) -> object:
    """执行一次仓库测试验证：verify 节点与迭代触顶强制验证共用同一执行代码。

    在 graph 节点运行期调用（位于 run_agent_graph 的 sandbox_executor 上下文内），
    与 verify 节点同执行器语义（KA_EXECUTOR / 沙箱容器）。
    """
    return run_tests(workspace_root=workspace_root)


def build_graph(llm: LLMClient, max_iterations: int = 20,
                max_verify_rounds: int = 3,
                workspace_root: str | None = None,
                code_graph: CodeGraph | None = None,
                knowledge_client: KnowledgeClient | None = None) -> object:
    """构建 LangGraph 图（闭包捕获 llm、上限参数与 W4 注入的双知识源）。"""

    @traced("graph.plan", as_type="span")
    def plan_node(state: AgentState) -> dict:
        try:
            msg = llm.chat(
                [{"role": "system", "content": PLAN_PROMPT},
                 {"role": "user", "content": state["task"]}],
                [],
            )
        except Exception as e:  # noqa: BLE001
            # 与 decide 同语义：计划失败不阻断（decide 阶段再失败会走 llm_error 终态）
            return {"plan": [f"（计划生成失败: {type(e).__name__}，直接执行）"]}
        return {"plan": _parse_plan(msg.content or "")}

    @traced("graph.decide", as_type="generation")
    def decide_node(state: AgentState) -> dict:
        system = _decide_system(state["plan"], state["verify_rounds"], max_verify_rounds)
        # G5：工具结果滚动压缩（env 可调；默认保留最近 30 条、旧工具结果截 1200 字符）
        keep = int(os.environ.get("KA_CONTEXT_KEEP_RECENT", "30"))
        limit = int(os.environ.get("KA_CONTEXT_TOOL_LIMIT", "1200"))
        history = _compact_messages(state["messages"], keep, limit)
        messages = [{"role": "system", "content": system}] + history
        # A12 预算纪律执行层：deepseek 实测无视提示词预算纪律——迭代过 2/3 且
        # 尚无任何写操作时追加显式提醒（提示词合规的模型不受影响，多收一条消息）
        if (max_iterations > 0 and state["iteration"] >= max_iterations * 2 // 3
                and not any(s.tool_name in ("write_file", "edit_file")
                            for s in state["steps"])):
            messages.append({"role": "user", "content":
                f"[预算提醒] 迭代预算已过 2/3（{state['iteration']}/{max_iterations}）"
                "且尚无任何文件变更落地：立即停止探索，剩余预算全部用于执行"
                "（edit_file/write_file）与验证，并在预算耗尽前产出结论。"})
        try:
            msg = llm.chat(messages, _graph_tools())
        except Exception as e:  # noqa: BLE001
            # LLM 端点故障（超时/限流/网络）不得击穿整个 run：优雅降级为可收尾
            # 状态，保留已完成工作供 Review/审批（arknights#4 实测：50 轮上下文
            # 膨胀后单次调用超时直接崩溃，遥测与工作现场全部丢失）。
            return {
                "messages": state["messages"] + [
                    {"role": "assistant", "content": f"[LLM 调用失败] {type(e).__name__}: {e}"}],
                "final_answer": f"LLM 调用失败，任务提前终止: {type(e).__name__}: {e}",
                "status": "llm_error",
                "pending_tool_calls": None,
            }
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in msg.tool_calls
            ]
        assistant_msg = {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}
        if msg.reasoning_content:
            # thinking 模式回传（G8）：端点要求 reasoning_content 随历史带回
            assistant_msg["reasoning_content"] = msg.reasoning_content
        return {
            "messages": state["messages"] + [assistant_msg],
            "pending_tool_calls": msg.tool_calls,
            "iteration": state["iteration"] + 1,
        }

    @traced("graph.execute", as_type="span")
    def execute_node(state: AgentState) -> dict:
        steps = list(state["steps"])
        messages = list(state["messages"])
        for tc in state["pending_tool_calls"] or []:
            try:
                result = _graph_dispatch(tc.name, tc.arguments, workspace_root,
                                         code_graph, knowledge_client)
            except Exception as e:  # noqa: BLE001
                # 工具执行异常转观察值回注（Arch-C1）：执行器层（docker exec 等）
                # 会 raise ToolError，execute 是最后一个无防护节点——与 G1 同型击穿面
                result = ToolError(f"工具执行异常: {type(e).__name__}: {e}")
            steps.append(AgentStep(tool_name=tc.name, arguments=tc.arguments, result=result))
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _result_to_text(result),
            })
        return {"steps": steps, "messages": messages, "pending_tool_calls": None}

    @traced("graph.verify", as_type="span")
    def verify_node(state: AgentState) -> dict:
        tr = _execute_verify_tests(workspace_root)
        return {
            "test_result": tr,
            "verify_rounds": state["verify_rounds"] + 1,
            "test_results": state["test_results"] + [tr],
        }

    @traced("graph.reflect", as_type="generation")
    def reflect_node(state: AgentState) -> dict:
        tr = state["test_result"]
        if isinstance(tr, TestResult):
            details = asdict(tr)
        else:
            details = {"error": getattr(tr, "message", str(tr))}
        try:
            msg = llm.chat(
                [{"role": "system",
                  "content": _reflect_system(json.dumps(details, ensure_ascii=False))},
                 {"role": "user", "content": state["task"]}],
                [],
            )
        except Exception as e:  # noqa: BLE001
            # 反思失败不阻断循环：记失败说明后回到 decide（decide 再失败走 llm_error 终态）
            return {"messages": state["messages"] + [
                {"role": "assistant",
                 "content": f"[失败分析] LLM 调用失败: {type(e).__name__}: {e}"}]}
        return {"messages": state["messages"] + [
            {"role": "assistant", "content": f"[失败分析] {msg.content}"}]}

    @traced("graph.finalize", as_type="span")
    def finalize_node(state: AgentState) -> dict:
        if state["status"] == "llm_error":
            # LLM 故障降级路径：decide 已写入终态结论（保留 type 与原因），不覆盖
            return {"final_answer": state.get("final_answer") or "LLM 调用失败",
                    "status": "llm_error"}
        content = state["messages"][-1].get("content") if state["messages"] else ""
        return {"final_answer": content or "任务完成", "status": "done"}

    @traced("graph.finalize_limited", as_type="span")
    def finalize_limited_node(state: AgentState) -> dict:
        return {"final_answer": "已达到上限，任务未完成", "status": "limit"}

    @traced("graph.finalize_iter_limited", as_type="span")
    def finalize_iter_limited_node(state: AgentState) -> dict:
        # 迭代触顶：收尾前强制执行一次测试验证，为 Reviewer/审批人补充测试证据；
        # 结果仅记入 verify_rounds/test_results，不改变触顶语义（final_answer/status）。
        tr = _execute_verify_tests(workspace_root)
        return {
            "final_answer": "已达到上限，任务未完成",
            "status": "limit",
            "test_result": tr,
            "verify_rounds": state["verify_rounds"] + 1,
            "test_results": state["test_results"] + [tr],
        }

    def route_after_decide(state: AgentState) -> str:
        if state["status"] == "llm_error":
            return "finalize"
        if state["pending_tool_calls"]:
            return "execute" if state["iteration"] <= max_iterations else "finalize_iter_limited"
        return "verify"

    def route_after_verify(state: AgentState) -> str:
        tr = state["test_result"]
        passed = isinstance(tr, TestResult) and tr.failed == 0 and tr.error == 0
        if passed:
            return "finalize"
        if state["verify_rounds"] >= max_verify_rounds:
            return "finalize_limited"
        return "reflect"

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("decide", decide_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("reflect", reflect_node)
    g.add_node("finalize", finalize_node)
    g.add_node("finalize_limited", finalize_limited_node)
    g.add_node("finalize_iter_limited", finalize_iter_limited_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "decide")
    g.add_conditional_edges("decide", route_after_decide,
                            {"execute": "execute", "verify": "verify",
                             "finalize_iter_limited": "finalize_iter_limited",
                             "finalize": "finalize"})
    g.add_edge("execute", "decide")
    g.add_conditional_edges("verify", route_after_verify,
                            {"finalize": "finalize", "finalize_limited": "finalize_limited",
                             "reflect": "reflect"})
    g.add_edge("reflect", "decide")
    g.add_edge("finalize", END)
    g.add_edge("finalize_limited", END)
    g.add_edge("finalize_iter_limited", END)
    return g.compile()


def _compact_messages(messages: list[dict], keep_recent: int, tool_limit: int) -> list[dict]:
    """G5 上下文滚动压缩：最近 keep_recent 条原样保留，更早的工具结果截断到
    tool_limit 字符（带标记）。上下文无压缩是 50 轮域任务超时崩溃的根因之一；
    短对话（<= keep_recent）零影响。"""
    if len(messages) <= keep_recent:
        return messages
    cut = len(messages) - keep_recent
    out = []
    for i, m in enumerate(messages):
        if (i < cut and m.get("role") == "tool"
                and len(m.get("content") or "") > tool_limit):
            out.append({**m, "content": m["content"][:tool_limit] + "…[已压缩]"})
        else:
            out.append(m)
    return out


def _run_issue_setup_commands(workspace_root: str | None = None) -> None:
    """沙箱创建后、任务开始前执行任务级环境准备命令（KA_ISSUE_SETUP_COMMANDS）。

    dateutil#1545 实测：src 布局仓库在容器内裸 pytest 收集不到包（import 失败），
    需 editable 安装仓库源码；pip install -e 必须带 --no-build-isolation（沙箱运行期
    无网络，build isolation 会尝试联网取 setuptools）。命令按 && 分隔逐条执行，
    单条超时 300s；失败不中断任务（返回值由 Agent 在后续观察中自行消化）。
    workspace_root 必须透传（审查 I1：缺省时 resolve 到进程 cwd/workspace，
    docker 模式 relpath 越界 → 静默 no-op，local 模式在错误目录执行）。
    """
    raw = os.environ.get("KA_ISSUE_SETUP_COMMANDS", "").strip()
    if not raw:
        return
    for cmd in (c.strip() for c in raw.split("&&")):
        if cmd:
            run_command(cmd, timeout=300, workspace_root=workspace_root)


def run_agent_graph(task: str, llm: LLMClient, max_iterations: int = 20,
                    max_verify_rounds: int = 3,
                    workspace_root: str | None = None,
                    code_graph: CodeGraph | None = None,
                    knowledge_client: KnowledgeClient | None = None) -> AgentGraphResult:
    """执行任务：LangGraph 图驱动（命令执行在沙箱上下文内，一个任务一个沙箱）。"""
    graph = build_graph(llm, max_iterations, max_verify_rounds, workspace_root,
                        code_graph, knowledge_client)
    with sandbox_executor(workspace_root or os.getcwd()):
        _run_issue_setup_commands(workspace_root)
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
