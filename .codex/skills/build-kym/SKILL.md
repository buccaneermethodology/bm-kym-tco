---
name: build-kym
description: Build or revise source-traceable fixed-template KYM JSON and reader Markdown from sources. Use for drafts, revisions, actor classification, inference, conflicts, and candidate Target Profiles; not for TCO coverage, implementation evidence, or accepted state.
---

# Build KYM

Create source-traceable KYM. JSON is truth; Markdown is deterministic. CLI validates/stores, not semantics.

## Preconditions

1. Run `kym --help`. If missing, fill placeholders in `references/doctor-cli-missing-report.json` and print the complete JSON to stdout only; this is environment diagnosis. Otherwise run `kym --workspace <workspace> doctor --product-id <product-id> --skill-dir <this-skill-dir> --expected-cli-version 0.4.3 [--output <report.json>]`. `ready` is not semantics or approval.
2. Choose a stable lowercase `product-id` and workspace.
3. Read [references/kym-contract.md](references/kym-contract.md) completely before drafting or validating JSON.
4. Read [references/kym-analysis-guide.md](references/kym-analysis-guide.md) completely for multi-actor, ambiguous, conflicting, inferred, or long-source analysis.
5. Read `references/kym-semantic-placement.md` completely for Product/Project placement, Naming Conventions, lint review, and pre-draft placement before any non-trivial KYM. The bundled reference is self-contained and applies to external workspaces. It keeps roadmap, taxonomy, protocol, Target, and lane concepts out of Business Rules. For Goal、Loop Goal、Stage Plan、执行计划或治理角色输入，also read `references/kym-reader-quality.md` before drafting.

## Workflow

### External business workspace handoff

This Skill distribution is versioned independently from the installed CLI. In an external business repository, use the installed `tco` CLI; the project does not need `src/kym_tco`, `governance/supported_host_registry_v1.json`, or the tool's source repository. The host does not automatically attach a pipeline to every Skill invocation: an operator must run the documented commands after both candidate packages exist.

1. Record the raw source inputs in a workspace-relative `source_input_manifest_v1` file and confirm its source refs/digests. Ask the installed CLI to normalize and freeze the exact manifest bytes and current source bytes:

   ```bash
   tco --workspace "$PWD" source-ledger build \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --output .kym-tco/runtime/source-ledgers/<revision>.json
   tco --workspace "$PWD" source-ledger verify \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --ledger .kym-tco/runtime/source-ledgers/<revision>.json
   ```

2. Inspect the host's actual project/global Skill selection. A project copy takes precedence when present; if that selected copy is broken or incompatible, stop and repair it rather than silently choosing global. If the host cannot provide a verifiable resolver attestation, write a short operator evidence note in the workspace and create a `selection-record` with both actual Skill directories, source labels, reasons, operator ID, and evidence ref. `operator_confirmed` is the operator's assertion, not a CLI claim that it observed the host UI. `caller_declared` inputs alone cannot authorize `external-run`.

   ```bash
   tco --workspace "$PWD" selection-record create \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> --build-kym-reason '<why this copy was selected>' \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> --build-tco-coverage-reason '<why this copy was selected>' \
     --operator-id <operator-id> --evidence-ref .kym-tco/runtime/skill-selection/selection-review.md \
     --output .kym-tco/runtime/skill-selection/current.json
   ```

   Add `--project-copy-exists build-kym` and/or `--project-copy-exists build-tco-coverage` when that project copy exists. The evidence note is written and reviewed by the operator before running the command.
3. Build and package the KYM candidate by following this Skill. Hand off the candidate file refs, source manifest refs, and source boundary to `build-tco-coverage`; do not call the CLI evidence bundle a candidate package.
4. After both candidate packages exist, run `tco host-preflight` for each selected Skill, then the supported deterministic pipeline:

   ```bash
   tco --workspace "$PWD" external-run \
     --host-profile installed_cli_external_workspace_v1 \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> \
     --source-manifest .kym-tco/<product-id>/source-inputs.json \
     --source-ledger .kym-tco/runtime/source-ledgers/<revision>.json \
     --skill-selection-record .kym-tco/runtime/skill-selection/<selection-id>.json \
     --output <kym-candidate-file> --output <tco-candidate-file> \
     --attempt-id <unique-attempt-id>
   ```

