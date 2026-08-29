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


def test_deepseek_provider_uses_its_own_env(monkeypatch):
    """KA_LLM_PROVIDER=deepseek 时读取 deepseek_api / DEEPSEEK_* 配置。"""
    monkeypatch.delenv("opencode_go_api", raising=False)
    monkeypatch.delenv("OPENCODE_GO_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_GO_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.setenv("KA_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("deepseek_api", "dk")
    client = OpenAICompatClient()
    assert client.base_url == "https://api.deepseek.com"
    assert client.model == "deepseek-v4-flash"
    # 环境变量可覆盖端点与模型
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ds.example.com/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    client = OpenAICompatClient()
    assert client.base_url == "https://ds.example.com/v1"
    assert client.model == "deepseek-chat"


def test_deepseek_provider_missing_key(monkeypatch):
    monkeypatch.setenv("KA_LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("deepseek_api", raising=False)
    with pytest.raises(ValueError, match="deepseek_api"):
        OpenAICompatClient()


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("KA_LLM_PROVIDER", "no-such-provider")
    monkeypatch.setenv("opencode_go_api", "k")
    with pytest.raises(ValueError, match="未知供应商"):
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


# ---- E5: timeout / max_retries 构造参数透传（P1-4） ----

@pytest.fixture
def capture_openai_kwargs(monkeypatch):
    """替身 openai.OpenAI，捕获构造 kwargs（seam 与 test_tokens_total_accumulates 一致）。"""
    captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    return captured


def _clear_timeout_env(monkeypatch):
    monkeypatch.delenv("KA_LLM_TIMEOUT_S", raising=False)
    monkeypatch.delenv("KA_LLM_MAX_RETRIES", raising=False)


def test_timeout_env_parsed_and_passed(monkeypatch, capture_openai_kwargs):
    """KA_LLM_TIMEOUT_S 为合法浮点时解析并透传给 openai.OpenAI。"""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("KA_LLM_TIMEOUT_S", "45.5")
    OpenAICompatClient(api_key="k")
    assert capture_openai_kwargs["timeout"] == 45.5


def test_timeout_invalid_env_falls_back_to_120(monkeypatch, capture_openai_kwargs):
    """KA_LLM_TIMEOUT_S 非法时回退默认 120.0。"""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("KA_LLM_TIMEOUT_S", "abc")
    OpenAICompatClient(api_key="k")
    assert capture_openai_kwargs["timeout"] == 120.0


def test_timeout_unset_env_omitted(monkeypatch, capture_openai_kwargs):
    """未设 KA_LLM_TIMEOUT_S 时不传 timeout（交给 SDK 默认）。"""
    _clear_timeout_env(monkeypatch)
    OpenAICompatClient(api_key="k")
    assert "timeout" not in capture_openai_kwargs


def test_max_retries_env_parsed_and_passed(monkeypatch, capture_openai_kwargs):
    """KA_LLM_MAX_RETRIES 为合法整数时解析并透传。"""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("KA_LLM_MAX_RETRIES", "5")
    OpenAICompatClient(api_key="k")
    assert capture_openai_kwargs["max_retries"] == 5


def test_max_retries_invalid_env_falls_back_to_2(monkeypatch, capture_openai_kwargs):
    """KA_LLM_MAX_RETRIES 非法时回退默认 2。"""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("KA_LLM_MAX_RETRIES", "xyz")
    OpenAICompatClient(api_key="k")
    assert capture_openai_kwargs["max_retries"] == 2


def test_max_retries_unset_env_omitted(monkeypatch, capture_openai_kwargs):
    """未设 KA_LLM_MAX_RETRIES 时不传 max_retries（交给 SDK 默认）。"""
    _clear_timeout_env(monkeypatch)
    OpenAICompatClient(api_key="k")
    assert "max_retries" not in capture_openai_kwargs


def test_explicit_timeout_max_retries_override_env(monkeypatch, capture_openai_kwargs):
    """显式传入 timeout/max_retries 优先于环境变量。"""
    _clear_timeout_env(monkeypatch)
    monkeypatch.setenv("KA_LLM_TIMEOUT_S", "10")
    monkeypatch.setenv("KA_LLM_MAX_RETRIES", "1")
    OpenAICompatClient(api_key="k", timeout=30.0, max_retries=3)
    assert capture_openai_kwargs["timeout"] == 30.0
    assert capture_openai_kwargs["max_retries"] == 3


def test_no_env_keeps_previous_construction_kwargs(monkeypatch, capture_openai_kwargs):
    """无 KA_LLM_TIMEOUT_S/KA_LLM_MAX_RETRIES 时构造 kwargs 与旧行为完全等价。"""
    _clear_timeout_env(monkeypatch)
    OpenAICompatClient(api_key="k", base_url="http://localhost:1")
    assert capture_openai_kwargs == {
        "api_key": "k",
        "base_url": "http://localhost:1",
    }


# ---- E5-1: 超时异常透传（设计意图钉子：客户端层外抛，由图层的 G1 优雅降级捕获）----

def test_chat_timeout_error_propagates(monkeypatch):
    """chat 遇 APITimeoutError 必须原样外抛（吞掉会让 G1 的降级无从谈起）。"""
    import httpx
    import openai
    from agent.llm import LLMMessage, OpenAICompatClient, ToolSpec

    client = OpenAICompatClient(api_key="k", base_url="http://localhost:1")

    def raise_timeout(*args, **kwargs):
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://localhost:1"))

    class FakeCompletions:
        def create(self, *args, **kwargs):
            raise_timeout(*args, **kwargs)

    class FakeChat:
        completions = FakeCompletions()

    monkeypatch.setattr(client._client, "chat", FakeChat())
    with pytest.raises(openai.APITimeoutError):
        client.chat([{"role": "user", "content": "hi"}], [])


# ---- G8: thinking 模式 reasoning_content 回传（deepseek-v4-flash 实测 400）----

def test_chat_carries_reasoning_content(monkeypatch):
    """端点返回 reasoning_content 时必须带入 LLMMessage（供回放侧带回）。"""
    from types import SimpleNamespace
    from agent.llm import OpenAICompatClient, ToolSpec

    client = OpenAICompatClient(api_key="k", base_url="http://localhost:1")
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="答案", tool_calls=None, reasoning_content="思考链..."))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )

    class FakeCompletions:
        def create(self, *args, **kwargs):
            return fake_resp

    class FakeChat:
        completions = FakeCompletions()

    monkeypatch.setattr(client._client, "chat", FakeChat())
    msg = client.chat([{"role": "user", "content": "hi"}], [])
    assert msg.reasoning_content == "思考链..."


def test_replayed_history_passes_reasoning_content_back(tmp_path):
    """回放历史时 reasoning_content 随 assistant 消息带回（G8 验收：端点不再 400）。"""
    import json as _json
    from agent.loop import run_agent
    from agent.llm import LLMMessage, MockLLMClient, ToolCall

    first = LLMMessage(role="assistant", content=None,
                       tool_calls=[ToolCall(id="t1", name="list_files", arguments={})],
                       reasoning_content="需要先看目录")
    second = LLMMessage(role="assistant", content="完成")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    llm = MockLLMClient([first, second])
    run_agent("列出文件", llm, max_iterations=3, workspace_root=str(tmp_path))
    second_call_messages = llm.calls[1][0]
    assistant_msgs = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert any(m.get("reasoning_content") == "需要先看目录" for m in assistant_msgs)
