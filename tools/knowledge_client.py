"""域知识客户端：经 stdio MCP 子进程接入可插拔知识服务（通用接口）。

以服务侧 venv 的 python 一次性子进程调用其 MCP 客户端类（module:Class 配置化），
本仓库零新增依赖，依赖与凭据隔离在服务侧。未配置时优雅降级（工厂返回 ToolError，
Agent 图中 search_knowledge 工具返回未启用提示，不阻塞任务）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from tools.file_tools import ToolError

# 服务侧一次性调用脚本：经其 MCP 客户端类同步调用 MCP 工具，stdout 输出文本结果
# （先重配 stdout 为 UTF-8，避免 Windows 默认 GBK 管道导致中文乱码）
_CALL_SCRIPT = (
    "import importlib, json, sys;"
    "sys.stdout.reconfigure(encoding='utf-8', errors='replace');"
    "cls = getattr(importlib.import_module(sys.argv[1]), sys.argv[2]);"
    "c = cls();"
    "print(c.call_tool(sys.argv[3], json.loads(sys.argv[4])))"
)

# kind → (MCP 工具名, 参数映射)。默认按"实体/事件/关系/时间线/故事"五类语义映射；
# 接入具体知识服务时按其 MCP 工具名调整此表。
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


def _resolve_service_python(service_dir: str) -> str:
    """定位服务侧 python：优先其 .venv（Windows Scripts / POSIX bin），回退系统 PATH。"""
    venv = Path(service_dir) / ".venv"
    for cand in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if cand.exists():
            return str(cand)
    found = shutil.which("python")
    return found or sys.executable


class McpKnowledgeClient:
    """MCP 知识服务适配器：每次查询一次子进程（依赖与凭据隔离在服务侧）。

    import_path 形如 "pkg.module:ClassName"（服务侧需提供 call_tool(tool, args) -> str）。
    """

    def __init__(self, service_dir: str, import_path: str,
                 python: str | None = None, timeout: int = 120):
        self.service_dir = service_dir
        self.import_path = import_path
        self.python = python or _resolve_service_python(service_dir)
        self.timeout = timeout

    def search(self, query: str, kind: str | None = None) -> str:
        tool, arg_fn = _KIND_TOOL.get(kind or "entity", _KIND_TOOL["entity"])
        return self._invoke(tool, arg_fn(query))

    def _invoke(self, tool: str, args: dict) -> str:
        mod_name, _, cls_name = self.import_path.partition(":")
        cmd = [self.python, "-c", _CALL_SCRIPT, mod_name, cls_name,
               tool, json.dumps(args, ensure_ascii=False)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=self.service_dir, timeout=self.timeout,
            )
        except FileNotFoundError:
            return f"域知识不可用: 服务侧 python 不存在（{self.python}）"
        except OSError as e:
            return f"域知识查询失败: {e}"
        except subprocess.TimeoutExpired:
            return f"域知识查询超时（{self.timeout}s）"
        if proc.returncode != 0:
            return f"域知识查询失败: {proc.stderr.strip() or proc.stdout.strip()}"
        out = proc.stdout.strip()
        if not out:
            return "域知识查询无结果"
        return out


def get_knowledge_client() -> KnowledgeClient | ToolError:
    """工厂：KA_KNOWLEDGE_MCP=1 且服务目录/导入路径齐备时返回适配器，否则 ToolError。"""
    if os.environ.get("KA_KNOWLEDGE_MCP") != "1":
        return ToolError("域知识未启用：请设置 KA_KNOWLEDGE_MCP=1（知识服务经 MCP 接入）")
    svc_dir = os.environ.get("KA_KNOWLEDGE_MCP_DIR", "")
    imp = os.environ.get("KA_KNOWLEDGE_MCP_IMPORT", "")
    if not svc_dir or not Path(svc_dir).is_dir():
        return ToolError("知识服务不可用：请设置 KA_KNOWLEDGE_MCP_DIR（知识服务仓库根目录）")
    if not imp or ":" not in imp:
        return ToolError("知识服务未配置导入路径：请设置 KA_KNOWLEDGE_MCP_IMPORT（module:ClassName）")
    return McpKnowledgeClient(svc_dir, imp)
