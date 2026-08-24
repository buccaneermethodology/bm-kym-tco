from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .common import ContractError, digest_bytes, digest_value, load_json, project_root
from .capability_claims import audit_runtime_capability_witnesses, resolve_witness_artifact
from .evidence_hub import build_integrity_report, json_bytes as evidence_json_bytes, render_failure_html
from .reader_language import audit_reader_language
from .semantic_risk import audit_tco_candidate
from .tco_ops import (
    compare_candidate_profiles,
    render_candidate_preview,
    run_verification,
    validate_candidate_coverage_model,
    validate_candidate_tco_model,
    validate_tco_execution_result,
)
from .validation import validate_document


CLAIM_CEILING = (
    "repo-local TCO v4 + Coverage v3 candidate package only; candidate_not_approved; "
    "no semantic correctness, approval, receipt, release, implementation completion, or production readiness"
)


class ProtectedReadError(ContractError):
    pass


class SnapshotMutationError(ContractError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _axes(execution: str, candidate: str, verification: str, audit: str) -> dict[str, str]:
    return {
        "environment_status": "ready",
        "package_execution_status": execution,
        "candidate_status": candidate,
        "verification_status": verification,
        "audit_status": audit,
        "approval_status": "not_requested",
    }


def _assert_read_allowed(path: Path, workspace: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ProtectedReadError(f"{label} 必须位于 workspace 内") from exc
    lowered = [part.lower() for part in relative.parts]
    protected = {"approval", "approvals", "receipt", "receipts", "authorization", "authorizations"}
    if (
        ".git" in lowered
        or bool(set(lowered) & protected)
        or (lowered and lowered[0] == "dashboard")
        or (len(lowered) > 1 and lowered[:2] == ["kb", "data"])
    ):
        raise ProtectedReadError(f"{label} 不得读取 approval/receipt/authorization 或治理真源 surface")
    return resolved


def _package_dir(workspace: Path, root: Path, package_id: str, requested: Path | None) -> Path:
    reports = (root / "reports").resolve()
    if requested is None:
        return reports / "tco-packages" / package_id
    if requested.is_absolute() or ".." in requested.parts:
        raise ContractError("package-dir 必须是 workspace 内 reports 相对路径")
    target = (workspace.resolve() / requested).resolve()
    if target == reports or reports not in target.parents:
        raise ContractError("package-dir 必须位于 .kym-tco/<product-id>/reports/**")
    return target


def _evidence_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return f"<external-input>/{path.name}"


def _looks_like_path(value: str) -> bool:
    return "://" not in value and ("/" in value or value.endswith((".json", ".md", ".py", ".txt", ".yaml", ".yml")))


def _discover_bound_paths(value: Any, workspace: Path, root: Path) -> set[Path]:
    found: set[Path] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and _looks_like_path(child) and (
                    key.endswith(("ref", "path")) or key in {"source_tco_model", "cwd"}
                ):
                    base = root if key == "source_tco_model" else workspace
                    candidate = _assert_read_allowed(base / child, workspace, f"bound ref {key}")
                    if candidate.is_file():
                        found.add(candidate)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and _looks_like_path(item):
            candidate = _assert_read_allowed(workspace / item, workspace, f"bound list ref {item}")
            if candidate.is_file():
                found.add(candidate)

    visit(value)
    return found


def _assert_verification_policy(config: dict[str, Any], workspace: Path) -> None:
    for command in config.get("verification_commands", []):
        _assert_read_allowed(workspace / command["cwd"], workspace, "verification cwd")
        for token in command.get("argv", [])[1:]:
            if isinstance(token, str) and _looks_like_path(token):
                _assert_read_allowed(workspace / token, workspace, "verification argv path")


def _snapshot(paths: set[Path], workspace: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(paths):
        rows.append({"path": path.relative_to(workspace).as_posix(), "digest": digest_bytes(path.read_bytes())})
    return {"schema": "kym_tco.tco_package_snapshot.v1", "files": rows, "snapshot_digest": digest_value(rows)}


def _assert_snapshot(snapshot: dict[str, Any], workspace: Path) -> None:
    for row in snapshot["files"]:
        path = workspace / row["path"]
        if not path.is_file() or digest_bytes(path.read_bytes()) != row["digest"]:
            raise SnapshotMutationError(f"snapshot source 发生漂移: {row['path']}")


def _seal_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(manifest)
    projection["self_integrity"]["digest"] = None
    manifest["self_integrity"]["digest"] = digest_value(projection)
    return manifest


def _manifest(package_id: str, product_id: str, result: str, finding: str | None, axes: dict[str, str], commands: list[dict[str, Any]], index: dict[str, Any] | None) -> dict[str, Any]:
    value = {
        "schema": "kym_tco.tco_package_manifest.v1",
        "package_id": package_id,
        "product_id": product_id,
        "state_axes": axes,
        "result": result,
        "finding_code": finding,
        "command_evidence": commands,
        "artifact_index": index,
        "self_integrity": {
            "path": "package-manifest.json",
            "digest": "sha256:" + "0" * 64,
            "digest_algorithm": "sha256_canonical_json_without_self_integrity_digest",
            "checksum_ref": "package-manifest.sha256",
        },
        "claim_ceiling": CLAIM_CEILING,
    }
    return _seal_manifest(value)


def _command(step: str, argv: list[str], exit_code: int, summary: str, evidence: str | None) -> dict[str, Any]:
    return {"step": step, "argv": argv, "cwd": "workspace", "exit_code": exit_code, "summary": summary, "evidence_path": evidence, "engine_identity": "in_process_shared_function", "shell": False}


def _finding_owner(finding: str) -> str:
    if "VERIFICATION" in finding:
        return "verification"
    if finding == "PACKAGE_COVERAGE_NOT_EVALUATED":
        return "coverage-evaluation"
    if "AUDIT" in finding:
        return "audit"
    if "PROTECTED" in finding or "POLICY" in finding:
        return "package-read-policy"
    if "MUTATION" in finding or "INTEGRITY" in finding:
        return "package-integrity"
    if "PUBLISH" in finding:
        return "package-publish"
    return "candidate-validation"


def _publish(stage: Path, target: Path, manifest: dict[str, Any]) -> None:
    validate_document("tco_package_manifest", manifest)
    raw = _json_bytes(manifest)
    (stage / "package-manifest.json").write_bytes(raw)
    (stage / "package-manifest.sha256").write_text(digest_bytes(raw) + "\n", encoding="utf-8")
    for path in stage.rglob("*"):
        if path.is_file():
            path.read_bytes()
    if target.exists():
        raise ContractError(f"package-dir 已存在，禁止覆盖: {target}")
    os.replace(stage, target)


def _failure(workspace: Path, target: Path, package_id: str, product_id: str, result: str, finding: str, axes: dict[str, str], argv: list[str], summary: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    commands = [_command("package", argv, 1, summary, "package-manifest.json")]
    manifest = _manifest(package_id, product_id, result, finding, axes, commands, None)
    stage = target.parent / f".{target.name}.staging"
    if stage.exists() or target.exists():
        return {**manifest, "package_dir": None}
    try:
        stage.mkdir(parents=True)
        for name, value in (extra or {}).items():
            (stage / name).write_bytes(_json_bytes(value))
        failure_artifacts = [
            {"path": name, "digest": digest_bytes((stage / name).read_bytes()), "producer": "failure-evidence", "candidate_boundary": "candidate_not_approved"}
            for name in sorted(extra or {})
        ]
        failure_index = {
            "schema": "kym_tco.tco_package_artifact_index.v1",
            "package_id": package_id,
            "package_result": result,
            "artifacts": failure_artifacts,
        }
        validate_document("tco_package_artifact_index", failure_index)
        failure_index_raw = _json_bytes(failure_index)
        (stage / "artifact-index.json").write_bytes(failure_index_raw)
        manifest = _manifest(
            package_id,
            product_id,
            result,
            finding,
            axes,
            commands,
            {"path": "artifact-index.json", "digest": digest_bytes(failure_index_raw)},
        )
        manifest_path = stage / "package-manifest.json"
        manifest_path.write_bytes(_json_bytes(manifest))
        preview_dir = stage / "preview"
        preview_dir.mkdir(parents=True)
        artifact_paths = [manifest_path, stage / "artifact-index.json"] + [stage / name for name in (extra or {})]
        integrity = build_integrity_report(
            html_path=preview_dir / "tco-preview.html",
            allowed_root=workspace,
            artifacts=[(f"failure-{index}", path) for index, path in enumerate(artifact_paths, start=1)],
            claim_ceiling=CLAIM_CEILING,
            state_axes=axes,
            finding_owner=_finding_owner(finding),
            path_root=stage,
        )
        validate_document("evidence_hub_integrity", integrity)
        (preview_dir / "evidence-hub-integrity.json").write_bytes(evidence_json_bytes(integrity))
        (preview_dir / "tco-preview.html").write_text(render_failure_html(manifest, integrity), encoding="utf-8")
        _publish(stage, target, manifest)
        return {**manifest, "package_dir": str(target)}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        return {**manifest, "package_dir": None}


def build_tco_candidate_package(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    node_ids: list[str] | None = None,
    execution_result_paths: list[Path] | None = None,
    package_dir: Path | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = project_root(workspace, product_id)
    argv = [
        "tco", "package-candidate", "--product-id", product_id,
        "--model", _evidence_path(model_path, workspace),
        "--coverage", _evidence_path(coverage_path, workspace),
    ]
    for node_id in node_ids or []:
        argv.extend(["--node", node_id])
    for path in execution_result_paths or []:
        argv.extend(["--execution-result", _evidence_path(path, workspace)])
    if package_dir is not None:
        argv.extend(["--package-dir", package_dir.as_posix()])
    seed = f"{model_path}|{coverage_path}".encode()
    fallback_id = "tco-package-" + hashlib.sha256(seed).hexdigest()[:12]
    try:
        model_path = _assert_read_allowed(model_path, workspace, "model")
        coverage_path = _assert_read_allowed(coverage_path, workspace, "coverage")
        execution_paths = [_assert_read_allowed(path, workspace, "execution result") for path in (execution_result_paths or [])]
    except ProtectedReadError as exc:
        target = _package_dir(workspace, root, fallback_id, package_dir)
        return _failure(workspace, target, fallback_id, product_id, "policy_blocked", "PACKAGE_PROTECTED_READ_FORBIDDEN", _axes("blocked", "not_generated", "not_run", "not_run"), argv, str(exc))

    package_id = "tco-package-" + hashlib.sha256(model_path.read_bytes() + coverage_path.read_bytes()).hexdigest()[:12]
    target = _package_dir(workspace, root, package_id, package_dir)
    try:
        config_path = _assert_read_allowed(root / "config.json", workspace, "config")
        model = load_json(model_path)
        coverage = load_json(coverage_path)
        config = load_json(config_path)
        _assert_verification_policy(config, workspace)
        paths = {model_path, coverage_path, config_path, *execution_paths}
        for document in (model, coverage, *[load_json(path) for path in execution_paths]):
            paths.update(_discover_bound_paths(document, workspace, root))
        source_revision = model.get("source_kym_revision")
        if isinstance(source_revision, str):
            source_kym = _assert_read_allowed(root / "kym/revisions" / f"kym-{source_revision}.json", workspace, "source KYM revision")
            if source_kym.is_file():
                paths.add(source_kym)
    except ProtectedReadError as exc:
        return _failure(workspace, target, package_id, product_id, "policy_blocked", "PACKAGE_PROTECTED_READ_FORBIDDEN", _axes("blocked", "not_generated", "not_run", "not_run"), argv, str(exc))
    try:
        if model.get("schema") != "kym_tco.tco_model.v4" or coverage.get("schema") != "kym_tco.tco_coverage_model.v3":
            raise ContractError("package-candidate 只接受 TCO model v4 + Coverage v3")
        validate_candidate_tco_model(workspace, product_id, model_path)
        validate_candidate_coverage_model(workspace, product_id, model_path, coverage_path)
        for path in execution_paths:
            validate_tco_execution_result(workspace, product_id, model_path, path)
    except (ContractError, OSError, ValueError) as exc:
        return _failure(workspace, target, package_id, product_id, "candidate_schema_invalid", "PACKAGE_CANDIDATE_SCHEMA_INVALID", _axes("failed", "invalid_candidate", "not_run", "not_run"), argv, str(exc))
    reader_language_audit = audit_reader_language(model)
    if reader_language_audit["verdict"] != "pass":
        return _failure(
            workspace,
            target,
            package_id,
            product_id,
            "candidate_audit_blocked",
            "PACKAGE_AUDIT_BLOCKER",
            _axes("blocked", "candidate_not_approved", "not_run", "blocker"),
            argv,
            f"reader-language audit 未通过: {reader_language_audit['error_count']} findings",
            {"reader-language-audit.json": reader_language_audit},
        )
    snapshot = _snapshot(paths, workspace)

    if coverage["coverage_mode"] == "not_evaluated":
        verification: dict[str, Any] = {}
        verification_status = "not_run"
        finding = "PACKAGE_COVERAGE_NOT_EVALUATED"
    else:
        verification = run_verification(workspace, product_id, node_ids or [])
        if not verification:
            return _failure(workspace, target, package_id, product_id, "verification_empty", "PACKAGE_VERIFICATION_EMPTY", _axes("failed", "candidate_not_approved", "failed", "not_run"), argv, "verification result 为空")
        failed = sorted(key for key, value in verification.items() if value.get("status") != "passed")
        if failed:
            return _failure(workspace, target, package_id, product_id, "verification_failed", "PACKAGE_VERIFICATION_FAILED", _axes("failed", "candidate_not_approved", "failed", "not_run"), argv, f"verification 未通过: {failed}", {"verification.json": verification})
        capability_claim_audit = audit_runtime_capability_witnesses(
            model, coverage, config, verification, workspace
        )
        if capability_claim_audit["verdict"] != "pass":
            return _failure(
                workspace,
                target,
                package_id,
                product_id,
                "verification_failed",
                "PACKAGE_VERIFICATION_FAILED",
                _axes("failed", "candidate_not_approved", "failed", "not_run"),
                argv,
                f"capability witness 对账失败: {capability_claim_audit['error_count']} findings",
                {
                    "verification.json": verification,
                    "capability-claim-audit.json": capability_claim_audit,
                },
            )
        verification_status = "passed"
        finding = None
    try:
        _assert_snapshot(snapshot, workspace)
    except SnapshotMutationError as exc:
        return _failure(workspace, target, package_id, product_id, "source_mutated", "PACKAGE_COVERAGE_MUTATION_FORBIDDEN", _axes("failed", "candidate_not_approved", verification_status, "not_run"), argv, str(exc))

    audit = audit_tco_candidate(workspace, product_id, model_path, coverage_path)
    blockers = [item for item in audit["findings"] if item.get("severity") == "error" or (item.get("confidence") == "high" and item.get("resolution_required") is True)]
    if blockers:
        return _failure(workspace, target, package_id, product_id, "candidate_audit_blocked", "PACKAGE_AUDIT_BLOCKER", _axes("blocked", "candidate_not_approved", verification_status, "blocker"), argv, f"{len(blockers)} blocking findings", {"audit.json": audit, "verification.json": verification})
    audit_status = "findings" if audit["findings"] else "passed"
    execution_status = "completed_with_findings" if audit_status == "findings" else "completed"
    if audit_status == "findings" and finding is None:
        finding = "PACKAGE_AUDIT_FINDINGS"

    stage = target.parent / f".{target.name}.staging"
    if stage.exists() or target.exists():
        raise ContractError(f"package-dir 或 staging 已存在，禁止覆盖: {target}")
    try:
        stage.mkdir(parents=True)
        comparison = compare_candidate_profiles(workspace, product_id, model_path, coverage_path)
        payloads = {
            "inputs/model.json": (model_path.read_bytes(), "validated-input"),
            "inputs/coverage.json": (coverage_path.read_bytes(), "validated-input"),
            "inputs/config.json": (config_path.read_bytes(), "validated-input"),
            "verification.json": (_json_bytes(verification), "verification"),
            "audit.json": (_json_bytes(audit), "semantic-audit"),
            "comparison.json": (_json_bytes(comparison), "profile-comparison"),
            "snapshot.json": (_json_bytes(snapshot), "source-snapshot"),
            "reader-language-audit.json": (_json_bytes(reader_language_audit), "reader-language-audit"),
        }
        if verification_status == "passed":
            payloads["capability-claim-audit.json"] = (
                _json_bytes(capability_claim_audit),
                "capability-claim-audit",
            )
            for index, witness in enumerate(capability_claim_audit["verified_artifacts"], start=1):
                artifact = resolve_witness_artifact(workspace, witness["artifact_ref"])
                if artifact is None:
                    raise ContractError(
                        f"verified runtime witness disappeared before package staging: {witness['artifact_ref']}"
                    )
                payloads[f"runtime-witnesses/{index:03d}-{artifact.name}"] = (
                    artifact.read_bytes(),
                    "runtime-capability-witness",
                )
        for index, path in enumerate(execution_paths, start=1):
            payloads[f"inputs/execution-result-{index:03d}.json"] = (path.read_bytes(), "validated-input")
        for rel, (raw, _) in payloads.items():
            destination = stage / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        artifacts = []
        for rel, (_, producer) in sorted(payloads.items()):
            artifacts.append({"path": rel, "digest": digest_bytes((stage / rel).read_bytes()), "producer": producer, "candidate_boundary": "candidate_not_approved"})
        index = {"schema": "kym_tco.tco_package_artifact_index.v1", "package_id": package_id, "package_result": "candidate_package_ready", "artifacts": artifacts}
        validate_document("tco_package_artifact_index", index)
        index_raw = _json_bytes(index)
        (stage / "artifact-index.json").write_bytes(index_raw)
        commands = [
            _command("validate", argv, 0, "TCO v4 与 Coverage v3 validation passed", "inputs/model.json"),
            _command("reader-language-audit", argv, 0, "v4 M1 reader-language audit passed; candidate-only authority retained", "reader-language-audit.json"),
            _command("verify", argv, 0, "verification passed" if verification_status == "passed" else "coverage explicitly not_evaluated", "verification.json"),
            _command("audit", argv, 0, "shared semantic audit passed" if audit_status == "passed" else "shared semantic audit returned non-blocking findings", "audit.json"),
            _command("render", argv, 0, "candidate preview rendered in run-owned staging", "preview/tco-preview.html"),
        ]
        if verification_status == "passed":
            commands.insert(
                3,
                _command(
                    "capability-claim-audit",
                    argv,
                    0,
                    "runtime operation/output/effect witnesses matched bounded Coverage claims",
                    "capability-claim-audit.json",
                ),
            )
        manifest = _manifest(package_id, product_id, "candidate_package_ready", finding, _axes(execution_status, "candidate_not_approved", verification_status, audit_status), commands, {"path": "artifact-index.json", "digest": digest_bytes(index_raw)})
        manifest_path = stage / "package-manifest.json"
        manifest_path.write_bytes(_json_bytes(manifest))
        evidence_paths = [stage / rel for rel in payloads] + [stage / "artifact-index.json", manifest_path]
        expected_digests = {
            f"source-{position}": digest_bytes(path.read_bytes())
            for position, path in enumerate(evidence_paths, start=1)
        }
        preview_dir = stage / "preview"
        preview_paths = render_candidate_preview(
            workspace,
            product_id,
            model_path,
            coverage_path,
            preview_dir,
            verification,
            execution_paths,
            _axes(execution_status, "candidate_not_approved", verification_status, audit_status),
            evidence_paths,
            expected_digests,
            "coverage-evaluation" if finding == "PACKAGE_COVERAGE_NOT_EVALUATED" else None,
            integrity_path_root=stage,
        )
        _assert_snapshot(snapshot, workspace)
        _publish(stage, target, manifest)
        return {**manifest, "package_dir": str(target)}
    except SnapshotMutationError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        return _failure(workspace, target, package_id, product_id, "source_mutated", "PACKAGE_COVERAGE_MUTATION_FORBIDDEN", _axes("failed", "candidate_not_approved", verification_status, "not_run"), argv, str(exc))
    except Exception as exc:
        shutil.rmtree(stage, ignore_errors=True)
        return _failure(
            workspace,
            target,
            package_id,
            product_id,
            "publish_failed",
            "PACKAGE_PUBLISH_FAILED",
            _axes("failed", "candidate_not_approved", verification_status, audit_status),
            argv,
            str(exc),
            {"publish-error.json": {"error": str(exc)}},
        )
