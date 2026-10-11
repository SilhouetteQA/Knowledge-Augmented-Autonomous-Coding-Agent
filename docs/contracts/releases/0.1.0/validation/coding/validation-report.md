# Foundation Contract validation report — coding

- release version: `0.1.0`
- verified repository commit (Candidate A): `ae186533cfef35df9c7832c27fcf60a627de433d`
- current HEAD: `ae186533cfef35df9c7832c27fcf60a627de433d`
- canonical payload repository: `wiki`
- canonical payload commit: `ae186533cfef35df9c7832c27fcf60a627de433d`
- contract_payload_hash: `sha256:64049830ba0d1ca2969bb04620ede2d54852e0171f2397d71bc646b2339a4576`
- payload_descriptor_hash: `sha256:5780138f1f6a1254010ad1d77ae0b63e3e7cd741415b17884c53babc41fa7fc4`
- schema_set_hash: `sha256:d785d52d0f6ed9fcfcdf4186b506a7e5af882167567a72f17da90d54f33cc596`

## Evidence

- contract tests: PASS
- historical replay (L2): PASS
- fresh smoke (L3): PASS (23 published event(s))
- full regression: PASS

## Historical replay (L2)

- run id: `foundation-0_1_0-c1-coding-replay`
- records: 3 across 3 source(s)
- status counts: `{"LEGACY_DATA_INSUFFICIENT": 3, "NOT_OBSERVED": 0, "OBSERVED": 0, "REPRODUCTION_RESTRICTED": 0}`
- mapping outcome counts: `{"LEGACY_DATA_INSUFFICIENT": 3, "NOT_OBSERVED": 0, "OBSERVED": 0, "REPRODUCTION_RESTRICTED": 0}`
- reproduction restriction: `REPRODUCTION_RESTRICTED`
- sanitized corpus: 3 record(s), artifact sha256 `sha256:f6644e3c7ece7aade6c1f0c8ecda57d129e2da5aef65e98894deccde3825b717`
- raw retention decision: `RAW_BUSINESS_EVIDENCE_RETAINED_IN_CONTROLLED_SOURCE_LOCATION`

## Producer coverage

| producer_id | mapping_stage | requirement | covered |
|---|---|---|---|
| `coding.agent.llm_usage` | `openai_compat` | ALL_STAGES | true |
| `coding.trace.generation_usage` | `langfuse_generation` | ALL_STAGES | true |
| `coding.benchmark.case_cost` | `normal` | ALL_STAGES | true |
| `coding.benchmark.case_cost` | `environment_error` | NOT_OBSERVED_ALLOWED | false |
| `coding.benchmark.case_cost` | `error` | NOT_OBSERVED_ALLOWED | false |
| `coding.trace.summary` | `sdk` | NOT_OBSERVED_ALLOWED | false |
| `coding.trace.summary` | `clickhouse` | NOT_OBSERVED_ALLOWED | false |

### Authorized `NOT_OBSERVED_ALLOWED` pairs

母 Spec §7.3:896「错误 stage 可 `NOT_OBSERVED`」/ §7.3:887「'未观察到'不等于失败」：下列 (producer_id, mapping_stage) 本次 run **未观测到**，`covered=false`。它们是**授权豁免的未观测项，不是通过，也不计入覆盖**。

- `coding.benchmark.case_cost` / `environment_error` — unobserved（covered=false）
- `coding.benchmark.case_cost` / `error` — unobserved（covered=false）
- `coding.trace.summary` / `sdk` — unobserved（covered=false）
- `coding.trace.summary` / `clickhouse` — unobserved（covered=false）

## Rule traceability

- declared rules: 68（只保存 test / evidence 引用，见 rule-traceability.json）

## Publication scan

- secret pattern / forbidden field / absolute path / size / UTF-8 扫描：PASS
- run manifest 只记录净化命令；secret / 临时目录 / 私人路径替换为 `<SECRET>` / `<WORKSPACE>` / `<REDACTED>`（§11.4:1253）

## Ancestry

- `A is ancestor of B`：pending —— B 由本步骤准备、由 Spec 14 的发布动作形成；本工具只验证 `--candidate` 是当前 HEAD 的祖先（HEAD=ae186533cfef35df9c7832c27fcf60a627de433d）
- 本工具**不**产出 cycle plan / cycle report / current.json（Spec14:89）

## Provisional decisions / known gaps

- G-01：`smoke-v0.1.json` 的 JSON 键名规范未定义，本仓库使用最小可追溯 schema。
- G-08：`changelog.md` 不在 §11.3 的 B 清单内，本工具显式列入 allowlist。
- G-16：Coding 侧 A_wiki 由 `AGENT_CONTRACT_CANONICAL_PAYLOAD_COMMIT` 提供。
- G-18：退出码 0/1/2 = 通过 / 校验失败 / 用法或配置错误。
