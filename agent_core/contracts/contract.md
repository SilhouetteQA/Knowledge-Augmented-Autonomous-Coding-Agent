# Foundation Contract v0.1 规范语义

> Contract Set 版本：`0.1.0`（契约族：`foundation`）
> 规范真相源：`docs/specs/2026-09-10-dual-agent-foundation-contract-master-spec.md`
> 本文件的作用域：Part I 中**已落地到本 Contract Set** 的规则，即
> Master Spec Appendix A.1–A.5 里属于 foundation 与 evidence 两族的条目。
>
> `FND-REL-*`（Cycle 发布完整性）与 `FND-REG-*`（回归与非侵入）按各自子 Spec 的
> 落地时点写入本文件；在写入之前，它们**不属于**本 Contract Set 的规范载荷。

## 0. 文档地位

本文件是 Contract Payload 的**规范性载体之一**，与 Pydantic 模型、`typing.Protocol`、
共享一致性测试、生成的 JSON Schema 和 `payload-descriptor.json` 地位平等：

| 载体 | 规范职责 |
|---|---|
| Pydantic v2 Models / StrEnum | 数据形状、序列化与结构性不变量 |
| `typing.Protocol` | 行为能力与调用签名 |
| `contract.md`（本文件） | 类型系统无法完整表达的语义、禁止项与状态关系 |
| Shared Conformance Tests | 对规范规则的可执行证明 |
| Generated JSON Schema | **派生快照**，不是真相源 |
| `payload-descriptor.json` | 版本、工具链、Schema 与 Rule ID 的机器目录 |

**冲突处理**：任一规范性载体互相矛盾时，不存在"代码优先""测试优先"或"文档优先"：

```text
NORMATIVE CONFLICT
→ CONTRACT_CONFLICT
→ release INVALID
→ Cycle cannot COMPLETE
```

修复必须先确认正确语义，再同步修改全部相关载体、重新生成 Schema、重跑共享测试与双仓验证。

**JSON Schema 的地位**：`schemas/*.schema.json` 由 Pydantic 自动生成并做确定性规范化，
只是受控的机器快照。不可手工编辑；`--check` 模式下生成结果与已提交快照不一致即为失败。

### 0.1 规则标识

规则标识格式为 `<FAMILY>-<SUBDOMAIN>-<NNN>`，发布后**永不复用**。
共享测试必须用机器可读 metadata 关联 Rule ID；测试函数名只是人类可读信息：

```python
@contract_rule("FND-COST-001")
def test_unknown_cost_is_not_zero():
    ...
```

---

## 1. 命名空间与版本

### 1.1 包结构白名单

Phase 1 的 `agent_core` 只允许两个层次：

```text
agent_core/__init__.py
agent_core/contracts/**
```

出现 provider client、retry、checkpoint、Adapter、领域模型、配置或项目状态代码时，
Repository Contract Gate 直接失败。项目 Adapter 一律位于各仓自己的包内
（Wiki：`arknights_wiki/adapters/foundation/`；Coding：`adapters/foundation/`），
**不得**放进 `agent_core`。

`agent_core/__init__.py` 只允许存在 namespace docstring，**不得** re-export
`Cost`、`Usage` 等契约符号。稳定 import 必须显式写出完整路径：

```python
from agent_core.contracts.models.cost import Cost
```

### 1.2 版本

Phase 1 只维护**一个锁步 Contract Set 版本**，不给各 family 建立独立 SemVer：

```text
0.1.0  Foundation Cycle 1
0.2.0  真实 L2/L3 反馈导致的非文档语义修订
0.x.y  不改变 Schema / 不变量 / 映射 / 行为的修正
1.0.0  首批成熟 family 已抽入 agent-core 并承诺稳定公共 API
```

硬门禁：

```text
same version + different payload/schema hash  → FAIL or DIVERGED
same schema hash + changed semantics without bump → VERSIONING_VIOLATION
Cycle completed but repo versions differ        → FAIL
```

