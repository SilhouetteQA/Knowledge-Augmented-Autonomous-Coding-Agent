# tests/test_file_tools.py
"""list_files 与路径安全测试"""
from tools.file_tools import FileEntry, ToolError, list_files, resolve_workspace_path


def test_list_files_ignores_special_dirs(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.py").write_text("print(1)", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / "__pycache__").mkdir()
    (ws / "sub" / "b.txt").write_text("x", encoding="utf-8")
    result = list_files(workspace_root=str(ws))
    assert not isinstance(result, ToolError)
    assert [e.path for e in result] == ["a.py", "sub", "sub/b.txt"]
    assert {e.path for e in result if not e.is_dir} == {"a.py", "sub/b.txt"}


def test_list_files_missing_workspace(tmp_path):
    result = list_files(workspace_root=str(tmp_path / "nope"))
    assert isinstance(result, ToolError)
    assert "工作区不存在" in result.message


def test_resolve_rejects_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = resolve_workspace_path("../outside.py", str(ws))
    assert isinstance(result, ToolError)
    assert "越界" in result.message
