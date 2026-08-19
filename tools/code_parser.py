"""代码解析：Python AST 提取代码元数据（模块/类/函数/导入/调用）。

W4 Repository Intelligence 的解析层：仅标准库静态分析；
语法错误文件跳过并计入 warnings（不中断全量构建）。
"""
import ast
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.file_tools import IGNORED_DIRS, ToolError


@dataclass
class FunctionInfo:
    """函数信息：calls 为 AST 静态收集的调用名（属性链取末段）。"""
    name: str
    module: str
    class_name: str | None = None
    calls: list[str] = field(default_factory=list)


@dataclass
class ClassInfo:
    """类信息：bases 为直接基类名，methods 为本体定义的方法名。"""
    name: str
    module: str
    bases: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


@dataclass
class ModuleInfo:
    """模块信息：imports 为直接导入的模块名（import x / from a.b import c → a.b）。"""
    name: str
    path: str
    imports: list[str] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)


@dataclass
class CodeMetadata:
    """全工作区代码元数据：modules 列表 + 解析警告。"""
    modules: list[ModuleInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _callee_name(node: ast.Call) -> str:
    """提取调用目标名：属性链取末段（a.b.c() → c），简化静态分析。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Call):
        return _callee_name(func)
    return "<dynamic>"


def _base_name(node) -> str:
    """基类名：Name 取 id，Attribute 取末段，其余占位。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "<expr>"


def _module_name_from_path(path: Path, root: Path) -> str:
    """相对根路径推导模块名：foo/bar.py → foo.bar；__init__.py → 所在目录名。"""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "<root>"


def _parse_function(node: ast.FunctionDef, module_name: str,
                    class_name: str | None = None) -> FunctionInfo:
    """解析函数定义：静态收集调用名（ast.walk 覆盖嵌套调用）。"""
    calls = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _callee_name(sub)
            if name not in calls:
                calls.append(name)
    return FunctionInfo(name=node.name, module=module_name,
                        class_name=class_name, calls=calls)


def parse_python_file(path: str, module_name: str) -> ModuleInfo:
    """解析单个 Python 文件为 ModuleInfo；语法错误抛 SyntaxError（由调用方记录）。"""
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    imports: list[str] = []
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            classes.append(ClassInfo(
                name=node.name, module=module_name,
                bases=[_base_name(b) for b in node.bases],
                methods=methods))
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    functions.append(_parse_function(sub, module_name, node.name))
        elif isinstance(node, ast.FunctionDef):
            functions.append(_parse_function(node, module_name))
    return ModuleInfo(name=module_name, path=path, imports=imports,
                      classes=classes, functions=functions)


def build_metadata(workspace_root: str, timeout: float = 60.0) -> CodeMetadata | ToolError:
    """遍历工作区所有 .py 构建元数据；语法错误跳过并记录；超时返回部分结果。

    排除 IGNORED_DIRS（.git / __pycache__ / .venv 等，与 list_files 一致）。
    """
    root = Path(workspace_root)
    if not root.exists():
        return ToolError(f"工作区不存在: {workspace_root}")
    metadata = CodeMetadata()
    start = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if time.monotonic() - start > timeout:
                metadata.warnings.append(f"解析超时（>{timeout}s），结果不完整")
                return metadata
            path = Path(dirpath) / name
            try:
                module_name = _module_name_from_path(path, root)
                metadata.modules.append(parse_python_file(str(path), module_name))
            except SyntaxError as e:
                metadata.warnings.append(
                    f"语法错误跳过: {path.relative_to(root)}（{e.msg} 第 {e.lineno} 行）")
    metadata.modules.sort(key=lambda m: m.name)
    return metadata
