# tests/test_llm.py
"""LLM 客户端测试：Mock 行为、配置校验、OpenAI 兼容消息转换"""
from types import SimpleNamespace

import pytest

from agent.llm import (
    LLMMessage,
    MockLLMClient,
    OpenAICompatClient,
    ToolCall,
    ToolSpec,
)


def test_mock_llm_pops_script_and_records_calls():
    script = [LLMMessage(role="assistant", content="你好")]
    mock = MockLLMClient(script)
    tools = [ToolSpec(name="list_files", description="列出文件", parameters={})]
    msg = mock.chat([{"role": "user", "content": "hi"}], tools)
    assert msg.content == "你好"
    assert mock.calls == [([{"role": "user", "content": "hi"}], tools)]
    # 脚本耗尽后仍可调用（返回占位文本）
    msg2 = mock.chat([], [])
    assert msg2.content == "（无预设响应）"


def test_openai_client_requires_env(monkeypatch):
    monkeypatch.delenv("opencode_go_api", raising=False)
    with pytest.raises(ValueError, match="opencode_go_api"):
        OpenAICompatClient()
    monkeypatch.setenv("opencode_go_api", "k")
    client = OpenAICompatClient()
    assert client.base_url == "https://opencode.ai/zen/go/v1"
    assert client.model == "mimo-v2.5"


def test_openai_client_chat_converts_response(monkeypatch):
    # 固定模型默认值，避免本机 OPENCODE_GO_MODEL 环境变量干扰断言
    monkeypatch.delenv("OPENCODE_GO_MODEL", raising=False)
    client = OpenAICompatClient(api_key="test-key", base_url="http://localhost:1")

    class FakeCompletions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            msg = SimpleNamespace(
                content="回答",
                tool_calls=[SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(
                        name="read_file", arguments='{"path": "a.py"}'),
                )],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    tools = [ToolSpec(name="read_file", description="读取文件",
                      parameters={"type": "object", "properties": {}})]
    msg = client.chat([{"role": "user", "content": "hi"}], tools)
    assert msg.content == "回答"
    assert fake.kwargs["model"] == "mimo-v2.5"
    assert fake.kwargs["tools"][0]["function"]["name"] == "read_file"
    assert msg.tool_calls[0].name == "read_file"
    assert msg.tool_calls[0].arguments == {"path": "a.py"}


def test_tokens_total_accumulates(monkeypatch):
    """OpenAICompatClient.chat 从 response usage 累计 tokens。"""
    from agent.llm import OpenAICompatClient

    class FakeResp:
        def __init__(self):
            self.choices = [type("C", (), {"message": type(
                "M", (), {"content": "ok", "tool_calls": None})()})()]
            self.usage = type("U", (), {"prompt_tokens": 11,
                                        "completion_tokens": 7})()

    captured = {}

    class FakeCompletions:
        """真实调用链 self._client.chat.completions.create(**params) 的末端。"""

        def create(self, **params):
            captured.update(params)
            return FakeResp()

    class FakeClient:
        # 修正说明：代理 openai.OpenAI 的构造；chat 必须为带 completions.create
        # 的属性链对象。brief 原版把 chat 写成直接返回响应的方法，会导致
        # self._client.chat.completions 抛 'method' object has no attribute
        # 'completions'，测试永远失败；这里改为与真实调用链一致的形状。
        def __init__(self, *a, **kw):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    c = OpenAICompatClient(api_key="k")
    c.chat([{"role": "user", "content": "hi"}], [])
    assert c.tokens_total == {"prompt": 11, "completion": 7}
    c.chat([{"role": "user", "content": "hi"}], [])
    assert c.tokens_total == {"prompt": 22, "completion": 14}


def test_mock_tokens_total_zero():
    """MockLLMClient 提供同构 tokens_total（默认 0）。"""
    from agent.llm import MockLLMClient
    m = MockLLMClient([])
    assert m.tokens_total == {"prompt": 0, "completion": 0}
