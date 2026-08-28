"""GitHub Issue Agent 编排测试：Mock LLM + Fake GitHub（不触网、不真推）。"""
import json
import os
import subprocess

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
    # A4 红线拦截：干跑 workdir 非 git 仓库，默认无红线（红线语义由专门集成测试覆盖）
    monkeypatch.setattr(issue_mod, "_enforce_red_lines", lambda d, b: [],
                        raising=False)


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

    def spy(llm, diff, issue_text, verify_rounds, context=None):
        seen["verify_rounds"] = verify_rounds
        return original(llm, diff, issue_text, verify_rounds, context)

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


def test_review_prompt_contains_domain_audit_point():
    """REVIEW_PROMPT 含域审查要点：删除条目三条件证据可核、元数据/日志无关改动指出。"""
    from agent.issue import REVIEW_PROMPT
    assert "三条件" in REVIEW_PROMPT
    assert "来源锚点" in REVIEW_PROMPT
    assert "元数据" in REVIEW_PROMPT


# --- A3: Reviewer 证据面扩展（审查上下文携带工作树事实） ---


def _git(cwd: str, *args: str) -> str:
    """测试内真实 git 调用；失败即断言错误（前置准备失败 = 测试环境问题）。"""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"git {' '.join(args)} 失败: {proc.stderr}"
    return proc.stdout.strip()


def _make_worktree_repo(tmp_path) -> str:
    """构造真实 git 仓库：基准提交后制造 修改+删除+新增 混合工作树状态。

    基准含 config/identity_map.json（后修改）、config/archived.json（后删除）、
    data/report.txt（报告类探针存在）；data/entity_source_map.json 从不创建
    （探针缺失面）；data/new.txt 为未跟踪新增文件。
    """
    repo = tmp_path / "worktree-repo"
    repo.mkdir()
    (repo / "config").mkdir()
    (repo / "data").mkdir()
    (repo / "config" / "identity_map.json").write_text("{}", encoding="utf-8")
    (repo / "config" / "archived.json").write_text("{}", encoding="utf-8")
    (repo / "data" / "report.txt").write_text("report", encoding="utf-8")
    _git(str(repo), "init")
    _git(str(repo), "symbolic-ref", "HEAD", "refs/heads/main")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", "base")
    # 工作树变更：修改 identity_map、删除 archived、新增未跟踪 new.txt
    (repo / "config" / "identity_map.json").write_text(
        '{"updated": true}', encoding="utf-8")
    (repo / "config" / "archived.json").unlink()
    (repo / "data" / "new.txt").write_text("new", encoding="utf-8")
    return str(repo)


def test_review_context_reports_worktree_facts(tmp_path):
    """_review_context 输出含四类事实：变更清单/删除文件/新增文件/关键路径存在性。"""
    out = issue_mod._review_context(_make_worktree_repo(tmp_path), "main")
    # ① 变更清单（status porcelain 原样行）
    assert "变更清单" in out
    assert " M config/identity_map.json" in out
    # ② 删除文件（diff --name-status 相对 base）
    assert "删除文件" in out
    assert "config/archived.json" in out
    # ④ 新增文件（status 未跟踪 ?? 项）
    assert "新增文件" in out
    assert "data/new.txt" in out
    # ③ 关键路径存在性：identity_map 存在 / entity_source_map 缺失 / 报告类存在
    assert "关键路径存在性" in out
    assert "config/identity_map.json: 存在" in out
    assert "data/entity_source_map.json: 缺失" in out
    assert "data/*.txt: 存在" in out


def test_review_context_degrades_for_non_git_dir(tmp_path):
    """非 git 目录：降级为简短提示而不是抛错（审查输入缺事实时明确告知）。"""
    out = issue_mod._review_context(str(tmp_path / "not-a-repo"), "main")
    assert out.startswith("（工作树事实不可用")
    # 存在但自身非 git 工作树（无 .git）：不能放任 git 向上回溯命中外围仓库，
    # 否则会把外层仓库的事实误报成 repo_dir 的事实。
    plain_dir = tmp_path / "plain-dir"
    plain_dir.mkdir()
    out3 = issue_mod._review_context(str(plain_dir), "main")
    assert out3 == "（工作树事实不可用: 目录不是 git 工作树）"
    # 不存在目录同样降级
    out2 = issue_mod._review_context(str(tmp_path / "no-such-dir"), "main")
    assert out2.startswith("（工作树事实不可用")


