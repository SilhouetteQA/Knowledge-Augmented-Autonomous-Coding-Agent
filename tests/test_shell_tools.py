# tests/test_shell_tools.py
"""Shell 工具测试：run_command 成功/失败/超时/越界/截断"""
from tools.file_tools import ToolError
from tools.shell_tools import MAX_COMMAND_OUTPUT, run_command


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
