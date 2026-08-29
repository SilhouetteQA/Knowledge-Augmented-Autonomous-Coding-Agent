# tests/test_file_tools.py
"""文件工具测试：list_files / read_file / search_code / write_file 与路径安全"""
from tools.file_tools import (
    FileContent,
    FileEntry,
    SearchResult,
    ToolError,
    WriteResult,
    list_files,
    read_file,
    resolve_workspace_path,
    search_code,
    write_file,
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


def test_resolve_rejects_absolute_outside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = resolve_workspace_path("C:/Windows", str(ws))
    assert isinstance(result, ToolError)
    assert "越界" in result.message


def test_list_files_subdir_workspace_relative(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "top.py").write_text("y = 2\n", encoding="utf-8")
    result = list_files(root="sub", workspace_root=str(ws))
    assert not isinstance(result, ToolError)
    # path 相对 workspace 根，不是相对搜索子目录
    assert [e.path for e in result] == ["sub/a.py"]


def test_search_code_subdir_workspace_relative(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ws / "top.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    results = search_code("hello", root="sub", workspace_root=str(ws))
    assert not isinstance(results, ToolError)
    assert len(results) == 1
    assert results[0].path == "sub/a.py"


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


def test_write_file_creates_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = write_file("sub/dir/new.py", "code", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.path == "sub/dir/new.py"
    assert r.bytes_written == 4
    assert r.overwritten is False
    assert (ws / "sub" / "dir" / "new.py").read_text(encoding="utf-8") == "code"


def test_write_file_overwrites(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("old", encoding="utf-8")
    r = write_file("a.py", "new", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.overwritten is True
    assert (ws / "a.py").read_text(encoding="utf-8") == "new"


def test_write_file_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = write_file("../evil.py", "x", workspace_root=str(ws))
    assert isinstance(r, ToolError)
    assert "越界" in r.message


def test_list_files_uses_workspace_root_env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    # 不显式传 workspace_root，读取环境变量 WORKSPACE_ROOT
    result = list_files(workspace_root=None)
    assert not isinstance(result, ToolError)
    assert [e.path for e in result] == ["a.py"]


# ---- edit_file（局部精确替换，W8 后实测补强：write_file 全量覆盖是 rich#3299 失败根因）----

from tools.file_tools import EditResult, edit_file  # noqa: E402


def test_edit_file_replaces_single_occurrence(tmp_path):
    (tmp_path / "mod.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    result = edit_file("mod.py", "return 1", "return 2", workspace_root=str(tmp_path))
    assert not isinstance(result, ToolError)
    assert result.replacements == 1
    assert "return 2" in (tmp_path / "mod.py").read_text(encoding="utf-8")
    assert "return 1" not in (tmp_path / "mod.py").read_text(encoding="utf-8")


def test_edit_file_missing_old_text_errors(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = edit_file("mod.py", "不存在片段", "y", workspace_root=str(tmp_path))
    assert isinstance(result, ToolError)
    assert "未找到" in result.message
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_file_ambiguous_requires_expected_count(tmp_path):
    (tmp_path / "mod.py").write_text("pass\npass\npass\n", encoding="utf-8")
    result = edit_file("mod.py", "pass", "ok", workspace_root=str(tmp_path))
    assert isinstance(result, ToolError)
    assert "3 次" in result.message
    # 显式 expected_count 确认后允许全部替换
    ok = edit_file("mod.py", "pass", "ok", expected_count=3, workspace_root=str(tmp_path))
    assert not isinstance(ok, ToolError)
    assert ok.replacements == 3
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "ok\nok\nok\n"


def test_edit_file_empty_old_text_rejected(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = edit_file("mod.py", "", "y", workspace_root=str(tmp_path))
    assert isinstance(result, ToolError)


def test_edit_file_missing_file(tmp_path):
    result = edit_file("nope.py", "a", "b", workspace_root=str(tmp_path))
    assert isinstance(result, ToolError)


def test_edit_file_multiline_with_indent(tmp_path):
    (tmp_path / "mod.py").write_text("if True:\n    pass\n", encoding="utf-8")
    result = edit_file("mod.py", "    pass", "    return 0", workspace_root=str(tmp_path))
    assert not isinstance(result, ToolError)
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "if True:\n    return 0\n"