5. The command returns a `bundle_ref` for the CLI **validation evidence bundle**. The Skill-generated **candidate packages** remain separate and unchanged. Supported consumers check the candidate ref and that bundle together:

   ```bash
   tco --workspace "$PWD" external-consume-check \
     --candidate <candidate-file> --evidence <bundle-ref>
   ```

   Only `consumable=true` means that candidate's bytes match a passing deterministic bundle; it remains `candidate_not_approved`. A failed, blocked, unknown, or retained-partial result is not consumable. Read `.kym-tco/runtime/host-failure-records/` and the referenced bundle/disposition; after changing inputs, Skill selection, or candidate bytes, retry with a new attempt ID. A successful gate never means semantic correctness, Coverage Met, approval, or release.

### Internal tool-development repository

When maintainers use this Skill in the tool's own source repository, retain its repo-local Supported Host registration and governance process. Those maintainer controls are separate from the external-workspace flow above and are not prerequisites for ordinary business repositories.

1. Initialize once:

   ```bash
   kym --workspace <workspace> init --product-id <product-id> --coverage-mode <repo|not_evaluated>
   ```

2. Snapshot sources with `kym ingest`; for visible chat, capture only verbatim in-scope turns.
3. Inspect legacy Markdown with `kym inspect-legacy`; findings are review inputs, not edits. Read manifests before chunks.
5. Before any write, gate intent: discussion, analysis, diagnosis, review, or design produces no KYM revision. A draft request produces candidate Target data only.
6. Classify source units as `product-domain`, `source-identity`, or `host-execution-metadata`. Exclude coding-host worktree/branch, global instructions, child-agent/session and model/tool/token budgets; retain Agent/Skill/CLI/LLM only as an explicit product capability. Then ledger authority/evidence, subject/predicate/purpose, statement/type/time/support, node/excluded neighbors, conflict/uncertainty/inference. Follow `source -> actor Who -> actor Why -> Product boundary/rules -> Project execution`.
7. Use the fixed `Customer / Product / Project` template. Preserve every required second-level node even when empty; do not rename, reorder, omit, or add nodes.
8. Separate source identity from Product actor: speaker/authority is provenance, not a role. Classify each actor once by its primary role: Customer pays/requests/accepts; Users directly use; Stakeholders govern or are affected/benefit. A supported product-direction decision maker is a Stakeholder; never infer a role from “User”.
9. Split mixed source text before placement. If one sentence defines a term and states a rule, create separate statements. Apply the remove-term test: if the remainder still says what must, must not, should remain, or when something happens, it is not only a Naming Conventions definition.
10. Draft complete `kym_model_v1`. Each statement needs stable ID, type, time/support status, authority, exact source refs, and node IDs. Mark non-explicit interpretation `support_status=inferred`; inferred sets need not be identical across runs.
12. Run the placement gate before topic branches. Put facts in `Info`, definitions/taxonomies in `Naming Conventions`, Target and claim boundaries in `Scope`, explicit non-goals in `Exclusions`, protocols/formats in `Standards`, roadmap/maturity/ordering in `Project/Schedule`, and concrete artifacts in `Deliverables`; keep ambiguous UI/API unresolved. Only implementation-enforced conditions, constraints, behavior, and failure boundaries may enter `Business Rules`. Business Rules are not a feature/artifact list or TCO tree; shared leaves use a source-derived semantic parent, never a title copied from a shipped example.
13. Preserve conflict and uncertainty as `disputed` or `unresolved`; never invent consensus or treat `unsupported` as baseline.
14. Keep `target_profiles=[]` unless current source explicitly names an enduring product/user Target or a revision preserves profiles. Goal/trial plan alone does not open Profile generation. Bind every label/objective/requirement to current source; never reuse example labels or paired shapes. Profiles remain candidate data.
15. 在 `coverage_mode=repo` 且配置 `evidence_roots` 时，于首个 candidate 前运行 `kym ingest-repo --product-id <id> --path <configured-relative-path>`；repo facts 必须保持 `externally_enriched` provenance、不得覆盖用户声明，冲突保持 disputed/unresolved。未配置 roots 才可明确退回 source-only。
16. Run the reader-quality pass in `references/kym-reader-quality.md`. Separate source-file snapshot/status, verified fact, proposal, Target, effect status and final verdict. JSON keeps statement refs; reader omits them. Inventory covers manifest/source/snapshot/content identities; content digest excludes `ingested_at` and matches closure/package. v2 shows Git/dirty/kind; v1 says not applicable. Drift blocks.
17. For populated Product branches,补充白话解释和必要的实例化 G-W-T；actor 与 implementation roles 分开。Specialized Golden fixtures are evaluation-only and must never become generic generation instructions.
18. Run placement checks, then package once; revisions add `--base`:

   ```bash
   kym --workspace <workspace> package-candidate --product-id <product-id> --candidate <draft.json> [--base <base-revision.json>]
   ```

   It uses one validate/lint/audit engine and staged readback, emitting revision/delta/reader/inventory/evidence/lineage/index/manifest/checksum. Initial delta is not applicable; revisions bind a real delta. Failure emits no revision/reader. `completed` stays `candidate_not_approved`, `approval_status=not_requested`.
