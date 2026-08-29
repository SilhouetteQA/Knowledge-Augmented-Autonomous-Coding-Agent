# W8 Human-in-the-loop 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 W5 的 review 与 push 之间插入人工审批门禁——`--issue` 干跑产出审批单并停止等待，`--approve` 审批后才 commit+push+PR；Reviewer 升级为独立审查角色；多余自动推送入口（`--push`）移除。

**Architecture:** 新增 `tools/approval.py` 审批单核心（dataclass + 读写 + 状态机 + 漂移检查），`run_issue_agent` 在 review 后无条件下发审批单（`approval_dir` 参数，评估模式传 None 保持旧行为），`main.py --approve` 为推送唯一入口；审批执行复用 W5 `tools/github_tools.py` 宿主 git/gh 操作（不改该文件）。

**Tech Stack:** Python 3.12 / argparse CLI / hashlib+json（审批单）/ subprocess git（漂移检查）/ pytest（TDD）/ 现有 MockLLMClient + Fake gh 测试模式。

## Global Constraints

- Python 3.12+；中文注释；代码无 emoji；UTF-8 编码。
- 凭据边界：gh/git 写操作仅宿主执行；沙箱容器内无凭据（延续 W5）。
- Seam 先行：本窗口公共 seam = `tools/approval.py`（ApprovalRequest / create_approval / save_approval / load_approval / approve_request）与 `main.py --approve`；测试只写在这些 seam 上。
- TDD：red → green，一次一个测试，最小实现。
- 精准修改：`tools/github_tools.py` 不改；`benchmark/runner.py` 仅删 `push=False` 一行（语义变更连带）。
- 审批单产物目录 `output/approvals/` 必须 gitignore（确认 `output/` 已忽略，见 Task 1 Step 6）。
- 环境（本机实测）：worktree 需复制主仓库 `.venv` 与 `.env`；git 2.53（支持 `init -b`）；ripgrep 在 `C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe`（测试需 `RIPGREP_BIN`）。
- 全量回归基线：227 passed / 4 skipped / 1 已知环境性失败（`test_docker_integration::test_clone_repo_when_empty`，容器内 github TLS）；新增用例不得破坏该基线。

---

### Task 0: 环境准备（venv / .env / 基线回归）

**Files:** 无代码变更；仅环境与验证。

**Interfaces:** 无。

- [ ] **Step 1: 复制 venv 与 .env 到 W8 worktree**

```powershell
roboCopy.exe "D:\AI project\Knowledge-Augmented Autonomous Coding Agent\.venv" ".venv" /E /NFL /NDL /NJH /NJS /NC /NS
Copy-Item "D:\AI project\Knowledge-Augmented Autonomous Coding Agent\.env" ".env" -Force
Test-Path ".venv\Scripts\python.exe"; Test-Path ".env"
```

Expected: 两次 `True`。

- [ ] **Step 2: 基线回归确认环境可用**

```powershell
$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"
.venv\Scripts\python.exe -m pytest tests/ -q
```

Expected: `227 passed / 4 skipped / 1 failed`（唯一失败 = `test_docker_integration::test_clone_repo_when_empty`，已知环境性项）。若失败数多于 1，先修复环境再继续（禁止带着环境问题开工）。

- [ ] **Step 3: 确认工作区干净（无泄漏文件）**

```bash
git status --porcelain
```

Expected: 空输出（`.env`/`.venv`/`.worktrees` 均 gitignore）。若非空输出说明有泄漏，先处理。

---

### Task 1: 审批单核心（tools/approval.py：schema / 读写 / 校验）

**Files:**
- Create: `tools/approval.py`
- Test: `tests/test_approval.py`

**Interfaces:**
- Produces: `ApprovalError(Exception)`；`ApprovalRequest` dataclass（字段：`approval_id, action_type, status, repository, issue_number, branch, base_branch, commit_message, diff, diff_sha256, review, verify_rounds, retry_count, created_at, decided_at=None, decision_comment=None, pr_url=None`，方法 `to_dict()`）；`create_approval(*, action_type, repository, issue_number, branch, base_branch, commit_message, diff, review, verify_rounds, retry_count, created_at=None) -> ApprovalRequest`；`save_approval(approval, directory) -> str`（返回路径）；`load_approval(path) -> ApprovalRequest`。
- Consumes: 无（不依赖 agent/issue）。

- [ ] **Step 1: 写失败测试 `tests/test_approval.py`（schema / 读写 / 校验）**

