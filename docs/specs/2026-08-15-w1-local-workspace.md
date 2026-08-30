# W1 本地 Workspace — 设计规格

> 日期：2026-08-15
> 窗口：W1（对应实现路径阶段 1：本地 Workspace）
> 状态：设计已获用户批准（2026-08-15），本文档为批准稿
> 关联：`docs/roadmap.md` W1 窗口；需求文档《03_..._实现内容与实现路径.md》第 12 节阶段 1

## 1. 背景与目标

实现路径阶段 1 的目标：Agent 能**理解并修改真实 Python 项目**。

- 背景：最终目标是从 GitHub Issue 到 PR 的自主闭环，W1 是第一步——本地文件级工具与最小 Agent 循环。
- 目标：交付 4 个文件工具（list_files / read_file / search_code / write_file）+ 最小 ReAct 循环（CLI 入口），用真实小型 Python 项目验证「读取 → 理解 → 修改 → 确认」闭环。
- 成功标准：`pytest tests/` 全绿；CLI 能对用户放入 `workspace/demo-project` 的真实项目完成一次「定位代码 → 修改 → 确认」任务。

## 2. 范围

### 2.1 做

- `tools/file_tools.py`：四个文件工具，全部带路径安全校验（限制在 workspace 根内）。
- `agent/llm.py`：LLMClient 协议 + OpenAI 兼容实现（opencode go 订阅的 deepseek-v4-flash）+ MockLLMClient（测试用）。
- `agent/loop.py`：最小 ReAct 循环 `run_agent(task)`，工具调用上限 10 轮。
- `main.py`：CLI 入口 `python main.py "<任务描述>"`。
- `.env.example`：LLM 配置模板（不含真实密钥）。
- `workspace/`：Agent 可操作根目录（工具强制限制范围）。

### 2.2 不做（留给后续窗口）

- Shell / Test 工具、Planner / Retry / Reflection / Git Diff（W2）
- Docker Sandbox（W3）
- Code Parser / Code Knowledge Graph / 领域知识接入（W4）
- GitHub API（W5）
- Web UI、LangGraph 框架、复杂 Multi-Agent（全部后续）

## 3. 架构与数据流

```text
用户任务描述
   │
   ▼
main.py (CLI)
   │
   ▼
agent/loop.py: run_agent(task)
   │  循环（上限 10 轮）：
   │    LLM(messages + 工具 JSON schema) ──► 工具调用？── 是 ──► tools/file_tools.py
   │         │                                    │              │
   │         │◄───────────────────────────────────┴─────── 结构化结果回注 messages
   │         │
   │         └── 否（无 tool_call）──► 最终回答
   ▼
AgentResult{steps, final_answer, iteration_count, stopped_by_limit}
```

## 4. 组件设计

### 4.1 文件工具 `tools/file_tools.py`

工具全部以 workspace 根（默认 `workspace/`，可经参数或 `WORKSPACE_ROOT` 环境变量覆盖）为安全边界。

| 函数 | 签名 | 行为 |
|---|---|---|
| `list_files` | `(root: str \| None = None) -> list[FileEntry]` | 递归列出 root 下文件与目录；跳过 `.git`、`__pycache__`、`.venv`、`node_modules`、`.worktrees`；`FileEntry{path, is_dir, size}` |
| `read_file` | `(path: str) -> FileContent` | 相对 workspace 根解析；大小上限 500KB（超出返回错误）；`FileContent{path, content, lines, size}`（content 带行号） |
| `search_code` | `(query: str, root: str \| None = None, ignore_case: bool = False) -> list[SearchResult]` | 调用 ripgrep（`rg --json`），`.gitignore` 自动感知、自动跳过二进制；`SearchResult{path, line, column, text}` |
| `write_file` | `(path: str, content: str) -> WriteResult` | 写入（自动建父目录）；`WriteResult{path, bytes_written, overwritten}` |

**路径安全模型**：所有 path 先 resolve 为绝对路径，再校验 `is_relative_to(workspace_root)`，越界直接拒绝（返回错误，不抛异常）。防止 Agent 逃逸出工作区。

**错误约定**：文件不存在 / 不可读 / 目录当文件读 / 越界 / 超大小限制 → 返回结构化错误对象（含原因），Agent 可据此自我纠正，不崩溃。

### 4.2 LLM 接口 `agent/llm.py`

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict          # JSON Schema

@dataclass
class LLMMessage:
    role: str                 # "assistant" | "tool"
    content: str | None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[ToolSpec]) -> LLMMessage: ...