def test_review_diff_injects_worktree_context():
    """传入 context 时 user 消息含「工作树事实」段，位于 Issue/验证轮数之后、Diff 之前。"""
    mock = MockLLMClient([LLMMessage(role="assistant", content="PASS ok")])
    issue_mod._review_diff(mock, diff="+a\n-b\n", issue_text="issue-text",
                           verify_rounds=1, context="变更清单:  x\n删除文件:  y")
    user = mock.calls[0][0][1]["content"]
    assert "工作树事实" in user
    assert "变更清单:  x" in user
    assert user.index("Issue:") < user.index("工作树事实") < user.index("Diff:")


def test_review_diff_without_context_omits_section():
    """不传 context（向后兼容）：user 消息不含「工作树事实」段。"""
    mock = MockLLMClient([LLMMessage(role="assistant", content="PASS ok")])
    issue_mod._review_diff(mock, diff="+a\n-b\n", issue_text="issue-text",
                           verify_rounds=1)
    user = mock.calls[0][0][1]["content"]
    assert "工作树事实" not in user


def test_run_issue_agent_passes_review_context_both_rounds(tmp_path, monkeypatch):
    """首轮与重试轮的 _review_diff 都收到工作树上下文（临时目录非 git → 降级提示）。"""
    _patch_github(monkeypatch)
    seen = {"contexts": []}
    original = issue_mod._review_diff

    def spy(llm, diff, issue_text, verify_rounds, context=None):
        seen["contexts"].append(context)
        return original(llm, diff, issue_text, verify_rounds, context)

    monkeypatch.setattr(issue_mod, "_review_diff", spy)
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
    run_issue_agent(task, MockLLMClient(script))
    assert len(seen["contexts"]) == 2
    assert all(c is not None for c in seen["contexts"])          # 两次调用都传了 context
    assert all("工作树事实不可用" in c for c in seen["contexts"])  # 非 git 目录 → 降级提示


# --- A4: 红线路径工具级拦截（_enforce_red_lines 还原 cost_log/generated_at 类改动） ---


def _make_redline_repo(tmp_path) -> str:
    """构造红线拦截测试仓库：基线提交后制造红线改动 + 合法改动。

    基线含 output/eval/cost_log.jsonl（红线：文件名含 cost_log）、
    data/extractions/v3_seed_db_v2.json（红线：文件内 _meta.generated_at 行，
    文件名不含模式，靠改动行内容命中）、a.txt（合法改动）、
    notes/other_meta.txt（env 覆盖模式的命中文件，名字含 other）。
    """
    repo = tmp_path / "redline-repo"
    repo.mkdir()
    (repo / "output" / "eval").mkdir(parents=True)
    (repo / "data" / "extractions").mkdir(parents=True)
    (repo / "notes").mkdir()
    (repo / "output" / "eval" / "cost_log.jsonl").write_text(
        '{"cost": 1}\n', encoding="utf-8")
    (repo / "data" / "extractions" / "v3_seed_db_v2.json").write_text(
        '{"_meta": {"generated_at": "2024-01-01"}}\n', encoding="utf-8")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    (repo / "notes" / "other_meta.txt").write_text("other meta\n", encoding="utf-8")
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", "base")
    # 工作树改动：红线两文件 + 合法文件 + env 命中文件（均未暂存）
    (repo / "output" / "eval" / "cost_log.jsonl").write_text(
        '{"cost": 1}\n{"cost": 2}\n', encoding="utf-8")
    (repo / "data" / "extractions" / "v3_seed_db_v2.json").write_text(
        '{"_meta": {"generated_at": "2025-01-01"}}\n', encoding="utf-8")
    (repo / "a.txt").write_text("base\nfixed\n", encoding="utf-8")
    (repo / "notes" / "other_meta.txt").write_text("other meta 2\n", encoding="utf-8")
    return str(repo)


def test_enforce_red_lines_reverts_red_line_authored_changes(tmp_path):
    """默认红线模式：cost_log（文件名命中）与 v3_seed（generated_at 行命中）
    被还原为 base 版本，合法改动 a.txt 保留，返回清单恰为两条红线路径。"""
    repo = _make_redline_repo(tmp_path)
    reverted = issue_mod._enforce_red_lines(repo, "main")
    assert sorted(reverted) == sorted([
        "output/eval/cost_log.jsonl",
        "data/extractions/v3_seed_db_v2.json",
    ])
    assert "a.txt" not in reverted
    # 红线文件内容还原为 base 版本
    assert open(os.path.join(repo, "output", "eval", "cost_log.jsonl"),
                encoding="utf-8").read() == '{"cost": 1}\n'
    assert open(os.path.join(repo, "data", "extractions", "v3_seed_db_v2.json"),
                encoding="utf-8").read() == '{"_meta": {"generated_at": "2024-01-01"}}\n'
    # 合法改动保留
    assert open(os.path.join(repo, "a.txt"), encoding="utf-8").read() == "base\nfixed\n"


