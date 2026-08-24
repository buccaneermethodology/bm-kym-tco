# TCO Modeling Guide

Use for semantic decomposition and candidate review; contract and CLI remain authoritative.

## Stable Model And Profiles

Keep surfaces separate: stable M tree and TSP; multiple persistent Target Profiles; one current Coverage evidence inventory; optional execution observations.

Reuse existing M for the same independent capability/policy. Add/split only independently designable, testable, acceptable units. Groups never count.

## M1 And Claim Boundaries

TSP is the human explanation, not an implementation shorthand. Each M1 Topic uses complete prose to explain when a user needs the ability and what help they receive. Every Scope included item separately says what users get; every excluded item says what they do not get or must still decide elsewhere. Purpose explains user value or misuse risk. Terms such as KYM, TCO, Coverage, schema or digest need plain-language context; field names, commands and Agent actions are not explanations. Preserve one-to-one meaning and run `scripts/audit_reader_language.py` early. For v4, `render-preview` rejects findings before writing candidate HTML; `package-candidate` records a blocked failure package, not a ready package. This expression-only gate never creates semantic approval.

TSP excludes implementation state, tests, gaps, dates and planning. Run observations belong in `tco_execution_result.v1`; v4 structured fields feed `coverage_requirements`. `decision_outputs` lists classifications; only runtime-proven execution-terminal outcomes enter current v4 `terminal_outcomes`. Avoid unscoped `reject/rejected`, outcome `candidate_input`, P06 metadata artifacts and policy-as-capability.

`excluded_outputs` is not non-goals. High-risk M1s need source-backed `claim_exclusions` or not-applicable rationale; examples are not enforcement unless implemented.

Separate candidate synthesis, review readiness, honest outcome and P06/Formal authority. `review_ready != P06 eligible != Formal M1`. Provider configuration evolution/comparability is not attempt metadata. MC children must share lifecycle, owner and acceptance ladder or provide `grouping_rationale`; regrouping never changes IDs, mappings, Coverage or approval.

Keep lifecycle, validation posture, Coverage/evidence, breadth and resolution status separate. Lifecycle is only `draft`, `contract_defined`, `implemented`, `maintained`, `deprecated`; `bounded_runtime`, `multi_repo_evidence`, `independently_validated` and legacy `unresolved` belong elsewhere. Maintained needs registration/owner/gate; deprecated needs replacement/sunset and cannot satisfy maintained Targets by rank.

## Target Profiles

- Each Target requirement appears once as `source_requirement_ref`; one requirement may map to multiple non-group M nodes, and one M may serve multiple requirements/Profiles.
- Profile denominator is requirement count, not M count. Carry old Profiles and pass `--base-model` to detect silent deletion.
- Requirement evidence must cohere with mapped M1: source ref/rationale, witness/NF subsets, distinct validation owner, capability-specific claim ceiling. Shared bundles need rationale.
- Requirement mapping must be semantically compatible across domain, capability, purpose, I/O, owner, witness/NF, claim ceiling and traceability; mismatch requires split or gap.
- A requirement may map to materialization plus readiness, but it still counts once. Coverage IDs, witness/NF mappings and predicates show which M1 predicate is covered.
- Requirement thresholds use separate `minimum_lifecycle_state`, `minimum_coverage_state`, `required_validation_posture`, `required_breadth`; no single maturity label.
- Lane-authority/relationship requirements map to authority-separation capability such as `m1-preserve-truth-boundaries`; candidate artifacts or P05 owners are wrong-domain evidence even if `node_ids` are correct.
- Cross-configuration trial/version/supersession/comparability maps to configuration-version management or a gap M1, not identity-freeze, manifest-attempt, observation-envelope or stale-evidence M1s.
- Long-term family/vision Profiles need `non_executable_vision_profile` or equivalent; not executable closeout denominators.
- Failure taxonomy is independent when testable. Baseline classes: `grounding_failure`, `extraction_failure`, `overclaim`, `lineage_failure`, `execution_failure`, `authority_failure`; `next_action`, rerun and routing are advisory or separate M1s.

## Coverage Evidence

Coverage is current evidence, not TSP or accepted state. Repo mode has one record per M1/Policy; v4 has one `requirement_coverage` row per `coverage_requirements.id`; legacy `scope_coverage` is compatibility only. `candidate_evidence` is visible but never counted.

Counted path evidence stays in trusted roots/kinds; command evidence names a registered command for the current M but cannot prove it ran. Advanced states need evidence and bounded claim ceiling. Multi-repo/trial breadth with mixed Provider configurations needs comparability verdict/eligibility; `non_comparable`, `comparability_unknown` and mutable aliases stay gaps.

The weakest direct state or mandatory coverage requirement state determines v4 effective Coverage. Direct state cannot hide a weaker branch. Coverage IDs align to declared inputs/outputs/outcomes/failures/metadata. Any state above `not_started` needs counted evidence.

A Gap is a Target requirement whose mapped M set misses the threshold. Report denominator, Met, Gap, node gaps and claim ceiling. In `not_evaluated`, report N/A, not 0/N. TCO never auto-writes a host Dashboard or Goal record.

## Candidate Review

Run model-only audit before Coverage drafting, or model plus Coverage audit before the candidate package: `python3 <resolved-build-tco-skill-dir>/scripts/audit_candidate.py --workspace <workspace> --product-id <product-id> --model <tco.json> [--coverage <coverage.json>] [--base-model <base-tco.json>]`.

The tool is read-only, writes JSON to stdout, never writes a workspace or accepted state. Exit `1` blocks readiness; warning-only exit `0` still needs judgment. Contract/I/O failure exits `2`.

Review: Profile/M deletion; duplicate/group mappings; placeholder evidence; M1-external witness/NF; copied owner; source/semantic mismatch; missing claim exclusions; lane-authority mismatch; configuration evolution as identity freeze; MC mix without `grouping_rationale`; vision Profile as executable closeout; mixed-config breadth without comparability/immutable alias; taxonomy collapsed into outcome/routing; generation with maturity/authority outputs; incomplete readiness M1; Coverage vocabulary in TSP; direct state hiding weak mandatory rows; advanced state with weak evidence/unbounded claim; cross-axis values; multi-repo without identity matrix; maintained/deprecated missing refs.

Model validation, audit success, verification, preview rendering or Profile comparison does not create accepted state.