Contract Payload **不得**保存自己的 `contract_payload_hash`；
`payload-descriptor.json` 必须在计算总哈希**之前**就能完整生成；
`contract-manifest.json` 必须位于它所标识的 Payload 之外；
治理成熟度（`EXPERIMENTAL` / `CYCLING` / …）变化不得改变契约身份。

---

## 2. 统一模型约束

所有 Foundation 模型默认：

```python
model_config = ConfigDict(extra="forbid")
```

跨边界 JSON 一律 UTF-8。金额使用 `Decimal`，序列化为**十进制字符串**；
禁止 JSON float 作为金额输入（Legacy float 必须经 `Decimal(str(value))` 转换）。
内部构造可以使用 `Decimal`，Python 模式序列化保留 `Decimal`。

**未知不等于零**是本契约的第一性语义：

```text
Unknown is not Zero
Estimated is not Reported
USD is not CNY
Raw Cost is not Converted Cost
```

### 2.1 存在性感知映射

Adapter 必须映射**可证明事实**，而不是 Legacy 为了计算方便填进去的默认值：

```text
Foundation mapping = value + presence + provenance + pricing evidence
Legacy normalized value ≠ Foundation fact
```

Facts extractor 自身**禁止**使用 `getattr(..., 0)`、`or 0` 等写法抹去 field presence；
也**禁止**从 aggregate default 反推缺失事实。

---

## 3. Usage

```yaml
Usage:
  input_tokens: int | null
  output_tokens: int | null
  total_tokens: int | null
  cache_read_tokens: int | null
  cache_write_tokens: int | null
  source: provider_reported | locally_calculated | estimated | unknown
  extensions: dict[str, JsonValue]
```

不变量：

- 所有非空 token 字段必须是非负整数。
- `0` 表示**明确观察到零**；`null` 表示**未知或未报告**。两者不可互换。
- provider 返回的 `total_tokens` **原样保留**，即使与 input/output 之和不一致也不改写。
- provider 未返回 `total_tokens` 时保持 `null`；**禁止**自动相加 input + output。
- `source` 是 object-level，描述已填充字段的共同来源；缺失字段为 `null`，
  不会单独降低来源等级。
- v0.1 **不做** per-field provenance。真实路径出现混合来源需求时只能由 L2/L3 证据
  驱动 v0.2，v0.1 不预设 per-field source。

映射语义（Adapter 侧）：

| Legacy / Provider 事实 | Foundation |
|---|---|
| usage object 缺失 | token 字段全 `null`，`source=unknown` |
| usage object 存在且字段明确为 0 | 对应字段 `0`，`source=provider_reported` |
| usage object 存在但字段缺失 | 对应字段 `null` |
| 按响应字符估算（Wiki runner） | input `null`，output 为估算 N，total `null`，`source=estimated` |
| provider total 存在 | 精确保留 |
| provider total 缺失 | `null`，禁止自动相加 |

---

## 4. Cost

```yaml
Cost:
  amount: Decimal-string | null
  currency: string | null
  source: provider_reported | price_table | estimated | unknown
  pricing_version: string | null
  extensions: dict[str, JsonValue]
```

不变量（来源矩阵）：

- 已知 `amount` 必须 `>= 0`，且 `currency` 必须存在。
- `currency` 采用**大写三字母** ISO 格式校验，**不使用**封闭 Enum。
- `source=unknown` 时 `amount` 必须为 `null`。
- `source=price_table` 时 `pricing_version` 必填。
- `amount=0` 只表示有 presence / provenance 证明的**真实零成本**。
- Contract 层**不做**汇率换算，也**不做**默认币种替换。
- `CostSource` 描述**认识论可信来源**，不只是价格数据的存储位置。
- `Cost` 只表达金额本身；task / span / report 范围属于消费方 family，
  **禁止**出现在 `Cost` 字段中。
