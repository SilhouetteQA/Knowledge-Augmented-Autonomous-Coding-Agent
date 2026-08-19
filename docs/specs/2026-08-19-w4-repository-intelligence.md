# W4 Repository Intelligence 设计规格

> 日期：2026-08-19
> 状态：草稿（待用户批准后进入 writing-plans）
> 关联：`docs/roadmap.md` W4 窗口、《03_Knowledge_Augmented_Autonomous_Coding_Agent_实现内容与实现路径.md》第 4 阶段
> 说明：本规格为窗口准备产物，起草于 main 分支；正式开发时在 worktree `feature/w4-repo-intelligence` 中执行

## 1. 背景与目标

W1-W3 已具备：四文件工具 + shell/test/git 只读三件套 + Docker 沙箱 + LangGraph 显式编排（plan → decide ⇄ execute → verify → reflect → finalize）。

W2 真实演示暴露的限制（devlog §W2）：

1. Agent 探索策略低效（第一轮读 77 个文件，大量无关）；
2. 根因分析依赖 decide 循环 LLM 自由探索，效率与稳定性受模型影响；
3. 无代码结构认知（继承/调用/依赖关系需靠 LLM 逐文件推断）。

W4 目标：**代码解析 → Code Metadata → Code Knowledge Graph**，并接入兄弟项目《Arknights LLM Wiki》的 **Arknights Knowledge Graph**（MCP），形成 Code Knowledge + Domain Knowledge 双知识检索，让 Agent 能以「哪个函数创建了角色 Entity」「该逻辑依赖哪些模块」这类结构化问题直达代码，而不是盲目读文件。

## 2. 验收标准（源自 roadmap）

1. 能回答「哪个函数创建了角色 Entity」「该逻辑依赖哪些模块」——由 `query_code_graph` 工具查询得到，而非 LLM 全文搜索；
2. 领域 Issue 修复演示成功：同一角色在不同章节被识别为不同 Entity 的定位与修复演示（双知识检索闭环）。

## 3. 设计决策（brainstorming 已确认）

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| D1 | Code Parser 选型 | **Python AST（stdlib）+ 既有 ripgrep**；不做 Tree-sitter | 目标仓库以 Python 为主（Arknights LLM Wiki 全 Python）；AST 零依赖、Windows 无编译坑；Tree-sitter 多语言支持留待后续（需求文档第 14 节禁止"复杂 Code Knowledge Graph"） |
| D2 | Code KG 存储 | 内存 dict 图（每任务构建，不落盘、不引图数据库） | 第一版查询为「谁调用 X」「依赖哪些模块」级别；Neo4j/networkx 属过度工程 |
| D3 | 域知识接入 | 复用兄弟项目 `arknights_wiki.mcp_server` 的 **MCP client**（`ArknightsMcpClient.call_tool`），以兄弟项目 venv 的 python 作为 MCP server 子进程 | 已有现成 5 工具（search_entities / search_events / query_relationship / query_timeline / search_story）；子进程隔离依赖，避免把 mcp 包装进本仓库 venv |
| D4 | 双知识检索形态 | 工具集新增 `search_knowledge`（域）与 `query_code_graph`（码），与既有 `search_code` 并列暴露给 decide | 保持工具化（Agent 按需调用），不引入独立 RAG 管线 |
| D5 | 构建时机 | 任务开始时对 workspace 全量构建一次 CodeGraph（模块级，含 .gitignore 排除），缓存在任务内存中 | 全量构建一次代价可控（AST 快），多次增量构建属 YAGNI |

## 4. 架构与组件

