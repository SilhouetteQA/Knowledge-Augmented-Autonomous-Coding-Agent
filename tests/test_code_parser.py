"""代码解析测试：AST 单文件解析 / 全工作区构建 / 语法错误跳过"""
import textwrap

import pytest

from tools.code_parser import build_metadata, parse_python_file
from tools.file_tools import ToolError

SAMPLE = textwrap.dedent('''
    import os
    from pathlib import Path

    class Base:
        def run(self):
            return self.work()

    class Worker(Base):
        def work(self):
            return helper(1)

    def helper(x):
        return x + 1

    def main():
        w = Worker()
        w.run()
        print(os.getcwd())
''')


def test_parse_extracts_imports(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    assert mod.imports == ["os", "pathlib"]


def test_parse_extracts_classes(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    assert {c.name for c in mod.classes} == {"Base", "Worker"}
    worker = next(c for c in mod.classes if c.name == "Worker")
    assert worker.bases == ["Base"]
    assert worker.methods == ["work"]
    base = next(c for c in mod.classes if c.name == "Base")
    assert base.methods == ["run"]


def test_parse_extracts_functions_and_calls(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")
    mod = parse_python_file(str(f), "sample")
    helper = next(x for x in mod.functions if x.name == "helper")
    assert helper.class_name is None
    assert helper.calls == []
    main = next(x for x in mod.functions if x.name == "main")
    assert {"Worker", "run", "print", "getcwd"} <= set(main.calls)
    work = next(x for x in mod.functions if x.name == "work")
    assert work.class_name == "Worker"
    assert work.calls == ["helper"]


def test_parse_syntax_error_raises(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        parse_python_file(str(bad), "bad")


def test_build_metadata_full_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ws / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "pkg" / "mod.py").write_text("import os\n\nclass C:\n    pass\n", encoding="utf-8")
    (ws / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    meta = build_metadata(str(ws))
    assert {m.name for m in meta.modules} == {"app", "pkg", "pkg.mod"}
    assert any("broken.py" in w for w in meta.warnings)


def test_build_metadata_skips_ignored_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".venv").mkdir()
    (ws / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".venv" / "lib.py").write_text("y = 2\n", encoding="utf-8")
    meta = build_metadata(str(ws))
    assert [m.name for m in meta.modules] == ["ok"]


def test_build_metadata_missing_workspace(tmp_path):
    result = build_metadata(str(tmp_path / "nonexistent"))
    assert isinstance(result, ToolError)
