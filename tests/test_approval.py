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