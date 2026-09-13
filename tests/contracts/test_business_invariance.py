"""Coding producer seam 业务不变量测试（Spec 08）。

off / observe 固定响应下，业务输出与 Legacy 副作用必须**精确一致**；Foundation
观察只是旁路，不改变任何业务返回、tokens_total、CaseResult 字段、Langfuse
参数或 TraceSummary 字段。

关键不变量（母 Spec §9.2–9.5 / Spec 08 Acceptance Criteria）：
- chat 的 LLMMessage 返回与 tokens_total 累加不变
- record_usage 的 Langfuse 参数不含 contract facts（不渗漏进 extra/metadata）
- benchmark 三个分支的 CaseResult 字段不变
- report_trace 的 TraceSummary 字段不变
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.contracts.enums.modes import ContractMode

from adapters.foundation.runtime import (
    CodingFoundationRuntime,
    reset_foundation_runtime,
    set_foundation_runtime,
)
from agent import llm as llm_mod
from benchmark import runner as runner_mod
from tools import report_trace as rt_mod
from tools import tracing as tracing_mod

COMMIT = "c" * 40
PAYLOAD_HASH = "sha256:" + "f" * 64
RUN_ID = "run-spec08"


class RecordingSink:
    def __init__(self) -> None:
        self.records = []

    def emit(self, record) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _clean_runtime():
    reset_foundation_runtime()
    yield
    reset_foundation_runtime()


def use_runtime(mode: ContractMode | str) -> CodingFoundationRuntime:
    runtime = CodingFoundationRuntime(
        sink=RecordingSink(),
        mode=mode,
        run_id=RUN_ID,
        repository_commit=COMMIT,
        payload_hash=PAYLOAD_HASH,
    )
    set_foundation_runtime(runtime)
    return runtime


def make_response(
    *, content: str = "ok", prompt_tokens: int = 7, completion_tokens: int = 5
) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeOpenAI:
    def __init__(self, response):
        self.chat = _FakeChat(response)


class _FakeLLM:
    def __init__(self):
        self.tokens_total = {"prompt": 0, "completion": 0}


def _make_case() -> SimpleNamespace:
    return SimpleNamespace(
        id="c1", category="x", repository="owner__repo",
        issue=SimpleNamespace(number=1), max_iterations=1,
    )


def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "sandbox_executor",
                        lambda repo_dir: contextlib.nullcontext())
    monkeypatch.setattr(runner_mod, "_ensure_repository", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_run_setup_commands", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# chat：返回与 tokens_total 不变
# --------------------------------------------------------------------------- #


def test_chat_return_and_tokens_total_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    """off/observe 下 chat 的 LLMMessage 与 tokens_total 精确一致。"""
    off = _run_chat(monkeypatch, ContractMode.OFF)
    obs = _run_chat(monkeypatch, ContractMode.OBSERVE)

    assert off["content"] == obs["content"] == "ok"
    assert off["tool_calls"] == obs["tool_calls"]
    assert off["reasoning"] == obs["reasoning"]
    assert off["tokens"] == obs["tokens"] == {"prompt": 7, "completion": 5}


def _run_chat(monkeypatch: pytest.MonkeyPatch, mode: ContractMode) -> dict:
    use_runtime(mode)
    monkeypatch.setattr(llm_mod.openai, "OpenAI",
                        lambda **kw: _FakeOpenAI(make_response()))
    client = llm_mod.OpenAICompatClient(api_key="k", base_url="http://x", model="mimo-v2.5")
    msg = client.chat([{"role": "user", "content": "hi"}], [])
    return {
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "reasoning": msg.reasoning_content,
        "tokens": dict(client.tokens_total),
    }


def test_chat_without_usage_does_not_call_record_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """usage 缺失时 Legacy 不调 record_usage（不变量保持）。"""
    calls: list = []
    monkeypatch.setattr(llm_mod, "record_usage",
                        lambda *a, **k: calls.append((a, k)))

    use_runtime(ContractMode.OBSERVE)
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="ok", tool_calls=None))], usage=None)
    monkeypatch.setattr(llm_mod.openai, "OpenAI", lambda **kw: _FakeOpenAI(response))
    client = llm_mod.OpenAICompatClient(api_key="k", base_url="http://x", model="mimo-v2.5")
    client.chat([{"role": "user", "content": "hi"}], [])

    assert calls == []  # Legacy 分支不变：usage 缺失不调 record_usage


# --------------------------------------------------------------------------- #
# record_usage：Langfuse 参数不渗漏 contract facts
# --------------------------------------------------------------------------- #


def test_record_usage_langfuse_params_exclude_contract_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """contract facts 绝不进入 Langfuse extra / metadata。"""
    calls: list = []

    class _FakeLangfuse:
        def update_current_generation(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(tracing_mod, "get_client", lambda: _FakeLangfuse())

    use_runtime(ContractMode.OBSERVE)
    tracing_mod.record_usage("mimo-v2.5", 10, 5, 0.0, extra={"retry": True},
                             contract_facts=True)

    assert len(calls) == 1
    update = calls[0]
    assert update["model"] == "mimo-v2.5"
    assert update["usage_details"] == {"input": 10, "output": 5}
    assert update["cost_details"] == {"total": 0.0}
    assert update["metadata"] == {"model": "mimo-v2.5", "retry": True}
    # contract facts 未渗漏进任何 Langfuse 字段
    assert "contract_facts" not in update
    for value in update.values():
        assert "contract_facts" not in str(value)


def test_record_usage_noop_when_client_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Langfuse client absent 时原 no-op 不变，且不依赖 Foundation 观察。"""
    monkeypatch.setattr(tracing_mod, "get_client", lambda: None)

    use_runtime(ContractMode.OBSERVE)
    # 不抛异常即通过（Foundation 观察独立于 Langfuse）
    tracing_mod.record_usage("mimo-v2.5", 10, 5, 0.0, contract_facts=True)


