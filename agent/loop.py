# agent/loop.py
"""最小 ReAct 循环：LLM 决策 → 工具调用 → 观察结果 → 循环，上限 max_iterations 轮。"""
import json
import os
from dataclasses import asdict, dataclass

from agent.llm import LLMClient, ToolSpec
from tools.docker_sandbox import sandbox_executor
from tools.file_tools import (
    ToolError,
    edit_file,
    list_files,
    read_file,
    search_code,
    write_file,
)
from tools.tracing import traced

# 系统提示词：约束工具使用范围与行为
SYSTEM_PROMPT = (
    "你是编码助手。你可以调用工具查看和修改工作区内的文件。\n"
    "规则：\n"
    "1. 只操作工作区内的文件，不要尝试访问工作区之外的路径。\n"
    "2. 每次工具调用后先观察结果，再决定下一步。\n"
    "3. 任务完成后，用中文总结你做了什么。"
)

# 最大迭代轮数（防死循环）
DEFAULT_MAX_ITERATIONS = 10


@dataclass
class AgentStep:
    """一次工具调用及其结果。"""
    tool_name: str
    arguments: dict
    result: object


@dataclass
class AgentResult:
    """一次任务的完整结果。"""
    steps: list[AgentStep]
    final_answer: str
    iteration_count: int
    stopped_by_limit: bool


def _build_tools() -> list[ToolSpec]:
    """四个文件工具的 JSON schema 描述（暴露给 LLM）。"""
    return [
        ToolSpec(
            name="list_files",
            description="列出工作区内的文件与目录（跳过 .git/__pycache__ 等）",
            parameters={
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "description": "相对工作区根的目录路径，缺省为工作区根",
                    },
                },
            },
        ),
        ToolSpec(
            name="read_file",
            description="读取工作区内文本文件内容（带行号，上限 500KB）",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区根的文件路径",
                    },
                },
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="search_code",
            description="在代码中搜索关键词（ripgrep，自动遵循 .gitignore）",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "root": {
                        "type": "string",
                        "description": "搜索范围目录，缺省为工作区根",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "是否忽略大小写",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="write_file",
            description="写入工作区内文件（自动创建父目录，覆盖已有内容）；新建文件用本工具",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区根的文件路径",
                    },
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        ),
        ToolSpec(
            name="edit_file",
            description="局部编辑已有文件：将 old_text 精确替换为 new_text（修改已有文件优先用本工具，"
                        "避免整文件重写丢失内容）。old_text 必须逐字符匹配（含缩进换行）且默认恰好出现 1 次，"
                        "出现多次时传 expected_count=<次数> 确认全部替换",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区根的文件路径",
                    },
                    "old_text": {"type": "string", "description": "要被替换的原文片段（精确匹配）"},
                    "new_text": {"type": "string", "description": "替换后的新片段"},
                    "expected_count": {"type": "integer",
                                       "description": "old_text 预期出现次数（多处替换时显式确认）"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        ),
    ]


def _tool_metadata(args, kwargs, result) -> dict:
    """工具 span metadata：记录工具名（W1 循环路径参数摘要可选，本处仅工具名）。"""
    return {"tool": args[0] if args else ""}


@traced("tool.execute", as_type="span", metadata_fn=_tool_metadata)
def _dispatch(name: str, args: dict, workspace_root: str | None) -> object:
    """工具调用分发：返回成功数据或 ToolError。"""
    if name == "list_files":
        return list_files(root=args.get("root"), workspace_root=workspace_root)
    if name == "read_file":
        path = args.get("path")
        if path is None:
            return ToolError("缺少参数: path")
        return read_file(path, workspace_root=workspace_root)
    if name == "search_code":
        query = args.get("query")
        if query is None:
            return ToolError("缺少参数: query")
        return search_code(
            query,
            root=args.get("root"),
            ignore_case=args.get("ignore_case", False),
            workspace_root=workspace_root,
        )
    if name == "write_file":
        path = args.get("path")
        content = args.get("content")
        if path is None:
            return ToolError("缺少参数: path")
        if content is None:
            return ToolError("缺少参数: content")
        return write_file(path, content, workspace_root=workspace_root)
    if name == "edit_file":
        path = args.get("path")
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if path is None:
            return ToolError("缺少参数: path")
        if old_text is None:
            return ToolError("缺少参数: old_text")
        if new_text is None:
            return ToolError("缺少参数: new_text")
        return edit_file(path, old_text, new_text,
                         expected_count=args.get("expected_count"),
                         workspace_root=workspace_root)
    return ToolError(f"未知工具: {name}")


def _result_to_text(result: object) -> str:
    """工具结果序列化为回注文本；ToolError 转 {error: ...}；字符串原样返回。"""
    if isinstance(result, ToolError):
        return json.dumps({"error": result.message}, ensure_ascii=False)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return json.dumps([
            asdict(x) if hasattr(x, "__dataclass_fields__") else x
            for x in result
        ], ensure_ascii=False)
    return json.dumps(asdict(result), ensure_ascii=False)


def run_agent(task: str, llm: LLMClient, max_iterations: int = DEFAULT_MAX_ITERATIONS,
              workspace_root: str | None = None) -> AgentResult:
    """执行任务：循环调用 LLM，执行其工具调用并回注结果，直至无工具调用或达上限。

    命令执行位于沙箱上下文内（一个任务一个沙箱，KA_EXECUTOR=docker 时全部命令进容器）。
    """
    with sandbox_executor(workspace_root or os.getcwd()):
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        tools = _build_tools()
        steps: list[AgentStep] = []
        for i in range(max_iterations):
            try:
                msg = llm.chat(messages, tools)
            except Exception as e:  # noqa: BLE001
                # LLM 故障优雅降级（与 graph G1 同语义）：保留已有步骤返回终态
                return AgentResult(
                    steps=steps,
                    final_answer=f"LLM 调用失败，任务提前终止: {type(e).__name__}: {e}",
                    iteration_count=i,
                    stopped_by_limit=False,
                )
            if not msg.tool_calls:
                return AgentResult(
                    steps=steps,
                    final_answer=msg.content or "",
                    iteration_count=i + 1,
                    stopped_by_limit=False,
                )
            results = []
            for tc in msg.tool_calls:
                result = _dispatch(tc.name, tc.arguments, workspace_root)
                steps.append(AgentStep(tool_name=tc.name, arguments=tc.arguments, result=result))
                results.append(result)
            # OpenAI 格式：先回注 assistant 的 tool_calls 消息，再逐条回注 tool 结果
            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            if msg.reasoning_content:
                # thinking 模式回传（G8）：端点要求 reasoning_content 随历史带回
                assistant_msg["reasoning_content"] = msg.reasoning_content
            messages.append(assistant_msg)
            for tc, result in zip(msg.tool_calls, results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _result_to_text(result),
                })
        return AgentResult(
            steps=steps,
            final_answer="已达到迭代上限，任务未完成",
            iteration_count=max_iterations,
            stopped_by_limit=True,
        )
