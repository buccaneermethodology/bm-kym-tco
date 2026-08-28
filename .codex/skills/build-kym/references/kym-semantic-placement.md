# KYM Semantic Placement

Reachable placement gate. In kym-tco, `kb/data/kym_semantic_placement.json` is canonical truth and `kb/docs/KYM_Semantic_Placement.md` is generated.

## Two-Pass Gate

1. Classify source units as product-domain, source-identity, or host-execution-metadata; exclude coding-host/session/worktree/global-instruction/child-agent details. Then ledger speaker/source authority, Product actor, evidence, semantic purpose, time/support status, candidate node, excluded neighbors, conflict/uncertainty, inference boundary.
2. Reverse-walk fixed nodes against admission questions. Empty nodes are valid. Speaker/authority is provenance, not Product actor role.
3. For Goal/plan input, classify status as source snapshot, verified current fact, or candidate/proposal.
4. Split mixed text: if one sentence defines a term and states a rule, create separate statements.

## Placement Map

Classify by reader question and product-code relevance; do not default important content into Business Rules.

- `Info`: facts/context/stage/lane positioning; not roadmap or normative behavior.
- `Naming Conventions`: terms/taxonomy/aliases/failure categories; remove-term remainder has no behavior rule.
- `Scope`: service/Target/support boundary and claim ceiling.
- `Exclusions`: explicit non-goals, unsupported/later/unauthorized scope.
- `Business Rules`: conditions, constraints, transitions, validation behavior, failure closure.
- `Standards`: schema, fields, protocols, formats, naming standards.
- `Schedule`: roadmap, maturity, phase, milestone, sequence, pause/resume.
- `Deliverables`: code, reports, fixtures, Markdown, schema files, concrete outputs.
- `Equipments and Tools`: CLI, validator, provider, script, tool, runtime dependency.
- `Team`: roles, reviewers, lanes, collaboration, approval ownership.

## Business Rules Admission Test

Place a statement in Business Rules only when it describes a condition, constraint, behavior, transition, validation rule, or failure boundary; would need code/schema/validator/runner/test evidence; and is not merely positioning, roadmap, maturity, taxonomy, Target, field/protocol, artifact, tool, or team role.

Default routing: maturity/roadmap/phase/level go to `Project / Schedule`; lane/discovery/stabilization state to `Info` or `Scope`; Observation Envelope, Trial Record, Artifact Package, failure taxonomy, and Candidate/Formal M1 terms to `Naming Conventions`; field protocols to `Standards`; enforceable rejection/threshold/authority rules to `Business Rules` or `Scope`; Target Lane definitions to `Scope` or `Target Profiles`, not Business Rules merely because they are important.

Business Rules may use a semantic parent and topic groups for shared leaves. Parent grouping is KYM organization only; do not generate M, TSP, Coverage, or TCO artifacts.

## Target Gate

Keep `target_profiles=[]` unless Target is explicit or preserved. A Goal, Session, trial, plan, or execution DAG is not a Target unless the source identifies enduring user/product value or says the Goal is a Target.

Every generated Target Profile must have a current-source-supported label, objective, refs, claim ceiling, and exclusions. Never import labels or a paired Target shape from a shipped example. Keep effect-status axes distinct from final verdicts.

## Pre-Validation Checklist

Before validate/lint/audit/store/render/preview, confirm Business Rules answer what code/schema/validator/runner/test must enforce; other nodes answer only their question; non-Business-Rules content cannot be read as M/TSP/Coverage scope; strict lint has no high-confidence finding without waiver.

Lint is read-only; errors block; findings need fix/waiver. Passing lint/audit approves no KYM, Target, TCO, Coverage, or accepted state.