def test_enforce_red_lines_env_override_patterns(tmp_path, monkeypatch):
    """KA_REDLINE_PATTERNS 覆盖默认模式：仅 other 命中，默认红线不再拦截。"""
    monkeypatch.setenv("KA_REDLINE_PATTERNS", "other")
    repo = _make_redline_repo(tmp_path)
    reverted = issue_mod._enforce_red_lines(repo, "main")
    assert reverted == ["notes/other_meta.txt"]
    # 默认模式不拦：cost_log / v3_seed 的改动保留在工作树
    assert '{"cost": 2}' in open(os.path.join(
        repo, "output", "eval", "cost_log.jsonl"), encoding="utf-8").read()
    assert "2025-01-01" in open(os.path.join(
        repo, "data", "extractions", "v3_seed_db_v2.json"),
        encoding="utf-8").read()


def test_enforce_red_lines_no_hits_returns_empty(tmp_path):
    """无红线文件（仅合法改动）：返回空清单，工作树零还原。"""
    repo = tmp_path / "clean-repo"
    repo.mkdir()
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", "base")
    (repo / "a.txt").write_text("base\nfixed\n", encoding="utf-8")
    assert issue_mod._enforce_red_lines(str(repo), "main") == []
    assert open(os.path.join(repo, "a.txt"), encoding="utf-8").read() == "base\nfixed\n"


def test_review_context_appends_extra_facts(tmp_path):
    """_review_context 的 extra_facts 追加为「额外事实」段（红线还原事实面）；
    缺省（None/空）不追加任何段。"""
    repo = _make_worktree_repo(tmp_path)
    out = issue_mod._review_context(
        repo, "main",
        extra_facts=["红线还原（2 个文件）: output/eval/cost_log.jsonl, "
                     "data/extractions/v3_seed_db_v2.json"])
    assert "额外事实" in out
    assert "红线还原（2 个文件）" in out
    assert "output/eval/cost_log.jsonl" in out
    assert "额外事实" not in issue_mod._review_context(repo, "main")


def test_run_issue_agent_records_red_line_reverts(tmp_path, monkeypatch):
    """红线还原清单进入审批单 JSON，且 _review_context 调用收到红线事实
    （Reviewer 可见）。_enforce_red_lines 打桩（集成测试不依赖真 git 仓库）。"""
    _patch_github(monkeypatch)
    fake_reverts = ["output/eval/cost_log.jsonl",
                    "data/extractions/v3_seed_db_v2.json"]
    monkeypatch.setattr(issue_mod, "_enforce_red_lines",
                        lambda d, b: list(fake_reverts))
    seen = {"facts": []}
    original_ctx = issue_mod._review_context

    def spy(repo_dir, base_branch, extra_facts=None):
        seen["facts"].append(extra_facts)
        return original_ctx(repo_dir, base_branch, extra_facts)

    monkeypatch.setattr(issue_mod, "_review_context", spy)
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path),
                     approval_dir=str(tmp_path / "approvals"))
    result = run_issue_agent(task, MockLLMClient(script))
    data = json.load(open(result.approval_path, encoding="utf-8"))
    assert data["red_line_reverts"] == fake_reverts
    # Reviewer 可见：reverts 非空时 _review_context 至少一次收到红线还原事实
    assert any(f and len(f) == 1 and "红线还原（2 个文件）" in f[0]
               for f in seen["facts"])


def test_run_issue_agent_no_red_lines_empty_field(tmp_path, monkeypatch):
    """无红线还原：审批单 red_line_reverts 为空清单（字段恒在）。"""
    _patch_github(monkeypatch)
    monkeypatch.setattr(issue_mod, "_enforce_red_lines", lambda d, b: [])
    script = _graph_script() + [
        LLMMessage(role="assistant", content="PASS 变更解决问题。"),
    ]
    task = IssueTask(repository="test/arc-wiki", issue_number=123,
                     workspace_root=_make_workdir(tmp_path),
                     approval_dir=str(tmp_path / "approvals"))
    result = run_issue_agent(task, MockLLMClient(script))
    data = json.load(open(result.approval_path, encoding="utf-8"))
    assert data["red_line_reverts"] == []
