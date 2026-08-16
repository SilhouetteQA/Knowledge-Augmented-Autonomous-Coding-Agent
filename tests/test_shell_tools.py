# tests/test_shell_tools.py
"""Shell 工具测试：run_command 成功/失败/超时/越界/截断"""
from tools.file_tools import ToolError
from tools.shell_tools import MAX_COMMAND_OUTPUT, run_command, run_tests


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
