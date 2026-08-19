"""代码知识图：由 CodeMetadata 构建内存图，提供结构化查询。

查询为静态分析结果，与 search_code 互补（知道符号名时直达代码位置）。
同名符号取首个定义（v1 限制）。
"""
from dataclasses import dataclass

from tools.code_parser import CodeMetadata, build_metadata
from tools.file_tools import ToolError


@dataclass
class CodeGraph:
    """内存图：节点 = 符号（module/class/function），边 = import/inherits/calls/defines。"""

    def __init__(self, metadata: CodeMetadata):
        self._modules: dict[str, dict] = {}
        self._classes: dict[str, dict] = {}
        self._functions: dict[str, list[dict]] = {}
        for m in metadata.modules:
            self._modules[m.name] = {"imports": list(m.imports), "path": m.path}
            for c in m.classes:
                self._classes.setdefault(c.name, {
                    "module": m.name, "bases": list(c.bases), "methods": list(c.methods)})
            for f in m.functions:
                self._functions.setdefault(f.name, []).append(
                    {"module": m.name, "class": f.class_name, "calls": list(f.calls)})

    def query_calls(self, function: str) -> list[str]:
        """谁调用了 function（按 module[.class].function 展示）。"""
        out = []
        for name, defs in self._functions.items():
            for d in defs:
                if function in d["calls"]:
                    prefix = f"{d['module']}.{d['class']}." if d["class"] else f"{d['module']}."
                    out.append(f"{prefix}{name}")
        return sorted(out)

    def query_inheritance(self, class_name: str) -> list[str]:
        """class_name 的继承链（含自身，沿首个基类向上递归；未知类返回自身）。"""
        chain: list[str] = []
        seen: set[str] = set()
        cur = class_name
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            info = self._classes.get(cur)
            if info is None or not info["bases"]:
                break
            cur = info["bases"][0]
        return chain

    def query_imports(self, module: str) -> list[str]:
        """module 的直接导入列表。"""
        info = self._modules.get(module)
        if info is None:
            return []
        return sorted(info["imports"])

    def query_module_of(self, symbol: str) -> list[str]:
        """定义 symbol 的模块（类或函数）。"""
        out = []
        if symbol in self._classes:
            out.append(self._classes[symbol]["module"])
        for d in self._functions.get(symbol, []):
            if d["module"] not in out:
                out.append(d["module"])
        return sorted(out)

    def search_symbols(self, keyword: str) -> list[str]:
        """符号名模糊搜索（不区分大小写）。"""
        k = keyword.lower()
        out = {n for n in self._classes if k in n.lower()}
        out |= {n for n in self._functions if k in n.lower()}
        return sorted(out)


def build_code_graph(workspace_root: str) -> CodeGraph | ToolError:
    """从工作区构建 CodeGraph；工作区缺失返回 ToolError。"""
    metadata = build_metadata(workspace_root)
    if isinstance(metadata, ToolError):
        return metadata
    return CodeGraph(metadata)


def query_code_graph(graph: CodeGraph, query: str, arg: str) -> list[str] | ToolError:
    """工具分发：query ∈ calls / inheritance / imports / module_of / symbols。

    空结果附加自导航提示（spec §7）：未命中时引导 LLM 改用 symbols 模糊查询
    或 search_code 全文搜索继续检索。
    """
    if query == "calls":
        out = graph.query_calls(arg)
    elif query == "inheritance":
        out = graph.query_inheritance(arg)
    elif query == "imports":
        out = graph.query_imports(arg)
    elif query == "module_of":
        out = graph.query_module_of(arg)
    elif query == "symbols":
        out = graph.search_symbols(arg)
    else:
        return ToolError(f"未知查询类型: {query}（可用 calls / inheritance / imports / module_of / symbols）")
    if not out:
        return [f"未找到与 {arg} 相关的符号信息，可用 symbols 模糊查询或 search_code 全文搜索"]
    return out