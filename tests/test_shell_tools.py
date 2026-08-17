# tests/test_shell_tools.py
"""Shell 工具测试：run_command 成功/失败/超时/越界/截断；git 只读工具"""
from tools.file_tools import ToolError
from tools.shell_tools import (
    MAX_COMMAND_OUTPUT, LocalExecutor, get_executor, git_diff, git_log, git_status,
    run_command, run_tests,
)


def test_run_command_success(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('hello')\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0
    assert "hello" in r.stdout
    assert r.timeout is False
    assert r.duration >= 0


def test_run_command_failure(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"import sys; sys.exit(3)\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 3


def test_run_command_timeout(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"import time; time.sleep(5)\"", timeout=1,
                    workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.timeout is True
    assert r.exit_code == -1
    assert r.duration < 5


def test_run_command_cwd_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("echo hi", cwd="../../..", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "越界" in r.message


def test_run_command_output_truncated(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('x' * 200000)\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert len(r.stdout) <= MAX_COMMAND_OUTPUT + 20  # 截断标记余量
    assert "截断" in r.stdout


def _write_project(tmp_path, files):
    """测试设施：在 tmp 下按 dict 写项目文件。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for name, content in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ws


def test_run_tests_all_pass(tmp_path):
    ws = _write_project(tmp_path, {
        "test_ok.py": "def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 2 == 2\n",
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 2
    assert r.failed == 0
    assert r.error == 0
    assert r.total == 2
    assert r.failures == []


def test_run_tests_with_failures(tmp_path):
    ws = _write_project(tmp_path, {
        "test_mix.py": (
            "def test_ok():\n    assert 1 == 1\n"
            "def test_bad():\n    assert 1 == 2\n"
        ),
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 1
    assert r.failed == 1
    assert r.total == 2
    assert len(r.failures) == 1
    assert "test_bad" in r.failures[0].test


def test_run_tests_collection_error(tmp_path):
    ws = _write_project(tmp_path, {
        "test_broken.py": "def test_x(:\n    pass\n",  # 语法错误
    })
    r = run_tests(workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.error >= 1


def test_run_tests_subpath(tmp_path):
    ws = _write_project(tmp_path, {
        "sub/test_a.py": "def test_a():\n    assert 1 == 1\n",
        "test_other.py": "def test_other():\n    assert 1 == 1\n",
    })
    r = run_tests(path="sub", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.passed == 1


import os
import subprocess as _sp


def _make_git_repo(tmp_path):
    """测试设施：初始化 tmp git 仓库并做一次提交。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    _sp.run(["git", "init", "-b", "main"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "config", "user.name", "tester"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "config", "user.email", "t@test.dev"], cwd=ws, capture_output=True, check=True)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=ws, capture_output=True, check=True)
    _sp.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True)
    return ws


def test_git_status_clean_and_dirty(tmp_path):
    ws = _make_git_repo(tmp_path)
    s = git_status(workspace_root=str(ws))
    assert not isinstance(s, ToolError)
    assert s.clean is True
    assert s.changes == []
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    s2 = git_status(workspace_root=str(ws))
    assert s2.clean is False
    assert any("a.py" in c for c in s2.changes)


def test_git_diff_shows_change(tmp_path):
    ws = _make_git_repo(tmp_path)
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    d = git_diff(workspace_root=str(ws))
    assert not isinstance(d, ToolError)
    assert "a.py" in d.stat
    assert "+x = 2" in d.diff


def test_git_log_entries(tmp_path):
    ws = _make_git_repo(tmp_path)
    log = git_log(workspace_root=str(ws))
    assert not isinstance(log, ToolError)
    assert len(log.entries) == 1
    assert "init" in log.entries[0]


def test_git_tools_not_a_repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # tmp_path 位于 worktree git 仓库内部，git 上溯会命中外层仓库而成功返回；
    # 用 GIT_CEILING_DIRECTORIES 限制上溯，使 ws 之外即视为「不属于任何仓库」，
    # 从而驱动 git 返回「not a git repository」进而得到 ToolError。
    old = os.environ.get("GIT_CEILING_DIRECTORIES")
    os.environ["GIT_CEILING_DIRECTORIES"] = str(ws.parent)
    try:
        s = git_status(workspace_root=str(ws))
    finally:
        if old is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = old
    assert isinstance(s, ToolError)


def test_get_executor_default_local(monkeypatch):
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    ex = get_executor()
    assert not isinstance(ex, ToolError)
    assert isinstance(ex, LocalExecutor)


def test_get_executor_env_docker_without_context(monkeypatch):
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "sandbox_executor" in ex.message


def test_get_executor_unknown(monkeypatch):
    monkeypatch.setenv("KA_EXECUTOR", "xxx")
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "未知执行器" in ex.message


def test_run_command_oom_field_default(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = run_command("python -c \"print('hi')\"", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.oom is False
