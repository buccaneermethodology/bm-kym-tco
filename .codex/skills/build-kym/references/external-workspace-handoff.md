# External business workspace handoff


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

