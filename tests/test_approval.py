"""审批单核心测试：schema 校验、读写往返、状态机（本文件不含远端副作用）。"""
import hashlib
import json

import pytest

from tools.approval import (
    ApprovalError, create_approval, load_approval, save_approval,
)


def _approval(**kw):
    """构造默认审批单（可覆盖字段）。"""
    defaults = dict(action_type="pr_push", repository="test/arc-wiki",
                    issue_number=123, branch="fix/issue-123", base_branch="main",
                    commit_message="fix: 修复 #123 修复重复创建实体",
                    diff="+fixed\n-fixed\n", review="PASS 修复一致。",
                    verify_rounds=2, retry_count=0)
    defaults.update(kw)
    return create_approval(**defaults)


def test_create_approval_pending_with_fingerprint():
    a = _approval()
    assert a.status == "pending"
    assert a.approval_id.startswith("test__arc-wiki-123-")
    # diff 指纹 = sha256(diff)（确定性：同 diff 同指纹）
    assert a.diff_sha256 == hashlib.sha256(a.diff.encode("utf-8")).hexdigest()


def test_create_approval_rejects_unknown_action():
    with pytest.raises(ApprovalError, match="未知动作类型"):
        _approval(action_type="knowledge_apply")


def test_save_load_roundtrip(tmp_path):
    a = _approval(review="PASS 独立审查通过。")
    path = save_approval(a, str(tmp_path))
    assert path.endswith(f"{a.approval_id}.json")
    loaded = load_approval(path)
    assert loaded == a
    assert loaded.to_dict() == a.to_dict()


def test_load_ignores_extra_fields(tmp_path):
    """前向兼容：审批单 JSON 里未来新增的字段不破坏旧版本读取。"""
    path = save_approval(_approval(), str(tmp_path))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["future_field"] = 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert load_approval(path).approval_id == data["approval_id"]


def test_load_approval_missing_file():
    with pytest.raises(ApprovalError, match="不存在"):
        load_approval("nope.json")


def test_load_approval_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ApprovalError, match="解析失败"):
        load_approval(str(p))


def test_load_approval_missing_field(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"approval_id": "x"}), encoding="utf-8")
    with pytest.raises(ApprovalError, match="缺字段"):
        load_approval(str(p))


def test_load_approval_invalid_status(tmp_path):
    p = tmp_path / "bad.json"
    data = _approval().to_dict()
    data["status"] = "halfway"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ApprovalError, match="非法状态"):
        load_approval(str(p))


# --- 审批执行（approve_request）：mock gh + 本地临时 git 仓库 ---
import os
import subprocess

import tools.approval as approval_mod


def _patch_github_ops(monkeypatch):
    """打桩 commit/push/PR（approval 模块顶层引用），记录调用。"""
    calls = {"commit": [], "push": [], "pr": []}
    monkeypatch.setattr(approval_mod, "commit_changes",
                        lambda d, m: calls["commit"].append(m) or None)
    monkeypatch.setattr(approval_mod, "push_branch",
                        lambda d, b: calls["push"].append(b) or None)
    monkeypatch.setattr(
        approval_mod, "create_pull_request",
        lambda repo, head, base, title, body:
            calls["pr"].append((head, base, title)) or
            "https://github.com/test/arc-wiki/pull/999")
    return calls


def _init_repo_with_change(tmp_path, filename="a.txt", content="base\n"):
    """构造本地 git 仓库：main 基线提交 + fix/issue-123 分支上的未提交改动。"""
    workdir = tmp_path / "test__arc-wiki"
    workdir.mkdir(parents=True)

    def run(*args):
        subprocess.run(["git", "-C", str(workdir), *args], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (workdir / filename).write_text(content, encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "base")
    run("checkout", "-b", "fix/issue-123")
    (workdir / filename).write_text(content + "fixed\n", encoding="utf-8")
    return str(workdir)


def _approval_for_repo(workdir):
    """按仓库当前真实 diff 生成审批单（diff 指纹与仓库状态严格一致）。"""
    diff = subprocess.run(["git", "-C", workdir, "diff", "main"],
                          capture_output=True, text=True,
                          check=True, encoding="utf-8").stdout
    return _approval(diff=diff, base_branch="main")


def test_approve_executes_push_and_pr(tmp_path, monkeypatch):
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    calls = _patch_github_ops(monkeypatch)
    result = approval_mod.approve_request(approval, "approve", None, workdir)
    assert result.status == "approved"
    assert result.pr_url == "https://github.com/test/arc-wiki/pull/999"
    assert result.decided_at is not None
    assert calls["commit"] == ["fix: 修复 #123 修复重复创建实体"]
    assert calls["push"] == ["fix/issue-123"]
    assert calls["pr"][0][:2] == ("fix/issue-123", "main")


def test_approve_drift_detected(tmp_path, monkeypatch):
    """审批后仓库又被改动 → diff sha 不一致 → 拒绝执行。"""
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    calls = _patch_github_ops(monkeypatch)
    # 审批单生成后，仓库文件再次被改动（漂移）
    with open(os.path.join(workdir, "a.txt"), "a", encoding="utf-8") as f:
        f.write("fixed2\n")
    with pytest.raises(ApprovalError, match="仓库状态与审批时不一致"):
        approval_mod.approve_request(approval, "approve", None, workdir)
    assert calls["commit"] == [] and calls["push"] == [] and calls["pr"] == []


def test_approve_branch_missing_aborts(tmp_path, monkeypatch):
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    subprocess.run(["git", "-C", workdir, "checkout", "main"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", workdir, "branch", "-D", "fix/issue-123"],
                   check=True, capture_output=True)
    with pytest.raises(ApprovalError, match="分支不存在"):
        approval_mod.approve_request(approval, "approve", None, workdir)


def test_reject_zero_side_effects(tmp_path, monkeypatch):
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    calls = _patch_github_ops(monkeypatch)
    result = approval_mod.approve_request(approval, "reject", "测试未并入现有套件", workdir)
    assert result.status == "rejected"
    assert result.decision_comment == "测试未并入现有套件"
    assert result.decided_at is not None
    assert calls["commit"] == [] and calls["push"] == [] and calls["pr"] == []


def test_approve_replay_rejected(tmp_path, monkeypatch):
    """已处理的审批单（终态）再次审批 → 拒绝（防重放）。"""
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    _patch_github_ops(monkeypatch)
    approval_mod.approve_request(approval, "reject", "拒绝", workdir)
    with pytest.raises(ApprovalError, match="重复审批"):
        approval_mod.approve_request(approval, "approve", None, workdir)


def test_approve_invalid_decision(tmp_path, monkeypatch):
    workdir = _init_repo_with_change(tmp_path)
    approval = _approval_for_repo(workdir)
    with pytest.raises(ApprovalError, match="非法审批决定"):
        approval_mod.approve_request(approval, "maybe", None, workdir)