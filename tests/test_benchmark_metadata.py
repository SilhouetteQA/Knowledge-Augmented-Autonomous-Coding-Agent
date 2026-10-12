"""T3 元信息测试：成本来源、报告渲染、锁定文件、真实执行器记录。"""
import hashlib
import json
from pathlib import Path

import pytest

import benchmark.runner as runner_mod
import tools.docker_sandbox as sandbox_mod
from benchmark.loader import BenchmarkCase, CaseError, load_cases
from benchmark.report import (BenchmarkReport, CaseResult, RunMetadata,
                              compare_reports, current_metadata,
                              enrich_run_metadata, judge_prompt_sha256,
                              save_json, save_markdown)
from tools.shell_tools import LocalExecutor


# --- 1) 成本：单价表为空 → cost_usd=None + cost_source="unknown" ---


def test_default_price_table_is_empty():
    """现状：仓库单价表为空 → 任何模型成本都是 unknown（而不是 0.0）。"""
    assert runner_mod.MODEL_PRICE_USD_PER_1K == {}
    assert runner_mod.cost_for("whatever", 10, 10) == (None, "unknown")


def test_cost_unknown_when_price_table_empty(monkeypatch):
    monkeypatch.setattr(runner_mod, "MODEL_PRICE_USD_PER_1K", {})
    assert runner_mod.cost_for("mimo-v2.5", 1000, 500) == (None, "unknown")
    assert runner_mod._cost_usd("mimo-v2.5", 1000, 500) is None


def test_cost_computed_when_price_known(monkeypatch):
    monkeypatch.setattr(runner_mod, "MODEL_PRICE_USD_PER_1K", {"mimo-v2.5": 0.004})
    cost, source = runner_mod.cost_for("mimo-v2.5", 1000, 0)
    assert source == "computed"
    assert cost == pytest.approx(0.004)


def test_case_result_defaults_to_unknown_source():
    """CaseResult 缺省 cost_source=unknown（旧 report.json 反序列化不会假装已知）。"""
    r = _case_result("c1", None)
    assert r.cost_source == "unknown"


# --- 2) 报告渲染容忍 None 成本 ---


def _meta(run_id="t3-run"):
    return RunMetadata(run_id=run_id, git_commit="abc", branch="b",
                       model="m", timestamp="2026-01-01T00:00:00",
                       executor="docker", params={})


def _case_result(case_id, cost, source="unknown"):
    return CaseResult(
        case_id=case_id, category="bug", status="not_resolved",
        resolution=False, test_pass=False, patch_acceptance=False,
        judge_verdict="SKIP", judge_reason="SKIP", tool_success_rate=0.0,
        iteration_count=0, latency_s=1.0, tokens_prompt=0, tokens_completion=0,
        cost_usd=cost, cost_source=source, diff="", errors=[])


def test_report_renders_unknown_cost(tmp_path):
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                             resolution_rate=0.0,
                             results=[_case_result("c1", None)])
    text = Path(save_markdown(report, str(tmp_path))).read_text(encoding="utf-8")
    assert "unknown" in text
    data = json.loads(Path(save_json(report, str(tmp_path))).read_text(encoding="utf-8"))
    assert data["results"][0]["cost_usd"] is None
    assert data["results"][0]["cost_source"] == "unknown"


def test_report_total_cost_handles_none(tmp_path):
    """混合成本：已知值照常求和并标注部分 unknown；全 None → unknown；均不崩。"""
    mixed = BenchmarkReport(metadata=_meta(), total=2, resolved=0,
                            resolution_rate=0.0,
                            results=[_case_result("c1", 0.5, "computed"),
                                     _case_result("c2", None)])
    text = Path(save_markdown(mixed, str(tmp_path))).read_text(encoding="utf-8")
    assert "0.5000" in text and "unknown" in text
    out = compare_reports([mixed])
    assert "0.5000" in out and "unknown" in out
    assert "unknown" in compare_reports([
        BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                        resolution_rate=0.0, results=[_case_result("c1", None)])])


def test_unknown_cost_survives_json_roundtrip(tmp_path):
    """--compare 路径（RunMetadata/CaseResult 反序列化）容忍 None 成本与缺失新键。"""
    report = BenchmarkReport(metadata=_meta(), total=1, resolved=0,
                             resolution_rate=0.0,
                             results=[_case_result("c1", None)])
    data = json.loads(Path(save_json(report, str(tmp_path))).read_text(encoding="utf-8"))
    meta = RunMetadata(**data["metadata"])
    assert meta.resolution_definition_version == "t3-1"
    assert meta.side_effect_policy == "read_only"
    r = CaseResult(**data["results"][0])
    assert r.cost_usd is None and r.cost_source == "unknown"
    # 旧报告缺新键 → 默认值兜底（向后兼容）
    legacy = {"run_id": "old", "git_commit": "c", "branch": "b", "model": "m",
              "timestamp": "t", "executor": "local", "params": {}}
    old = RunMetadata(**legacy)
    assert old.executor_actual == "" and old.sandbox_policy == {}
    assert old.iteration_limits == {} and old.timeouts == {}
    assert old.judge_prompt_sha256 == ""