```
tools/code_parser.py        # 新增：Python AST 解析 → CodeMetadata（模块/类/函数/导入）
tools/code_graph.py         # 新增：CodeGraph 构建与查询（内存图，查询工具接入）
tools/knowledge_client.py   # 新增：KnowledgeClient 协议 + ArknightsMCP 适配器 + Mock
tools/file_tools.py         # 不变：search_code 已有
agent/graph.py              # 修改：工具集加入 search_knowledge / query_code_graph；decide 提示词引用可用查询
main.py                     # 微调：--graph 模式下任务开始前构建 CodeGraph（可选开关 --index）
config/                     # env：ARKNIGHTS_WIKI_DIR（兄弟项目路径）、ARKNIGHTS_USE_MCP=1
```

### 4.1 CodeMetadata（seam 1：code_parser）

```python
@dataclass
class CodeMetadata:
    modules: list[ModuleInfo]     # name / path
    classes: list[ClassInfo]      # name / module / bases / methods
    functions: list[FunctionInfo] # name / module / class(所属,可选) / calls / params

def parse_python_file(path: str, module_name: str) -> ModuleInfo: ...
def build_metadata(workspace_root: str) -> CodeMetadata: ...
```

- 解析范围：`*.py`，自动跳过 `.git/`、`__pycache__/`、`.venv/`、`.worktrees/`（与 W1 list_files 同一排除规则）
- `calls` 由 AST 中 `Call` 节点收集函数名（同一文件内定义的局部名优先匹配）
- 导入关系从 `Import` / `ImportFrom` 收集（模块 → 模块边）

### 4.2 CodeGraph（seam 2：code_graph）

```python
class CodeGraph:
    def __init__(self, metadata: CodeMetadata): ...
    def query_calls(self, function: str) -> list[str]       # 谁调用该函数
    def query_inheritance(self, class_name: str) -> list[str]  # 继承链
    def query_imports(self, module: str) -> list[str]       # 模块依赖（直接导入）
    def query_module_of(self, symbol: str) -> list[str]     # 符号所在模块
    def search_symbols(self, keyword: str) -> list[str]     # 符号模糊搜索（与 search_code 互补）

def build_code_graph(workspace_root: str) -> CodeGraph: ...
def query_code_graph(graph: CodeGraph, query: str, arg: str) -> list | str: ...  # 工具分发入口
```

- 图结构：节点 = module / class / function；边 = import(module→module) / inherits(class→class) / calls(function→function) / defines(class→method)
- 查询失败返回结构化错误（ToolError 风格），未知符号返回空结果 + 提示

### 4.3 KnowledgeClient（seam 3：knowledge_client）

```python
class KnowledgeClient(Protocol):
    def search(self, query: str, kind: str | None = None) -> str: ...

class ArknightsMCPClient:
    """适配兄弟项目 MCP：以兄弟项目 venv python 启动 server 子进程"""
    def __init__(self, wiki_dir: str): ...   # 来自 ARKNIGHTS_WIKI_DIR
    def search(self, query, kind=None) -> str:
        # kind → MCP 工具映射：entity→search_entities, story→search_story,
        # relationship→query_relationship, timeline→query_timeline, 默认 search_entities
        # 调用 ArknightsMcpClient.call_tool(name, {...})

class MockKnowledgeClient:
    """测试用：预设返回"""
```

- 兄弟项目未配置（`ARKNIGHTS_WIKI_DIR` 缺失）或 MCP 不可用时 → `ToolError`（不写 fallback，按工程约束）
- `get_knowledge_client()` 工厂（懒加载单例）

### 4.4 工具接入（agent/graph.py 修改）

`_graph_tools()` 新增两个 ToolSpec：

- `search_knowledge(query, kind?)`：查询 Arknights 域知识（实体/剧情/关系/时间线）
- `query_code_graph(query, arg)`：查询代码结构（calls / inheritance / imports / module_of / symbols）

`_graph_dispatch` 增加两个分支；CodeGraph 实例经闭包注入（`run_agent_graph(..., code_graph=...)`，缺省 None 时工具返回提示"未构建索引"）。

## 5. 双知识检索用法（演示场景）

目标演示：领域 Issue「同一角色在不同章节被识别为不同 Entity」的**定位**：

