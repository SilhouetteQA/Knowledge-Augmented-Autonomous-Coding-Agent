"""T3b：冻结 fixture 提交的强制与记录（母 Spec §24 "repository commit/fixture state"）。

T3 只把 pin 记录进报告；T3b 让 runner 真的 checkout 钉住的提交，并把"钉住值 vs 实际检出值"
写进每个 case 结果与 run metadata。只记录不检出 = fixture 状态没有固定，基线不可复现。

本文件不联网：clone/sync/checkout/HEAD 全部 monkeypatch。
"""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from benchmark import runner
from benchmark.report import CaseResult, RunMetadata


class TestStampFixture:
    def test_records_pinned_and_actual(self):
        r = CaseResult(
            case_id="c1", category="bug", status="resolved", resolution=True,
            test_pass=True, patch_acceptance=True, judge_verdict="PASS",
            judge_reason="", tool_success_rate=1.0, iteration_count=1,
            latency_s=1.0, tokens_prompt=1, tokens_completion=1,
            cost_usd=None, diff="",
        )
        out = runner._stamp_fixture(r, "a" * 40, "a" * 40)
        assert out.repository_commit_pinned == "a" * 40
        assert out.repository_commit_actual == "a" * 40
        assert out.errors == []

    def test_mismatch_is_recorded_not_silent(self):
        r = CaseResult(
            case_id="c1", category="bug", status="resolved", resolution=True,
            test_pass=True, patch_acceptance=True, judge_verdict="PASS",
            judge_reason="", tool_success_rate=1.0, iteration_count=1,
            latency_s=1.0, tokens_prompt=1, tokens_completion=1,
            cost_usd=None, diff="",
        )
        runner._stamp_fixture(r, "a" * 40, "b" * 40)
        assert any("fixture 提交不匹配" in e for e in r.errors)

    def test_empty_pin_is_not_a_mismatch(self):
        r = CaseResult(
            case_id="c1", category="bug", status="resolved", resolution=True,
            test_pass=True, patch_acceptance=True, judge_verdict="PASS",
            judge_reason="", tool_success_rate=1.0, iteration_count=1,
            latency_s=1.0, tokens_prompt=1, tokens_completion=1,
            cost_usd=None, diff="",
        )
        runner._stamp_fixture(r, "", "c" * 40)
        assert r.errors == []
        assert r.repository_commit_actual == "c" * 40


class TestEnsureRepositoryPinsCommit:
    """钉住时必须 checkout 钉住的提交，且返回实际 HEAD。"""

    def _patch_env(self, monkeypatch, checkout_calls, head="f" * 40):
        monkeypatch.setattr(runner, "get_repository",
                            lambda repo: types.SimpleNamespace(default_branch="master"))
        monkeypatch.setattr(runner, "clone_repository", lambda repo_dir, repo: None)
        monkeypatch.setattr(runner, "sync_repository", lambda repo_dir, base: None)
        monkeypatch.setattr(runner.os.path, "isdir", lambda p: False)
        monkeypatch.setattr(runner.os, "makedirs", lambda *a, **k: None)

        def fake_checkout(repo_dir, ref):
            checkout_calls.append(ref)
            return None

        monkeypatch.setattr(runner, "_git_checkout", fake_checkout)
        monkeypatch.setattr(runner, "_git_head", lambda repo_dir: head)

    def test_pinned_commit_is_checked_out(self, monkeypatch):
        calls: list[str] = []
        self._patch_env(monkeypatch, calls)
        head = runner._ensure_repository("/tmp/ws", "/tmp/ws/repo", "dbader/schedule", "d" * 40)
        assert calls == ["d" * 40], "必须检出钉住的提交，而不是默认分支"
        assert head == "f" * 40, "应返回实际检出的 HEAD"

    def test_without_pin_falls_back_to_base_branch(self, monkeypatch):
        calls: list[str] = []
        self._patch_env(monkeypatch, calls)
        runner._ensure_repository("/tmp/ws", "/tmp/ws/repo", "dbader/schedule")
        assert calls == ["master"], "未钉住时行为必须与改造前一致（检出默认分支）"

    def test_existing_repo_syncs_then_checks_out_pin(self, monkeypatch):
        calls: list[str] = []
        synced: list[str] = []
        self._patch_env(monkeypatch, calls)
        monkeypatch.setattr(runner.os.path, "isdir", lambda p: True)
        monkeypatch.setattr(runner, "sync_repository",
                            lambda repo_dir, base: synced.append(base) or None)
        runner._ensure_repository("/tmp/ws", "/tmp/ws/repo", "dbader/schedule", "e" * 40)
        assert synced == ["master"]
        assert calls == ["e" * 40]

    def test_checkout_failure_raises(self, monkeypatch):
        from tools.github_tools import ToolError

        calls: list[str] = []
        self._patch_env(monkeypatch, calls)
        monkeypatch.setattr(runner, "_git_checkout", lambda repo_dir, ref: "boom")
        with pytest.raises(ToolError):
            runner._ensure_repository("/tmp/ws", "/tmp/ws/repo", "dbader/schedule", "d" * 40)


