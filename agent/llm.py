# agent/llm.py
"""LLM 客户端：可插拔协议 + OpenAI 兼容实现 + Mock 实现。

真实配置从环境变量读取：opencode_go_api（Key）、OPENCODE_GO_BASE_URL（端点，
默认 https://opencode.ai/zen/go/v1）、OPENCODE_GO_MODEL（模型，默认 mimo-v2.5）。
"""
import json
import os
from dataclasses import dataclass
from typing import Protocol

import openai


@dataclass
class ToolSpec:
    """工具描述（OpenAI function calling 格式的简化视图）。"""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """一次工具调用：id 用于回注 tool 结果消息。"""
    id: str
    name: str
    arguments: dict


@dataclass
class LLMMessage:
    """LLM 返回消息：有 tool_calls 表示需要执行工具，否则 content 为最终回答。"""
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class LLMClient(Protocol):
    """LLM 客户端协议：ReAct 循环只依赖此接口。"""

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage: ...


class OpenAICompatClient:
    """OpenAI 兼容实现（opencode go 服务的 mimo-v2.5）。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or os.environ.get("opencode_go_api", "")
        self.base_url = base_url or os.environ.get(
            "OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1")
        self.model = model or os.environ.get("OPENCODE_GO_MODEL", "mimo-v2.5")
        if not self.api_key:
            raise ValueError("缺少 API Key：请设置环境变量 opencode_go_api")
        self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage:
        params: dict = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        resp = self._client.chat.completions.create(**params)
        msg = resp.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments or "{}"),
                )
                for tc in msg.tool_calls
            ]
        return LLMMessage(role="assistant", content=msg.content, tool_calls=tool_calls)


class MockLLMClient:
    """测试用 Mock：按脚本顺序弹出预设响应，并记录每次调用。"""

    def __init__(self, script: list[LLMMessage]):
        self.script = script
        self.calls: list[tuple[list[dict], list[ToolSpec]]] = []

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage:
        self.calls.append((messages, tools))
        if not self.script:
            return LLMMessage(role="assistant", content="（无预设响应）")
        return self.script.pop(0)
