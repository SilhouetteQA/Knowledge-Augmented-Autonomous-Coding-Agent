# W6+W7 综合会话归档与下会话入口（2026-08-26）

> 本文件是 W6（Evaluation）+ W7（Observability）窗口合并完成后的会话收尾归档，
> 同时是下会话（W8 审批 + 评估问题修复）的**中心入口文档**。
> 窗口级细节归档见 `2026-08-26-w6-close-session.md` 与 `2026-08-26-w7-close-session.md`。

## 一、本会话完成内容（W6 + W7 全部落地）

- **W6 Evaluation**（合并 76cae48）：基准案例库 5 个、评测运行器（双判定）、LLM-as-a-Judge、`--benchmark/--compare`、Langfuse 可开关 tracing；真实评测 Resolution Rate 20%（1/5）。
- **W7 Observability**（合并 4035ce8）：graph 七节点 + 工具 + 测试 + review 全链路埋点、retry/verify 指标、`--trace-report` 导出（ClickHouse 直查）、OTLP 贯通验证；全量回归 230 passed。
- 全量回归最终：229 passed / 4 skipped / 1 环境性失败（容器内 github TLS，非代码回归）。
- readme 状态行已更新：**W0-W7 已完成**，仅剩 W8（Human-in-the-loop）。

## 二、评估暴露的能力画像（W6 真实数据结论）

| 能力域 | 证据 | 评定 |
|--------|------|------|
| 明确 bug 修复 | W2/W5/W6-608 | ✅ 最强项 |
| 依赖自举（写降级模块） | W3 | ✅ 已验证 |
| 代码理解/定位/分析 | W1/W2/W4 | ✅ 可靠 |
| 领域知识结合检索 | W4 | ✅ 已落地 |
| feature 实现 | W6-99（Judge 确认等价） | ⚠️ 实现可但被环境误伤 |
| 开放型任务（补测试/重构） | W6-602/622（diff 空） | ❌ 真实短板 |
| 长任务收敛 | W5/W6 多次触达 30 迭代上限 | ⚠️ 探索开销大 |

## 三、评估与观测暴露的问题清单（下会话按此处理）

### P0（先修，一修即可提升评测可信度）

1. **test_pass 环境性误伤（pytz 缺失）**：沙箱镜像预装目标仓库依赖（schedule→pytz 进 Dockerfile），或 case 级 setup_commands（pip install -r requirements-dev.txt）——基准责任（SWE-bench 做法：环境先装好再测）。
2. **基线测试预检**：评测先跑"原仓库未修改"的 must_pass；基线即红（依赖缺失）→ case 标注 `environment-error` 而非 Agent 失败——把环境缺口与 Agent 能力分离（646/99 的 test=False 属此类误判）。

### P1（提升可观测性与稳定性）

3. **test_pass=False 无失败详情**：report 补 failures 摘要（当前只记 bool，阻塞定位）。
4. **LLM 调用无重试**：OpenAICompatClient 配 timeout/max_retries（W5 APITimeoutError 整单崩溃遗留）。

### P2（随排期）

5. 单价表 `MODEL_PRICE_USD_PER_1K` 空（用户提供 mimo-v2.5 实际单价）。
6. resolution_rate 分母含 error case 的口径决策。
7. 开放型任务收敛引导（提示词/计划节点按任务类型；迭代上限按类型配置）。
8. case 级 span（case_id metadata）补埋（spec §4.6）。
9. 无效 trace_id 返回空报告 rc=0 → 应 exit 1。
10. Agent 顺手改动（docstring 改坏）Review 未发现 → Review 提示词加格式/文档回归检查。
11. 验证脚本非正式测试（test_fix.py 而非并入 test_schedule.py）→ Review 提示词要求测试并入既有套件。
12. 容器内 github TLS 偶发（镜像/网络层）。

## 四、下会话执行顺序（用户指定）

1. **先做 W8 Human-in-the-loop（审批门禁）**：
   - 创建 worktree `.worktrees/w8-human-in-the-loop` / 分支 `feature/w8-human-in-the-loop`（从 main@79aa431）；
   - 完整 Superpowers 流程：brainstorming（一次一问：Reviewer Agent 独立角色 / diff 展示与人工审批界面形态 / 审批门禁后的 act 范围：commit→push→PR 全自动 or 分步）/ spec → 用户批准 → plan → TDD 实施；
   - 验收标准（roadmap）：Agent 完成工作后停下等待审批；审批后自动完成 PR；演示完整闭环。
   - **联动**：知识纠错第二阶段（W5 冻结）依赖 W8 HITL 门禁就绪后可解冻排期。
2. **然后处理评估问题**：按 P0 → P1 → P2 顺序（P0 依赖预装 + 基线预检一次 commit 即可；P1 失败摘要 + LLM 重试；P2 逐项）。修复在 W6 遗留归属——建议在 W8 worktree 完成后开一个小窗口（或并入 W8 后段），以独立 commit 进入 main。

## 五、环境备忘（W8 会话前置检查）

- LLM：opencode_go_api / OPENCODE_GO_BASE_URL / OPENCODE_GO_MODEL=mimo-v2.5 / NO_PROXY（主仓库 .env）。
- Docker daemon：`<local-disk-path>\Docker Desktop.exe`（沙箱需提权启动；服务 com.docker.service）。
- git 镜像：`url.https://ghfast.top/https://github.com/.insteadOf` 已全局生效（github.com 主站 SNI 阻断）；git 全局死代理（<local-proxy>）已清除。
- Langfuse：兄弟项目栈 6 容器在跑（http://localhost:3000，v4 events_only）；ClickHouse 8123（口令仅运行时注入）。
- venv：主仓库 `.venv` 共享（含 langfuse 4.14.4 / clickhouse-connect 1.7.1）。
- 已知环境性测试失败：`test_docker_integration::test_clone_repo_when_empty`（容器内 github TLS）。
- schedule 测试必须 `--executor docker`（Windows 宿主 time.tzset 崩溃）。