- 价格表推导出的已知金额必须引用可复现的 pricing snapshot 标识
  （Wiki：对整个 `pricing.json` 做 canonical JSON、sorted keys、compact UTF-8 后取 SHA256；
  **不得**使用 mtime、`dict` repr 或 Git 时间戳）。
- 单独的 Legacy 数值 `0` **不足以**证明真实零成本。

Pricing facts 映射：

| Pricing 事实 | Foundation |
|---|---|
| price entry 缺失 | `amount=null`、`source=unknown`、保留币种上下文 |
| price 为 `null` / `tbd` | 同上 |
| price entry 有效且 `estimate=true` | 已知 Decimal amount、`source=estimated` |
| price entry 有效且确认价格 | 已知 Decimal amount、`source=price_table`、`pricing_version` 必填 |
| provider 明确报告货币成本 | `source=provider_reported` |

---

## 5. CostSummary

```yaml
CostSummary:
  known_amount: Decimal-string | null
  currency: string | null
  complete: bool
  component_count: int
  known_component_count: int
  unknown_component_count: int
  extensions: dict[str, JsonValue]
```

不变量：

```text
all counts >= 0
component_count = known_component_count + unknown_component_count
complete = (unknown_component_count == 0)
```

- `known_component_count=0` 时 `known_amount=null`。
- `known_component_count>0` 时 `known_amount>=0` 且 `currency` 非空。
- **真实零成本属于 known component**。
- 所有非空 currency 必须一致；已知金额 `USD` 与未知金额 `CNY` **仍然是** CurrencyMismatch。
- currency 缺失本身不产生 mismatch，但对应未知金额仍令 summary incomplete。
- 空集合固定为 `0/0/0/null/null/complete=true`。
- Summary **不声明**统一 `source` 或 `pricing_version`。
- **币种兼容性必须先于完整性计算**；冲突显式失败，
  不得返回伪造的 `complete=false` 混合金额。
- Summary 必须由**被观察到的组成项**构建，
  **禁止**从 `legacy total` 反推组成项、完整性或真实零成本。

正确关系：

```text
Legacy components ─→ Legacy aggregation ─→ unchanged legacy result
        │
        └→ provenance facts ─→ Foundation Cost components ─→ CostSummary
```

malformed legacy record 可以继续按旧逻辑跳过，但 `observe` 必须产生失败 Evidence，
`strict` replay 必须失败。

---

## 6. ErrorEnvelope

```yaml
ErrorEnvelope:
  category: validation | compatibility | aggregation
  code: string
  message: string
  retryable: bool
  extensions: dict[str, JsonValue]
```

v0.1 Foundation codes：

```text
foundation.invalid_usage
foundation.invalid_cost
foundation.invalid_cost_summary
foundation.schema_version_mismatch
foundation.currency_mismatch
```

约束：

- 机器逻辑**只允许**依据 `code`，**禁止**解析 `message`。
- 以上错误均固定 `retryable=false`。
- `ErrorEnvelope` 是**边界 DTO**，**不是**异常类型，也不是内部 Result 模式：

```text
禁止 raise ErrorEnvelope / except ErrorEnvelope
禁止替换仓库现有异常类、传播、重试与恢复逻辑
禁止成为内部 Result[T, ErrorEnvelope] 通用返回模式
```

- 仅在序列化、API / IPC / MCP、跨仓 Adapter 或 Evaluation artifact 边界，
  由 Adapter 把内部错误映射为 Envelope。
- 错误细节必须**结构化、已净化且有界**；异常对象与堆栈traceback 禁止进入。
- `cause_code` 延期到 v0.2，仅在 Cycle 1 真实反馈证明需要时才可引入。