# --------------------------------------------------------------------------- #
# benchmark：三个分支的 CaseResult 不变
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stage", ["environment_error", "normal", "error"])
def test_benchmark_case_result_invariant(
    stage: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """三个分支的 CaseResult 字段在 off/observe 下精确一致。"""
    off = _run_case(monkeypatch, tmp_path, stage, ContractMode.OFF)
    obs = _run_case(monkeypatch, tmp_path, stage, ContractMode.OBSERVE)

    assert off.status == obs.status
    assert off.resolution == obs.resolution
    assert off.cost_usd == obs.cost_usd
    assert off.tokens_prompt == obs.tokens_prompt
    assert off.tokens_completion == obs.tokens_completion
    assert off.diff == obs.diff


def _run_case(monkeypatch, tmp_path, stage, mode) -> object:
    use_runtime(mode)
    _patch_runner(monkeypatch)
    if stage == "environment_error":
        monkeypatch.setattr(runner_mod, "_test_summary", lambda *a, **k: (False, "x"))
    elif stage == "normal":
        monkeypatch.setattr(runner_mod, "_test_summary", lambda *a, **k: (True, ""))
        monkeypatch.setattr(runner_mod, "run_issue_agent", lambda *a, **k: SimpleNamespace())
        monkeypatch.setattr(runner_mod, "_case_result",
                            lambda *a, **k: SimpleNamespace(status="resolved",
                                                            resolution=True,
                                                            cost_usd=0.0,
                                                            tokens_prompt=0,
                                                            tokens_completion=0,
                                                            diff=""))
    else:  # error
        monkeypatch.setattr(runner_mod, "_ensure_repository",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    return runner_mod._run_one_case(_make_case(), _FakeLLM(), str(tmp_path), str(tmp_path))


# --------------------------------------------------------------------------- #
# report_trace：TraceSummary 不变
# --------------------------------------------------------------------------- #


def test_report_trace_summary_invariant() -> None:
    """sdk 与 clickhouse 的 TraceSummary 在 off/observe 下精确一致。"""
    # sdk
    use_runtime(ContractMode.OFF)
    off_sdk = rt_mod._summarize_trace(
        "t", SimpleNamespace(name="task", observations=[
            SimpleNamespace(name="llm.chat", latency=1.0, start_time=None, end_time=None,
                            metadata=None, cost_details={"total": 0.05})]))
    use_runtime(ContractMode.OBSERVE)
    obs_sdk = rt_mod._summarize_trace(
        "t", SimpleNamespace(name="task", observations=[
            SimpleNamespace(name="llm.chat", latency=1.0, start_time=None, end_time=None,
                            metadata=None, cost_details={"total": 0.05})]))
    assert off_sdk == obs_sdk

    # clickhouse
    rows = [("generation", "llm.chat", None, None, {"input": 10, "output": 5},
             0.05, None, None, [], [], False)]
    use_runtime(ContractMode.OFF)
    off_ch = rt_mod._summarize_events("t", rows)
    use_runtime(ContractMode.OBSERVE)
    obs_ch = rt_mod._summarize_events("t", rows)
    assert off_ch == obs_ch


def test_report_trace_renderer_invariant() -> None:
    """Markdown 渲染不受 Foundation 观察影响。"""
    use_runtime(ContractMode.OBSERVE)
    rows = [("generation", "llm.chat", None, None, {"input": 10, "output": 5},
             0.05, None, None, [], [], False)]
    summary = rt_mod._summarize_events("t", rows)

    md = rt_mod.trace_report_md(summary)
    assert "llm.chat" in md
    assert "$0.0500" in md