class TestFixtureCommitsInMetadata:
    def test_metadata_carries_pinned_and_actual(self, monkeypatch):
        def fake_run_one_case(case, llm, case_dir, repo_root):
            r = CaseResult(
                case_id=case.id, category="bug", status="resolved", resolution=True,
                test_pass=True, patch_acceptance=True, judge_verdict="PASS",
                judge_reason="", tool_success_rate=1.0, iteration_count=1,
                latency_s=1.0, tokens_prompt=1, tokens_completion=1,
                cost_usd=None, diff="",
            )
            return runner._stamp_fixture(r, "a" * 40, "a" * 40)

        monkeypatch.setattr(runner, "_run_one_case", fake_run_one_case)
        monkeypatch.setattr(runner, "enrich_run_metadata",
                            lambda meta, **kw: meta)
        monkeypatch.setattr(runner, "current_metadata",
                            lambda model, executor, **p: RunMetadata(
                                run_id="t", git_commit="c", branch="b", model=model,
                                timestamp="t", executor=executor, params={}))

        cases = [types.SimpleNamespace(id="c1", category="bug")]
        report = runner.run_benchmark_cases(
            types.SimpleNamespace(model="m"), cases, "", "", "local", "")
        assert report.metadata.fixture_commits == {
            "c1": {"pinned": "a" * 40, "actual": "a" * 40}
        }


class TestLockPathDiscovery:
    """T3b：冻结版本目录的唯一候选必须被发现（此前会被静默忽略）。"""

    def _make(self, tmp_path, versions, monkeypatch):
        cases = tmp_path / "benchmark" / "cases"
        cases.mkdir(parents=True)
        for v in versions:
            d = tmp_path / "benchmark" / "frozen" / v
            d.mkdir(parents=True)
            (d / "cases.lock.json").write_text('{"c1": "a"}', encoding="utf-8")
        monkeypatch.delenv("KA_BENCHMARK_FROZEN_VERSION", raising=False)
        monkeypatch.delenv("KA_BENCHMARK_CASES_LOCK", raising=False)
        return str(cases)

    def test_unique_candidate_is_discovered(self, tmp_path, monkeypatch):
        from benchmark.loader import default_lock_path

        cases = self._make(tmp_path, ["w11a-v1"], monkeypatch)
        got = default_lock_path(cases)
        assert got.endswith(str(Path("frozen") / "w11a-v1" / "cases.lock.json")), got

    def test_multiple_candidates_raise_instead_of_guessing(self, tmp_path, monkeypatch):
        from benchmark.loader import CaseError, default_lock_path

        cases = self._make(tmp_path, ["w11a-v1", "v2"], monkeypatch)
        with pytest.raises(CaseError):
            default_lock_path(cases)

    def test_explicit_version_wins(self, tmp_path, monkeypatch):
        from benchmark.loader import default_lock_path

        cases = self._make(tmp_path, ["w11a-v1", "v2"], monkeypatch)
        got = default_lock_path(cases, version="v2")
        assert got.endswith(str(Path("frozen") / "v2" / "cases.lock.json")), got

    def test_exact_default_wins_over_discovery(self, tmp_path, monkeypatch):
        from benchmark.loader import default_lock_path

        cases = self._make(tmp_path, ["v1", "w11a-v1"], monkeypatch)
        got = default_lock_path(cases)
        assert got.endswith(str(Path("frozen") / "v1" / "cases.lock.json")), got

    def test_no_candidates_keeps_legacy_default(self, tmp_path, monkeypatch):
        from benchmark.loader import default_lock_path

        cases = self._make(tmp_path, [], monkeypatch)
        got = default_lock_path(cases)
        assert got.endswith(str(Path("frozen") / "v1" / "cases.lock.json")), got

    def test_lock_is_actually_applied_to_cases(self, tmp_path, monkeypatch):
        """端到端：frozen/<ver>/cases.lock.json 存在时，load_cases 必须把提交钉上。"""
        from benchmark.loader import load_cases

        cases_dir = tmp_path / "benchmark" / "cases"
        (cases_dir / "bug").mkdir(parents=True)
        gold_dir = cases_dir / "gold"
        gold_dir.mkdir(parents=True)
        (gold_dir / "c1.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
        (cases_dir / "bug" / "c1.json").write_text(json.dumps({
            "id": "c1", "category": "bug", "repository": "dbader/schedule",
            "issue": {"number": 1, "title": "t", "body": "b", "labels": [], "state": "open"},
            "gold_patch": "gold/c1.diff", "must_pass": ["pytest"], "max_iterations": 5,
        }), encoding="utf-8")
        d = tmp_path / "benchmark" / "frozen" / "w11a-v1"
        d.mkdir(parents=True)
        (d / "cases.lock.json").write_text(json.dumps({"c1": "b" * 40}), encoding="utf-8")
        monkeypatch.delenv("KA_BENCHMARK_FROZEN_VERSION", raising=False)
        monkeypatch.delenv("KA_BENCHMARK_CASES_LOCK", raising=False)

        loaded = load_cases(str(cases_dir))
        assert [c.repository_commit for c in loaded] == ["b" * 40]


def test_git_head_on_real_repo_matches_git():
    """_git_head 在真实仓库上必须与 git rev-parse HEAD 一致。"""
    import subprocess

    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("当前目录不是 git 仓库")
    assert runner._git_head(os.getcwd()) == proc.stdout.strip()


def test_git_head_on_non_repo_returns_empty():
    import tempfile

    assert runner._git_head(tempfile.gettempdir()) == ""
