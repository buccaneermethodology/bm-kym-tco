from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import ContractError, load_json, write_json
from .tco_ops import (
    approve_coverage,
    build_report,
    compare_candidate_profiles,
    diff_json,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
    render_candidate_preview,
    render_report,
    run_verification,
    validate_tco_execution_result,
    validate_candidate_coverage_model,
    validate_candidate_tco_model,
    validate_coverage_model,
    validate_tco_model,
)
from .workspace import init_project
from .doctor import run_doctor
from .tco_package import build_tco_candidate_package
from .tco_multi_input import build_multi_input_candidate_package


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tco", description="TCO model, coverage, verification, and rendering CLI")
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
    validate_model = sub.add_parser("validate-model")
    validate_model.add_argument("--product-id", required=True)
    validate_model.add_argument("--model", type=Path, required=True)
    validate_candidate_model = sub.add_parser("validate-candidate-model")
    validate_candidate_model.add_argument("--product-id", required=True)
    validate_candidate_model.add_argument("--model", type=Path, required=True)
    validate_coverage = sub.add_parser("validate-coverage")
    validate_coverage.add_argument("--product-id", required=True)
    validate_coverage.add_argument("--model", type=Path, required=True)
    validate_coverage.add_argument("--coverage", type=Path, required=True)
    validate_candidate_coverage = sub.add_parser("validate-candidate-coverage")
    validate_candidate_coverage.add_argument("--product-id", required=True)
    validate_candidate_coverage.add_argument("--model", type=Path, required=True)
    validate_candidate_coverage.add_argument("--coverage", type=Path, required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("--base", type=Path, required=True)
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--output", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--product-id", required=True)
    verify.add_argument("--node", action="append", default=[])
    verify.add_argument("--output", type=Path)
    approve = sub.add_parser("approve-coverage")
    approve.add_argument("--product-id", required=True)
    approve.add_argument("--model", type=Path, required=True)
    approve.add_argument("--proposal", type=Path, required=True)
    approve.add_argument("--approval-ref", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approval-source", choices=["human", "authorized_system"], default="human")
    approve.add_argument("--authorization", type=Path)
    render = sub.add_parser("render")
    render.add_argument("--product-id", required=True)
    render.add_argument("--model", type=Path, required=True)
    render.add_argument("--coverage", type=Path, required=True)
    render.add_argument("--verification-results", type=Path)
    render.add_argument("--output-dir", type=Path)
    render_preview = sub.add_parser("render-preview")
    render_preview.add_argument("--product-id", required=True)
    render_preview.add_argument("--model", type=Path, required=True)
    render_preview.add_argument("--coverage", type=Path, required=True)
    render_preview.add_argument("--verification-results", type=Path)
    render_preview.add_argument("--execution-result", type=Path, action="append", default=[])
    render_preview.add_argument("--output-dir", type=Path)
    compare = sub.add_parser("compare-profiles")
    compare.add_argument("--product-id", required=True)
    compare.add_argument("--model", type=Path, required=True)
    compare.add_argument("--coverage", type=Path, required=True)
    compare_candidate = sub.add_parser("compare-candidate-profiles")
    compare_candidate.add_argument("--product-id", required=True)
    compare_candidate.add_argument("--model", type=Path, required=True)
    compare_candidate.add_argument("--coverage", type=Path, required=True)
    migrate_v1 = sub.add_parser("migrate-v1-to-v2")
    migrate_v1.add_argument("--model", type=Path, required=True)
    migrate_v1.add_argument("--coverage", type=Path)
    migrate_v1.add_argument("--output-dir", type=Path, required=True)
    migrate_v2 = sub.add_parser("migrate-v2-to-v3")
    migrate_v2.add_argument("--model", type=Path, required=True)
    migrate_v2.add_argument("--coverage", type=Path)
    migrate_v2.add_argument("--output-dir", type=Path, required=True)
    migrate_v3 = sub.add_parser("migrate-v3-to-v4")
    migrate_v3.add_argument("--model", type=Path, required=True)
    migrate_v3.add_argument("--coverage", type=Path)
    migrate_v3.add_argument("--output-dir", type=Path, required=True)
    validate_execution = sub.add_parser("validate-execution-result")
    validate_execution.add_argument("--product-id", required=True)
    validate_execution.add_argument("--model", type=Path, required=True)
    validate_execution.add_argument("--execution-result", type=Path, required=True)
    materialize_gaps = sub.add_parser("materialize-target-gaps")
    materialize_gaps.add_argument("--model", type=Path, required=True)
    materialize_gaps.add_argument("--gap-specs", type=Path, required=True)
    materialize_gaps.add_argument("--output", type=Path, required=True)
    materialize_gaps.add_argument("--traceability-output", type=Path)
    package = sub.add_parser("package-candidate")
    package.add_argument("--product-id", required=True)
    package.add_argument("--model", type=Path, required=True)
    package.add_argument("--coverage", type=Path, required=True)
    package.add_argument("--node", action="append", default=[])
    package.add_argument("--execution-result", type=Path, action="append", default=[])
    package.add_argument("--package-dir", type=Path)
    multi_input = sub.add_parser("orchestrate-candidate")
    multi_input.add_argument("--product-id", required=True)
    multi_input.add_argument("--input-manifest", type=Path, required=True)
    multi_input.add_argument("--package-dir", type=str)
    return root


def run(args: argparse.Namespace) -> dict[str, object]:
    workspace = args.workspace.resolve()
    if args.command == "doctor":
        return run_doctor(
            tool="tco",
            workspace=workspace,
            product_id=args.product_id,
            skill_dir=args.skill_dir,
            expected_cli_version=args.expected_cli_version,
            output=args.output,
        )
    if args.command in {"validate-model", "validate-candidate-model", "validate-coverage", "validate-candidate-coverage", "render", "render-preview", "compare-profiles", "compare-candidate-profiles", "approve-coverage"}:
        model_path = getattr(args, "model", None)
        if model_path is not None and load_json(model_path).get("schema") == "kym_tco.tco_model.v1":
            raise ContractError("TCO v1 已停止被普通命令接受；请使用 tco migrate-v1-to-v2")
    if args.command == "init":
        path = init_project(workspace, args.product_id, args.coverage_mode)
        return {"status": "initialized", "project_root": str(path)}
    if args.command == "validate-model":
        model = validate_tco_model(workspace, args.product_id, args.model)
        return {"status": "valid", "node_count": len(model["nodes"]), "profile_count": len(model["target_profiles"])}
    if args.command == "validate-candidate-model":
        model = validate_candidate_tco_model(workspace, args.product_id, args.model)
        return {
            "status": "valid",
            "report_mode": "candidate_not_approved",
            "node_count": len(model["nodes"]),
            "profile_count": len(model["target_profiles"]),
        }
    if args.command == "validate-coverage":
        model, coverage = validate_coverage_model(workspace, args.product_id, args.model, args.coverage)
        return {"status": "valid", "node_count": len(model["nodes"]), "record_count": len(coverage["records"])}
    if args.command == "validate-candidate-coverage":
        model, coverage = validate_candidate_coverage_model(workspace, args.product_id, args.model, args.coverage)
        return {
            "status": "valid",
            "report_mode": "candidate_not_approved",
            "node_count": len(model["nodes"]),
            "record_count": len(coverage["records"]),
        }
    if args.command == "diff":
        delta = diff_json(load_json(args.base), load_json(args.candidate))
        if args.output:
            write_json(args.output, delta)
        return {"status": "diffed", "delta": delta, "output": str(args.output) if args.output else None}
    if args.command == "verify":
        results = run_verification(workspace, args.product_id, args.node)
        if args.output:
            write_json(args.output, results)
        failed = [key for key, value in results.items() if value["status"] != "passed"]
        if failed:
            raise ContractError(f"verification commands 未通过: {failed}")
        return {"status": "verified", "results": results, "output": str(args.output) if args.output else None}
    if args.command == "approve-coverage":
        coverage, receipt = approve_coverage(
            workspace,
            args.product_id,
            args.model,
            args.proposal,
            args.approval_ref,
            args.approved_by,
            args.approval_source,
            args.authorization,
        )
        return {"status": "approved", "coverage": str(coverage), "receipt": str(receipt)}
    if args.command == "render":
        verification = load_json(args.verification_results) if args.verification_results else None
        paths = render_report(
            workspace, args.product_id, args.model, args.coverage, args.output_dir, verification
        )
        return {"status": "rendered", **{key: str(value) for key, value in paths.items()}}
    if args.command == "render-preview":
        verification = load_json(args.verification_results) if args.verification_results else None
        paths = render_candidate_preview(
            workspace, args.product_id, args.model, args.coverage, args.output_dir, verification, args.execution_result
        )
        return {
            "status": "rendered",
            "report_mode": "candidate_not_approved",
            **{key: str(value) for key, value in paths.items()},
        }
    if args.command == "compare-profiles":
        report = build_report(workspace, args.product_id, args.model, args.coverage)
        return {"status": "compared", "target_profiles": report["target_profiles"]}
    if args.command == "compare-candidate-profiles":
        profiles = compare_candidate_profiles(workspace, args.product_id, args.model, args.coverage)
        return {
            "status": "compared",
            "report_mode": "candidate_not_approved",
            "target_profiles": profiles,
        }
    if args.command == "migrate-v1-to-v2":
        from .tco_ops import migrate_v1_to_v2
        model, coverage = migrate_v1_to_v2(args.model, args.coverage)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        model_path = args.output_dir / "model-v2.json"; write_json(model_path, model)
        result = {"status": "candidate_not_approved", "model": str(model_path), "unresolved": True}
        if coverage is not None:
            coverage_path = args.output_dir / "coverage-v2.json"; write_json(coverage_path, coverage); result["coverage"] = str(coverage_path)
        return result
    if args.command == "migrate-v2-to-v3":
        model, coverage = migrate_v2_to_v3(args.model, args.coverage)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        model_path = args.output_dir / "model-v3.json"; write_json(model_path, model)
        result = {"status": "candidate_not_approved", "model": str(model_path), "created_execution_results": []}
        if coverage is not None:
            coverage_path = args.output_dir / "coverage-v3.json"; write_json(coverage_path, coverage); result["coverage"] = str(coverage_path)
        return result
    if args.command == "migrate-v3-to-v4":
        model, coverage = migrate_v3_to_v4(args.model, args.coverage)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        model_path = args.output_dir / "model-v4.json"; write_json(model_path, model)
        result = {"status": "candidate_not_approved", "model": str(model_path), "coverage_denominator": "coverage_requirements"}
        if coverage is not None:
            coverage_path = args.output_dir / "coverage-v3.json"; write_json(coverage_path, coverage); result["coverage"] = str(coverage_path)
        return result
    if args.command == "validate-execution-result":
        result = validate_tco_execution_result(workspace, args.product_id, args.model, args.execution_result)
        return {"status": "valid", "execution_result_id": result["execution_result_id"], "m1_id": result["m1_id"], "effect_status": result["effect_status"]}
    if args.command == "materialize-target-gaps":
        from .tco_ops import materialize_target_gap_m1s
        model = load_json(args.model)
        if model.get("schema") != "kym_tco.tco_model.v2" or model.get("candidate_status") != "candidate_not_approved":
            raise ContractError("materialize-target-gaps 只接受 candidate_not_approved TCO v2 model")
        specs_document = load_json(args.gap_specs)
        if set(specs_document) != {"gap_specs"} or not isinstance(specs_document["gap_specs"], list):
            raise ContractError("gap specs 必须是仅含 gap_specs array 的 JSON document")
        materialized, traceability = materialize_target_gap_m1s(model, specs_document["gap_specs"])
        write_json(args.output, materialized)
        if args.traceability_output:
            write_json(args.traceability_output, {"candidate_status": "candidate_not_approved", "target_to_m1_traceability": traceability})
        return {"status": "candidate_not_approved", "model": str(args.output), "target_to_m1_traceability": traceability, "traceability_output": str(args.traceability_output) if args.traceability_output else None}
    if args.command == "package-candidate":
        return build_tco_candidate_package(
            workspace, args.product_id, args.model, args.coverage,
            args.node, args.execution_result, args.package_dir,
        )
    if args.command == "orchestrate-candidate":
        return build_multi_input_candidate_package(
            workspace, args.product_id, args.input_manifest, args.package_dir
        )
    raise ContractError(f"未实现命令: {args.command}")


def main() -> None:
    args = parser().parse_args()
    try:
        result = run(args)
    except (ContractError, OSError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("state_axes", {}).get("package_execution_status") in {"blocked", "failed"}:
        raise SystemExit(1)
    if args.command == "doctor" and result["environment_status"] != "ready":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
