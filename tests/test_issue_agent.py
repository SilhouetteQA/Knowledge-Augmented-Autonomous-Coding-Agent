"""GitHub Issue Agent 编排测试：Mock LLM + Fake GitHub（不触网、不真推）。"""
import json
import os

import pytest

import agent.issue as issue_mod
from agent.issue import IssueTask, run_issue_agent
from agent.llm import LLMMessage, MockLLMClient
from tools.file_tools import ToolError
from tools.github_tools import GitHubIssue, GitHubRepo


def _fake_issue():
    return GitHubIssue(number=123, title="修复重复创建实体",
                       body="同一角色在不同章节被识别为不同 Entity。",
                       labels=["bug"], state="open")


def _fake_repo():
    return GitHubRepo(full_name="test/arc-wiki",
                      clone_url="https://github.com/test/arc-wiki.git",
                      default_branch="main", language="Python")


def _patch_github(monkeypatch):
    """替换 issue 模块的 GitHub 访问（不触网、不写磁盘仓库）。"""
    monkeypatch.setattr(issue_mod, "get_issue", lambda repo, n: _fake_issue())
    monkeypatch.setattr(issue_mod, "get_repository", lambda repo: _fake_repo())
    monkeypatch.setattr(issue_mod, "clone_repository", lambda d, r: None)
    monkeypatch.setattr(issue_mod, "create_branch", lambda d, b, base: None)
    monkeypatch.setattr(issue_mod, "git_diff_since",
                        lambda d, base: "+fixed\n-fixed\n")


def _graph_script():
    """run_agent_graph 的 plan + decide 两条 Mock 消息。"""
    return [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
    ]