```

- `OpenAICompatClient`：OpenAI SDK 兼容协议（`base_url` / `api_key` / `model` 可配置），配置来自环境变量：
  - `opencode_go_api` — API Key（用户指定）
  - `OPENCODE_GO_BASE_URL` — 端点地址（实施时用户填入 .env）
  - `OPENCODE_GO_MODEL` — 默认 `deepseek-v4-flash`
- `MockLLMClient`：脚本驱动（按调用序列返回预设响应），TDD 全程使用，不消耗真实 API。

### 4.3 ReAct 循环 `agent/loop.py`

```python
@dataclass
class AgentStep:
    tool_name: str
    arguments: dict
    result: object

@dataclass
class AgentResult:
    steps: list[AgentStep]
    final_answer: str
    iteration_count: int
    stopped_by_limit: bool

def run_agent(task: str, llm: LLMClient, max_iterations: int = 10,
              workspace_root: str | None = None) -> AgentResult
```

循环规则：
1. 系统提示词：你是编码助手，工具仅可操作 workspace 内文件；每次工具调用后观察结果再行动。
2. 每轮：调 LLM（messages + 四个工具的 JSON schema）→ 有 `tool_calls` 则逐个执行（异常捕获转结构化错误回注）→ 无 `tool_calls` 则以 LLM 文本为 `final_answer` 结束。
3. 达到 `max_iterations` 仍未结束：停止，`stopped_by_limit=True`，输出已达上限说明。

### 4.4 CLI `main.py`

```text
python main.py "<任务描述>" [--workspace workspace] [--max-iterations 10]
```

逐行打印：任务、每一步（工具名 + 参数摘要 + 结果摘要）、最终回答、总迭代数。

## 5. 外部依赖

| 依赖 | 说明 | 状态 |
|---|---|---|
| Python >= 3.12 | 项目运行时 | 已具备（3.12.10） |
| ripgrep >= 14 | `search_code` 后端 | 已安装 14.1.1（`%LOCALAPPDATA%\Programs\rg`，已加入用户 PATH）；实现支持 `RIPGREP_BIN` 环境变量覆盖路径（为 W3 沙箱场景预留） |
| openai SDK | OpenAI 兼容客户端 | 实施时加入 pyproject 依赖 |
| pytest | 测试 | 已配置 |

## 6. 测试策略（TDD）

**Seam（已与用户确认）**：四个工具公共函数 + `run_agent(task)`。不测 LLM 内部。

- `tests/test_file_tools.py`
  - list_files：正常结构 / 忽略规则（.git、__pycache__）/ 不存在的根
  - read_file：正常（带行号）/ 文件不存在 / 目录 / 超 500KB / 越界路径
  - search_code：命中（path/line/column/text）/ 无命中 / 忽略大小写开关 / rg 不可用（`RIPGREP_BIN` 指向不存在路径）时的结构化错误
  - write_file：新建（含子目录）/ 覆盖已有 / 越界拒绝 / 内容字节数正确
- `tests/test_loop.py`（MockLLMClient 驱动）
  - 脚本 A：读文件 → 写文件 → 结束，断言工具调用顺序与 final_answer
  - 脚本 B：一次调用全部 4 个工具后结束
  - 工具抛错时结果以错误形式回注，循环不中断
  - 达到 max_iterations 停止且 stopped_by_limit=True

## 7. 验证计划

1. `pytest tests/` 全绿。
2. 用户将 `<local-projects-dir>` 下一个小型真实项目放入 `workspace/demo-project`。
3. 执行 `python main.py "在 demo-project 中定位 XX 函数并添加一行日志"`，观察 Agent 自主完成：搜索 → 读取 → 写入 → 确认。
4. 人工核对修改正确、未越界。

## 8. 风险与取舍

| 取舍 | 决策 | 理由 |
|---|---|---|
| 不引入 LangGraph | W1 用最小 while 循环 | 简单优先；W2 正式化 Agent Loop 时再引入，避免复杂度前置 |
| search_code 直接依赖 ripgrep | 用户选定 | 获得 .gitignore 感知/二进制跳过/高性能；便携版已装，RIPGREP_BIN 可覆盖 |
| read_file 500KB 上限 | 截断保护 | 防止大文件撑爆 LLM 上下文（遵循「不把整个仓库塞进 Prompt」原则） |
| 工具错误不抛异常 | 结构化错误回注 | Agent 可观察并自我纠正，符合 ReAct 循环设计 |
| demo-project 不内置 | 用户届时放入真实项目 | 真实项目验证更有说服力，且不复制兄弟项目文件 |

## 9. 遗留问题

- `OPENCODE_GO_BASE_URL` 具体端点地址待用户提供（写入 `.env`，不入库）。
- `.env` 不入库；`.env.example` 入库。
