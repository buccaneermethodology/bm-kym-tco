# Changelog

## [0.4.4] - Unreleased candidate

- Add an explicit released-identity overlay for the two Skills while keeping source manifests marked `v0.4.4-candidate/unreleased`.
- Add an external-host profile pinned to exact v0.4.4 released Skill manifests, trees, and SKILL.md bytes; mixed, candidate, or modified identities fail closed.
- Add deterministic local public projection, package evidence, and a dynamic file-level rights ledger/receipt template. These remain candidate evidence and do not authorize publication or Skill installation sync.
- Align the CLI runtime version with package metadata at 0.4.4. KYM/TCO/Coverage outputs remain candidate-only.

## [0.4.3] - Unreleased candidate

- Add an installed-CLI host workflow for external business workspaces. The target workspace does not need the tool source tree or internal host registry.
- Add deterministic source-ledger, Skill-selection, host-preflight, external-run, and candidate-consumption checks with fail-closed handling for invalid or partial results.
- Make the two bundled Skills and end-user guides usable without the tool's development-only governance Skill or repository-local knowledge files.
- Preserve candidate-only lifecycle and keep semantic review, human approval, release authorization, and runtime validation as separate decisions.
- This is an unreleased candidate. It does not claim external rights approval, a published package, universal host automation, or approved KYM/TCO/Coverage results.
