# scripts/b13_probe.py
"""B13 探针：docker 执行器下 setup 失败与基线失败两种形态的一次性验证。

验证目标（台账 B13/E3-a）：
  1. case.setup_commands 失败 → case 收敛为 error，errors[0] 含「setup 命令失败」；
  2. setup 成功但基线 must_pass 失败 → status=environment_error，未运行 Agent；
用法：KA_EXECUTOR=docker python scripts/b13_probe.py（需 docker daemon 与沙箱镜像）
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["KA_EXECUTOR"] = "docker"

from agent.llm import MockLLMClient  # noqa: E402
from benchmark import runner as runner_mod  # noqa: E402
from benchmark.loader import BenchmarkCase  # noqa: E402
from tools.github_tools import GitHubIssue  # noqa: E402

WS = os.path.join("output", "b13-probe", "workspace")
REPO = os.path.join(WS, "local__probe")


def _prepare_repo() -> None:
    os.makedirs(REPO, exist_ok=True)
    with open(os.path.join(REPO, "test_fail.py"), "w", encoding="utf-8") as f:
        f.write("def test_always_fails():\n    assert False, 'B13 探针：基线必挂'\n")
    if os.path.isdir(os.path.join(REPO, ".git")):
        return   # 幂等：重跑时仓库已就绪
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=REPO, check=True)
    subprocess.run(["git", "add", "-A"], cwd=REPO, check=True)
    subprocess.run(["git", "-c", "user.email=p@p", "-c", "user.name=p",
                    "commit", "-qm", "probe"], cwd=REPO, check=True)


def _case(setup_commands: list[str]) -> BenchmarkCase:
    return BenchmarkCase(
        id="probe-b13", category="bug", repository="local/probe",
        issue=GitHubIssue(number=1, title="B13 探针", body="",
                          labels=[], state="open"),
        gold_patch="", must_pass=["test_fail.py"],
        setup_commands=setup_commands)


def main() -> int:
    _prepare_repo()
    # 评测 runner 的克隆/同步环节（gh api）与本探针无关：仓库已本地就绪，置空
    runner_mod._ensure_repository = lambda *a, **k: None

    ok = True
    r1 = runner_mod._run_one_case(_case(['python -c "import sys; sys.exit(3)"']),
                                  MockLLMClient([]), ".", WS)
    print(f"[1] setup 失败形态: status={r1.status} errors={r1.errors}")
    ok &= r1.status == "error" and any("setup 命令失败" in e for e in r1.errors)

    r2 = runner_mod._run_one_case(_case([]), MockLLMClient([]), ".", WS)
    print(f"[2] 基线失败形态: status={r2.status} errors={r2.errors}")
    ok &= r2.status == "environment_error" and any("基线测试失败" in e for e in r2.errors)

    print("B13 探针:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
