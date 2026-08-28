# kym-tco

[中文说明](README.zh-CN.md)

Deterministic tooling for two agent workflows:

- `build-kym`: immutable source snapshots, KYM revisions, Markdown views, deltas, baseline approvals, and Target approvals.
- `build-tco-coverage`: approved-Target TCO models, evidence proposals, allowlisted verification, Coverage approvals, profile comparison, and HTML/JSON reports.

The CLIs never call an LLM. An invoking agent performs semantic synthesis; the tools enforce provenance, revision, approval, graph, vocabulary, and evidence boundaries.

## Install

```bash
uv tool install --editable .
kym --help
tco --help
```

Python 3.9+ is supported. Project data lives under `<workspace>/.kym-tco/<product-id>/`.

For a step-by-step Chinese guide covering both Codex Skills and CLI setup, see
[`docs/INSTALLATION_AND_USAGE.zh-CN.md`](docs/INSTALLATION_AND_USAGE.zh-CN.md).
For a beginner-friendly explanation of the KYM -> TCO generation principle,
see [`docs/KYM_TO_TCO_WORKFLOW.zh-CN.md`](docs/KYM_TO_TCO_WORKFLOW.zh-CN.md).
Open-source readiness notes are in
[`docs/OPEN_SOURCE_CHECKLIST.zh-CN.md`](docs/OPEN_SOURCE_CHECKLIST.zh-CN.md),
with contribution and security policies in
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md).

## Recommended Codex workflow

Use the two bundled Skills for semantic work. Start a new Codex task with the
following prompt, replacing `<conversation-or-document-path>` with the path to
the input you want to analyze:

```text
$build-kym

Create a KYM from this product conversation.
- Product ID: <product-id>
- Workspace: <workspace-path>
- Input file: <conversation-or-document-path>
- First produce a reviewable KYM draft. Do not automatically approve a baseline or Target.

The agent must:
1. Save an immutable input snapshot.
2. Produce KYM JSON with source references, statement types, temporal status, and support status.
3. Render a Markdown view.
4. List conflicts, unresolved questions, and the source boundary.
5. Stop at "draft ready for review".
```

After reviewing the draft, send a new, explicit reply:

```text
Approve the current KYM revision as the baseline.
```

Review the Target separately, then send another explicit reply:

```text
Approve Target-<target-id>.
```

Only after both the baseline and Target have valid receipts can TCO work begin.
Start or continue a Codex task with:

```text
$build-tco-coverage

Using the approved <product-id> KYM baseline and Target-<target-id>:
1. Create a stable TCO feature coverage tree.
2. Write a TSP for every M node.
3. Create a Coverage proposal.
4. Verify repository evidence.
5. Report the Target profile Met, Gap, and claim ceiling.
6. Do not automatically approve Coverage.
```

After reviewing the proposal package, explicitly approve it with:

```text
Approve the current TCO model and Coverage proposal.
```

The Skills perform the semantic drafting and decomposition. They keep every
approval separate from the request that creates the draft or proposal.

## KYM template and Golden examples

KYM drafts use a fixed reader-facing hierarchy: `Customer` contains
`Customers / Users / Stakeholders`; `Product` contains `Info / Naming
Conventions / Scope / Exclusions / Business Rules / Documents / Problems
Unsolved / Standards`; and `Project` contains `Deliverables / Equipments and
Tools / Schedule / Team`. Required first- and second-level nodes remain visible
when empty. Machine provenance stays in JSON and source manifests; rendered
Markdown contains no HTML validation annotations.

Each Golden example is a self-contained directory under
`tests/fixtures/golden/<example-id>/` with `conversation.md`,
`expected_kym.json`, and `expected_kym.md`. Source-silent interpretations are
optional, but any included interpretation must use `support_status=inferred`.

## Direct CLI use

To initialize a project directly from its repository:

```bash
cd /path/to/your/project

kym --workspace . init \
  --product-id <product-id> \
  --coverage-mode repo

kym --workspace . ingest \
  --product-id <product-id> \
  --source /path/to/conversation.txt
```

The project data is stored at:

```text
<workspace>/.kym-tco/<product-id>/
```

List all available commands with:

```bash
kym --help
tco --help
```

The CLI performs deterministic ingest, validation, diffing, receipts,
verification, and rendering. It does not interpret a conversation or generate
semantic KYM content by itself; use the two Skills above for semantic drafting.

## Repository governance

### Governed task entry

For a non-trivial persistent change in this repository, start with the repo-local checkpoint before any Builder write:

```bash
python3 .codex/skills/kym-tco-governed-checkpoints/scripts/task_entry.py assess --change-kind non-trivial
python3 .codex/skills/kym-tco-governed-checkpoints/scripts/task_entry.py bootstrap --repo . --session-id <S-xxx> --parent <SP-xxx> --slug <Slug> --topic <topic> --scope <scope> --purpose <purpose> --objective <objective> --claim-ceiling <ceiling>
python3 .codex/skills/kym-tco-governed-checkpoints/scripts/task_entry.py verify --repo . --session-id <S-xxx> --slug <Slug> --phase entry
```

This creates a `doing` Session, five Markdown entry artifacts, and a safe visible Agent Log; it does not mark work complete or replace independent Validation, Semantic Review, human authority, or reconciliation. Before closeout, explicitly route BDD, KB, and Dashboard through `verify --phase closeout`. The eight repo-local Skills share this bridge. It is a repository-local, detectable contract—not a promise that every host platform will automatically invoke a Skill.

Stable project and governance truth is stored as JSON under `kb/data/`.
`kb/docs/` is a generated reading surface; verify it with:

```bash
python3 kb/tools/render_kb.py --check
```

Execution state lives separately under `Dashboard/`. Upstream governance drift
is review-only: `tools/governance_sync.py validate` and `diff` never modify the
repository or approve an adaptation.

The canonical Dashboard field contract is
`kb/data/strategy/dashboard_governance_contract.json`. Lifecycle `Status` is
limited to `todo`, `doing`, `blocked`, `decision-needed`, `done`, and
`archived`; delivery boundedness, claim ceilings, authority/blockers, and
review verdicts remain separate. Validate current rows and a Git transition
baseline with:

```bash
python3 tools/dashboard_lint.py --check
python3 tools/dashboard_lint.py --check --base <git-ref>
```

The eight repo-local Skills have deterministic behavior fixtures and a
machine-readable comparison matrix. Local behavior checks always run; an
optional source review may compare upstream invariants without modifying this
repository:

```bash
python3 tools/governance_sync.py invariants
python3 tools/governance_sync.py invariants --source-root <semx-root>
```

The report distinguishes intentional local divergence from unexpected drift.
It never applies, merges, approves, installs, or publishes an upstream change.

## Version-pinned Skill distribution

For team use and reproducible results, select a specific release tag and use
the Skills committed in that tag. Do not copy an unversioned global Skill
directory between projects.

```bash
git clone --branch <release-tag> --depth 1 https://github.com/buccaneermethodology/kym-tco.git
cd kym-tco

python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --dry-run

# Install only the two generation Skills.
python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --dry-run

python3 tools/install_codex_skills.py \
  --source .codex/skills \
  --target /path/to/target-project/.codex/skills \
  --skill build-kym \
  --skill build-tco-coverage \
  --install
```

The default operation is a dry-run. Installation creates only missing Skills,
verifies copied files, and refuses to overwrite a non-identical target Skill.
Repeat `--skill NAME` to install a selected subset; omit it to install every
repo-local Skill. A selected name must exist and may be specified only once.
Upgrading is an explicit new-tag selection followed by another dry-run and
installation; Skills never update automatically. A release tag, successful
tests, or an installer report does not create source/license authority or any
KYM/TCO approval.

## Generation knowledge and source boundary

The repository keeps generation guidance separate from runtime contracts:

- `kb/data/kym_generation_knowledge.json` defines the source/authority -> actor
  Who/Why -> Product boundary/rules -> Project execution analysis sequence,
  placement rules, and candidate-review risks.
- `kb/data/tco_generation_knowledge.json` defines stable M/TSP modeling,
  persistent Target Profiles, conservative Coverage/evidence, and Gap review.
- `kb/data/evolution/bm_doc_adoption.json` records the file digests and
  adopt/adapt/reject/evolution decisions for the imported reference material.

The corresponding files under `kb/docs/` are deterministic reading views.
Raw material under `kb/BM-doc/` is reference/design lineage only: its old node
names, external product facts, historical counts, commands, and future designs
are not current repository truth. Its source/redistribution license remains a
human review requirement. See `kb/BM-doc/README.md` before using it.

The two Skills load one compact analysis guide on demand; they do not copy the
long reference documents into every prompt.

## Candidate-only generation audit

After normal validation, maintainers and agents can run a read-only semantic
risk audit before asking for human review:

```bash
python3 tools/audit_generation_quality.py kym \
  --workspace <workspace> \
  --product-id <product-id> \
  --candidate <kym.json> \
  [--base <base-kym.json>]

python3 tools/audit_generation_quality.py tco \
  --workspace <workspace> \
  --product-id <product-id> \
  --model <tco.json> \
  [--coverage <coverage.json>] \
  [--base-model <base-tco.json>]
```