# --- 3) T3 元信息采集 ---


def test_enrich_run_metadata_records_t3_fields():
    llm = type("L", (), {"base_url": "http://x", "model": "m"})()
    cases = [type("C", (), {"id": "c1", "max_iterations": 30})()]
    meta = enrich_run_metadata(current_metadata("m", "docker"), llm=llm, cases=cases)
    assert meta.base_url == "http://x"
    assert meta.resolution_definition_version == "t3-1"
    assert meta.side_effect_policy == "read_only"
    assert meta.iteration_limits["per_case"] == {"c1": 30}
    assert meta.iteration_limits["fallback_to_run_level"] == []
    assert set(meta.timeouts) == {"KA_SETUP_TIMEOUT_S", "KA_LLM_TIMEOUT_S",
                                  "KA_LLM_MAX_RETRIES"}
    assert meta.judge_prompt_sha256 == judge_prompt_sha256()
    assert len(meta.judge_prompt_sha256) == 64


def test_iteration_limits_fallback_to_run_level():
    from benchmark.report import effective_iteration_limits
    out = effective_iteration_limits([type("C", (), {"id": "c1"})()], run_level=7)
    assert out["per_case"] == {"c1": 7}
    assert out["fallback_to_run_level"] == ["c1"]


def test_timeouts_reflect_env(monkeypatch):
    from benchmark.report import effective_timeouts
    monkeypatch.setenv("KA_SETUP_TIMEOUT_S", "45")
    monkeypatch.setenv("KA_LLM_TIMEOUT_S", "33.5")
    monkeypatch.setenv("KA_LLM_MAX_RETRIES", "5")
    assert effective_timeouts() == {"KA_SETUP_TIMEOUT_S": 45,
                                    "KA_LLM_TIMEOUT_S": 33.5,
                                    "KA_LLM_MAX_RETRIES": 5}


def test_judge_prompt_sha256_matches_judge_module():
    from benchmark.judge import JUDGE_PROMPT
    assert judge_prompt_sha256() == hashlib.sha256(
        JUDGE_PROMPT.encode("utf-8")).hexdigest()


# --- 4) 锁定文件：可选 + 存在时钉住提交 ---


def _write_case(cases_dir, category, case_id, **overrides):
    data = {
        "id": case_id, "category": category, "repository": "dbader/schedule",
        "issue": {"number": 646, "title": "t", "body": "b", "labels": [],
                  "state": "open"},
        "gold_patch": "gold/%s.diff" % case_id, "must_pass": ["test_schedule.py"],
        "max_iterations": 30,
    }
    data.update(overrides)
    p = cases_dir / category / (case_id + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.parent.parent / "gold").mkdir(parents=True, exist_ok=True)
    (p.parent.parent / "gold" / ("%s.diff" % case_id)).write_text(
        "+guard", encoding="utf-8")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _cases_dir(tmp_path):
    """cases_dir = <tmp>/benchmark/cases → 默认锁定路径落在 <tmp>/benchmark 内。"""
    return tmp_path / "benchmark" / "cases"


def _no_lock_env(monkeypatch):
    monkeypatch.delenv("KA_BENCHMARK_CASES_LOCK", raising=False)
    monkeypatch.delenv("KA_BENCHMARK_FROZEN_VERSION", raising=False)


def test_lock_file_is_optional(tmp_path, monkeypatch):
    """无锁定文件：加载成功且不钉提交（向后兼容）。"""
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646")
    cases = load_cases(str(cases_dir))
    assert len(cases) == 1
    assert cases[0].repository_commit == ""


def test_default_lock_file_pins_commit(tmp_path, monkeypatch):
    """默认路径 frozen/<version>/cases.lock.json 存在 → 钉住锁定值。"""
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646")
    lock_dir = tmp_path / "benchmark" / "frozen" / "v1"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "cases.lock.json").write_text(
        json.dumps({"schedule-646": "deadbeef" * 5}), encoding="utf-8")
    assert load_cases(str(cases_dir))[0].repository_commit == "deadbeef" * 5
    # 版本切换：KA_BENCHMARK_FROZEN_VERSION 指向另一版本目录
    other = tmp_path / "benchmark" / "frozen" / "v2"
    other.mkdir(parents=True, exist_ok=True)
    (other / "cases.lock.json").write_text(
        json.dumps({"schedule-646": "v2commit"}), encoding="utf-8")
    monkeypatch.setenv("KA_BENCHMARK_FROZEN_VERSION", "v2")
    assert load_cases(str(cases_dir))[0].repository_commit == "v2commit"


def test_inline_repository_commit_exposed(tmp_path, monkeypatch):
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646", repository_commit="cafe1234")
    assert load_cases(str(cases_dir))[0].repository_commit == "cafe1234"