19. Use manual commands or `scripts/audit_candidate.py` only for diagnosis; same-engine calls are not independent evidence.
20. JSON is provenance truth. reader-facing Markdown contains no machine HTML comments and comes only from renderer/package. Require byte-identical rerender, bare `#`, ASCII/two-space lists, no NBSP/Tab/bold heading/trailing spaces; preserve empty nodes/inventory.
21. Present revision, digest, delta, conflicts, bounds and findings.
22. The terminal state is a validated candidate package plus rendered reader Markdown, artifact index, manifest, and checksum; blocked/failed reports are honest terminals, not candidate readiness.
23. For KYM-to-TCO Automation, hand off only candidate revision/profile paths and source boundaries to `$build-tco-coverage`.

## Hard Boundaries

- Do not call an external Provider or claim CLI-generated semantics.
- Do not build TCO/M trees, inspect evidence, assign Coverage, or run implementation gates.
- Do not generate M, TSP, Coverage, or TCO artifacts from Business Rules; KYM parent decomposition never becomes a coverage tree.
- Draft, render, scheduled run, or passing gate is not accepted state. Do not create accepted-state artifacts from this Skill.
- Do not treat omitted optional inference as a defect; never label inference `source_supported`.
- Do not fabricate Why content, duplicate an actor to fill role branches, turn Business Rules into a feature list, or treat a capability as a concrete Deliverable.
- Do not equate Goal/Session/trial/plan with Target, merge verdict/effect axes, or call source-only status verified current state.
- Do not pad `Equipments and Tools` with roles, artifacts, or unnamed generic tooling; keep execution dependencies out of `Deliverables` and governance participants out of Customer branches.
- Source silence is not `N/A`: use an unresolved statement or leave the fixed node empty.
- Do not place capability maturity, roadmap, lane relationship, observation envelope, failure taxonomy, Candidate/Formal M1 terminology, or Target Lane definitions under `Business Rules` just because they are important. Route them through the placement gate; only implementation-enforced constraints belong in `Business Rules`.
- KYM, Target, manifest, snapshot, chunk/turn metadata, or source identity changes require a fresh validate/lint/audit/store/render cycle.
- Preserve earlier Target Profiles when creating a new revision.

## Output

Report paths, conflicts/questions, inference and gates. End at `draft ready for review` or `candidate ready for downstream preview`, never accepted state.