```python
# tests/test_approval.py
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
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"
.venv\Scripts\python.exe -m pytest tests/test_approval.py -v
```

Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'tools.approval'`。

- [ ] **Step 3: 最小实现 `tools/approval.py`**

```python
"""审批单核心：人工审批门禁的工作工件（W8 Human-in-the-loop）。

审批单 = 一次等待人工决策的高风险操作请求。当前仅 pr_push 动作；
预留 knowledge_apply 等扩展点在 KNOWN_ACTIONS 注册（解冻后实现对应执行逻辑）。
"""
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

# 已知动作类型（扩展点：知识纠错 --apply 解冻后注册并实现执行逻辑）
KNOWN_ACTIONS = {"pr_push"}
# 状态机：pending（待审批）→ approved / rejected（终态，防重放）
STATUSES = {"pending", "approved", "rejected"}
# 审批单必填字段（load 校验）
_REQUIRED_FIELDS = [
    "approval_id", "action_type", "status", "repository", "issue_number",
    "branch", "base_branch", "commit_message", "diff", "diff_sha256",
    "review", "verify_rounds", "retry_count", "created_at",
]


class ApprovalError(Exception):
    """审批单相关错误（文件缺失/校验失败/状态非法/前置检查失败）。"""


@dataclass
class ApprovalRequest:
    """审批单：一次待人工审批的高风险操作请求（当前为 pr_push）。"""
    approval_id: str
    action_type: str
    status: str
    repository: str            # owner/name
    issue_number: int
    branch: str                # 待推送分支
    base_branch: str           # diff 基准 / PR base
    commit_message: str
    diff: str                  # 审批时的 diff 全文（sha256 校验漂移）
    diff_sha256: str
    review: str                # Reviewer Agent 结论文本
    verify_rounds: int
    retry_count: int
    created_at: str
    decided_at: str | None = None
    decision_comment: str | None = None
    pr_url: str | None = None

    def to_dict(self) -> dict:
        """序列化为 dict（dataclasses.asdict）。"""
        return asdict(self)