1. `query_code_graph(query="module_of", arg="Entity")` → 找到 Entity 定义模块；
2. `query_code_graph(query="calls", arg="create_entity")` → 找到创建实体的函数；
3. `search_knowledge(query="角色名", kind="entity")` → 确认域侧实体命名与章节来源字段；
4. 结合 `search_code` 定位章节归一化缺失处 → 进入修复闭环（复用地 decide/verify/reflect）。

验收演示（W4 末）：workspace = Arknights LLM Wiki 仓库副本，以上 1-4 步由 Agent 自主完成，最终定位到 Entity 合并/创建逻辑并给出修复方向（代码修改本身属演示可选，若修改则走完整测试闭环）。

## 6. 配置

| env | 默认 | 说明 |
|-----|------|------|
| `ARKNIGHTS_WIKI_DIR` | 无 | 兄弟项目根目录（含 `arknights_wiki` 包与 venv） |
| `ARKNIGHTS_USE_MCP` | 无（=1 时启用） | 启用域知识检索 |
| `KA_CODE_INDEX` | `1` | 任务开始时是否自动构建 CodeGraph（0 关闭） |

## 7. 错误处理与返回语义

| 场景 | 返回 |
|------|------|
| 非 Python 文件 / 语法错误文件 | 跳过该文件并记录（不中断构建），语法错误文件计入 warnings |
| 查询未知符号 | 空结果 + 提示「未找到符号 X，可用 search_symbols 或 search_code」 |
| MCP 不可用（wiki 目录缺失 / ARKNIGHTS_USE_MCP 未开） | `ToolError`（明确信息） |
| 解析超大仓库 | 构建限时（默认 60s），超时返回部分结果 + 提示 |

## 8. 测试策略（TDD，seam = 三组接口）

1. **code_parser 单元测试**：小型样例（类继承 / 函数调用 / import / 语法错误跳过 / 排除目录）
2. **code_graph 查询测试**：样例工程构建 → 四类查询断言（calls / inheritance / imports / module_of / symbols）
3. **knowledge_client 测试**：Mock 客户端行为；MCP 适配器以 Fake 子进程或 `ARKNIGHTS_USE_MCP` 关闭路径覆盖；集成（`@pytest.mark.mcp`，兄弟项目可用时）实测 `search_entities`
4. **graph 工具集回归**：Mock LLM 驱动 decide 调用新工具 → dispatch 正确回注
5. **真实演示**：Arknights LLM Wiki 副本 + 真实 LLM（火山引擎 deepseek-v4-flash-ga-260731）定位领域 Issue

## 9. 非目标（YAGNI，第一版不做）

- 不做 Tree-sitter 多语言解析（Python-only）
- 不做 CodeGraph 落盘/增量更新/图数据库
- 不做独立 RAG 管线（检索融合留待 W6 评估数据驱动）
- 不做 API-Service-Repository 层级推导（依赖手工标注/启发式，价值待验证，roadmap 中留作后续）
- 不做跨仓库知识

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| AST 调用图对动态调用（getattr/装饰器）不敏感 | calls 仅收集静态字面名；查询结果标注「静态分析」；与 search_code 互补 |
| 兄弟项目 MCP 依赖较重（mcp 包、数据文件） | 以兄弟项目 venv 子进程启动 server，本仓库零新增依赖；集成测试跳过条件 |
| 全量 AST 构建大仓库慢 | 限时 + 排除规则 + 构建进度提示；只读工具可重复调用 |
| MCP server 与兄弟项目版本漂移 | 固定 ARKNIGHTS_WIKI_DIR 指向已安装副本；接口以 client.call_tool 为准 |

## 11. 依赖

- 新增 Python 依赖：无（AST / os / dataclasses 均为标准库）
- 外部：兄弟项目 `arknights_wiki`（MCP server 与数据）、其 venv 可运行 `python -m arknights_wiki.mcp_server.server`
