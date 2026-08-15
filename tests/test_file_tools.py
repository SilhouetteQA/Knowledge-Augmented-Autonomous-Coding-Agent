# tests/test_file_tools.py
"""文件工具测试：list_files / read_file / search_code 与路径安全"""
from tools.file_tools import (
    FileContent,
    FileEntry,
    SearchResult,
    ToolError,
    list_files,
    read_file,
    resolve_workspace_path,
    search_code,
)


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


def test_read_file_with_line_numbers(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x=1\ny=2\n", encoding="utf-8")
    fc = read_file("a.py", workspace_root=str(ws))
    assert not isinstance(fc, ToolError)
    assert fc.lines == ["x=1", "y=2"]
    assert "1 | x=1" in fc.content and "2 | y=2" in fc.content
    assert fc.size == len((ws / "a.py").read_bytes())


def test_read_file_missing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = read_file("nope.py", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "不存在" in r.message


def test_read_file_directory(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    r = read_file("sub", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "目录" in r.message


def test_read_file_too_large(tmp_path, monkeypatch):
    import tools.file_tools as ft

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.py").write_text("a" * 600, encoding="utf-8")
    monkeypatch.setattr(ft, "MAX_READ_SIZE", 500)
    r = read_file("big.py", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "过大" in r.message


def test_search_code_finds_match(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ws / "b.py").write_text("x = hello()\n", encoding="utf-8")
    results = search_code("hello", workspace_root=str(ws))
    assert not isinstance(results, ToolError)
    assert len(results) == 2
    first = results[0]
    assert first.path == "a.py"
    assert first.line == 1
    assert first.column == 5
    assert "def hello" in first.text


def test_search_code_no_match(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x=1\n", encoding="utf-8")
    results = search_code("zzz_nope", workspace_root=str(ws))
    assert results == []


def test_search_code_ignore_case(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("Hello\n", encoding="utf-8")
    assert search_code("hello", workspace_root=str(ws)) == []
    hits = search_code("hello", ignore_case=True, workspace_root=str(ws))
    assert len(hits) == 1


def test_search_code_rg_missing(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_BIN", str(tmp_path / "no-rg.exe"))
    r = search_code("x", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "ripgrep 不可用" in r.message
