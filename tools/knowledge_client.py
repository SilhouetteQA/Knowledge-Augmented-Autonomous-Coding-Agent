"""域知识客户端：Arknights 知识库 MCP 适配（兄弟项目《Arknights LLM Wiki》）。

以兄弟项目 venv 的 python 一次性子进程调用其 ArknightsMcpClient（stdio MCP），
本仓库零新增依赖，依赖与凭据隔离在兄弟项目侧。
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from tools.file_tools import ToolError

# 兄弟项目内一次性调用脚本：经其 ArknightsMcpClient 同步调用 MCP 工具，stdout 输出文本结果
_CALL_SCRIPT = (
    "import json, sys;"
    "from arknights_wiki.mcp_server.client import ArknightsMcpClient;"
    "c = ArknightsMcpClient();"
    "print(c.call_tool(sys.argv[1], json.loads(sys.argv[2])))"
)

# kind → (MCP 工具名, 参数映射)
_KIND_TOOL = {
    "entity": ("search_entities", lambda q: {"query": q, "limit": 5}),
    "event": ("search_events", lambda q: {"entity": q, "limit": 5}),
    "relationship": ("query_relationship", lambda q: {"entity_name": q}),
    "timeline": ("query_timeline", lambda q: {"query": q, "limit": 10}),
    "story": ("search_story", lambda q: {"query": q, "limit": 5}),
}


class KnowledgeClient(Protocol):
    """域知识检索协议：Agent 工具只依赖此接口。"""

    def search(self, query: str, kind: str | None = None) -> str: ...


class MockKnowledgeClient:
    """测试用：按 (kind, query) 预设响应，并记录每次调用。"""

    def __init__(self, script: dict[tuple[str | None, str], str] | None = None):
        self.script = script or {}
        self.calls: list[tuple[str | None, str]] = []

    def search(self, query: str, kind: str | None = None) -> str:
        self.calls.append((kind, query))
        return self.script.get((kind, query), f"（无预设响应: {kind} {query}）")


def _resolve_sibling_python(wiki_dir: str) -> str:
    """定位兄弟项目 venv python：Windows .venv/Scripts/python.exe，POSIX .venv/bin/python。"""
    venv = Path(wiki_dir) / ".venv"
    for cand in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


class ArknightsMCPClient:
    """兄弟项目 MCP 适配器：每次查询一次子进程（依赖与凭据隔离）。"""

    def __init__(self, wiki_dir: str, python: str | None = None, timeout: int = 120):
        self.wiki_dir = wiki_dir
        self.python = python or _resolve_sibling_python(wiki_dir)
        self.timeout = timeout

    def search(self, query: str, kind: str | None = None) -> str:
        tool, arg_fn = _KIND_TOOL.get(kind or "entity", _KIND_TOOL["entity"])
        return self._invoke(tool, arg_fn(query))

    def _invoke(self, tool: str, args: dict) -> str:
        cmd = [self.python, "-c", _CALL_SCRIPT, tool, json.dumps(args, ensure_ascii=False)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=self.wiki_dir, timeout=self.timeout,
            )
        except FileNotFoundError:
            return f"域知识不可用: 兄弟项目 python 不存在（{self.python}）"
        except subprocess.TimeoutExpired:
            return f"域知识查询超时（{self.timeout}s）"
        if proc.returncode != 0:
            return f"域知识查询失败: {proc.stderr.strip() or proc.stdout.strip()}"
        out = proc.stdout.strip()
        if not out:
            return "域知识查询无结果"
        return out


def get_knowledge_client() -> KnowledgeClient | ToolError:
    """工厂：ARKNIGHTS_USE_MCP=1 且 ARKNIGHTS_WIKI_DIR 可定位时返回适配器，否则 ToolError。"""
    if os.environ.get("ARKNIGHTS_USE_MCP") != "1":
        return ToolError("域知识未启用：请设置 ARKNIGHTS_USE_MCP=1")
    wiki_dir = os.environ.get("ARKNIGHTS_WIKI_DIR", "")
    if not wiki_dir or not (Path(wiki_dir) / "arknights_wiki").exists():
        return ToolError("兄弟项目不可用：请设置 ARKNIGHTS_WIKI_DIR（指向 Arknights LLM Wiki 根目录）")
    return ArknightsMCPClient(wiki_dir)