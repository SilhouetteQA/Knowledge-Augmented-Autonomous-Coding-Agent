"""LLM-as-a-Judge 判定测试。"""
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