---
name: build-tco-coverage
description: Build or revise candidate-only TCO models, Target Profiles, Coverage proposals, verification evidence, comparisons, and tco-preview.html from KYM/Target inputs. Use for M/TSP/Coverage review or migration. Do not create accepted state.
---

# Build TCO Coverage

The agent performs semantic decomposition; `tco` deterministically validates, verifies, compares, and renders candidates.

## Preconditions

1. Run `tco --help`. Missing: fill `references/doctor-cli-missing-report.json` and print stdout; no semantic verdict. Available: run `tco --workspace <workspace> doctor --product-id <id> --skill-dir <skill-dir> --expected-cli-version 0.4.6`. `ready` means environment only.
2. Require explicit candidate KYM revision/profile inputs and source boundaries.
3. Read [references/tco-contract.md](references/tco-contract.md) when drafting model, Coverage, or project config.
4. Read [references/tco-modeling-guide.md](references/tco-modeling-guide.md) completely when decomposing M/TSP, preserving Profiles, assigning Coverage, or reviewing gaps. It guides analysis; the contract/CLI remain authoritative.

## Workflow

### External business workspace handoff

Use the installed CLI in external business workspaces. Read [references/external-workspace.md](references/external-workspace.md) for exact source-ledger build and source-ledger verify, selection-record, host-preflight, external-run and external-consume-check calls. Business repositories need no tool source or internal registry. The pipeline is explicit; arbitrary platforms do not automatically invoke it. Project Skill selection takes precedence; broken/incompatible project copies block rather than fall back. An operator_confirmed selection is an operator assertion, not host resolver attestation. The CLI bundle_ref names the validation evidence bundle, not a candidate package; consumable=true only binds passing deterministic evidence to candidate_not_approved bytes.

Source Skill changes remain unreleased candidates. A released CLI pins released Skill identity; adding progress tools does not authorize bypassing that check or claim the new source Skill works with the pinned external-run profile. Direct progress companion use needs only Python and workspace inputs.

### Task contributions and stage progress

For task/Goal-driven updates, read [references/tco-progress.md](references/tco-progress.md). Keep stable M1 capabilities; link independently acceptable progress items and each included task's actual contribution. Record evidence-only changes, new gaps, regressions or no-change reasons as well as forward movement. Use a new snapshot with an explicit baseline and cutoff; retain unresolved atomic requirements and historical unknowns. Never force a Coverage/lifecycle upgrade because a task ended, or turn task counts into product completion rates. Reference task IDs from the project's execution system; this companion does not own task lifecycle or schedule work.

After existing CLI candidate checks/package, run `python3 <resolved-build-tco-skill-dir>/scripts/progress.py validate --workspace <workspace> --progress <progress.json>`, then `render` with the same inputs and `--output-dir <new-directory>`. Validation failure blocks the progress preview; it does not change the original candidate. Return both the CLI tco-preview.html and the progress Markdown/HTML. Show before/after capability axes and bound CLI report results separately from local progress; absent reports mean unknown, not zero or pass. These steps apply to both external and internal workspace routes.

### Internal tool-development repository

When maintainers use this Skill in the tool's own source repository, retain its repo-local Supported Host registration and governance process. Those maintainer controls are separate from ordinary business-workspace use.

