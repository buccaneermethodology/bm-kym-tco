---
name: build-kym
description: Build or revise source-traceable fixed-template KYM JSON and reader Markdown from explicit sources. Use for drafts, revisions, actor classification, inference and conflicts; do not create accepted state.
---

# Build KYM

Use this Skill to help an agent draft a source-traceable KYM. The JSON is the semantic candidate; the Markdown reader is a deterministic view. The `kym` CLI validates, lints, packages and renders; it does not call an LLM and does not approve the result.

## Preconditions

1. Run `kym --help` and, when available, `kym --workspace <workspace> doctor --product-id <id> --skill-dir <this-skill-dir> --expected-cli-version 0.4.1`.
2. Choose a stable lowercase `product-id` and explicit workspace.
3. Read `references/kym-contract.md` and `references/kym-analysis-guide.md` before drafting.
4. Preserve the exact source boundary, actor/authority, source refs, temporal status, support status and inference boundary.

## Workflow

1. Snapshot or reference the supplied source with a stable manifest.
2. Build a statement ledger before writing JSON: source unit, actor, claim, candidate node, conflict, uncertainty and excluded neighboring placement.
3. Use the fixed `Customer / Product / Project` hierarchy. Keep required nodes present even when empty; do not invent consensus.
4. Classify actors by supported role; speaker identity is provenance, not automatically a Product role.
5. Mark non-explicit interpretations as `support_status=inferred`; retain `disputed` and `unresolved` rather than silently resolving them.
6. Keep Target Profiles candidate-only and separate from approval. Do not turn Goals or plans into accepted Targets.
7. Run placement and reader-quality checks from the bundled references. Machine provenance stays in JSON; the reader should remain readable.
8. Package with one deterministic command, for example:

   ```bash
   kym --workspace <workspace> package-candidate --product-id <id> --candidate <draft.json>
   ```

9. Report revision, digest, conflicts, bounds and findings. Stop at a reviewable candidate.

## Boundaries

- Do not call an external Provider from the CLI or claim that the CLI generated semantics.
- Do not create baseline, Target, Coverage or approval receipts automatically.
- Do not build TCO/M trees from KYM Business Rules; hand off only an explicit candidate KYM and source boundary to `build-tco-coverage`.
- Passing validation, rendering or packaging is not semantic approval, promotion or production readiness.

## Portable host behavior

If the host provides its own governance or review gate, follow it. Otherwise remain candidate-only, preserve provenance and stop for human review. This public Skill does not require any private repository Skill or private path.