Evidence 基础设施使用独立机器码 `evidence.sink_write_failed`、
`evidence.invalid_run_id`、`evidence.artifact_unavailable`。它们是**基础设施诊断码**，
不是新的 `ErrorCategory`，也不属于 `foundation.*` 语义错误。
sink 无法写入时不得递归使用同一个 sink 记录失败；最后防线是结构化应用日志与进程内计数。

---

## 7. Extension Boundary

`extensions` 是受限逃生口，**不是**第二套自由 Schema。

合法 key 格式：

```text
^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$
```

已注册第一段：

```text
wiki      项目 Adapter 可写
coding    项目 Adapter 可写
provider  项目 Adapter 可写
adapter   项目 Adapter 可写
foundation  保留，项目 Adapter 禁止写入
shared      保留，项目 Adapter 禁止写入
```

值类型为递归 JSON：

```python
JsonScalar = str | int | float | bool | None
JsonValue  = JsonScalar | list[JsonValue] | dict[str, JsonValue]
```

**禁止** `Decimal`、datetime、Path、bytes、Exception、Pydantic 实例、dataclass
或任意 Python object，也禁止 tuple 等非 JSON 容器。时间等内容必须由 Adapter
转成稳定 JSON 表达（例如 RFC3339 字符串）。

容量与深度：

- 对 `extensions` 做 canonical JSON UTF-8 序列化后不得超过 **16 KiB**。
- `extensions` **根映射不计层**；extension value 的第一层 container 记作 depth 1；
  最大 depth 为 **4**。

敏感内容：

- **敏感 key 名单 → 硬拒绝**。生产者**禁止**写入 raw prompt、完整 response、
  reasoning、traceback、credentials、Authorization、token、cookie、password、secret、
  API key、未脱敏本机绝对路径、代码正文、diff 或完整 provider payload。
- **内容启发式 → 只告警并计数**。scanner 只是辅助防线，
  生产者承担不写入敏感内容的首要责任，因此内容命中不阻断合法扩展。
- 判定为"指标名"的 key 不受误伤：`prompt_tokens`、`response_count`、
  `cache_read_tokens` 之类属于合法指标。

Foundation 对 `wiki.*`、`coding.*`、`provider.*`、`adapter.*` 全部保持**语义不透明**：
只能存储、序列化、传播或统计 key，**不得**基于它们改变 Cost、Usage 或其他规范行为。

连续两个 Cycle 被双仓共同使用只产生 `PROMOTION_CANDIDATE`；
是否晋升还需判断语义是否相同、是否稳定、能否定义不变量以及是否长期存在。

---

## 8. Contract Mode

```python
class ContractMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    STRICT = "strict"
```

唯一配置入口：`AGENT_CONTRACT_MODE`。

```text
OFF     完全跳过 facts / Adapter / Evidence 分支
OBSERVE 正常映射并记录 PASS/FAIL 证据；mapping 或 sink 失败不影响业务
STRICT  使用完全相同路径并记录证据；任何契约失败令验证命令失败
```

- `observe` **不是** fallback，也不存在 Foundation 主路径。
- observe 与 strict 必须执行**同一份** extractor 与 Adapter，只有失败策略不同。
- 未配置时默认 `off`（不介入业务）；非法的显式取值必须**明确报配置错误**，
  **禁止**静默降级为 `off`。
- Evidence sink 写失败时：结构化应用日志 + 内存 `sink_failure_count` →
  observe 下业务继续 → 该次运行对 Evidence Gate **无效**。
- 即时回滚：`AGENT_CONTRACT_MODE=off`。
- 代码回滚点是各 Producer 的旁路调用与项目 Adapter 层；
  由于 Legacy 从未消费 Foundation 输出，删除这些调用即可恢复原实现。
  **不得**用"Foundation 失败后回退 Legacy"的双主路径描述回滚。

---

## 9. Evidence 与发布边界

### 9.1 观测对象

`FoundationObservation` 用**显式结构**承载一次 producer 观测到的 Foundation 事实，
不是任意 `dict`：

