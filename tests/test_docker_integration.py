# tests/test_docker_integration.py
"""Docker 沙箱集成测试：需要 Docker Desktop 运行（无 Docker 时自动跳过）。"""
import shutil
import subprocess

import pytest

from tools.docker_sandbox import DEFAULT_IMAGE


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
