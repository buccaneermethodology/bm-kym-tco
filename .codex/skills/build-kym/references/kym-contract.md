# KYM Contract

## Authority and statements

`source snapshot -> draft -> validated candidate revision -> rendered reader Markdown -> candidate handoff`.

Every statement has stable `id`, `statement_type`, `temporal_status`, `support_status`, `authority`, exact `source_refs`, and `tree_node_ids`. `inferred` remains provenance-bound; external enrichment is a separate ingested source.

## Fixed tree

```text
Customer
├── Customers
├── Users
└── Stakeholders
Product
├── Info
├── Naming Conventions
├── Scope
├── Exclusions
├── Business Rules
├── Documents
├── Problems Unsolved
└── Standards
Project
├── Deliverables
├── Equipments and Tools
├── Schedule
└── Team
```

Keep fixed second-level nodes, in order, even when empty; do not rename or add them. Third-level nodes may follow source meaning.

Roles are exclusive by primary product relation: Customers pay/request/accept; Users directly use; Stakeholders govern or are affected/benefit. Source speaker/assistant/authority is provenance, not Product actor role; do not map “User” or a speaker name automatically. Do not repeat the same actor. A product-direction decision maker belongs in Stakeholders when governance is supported.

`Info` is context; `Naming Conventions` terminology/taxonomy; `Scope` included boundary; `Exclusions` excluded/later behavior; `Business Rules` conditions, constraints, behavior, failure boundaries; `Documents` source/reference artifacts; `Problems Unsolved` unresolved problems; `Standards` protocols/policies/formats. `Deliverables` are artifacts; `Equipments and Tools` tools; `Schedule` time semantics; `Team` roles/collaboration. Do not turn capabilities into deliverables or rules into feature lists. Shared Business Rules leaves may use a semantic parent/topic groups, but must not create M, TSP, Coverage, or TCO artifacts.

Naming root must not own statements directly. Apply the remove-term test and split mixed definition/rule text. Ambiguous UI/API stays unresolved. Source-silent interpretations are optional but must be `inferred` with authority and refs.

## Target, revision, lint

Target Profiles live under `Product/Scope/Target Profiles`; ordinary drafts keep `target_profiles=[]`, while revisions preserve prior profiles. If the source names `Target-B Mainline` and `Target-A Calibration/Promotion Side-lane`, require two complete candidate Profiles with unique `id`, `label`, `objective`, non-empty `requirement_refs`, `claim_ceiling`, and `exclusions`. Store complete candidate revisions and retain superseded statements.

`kym lint` is read-only/model-free. It preserves finding fields and adds `confidence`, `resolution_required`, `current_node_ids`, `suggested_node_ids`. Exit `0` clean, `1` deterministic error or strict high-confidence risk, `2` input/contract error. No lint/Markdown/test result creates accepted state.
