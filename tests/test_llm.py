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
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="opencode_go_api"):
        OpenAICompatClient()
    monkeypatch.setenv("opencode_go_api", "k")
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="OPENCODE_GO_BASE_URL"):
        OpenAICompatClient()


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
    assert fake.kwargs["model"] == "deepseek-4-flash"
    assert fake.kwargs["tools"][0]["function"]["name"] == "read_file"
    assert msg.tool_calls[0].name == "read_file"
    assert msg.tool_calls[0].arguments == {"path": "a.py"}
