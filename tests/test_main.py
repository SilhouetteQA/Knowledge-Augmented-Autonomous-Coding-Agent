# tests/test_main.py
"""CLI 测试：参数解析与 main 输出（注入 fake，不触发真实 LLM）"""
import main
from agent.loop import AgentResult


def test_parser_defaults():
    args = main.build_parser().parse_args(["任务"])
    assert args.max_iterations == 10
    assert args.workspace == "workspace"


def test_main_prints_result(monkeypatch, capsys):
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    rc = main.main(["测试任务", "--max-iterations", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "测试任务" in out
    assert "搞定" in out


def test_main_executor_docker_sets_env(monkeypatch, capsys):
    import os
    fake_result = AgentResult(steps=[], final_answer="搞定", iteration_count=1,
                              stopped_by_limit=False)
    monkeypatch.setattr("main.run_agent", lambda *a, **k: fake_result)
    monkeypatch.setattr("main.OpenAICompatClient", lambda: object())
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    try:
        rc = main.main(["测试任务", "--executor", "docker"])
        assert rc == 0
        assert os.environ["KA_EXECUTOR"] == "docker"
    finally:
        # 恢复环境，避免 KA_EXECUTOR=docker 泄漏影响同进程内其他测试
        os.environ.pop("KA_EXECUTOR", None)
