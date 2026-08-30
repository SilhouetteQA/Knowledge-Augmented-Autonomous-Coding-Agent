"""红线模式集（审查 M23 下沉）：工具层与 Agent 层共用的单一来源。

tools/approval.py 此前在审批执行时反向 import agent.issue 的私有函数
（工具层依赖 Agent 层的分层倒置 + 运行时 ImportError 面）；下沉后两者均从
本模块导入。
"""
import os

# 命中模式的文件被 _enforce_red_lines 从工作树还原，不得进入 diff/审查/审批单。
# cost_log：output/eval/cost_log.jsonl 类运行成本日志（运行评测/提取脚本的副作用）；
# generated_at：v3_seed 等数据文件 _meta.generated_at 行的误改。
RED_LINE_PATTERNS = ("cost_log", "generated_at")


def red_line_patterns() -> tuple[str, ...]:
    """红线模式集：默认常量；KA_REDLINE_PATTERNS（逗号分隔子串）覆盖，空/未设 → 默认。"""
    raw = os.environ.get("KA_REDLINE_PATTERNS", "").strip()
    if not raw:
        return RED_LINE_PATTERNS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# 兼容别名（agent.issue 历史私有名，既有调用点不破坏）
_red_line_patterns = red_line_patterns
