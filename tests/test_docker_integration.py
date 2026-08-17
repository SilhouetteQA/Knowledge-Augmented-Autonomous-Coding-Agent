# tests/test_docker_integration.py
"""Docker 沙箱集成测试：需要 Docker Desktop 运行（无 Docker 时自动跳过）。"""
import shutil
import subprocess
import time

import pytest

from tools.docker_sandbox import DEFAULT_IMAGE, SandboxConfig, SandboxManager
from tools.file_tools import ToolError
from tools.shell_tools import LocalExecutor, git_status


def _docker_available() -> bool:
    """docker CLI 存在且 daemon 可达。"""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker 不可用"),
]


def test_build_sandbox_image():
    """镜像可构建（docker build 幂等，利用缓存）。"""
    r = subprocess.run(
        ["docker", "build", "-t", DEFAULT_IMAGE, "."],
        capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    r2 = subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE],
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0


def _new_manager(tmp_path, **cfg):
    """在 tmp workspace 上创建真实沙箱管理器（配置可覆盖）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    return SandboxManager(SandboxConfig(**cfg), str(ws)), ws


def test_lifecycle_exec_and_no_residue(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        r = mgr.exec("echo hello-from-sandbox")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert "hello-from-sandbox" in r.stdout
    # 销毁后无残留
    probe = subprocess.run(["docker", "ps", "-a", "--filter", f"name={mgr.name}", "--format",
                            "{{.Names}}"], capture_output=True, text=True, timeout=30)
    assert mgr.name not in probe.stdout


def test_command_timeout_terminated(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        start = time.monotonic()
        r = mgr.exec("sleep 10", timeout=2)
        elapsed = time.monotonic() - start
        assert not isinstance(r, ToolError)
        assert r.timeout is True
        assert r.exit_code == -1
        assert elapsed < 9  # 2 秒超时 + 5 秒宽限内


def test_memory_oom_terminated(tmp_path):
    mgr, ws = _new_manager(tmp_path, memory="64m")
    with mgr:
        r = mgr.exec("python -c \"x = bytearray(300 * 1024 * 1024)\"")
        assert not isinstance(r, ToolError)
        assert r.oom is True
        assert r.exit_code == 137


def test_network_isolated(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    with mgr:
        r = mgr.exec("python -c \"import urllib.request; "
                     "urllib.request.urlopen('https://example.com', timeout=5)\"")
        assert not isinstance(r, ToolError)
        assert r.exit_code != 0  # --network none 下连接失败


def test_workspace_mount_sync(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    (ws / "host.txt").write_text("from-host", encoding="utf-8")
    with mgr:
        r = mgr.exec("cat /workspace/host.txt")
        assert not isinstance(r, ToolError)
        assert "from-host" in r.stdout
        r2 = mgr.exec("echo from-container > /workspace/container.txt")
        assert not isinstance(r2, ToolError) and r2.exit_code == 0
    assert (ws / "container.txt").read_text(encoding="utf-8").strip() == "from-container"


def test_git_in_container_matches_host(tmp_path):
    mgr, ws = _new_manager(tmp_path)
    # 宿主侧初始化仓库并提交
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    (ws / "a.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True)
    (ws / "a.txt").write_text("v2", encoding="utf-8")
    with mgr:
        # 容器内 git_status 与宿主一致（filemode=false 注入后无 mode change 噪音）
        host_status = git_status(workspace_root=str(ws))
        container_out = mgr.exec("git status --short")
        assert not isinstance(container_out, ToolError)
        assert not isinstance(host_status, ToolError)
        # 容器与宿主按「行」比对（git status --short 输出逐行一致）
        container_lines = [l for l in container_out.stdout.splitlines() if l.strip()]
        assert sorted(container_lines) == sorted(host_status.changes)


def test_executor_consistency(tmp_path):
    """同一命令在 local 与 docker 执行器下输出一致（W3 spec §8.3）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    local = LocalExecutor().run_command("python -c \"print('same')\"", cwd=str(ws),
                                        workspace_root=str(ws))
    mgr = SandboxManager(SandboxConfig(), str(ws))
    with mgr:
        docker = mgr.exec("python -c \"print('same')\"")
    assert not isinstance(local, ToolError) and not isinstance(docker, ToolError)
    assert local.exit_code == docker.exit_code
    assert local.stdout.strip() == docker.stdout.strip()


def test_pip_install_at_create(tmp_path):
    """创建阶段自动安装 requirements.txt（创建期网络可用）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    mgr = SandboxManager(SandboxConfig(), str(ws))
    with mgr:
        r = mgr.exec("python -c \"import six; print(six.__version__)\"")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert "1.16.0" in r.stdout


def test_clone_repo_when_empty(tmp_path):
    """workspace 为空 + repo_url 配置 → 创建阶段克隆（公网小仓库）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SandboxManager(
        SandboxConfig(repo_url="https://github.com/octocat/Hello-World.git"), str(ws))
    with mgr:
        r = mgr.exec("git -C /workspace log --oneline -1")
        assert not isinstance(r, ToolError)
        assert r.exit_code == 0
        assert r.stdout.strip() != ""
