"""MCP 知识服务集成测试：需要 KA_KNOWLEDGE_MCP* 环境配置指向可用知识服务。

跳过条件：KA_KNOWLEDGE_MCP != 1 或 KA_KNOWLEDGE_MCP_DIR/IMPORT 不可用。
"""
import os

import pytest

from tools.knowledge_client import McpKnowledgeClient

_SVC_DIR = os.environ.get("KA_KNOWLEDGE_MCP_DIR", "")
_IMP = os.environ.get("KA_KNOWLEDGE_MCP_IMPORT", "")

pytestmark = pytest.mark.mcp


def _skip_reason() -> str | None:
    if os.environ.get("KA_KNOWLEDGE_MCP") != "1":
        return "KA_KNOWLEDGE_MCP != 1"
    if not _SVC_DIR or not os.path.isdir(_SVC_DIR):
        return "KA_KNOWLEDGE_MCP_DIR 未配置"
    if not _IMP:
        return "KA_KNOWLEDGE_MCP_IMPORT 未配置"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_entities_live():
    client = McpKnowledgeClient(_SVC_DIR, _IMP)
    out = client.search("阿米娅", "entity")
    assert out and "阿米娅" in out


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_query_relationship_live():
    client = McpKnowledgeClient(_SVC_DIR, _IMP)
    out = client.search("阿米娅", "relationship")
    assert out and ("阿米娅" in out or "未在索引中找到" in out)


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_story_live():
    client = McpKnowledgeClient(_SVC_DIR, _IMP)
    out = client.search("博士", "story")
    assert out
