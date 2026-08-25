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


def test_traced_disabled_returns_same_function():
    def f(x):
        return x + 1
    decorated = tracing.traced("span1")(f)
    assert decorated is f                        # 身份保留
    assert decorated(1) == 2


def test_traced_enabled_wraps_observe(monkeypatch):
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk"
    os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"
    calls = []
    class FakeObserve:
        def __init__(self, name, as_type, capture_input=False,
                     capture_output=False):
            calls.append((name, as_type))
        def __call__(self, func):
            def wrapped(*a, **kw):
                return func(*a, **kw)
            return wrapped
    import langfuse
    monkeypatch.setattr(langfuse, "observe", FakeObserve)
    def f(x):
        return x + 1
    decorated = tracing.traced("span1", as_type="span")(f)
    assert decorated is not f
    assert decorated(1) == 2
    assert calls == [("span1", "span")]


def test_record_usage_disabled_noop():
    tracing.record_usage("mimo-v2.5", 10, 5, 0.001)   # 不抛异常即可