"""tracing 可开关行为测试：关闭态直通零副作用，开启态包装 observe。"""
import os

import pytest

import tools.tracing as tracing


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "LANGFUSE_BASE_URL", "KA_TRACING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tracing, "_client", None)


def test_disabled_by_default():
    assert tracing.is_enabled() is False
    assert tracing.get_client() is None


def test_disabled_when_keys_missing():
    os.environ["KA_TRACING"] = "1"
    assert tracing.is_enabled() is False


def test_enabled_only_with_three_keys(monkeypatch):
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    assert tracing.is_enabled() is False            # 缺 BASE_URL
    os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"
    assert tracing.is_enabled() is True


def test_ka_tracing_0_disables_even_with_keys():
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"
    os.environ["KA_TRACING"] = "0"
    assert tracing.is_enabled() is False


def test_traced_disabled_returns_same_function(monkeypatch):
    """禁用态语义（I4 修复后）：装饰总是包装，调用时 get_client()=None 直通——
    行为等价（返回值不变、零观测副作用），不再要求装饰期函数身份。"""
    def f(x):
        return x + 1
    decorated = tracing.traced("span1")(f)
    monkeypatch.setattr(tracing, "get_client", lambda: None)
    assert decorated(1) == 2
    assert decorated(41) == 42


def test_traced_enabled_writes_metadata(monkeypatch):
    """开启态：metadata_fn 结果写入 span（上下文管理器模式，SDK v4 下生效）。"""
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"
    events = []

    class FakeSpan:
        def __init__(self, name, as_type):
            events.append(("start", name, as_type))
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def update(self, **kw):
            events.append(("update", kw))

    class FakeClient:
        def start_as_current_observation(self, name=None, as_type=None):
            return FakeSpan(name, as_type)

    monkeypatch.setattr(tracing, "get_client", lambda: FakeClient())
    monkeypatch.setattr(tracing, "_client", object())

    def f(x):
        return x + 1
    decorated = tracing.traced("span1", as_type="span",
                               metadata_fn=lambda a, k, r: {"result": r})(f)
    assert decorated(1) == 2
    assert ("start", "span1", "span") in events
    assert ("update", {"metadata": {"result": 2}}) in events


def test_traced_enabled_async_not_wrapped(monkeypatch):
    """开启态：异步函数直通不包装（不支持异步埋点，保持行为）。"""
    import asyncio
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"
    called = []
    monkeypatch.setattr(tracing, "get_client",
                        lambda: (_ for _ in ()).throw(AssertionError("不应调用 client")))
    async def f(x):
        return x + 1
    decorated = tracing.traced("a")(f)
    assert decorated is f
    assert asyncio.run(decorated(1)) == 2


def test_record_usage_disabled_noop():
    tracing.record_usage("mimo-v2.5", 10, 5, 0.001)   # 不抛异常即可