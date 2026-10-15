"""Read-only Git freshness evidence."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .common import ContractError, digest_value


def audit_git_freshness(workspace: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise ContractError(completed.stderr.strip() or f"git {' '.join(args)} exit={completed.returncode}")
        return completed.stdout.strip()
    try:
        branch = run("branch", "--show-current")
        head = run("rev-parse", "HEAD")
        status = run("status", "--porcelain=v1")
        tag = run("describe", "--tags", "--exact-match", "HEAD") if subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=workspace, capture_output=True, text=True, check=False).returncode == 0 else None
        return {
            "schema": "kym_tco.git_freshness_observation.v1", "branch": branch, "head": head,
            "working_tree": "dirty" if status else "clean", "scoped_dirty_digest": digest_value(status),
            "tag": tag, "comparison_to_main": "unknown", "origin_status": "unknown",
            "claim_ceiling": "git_freshness_observation_only_no_release_authority",
        }
    except (OSError, ContractError) as exc:
        return {"schema": "kym_tco.git_freshness_observation.v1", "status": "unknown", "error": str(exc), "claim_ceiling": "git_freshness_observation_only_no_release_authority"}
