"""Langfuse 可开关 trace：懒加载 client + traced 装饰器 + usage 记录。

设计原则（照搬兄弟项目 Arknights LLM Wiki observability 模式，独立实现）：
  - 可开关：LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL 三键齐备且 KA_TRACING != "0" 才启用
  - 懒加载：get_client() 首次调用才初始化；关闭态零开销、零网络
  - 不侵入：未启用时 traced() 返回原函数（身份保留），业务无感知
"""
from __future__ import annotations

import functools
import os

_client = None


def is_enabled() -> bool:
    """全局开关：三键齐备且 KA_TRACING 未显式设为 '0'。"""
    if os.environ.get("KA_TRACING") == "0":
        return False
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and os.environ.get("LANGFUSE_BASE_URL")
    )


def get_client():
    """懒加载 Langfuse 客户端；未启用返回 None。"""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        from langfuse import get_client as _get_client
        _client = _get_client()
    return _client


def flush() -> None:
    """冲刷未导出 trace（短生命周期进程结束前调用）。"""
    c = get_client()
    if c is not None:
        try:
            c.flush()
        except Exception:  # noqa: BLE001 — 观测层失败不影响业务
            pass


def traced(name: str | None = None, as_type: str = "span", metadata_fn=None):
    """可开关 trace 装饰器；关闭态返回原函数。

    metadata_fn: (args, kwargs, result) -> dict | None，结果写入 span metadata。
    """
    def deco(func):
        if not is_enabled():
            return func
        from langfuse import observe
        decorated = observe(name=name or func.__name__,
                            as_type=as_type)(func)
        if metadata_fn is None:
            return decorated

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = decorated(*args, **kwargs)
            try:
                meta = metadata_fn(args, kwargs, result)
                if meta:
                    c = get_client()
                    if c is not None:
                        c.update_current_span(metadata=meta)
            except Exception:  # noqa: BLE001 — 观测层失败不影响业务
                pass
            return result
        return wrapper
    return deco


def record_usage(model: str, tokens_in: int, tokens_out: int,
                 cost_usd: float, extra: dict | None = None) -> None:
    """在当前 generation observation 上记录 usage/cost（须处于 generation 内）。"""
    c = get_client()
    if c is None:
        return
    try:
        usage = {"input": int(tokens_in or 0), "output": int(tokens_out or 0)}
        details = {"total": round(float(cost_usd or 0.0), 6)}
        meta = {"model": model}
        if extra:
            meta.update(extra)
        c.update_current_generation(
            model=model, usage_details=usage, cost_details=details,
            metadata=meta)
    except Exception:  # noqa: BLE001 — 观测层失败不影响业务
        pass