# TCO Contract Reference

## Artifacts

- TCO model: stable M tree, TSP, KYM refs and Target mappings; v4 M1 has no current `effect_status`.
- Coverage proposal: evidence, effective/claim state, exact requirement rows, missing IDs and claim ceiling.
- Execution/verification results: observations and command outcomes; neither promotes Coverage.
- Candidate preview: model/proposal, always `candidate_not_approved`.

v4 `scope` is boundary only. Denominators live in `coverage_requirements` with `category/ref/mandatory`; repo-mode Coverage has one exact row per requirement.

`excluded_outputs` only accounts for decision outputs this M1 does not produce. High-risk M1s use source-backed `claim_exclusions` or not-applicable rationale for semantic risk, Provider/Candidate readiness, authority/P06, canonical write, runtime-to-semantic, comparison/Arena or promotion.

`decision_outputs` are classifications, not Goal/execution termination. v4 stores only execution-terminal values in `decision_semantics.terminal_outcomes`; terminal classification needs runtime evidence. Materialization, P05 readiness, honest outcome and P06/Formal authority remain separate; `review_ready != P06 eligible != Formal M1`.

TCO validation recomputes source/model identity. Manifest authority change or coordinated snapshot/path rewrite creates a new candidate identity.

Provider/model/prompt/schema identity is one-attempt metadata. Cross-config trial, migration, supersession or comparison needs configuration-version management or a gap M1; it records lineage/comparability and never promotes winners.

MC grouping is not acceptance. Children should share lifecycle, owner and ladder; otherwise split or provide `grouping_rationale`. Regrouping never enters denominator or changes IDs, mappings, Coverage or evidence.

## State

Reserved order: `unknown < not_started < design_only < schema_validator < test_bound < bounded_runtime < integrated_runtime`.

Projects may add states/labels, but not change reserved IDs, ranks or definitions. Config, model, Coverage and reports bind one vocabulary digest.

Capability lifecycle is a separate v4 axis: `draft < contract_defined < implemented < maintained`, with `deprecated` as a terminal branch rather than a normal rank. `maintained` requires maintained registration, lifecycle owner and continuous gate; test pass, independent validation or bounded runtime never creates it automatically. `deprecated` requires replacement or sunset reference and does not satisfy a maintained Target.

Validation posture is separate from lifecycle and Coverage: `not_validated < producer_checked < independently_validated`. `independently_validated` never belongs in lifecycle. Resolution status is separate again: `resolved`, `unresolved`, `disputed` describe whether a semantic/modeling issue is closed; legacy `unresolved` may be read during migration but new v4 Coverage must not generate it as lifecycle.

Breadth is separate: `single_trial`, `single_repo`, `known_family`, `multi_repo`, `cross_language`, `arbitrary_attempt`. Advanced breadth must bind sample identity; `multi_repo` needs an identity-bound repo matrix and does not follow from bounded runtime.

### v4 Profile 的精确目标字段

`target_profiles[].coverage_goals` 是目标门槛，不是当前 M 的状态。每项必须有 `id`、`minimum_coverage_state`、`minimum_lifecycle_state`、`required_validation_posture` 四字段。`id` 不重复；只允许下列双目标或三目标组合。coverage 使用以下精确保留 ID，不能按当前进展试填别的词。

双目标形式（兼容既有运行门槛）：

| id | minimum_coverage_state | minimum_lifecycle_state | required_validation_posture |
| --- | --- | --- | --- |
| contract | design_only | contract_defined | producer_checked |
| implementation | bounded_runtime | implemented | independently_validated |

三目标形式（区分测试支持和实际运行）：

| id | minimum_coverage_state | minimum_lifecycle_state | required_validation_posture |
| --- | --- | --- | --- |
| contract | design_only | contract_defined | producer_checked |
| implementation | test_bound | implemented | independently_validated |
| runtime | bounded_runtime | implemented | independently_validated |

不能只把implementation改成test_bound而省略runtime；不能把contract的coverage改成schema_validator/test_bound，也不能互换coverage、lifecycle和validation字段。收到“Profile contract/implementation goal ... 无效”时，逐字段对照所选整组表，先确认是否遗漏或错配，不枚举配置求通过。完整shape见已安装包`kym_tco/schemas/tco_model_v4.schema.json`的`$defs.profile`；表中的额外配对规则来自现有CLI合同，schema只验证枚举和形状不足以代替它。

这些是工具已有目标轴格式，不创造用户业务要求或当前达成事实。Profile身份/描述/requirements继续来自实际KYM Target，TP与M保持多对多，共享M不复制；每条业务要求的源支持阈值与覆盖映射分别保留。若业务目标与工具表示能力存在差异，应明确说明限制，不能为填格式抬高/降低用户要求。当前进展另写Coverage/ECQ；缺证据保留未知/未评估，不能把目标阈值当已经达到，也不能因尚未实现而降低目标。

Conservative rollup:

- v4 effective state is the weakest direct state or mandatory coverage requirement state; v2/v3 use mandatory Scope rows.
- Direct state cannot hide weak branches; groups aggregate and do not count.
- Repo-mode Coverage covers all M1/Policy nodes.

## Evidence Rules

- Counted paths: `code|schema|test|bdd|document` under trusted roots; `command` evidence names a configured command; `candidate_evidence` is visible but not counted.
- Verification uses absolute `argv[0]`, restricted cwd, timeout, allowed env and `shell=False`; Coverage binds model digest.
- Mixed-configuration breadth needs comparability verdict/eligibility; `non_comparable`, `comparability_unknown` and mutable aliases block advanced same-model claims.
- Cross-axis values fail closed: lifecycle cannot be `bounded_runtime`, `multi_repo_evidence`, `independently_validated` or legacy `unresolved`; Coverage cannot be `maintained`; validation posture and resolution status stay separate.
- v2 `effect_status=not_executed` is a legacy model field. It means the v2 model had no bound execution result; it must not become Coverage `not_started` or run-level `not_executed`.

Before preview, show model digest, model/TSP/Target/Coverage deltas, Profile Met/Gap, claim boundaries and observations. Later model edits invalidate the package.
Preview, review packages and profile comparison show lifecycle, Coverage/evidence state, validation posture and breadth separately. They must not display or imply one combined maturity status.

## Profiles

Each Target requirement maps once via `source_requirement_ref`; mapped M1s need source refs or rationale. Witness/NF mappings must be mapped-M1 subsets; reused bundles need rationale.

Long-term family/vision Profiles need `non_executable_vision_profile` or equivalent and coexist with prior denominators.

Requirement mapping needs semantic compatibility across capability, purpose, I/O, owner, witness/NF, claim ceiling and traceability; mismatch fails closed or exposes a gap.

Failure taxonomy is independent when testable: honest outcome decides attempt posture; Provider failure classification explains root cause with baseline classes `grounding_failure`, `extraction_failure`, `overclaim`, `lineage_failure`, `execution_failure`, `authority_failure`.

Lane relationship/truth-boundary requirements map to authority-separation M1s; candidate artifacts or P05 owners are wrong-domain evidence. Readiness evaluation needs distinct I/O, owner, witness, NFs and predicates. A Target may map to materialization plus readiness, but denominator remains requirement count.

## No-Repo Mode

Use `coverage_mode=not_evaluated`, no Coverage records, `N/A` profile results.
