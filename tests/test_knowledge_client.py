"""知识客户端测试：Mock 行为 / kind 映射 / 子进程调用 / 工厂"""
import json
import types

import pytest

from tools.file_tools import ToolError
from tools.knowledge_client import (
    ArknightsMCPClient,
    MockKnowledgeClient,
    _KIND_TOOL,
    get_knowledge_client,
)


def test_mock_knowledge_client_records_calls():
    mock = MockKnowledgeClient({("entity", "阿米娅"): "实体: 阿米娅"})
    assert mock.search("阿米娅", "entity") == "实体: 阿米娅"
    assert mock.search("罗德岛", "story") == "（无预设响应: story 罗德岛）"
    assert mock.calls == [("entity", "阿米娅"), ("story", "罗德岛")]


def test_kind_mapping():
    assert _KIND_TOOL["entity"][0] == "search_entities"
    assert _KIND_TOOL["event"][0] == "search_events"
    assert _KIND_TOOL["relationship"][0] == "query_relationship"
    assert _KIND_TOOL["timeline"][0] == "query_timeline"
    assert _KIND_TOOL["story"][0] == "search_story"


def test_arknights_client_invoke_ok(monkeypatch):
    client = ArknightsMCPClient("wiki", python="py", timeout=10)

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "py"
        assert cmd[1] == "-c"
        assert json.loads(cmd[4]) == {"query": "阿米娅", "limit": 5}
        return types.SimpleNamespace(returncode=0, stdout="实体结果", stderr="")

    monkeypatch.setattr("tools.knowledge_client.subprocess.run", fake_run)
    assert client.search("阿米娅", "entity") == "实体结果"


def test_arknights_client_invoke_failure(monkeypatch):
    client = ArknightsMCPClient("wiki", python="py", timeout=10)

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("tools.knowledge_client.subprocess.run", fake_run)
    assert "查询失败" in client.search("x")


def test_get_knowledge_client_disabled(monkeypatch):
    monkeypatch.delenv("ARKNIGHTS_USE_MCP", raising=False)
    monkeypatch.delenv("ARKNIGHTS_WIKI_DIR", raising=False)
    assert isinstance(get_knowledge_client(), ToolError)


def test_get_knowledge_client_missing_dir(monkeypatch):
    monkeypatch.setenv("ARKNIGHTS_USE_MCP", "1")
    monkeypatch.setenv("ARKNIGHTS_WIKI_DIR", "C:\\nonexistent")
    assert isinstance(get_knowledge_client(), ToolError)


def test_get_knowledge_client_ok(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "arknights_wiki").mkdir(parents=True)
    monkeypatch.setenv("ARKNIGHTS_USE_MCP", "1")
    monkeypatch.setenv("ARKNIGHTS_WIKI_DIR", str(wiki))
    client = get_knowledge_client()
    assert isinstance(client, ArknightsMCPClient)
    assert client.wiki_dir == str(wiki)