def _make_workdir(tmp_path) -> str:
    """构造克隆目录（test__arc-wiki）并放入通过测试，保证图内 verify 通过。"""
    workdir = tmp_path / "test__arc-wiki"
    workdir.mkdir(parents=True)
    (workdir / "test_ok.py").write_text(
        "def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    return str(tmp_path)


def test_dry_run_full_pipeline(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None,
                        raising=False)   # 3a 已移除该属性：run 内不再调用，仅断言零提交
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决了问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.issue.number == 123
    assert result.branch == "fix/issue-123"
    assert result.review.startswith("PASS")
    assert result.pr_url is None
    assert result.approval_path is None      # 未配 approval_dir 不产单（评估兼容）
    assert commits == []
    assert result.retry_count == 0           # 无 Review FAIL 重试


def test_issue_read_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(issue_mod, "get_issue",
                        lambda repo, n: ToolError("gh 未认证"))
    task = IssueTask(repository="test/arc-wiki", issue_number=1,
                     workspace_root=_make_workdir(tmp_path))
    with pytest.raises(ToolError, match="读取 Issue 失败"):
        run_issue_agent(task, MockLLMClient([]))


def test_dry_run_no_changes_review_fail(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    monkeypatch.setattr(issue_mod, "git_diff_since", lambda d, base: "")
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS ok"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.review == "FAIL 无代码变更"
    assert result.pr_url is None


def _patch_push_calls(monkeypatch):
    """打桩 commit/push/PR（真执行会触网或落在非 git 目录）。

    raising=False：run 内已不再调用这些函数（3a 移除 import），打桩仅用于
    记录调用并断言"零调用"（推送唯一入口为 --approve）。
    """
    calls = {"commit": [], "push": [], "pr": []}
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: calls["commit"].append(m) or None,
                        raising=False)
    monkeypatch.setattr(issue_mod, "push_branch",
                        lambda d, b: calls["push"].append(b) or None,
                        raising=False)
    monkeypatch.setattr(
        issue_mod, "create_pull_request",
        lambda repo, head, base, title, body:
            calls["pr"].append((head, base, title)) or
            "https://github.com/test/arc-wiki/pull/999",
        raising=False)
    return calls


def test_review_fail_triggers_retry(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    calls = _patch_push_calls(monkeypatch)
    script = [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
        LLMMessage(role="assistant", content="FAIL 缺少边界测试"),
        LLMMessage(role="assistant", content=json.dumps(["补测试"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已补测试"),
        LLMMessage(role="assistant", content="PASS 测试已补齐。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.review.startswith("PASS")
    # 新语义：run 内永不推送，重试通过亦无 commit/push/PR（推送唯一入口为 --approve）
    assert calls["commit"] == [] and calls["push"] == [] and calls["pr"] == []


def test_issue_creates_approval_request(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    calls = _patch_push_calls(monkeypatch)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path),
                     approval_dir=str(tmp_path / "approvals"))
    result = run_issue_agent(task, MockLLMClient(script))
    # 干跑产单：审批文件存在且内容完整
    path = result.approval_path
    assert path is not None and os.path.isfile(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["status"] == "pending"
    assert data["action_type"] == "pr_push"
    assert data["branch"] == "fix/issue-123"
    assert data["base_branch"] == "main"
    assert data["diff"] == "+fixed\n-fixed\n"
    assert data["review"].startswith("PASS")
    assert data["verify_rounds"] == result.verify_rounds
    assert data["retry_count"] == 0
    # 无远端副作用：run 内不执行 commit/push/PR
    assert calls["commit"] == [] and calls["push"] == [] and calls["pr"] == []


def test_issue_snapshot_skips_get_issue(tmp_path, monkeypatch):
    """issue_snapshot 非 None 时跳过在线 get_issue（离线可重跑）。"""
    _patch_github(monkeypatch)
    called = []
    monkeypatch.setattr(issue_mod, "get_issue",
                        lambda repo, n: called.append(n) or _fake_issue())
    snap = GitHubIssue(number=646, title="[CRASH] guard self.unit against None",
                       body="crash when unit is None", labels=[], state="open")
    task = IssueTask(repository="test/arc-wiki", issue_number=646,
                     workspace_root=_make_workdir(tmp_path),
                     issue_snapshot=snap)
    result = run_issue_agent(task, MockLLMClient(_graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决了问题。")]))
    assert called == []                      # get_issue 未被调用
    assert result.issue.number == 646        # 使用快照数据
    assert result.issue.title == "[CRASH] guard self.unit against None"


def test_review_fail_still_generates_approval_request(tmp_path, monkeypatch):
    """Review 重试后仍 FAIL：仍产审批单（人工是最终仲裁者，FAIL 结论供拒绝参考）。"""
    _patch_github(monkeypatch)
    calls = _patch_push_calls(monkeypatch)
    script = [
        LLMMessage(role="assistant", content=json.dumps(["修复"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="已修复"),
        LLMMessage(role="assistant", content="FAIL 有严重问题"),
        LLMMessage(role="assistant", content=json.dumps(["再修"], ensure_ascii=False)),
        LLMMessage(role="assistant", content="修好了"),
        LLMMessage(role="assistant", content="FAIL 仍有问题"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path),
                     approval_dir=str(tmp_path / "approvals"))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.pr_url is None
    assert calls["commit"] == [] and calls["push"] == [] and calls["pr"] == []
    data = json.load(open(result.approval_path, encoding="utf-8"))
    assert data["status"] == "pending"
    assert data["review"].startswith("FAIL")
    assert data["retry_count"] == 1


def test_review_diff_receives_verify_rounds(tmp_path, monkeypatch):
    """Reviewer 输入含验证轮数（独立角色输入 = Issue + diff + verify_rounds）。"""
    _patch_github(monkeypatch)
    seen = {}
    original = issue_mod._review_diff

    def spy(llm, diff, issue_text, verify_rounds):
        seen["verify_rounds"] = verify_rounds
        return original(llm, diff, issue_text, verify_rounds)

    monkeypatch.setattr(issue_mod, "_review_diff", spy)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    run_issue_agent(task, MockLLMClient(script))
    assert "verify_rounds" in seen


# --- W7 Task 3: issue.run span metadata（verify_rounds / retry_count） ---


def _probe_issue_module(monkeypatch, fake_traced):
    """以独立模块名重放 agent/issue.py 顶层，捕获模块级 traced 装饰注册。"""
    import importlib.util
    import pathlib
    import sys
    import tools.tracing as tracing
    monkeypatch.setattr(tracing, "traced", fake_traced)
    src = pathlib.Path(__file__).resolve().parent.parent / "agent" / "issue.py"
    spec = importlib.util.spec_from_file_location("agent.issue_probe", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)


def test_issue_run_traced_registered_with_metadata_fn(monkeypatch):
    """run_issue_agent 模块级注册 traced("issue.run") agent span，且 metadata_fn 已接线。"""
    registry = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        registry.append((name, as_type, metadata_fn))
        return lambda f: f

    _probe_issue_module(monkeypatch, fake_traced)
    assert any(n == "issue.run" and t == "agent" and m is not None
               for n, t, m in registry)


def test_issue_run_metadata_returns_verify_rounds_and_retry_count():
    """_issue_run_metadata 汇总 verify_rounds 与 retry_count 到 span metadata。"""
    from agent.issue import _issue_run_metadata

    class FakeResult:
        verify_rounds = 2
        retry_count = 1

    meta = _issue_run_metadata((), {}, FakeResult())
    assert meta == {"verify_rounds": 2, "retry_count": 1}
    # 结果对象缺字段时回退 0（旧对象直通不报错）
    assert _issue_run_metadata((), {}, object()) == {"verify_rounds": 0, "retry_count": 0}


def test_review_fail_retry_sets_retry_count_one(tmp_path, monkeypatch):
    """Review FAIL 触发重试后，retry_count 记为 1（即使重试后结论非 PASS）。"""
    _patch_github(monkeypatch)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="FAIL 缺少边界测试"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path))
    result = run_issue_agent(task, MockLLMClient(script))
    assert result.retry_count == 1        # 首轮 FAIL 即发生一次重试


# --- W7 Task 4: review 环节埋点（_review_diff generation span + 结论首行 metadata） ---


def test_review_diff_traced_registered_as_generation(monkeypatch):
    """_review_diff 模块级注册 traced("review") generation span，且 metadata_fn 已接线。"""
    registry = []

    def fake_traced(name=None, as_type="span", metadata_fn=None):
        registry.append((name, as_type, metadata_fn))
        return lambda f: f

    _probe_issue_module(monkeypatch, fake_traced)
    assert any(n == "review" and t == "generation" and m is not None
               for n, t, m in registry)


def test_review_metadata_records_first_line():
    """_review_metadata 记录结论首行（PASS/FAIL），空文本首行为空串。"""
    from agent.issue import _review_metadata

    class FakeResult:
        """模拟 review 返回文本（str(result) 即结论）。"""

        def __init__(self, text):
            self.text = text

        def __str__(self):
            return self.text

    assert _review_metadata((), {}, FakeResult("PASS 修复点一致。")) == \
        {"first_line": "PASS 修复点一致。", "has_manual_flag": False}
    assert _review_metadata((), {}, FakeResult("")) == \
        {"first_line": "", "has_manual_flag": False}
    # 首行截断 80 字符（metadata 限长）
    long_line = "PASS " + "x" * 100
    assert _review_metadata((), {}, long_line) == {
        "first_line": ("PASS " + "x" * 75)[:80], "has_manual_flag": False}


def test_review_metadata_records_manual_flag():
    """Review 结论含「人工关注」行时 metadata 标记 has_manual_flag=True。"""
    from agent.issue import _review_metadata

    class FakeResult:
        def __init__(self, text):
            self.text = text

        def __str__(self):
            return self.text

    assert _review_metadata((), {}, FakeResult(
        "PASS 修复一致。\n人工关注 沙箱缺 pytz 依赖。")) == {
        "first_line": "PASS 修复一致。", "has_manual_flag": True}
    assert _review_metadata((), {}, FakeResult("PASS ok")) == {
        "first_line": "PASS ok", "has_manual_flag": False}
