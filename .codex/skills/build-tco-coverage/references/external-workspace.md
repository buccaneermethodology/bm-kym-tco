# External business workspace handoff

This Skill distribution is independently versioned from the CLI. For an external business repository, use the installed `tco` CLI. The business repo need not contain `src/kym_tco`, the internal host registry, or tool source. Skill invocation does not automatically trigger the pipeline on arbitrary platforms; run the following host calls explicitly after the semantic candidate work is complete.

1. Build the Coverage proposal, validate it, and package it as a Skill-generated candidate package. Preserve source/Target boundaries and do not treat CLI validation as semantic synthesis.
2. Build and verify the source ledger against one or more raw manifests and current workspace bytes:

   ```bash
   tco --workspace "$PWD" source-ledger build \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --output .kym-tco/runtime/source-ledgers/<revision>.json
   tco --workspace "$PWD" source-ledger verify \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --ledger .kym-tco/runtime/source-ledgers/<revision>.json
   ```

   Repeat `--input-manifest` for additional manifests. The ledger retains each raw manifest byte digest, declared denominator, normalization rule version, and actual source bytes. `external-run` repeats this check; changing a manifest or source invalidates the ledger.
3. Inspect the host's actual project/global Skill selection. If a project Skill exists, it takes precedence; a broken or incompatible project copy blocks the run and must not fall back to global. When no verifiable host resolver attestation is available, save a short operator evidence note in the workspace, then create a durable `selection-record` with both selected directories, source labels, reasons, operator ID, and evidence ref. `operator_confirmed` records the operator's assertion and does not claim the CLI observed the UI. Caller-declared paths alone are diagnostic and cannot authorize the run.

   ```bash
   tco --workspace "$PWD" selection-record create \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> --build-kym-reason '<why this copy was selected>' \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> --build-tco-coverage-reason '<why this copy was selected>' \
     --operator-id <operator-id> --evidence-ref .kym-tco/runtime/skill-selection/selection-review.md \
     --output .kym-tco/runtime/skill-selection/current.json
   ```

   Add `--project-copy-exists build-kym` and/or `--project-copy-exists build-tco-coverage` when that project copy exists. The operator writes and reviews the evidence note before recording the assertion.
4. Run `tco host-preflight` for each actual selected Skill directory. It only verifies installed runtime/profile and Skill identity; it does not run candidate gates. Then explicitly invoke the deterministic pipeline:

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

5. The `bundle_ref` returned by the CLI names the **validation evidence bundle**, not another candidate package. A supported consumer verifies each candidate against that bundle:

   ```bash
   tco --workspace "$PWD" external-consume-check \
     --candidate <candidate-file> --evidence <bundle-ref>
   ```

   `consumable=true` means the referenced bytes match a passing deterministic validation bundle and still have lifecycle `candidate_not_approved`. Failure, blocked, unknown evidence, or retained partial output is not consumable. Inspect `.kym-tco/runtime/host-failure-records/` and bundle disposition; change the attempt ID for every retry after changing source, Skills, versions, or candidate bytes. A pass does not mean semantic correctness, Coverage Met, approval, promotion, or release.

