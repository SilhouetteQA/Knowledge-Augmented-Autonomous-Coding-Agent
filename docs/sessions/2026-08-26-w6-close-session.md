# W6 Evaluation 窗口会话归档（2026-08-25 ~ 2026-08-26）

> 归档范围：W6 窗口完整生命周期（brainstorming → spec → plan → SDD 实施 → 真实验收 → 合并）。
> 关联：`docs/specs/2026-08-25-w6-evaluation.md`、`docs/plans/2026-08-25-w6-evaluation.md`、`docs/devlog.md`（W6 条目）、`docs/roadmap.md`（W6 [x]）。

## 一、交付总结

**W6 Evaluation 已合并回 main（76cae48），分支与 worktree 已清理。全量回归 207 passed / 4 skipped / 1 环境性失败。**

| 交付物 | 说明 |
|--------|------|
| `benchmark/` 包 | loader（案例 schema 逐键校验）/ judge（LLM-as-a-Judge 等价性）/ report（metadata+JSON+Markdown+compare）/ runner（双判定+八项指标采集） |
| `benchmark/cases/` | 5 个真实基准案例（bug×2/feature/test/refactor，真实 GitHub Issue 快照 + gold patch） |
| `tools/tracing.py` | Langfuse 可开关 trace（懒加载/上下文管理器/usage 记录），LLM generation + issue/benchmark 埋点 |
| `agent/issue.py` | issue_snapshot 离线注入 seam（评测可离线路由） |
| `agent/llm.py` | token 用量累计 + generation 埋点 |
| `main.py` | `--benchmark / --cases / --executor / --benchmark-out / --compare` |
| `docker/langfuse/` | Langfuse v4 compose + .env.example 模板（密钥不入库） |
| 文档 | spec/plan/devlog/roadmap 全部更新，验收标准达成情况如实记录 |

## 二、关键指标（真实评测，docker 执行器）

**run 20260826-034824（修复闭环后）：Resolution Rate 20%（1/5）**

| case | 类别 | 结果 | 关键信号 |
|------|------|------|---------|
| schedule-608 | bug | **resolved** | test=True + Judge PASS（25 迭代，1665s） |
| schedule-646 | bug | not_resolved | Judge PASS + accept=True（修复等价确认），test=False |
| schedule-99 | feature | not_resolved | Judge PASS + accept=True，test=False |
| schedule-602 | test | not_resolved | Judge SKIP（无代码变更） |
| schedule-622 | refactor | not_resolved | Judge SKIP，test=False |

- **test_pass=False 根因**：沙箱镜像缺 pytz 依赖 → schedule 时区测试群 40 项失败（Agent 修复等价性已由 LLM Judge 确认，非能力结论）
- **评测演进**：run1（002247，克隆全失败=环境）→ run2（004155，克隆通但沙箱受限）→ run3（034824，全链路真实运行，20%）
- 单 case 成本实况：1-5M prompt tokens / 341-1665s（真实 LLM 消耗记录）

## 三、关键决策与经验

1. **执行器硬约束**：docker 执行器下 `run_tests` 必须在 `sandbox_executor` 上下文内（ContextVar _CURRENT_EXECUTOR）；runner 的 test_pass 判定阶段需独立沙箱——W6 实测发现并修复（09a2fb8）。
2. **DockerExecutor 路径解析**：相对必须 pass 路径以宿主 CWD 为基准解析会越界——runner 改为绝对路径后闭环（ed4a709）。
3. **评测基准依赖缺口**：must_pass 目标仓库依赖（pytz）需预装或 case 级 pip install；建议评测前跑原仓库基线测试确认环境完整。
4. **网络环境**：本机 github.com 主站 SNI 阻断，api.github.com/codeload 可达；git 全局残留死代理（Clash 7892）。解法：`git config --global --unset http.proxy` + `url."https://ghfast.top/https://github.com/".insteadOf`。schedule 测试需 docker（Windows 宿主 time.tzset 崩溃）。
5. **Langfuse v4 events_only**：兄弟项目部署为 events_only 模式，经典读接口 404，SDK 4.14.4 自动适配事件通道落库（events_core 实证）。
6. **质量门禁**：SDD 全程 10 任务实施 + 每任务独立审查 + 多轮修复（loader IMP-1、tracing metadata_fn 上下文管理器、judge 词边界+注入面、runner 沙箱+路径、Tool Success Rate 采集、PASS 标点边界）+ 最终全分支双轴审查（2 Important 已修）。
7. **模型实测**：mimo-v2.5 真实 tool calling 与多轮迭代稳定（5 case 全跑完，最长 1665s/25 迭代）。

## 四、遗留问题（供后续窗口）

- **评测基准改进**：沙箱镜像预装 pytz 或 case 级依赖安装；test_pass=False 报告补失败摘要（当前只记 bool）；评测前基线测试预检。
- **domain 类案例**：暂缺 1 例（Arknights 领域逻辑 issue，需可 clone 仓库）。
- **单价表**：MODEL_PRICE_USD_PER_1K 空表 → 成本列 0（待用户提供 mimo-v2.5 实际单价）。
- **schedule-646 resolution=True 锚点**：未在评测中重证（pytz 缺口），人工可复核（W5 已验证 Agent 可解）。
- **W7 衔接**：tools/tracing.py 接口（is_enabled/get_client/flush/traced/record_usage）已就绪；case 级 span（case_id metadata）埋点、tracing.flush() 接线、Langfuse v4 读接口复核列入 W7。

## 五、W7 入口指引（下会话）

1. 刷线：readme → docs/devlog.md（W6 条目 + 遗留）→ docs/roadmap.md（W7 待开始）。
2. worktree 已就绪：`.worktrees/w7-observability` / 分支 `feature/w7-observability`（spec 草稿已在：`docs/specs/2026-08-25-w7-observability.md`，brainstorming 三项决策已确认：Langfuse 为主+OTLP 验证 / 完整链路埋点 / 本地汇总报告+UI）。
3. W7 spec §11 待完善项：核对 W6 实际 tracing.py 接口与埋点清单匹配、graph 节点函数名与行号、CLI 命名、与 W6 报告联动。
4. 环境备忘：LLM 配置（opencode_go_api + OPENCODE_GO_BASE_URL + mimo-v2.5 + NO_PROXY）；Docker daemon 需启动（`<local-disk-path>\Docker Desktop.exe`）；git 镜像 insteadOf 仍生效（github.com 不可直连）；评测命令须 `--executor docker`。