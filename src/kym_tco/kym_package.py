from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .common import ContractError, digest_bytes, digest_value, load_json, next_revision, project_root, utc_now
from .kym_ops import build_delta, reader_manifest_digest, render_kym, semantic_validate_kym, source_closure_digest
from .semantic_risk import audit_kym_candidate
from .validation import validate_document


CLAIM_CEILING = "repo-local KYM candidate package only; candidate_not_approved; no semantic correctness, approval, receipt, release, or production readiness"
CASE_FINGERPRINTS = {"PACKAGE_CANDIDATE_SCHEMA_INVALID": "e852134b904f2489f2e71758082acfd8c1762b3d650cbf3d3f79412b89d1cffb", "PACKAGE_AUDIT_BLOCKER": "735ffebc97c2a684ea176131094ae528549d88d170463f7da872b3ccc74aa6cf"}


class ProtectedReadError(ContractError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _axes(execution: str, candidate: str, audit: str) -> dict[str, str]:
    return {"environment_status": "ready", "package_execution_status": execution, "candidate_status": candidate, "verification_status": "not_run", "audit_status": audit, "approval_status": "not_requested"}


def _package_dir(workspace: Path, root: Path, package_id: str, requested: Path | None) -> Path:
    reports = (root / "reports").resolve()
    target = reports / "kym-packages" / package_id if requested is None else (workspace.resolve() / requested).resolve()
    if requested is not None and (requested.is_absolute() or ".." in requested.parts):
        raise ContractError("package-dir 必须是 workspace 内 reports 相对路径")
    if target == reports or reports not in target.parents:
        raise ContractError("package-dir 必须位于 .kym-tco/<product-id>/reports/**")
    return target


def _command(step: str, argv: list[str], exit_code: int, summary: str, evidence_path: str | None) -> dict[str, Any]:
    return {"step": step, "argv": argv, "cwd": "workspace", "exit_code": exit_code, "stdout_summary": summary, "stderr_summary": "", "evidence_path": evidence_path, "engine_identity": "in_process_shared_function", "shell": False}


def _evidence_input_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return f"<external-input>/{path.name}"


def _assert_read_allowed(path: Path, workspace: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(workspace)
    except ValueError:
        raise ProtectedReadError(f"{label} 必须位于 workspace 内")
    lowered = [part.lower() for part in relative.parts]
    protected_tokens = {"approval", "approvals", "receipt", "receipts", "authorization", "authorizations"}
    protected = (
        ".git" in lowered or bool(protected_tokens & set(lowered))
        or (lowered and lowered[0] == "dashboard")
        or (len(lowered) >= 2 and lowered[0] == "kb" and lowered[1] == "data")
    )
    if protected:
        raise ProtectedReadError(f"{label} 不得读取 approval/receipt/authorization 或治理真源 surface")
    return resolved


def _seal_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(manifest)
    projection["self_integrity"]["digest"] = None
    manifest["self_integrity"]["digest"] = digest_value(projection)
    return manifest


def _assert_source_reads_allowed(candidate: dict[str, Any], root: Path, workspace: Path) -> None:
    refs = candidate.get("source_manifest_refs")
    if not isinstance(refs, list):
        return
    for ref in refs:
        if not isinstance(ref, str):
            continue
        manifest_path = _assert_read_allowed(root / ref, workspace, "source manifest")
        manifest = load_json(manifest_path)
        snapshot_ref = manifest.get("snapshot_ref")
        if isinstance(snapshot_ref, str):
            _assert_read_allowed(root / snapshot_ref, workspace, "source snapshot")
        chunks = manifest.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                if isinstance(chunk, dict) and isinstance(chunk.get("ref"), str):
                    _assert_read_allowed(root / chunk["ref"], workspace, "source chunk")


def _failure_manifest(package_id: str, product_id: str, result: str, code: str, axes: dict[str, str], command: dict[str, Any]) -> dict[str, Any]:
    manifest = {"schema": "kym_tco.kym_package_manifest.v1", "package_id": package_id, "product_id": product_id, "state_axes": axes, "result": result, "finding_code": code, "case_fingerprint": CASE_FINGERPRINTS.get(code), "command_evidence": [command], "artifact_index": None, "self_integrity": {"path": "package-manifest.json", "path_scope": "package_dir", "digest": "sha256:" + "0" * 64, "digest_algorithm": "sha256_canonical_json_without_self_integrity_digest", "checksum_ref": "package-manifest.sha256", "producer": "package-manifest", "candidate_boundary": "candidate_not_approved"}, "claim_ceiling": CLAIM_CEILING}
    return _seal_manifest(manifest)


def _publish_failure(target: Path, manifest: dict[str, Any], extra_files: dict[str, bytes] | None = None) -> None:
    if target.exists():
        raise ContractError(f"package-dir 已存在，禁止覆盖: {target}")
    stage = target.parent / f".{target.name}.staging"
    if stage.exists():
        raise ContractError(f"package staging 已存在: {stage}")
    try:
        stage.mkdir(parents=True)
        for name, raw_file in (extra_files or {}).items():
            (stage / name).write_bytes(raw_file)
        raw = _json_bytes(manifest)
        (stage / "package-manifest.json").write_bytes(raw)
        (stage / "package-manifest.sha256").write_text(digest_bytes(raw) + "\n", encoding="utf-8")
        validate_document("kym_package_manifest", json.loads(raw))
        os.replace(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _policy_blocked_result(
    target: Path,
    package_id: str,
    product_id: str,
    argv: list[str],
    exc: ProtectedReadError,
) -> dict[str, Any]:
    manifest = _failure_manifest(
        package_id,
        product_id,
        "policy_blocked",
        "PACKAGE_PROTECTED_READ_FORBIDDEN",
        _axes("blocked", "not_generated", "not_run"),
        _command("read-policy", argv, 1, str(exc), "package-manifest.json"),
    )
    try:
        _publish_failure(target, manifest)
        return {**manifest, "package_dir": str(target)}
    except (ContractError, OSError):
        return {**manifest, "package_dir": None}


def build_kym_candidate_package(workspace: Path, product_id: str, candidate_path: Path, base_path: Path | None = None, package_dir: Path | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = project_root(workspace, product_id)
    argv = ["kym", "package-candidate", "--product-id", product_id, "--candidate", _evidence_input_path(candidate_path, workspace)]
    if base_path is not None:
        argv.extend(["--base", _evidence_input_path(base_path, workspace)])
    if package_dir is not None:
        argv.extend(["--package-dir", package_dir.as_posix()])
    fallback_id = "kym-package-" + hashlib.sha256(str(candidate_path).encode("utf-8")).hexdigest()[:12]
    try:
        candidate_resolved = _assert_read_allowed(candidate_path, workspace, "candidate")
    except ProtectedReadError as exc:
        target = _package_dir(workspace, root, fallback_id, package_dir)
        return _policy_blocked_result(target, fallback_id, product_id, argv, exc)
    raw_candidate = candidate_resolved.read_bytes()
    package_id = "kym-package-" + hashlib.sha256(raw_candidate).hexdigest()[:12]
    target = _package_dir(workspace, root, package_id, package_dir)
    try:
        base_resolved = _assert_read_allowed(base_path, workspace, "base") if base_path else None
    except ProtectedReadError as exc:
        return _policy_blocked_result(target, package_id, product_id, argv, exc)
    try:
        candidate = load_json(candidate_resolved)
    except (ContractError, OSError, ValueError) as exc:
        manifest = _failure_manifest(package_id, product_id, "candidate_schema_invalid", "PACKAGE_CANDIDATE_SCHEMA_INVALID", _axes("failed", "invalid_candidate", "not_run"), _command("validate", argv, 2, str(exc), "package-manifest.json"))
        try:
            _publish_failure(target, manifest)
            return {**manifest, "package_dir": str(target)}
        except (ContractError, OSError):
            return {**manifest, "package_dir": None}
    try:
        _assert_source_reads_allowed(candidate, root, workspace)
    except ProtectedReadError as exc:
        return _policy_blocked_result(target, package_id, product_id, argv, exc)
    try:
        semantic_validate_kym(candidate, root)
    except (ContractError, OSError, ValueError) as exc:
        manifest = _failure_manifest(package_id, product_id, "candidate_schema_invalid", "PACKAGE_CANDIDATE_SCHEMA_INVALID", _axes("failed", "invalid_candidate", "not_run"), _command("validate", argv, 2, str(exc), "package-manifest.json"))
        try:
            _publish_failure(target, manifest)
            return {**manifest, "package_dir": str(target)}
        except (ContractError, OSError):
            return {**manifest, "package_dir": None}

    if base_resolved is not None:
        try:
            base_for_policy = load_json(base_resolved)
            _assert_source_reads_allowed(base_for_policy, root, workspace)
        except ProtectedReadError as exc:
            return _policy_blocked_result(target, package_id, product_id, argv, exc)

    audit = audit_kym_candidate(workspace, product_id, candidate_resolved, base_resolved)
    blockers = [item for item in audit["findings"] if item.get("severity") == "error" or (item.get("confidence") == "high" and item.get("resolution_required") is True)]
    if blockers:
        manifest = _failure_manifest(package_id, product_id, "candidate_audit_blocked", "PACKAGE_AUDIT_BLOCKER", _axes("blocked", "candidate_not_approved", "blocker"), _command("lint-audit", argv, 1, f"{len(blockers)} blocking findings", "audit.json"))
        try:
            _publish_failure(target, manifest, {"audit.json": _json_bytes(audit)})
            return {**manifest, "package_dir": str(target)}
        except (ContractError, OSError):
            return {**manifest, "package_dir": None}

    base = None
    normalized = copy.deepcopy(candidate)
    revision = "r001"
    delta = None
    if base_path is not None:
        base = load_json(base_resolved)
        _assert_source_reads_allowed(base, root, workspace)
        semantic_validate_kym(base, root, enforce_template=False)
        if normalized.get("base_revision") != base["revision"]:
            raise ContractError("candidate.base_revision 与 base revision 不一致")
        revision = next_revision((root / "kym/revisions").glob("kym-r*.json"))
    elif list((root / "kym/revisions").glob("kym-r*.json")):
        raise ContractError("已有 revision；package-candidate 必须提供 --base")
    normalized["revision"] = revision
    normalized["base_revision"] = base["revision"] if base else None
    normalized["created_at"] = utc_now()
    semantic_validate_kym(normalized, root)
    if base is not None:
        delta = build_delta(base, normalized)
    reader = render_kym(normalized, root).encode("utf-8")
    closure = source_closure_digest(normalized, root)
    revision_rel = f"kym/revisions/kym-{revision}.json"
    reader_rel = f"kym/views/kym-{revision}.md"
    delta_rel = f"kym/deltas/{base['revision']}-to-{revision}.json" if base else None
    lineage = {"schema": "kym_tco.kym_package_lineage.v1", "lineage_kind": "revision" if base else "initial", "base_revision": base["revision"] if base else None, "base_digest": digest_value(base) if base else None, "candidate_digest": digest_value(normalized), "source_closure_digest": closure, "delta_status": "present" if delta else "not_applicable_initial_revision", "delta_ref": delta_rel, "delta_digest": digest_value(delta) if delta else None}
    validate_document("kym_package_lineage", lineage)
    source_inventory = {
        "schema": "kym_tco.kym_package_source_manifest.v1",
        "digest_kind": "manifest_content_excluding_ingested_at",
        "sources": [
            {"path": ref, "digest": reader_manifest_digest(load_json(root / ref))}
            for ref in normalized["source_manifest_refs"]
        ],
        "source_closure_digest": closure,
    }
    validation_report = {"status": "passed", "candidate_digest": digest_value(normalized), "revision": revision}
    payloads: dict[str, tuple[bytes, str]] = {
        revision_rel: (_json_bytes(normalized), "store-revision"), reader_rel: (reader, "render-reader"),
        "source-manifest.json": (_json_bytes(source_inventory), "source-closure"), "validation.json": (_json_bytes(validation_report), "validate"),
        "audit.json": (_json_bytes(audit), "lint-audit"), "lineage.json": (_json_bytes(lineage), "lineage"),
    }
    if delta is not None and delta_rel is not None:
        payloads[delta_rel] = (_json_bytes(delta), "build-delta")
    canonical_paths = {revision_rel, reader_rel, *([delta_rel] if delta_rel else [])}
    artifacts = [{"path": path, "path_scope": "product_root" if path in canonical_paths else "package_dir", "digest": digest_bytes(raw), "producer": producer, "candidate_boundary": "candidate_not_approved"} for path, (raw, producer) in sorted(payloads.items())]
    index = {"schema": "kym_tco.kym_package_artifact_index.v1", "package_id": package_id, "artifacts": artifacts}
    validate_document("kym_package_artifact_index", index)
    index_raw = _json_bytes(index)
    commands = [_command("validate", argv, 0, "schema and semantics passed", "validation.json"), _command("lint-audit", argv, 0, "shared audit engine passed", "audit.json"), _command("publish", argv, 0, "staged payload ready", "artifact-index.json")]
    manifest = {"schema": "kym_tco.kym_package_manifest.v1", "package_id": package_id, "product_id": product_id, "state_axes": _axes("completed", "candidate_not_approved", "passed"), "result": "candidate_package_ready", "finding_code": None, "case_fingerprint": None, "command_evidence": commands, "artifact_index": {"path": "artifact-index.json", "path_scope": "package_dir", "digest": digest_bytes(index_raw), "producer": "artifact-index", "candidate_boundary": "candidate_not_approved"}, "self_integrity": {"path": "package-manifest.json", "path_scope": "package_dir", "digest": "sha256:" + "0" * 64, "digest_algorithm": "sha256_canonical_json_without_self_integrity_digest", "checksum_ref": "package-manifest.sha256", "producer": "package-manifest", "candidate_boundary": "candidate_not_approved"}, "claim_ceiling": CLAIM_CEILING}
    _seal_manifest(manifest)
    validate_document("kym_package_manifest", manifest)
    manifest_raw = _json_bytes(manifest)
    if target.exists():
        raise ContractError(f"package-dir 已存在，禁止覆盖: {target}")
    stage = target.parent / f".{target.name}.staging"
    if stage.exists():
        raise ContractError(f"package staging 已存在: {stage}")
    stage.mkdir(parents=True)
    published: list[Path] = []
    try:
        for rel, (raw, _) in payloads.items():
            staged = stage / "payload" / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(raw)
            if digest_bytes(staged.read_bytes()) != digest_bytes(raw):
                raise ContractError(f"staging readback digest mismatch: {rel}")
        (stage / "artifact-index.json").write_bytes(index_raw)
        (stage / "package-manifest.json").write_bytes(manifest_raw)
        (stage / "package-manifest.sha256").write_text(digest_bytes(manifest_raw) + "\n", encoding="utf-8")
        for rel in canonical_paths:
            destination = root / rel
            if destination.exists():
                raise ContractError(f"canonical artifact 已存在，禁止覆盖: {destination}")
        for rel in canonical_paths:
            destination = root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / "payload" / rel, destination)
            published.append(destination)
        for rel in set(payloads) - canonical_paths:
            destination = stage / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / "payload" / rel, destination)
        shutil.rmtree(stage / "payload", ignore_errors=True)
        os.replace(stage, target)
    except Exception as exc:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        failure = _failure_manifest(package_id, product_id, "publish_failed", "PACKAGE_PUBLISH_FAILED", _axes("failed", "candidate_not_approved", "passed"), _command("publish", argv, 2, str(exc), None))
        return {**failure, "package_dir": None}
    return {**manifest, "package_dir": str(target)}