```yaml
usage: Usage | null
cost: Cost | null
cost_summary: CostSummary | null
```

三者**至少一项必须存在**。空观测不是"合法的 zero"，必须被拒绝。

`FoundationObservation` 是跨边界 DTO：它不是业务 `Result`，不得成为业务函数返回值，
不得继承 `BaseException`，也不得替代仓库现有的返回契约。

### 9.2 证据记录

`EvidenceRecord` 的字段集合是**封闭**的（禁止额外字段）：

```yaml
event_id: str              run_id: str                repository: wiki | coding
repository_commit: str     producer_id: str          mapping_stage: str
contract_mode: observe | strict
contract_version: str      contract_payload_hash: str
timestamp: str             validation_status: PASS | FAIL
sanitized_input_facts: dict[str, JsonValue]
foundation_output: FoundationObservation | null
error_envelope: ErrorEnvelope | null
```

状态不变量：

```text
PASS → foundation_output 存在 AND error_envelope 为 null
FAIL → error_envelope 存在；foundation_output 可空
```

`contract_mode` 只允许 `observe` / `strict`：`off` 完全跳过 facts 与 Evidence 分支，
因而**不产出证据**。任一其它取值必须明确失败，不得静默归为某一模式。

`producer_id` 与 `mapping_stage` 必须存在，并由 Producer Registry 约束其实际取值。

### 9.3 标识与路径安全

```text
run_id     ^[A-Za-z0-9_-]+$
event_id   canonical UUID（8-4-4-4-12）或 ULID（26 位 Crockford base32）
```

`event_id` 统一规范化为小写：UUID 的规范形式本就小写，ULID 的 Crockford 字母表
大小写不敏感，统一小写避免"同一事件两种拼写"绕过唯一性判断。

**任一标识都不得携带 `/`、`\`、`..`、`:` 参与路径构造。** 共享层提供
`is_path_safe_identifier` 作为该判定的唯一实现；项目 sink 在拼路径前必须**再次**调用，
与模型层校验互为冗余。路径安全与字符集合法性是两件事：空格对路径无害，
但仍会被 `run_id` 的形状规则拒绝。

### 9.4 EvidenceSink Protocol 与失败语义

```python
class EvidenceSink(Protocol):
    def emit(self, record: EvidenceRecord) -> None: ...
