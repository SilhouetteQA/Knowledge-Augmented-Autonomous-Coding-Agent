"""LLM-as-a-Judge 判定测试。"""
import pytest

from agent.llm import LLMMessage, MockLLMClient
from benchmark.judge import JudgeResult, judge_patch
from tools.github_tools import GitHubIssue


def _issue():
    return GitHubIssue(number=646, title="guard self.unit against None",
                       body="crash when unit is None", labels=[], state="open")


def test_judge_pass():
    llm = MockLLMClient([LLMMessage(role="assistant",
                                    content="PASS 修复点一致。")])
    r = judge_patch(llm, "+guard", "+guard", _issue())
    assert r.verdict == "PASS"
    assert r.reason.startswith("PASS")


def test_judge_fail():
    llm = MockLLMClient([LLMMessage(role="assistant",
                                    content="FAIL 未覆盖崩溃点。")])
    r = judge_patch(llm, "+x", "+guard", _issue())
    assert r.verdict == "FAIL"


def test_judge_skip_when_diff_empty():
    llm = MockLLMClient([])
    r = judge_patch(llm, "", "+guard", _issue())
    assert r.verdict == "SKIP"
    assert "无代码变更" in r.reason


def test_judge_nonstandard_output_treated_fail():
    llm = MockLLMClient([LLMMessage(role="assistant", content="不确定")])
    r = judge_patch(llm, "+a", "+b", _issue())
    assert r.verdict == "FAIL"


@pytest.mark.parametrize("text", [
    "PASS：修复点一致",
    "PASS: ok",
    "PASS。",
    "PASS，一致",
    "PASS,一致",
    "PASS；",
    "PASS;",
    "PASS",
])
def test_judge_pass_with_punctuation_separators(text):
    """PASS 后常见标点分隔（半角/全角冒号、逗号、句号、分号）不误判为 FAIL。"""
    llm = MockLLMClient([LLMMessage(role="assistant", content=text)])
    r = judge_patch(llm, "+a", "+b", _issue())
    assert r.verdict == "PASS", text


def test_judge_pass_requires_word_boundary():
    """'PASSED' 等前缀不误判为 PASS（词边界匹配）。"""
    llm = MockLLMClient([LLMMessage(role="assistant", content="PASSED 但语义不同")])
    r = judge_patch(llm, "+a", "+b", _issue())
    assert r.verdict == "FAIL"


def test_judge_empty_gold_skips():
    """gold patch 为空 → SKIP。"""
    llm = MockLLMClient([])
    r = judge_patch(llm, "+a", "", _issue())
    assert r.verdict == "SKIP"
    assert "gold patch 为空" in r.reason