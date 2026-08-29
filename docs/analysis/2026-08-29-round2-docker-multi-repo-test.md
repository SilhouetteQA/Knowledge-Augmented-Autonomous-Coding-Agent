# 第二轮实测报告：修复后 Agent × Docker 沙箱 × 三仓库（2026-08-29）

> 前置：第一轮（docs/analysis/2026-08-29-real-issue-capability-test.md）定位四瓶颈 → 当日全部修复并合并 main（环境声明 / edit_file / 破坏性 git 拦截 / Reviewer 测试证据面，+F4 盲点、G1 LLM 优雅降级、G3 任务级环境准备钩子、G8 reasoning_content 回传、KA_TEST_TIMEOUT_S 等清偿项）。
> 本轮环境：docker 沙箱（ka-sandbox:py312-v1x 派生镜像，含 pytest 8.3.5 / pytz / six / hypothesis / lxml 等测试依赖）、执行器全 docker、模型 mimo-v2.5 →（订阅周上限后）deepseek-v4-flash @ api.deepseek.com。
> 遥测：output/exp/*.json（逐步工具调用 + 审查 + diff）。**按用户指示，全部结果仅留档，暂不审批。**

## 一、结果总览

| # | 仓库 / Issue | 规模 | 模型 | 结果 | 迭代 | 审查 |
|---|--------------|------|------|------|------|------|
| 1 | executablebooks/markdown-it-py #415（blockquote 尾表格 IndexError） | 中 | mimo | **完整解决** | **9** | PASS |
| 2 | dateutil/dateutil #1545（isoparse 截断小数秒，应四舍五入） | 中 | mimo→deepseek | 3 轮未收敛（v1 环境污染 / v2 触顶但源码修复正确 / v3 预算纪律失效） | 41/35/41 | PASS(v2 误判)/FAIL |
| 3 | SilhouetteQA/Arknights-LLM-wiki #4（试点三：3 条死数据候选） | 域任务 | mimo(超时崩溃)→deepseek(400 阻断) | 未交付（v3 重跑中） | 45+/39 | FAIL(准确) |

## 二、案例 1：markdown-it-py#415 —— 修复后的标杆样本

9 迭代闭环：3 步定位（read_file×6 集中在两个规则文件）→ **edit_file ×2 精确修改**（第一轮实测的 write_file 全量覆盖灾难未再出现）→ 全量 981 passed → Reviewer PASS。diff 恰好 2 文件 8 行，零残留，原始复现脚本通过。**四项修复在该案例全部正反馈**：环境声明（无 /testbed 幻觉）、edit_file（无覆盖灾难）、git 拦截（未触发=无自伤）、Reviewer 证据（新增测试文件未被误判——本例未新增测试文件，属同类规避）。

## 三、案例 2：dateutil#1545 —— 环境与模型的双重放大镜

三轮各暴露一层（问题递进，价值都在暴露面）：

- **v1（mimo）**：沙箱镜像 pre-install 的 python-dateutil 遮蔽仓库源码 → Agent 自适应发现 import 指向 site-packages（聪明），靠 cp 进 site-packages 绕过；verify 仍 13 收集错误 → 触顶。**基线 2032 green 是假绿（测的是安装版不是源码）**。产出：F4 盲点修复（total=0 且 error>0 必须出证据）+ G3 钩子设计。
- **v2（mimo，钩子前）**：Agent 用 PYTHONPATH=src 自适配全部命令，源码修复**正确**（123457+进位链，独立验证通过）；但 verify 节点是系统裸跑，绕不过 → 3 verify 轮失败 → 触顶；**Reviewer 误 PASS**（F4 修复已在 main 但本轮无异常事实可用时 Reviewer 仍凭 diff 好看放行）。
- **v3（deepseek，钩子后）**：环境已就绪（editable 安装生效），Agent 却转入"上游考古"——pip download / curl GitHub / git show master 查上游是否已修，**~40 轮全部耗在调查，预算纪律（前 1/3 探索、后 2/3 执行）被模型无视**；最后一笔 edit_file 还打在 `_DATE_SEP` 常量（错误位置）。触顶 FAIL。
- 源码里其实留有 v2 的正确修复（123457 达成）。**结论：环境问题修完后，dateutil 的剩余瓶颈是模型的预算纪律与目标聚焦——mimo v2 版已证明可解，deepseek 不如 mimo 专注。**

## 四、案例 3：Arknights-LLM-wiki#4 —— 两次生产级故障模式的诞生地

- **v1（mimo）**：45+ 轮后 **APITimeoutError 击穿 decide 节点**，整个 run 崩溃（遥测丢失、工作树现场保留）。根因=50 轮域任务上下文膨胀 + mimo 订阅周限。→ **G1 修复**（plan/decide/reflect 三节点 LLM 异常 → 优雅终态）。
- **v2（deepseek）**：G1 首战生效——第 39 轮 deepseek thinking 模式 400（"reasoning_content must be passed back"）→ **run 优雅收尾**（终态结论写入 final_answer、Review 完整执行、FAIL 判定准确）。39 轮全耗在核验与 git 考古（ls-tree HEAD 对比 PR#3 状态），无交付。→ **G8 修复**（reasoning_content 随 LLMMessage 回放带回，仅端点返回时带回，mimo 不受影响）。
- Reviewer 附加发现：兄弟项目套件在容器内 2 errors（0 passed/0 failed/2 error）——测试基线在容器内不绿，回归验收语义受损（遗留：兄弟项目容器内基线核清）。
- v3（G8 修复后）重跑进行中，结果另记。

## 五、本轮沉淀的产品修复（全部已提交 main，回归 385 passed）

| # | 修复 | 触发来源 |
|---|------|----------|
| G1 | plan/decide/reflect LLM 异常 → 优雅终态（不再击穿 run） | arknights v1 崩溃 |
| G3 | KA_ISSUE_SETUP_COMMANDS 任务级环境准备钩子（沙箱内、300s/条） | dateutil src 布局 |
| G8 | reasoning_content 回传 | deepseek thinking 400 |
| F4b | 测试证据事实补"验证异常"形态（total=0 且 error>0） | dateutil Reviewer 误 PASS |
| 清偿 | KA_TEST_TIMEOUT_S（run_tests 超时可配）/ approval_id 随机后缀 / load 字符集校验 / _run→run_host 公开化 / E5-1 透传测试 / 报告 ✓✗→中文 | 各轮 Minor 台账 |

## 六、环境备忘（镜像与基线的教训）

1. **派生镜像 = 新的依赖遮蔽面**：往沙箱镜像装"测试依赖"时，任何与目标仓库同名的包都会遮蔽仓库源码（dateutil 教训）——镜像依赖清单必须避开常见仓库名，或任务级 editable 安装覆盖（G3 钩子 + KA_ISSUE_SETUP_COMMANDS="pip install -e . --no-build-isolation"）。
2. **连续 bake 造成 pytest 版本漂移**（8.3.5→8.4+→回钉），派生镜像用 `pip install pkg==ver` 钉死。
3. 容器 root 写文件（__pycache__/.pytest_cache）宿主难清 → 统一用一次性 root 容器清理；实验命令带 PYTHONDONTWRITEBYTECODE=1。
4. 假绿基线：**"全量测试通过"必须确认测的是仓库源码**（import 路径核验），否则 verify 与基线同空转。

## 七、未完结项（移交审查与计划）

见 `docs/analysis/2026-08-29-unfinished-ledger.md`（本轮 + 历轮全量台账）。
