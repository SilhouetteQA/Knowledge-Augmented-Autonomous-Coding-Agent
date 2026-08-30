# agent/llm.py
"""LLM 客户端：可插拔协议 + OpenAI 兼容实现 + Mock 实现。

真实配置按供应商（KA_LLM_PROVIDER，默认 opencode_go）从环境变量读取：
- opencode_go：opencode_go_api（Key）、OPENCODE_GO_BASE_URL（端点，
  默认 https://opencode.ai/zen/go/v1）、OPENCODE_GO_MODEL（模型，默认 mimo-v2.5）；
- deepseek：deepseek_api（Key）、DEEPSEEK_BASE_URL（端点，默认
  https://api.deepseek.com）、DEEPSEEK_MODEL（模型，默认 deepseek-v4-flash，
  端点实测模型列表：deepseek-v4-flash / deepseek-v4-flash-vision-exp / deepseek-v4-pro）。

请求超时与重试（P1-4，优先 SDK 原生参数，不自造重试循环）：
- KA_LLM_TIMEOUT_S：请求超时秒数（float，非法值回退 120.0；未设则不传，
  使用 SDK 默认超时）；
- KA_LLM_MAX_RETRIES：最多重试次数（int，非法值回退 2；未设则不传，
  使用 SDK 默认重试次数）。
"""
import json
import os
from dataclasses import dataclass
from typing import Protocol

import openai

from tools.tracing import record_usage, traced

# 供应商配置表（扩展新供应商时在此注册）
LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "opencode_go": {
        "key_env": "opencode_go_api",
        "base_url_env": "OPENCODE_GO_BASE_URL",
        "base_url_default": "https://opencode.ai/zen/go/v1",
        "model_env": "OPENCODE_GO_MODEL",
        "model_default": "mimo-v2.5",
    },
    "deepseek": {
        "key_env": "deepseek_api",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com",
        "model_env": "DEEPSEEK_MODEL",
        "model_default": "deepseek-v4-flash",
    },
}


def _env_timeout() -> float | None:
    """读取 KA_LLM_TIMEOUT_S：float 解析，非法值回退 120.0，未设返回 None。"""
    raw = os.environ.get("KA_LLM_TIMEOUT_S")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 120.0


def _env_max_retries() -> int | None:
    """读取 KA_LLM_MAX_RETRIES：int 解析，非法值回退 2，未设返回 None。"""
    raw = os.environ.get("KA_LLM_MAX_RETRIES")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


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
    """LLM 返回消息：有 tool_calls 表示需要执行工具，否则 content 为最终回答。

    reasoning_content：thinking 模式端点（deepseek-v4-flash 实测）随 assistant
    消息返回的思维链；回放历史时必须原样带回，否则端点 400
    （"The reasoning_content in the thinking mode must be passed back"）。
    """
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None


class LLMClient(Protocol):
    """LLM 客户端协议：ReAct 循环只依赖此接口。"""

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage: ...


def _parse_tool_call(tc) -> ToolCall:
    """OpenAI tool_call → ToolCall（IM-12：arguments 非法 JSON 不再击穿整任务）。

    JSON 截断/畸形（thinking 端点实测偶发）或合法 JSON 但非对象（如 list）时，
    构造保留原文的哨兵错误型 ToolCall（__unparsed_arguments__）；dispatch 层
    返回结构化错误作工具观察值，模型下一轮自行重试（对话不终止为 llm_error）。
    """
    raw = tc.function.arguments or ""
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ToolCall(id=tc.id, name=tc.function.name,
                        arguments={"__unparsed_arguments__": raw})
    if not isinstance(args, dict):
        return ToolCall(id=tc.id, name=tc.function.name,
                        arguments={"__unparsed_arguments__": raw})
    return ToolCall(id=tc.id, name=tc.function.name, arguments=args)


class OpenAICompatClient:
    """OpenAI 兼容实现（按 KA_LLM_PROVIDER 选择供应商与模型）。

    timeout / max_retries 传入 openai.OpenAI 构造（SDK 原生超时与重试，
    不在此自造重试循环）。两者为 None 时分别从 KA_LLM_TIMEOUT_S /
    KA_LLM_MAX_RETRIES 读取；仍未设则不在构造中传该参数（SDK 默认）。
    注意：openai SDK 不接受 max_retries=None（会抛 TypeError），
    因此未解析到值时必须省略该参数而非传 None。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 max_retries: int | None = None):
        provider = os.environ.get("KA_LLM_PROVIDER", "opencode_go")
        conf = LLM_PROVIDERS.get(provider)
        if conf is None:
            raise ValueError(
                f"未知供应商: {provider}（已知: {sorted(LLM_PROVIDERS)}，"
                f"通过 KA_LLM_PROVIDER 选择）")
        self.api_key = api_key or os.environ.get(conf["key_env"], "")
        self.base_url = base_url or os.environ.get(
            conf["base_url_env"], conf["base_url_default"])
        self.model = model or os.environ.get(conf["model_env"], conf["model_default"])
        if not self.api_key:
            raise ValueError(
                f"缺少 API Key：请设置环境变量 {conf['key_env']}"
                f"（供应商 {provider}）")
        if timeout is None:
            timeout = _env_timeout()
        if max_retries is None:
            max_retries = _env_max_retries()
        client_kwargs: dict = {"api_key": self.api_key, "base_url": self.base_url}
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        self._client = openai.OpenAI(**client_kwargs)
        self.tokens_total: dict[str, int] = {"prompt": 0, "completion": 0}

    @traced("llm.chat", as_type="generation")
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
        usage = getattr(resp, "usage", None)
        if usage is not None:
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            self.tokens_total["prompt"] += pt
            self.tokens_total["completion"] += ct
            record_usage(self.model, pt, ct, 0.0)
        msg = resp.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [_parse_tool_call(tc) for tc in msg.tool_calls]
        reasoning = getattr(msg, "reasoning_content", None)
        return LLMMessage(role="assistant", content=msg.content, tool_calls=tool_calls,
                          reasoning_content=reasoning)


class MockLLMClient:
    """测试用 Mock：按脚本顺序弹出预设响应，并记录每次调用。"""

    def __init__(self, script: list[LLMMessage]):
        self.script = script
        self.calls: list[tuple[list[dict], list[ToolSpec]]] = []
        self.tokens_total: dict[str, int] = {"prompt": 0, "completion": 0}

    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage:
        self.calls.append((messages, tools))
        if not self.script:
            return LLMMessage(role="assistant", content="（无预设响应）")
        return self.script.pop(0)
