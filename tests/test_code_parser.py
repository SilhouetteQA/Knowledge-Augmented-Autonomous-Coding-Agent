"""代码解析测试：AST 单文件解析 / 全工作区构建 / 语法错误跳过"""
import textwrap

import pytest

from tools.code_parser import parse_python_file

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
