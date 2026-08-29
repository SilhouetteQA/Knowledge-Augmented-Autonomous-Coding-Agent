# 真实 GitHub Issue 能力实测：每轮能力边界与大仓库应对（2026-08-29）

> 动机：用户质询——"30/60 轮解不完一个问题，遇到大型仓库/复杂 issue 是否完全没办法？"
> 方法：在不改产品代码、不碰远端写操作（无 push/PR）前提下，选两个**仍开放**的真实 bug issue（HEAD 上必然未修复，避免已修复 issue 空跑），以 mimo-v2.5 + local 执行器跑完整 --issue 干跑链路（克隆→分支→工作→测试→Reviewer），用驱动脚本 dump 全部步骤遥测（output/exp/run_real_issue.py，不入库）。
> 仓库/模型：Textualize/rich @9d8f9a3、python-jsonschema/jsonschema @2d7d41e；mimo-v2.5（.env 默认）。

## 一、每轮能力边界（代码事实，agent/graph.py）

- 一轮迭代 = `decide`（LLM 看全部历史 + 11 个工具，产出 1-N 个工具调用）+ `execute`（执行这些调用）。**轮数本质是"LLM 决策次数/工具调用批次"预算**。
- 测试只在两种时机运行：Agent 停止调用工具（verify 节点，最多 3 轮，失败进 reflect 再决策）或迭代触顶（finalize_iter_limited 强制一次）。40 轮里 Agent 可能只主动 run_tests 一两次。
- 迭代触顶 = "已达到上限，任务未完成"（status=limit），工作树现场保留，Reviewer 据实审查。
- 上下文无压缩：全部历史逐轮重发，工具结果上限 100KB（run_command）/500KB（read_file 拒读更大）。

## 二、实测结果

### 案例 1：jsonschema#1159（multipleOf 整值浮点失败）——实质解决

- 27 迭代（预算 40，剩 13 富余）、verify 1 轮、未触顶。
- 过程：3 步定位到 `jsonschema/_keywords.py`（search_code→read_file）→ 修复（整值浮点分母转 int 走精确整数路径）→ 新增回归测试 `jsonschema/tests/test_multipleof_issue.py`（在套件目录内）→ 全量 verify **7892 passed / 0 failed**（基线 7881 + 11 新测试）。
- 独立复核：原始复现脚本通过；`multipleOf: 0.0` 边界由 metaschema exclusiveMinimum 拦截，无 0 除回归。
- **Reviewer FAIL 属误判**：理由"测试未并入正式套件"不成立（新文件就在 `jsonschema/tests/` 且被全量 verify 收集运行）；另一条边界担忧（11.000000000000001）为推测性。按 W8 流程这会触发一轮 27 迭代的无谓重跑。

### 案例 2：rich#3299（Segment._split_cells 非单位字符切分错误）——触顶失败

- 两轮各 41 迭代全部触顶，Reviewer FAIL（判定准确：源文件未改、18 个残留脚本）。
- 失败链条（遥测逐步还原）：
  1. 第 1-6 步定位高效（search→read segment.py/cells.py，5 步内完成）；
  2. 第 7-9 步幻觉 SWE-bench 式 `cd /testbed`（本环境无此目录）；
  3. **第 23 步 write_file 全量重写 tests/test_segment.py，丢失全部原有测试**（工具集只有全量覆盖型 write_file，无局部编辑/追加）；
  4. 第 29/32 步用 run_command 跑 `git stash` 自伤（stash 掉自己的修改后试图恢复）；
  5. 第 29-40 步陷入"恢复现场/字节对比"泥潭，从未回到 rich/segment.py 本体。

## 三、结论（回答"大仓库/复杂 issue 是否没办法"）

1. **导航与定位不是瓶颈**：两个案例都在 ≤6 步内找到正确文件；大仓库的"理解成本"被 search_code/read_file 覆盖得比预想好。
2. **瓶颈是每轮质量，不是轮数**：rich 用满约 80 轮也没走到修复那一步（每轮都在还上一轮的债）；jsonschema 27 轮解出还有富余。30/60 轮解不出 Arknights 域任务另有原因（域判断+deepseek 模型故障，见 W8 记录），不能直接外推为"编码能力不足"。
3. **四个具体失败模式（都有便宜修法）**：
   - 环境模型错位：Agent 默认 POSIX 容器布局（/testbed、/workspace、which python3），本环境是 Windows 宿主+repo 根 cwd。修法：decide 提示词开头声明真实执行环境（OS/工作目录/命令语义），一行成本。
   - write_file 全量覆盖：大文件加几行要重写全文，既有 token 风险又有"丢内容"灾难（rich 案例的直接根因）。修法：提供最小 patch/edit 工具，或 write_file 对已有文件做前后 diff 告警。
   - run_command 走 git 无护栏：stash/checkout 类破坏性命令无黑名单，Agent 自伤后恢复消耗巨大。修法：危险 git 子命令拦截或确认（与红线拦截同机制）。
   - Reviewer 误判：对"新增测试文件"机械套用"独立脚本未并入"规则。修法：工作树事实里补"verify 实际收集了该文件（N passed）"证据，让 Reviewer 有据可依。
4. **对完全自动化的含义**（衔接 D5 评估）：执行成功率的上限受上述工程问题压制；先修这四项，再谈提高自动放行条件，比单纯加大 max_iterations 更有效。

## 四、环境准备记录（复现所需）

- docker daemon 未运行 → local 执行器；仓库测试依赖预装进 agent .venv（rich 无额外依赖；jsonschema 补 jsonpath-ng/six/cffi，venv 内有 dist-info 在而模块文件缺失的损坏包需 --force-reinstall）。
- pip 默认源被网络重置 → 清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple` 可用。
- rich 基线 test_log 失败为环境性（本机终端无 OSC8 超链接序列）→ `PYTEST_ADDOPTS=--deselect tests/test_log.py::test_log` 剔除单测，基线转绿。
- requests#6102 曾入选后放弃：全套件 152s 超过 run_tests 120s 上限且基线 12 失败，verify 永远过不去。
- 实验产物：output/exp/{run_real_issue.py, rich3299.json, jsonschema1159.json, *.log}；工作树保留在 workspace/Textualize__rich（污染现场供检查）与 workspace/python-jsonschema__jsonschema（修复现场）。