1. Initialize or reuse `.kym-tco/<product-id>/`.
2. Read the candidate KYM revision and selected candidate Target only. Do not load the original long conversation unless a provenance dispute requires it.
3. Draft a stable TCO v4 model with a Capability Projection Ledger (`Business Rule → capability → M1/Mc/Policy`). Keep materialization, readiness and honest outcome separate; use v2/v3 only for migration. `mc` organizes domains, `policy` is a rule, and `m1` is one independently testable owned ability. Freeze `execution_semantics` (`compare|execute|ingest|package|render|synthesize|validate`) before Coverage; each output is exactly one of `materialized_output_ids|validated_output_ids|unresolved_output_ids`, and unresolved output blocks `bounded_runtime`.
4. Write reader-facing Topic/Scope/Purpose and run `scripts/audit_reader_language.py --model <model.json>`. Keep v4 scope as boundary only; use separate input/output/decision fields and `coverage_requirements` with `category/ref/mandatory`. Preserve prior Profiles, bind each Target requirement once to non-group M nodes, and never let repository evidence invent a Target requirement. Keep lifecycle, validation, Coverage, breadth and resolution axes orthogonal; cite source-backed claim exclusions.
5. Draft proposal-only Coverage from trusted evidence, never `candidate_evidence`; one row per requirement, mandatory misses block Met. `bounded_runtime` requires matching node/operation/output/effect and a workspace artifact whose bytes match `artifact_digest`; missing or stale witnesses fail closed. Use `tco verify` for registered checks. Register verification with absolute `argv[0]`, cwd, timeout, node IDs and allowed env; no shell.
6. Run:

   ```bash
   tco --workspace <workspace> validate-candidate-model --product-id <product-id> --model <model.json>
   tco --workspace <workspace> validate-candidate-coverage --product-id <product-id> --model <model.json> --coverage <proposal.json>
   tco --workspace <workspace> verify --product-id <product-id> [--node <node-id>] --output <verification.json>
   python3 <resolved-build-tco-skill-dir>/scripts/audit_candidate.py --workspace <workspace> --product-id <product-id> --model <model.json> --coverage <proposal.json> [--base-model <base-model.json>]
   ```

   Resolve the audit script from this `SKILL.md`. Witnesses bind node, operation, output, workspace artifact, byte digest and effect. Materialized paths are content-addressed and immutable across reruns; packaging preserves them. Verification never promotes state; audit errors block readiness.
7. For v4/v3, run `tco --workspace <workspace> package-candidate --product-id <id> --model <model> --coverage <proposal>`. It consumes drafts under one snapshot; v4 reader-language findings block ready packaging, `not_evaluated` stays `not_run`, and it never overwrites or approves.
8. Do not run accepted-state commands from this Skill.
9. Candidate commands accept v2/v3 legacy and v4. Migration stays `candidate_not_approved`, never copies approval or creates run results, and exposes unresolved legacy semantics as gaps. Preview/compare show full M1 contracts, separate state axes, Profile Met/Gap and bound observations; v2 `effect_status` remains compatibility text.

## Hard Boundaries

- Do not create accepted-state artifacts.
- Do not collapse TSP and Coverage; implementation status never belongs in Topic/Scope/Purpose.
- Do not count path existence, candidate evidence, or an unrun command as verified implementation.
- Capability verbs, execution semantics, outputs, witnesses and Coverage must describe one operation; validating an existing artifact does not prove its generation.
- v3->v4 migration cannot infer resolved operation or per-output effect from title keywords. Put legacy outputs in `unresolved_output_ids`, record the semantic gap in lineage, and cap migrated M1 Coverage/lifecycle/validation/resolution at `design_only | draft | not_validated | unresolved` until explicit review resolves it.
- Do not let a direct node state hide a weaker mandatory coverage requirement or legacy Scope state.
- Do not map Target requirements to group nodes, remove historical Profiles silently, mix Coverage vocabulary into TSP, or count command evidence registered for another M.
- Do not change Coverage state because a gate passed. Create a new candidate proposal.
- Do not put `bounded_runtime`, `multi_repo_evidence` or `independently_validated` in lifecycle; do not put `maintained` in Coverage; do not use legacy `unresolved` as new v4 lifecycle. `maintained` requires registration, owner and continuous gate. `deprecated` needs replacement/sunset and does not satisfy a maintained Target by rank.
- Automation may rerun candidate validation, `tco package-candidate`, `tco verify`, `tco render-preview`, and `tco compare-candidate-profiles`. Semantic changes produce a new candidate package; successful gates never promote Coverage or approval.

## Output

Report model/proposal/preview paths, vocabulary digest, exact profile denominators, Met/Gap, node-level gaps, verification failures, warnings, and claim ceilings. For task updates also report the fact cutoff, included/excluded stages, changed M1s, task contributions, unchanged-state reasons, atomic requirement dispositions, blockers and next steps. End at `candidate_not_approved` / `proposal ready for preview`, never accepted Coverage.
