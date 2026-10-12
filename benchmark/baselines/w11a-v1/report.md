# Benchmark Report 20261012-183750

- git: `723c4a524348670c231109ce23c72745aa659134` (feature/pre-extraction-baseline)
- model: `deepseek/deepseek-v4.1-flash` | executor: `docker`
- timestamp: 2026-10-12T18:37:50
- params: {}
- provider: `command_goat` | base_url: `https://api.commandcode.ai/provider/v1`
- temperature: None | max_tokens: None
- executor: 声明 `docker` / 实际 `docker`
- sandbox_policy: {"executor": "docker", "image": "ka-sandbox:py312-v1", "cpus": 2.0, "memory": "1g", "pids_limit": 512, "network": false}
- iteration_limits: {"run_level": null, "per_case": {"schedule-602": 30, "schedule-608": 30, "schedule-622": 30, "schedule-646": 30, "schedule-99": 30}, "fallback_to_run_level": []}
- timeouts: {"KA_SETUP_TIMEOUT_S": 60, "KA_LLM_TIMEOUT_S": null, "KA_LLM_MAX_RETRIES": null}
- resolution_definition_version: `t3-1`
- side_effect_policy: `read_only`
- judge_prompt_sha256: `5aabbf69a988c45bb16c91c1dc99c3fe471c184fe287a1e6ebea5d63feed1fa0`
- fixture_commits: {"schedule-602": {"pinned": "82a43db1b938d8fdf60103bd41f329e06c8d3651", "actual": "82a43db1b938d8fdf60103bd41f329e06c8d3651"}, "schedule-608": {"pinned": "82a43db1b938d8fdf60103bd41f329e06c8d3651", "actual": "82a43db1b938d8fdf60103bd41f329e06c8d3651"}, "schedule-622": {"pinned": "82a43db1b938d8fdf60103bd41f329e06c8d3651", "actual": "82a43db1b938d8fdf60103bd41f329e06c8d3651"}, "schedule-646": {"pinned": "82a43db1b938d8fdf60103bd41f329e06c8d3651", "actual": "82a43db1b938d8fdf60103bd41f329e06c8d3651"}, "schedule-99": {"pinned": "82a43db1b938d8fdf60103bd41f329e06c8d3651", "actual": "82a43db1b938d8fdf60103bd41f329e06c8d3651"}}

## 核心指标

**Issue Resolution Rate: 40%** (2/5)

**Adjusted Resolution Rate: 50%** (2/4)

> 口径说明：Resolution Rate 分母为全部案例（raw，含 error / environment_error）；Adjusted Resolution Rate 分母排除执行异常（error）与基线环境错误（environment_error），仅统计实际运行 Agent 的案例，代表被测能力口径；分母为 0 时 Adjusted 记 0.0。

**环境错误: 0**（基线测试失败，未运行 Agent）

## 分类统计

| 类别 | 任务数 | 解决 | 解决率 | 环境错误 |
|------|--------|------|--------|----------|
| bug | 2 | 1 | 50% | 0 |
| feature | 1 | 1 | 100% | 0 |
| refactor | 1 | 0 | 0% | 0 |
| test | 1 | 0 | 0% | 0 |

## 明细

| case_id | 类别 | 状态 | 测试 | 接受 | Judge | 迭代 | 耗时(s) | 成本($) | 失败摘要 | 交付一致 |
|---------|------|------|------|------|-------|------|---------|---------|----------|---------|
| schedule-602 | test | error | FAIL | FAIL | SKIP | 0 | 68.6 | unknown |  | 一致 |
| schedule-608 | bug | not_resolved | PASS | FAIL | SKIP | 24 | 358.1 | unknown |  | 一致 |
| schedule-622 | refactor | not_resolved | PASS | FAIL | SKIP | 11 | 129.3 | unknown |  | 一致 |
| schedule-646 | bug | resolved | PASS | PASS | PASS | 24 | 370.2 | unknown |  | 一致 |
| schedule-99 | feature | resolved | PASS | PASS | PASS | 31 | 489.9 | unknown |  | 一致 |

## Judge 理由

### schedule-602 (SKIP)

SKIP 执行异常

### schedule-608 (SKIP)

SKIP 无代码变更

### schedule-622 (SKIP)

SKIP 无代码变更

### schedule-646 (PASS)

PASS



理由：

- 两个 patch 都针对同一缺陷：`Job.__repr__` 中在 `self.interval == 1` 时对可能为 `None` 的 `self.unit` 执行切片 `self.unit[:-1]`，导致 `TypeError: 'NoneType' object is not subscriptable`。

- 参考（Gold）patch 在两处 repr 分支（`at_time is not None` 分支与常规分支）把判断改为 `self.unit and self.interval == 1`；Agent patch 先计算一个 `unit` 变量，条件为 `self.interval == 1 and self.unit is not None`，然后在两处分支统一使用该变量。两者的守卫语义等价：`unit` 为 `None` 时都不切片、直接返回原值，正常复数单位仍会去掉尾部 "s"。边界情况（如空字符串）结果也一致（`""[:-1] == ""`）。

- 仅实现写法不同（提取局部变量 vs 就地条件），行为与覆盖面一致，Agent patch 还补充了回归测试。

- 因此 Agent patch 修复了 Gold patch 所针对的同一缺陷，功能等价。

### schedule-99 (PASS)

PASS



Agent patch 与 Gold patch 解决的是同一问题：为任务增加“仅在周末”或“仅在工作日（周一到周五）”执行的调度能力。



两者核心功能等价：

- Agent patch 提供了 `.weekend`、`.workweek`（以及 `.workdays`、`.weekends` 别名）；

- Gold patch 提供了 `.weekend`、`.workday`；

- 命名和内部表示不同（Agent 用字符串集合，Gold 用整数集合），但都通过限制可运行星期几来实现相同调度语义。



Agent patch 同样包含了：

- 对间隔不为 1 的日常任务抛出 `IntervalError`；

- 在计算下次运行时间时跳过不允许的星期几；

- 对 `.at()` 时间格式的适配；

- 相应的测试覆盖。



因此，虽然实现细节和部分属性命名不同，但功能点一致，判定为 PASS。