The utility writes stable JSON to stdout and never writes workspace artifacts
or receipts. Exit `1` means audit errors block candidate review readiness;
warning-only output exits `0` but still requires human judgment; contract or
I/O failure exits `2`. The report has no approval verdict. Schema validation,
an empty finding set, Golden similarity, or a passing verification command does
not approve a KYM baseline, Target, TCO model, or Coverage proposal.
Installed Skills carry a bundled wrapper for the same API, so they do not depend
on the target project containing this repository's `tools/` directory.

## Authority chain

```text
source snapshot
-> KYM draft
-> human- or bound-authorized-system KYM baseline receipt
-> human- or bound-authorized-system Target receipt
-> TCO model draft
-> Coverage proposal
-> allowlisted verification
-> human- or bound-authorized-system Coverage receipt
-> TCO Coverage report
```

A Markdown view, passing command, embedded status word, or model file cannot create approval.

## KYM commands

```bash
kym init --product-id <id> [--coverage-mode repo|not_evaluated]
kym ingest --product-id <id> --source <conversation-or-document>
kym inspect-legacy --input <legacy-kym.md>
kym validate --product-id <id> --input <draft.json>
kym store-initial --product-id <id> --candidate <draft.json>
kym render --product-id <id> --input <revision.json>
kym diff --product-id <id> --base <revision.json> --candidate <candidate.json>
kym merge --product-id <id> --base <revision.json> --candidate <candidate.json>
kym approve baseline --product-id <id> --revision <revision.json> --approval-ref <ref> [--approval-source human|authorized_system --approved-by <principal> --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
kym approve target --product-id <id> --revision <revision.json> --target-id <id> --approval-ref <ref> [--approval-source human|authorized_system --approved-by <principal> --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
```

Every approved statement has stable identity, type, temporal/support status, authority, and source/turn/line provenance. Baseline and Target receipts bind both the reviewed artifact and a source-closure digest covering manifest identity/content, snapshot digest, chunk IDs/refs/digests, and turn metadata. Old revisions are never overwritten. Conflicts and unresolved statements may remain in an approved baseline but cannot become approved Target requirements.

## TCO commands

```bash
tco init --product-id <id> [--coverage-mode repo|not_evaluated]
tco validate-model --product-id <id> --model <model.json>
tco validate-coverage --product-id <id> --model <model.json> --coverage <proposal.json>
tco verify --product-id <id> [--node <node-id>] --output <results.json>
tco approve-coverage --product-id <id> --model <model.json> --proposal <proposal.json> --approval-ref <ref> --approved-by <principal> [--approval-source human|authorized_system --authorization <workspace>/.kym-tco/<id>/automation/authorizations/<authorization>.json]
tco render --product-id <id> --model <model.json> --coverage <approved-coverage.json>
tco compare-profiles --product-id <id> --model <model.json> --coverage <approved-coverage.json>
tco diff --base <json> --candidate <json>
```

Reserved Coverage states are:

```text
unknown < not_started < design_only < schema_validator < test_bound < bounded_runtime < integrated_runtime
```

Projects may add states or change labels, but cannot change reserved IDs, ranks, or definitions. Passing verification only refreshes results; it never changes Coverage state. In `not_evaluated` mode, profiles report `N/A`, not `0/N`.

## Verification and automation boundary

Verification commands are registered in project config as `argv`, restricted cwd, timeout, node IDs, and allowed environment names. `argv[0]` must be an absolute executable path; the child receives only environment variables named by `allowed_env`, with no implicit host `PATH`. Coverage JSON cannot inject shell commands.

No scheduled automation is included in v1. Automation may rerun `tco verify`, `tco render`, and emit change reports. It must stop for semantic model changes, Target changes, Coverage proposals, authorization changes, or governance-checkout drift. It may create an approval receipt only when `--approval-source authorized_system` is paired with a workspace-local, pre-existing authorization that binds the system principal, Goal, product, scope, Targets, expiry, and approval-reference prefix; the receipt records that binding and is never labeled `human`.

## Tests

The product and Skill contract suite and the repository-governance suite use
separate discovery roots. Run both commands to execute the complete local set:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s tests/governance -v
```

The complete local Validation run is self-contained and never requires an
external checkout or `SEMX_ROOT`:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v && \
PYTHONPATH=src python3 -m unittest discover -s tests/governance -v
```

The suite covers source chunking, provenance, conflicts, immutable revisions and receipts, Target denominator stability, graph validation, hidden fields, vocabulary drift, allowlisted verification, and no-repo output.
