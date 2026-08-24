#!/usr/bin/env python3
"""Run the installed package's read-only KYM candidate audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[4] / "src"
if REPO_SRC.is_dir() and str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from kym_tco.common import ContractError
from kym_tco.semantic_risk import audit_kym_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--base", type=Path)
    args = parser.parse_args()
    try:
        report = audit_kym_candidate(
            args.workspace.resolve(),
            args.product_id,
            args.candidate.resolve(),
            args.base.resolve() if args.base is not None else None,
        )
    except (ContractError, OSError, ValueError) as exc:
        report = {
            "report_kind": "generation_quality_audit_input_error",
            "candidate_only": True,
            "error_count": 1,
            "warning_count": 0,
            "findings": [{
                "code": "AUDIT_INPUT_ERROR",
                "severity": "error",
                "artifact_kind": "input",
                "path": "/",
                "subject_ids": [],
                "message": str(exc),
                "evidence": {},
                "confidence": "high",
                "resolution_required": True,
                "current_node_ids": [],
                "suggested_node_ids": [],
            }],
            "authority_note": "输入错误报告不创建接受态。",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
