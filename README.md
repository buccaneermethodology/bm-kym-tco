# bm-kym-tco

Deterministic CLI tooling and two portable Codex Skills for source-traceable KYM drafting and candidate TCO/Coverage work.

This first public release contains exactly:

- the `kym` and `tco` CLI entrypoints and their required runtime/schema closure;
- `.codex/skills/build-kym`;
- `.codex/skills/build-tco-coverage`;
- public documentation, license, CI and the selective Skill installer.

The CLI does not call an LLM. Skills guide semantic drafting; the CLI validates, packages, compares and renders candidate artifacts deterministically. Drafts, previews and passing checks are not semantic approval, promotion or production readiness.

## Install

```bash
python -m pip install .
kym --help
tco --help
```

Python 3.9+ is supported. The package name is `bm-kym-tco`, the import package remains `kym_tco`, and the console scripts are `kym` and `tco`.

## Skills

Copy the two `.codex/skills/` directories into a Codex workspace, or use `tools/install_codex_skills.py` for an explicit destination. No other repository Skills are included or implied.

Use `build-kym` to create source-traceable KYM drafts and `build-tco-coverage` to create candidate TCO models, Coverage proposals and previews. Keep human review and approval as separate steps.

## Scope and maintenance

`bm-kym-tco` is a one-way public projection of the internal `kym-tco` source. The internal repository remains the maintenance source of truth. Public changes are produced from an explicit allowlist, reviewed for license and leakage, and read back by digest; the public repository is not automatically merged back into the internal repository.

See [the Chinese installation guide](docs/INSTALLATION_AND_USAGE.zh-CN.md), [the KYM-to-TCO workflow](docs/KYM_TO_TCO_WORKFLOW.zh-CN.md), [the release checklist](docs/OPEN_SOURCE_CHECKLIST.zh-CN.md), [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