def _sha256(text: str) -> str:
    """diff 指纹：审批执行前重新比对，防仓库状态漂移。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_approval(*, action_type: str, repository: str, issue_number: int,
                    branch: str, base_branch: str, commit_message: str,
                    diff: str, review: str,
                    verify_rounds: int, retry_count: int,
                    created_at: str | None = None) -> ApprovalRequest:
    """生成审批单：approval_id（repository__issue-runid）+ diff_sha256 指纹。"""
    if action_type not in KNOWN_ACTIONS:
        raise ApprovalError(f"未知动作类型: {action_type}（已知: {sorted(KNOWN_ACTIONS)}）")
    stamp = created_at or time.strftime("%Y%m%d-%H%M%S")
    approval_id = f"{repository.replace('/', '__')}-{issue_number}-{stamp}"
    return ApprovalRequest(
        approval_id=approval_id, action_type=action_type, status="pending",
        repository=repository, issue_number=issue_number, branch=branch,
        base_branch=base_branch, commit_message=commit_message, diff=diff,
        diff_sha256=_sha256(diff), review=review, verify_rounds=verify_rounds,
        retry_count=retry_count, created_at=stamp)


def save_approval(approval: ApprovalRequest, directory: str) -> str:
    """写审批单到 directory/approval_id.json，返回文件路径。"""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{approval.approval_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(approval.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_approval(path: str) -> ApprovalRequest:
    """读审批单并校验 schema；非法文件/字段报 ApprovalError。"""
    if not os.path.isfile(path):
        raise ApprovalError(f"审批单不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ApprovalError(f"审批单解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ApprovalError("审批单格式非法：JSON 根必须是对象")
    missing = [k for k in _REQUIRED_FIELDS if k not in data]
    if missing:
        raise ApprovalError(f"审批单缺字段: {missing}")
    if data["status"] not in STATUSES:
        raise ApprovalError(f"非法状态: {data['status']}")
    if data["action_type"] not in KNOWN_ACTIONS:
        raise ApprovalError(f"未知动作类型: {data['action_type']}")
    # 仅取 dataclass 已知字段（前向兼容：忽略未来新增字段）
    known = set(ApprovalRequest.__dataclass_fields__)
    return ApprovalRequest(**{k: v for k, v in data.items() if k in known})
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_approval.py -v
```

Expected: 8 passed。

- [ ] **Step 5: 提交**

```bash
git add tools/approval.py tests/test_approval.py
git commit -m "feat(approval): 审批单核心——schema/读写/校验/状态机（pr_push 动作，预留 knowledge_apply）"
```

- [ ] **Step 6: 确认产物目录 gitignore**

```bash
git check-ignore output/approvals
```

Expected: 输出 `output/approvals`（已忽略）。若命令退出码非 0（未忽略），在 `.gitignore` 追加 `output/` 并提交（`chore: gitignore 覆盖审批单产物目录`）。

---

### Task 2: 审批执行（approve_request：漂移检查 / 推送 / 拒绝）

**Files:**
- Modify: `tools/approval.py`（追加 approve_request 与两个辅助函数）
- Test: `tests/test_approval.py`（追加执行链路用例）

**Interfaces:**
- Consumes: Task 1 的 `ApprovalRequest` / `ApprovalError`；`tools.github_tools.commit_changes / push_branch / create_pull_request / git_diff_since`（模块顶层引用，测试 monkeypatch 同名属性）；`tools.file_tools.ToolError`。
- Produces: `approve_request(approval: ApprovalRequest, decision: str, comment: str | None, repo_dir: str) -> ApprovalRequest`（返回更新后的审批单，由调用方 save）。

- [ ] **Step 1: 写失败测试（追加到 tests/test_approval.py 末尾）**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_approval.py -v
```

Expected: 原有 8 passed + 新增 6 个 FAIL（`AttributeError: module 'tools.approval' has no attribute 'approve_request'`）。

- [ ] **Step 3: 实现 approve_request（追加到 tools/approval.py 末尾）**

在 `tools/approval.py` 顶部 import 区追加：

```python
import subprocess

from tools.file_tools import ToolError
from tools.github_tools import (
    commit_changes, create_pull_request, git_diff_since, push_branch,
)
```

文件末尾追加：

```python
def _branch_exists(repo_dir: str, branch: str) -> bool:
    """本地 refs/heads/<branch> 是否存在（git rev-parse --verify）。"""
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_dir, capture_output=True)
    return probe.returncode == 0


def _check_drift(approval: ApprovalRequest, repo_dir: str) -> None:
    """执行前漂移检查：分支存在 + 当前 diff 与审批时指纹一致。"""
    if not _branch_exists(repo_dir, approval.branch):
        raise ApprovalError(
            f"分支不存在: {approval.branch}（仓库状态变化，请重新运行 --issue 生成新审批单）")
    current = git_diff_since(repo_dir, approval.base_branch)
    if isinstance(current, ToolError):
        raise ApprovalError(f"读取当前 diff 失败: {current.message}")
    if _sha256(current) != approval.diff_sha256:
        raise ApprovalError(
            "仓库状态与审批时不一致（diff 已变化），请重新运行 --issue 生成新审批单")


def approve_request(approval: ApprovalRequest, decision: str,
                    comment: str | None, repo_dir: str) -> ApprovalRequest:
    """执行人工审批：approve → 漂移检查 → commit → push → PR；reject → 仅记录。

    返回更新后的审批单（status/decided_at/decision_comment/pr_url），由调用方 save。
    """
    if approval.status != "pending":
        raise ApprovalError(f"审批单已处理（{approval.status}），拒绝重复审批")
    if decision not in ("approve", "reject"):
        raise ApprovalError(f"非法审批决定: {decision}（仅 approve/reject）")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if decision == "reject":
        approval.decided_at = now
        approval.decision_comment = comment
        approval.status = "rejected"
        return approval
    _check_drift(approval, repo_dir)
    err = commit_changes(repo_dir, approval.commit_message)
    if err is not None:
        raise ApprovalError(f"提交失败: {err.message}")
    err = push_branch(repo_dir, approval.branch)
    if err is not None:
        raise ApprovalError(f"推送失败: {err.message}（已在本机提交，可手工 git push 后处理）")
    pr_body = (f"Closes #{approval.issue_number}\n\n{approval.commit_message}\n\n"
               f"---\n验证轮数: {approval.verify_rounds}\n审查结论:\n{approval.review}")
    pr_url = create_pull_request(approval.repository, approval.branch,
                                 approval.base_branch, approval.commit_message,
                                 pr_body)
    if isinstance(pr_url, ToolError):
        raise ApprovalError(f"创建 PR 失败: {pr_url.message}（分支已推送）")
    approval.decided_at = now
    approval.pr_url = pr_url
    approval.status = "approved"
    return approval
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_approval.py -v
```

Expected: 14 passed。

- [ ] **Step 5: 提交**

```bash
git add tools/approval.py tests/test_approval.py
git commit -m "feat(approval): 审批执行——漂移检查（diff sha256+分支存在）/ approve 推送建 PR / reject 零副作用 / 防重放"
```

---

### Task 3: run_issue_agent 产单串联 + Reviewer 独立角色 + 移除 push

**Files:**
- Modify: `agent/issue.py`（IssueTask 字段、Reviewer 提示词与 _review_diff、产单逻辑、移除推送块、repo_dir_name 公开化）
- Modify: `benchmark/runner.py:120`（删 `push=False,` 一行）
- Test: `tests/test_issue_agent.py`

**Interfaces:**
- Consumes: Task 1-2 的 `create_approval / save_approval`；`tools.github_tools`（保留原 import 中的 clone/sync/branch/diff 模块函数）。
- Produces: `IssueTask(approval_dir: str | None = None)`（替换原 `push` 字段）；`IssueAgentResult.approval_path: str | None`；`repo_dir_name(repository) -> str`（原 `_repo_dir_name` 公开化，供 Task 4 使用）；`_review_diff(llm, diff, issue_text, verify_rounds)`。

- [ ] **Step 1: 更新测试（先改断言再实现，red）**

`tests/test_issue_agent.py` 顶部加 `import os`（现有 import json、pytest 保留）。逐段替换：

替换 `test_dry_run_full_pipeline` 的 task 构造与断言（去掉 push，加 approval 断言）：

```python
def test_dry_run_full_pipeline(tmp_path, monkeypatch):
    _patch_github(monkeypatch)
    commits = []
    monkeypatch.setattr(issue_mod, "commit_changes",
                        lambda d, m: commits.append(m) or None)
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
```

替换 `test_review_fail_triggers_retry`（去掉 push=True，断言不再提交）：

```python
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
```

把 `test_push_mode_creates_pr` 整体替换为产单用例：

```python
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
```

把 `test_push_mode_review_fail_aborts` 整体替换为「FAIL 仍产单」用例：

```python
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
```

新增 review 输入升级断言（verify_rounds 进入审查输入）：

```python
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
```

追加 review span metadata 扩展断言（「人工关注」标记进入 metadata，spec §4.4）：

```python
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
```

同时更新既有 `test_review_metadata_records_first_line` 断言（dict 新增 has_manual_flag 键）：

```python
    assert _review_metadata((), {}, FakeResult("PASS 修复点一致。")) == \
        {"first_line": "PASS 修复点一致。", "has_manual_flag": False}
    assert _review_metadata((), {}, FakeResult("")) == \
        {"first_line": "", "has_manual_flag": False}
    # 首行截断 80 字符（metadata 限长）
    long_line = "PASS " + "x" * 100
    assert _review_metadata((), {}, long_line) == {
        "first_line": ("PASS " + "x" * 75)[:80], "has_manual_flag": False}
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_issue_agent.py -v
```

Expected: 新用例 FAIL（`TypeError: IssueTask.__init__() got an unexpected keyword argument 'approval_dir'` / `_review_diff` 签名不匹配等）。

- [ ] **Step 3: 实现 `agent/issue.py` 变更**

**3a. 顶部 import 区**：`from tools.github_tools import (...)` 中移除 `commit_changes, create_pull_request, push_branch`（run 内不再推送）；追加 `from tools.approval import create_approval, save_approval`。

**3b. REVIEW_PROMPT 替换为独立审查角色版：**

```python
# Review 提示词（独立审查角色）：审查输入 = Issue + diff + 验证轮数，不见工作过程。
REVIEW_PROMPT = (
    "你是独立代码审查者（Reviewer Agent），独立于实施该变更的 Coder，审查以下变更：\n"
    "1. 变更是否解决 Issue 描述的问题；\n"
    "2. 是否有明显错误或遗漏（语法/逻辑/回归风险）；\n"
    "3. 是否混入与 Issue 无关的改动（格式调整、文档顺手修改等无关变更应明确指出，"
    "此类改动不得进入 PR）；\n"
    "4. 测试是否已并入仓库现有测试套件（若发现独立验证脚本而非正式测试，应指出并要求并入）。\n"
    "输出格式：第一行结论（PASS 或 FAIL），后续为中文要点列表；"
    "需要人工特别关注的事项以「人工关注」开头单独列出。"
)
```

**3c. `_repo_dir_name` 改名为公开 `repo_dir_name`（定义与使用处一并替换）；`_review_diff` 增加 verify_rounds 入参；`_review_metadata` 扩展「人工关注」标记：**

```python
def _review_metadata(args, kwargs, result) -> dict:
    """review span metadata：记录结论首行（PASS/FAIL）与「人工关注」标记。"""
    text = str(result or "").strip()
    first = (text.splitlines() or [""])[0]
    return {
        "first_line": first[:80],
        "has_manual_flag": "人工关注" in text,
    }


def _review_diff(llm: LLMClient, diff: str, issue_text: str,
                 verify_rounds: int) -> str:
    """独立 Reviewer 审查 diff，返回结论文本（首行 PASS/FAIL + 中文要点）。"""
    if not diff.strip():
        return "FAIL 无代码变更"
    msg = llm.chat(
        [{"role": "system", "content": REVIEW_PROMPT},
         {"role": "user",
          "content": f"Issue:\n{issue_text}\n\n验证轮数: {verify_rounds}\n\nDiff:\n{diff}"}],
        [],
    )
    return (msg.content or "FAIL 审查无输出").strip()
```

**3d. `IssueTask` 与 `IssueAgentResult`：**

```python
@dataclass
class IssueTask:
    """Issue 任务输入。"""
    repository: str              # owner/name
    issue_number: int
    workspace_root: str          # 仓库克隆父目录（如 workspace/repos）
    approval_dir: str | None = None   # 非 None：review 后生成审批单并停止（等待人工审批）
    max_iterations: int = 20
    issue_snapshot: GitHubIssue | None = None   # 非 None 时离线模式：跳过 get_issue


@dataclass
class IssueAgentResult:
    """Issue 任务结果。"""
    issue: GitHubIssue
    steps: list[AgentStep]
    final_answer: str
    branch: str
    diff: str
    review: str
    pr_url: str | None            # 恒为 None（推送唯一入口为 --approve，兼容旧字段）
    stopped_by_limit: bool
    iteration_count: int
    verify_rounds: int
    retry_count: int = 0          # FAIL 重试发生次数（0/1）
    approval_path: str | None = None   # approval_dir 非 None 时生成的审批单路径
```

**3e. `run_issue_agent` 主体变更**（保留 fetch→clone/sync→branch→graph→diff→review→FAIL 重试 1 轮全部原逻辑；仅替换两处调用 `_review_diff` 加 `result.verify_rounds` 实参，并把结尾推送块替换为产单块）：

替换原有推送块（从 `pr_url = None` 到 `if task.push ...` 整段）为：

```python
    approval_path = None
    if task.approval_dir:
        approval = create_approval(
            action_type="pr_push", repository=task.repository,
            issue_number=issue.number, branch=branch,
            base_branch=repo_info.default_branch,
            commit_message=f"fix: 修复 #{issue.number} {issue.title}",
            diff=diff, review=review,
            verify_rounds=result.verify_rounds, retry_count=1 if retried else 0)
        approval_path = save_approval(approval, task.approval_dir)

    return IssueAgentResult(
        issue=issue, steps=result.steps, final_answer=result.final_answer,
        branch=branch, diff=diff, review=review, pr_url=None,
        stopped_by_limit=result.stopped_by_limit,
        iteration_count=result.iteration_count,
        verify_rounds=result.verify_rounds,
        retry_count=1 if retried else 0,
        approval_path=approval_path,
    )
```

**3f. 模块 docstring 更新为：** `"""GitHub Issue Agent 全链路编排。\n\nGet Issue → Clone → Branch → Work（LangGraph 沙箱）→ Diff → Review（独立 Reviewer）→ 生成审批单并停止；\n推送唯一入口为 main.py --approve（人工审批门禁，W8）。\n凭据边界：gh/git 写操作在宿主与 --approve 阶段执行（沙箱容器内无凭据）。"""`

**3g. `benchmark/runner.py:120` 删除 `push=False,` 一行**（IssueTask 已无该字段）。

- [ ] **Step 4: 运行测试确认通过**

```powershell
$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"
.venv\Scripts\python.exe -m pytest tests/test_issue_agent.py tests/test_benchmark_runner.py tests/test_main.py -q
```

Expected: 全部通过（test_issue_agent 新旧用例合计 + runner/main 回归不受影响）。

- [ ] **Step 5: 提交**

```bash
git add agent/issue.py benchmark/runner.py tests/test_issue_agent.py
git commit -m "feat(issue): review 后生成审批单并停止（approval_dir），Reviewer 升级独立审查角色（四要点+验证轮数输入），移除 push 字段与 run 内推送"
```

---

### Task 4: main.py CLI 审批模式（--approve / 移除 --push）

**Files:**
- Modify: `main.py`（parser、_run_issue_mode、新增 _run_approve_mode、派发、模块 docstring）
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: Task 2 `approve_request`，Task 1 `load_approval / save_approval / ApprovalError`，Task 3 `repo_dir_name`。
- Produces: CLI `python main.py --approve <审批单路径> --decision approve|reject [--comment "..."]`。

- [ ] **Step 1: 写失败测试（追加/替换 tests/test_main.py）**

先给文件顶部加 `import pytest`（现有 import 为 `os` 与 `main`），再替换 `test_main_issue_mode_dry_run`：

```python
def test_main_issue_mode_generates_approval(monkeypatch, capsys):
    from agent.issue import IssueAgentResult
    from tools.github_tools import GitHubIssue
    fake_issue = GitHubIssue(number=1, title="标题", body="b", labels=[], state="open")
    fake = IssueAgentResult(issue=fake_issue, steps=[], final_answer="ok",
                            branch="fix/issue-1", diff="+x", review="PASS ok",
                            pr_url=None, stopped_by_limit=False,
                            iteration_count=1, verify_rounds=1,
                            approval_path="output/approvals/x-1-1-20260826-120000.json")
    monkeypatch.setattr("main.run_issue_agent", lambda *a, **k: fake)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["test/arc-wiki#1", "--issue"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix/issue-1" in out
    assert "等待人工审批" in out       # 停等审批提示（替换原 dry-run 文案）
```

追加（放到文件末尾）：

```python
# --- W8: --approve 审批模式 ---


def test_approve_cli_happy_path(monkeypatch, capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="PASS ok", verify_rounds=1, retry_count=0,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    decided = a
    decided.status = "approved"
    decided.pr_url = "https://github.com/test/arc-wiki/pull/9"
    decided.decided_at = "2026-08-26T12:01:00"
    monkeypatch.setattr("main.approve_request",
                        lambda *a2, **k: decided)
    monkeypatch.setattr("main.save_approval", lambda *a2, **k: path)
    rc = main.main(["--approve", path, "--decision", "approve"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已批准并推送" in out
    assert "pull/9" in out


def test_approve_cli_reject(monkeypatch, capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="FAIL 测试未并入套件", verify_rounds=1, retry_count=1,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    decided = a
    decided.status = "rejected"
    decided.decision_comment = "测试未并入现有套件"
    decided.decided_at = "2026-08-26T12:01:00"
    monkeypatch.setattr("main.approve_request", lambda *a2, **k: decided)
    rc = main.main(["--approve", path, "--decision", "reject",
                    "--comment", "测试未并入现有套件"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已拒绝" in out


def test_approve_cli_missing_file(capsys):
    rc = main.main(["--approve", "no-such-approval.json", "--decision", "approve"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "不存在" in out


def test_approve_cli_missing_decision(capsys, tmp_path):
    from tools.approval import create_approval, save_approval
    a = create_approval(action_type="pr_push", repository="test/arc-wiki",
                        issue_number=1, branch="fix/issue-1", base_branch="main",
                        commit_message="fix: 修复 #1 标题", diff="+x",
                        review="PASS ok", verify_rounds=1, retry_count=0,
                        created_at="20260826-120000")
    path = save_approval(a, str(tmp_path))
    rc = main.main(["--approve", path])
    out = capsys.readouterr().out
    assert rc == 1
    assert "缺少审批决定" in out


def test_push_flag_removed():
    """--push 已移除：任何绕过人工门禁的自动推送入口都不存在。"""
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["--push"])
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_main.py -v
```

Expected: 新用例 FAIL（`--approve` 未定义 / 输出文案不符等）。

- [ ] **Step 3: 实现 main.py 变更**

**3a. 顶部 import 追加：**

```python
from agent.issue import repo_dir_name, run_issue_agent    # 替换原 run_issue_agent 行
from tools.approval import (ApprovalError, approve_request,
                            load_approval, save_approval)   # 新增
```

**3b. 模块 docstring（第 2 行）中 `[--push]` 删除，改注审批模式：** `...Issue 模式：python main.py "<owner/name>#<issue_number>" --issue；审批模式：python main.py --approve <审批单路径> --decision approve|reject...`

**3c. build_parser：删除 `--push` 块（第 41-42 行），追加：**

```python
    parser.add_argument("--approve", default=None, metavar="审批单路径",
                        help="审批模式：审批 W8 审批单（配合 --decision approve|reject，approve 才推送并建 PR）")
    parser.add_argument("--decision", choices=["approve", "reject"], default=None,
                        help="审批决定：approve（commit+push+创建 PR）或 reject（拒绝，零远端副作用）")
    parser.add_argument("--comment", default=None,
                        help="审批意见（reject 建议填写原因，将写入审批单 decision_comment）")
```

**3d. `_run_issue_mode`：构造 task 的 `push=...` 行替换为：**

```python
    task = IssueTask(
        repository=repo, issue_number=int(num),
        workspace_root=args.workspace,
        approval_dir=os.environ.get("KA_APPROVAL_DIR", "output/approvals"),
        max_iterations=args.max_iterations,
    )
```

末尾输出块（第 127-130 行）替换为：

```python
    if result.pr_url:
        print(f"PR: {result.pr_url}")
    elif result.approval_path:
        print(f"审批单已生成: {result.approval_path}")
        print("等待人工审批：审阅差异与审查报告后执行 "
              f"--approve {result.approval_path} --decision approve|reject")
    else:
        print("（无审批单：approval_dir 未配置）")
    return 0
```

**3e. 新增 `_run_approve_mode`（放在 `_run_issue_mode` 之后）：**

```python
def _run_approve_mode(args: argparse.Namespace) -> int:
    """审批模式：加载审批单 → 人工决定 → 执行（approve 才产生远端副作用）。"""
    path = args.approve
    if not os.path.isfile(path):
        print(f"审批单不存在: {path}")
        return 1
    if args.decision is None:
        print("缺少审批决定：--decision approve|reject")
        return 1
    try:
        approval = load_approval(path)
        repo_dir = os.path.join(args.workspace,
                                repo_dir_name(approval.repository))
        approval = approve_request(approval, args.decision, args.comment, repo_dir)
        saved = save_approval(approval, os.path.dirname(path))
    except ApprovalError as e:
        print(f"审批失败: {e}")
        return 1
    if approval.status == "approved":
        print(f"已批准并推送: {approval.pr_url}")
    else:
        print("已拒绝（零远端副作用）")
    print(f"审批单已更新: {saved}")
    return 0
```

**3f. `_main_inner` 派发：`if args.correct:` 之后插入 `if args.approve: return _run_approve_mode(args)`**（审批模式无需 LLM，置于 LLM 创建之前）。

- [ ] **Step 4: 运行测试确认通过**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_main.py tests/test_issue_agent.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): 新增 --approve 审批模式（唯一推送入口），移除 --push 与 KA_ISSUE_PUSH，--issue 产单停等"
```

---

### Task 5: 真实验收（dry-run 停等 / push 通道 / 真实 PR）

**Files:** 无新代码；产出验收证据记录到 `docs/devlog.md`（Step 8）。

**Interfaces:** 无。

- [ ] **Step 1: 全量回归（实施后基线）**

```powershell
$env:RIPGREP_BIN="C:\Users\Silhouette\AppData\Local\Programs\rg\rg.exe"
.venv\Scripts\python.exe -m pytest tests/ -q
```

Expected: `241 passed / 4 skipped / 1 failed`（227 基线 + 14 新审批用例；唯一失败为已知容器内 github TLS 项）。若计数不符，先定位再继续。

- [ ] **Step 2: 环境前置检查**

```powershell
gh auth status
git config --global --get-regexp "url\..*insteadof"   # 预期 ghfast.top 镜像重写存在
```

Expected: gh 已登录（SilhouetteQA）；镜像重写存在（W6 配置）。若 gh 未登录：`gh auth login`（需用户参与）。

- [ ] **Step 3: 真实 dry-run 停等演示（dbader/schedule#646）**

```powershell
python main.py "dbader/schedule#646" --issue
```

Expected: Agent 全链路工作 → 独立 Reviewer 结论 → `审批单已生成: output/approvals/XXXX.json` + `等待人工审批：...` 提示；无任何远端副作用（gh api 只读）。人工审阅 `output/approvals/XXXX.json` 中的 `diff` 与 `review` 字段（可同时 `git -C workspace/dbader__schedule diff main` 对照）。

- [ ] **Step 4: 拒绝路径实测**

```powershell
python main.py --approve output/approvals/XXXX.json --decision reject --comment "演示拒绝路径"
```

Expected: `已拒绝（零远端副作用）`；审批单 status=rejected；`gh api repos/dbader/schedule/pulls` 无新 PR。**人工作见证：审批单再次 --approve 必须报"已处理，拒绝重复审批"（防重放）。**

- [ ] **Step 5: push 通道验证（真实 PR 前置条件）**

与用户确认测试仓库 `R`（有写权限；无则 `gh repo create --private <user>/w8-push-test` 需用户同意）。验证命令：

```powershell
gh repo clone R workspace/w8-push-test
git -C workspace/w8-push-test checkout -b test/w8-push-<ts>
# 追加一行后提交并推送：
git -C workspace/w8-push-test add -A; git -C workspace/w8-push-test commit -m "test: w8 push 通道验证"
git -C workspace/w8-push-test push -u origin test/w8-push-<ts>
git -C workspace/w8-push-test push origin --delete test/w8-push-<ts>
```

Expected: push 成功（直连或镜像通道）。若 `git push` 因 SNI 阻断失败（github.com 443 CONN_RESET）：记录实测现象，与用户确认替代通道（如临时 git config url.insteadOf 仅 fetch、换 push 目标仓库、或接受「真实 PR 演示改期」）。**该步为写操作，执行前需用户确认。**

- [ ] **Step 6: 真实 PR 演示（依赖 Step 5 + 用户仓库）**

```powershell
python main.py "R#N" --issue                    # 产单（R = 用户测试仓库，N = open issue 编号）
python main.py --approve output/approvals/XXXX.json --decision approve
```

Expected: 输出 `已批准并推送: https://github.com/<user>/R/pull/<n>`；`gh pr view <n> --repo R` 确认 PR 内容（commit message、body 含验证轮数与审查结论）；远端分支存在。**演示前用户确认，演示后远端分支可由用户删除（不自动删）。**

- [ ] **Step 7: 回归收尾确认**

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
git status --porcelain
```

Expected: 回归仍全绿（演示产物 workspace/、output/ 均 gitignore）；git status 干净。

---

### Task 6: 完全自动化可行性评估 + 窗口收尾（文档 / 合并）

**Files:**
- Modify: `docs/roadmap.md`（W8 [x] + 完成记录 + 验收标准达成 + 可行性评估结论）
- Modify: `docs/devlog.md`（W8 条目：决策/指标/遗留）
- Modify: `readme.md`（状态行：W0-W8 全部完成）

**Interfaces:** 无。

- [ ] **Step 1: 写 D5 完全自动化可行性评估（写入 roadmap W8 完成记录）**

内容提纲（基于真实数据填写）：
- W6 指标回顾：Resolution Rate 20%（1/5，docker 执行器），schedule-646/99 Judge PASS 但 test_pass=False（镜像缺 pytz 的评测基准缺口）；W5 实证：Agent 顺手改动 docstring、遗留独立验证脚本、迭代上限触达。
- W8 门禁运行数据：审批通过/拒绝次数、人工关注标记出现次数、漂移检查触犯次数（Step 5-6 实跑后回填）。
- 结论建议：自动化推送候选条件（测试全绿 + Judge PASS + 无无关改动 + 无「人工关注」标记 + 低风险类别）；知识写回类（knowledge_apply）与外部 API 副作用类永不自动；基于本窗口 mock 验收结果评估全自动的差距（当前差：人工最终仲裁 + reviewer 盲区收敛证据不足）。

- [ ] **Step 2: 更新 roadmap W8 状态与完成记录**

勾选全部任务（Reviewer Agent / Diff 展示与审批流程 / 审批后才 push+PR / 自动化可行性评估 / TDD+Review+合并）；完成记录写入：两段式 --approve 设计与实现、审批单机制（pr_push / 预留 knowledge_apply）、验收证据（mock 用例数 + 真实 dry-run + 真实 PR URL）、遗留问题（push 通道实测结论、知识纠错第二阶段已解冻待排期）。

- [ ] **Step 3: 更新 devlog（W8 条目：决策/指标/遗留）与 readme 状态行**

- devlog：窗口范围、5 项设计决策、实施要点（产单即停、防重放、漂移检查）、真实验收结果、遗留（评估问题清单 P0/P1/P2 仍待处理，见 W6W7 归档第三节）。
- readme：状态行 W0-W7 已完成 → W0-W8 全部完成（Knowledge-Augmented Autonomous Coding Agent 窗口路线图收官）。

- [ ] **Step 4: 提交文档**

```bash
git add docs/roadmap.md docs/devlog.md readme.md
git commit -m "docs: W8 收尾——roadmap/devlog/readme 状态更新（含完全自动化可行性评估结论）"
```

- [ ] **Step 5: 双轴审查 + 修复 + 全量回归**

使用 superpowers:requesting-code-review（Standards 轴 + Spec 轴）审查 `feature/w8-human-in-the-loop` 全部分支变更 → 修复审查意见（如需要）→

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

Expected: 全部通过（含已知环境性 1 failed）。

- [ ] **Step 6: 合并回 main 并清理**

```bash
git checkout main
git merge feature/w8-human-in-the-loop
git branch -d feature/w8-human-in-the-loop
git worktree remove .worktrees/w8-human-in-the-loop --force
```

Expected: 合并成功、分支与 worktree 删除（从主仓库目录执行；merge 前确认 main 上无未合并他人提交——`git fetch` 后若 main 有更新需先 rebase/merge）。

- [ ] **Step 7: 知识纠错第二阶段解冻提示**

在 devlog W8 条目的「下一步」注明：知识纠错第二阶段（LLM 事实核查 → `--apply` 写回）已具备审批门禁通道（action_type=knowledge_apply 预留），待用户排期实施；届时复用 `--approve` 审批流程。