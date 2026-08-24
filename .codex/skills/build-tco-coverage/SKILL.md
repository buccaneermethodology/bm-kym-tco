---
name: build-tco-coverage
description: Build or revise candidate-only TCO models, Target Profiles, Coverage proposals and previews from explicit KYM/Target inputs. Do not create accepted state.
---

# Build TCO Coverage

Use this Skill for semantic decomposition and proposal drafting. The `tco` CLI validates, verifies, compares, packages and renders candidate artifacts deterministically; it does not call an LLM and does not approve Coverage.

## Preconditions

1. Run `tco --help` and, when available, `tco --workspace <workspace> doctor --product-id <id> --skill-dir <skill-dir> --expected-cli-version 0.4.1`.
2. Require explicit candidate KYM revision, Target/Profile inputs and source boundaries.
3. Read `references/tco-contract.md` and `references/tco-modeling-guide.md` before decomposing.

## Workflow

1. Preserve every existing `source_target_id` and compare revisions against a base model.
2. Build a Capability Projection Ledger from source-supported requirements to Mc/M1/Policy. An M1 must have one owner, testable boundaries, complete v4 semantics and no hidden children.
3. Keep operation, inputs, outputs, decisions, terminal outcomes, metadata and excluded outputs separate. Resolve cycles, missing semantics and denominator drift before Coverage.
4. Keep lifecycle, validation, Coverage/evidence, breadth and resolution axes orthogonal. Candidate evidence does not become semantic approval.
5. Write proposal-only Coverage with one row per requirement; mandatory misses remain Gap and `not_evaluated` stays `not_evaluated`.
6. Run the reader-language audit early, then use deterministic commands such as:

   ```bash
   tco --workspace <workspace> validate-candidate-model --product-id <id> --model <model.json>
   tco --workspace <workspace> validate-candidate-coverage --product-id <id> --model <model.json> --coverage <coverage.json>
   tco --workspace <workspace> verify --product-id <id> --output <verification.json>
   tco --workspace <workspace> package-candidate --product-id <id> --model <model.json> --coverage <coverage.json>
   ```

7. Render previews and report Met/Gap, evidence identity, claim ceiling and unresolved items. Stop at candidate-only review readiness.

## Boundaries

- Do not infer Target requirements from repository evidence or create approval/promotion/receipt state.
- Do not call an external Provider from the CLI or claim semantic synthesis correctness from deterministic checks.
- Do not treat a preview, package or runtime witness as production readiness.
- Keep human review, approval and any external authority separate from this Skill.

## Portable host behavior

If the host provides its own governance or review gate, follow it. Otherwise remain candidate-only and stop for human review. This public Skill does not require any private repository Skill or private path.
