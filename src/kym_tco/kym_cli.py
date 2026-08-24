from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import ContractError, load_json, project_root, write_json
from .kym_ops import (
    approve_baseline,
    approve_target,
    build_delta,
    ingest_source,
    inspect_legacy_markdown,
    merge_revision,
    render_kym_file,
    store_initial_revision,
    validate_kym_file,
)
from .semantic_risk import audit_kym_candidate
from .workspace import init_project
from .doctor import run_doctor
from .kym_package import build_kym_candidate_package


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kym", description="KYM source, revision, render, diff, and approval CLI")
    root.add_argument("--workspace", type=Path, default=Path.cwd())
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--product-id", required=True)
    doctor.add_argument("--skill-dir", type=Path, required=True)
    doctor.add_argument("--expected-cli-version", required=True)
    doctor.add_argument("--output", type=Path)

    init = sub.add_parser("init")
    init.add_argument("--product-id", required=True)
    init.add_argument("--coverage-mode", choices=["repo", "not_evaluated"], default="repo")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--product-id", required=True)
    ingest.add_argument("--source", type=Path, required=True)
    ingest_repo = sub.add_parser("ingest-repo")
    ingest_repo.add_argument("--product-id", required=True)
    ingest_repo.add_argument("--path", type=Path, required=True)

    inspect_legacy = sub.add_parser("inspect-legacy")
    inspect_legacy.add_argument("--input", type=Path, required=True)
    inspect_legacy.add_argument("--output", type=Path)

    validate = sub.add_parser("validate")
    validate.add_argument("--product-id", required=True)
    validate.add_argument("--input", type=Path, required=True)

    lint = sub.add_parser("lint")
    lint.add_argument("--product-id", required=True)
    lint.add_argument("--input", type=Path, required=True)
    lint.add_argument("--base", type=Path)
    lint.add_argument("--strict", action="store_true")

    render = sub.add_parser("render")
    render.add_argument("--product-id", required=True)
    render.add_argument("--input", type=Path, required=True)
    render.add_argument("--output", type=Path)

    diff = sub.add_parser("diff")
    diff.add_argument("--product-id", required=True)
    diff.add_argument("--base", type=Path, required=True)
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--output", type=Path)

    initial = sub.add_parser("store-initial")
    initial.add_argument("--product-id", required=True)
    initial.add_argument("--candidate", type=Path, required=True)

    merge = sub.add_parser("merge")
    merge.add_argument("--product-id", required=True)
    merge.add_argument("--base", type=Path, required=True)
    merge.add_argument("--candidate", type=Path, required=True)

    package = sub.add_parser("package-candidate")
    package.add_argument("--product-id", required=True)
    package.add_argument("--candidate", type=Path, required=True)
    package.add_argument("--base", type=Path)
    package.add_argument("--package-dir", type=Path)

    approve = sub.add_parser("approve")
    approve_sub = approve.add_subparsers(dest="approval_scope", required=True)
    baseline = approve_sub.add_parser("baseline")
    baseline.add_argument("--product-id", required=True)
    baseline.add_argument("--revision", type=Path, required=True)
    baseline.add_argument("--approval-ref", required=True)
    baseline.add_argument("--approved-by")
    baseline.add_argument("--approval-source", choices=["human", "authorized_system"], default="human")
    baseline.add_argument("--authorization", type=Path)
    target = approve_sub.add_parser("target")
    target.add_argument("--product-id", required=True)
    target.add_argument("--revision", type=Path, required=True)
    target.add_argument("--target-id", required=True)
    target.add_argument("--approval-ref", required=True)
    target.add_argument("--approved-by")
    target.add_argument("--approval-source", choices=["human", "authorized_system"], default="human")
    target.add_argument("--authorization", type=Path)
    return root


def run(args: argparse.Namespace) -> dict[str, object]:
    workspace = args.workspace.resolve()
    if args.command == "doctor":
        return run_doctor(
            tool="kym",
            workspace=workspace,
            product_id=args.product_id,
            skill_dir=args.skill_dir,
            expected_cli_version=args.expected_cli_version,
            output=args.output,
        )
    if args.command == "init":
        path = init_project(workspace, args.product_id, args.coverage_mode)
        return {"status": "initialized", "project_root": str(path)}
    if args.command == "ingest":
        path = ingest_source(workspace, args.product_id, args.source)
        return {"status": "ingested", "manifest": str(path)}
    if args.command == "ingest-repo":
        from .kym_ops import ingest_repo
        path = ingest_repo(workspace, args.product_id, args.path)
        return {"status": "ingested", "manifest": str(path), "provenance": "externally_enriched"}
    if args.command == "inspect-legacy":
        report = inspect_legacy_markdown(args.input)
        if args.output:
            write_json(args.output, report)
        return {
            "status": "inspected",
            "finding_count": report["finding_count"],
            "findings": report["findings"],
            "output": str(args.output) if args.output else None,
        }
    if args.command == "validate":
        document = validate_kym_file(workspace, args.product_id, args.input)
        return {"status": "valid", "revision": document["revision"]}
    if args.command == "lint":
        report = audit_kym_candidate(
            workspace,
            args.product_id,
            args.input.resolve(),
            args.base.resolve() if args.base is not None else None,
        )
        report["status"] = "linted"
        report["strict"] = bool(args.strict)
        return report
    if args.command == "render":
        path = render_kym_file(workspace, args.product_id, args.input, args.output)
        return {"status": "rendered", "output": str(path)}
    if args.command == "diff":
        # 允许把模板合同固定前的 immutable revision 作为迁移 base；
        # candidate 始终必须符合当前模板。
        base = validate_kym_file(workspace, args.product_id, args.base, enforce_template=False)
        candidate = validate_kym_file(workspace, args.product_id, args.candidate)
        delta = build_delta(base, candidate)
        if args.output:
            write_json(args.output, delta)
        return {"status": "diffed", "delta": delta, "output": str(args.output) if args.output else None}
    if args.command == "store-initial":
        path = store_initial_revision(workspace, args.product_id, args.candidate)
        return {"status": "stored", "revision": str(path)}
    if args.command == "merge":
        revision, delta = merge_revision(workspace, args.product_id, args.base, args.candidate)
        return {"status": "merged", "revision": str(revision), "delta": str(delta)}
    if args.command == "package-candidate":
        return build_kym_candidate_package(workspace, args.product_id, args.candidate, args.base, args.package_dir)
    if args.command == "approve" and args.approval_scope == "baseline":
        path = approve_baseline(
            workspace, args.product_id, args.revision, args.approval_ref, args.approved_by,
            args.approval_source, args.authorization,
        )
        return {"status": "approved", "scope": "baseline", "receipt": str(path)}
    if args.command == "approve" and args.approval_scope == "target":
        path = approve_target(
            workspace, args.product_id, args.revision, args.target_id, args.approval_ref, args.approved_by,
            args.approval_source, args.authorization,
        )
        return {"status": "approved", "scope": "target", "receipt": str(path)}
    raise ContractError(f"未实现命令: {args.command}")


def main() -> None:
    args = parser().parse_args()
    try:
        result = run(args)
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "doctor" and result["environment_status"] != "ready":
        raise SystemExit(1)
    if args.command == "lint":
        strict_blockers = [
            item
            for item in result["findings"]
            if item.get("severity") == "error"
            or (args.strict and item.get("confidence") == "high" and item.get("resolution_required") is True)
        ]
        if strict_blockers:
            raise SystemExit(1)
    if args.command == "package-candidate" and result["state_axes"]["package_execution_status"] in {"blocked", "failed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
