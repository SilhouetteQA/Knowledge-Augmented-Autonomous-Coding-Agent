"""MCP 集成测试：需要兄弟项目《Arknights LLM Wiki》（ARKNIGHTS_WIKI_DIR 指向其根目录）。

跳过条件：ARKNIGHTS_USE_MCP != 1 或 ARKNIGHTS_WIKI_DIR 不可定位。
"""
import os

import pytest

from tools.knowledge_client import ArknightsMCPClient

_WIKI_DIR = os.environ.get("ARKNIGHTS_WIKI_DIR", "")

pytestmark = pytest.mark.mcp


def _skip_reason() -> str | None:
    if os.environ.get("ARKNIGHTS_USE_MCP") != "1":
        return "ARKNIGHTS_USE_MCP != 1"
    if not _WIKI_DIR or not os.path.isdir(_WIKI_DIR):
        return "ARKNIGHTS_WIKI_DIR 未配置"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_entities_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("阿米娅", "entity")
    assert out and "阿米娅" in out


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_query_relationship_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("阿米娅", "relationship")
    assert out and ("阿米娅" in out or "未在索引中找到" in out)


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "skip")
def test_search_story_live():
    client = ArknightsMCPClient(_WIKI_DIR)
    out = client.search("博士", "story")
    assert out