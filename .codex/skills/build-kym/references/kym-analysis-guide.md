# KYM Analysis Guide

Use with `kym-contract.md`; fixed schema, provenance, revision identity, and candidate boundaries remain authoritative.

## Two passes

Ledger first: source actor/authority, evidence, subject, predicate, purpose, time, statement/support type, candidate node, excluded neighbors, conflict/uncertainty, explicit vs inferred.

First classify source units as product-domain, source identity, or host-execution metadata. Exclude coding-host/session/worktree/global-instruction/child-agent details; a named Agent/Skill/CLI remains eligible only when it is the analyzed product's capability. Then walk fixed nodes backwards against admission questions. Empty nodes are valid; silence is not `N/A`. Route `source -> actor Who -> actor Why -> Product boundaries/rules -> Project execution`.

## Primary role decision

Use one primary branch per Product actor. Customers pay/request/accept; Users directly use; Stakeholders govern or are affected/benefit. Speaker/source authority is provenance; “User” is not enough. A product-direction decision maker is a Stakeholder when governance is supported. Uncertain classification is unresolved, not duplicate placement. Who/Why may be third-level statements, not new fixed fields.

## Product

- `Info`: reference facts only; future/normative behavior belongs elsewhere.
- `Naming Conventions`: definitions, distinctions, taxonomy. Remove term name; if remainder still says must/must not/should/when, split to `Business Rules`, `Scope`, or `Exclusions`.
- `Scope`/`Exclusions`: included vs excluded/later boundary; flag polarity conflicts.
- `Business Rules`: supported conditions, constraints, behavior, failure boundaries, and coarse value-linked capabilities; not a feature dump, artifact list, or TCO tree. Shared leaves may use a semantic parent/topic groups, not a schema node or M/TSP/Coverage/TCO output.
- `Documents`: material identity; `Problems Unsolved`: unresolved domain questions; `Standards`: protocols, policies, formats.

## Project

`Deliverables` are code/docs/packages/reports/fixtures/rendered files/scripts; a capability such as “auditable” or “supports parallel work” is not itself a Deliverable. `Equipments and Tools` are named tools; leave empty when source-silent. `Schedule` has milestones/time/order/dependencies/pauses. `Team` has execution/authority/review/independence roles. Do not restore Project Information or Dependencies. For Goal/plan inputs, read `kym-reader-quality.md`: a Goal/trial is not automatically a Target Profile, and source status is not automatically current truth.

## Target Profiles and explicit context

Keep `target_profiles=[]` for ordinary requests. Only an enduring Target explicitly named by the current source opens Profile generation. Each Profile uses a current-source-supported label/objective with candidate-statement refs, claim ceiling, and exclusions; do not reuse historical example labels or paired shapes. Profiles cannot cite disputed, unresolved, superseded, or Naming-only statements.

## Conflict and inference

Do not use latest-answer-wins. Preserve disputed or unresolved claims with provenance; supported corrections may supersede older statements. Inference is optional, explicitly `inferred`, authority-labelled, and source-motivated; external knowledge must be ingested as `externally_enriched`.

## Candidate-only review

Run `kym lint` and bundled `scripts/audit_candidate.py` before candidate readiness. Both are read-only/model-free and never create accepted-state artifacts. Errors block; strict high-confidence findings need fix/waiver. Clean validation, audit, Golden similarity, or Markdown never equals accepted state.