def test_lock_env_path_overrides_inline(tmp_path, monkeypatch):
    """KA_BENCHMARK_CASES_LOCK 指定路径；锁定值优先于 case 内联值。"""
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646", repository_commit="inline")
    lock = tmp_path / "custom.lock.json"
    lock.write_text(json.dumps({"cases": {"schedule-646": "pinned"}}),
                    encoding="utf-8")
    monkeypatch.setenv("KA_BENCHMARK_CASES_LOCK", str(lock))
    assert load_cases(str(cases_dir))[0].repository_commit == "pinned"


def test_explicit_lock_path_argument(tmp_path, monkeypatch):
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646")
    lock = tmp_path / "l.json"
    lock.write_text(json.dumps({"schedule-646": "abc123"}), encoding="utf-8")
    assert load_cases(str(cases_dir), lock_path=str(lock))[0].repository_commit \
        == "abc123"


def test_broken_lock_file_raises(tmp_path, monkeypatch):
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646")
    lock_dir = tmp_path / "benchmark" / "frozen" / "v1"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "cases.lock.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CaseError, match="锁定文件"):
        load_cases(str(cases_dir))


def test_invalid_repository_commit_type_raises(tmp_path, monkeypatch):
    _no_lock_env(monkeypatch)
    cases_dir = _cases_dir(tmp_path)
    _write_case(cases_dir, "bug", "schedule-646", repository_commit=123)
    with pytest.raises(CaseError, match="repository_commit"):
        load_cases(str(cases_dir))


# --- 5) 真实执行器记录 / 沙箱策略 ---


def test_actual_executor_local_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    sandbox_mod.reset_actual_executor()
    try:
        assert sandbox_mod.get_actual_executor() == ""
        with sandbox_mod.sandbox_executor(str(tmp_path)) as ex:
            assert isinstance(ex, LocalExecutor)
        assert sandbox_mod.get_actual_executor() == "local"
    finally:
        sandbox_mod.reset_actual_executor()


def test_report_executor_actual_reflects_local(tmp_path, monkeypatch):
    """声明 docker 但真实跑的是 local → executor_actual 记 local（T3 核心诉求）。"""
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    sandbox_mod.reset_actual_executor()
    try:
        with sandbox_mod.sandbox_executor(str(tmp_path)):
            meta = enrich_run_metadata(current_metadata("m", "docker"))
        assert meta.executor_actual == "local"
    finally:
        sandbox_mod.reset_actual_executor()


def test_effective_sandbox_policy_exposes_limits(monkeypatch):
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    monkeypatch.setenv("KA_SANDBOX_CPUS", "4")
    monkeypatch.setenv("KA_SANDBOX_MEMORY", "2g")
    monkeypatch.setenv("KA_SANDBOX_PIDS", "256")
    policy = sandbox_mod.get_effective_sandbox_policy()
    assert policy == {"executor": "local", "image": sandbox_mod.DEFAULT_IMAGE,
                      "cpus": 4.0, "memory": "2g", "pids_limit": 256,
                      "network": False}


def test_report_carries_sandbox_policy():
    meta = enrich_run_metadata(current_metadata("m", "local"))
    assert meta.sandbox_policy.get("image") == sandbox_mod.DEFAULT_IMAGE
    assert meta.sandbox_policy.get("network") is False


# --- 6) 端到端：runner 产出的 CaseResult 携带 unknown 成本来源 ---


def test_runner_emits_unknown_cost_source(tmp_path, monkeypatch):
    from agent.llm import LLMMessage, MockLLMClient
    from tools.github_tools import GitHubIssue
    from tools.shell_tools import TestResult

    (tmp_path / "gold").mkdir(parents=True, exist_ok=True)
    (tmp_path / "gold" / "c.diff").write_text("+g", encoding="utf-8")
    case = BenchmarkCase(
        id="c", category="bug", repository="dbader/schedule",
        issue=GitHubIssue(number=1, title="t", body="b", labels=[], state="open"),
        gold_patch="gold/c.diff", must_pass=["t.py"], max_iterations=30)
    monkeypatch.setattr(runner_mod, "_ensure_repository", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "run_issue_agent",
                        lambda task, llm, **kw: type("R", (), {
                            "diff": "+x", "iteration_count": 1, "steps": []})())
    monkeypatch.setattr(runner_mod, "run_tests",
                        lambda path, workspace_root: TestResult(1, 0, 0, 1, 0.1, []))
    monkeypatch.setattr(runner_mod, "MODEL_PRICE_USD_PER_1K", {})
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASS 一致。")])
    report = runner_mod.run_benchmark_cases(
        llm, [case], str(tmp_path), "", "local", tmp_path)
    r = report.results[0]
    assert r.cost_usd is None
    assert r.cost_source == "unknown"
    assert report.metadata.executor_actual == "local"
    assert report.metadata.resolution_definition_version == "t3-1"
