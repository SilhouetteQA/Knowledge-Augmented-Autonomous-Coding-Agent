"""Coding producer seam 接线测试（Spec 08）。

覆盖七个 stage variant：openai_compat、langfuse_generation、normal、
environment_error、error、sdk、clickhouse。同一模型调用产生两条 producer
evidence（coding.agent.llm_usage + coding.trace.generation_usage）是**预期而非
重复错误**（母 Spec §7.2 / Spec 08 验证要求）。

测试只 monkeypatch **模块边界**（openai.OpenAI / sandbox_executor / 内部 helper），
不 monkeypatch 业务私有函数。每个测试前注入 runtime，结束后清空进程级缓存。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.contracts.enums.evidence import ValidationStatus
from agent_core.contracts.enums.modes import ContractMode
from agent_core.contracts.models.evidence import EvidenceRecord

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
    """口袋 sink：只收集记录，不落盘。"""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def emit(self, record: EvidenceRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _clean_runtime():
    reset_foundation_runtime()
    yield
    reset_foundation_runtime()


def use_runtime(
    mode: ContractMode | str = ContractMode.OBSERVE,
) -> tuple[CodingFoundationRuntime, RecordingSink]:
    sink = RecordingSink()
    runtime = CodingFoundationRuntime(
        sink=sink,
        mode=mode,
        run_id=RUN_ID,
        repository_commit=COMMIT,
        payload_hash=PAYLOAD_HASH,
    )
    set_foundation_runtime(runtime)
    return runtime, sink


# --------------------------------------------------------------------------- #
# fake 与固定响应
# --------------------------------------------------------------------------- #


def make_response(
    *,
    content: str = "ok",
    prompt_tokens: int = 7,
    completion_tokens: int = 5,
    with_usage: bool = True,
) -> SimpleNamespace:
    usage = (
        SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        if with_usage
        else None
    )
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _FakeCompletions:
    def __init__(self, response: SimpleNamespace) -> None:
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeChat:
    def __init__(self, response: SimpleNamespace) -> None:
        self.completions = _FakeCompletions(response)


class _FakeOpenAI:
    def __init__(self, response: SimpleNamespace) -> None:
        self.chat = _FakeChat(response)


class _FakeLLM:
    """benchmark 用的最小 llm（只需 tokens_total）。"""

    def __init__(self) -> None:
        self.tokens_total: dict[str, int] = {"prompt": 0, "completion": 0}


def _make_case() -> SimpleNamespace:
    return SimpleNamespace(
        id="c1", category="x", repository="owner__repo",
        issue=SimpleNamespace(number=1), max_iterations=1,
    )


def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 benchmark 的沙箱 / 仓库 / setup 全部换成 no-op。"""
    monkeypatch.setattr(runner_mod, "sandbox_executor",
                        lambda repo_dir: contextlib.nullcontext())
    monkeypatch.setattr(runner_mod, "_ensure_repository", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_run_setup_commands", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# openai_compat + langfuse_generation（agent/llm.py + tools/tracing.py）
# --------------------------------------------------------------------------- #


def test_chat_seam_emits_two_producer_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat 成功响应后产出 openai_compat + langfuse_generation 两条证据，ledger 只写一次。"""
    runtime, sink = use_runtime()
    monkeypatch.setattr(llm_mod.openai, "OpenAI",
                        lambda **kw: _FakeOpenAI(make_response()))
    client = llm_mod.OpenAICompatClient(api_key="k", base_url="http://x", model="mimo-v2.5")

    msg = client.chat([{"role": "user", "content": "hi"}], [])

    assert msg.content == "ok"
    stages = [r.mapping_stage for r in sink.records]
    assert stages.count("openai_compat") == 1
    assert stages.count("langfuse_generation") == 1
    # 两条 producer evidence 都是 PASS
    assert all(r.validation_status is ValidationStatus.PASS for r in sink.records)
    # openai_compat 写 ledger，langfuse_generation 不写 → 只 append 一次
    assert len(runtime.case_components(0)) == 1


def test_chat_seam_without_usage_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 未报告 usage：openai_compat 仍产出，但 usage=unknown（不是零）。"""
    _, sink = use_runtime()
    monkeypatch.setattr(llm_mod.openai, "OpenAI",
                        lambda **kw: _FakeOpenAI(make_response(with_usage=False)))
    client = llm_mod.OpenAICompatClient(api_key="k", base_url="http://x", model="mimo-v2.5")

    client.chat([{"role": "user", "content": "hi"}], [])

    openai_record = next(r for r in sink.records if r.mapping_stage == "openai_compat")
    usage = openai_record.foundation_output.usage  # type: ignore[union-attr]
    assert usage.input_tokens is None
    assert usage.source == "unknown"


def test_record_usage_seam_langfuse_generation() -> None:
    """record_usage 传 contract_facts 时产出 langfuse_generation 证据。"""
    _, sink = use_runtime()

    tracing_mod.record_usage("mimo-v2.5", 10, 5, 0.0, contract_facts=True)

    gen = [r for r in sink.records if r.mapping_stage == "langfuse_generation"]
    assert len(gen) == 1
    assert gen[0].producer_id == "coding.trace.generation_usage"


def test_record_usage_without_contract_facts_no_observation() -> None:
    """未传 contract_facts 时向后兼容，不产出契约证据。"""
    _, sink = use_runtime()

    tracing_mod.record_usage("mimo-v2.5", 10, 5, 0.0)

    assert sink.records == []


# --------------------------------------------------------------------------- #
# benchmark 三个 case stage（benchmark/runner.py）
# --------------------------------------------------------------------------- #


def test_benchmark_environment_error_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """基线失败 → environment_error 分支产 case_cost 证据。"""
    _, sink = use_runtime()
    _patch_runner(monkeypatch)
    monkeypatch.setattr(runner_mod, "_test_summary", lambda *a, **k: (False, "baseline fail"))

    result = runner_mod._run_one_case(_make_case(), _FakeLLM(), str(tmp_path), str(tmp_path))

    assert result.status == "environment_error"
    recs = [r for r in sink.records if r.mapping_stage == "environment_error"]
    assert len(recs) == 1
    assert recs[0].producer_id == "coding.benchmark.case_cost"


def test_benchmark_normal_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """基线通过 + Agent 正常 → normal 分支产 case_cost 证据。"""
    _, sink = use_runtime()
    _patch_runner(monkeypatch)
    monkeypatch.setattr(runner_mod, "_test_summary", lambda *a, **k: (True, ""))
    monkeypatch.setattr(runner_mod, "run_issue_agent", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(runner_mod, "_case_result",
                        lambda *a, **k: SimpleNamespace(status="resolved"))

    result = runner_mod._run_one_case(_make_case(), _FakeLLM(), str(tmp_path), str(tmp_path))

    assert result.status == "resolved"
    recs = [r for r in sink.records if r.mapping_stage == "normal"]
    assert len(recs) == 1
    assert recs[0].producer_id == "coding.benchmark.case_cost"


def test_benchmark_error_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """执行异常 → error 分支产 case_cost 证据。"""
    _, sink = use_runtime()
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        runner_mod, "_ensure_repository",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = runner_mod._run_one_case(_make_case(), _FakeLLM(), str(tmp_path), str(tmp_path))

    assert result.status == "error"
    recs = [r for r in sink.records if r.mapping_stage == "error"]
    assert len(recs) == 1
    assert recs[0].producer_id == "coding.benchmark.case_cost"


# --------------------------------------------------------------------------- #
# sdk + clickhouse（tools/report_trace.py）
# --------------------------------------------------------------------------- #


def test_report_trace_sdk_stage() -> None:
    """SDK 路径形成旧 TraceSummary 后旁路 emit stage=sdk。"""
    _, sink = use_runtime()
    obs = [
        SimpleNamespace(name="llm.chat", latency=1.0, start_time=None, end_time=None,
                        metadata=None, cost_details={"total": 0.05}),
    ]

    summary = rt_mod._summarize_trace("t", SimpleNamespace(name="task", observations=obs))

    assert summary.task == "task"
    assert summary.cost_usd == 0.0  # 旧值不变（SDK 路径硬编码 0）
    recs = [r for r in sink.records if r.mapping_stage == "sdk"]
    assert len(recs) == 1
    assert recs[0].producer_id == "coding.trace.summary"


def test_report_trace_sdk_missing_cost_is_unknown() -> None:
    """SDK 缺 cost 时旧 0.0 映射 unknown（不误判为真零）。"""
    _, sink = use_runtime()
    obs = [SimpleNamespace(name="llm.chat", latency=1.0, start_time=None, end_time=None,
                           metadata=None)]

    rt_mod._summarize_trace("t", SimpleNamespace(name="task", observations=obs))

    rec = next(r for r in sink.records if r.mapping_stage == "sdk")
    summary = rec.foundation_output.cost_summary  # type: ignore[union-attr]
    assert summary.known_amount is None
    assert summary.component_count == 0


def test_report_trace_clickhouse_stage() -> None:
    """ClickHouse 路径在 _as_float 前区分 total_cost 缺失与显式零。"""
    _, sink = use_runtime()
    rows = [
        ("generation", "llm.chat", None, None, {"input": 10, "output": 5},
         0.05, None, None, [], [], False),
        ("span", "tool.x", None, None, {}, None, None, None, [], [], False),
    ]

    summary = rt_mod._summarize_events("t", rows)

    assert summary.cost_usd == 0.05  # 旧累加不变
    rec = next(r for r in sink.records if r.mapping_stage == "clickhouse")
    assert rec.producer_id == "coding.trace.summary"
    cs = rec.foundation_output.cost_summary  # type: ignore[union-attr]
    # 仅 generation row 形成 component；provider 报告的真实成本
    assert cs.component_count == 1
    assert cs.known_amount is not None


def test_report_trace_clickhouse_absent_cost_is_unknown() -> None:
    """total_cost 缺失（None）与显式零区分：缺失 → unknown 而非真零。"""
    _, sink = use_runtime()
    rows = [
        ("generation", "llm.chat", None, None, {"input": 1, "output": 1},
         None, None, None, [], [], False),
    ]

    rt_mod._summarize_events("t", rows)

    rec = next(r for r in sink.records if r.mapping_stage == "clickhouse")
    cs = rec.foundation_output.cost_summary  # type: ignore[union-attr]
    assert cs.component_count == 1
    assert cs.known_amount is None  # absent → unknown


# --------------------------------------------------------------------------- #
# off 模式与七 stage 覆盖
# --------------------------------------------------------------------------- #


def test_off_mode_runs_no_seam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """off 模式下所有 seam 都不产出证据。"""
    runtime, sink = use_runtime(ContractMode.OFF)
    monkeypatch.setattr(llm_mod.openai, "OpenAI",
                        lambda **kw: _FakeOpenAI(make_response()))
    client = llm_mod.OpenAICompatClient(api_key="k", base_url="http://x", model="mimo-v2.5")
    client.chat([{"role": "user", "content": "hi"}], [])
    tracing_mod.record_usage("mimo-v2.5", 1, 2, 0.0, contract_facts=True)

    _patch_runner(monkeypatch)
    monkeypatch.setattr(runner_mod, "_test_summary", lambda *a, **k: (False, "x"))
    runner_mod._run_one_case(_make_case(), _FakeLLM(), str(tmp_path), str(tmp_path))

    rt_mod._summarize_trace("t", SimpleNamespace(name="task", observations=[]))

    assert sink.records == []
    assert runtime.ledger_allocated is False
    assert runtime.sink_failure_count == 0


# ---- A7: 进程级 runtime 必须采纳 AGENT_CONTRACT_RUN_ID --------------------------

def test_env_built_runtime_adopts_agent_contract_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A7 回归钉子：按环境构造的 runtime 必须用 ``AGENT_CONTRACT_RUN_ID`` 作为 run_id。

    L3 gate 只在 ``<evidence_root>/<manifest run_id>/events`` 下找证据。若
    ``_build_runtime_from_env`` 不读该环境变量，就会回落到进程级 UUID，把证据写进
    gate **永远找不到**的目录 —— 现象是「0 事件 + required producer/stage 未观测」，
    而业务路径完全正常（Spec 13 在 Coding 仓第一次真实运行时的真实表现：
    102 条格式完全正确、commit 与 mode 都对的事件被写进了
    ``<staging>/7e3c49b5-…/events/``）。
    """
    from adapters.foundation import runtime as rt

    monkeypatch.setenv("AGENT_CONTRACT_MODE", "observe")
    monkeypatch.setenv("AGENT_CONTRACT_RUN_ID", "foundation-0_1_0-c1-coding-smoke")
    monkeypatch.setenv("AGENT_CONTRACT_COMMIT", COMMIT)
    monkeypatch.setenv("AGENT_CONTRACT_EVIDENCE_DIR", str(tmp_path / "staging"))
    rt.reset_foundation_runtime()

    runtime = rt.get_foundation_runtime()

    assert runtime.run_id == "foundation-0_1_0-c1-coding-smoke"


def test_env_built_runtime_ignores_unsafe_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不安全的 run_id 在 observe 下被忽略（回落 UUID）而不是抛错击穿业务。"""
    from adapters.foundation import runtime as rt

    monkeypatch.setenv("AGENT_CONTRACT_MODE", "observe")
    monkeypatch.setenv("AGENT_CONTRACT_RUN_ID", "../escape")
    monkeypatch.setenv("AGENT_CONTRACT_COMMIT", COMMIT)
    monkeypatch.setenv("AGENT_CONTRACT_EVIDENCE_DIR", str(tmp_path / "staging"))
    rt.reset_foundation_runtime()

    runtime = rt.get_foundation_runtime()

    assert runtime.run_id != "../escape"