```

Protocol 只定义行为边界，**不定义**目录、命名、rotation 或 cleanup；符合实现必须
接收合法记录，无法持久化时**显式报告失败**，不得静默丢弃。

失败语义：

```text
Sink failure != business failure          （observe 下不得改变业务返回）
Sink failure  = evidence gate failure     （该 run 的证据失效）
partial .tmp  != valid evidence
```

失败只允许使用 `evidence.*` 基础设施诊断码写结构化应用日志与内存 failure counter，
**不得**污染 Foundation semantic error。因此失败上报类型与 `ErrorEnvelope` 互不继承，
`evidence.*` 也不是新的 `ErrorCategory`。失败时禁止递归地再次调用同一个 sink。

### 9.5 容量与脱敏

```text
单条 EvidenceRecord 的 canonical JSON UTF-8  ≤ 64 KiB
sanitized_input_facts 的键数               ≤ 64
fact key 形状  ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$
fact key 首段  wiki | coding | provider | adapter（foundation / shared 保留）
```

`foundation` 与 `shared` 是保留命名空间，项目 Adapter 禁止写入。

脱敏采用与 Extension Boundary 相同的两层防线，但归属 `EVD-DATA-001`：

```text
敏感 key（raw prompt / response / reasoning / traceback / 凭据）→ 硬拒绝
内容启发式（绝对路径 / Bearer / 长随机串）                     → 只告警并计数
```

fact 值域是递归 JSON，复用 Extension Boundary 的唯一实现；`Decimal`、datetime、
`Path`、`bytes` 等一律拒绝。`prompt_tokens`、`response_chars` 这类**指标名**不得被误伤。

### 9.6 发布边界

发布必须采用 **allowlist extraction**，不得采用"序列化原始对象后删除已知敏感字段"
的 denylist 做法：

```text
controlled business / historical source
→ minimal staging facts
→ construct new publishable Evidence DTO
→ validate and scan
→ hash
→ release snapshot
```

可进入 Evidence Publication Commit B 的 artifact 是**显式闭集**：

```text
run-manifest.json          contract-manifest.json     evidence-manifest.json
validation-report.md       rule-traceability.json     sanitized-replay-corpus.jsonl
```

最终 `cycle-report.json` / `cycle-report.md` 只在 Wiki Finalization C 写入，
**不在**上述清单内。

`staging` 只表示"尚未发布聚合"，仍然禁止原始 Prompt、response、trace、代码与凭据：
**staging evidence ≠ raw business payload**。

`REPRODUCTION_RESTRICTED` 表示结论有真实证据支持，但完整复现依赖不可发布数据或受限
环境；它不是 `FAIL`，也不能伪报为完全可复现。

---

## 10. 规则索引

### 10.1 包与版本

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| FND-PKG-001 | Phase 1 `agent_core` 只能包含根 `__init__.py` 与 `contracts/**`。 | Spec 03/10 allowlist tree test |
| FND-PKG-002 | 项目 Adapter 不得位于 `agent_core` 之下。 | Spec 03/10 import/tree test |
| FND-PKG-003 | 构建出的发行包必须暴露 `agent_core.contracts` 与打包的 schemas。 | Spec 10 clean wheel smoke |
| FND-PKG-004 | `agent_core.__init__` 不得 re-export 契约/运行时符号。 | Spec 03 `test_versioning.py` |
| FND-VER-001 | Phase 1 使用一个锁步 Contract Set 版本；任何语义变化都要求 minor bump。 | Spec 03 `test_versioning.py` |
| FND-VER-002 | Contract Payload 不得包含自己的 `contract_payload_hash`。 | Spec 03 descriptor schema test |
| FND-VER-003 | `payload-descriptor.json` 必须在 payload 哈希之前完全可派生。 | Spec 03 确定性生成测试 |
| FND-VER-004 | `contract-manifest.json` 必须位于它标识的 payload 之外。 | Spec 03 tree/manifest test |
| FND-VER-005 | Evidence Manifest 必须绑定已存在的 Candidate commit 与 Payload Hash。 | Spec 14 manifest validation |
| FND-VER-006 | 治理生命周期变化不得改变 payload 身份（除非规范内容变化）。 | Spec 03 hash fixture test |

### 10.2 存在性感知映射与 Usage

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| FND-MAP-001 | Adapter 必须映射可证明事实，而非 Legacy 便利默认值。 | Spec 05/06 adapter tests |
| FND-MAP-002 | Facts 提取必须保留 presence 与 provenance，禁止从聚合默认值重建缺失事实。 | Spec 05/06 adapter tests |
| FND-USAGE-001 | 已知 token 计数必须非负；显式零与未知 `null` 必须可区分。 | `conformance/test_usage.py` |
| FND-USAGE-002 | provider 报告的 `total_tokens` 必须精确保留，即使与可见组成项不一致。 | `conformance/test_usage.py` |
| FND-USAGE-003 | 缺失 `total_tokens` 必须保持 `null`；Adapter 不得合成 input + output。 | `conformance/test_usage.py` |

### 10.3 Cost

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| FND-COST-001 | 未知成本必须是 `amount=null, source=unknown`；不得用零表示。 | `conformance/test_cost.py` |
| FND-COST-002 | 聚合不得做隐式 FX；多个非空币种必须显式失败。 | `conformance/test_cost_summary.py` |
| FND-COST-003 | 金额必须使用 `Decimal` 并以十进制字符串序列化；float 输入被拒绝。 | `conformance/test_cost.py` |
| FND-COST-004 | 非空 currency 必须是大写三字母码。 | `conformance/test_cost.py` |
| FND-COST-005 | `source` / `amount` / `currency` / `pricing_version` 必须满足来源不变量矩阵。 | `conformance/test_cost.py` |
| FND-COST-006 | `source=price_table` 必须包含可复现的 `pricing_version`。 | `conformance/test_cost.py` |
| FND-COST-007 | 只有 provider 或确认的价格证据为已观察调用明确证明零时，零才是已知值。 | `conformance/test_cost.py` |
| FND-COST-008 | 未知金额可以保留已知币种上下文，但不因此成为已知成本。 | `conformance/test_cost.py` |
| FND-COST-009 | Raw Cost 必须保持源币种，不得声称已换算/归一化。 | `conformance/test_cost.py` |
| FND-COST-010 | `Cost` 不得包含 task/span/report 范围；范围属于消费方 family。 | `conformance/test_cost.py` |
| FND-COST-011 | `CostSource` 描述认识论来源，不是价格数据的存储位置。 | `conformance/test_cost.py` |
| FND-COST-012 | 单独的 Legacy 数值零不足以证明真实零成本。 | `conformance/test_cost.py` |
| FND-COST-013 | 价格表推导的已知金额必须引用 canonical pricing snapshot 标识。 | `conformance/test_cost.py` |

### 10.4 CostSummary

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| FND-CSUM-001 | 所有组成项计数必须非负。 | `conformance/test_cost_summary.py` |
| FND-CSUM-002 | `component_count == known_component_count + unknown_component_count`。 | `conformance/test_cost_summary.py` |
| FND-CSUM-003 | `complete == (unknown_component_count == 0)`。 | `conformance/test_cost_summary.py` |
| FND-CSUM-004 | 已知组成项为 0 时要求 `known_amount=null`。 | `conformance/test_cost_summary.py` |
| FND-CSUM-005 | 存在已知组成项时要求非负 `known_amount` 且 currency 非空。 | `conformance/test_cost_summary.py` |
| FND-CSUM-006 | 真实零成本是一个 known component。 | `conformance/test_cost_summary.py` |
| FND-CSUM-007 | 所有非空组成项币种必须一致。 | `conformance/test_cost_summary.py` |
| FND-CSUM-008 | 空币种本身不产生 mismatch，但未知金额仍令 summary incomplete。 | `conformance/test_cost_summary.py` |
| FND-CSUM-009 | 空输入必须得到 0/0/0、null 金额与币种、`complete=true`。 | `conformance/test_cost_summary.py` |
| FND-CSUM-010 | CostSummary 不得声明统一的 `source` 或 `pricing_version`。 | `conformance/test_cost_summary.py` |
| FND-CSUM-011 | Summary 必须由被观察到的组成项构建，不得从 Legacy total 反推。 | `conformance/test_cost_summary.py` |

### 10.5 错误、模式与扩展边界

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| FND-ERR-001 | ErrorEnvelope 是边界 DTO，不是异常类或内部控制流框架。 | `conformance/test_error_envelope.py` |
| FND-ERR-002 | 机器行为必须使用稳定 `code`；`message` 不得驱动逻辑。 | `conformance/test_error_envelope.py` |
| FND-ERR-003 | Foundation v0.1 错误必须 `retryable=false`。 | `conformance/test_error_envelope.py` |
| FND-ERR-004 | 错误细节必须结构化、已净化且有界；禁止异常对象与堆栈。 | `conformance/test_error_envelope.py` |
| FND-ERR-005 | ErrorEnvelope 只能在声明的序列化 / API / IPC / MCP / Adapter / artifact 边界产生。 | `conformance/test_error_envelope.py` |
| FND-EXT-001 | 每个 extension key 必须使用已注册命名空间与合法点分标识。 | `conformance/test_extensions.py` |
| FND-EXT-002 | Extension 值必须递归 JSON 兼容。 | `conformance/test_extensions.py` |
| FND-EXT-003 | canonical 序列化后的 `extensions` 不得超过 16 KiB。 | `conformance/test_extensions.py` |
| FND-EXT-004 | Extension 值嵌套不得超过四层 container（根映射不计）。 | `conformance/test_extensions.py` |
| FND-EXT-005 | 密钥、原始 prompt/response、堆栈、凭据与未脱敏路径不得进入 extensions。 | `conformance/test_extensions.py` |
| FND-EXT-006 | Foundation 逻辑必须把项目/provider extension 视为不透明，不得据此改变规范行为。 | `conformance/test_extensions.py` |
| FND-MODE-001 | `off` / `observe` / `strict` 具有本章定义的精确语义。 | `conformance/test_modes.py` |
| FND-MODE-002 | observe 与 strict 必须执行同一 extractor 与 Adapter；只有失败策略不同。 | `conformance/test_modes.py` + 项目 Adapter 层 |
| FND-MODE-003 | 非法 `AGENT_CONTRACT_MODE` 必须令配置校验失败，不得静默变为 off。 | `conformance/test_modes.py` |
| FND-MODE-004 | observe 的映射失败必须保持 Legacy 行为并产出失败校验证据。 | 项目 invariance harness（Spec 09） |

### 10.6 证据与发布

| Rule ID | 规范语句 | 测试落点 |
|---|---|---|
| EVD-RUN-001 | Controlled smoke / replay / strict 命令必须收到显式且安全的 `run_id`。 | `conformance/test_evidence.py` + Spec 09 run harness |
| EVD-RUN-002 | 每条 EvidenceRecord 必须绑定 contract version、Payload Hash 与已验证的仓库提交。 | `conformance/test_evidence.py` |
| EVD-SINK-001 | EvidenceSink 不得静默丢弃合法记录。 | `conformance/test_evidence.py`（接口与失败形状）+ Spec 09 注入失败测试 |
| EVD-SINK-002 | observe 下 sink 失败不得改变业务结果。 | 项目 invariance harness（Spec 09） |
| EVD-SINK-003 | 任何 sink 失败都令该 run 不再是 Cycle Evidence。 | Spec 09 smoke closure |
| EVD-SINK-004 | 文件形式的事件发布必须原子且并发写入安全。 | Spec 09 项目 sink 测试 |
| EVD-DATA-001 | Staging Evidence 必须排除原始业务内容，并遵守 allowlist、JSON 与 64 KiB 约束。 | `conformance/test_evidence.py` |
| EVD-PUB-001 | Git 发布 artifact 必须由显式 allowlist 构造。 | `conformance/test_evidence.py`（清单闭集）+ Spec 10/14 发布变换测试 |
| EVD-PUB-002 | 原始 prompt、response、reasoning、代码正文、diff 与原始 trace 不得进入发布。 | Spec 10/14 forbidden-field scan |
| EVD-PUB-003 | 凭据、环境转储与私有端点不得进入发布。 | Spec 10/14 secret scan |
| EVD-PUB-004 | Replay 语料只能包含复现契约语义所需的字段。 | Spec 12 corpus schema test |
| EVD-PUB-005 | 受限证据必须标记 `REPRODUCTION_RESTRICTED`。 | `conformance/test_evidence.py` + Spec 14 状态校验 |
| EVD-PUB-006 | 已发布证据必须绑定版本、Payload Hash 与 Candidate commit。 | Spec 14 manifest validation |
| EVD-PUB-007 | 原始临时证据必须有显式的保留决策。 | Spec 14 publication checklist |

`evidence.*` sink / I/O 诊断码属于基础设施诊断，不是新的 `ErrorCategory`，
也不是 `foundation.*` 语义错误。`FND-REL-*` 与 `FND-REG-*` 在各自子 Spec 落地前
不属于本 Contract Set 载荷。
