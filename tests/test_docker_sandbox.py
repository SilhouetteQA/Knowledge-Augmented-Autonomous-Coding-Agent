# tests/test_docker_sandbox.py
"""Docker 沙箱单元测试：FakeDocker 替换 docker CLI，不依赖真实 Docker。"""
import subprocess

import pytest

from tools.docker_sandbox import (
    DockerExecutor, SandboxConfig, SandboxManager, sandbox_executor,
    _container_name, _sh_quote, get_sandbox_config,
)
from tools.file_tools import ToolError
from tools.shell_tools import LocalExecutor, get_executor, run_command


class FakeDocker:
    """记录 docker 调用；plan 中键为子串匹配（如 "bash -lc"），命中即返回。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.plan: dict[str, subprocess.CompletedProcess] = {}

    def runner(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        joined = " ".join(args)
        for key, proc in self.plan.items():
            if key in joined:
                return proc
        return subprocess.CompletedProcess(args, 0, "", "")

    def called(self, *args: str) -> bool:
        return list(args) in self.calls

    def count(self, *prefix: str) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))


def test_container_name_stable():
    assert _container_name(r"C:\ws\demo-project") == _container_name(r"C:\ws\demo-project")
    assert "ka-sandbox-" in _container_name("x")


def test_sh_quote():
    assert _sh_quote("echo hi") == "'echo hi'"
    assert _sh_quote("it's") == "'it'\\''s'"


def test_get_sandbox_config_defaults(monkeypatch):
    for k in ("KA_SANDBOX_IMAGE", "KA_SANDBOX_CPUS", "KA_SANDBOX_MEMORY",
              "KA_SANDBOX_PIDS", "KA_SANDBOX_NETWORK", "KA_SANDBOX_REPO_URL"):
        monkeypatch.delenv(k, raising=False)
    cfg = get_sandbox_config()
    assert cfg.image == "ka-sandbox:py312-v1"
    assert cfg.cpus == 2.0
    assert cfg.memory == "1g"
    assert cfg.pids_limit == 512
    assert cfg.network is False
    assert cfg.repo_url is None


def test_get_sandbox_config_overrides(monkeypatch):
    monkeypatch.setenv("KA_SANDBOX_CPUS", "4")
    monkeypatch.setenv("KA_SANDBOX_MEMORY", "2g")
    monkeypatch.setenv("KA_SANDBOX_NETWORK", "1")
    monkeypatch.setenv("KA_SANDBOX_REPO_URL", "https://example.com/r.git")
    cfg = get_sandbox_config()
    assert cfg.cpus == 4.0
    assert cfg.memory == "2g"
    assert cfg.network is True
    assert cfg.repo_url == "https://example.com/r.git"


def test_create_passes_resource_limits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    run = [c for c in fake.calls if c[:2] == ["docker", "run"]][0]
    assert "--cpus" in run and "2.0" in run
    assert "--memory" in run and "1g" in run
    assert "--memory-swap" in run and "1g" in run
    assert "--pids-limit" in run and "512" in run
    assert "--network" in run and "none" in run
    assert "--tmpfs" in run and "/tmp" in run
    assert "-v" in run and f"{ws}:/workspace" in run
    assert "--name" in run and mgr.name in run
    assert run[-2:] == ["sleep", "infinity"]


def test_create_network_optin(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(network=True), str(ws), docker_runner=fake.runner)
    mgr.create()
    run = [c for c in fake.calls if c[:2] == ["docker", "run"]][0]
    assert "--network" not in run


def test_create_installs_requirements(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    assert fake.called("docker", "exec", mgr.name, "pip", "install",
                       "-r", "/workspace/requirements.txt")


def test_create_clones_repo_when_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(repo_url="https://example.com/r.git"),
                         str(ws), docker_runner=fake.runner)
    mgr.create()
    assert fake.called("docker", "exec", mgr.name, "git", "clone",
                       "https://example.com/r.git", "/workspace")


def test_create_skips_clone_when_not_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(repo_url="https://example.com/r.git"),
                         str(ws), docker_runner=fake.runner)
    mgr.create()
    assert not fake.called("docker", "exec", mgr.name, "git", "clone",
                           "https://example.com/r.git", "/workspace")


def test_create_missing_workspace(tmp_path):
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(tmp_path / "nope"), docker_runner=fake.runner)
    with pytest.raises(ToolError, match="不存在"):
        mgr.create()


def test_exec_before_create(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    r = mgr.exec("echo hi")
    assert isinstance(r, ToolError)
    assert "未创建" in r.message


def test_exec_wraps_timeout_and_cwd(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    r = mgr.exec("echo hi", cwd=str(ws / "sub"), timeout=7)
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0
    exec_call = [c for c in fake.calls if c[:2] == ["docker", "exec"]][-1]
    wrapped = exec_call[-1]
    assert "timeout -s KILL -k 5s 7 bash -lc" in wrapped
    assert "cd '/workspace/sub' &&" in wrapped


def test_exec_timeout_semantics(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    fake.plan["bash -lc"] = subprocess.CompletedProcess([], 124, "", "timeout")
    r = mgr.exec("sleep 10", timeout=2)
    assert not isinstance(r, ToolError)
    assert r.timeout is True
    assert r.exit_code == -1
    assert r.duration >= 0


def test_exec_oom_semantics(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    fake.plan["bash -lc"] = subprocess.CompletedProcess([], 137, "", "Killed")
    r = mgr.exec("python -c 'x=bytearray(10**9)'")
    assert not isinstance(r, ToolError)
    assert r.oom is True
    assert r.exit_code == 137


def test_destroy_removes_container(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    mgr.destroy()
    assert fake.called("docker", "rm", "-f", mgr.name)
    mgr.destroy()  # 幂等：不重复 rm
    assert fake.count("docker", "rm", "-f") == 1


def test_context_manager_destroys(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    with SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner) as mgr:
        assert mgr._created
    assert not mgr._created
    assert fake.called("docker", "rm", "-f", mgr.name)


def test_docker_executor_run_command_via_manager(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    r = ex.run_command("echo hi", workspace_root=str(ws))
    assert not isinstance(r, ToolError)
    assert r.exit_code == 0


def test_docker_executor_run_tests_parses(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    fake = FakeDocker()
    fake.plan["pytest"] = subprocess.CompletedProcess([], 0, "1 passed in 0.1s", "")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    tr = ex.run_tests(workspace_root=str(ws))
    assert not isinstance(tr, ToolError)
    assert tr.passed == 1
    assert tr.total == 1


def test_docker_executor_run_git(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    fake.plan["git status"] = subprocess.CompletedProcess([], 0, " M a.txt\n", "")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    out = ex.run_git(["status", "--short"], workspace_root=str(ws))
    assert out == " M a.txt\n"


def test_docker_executor_run_git_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = FakeDocker()
    # "git status" 而非 "git"：避免 tmp 目录名含 "git"（test_..._run_git_error）与 docker run 的 -v 路径子串误配
    fake.plan["git status"] = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
    mgr = SandboxManager(SandboxConfig(), str(ws), docker_runner=fake.runner)
    mgr.create()
    ex = DockerExecutor(mgr)
    out = ex.run_git(["status"], workspace_root=str(ws))
    assert isinstance(out, ToolError)
    assert "not a git repository" in out.message


def test_sandbox_executor_local_no_container(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("KA_EXECUTOR", raising=False)
    with sandbox_executor(str(ws)) as ex:
        assert isinstance(ex, LocalExecutor)


def test_sandbox_executor_docker_lifecycle(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    fake = FakeDocker()
    with sandbox_executor(str(ws), docker_runner=fake.runner) as ex:
        assert isinstance(ex, DockerExecutor)
        assert any(c[:3] == ["docker", "run", "-d"] for c in fake.calls)
        r = run_command("echo hi", workspace_root=str(ws))  # 上下文内模块级调用走容器
        assert not isinstance(r, ToolError)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)


def test_sandbox_executor_resets_context(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("KA_EXECUTOR", "docker")
    fake = FakeDocker()
    with sandbox_executor(str(ws), docker_runner=fake.runner):
        pass
    ex = get_executor()
    assert isinstance(ex, ToolError)
    assert "sandbox_executor" in ex.message
