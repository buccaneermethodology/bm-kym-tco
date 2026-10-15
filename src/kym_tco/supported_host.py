"""Supported-host binding, idempotent invocation, and Slice 4 pipeline handoff."""
from __future__ import annotations

import json
import errno
import base64
import importlib.metadata
import os
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import fcntl

from jsonschema import Draft202012Validator

from .common import (
    ContractError,
    digest_bytes,
    digest_value,
    ensure_relative_path,
    load_json,
    resolved_within,
    utc_now,
)
from .source_adapters import (
    load_source_input_manifest,
    validate_normalized_source_ledger,
    verify_normalized_manifests,
)


PROFILE_ID = "codex_repo_local_skill_workflow_v1"
EXTERNAL_PROFILE_ID = "installed_cli_external_workspace_v1"
EXTERNAL_SKILL_DISTRIBUTIONS = {
    "build-kym": {
        "distribution_version": "v0.4.3-candidate",
        "release_status": "unreleased",
        "skill_byte_digest": "sha256:fa70577202ab83d66282c0a8ee65e88bd5edde756002e9399e5c12db5d67970f",
        "skill_tree_digest": "sha256:a0fb02ee98a8beec501413a1f7d04af26df2f75ab6bbc83499a72500297b8f0b",
        "distribution_manifest_digest": "sha256:001600bba6576f25d0c8240261b5953d17ee779f34245fd2eb44f75e248911e9"
    },
    "build-tco-coverage": {
        "distribution_version": "v0.4.3-candidate",
        "release_status": "unreleased",
        "skill_byte_digest": "sha256:8bcdee6e8a7402c0421a4f0c74e47661ed72837c457e381f33299ee137894744",
        "skill_tree_digest": "sha256:bf6b2f95b7abd879db1bff0ef2339a6733b98b000ce07874e6af4cf3774309b0",
        "distribution_manifest_digest": "sha256:45f3bc26e5ca1d7db8d9877da600d49bd79a5cd0abb43c60820309c1642e5395"
    }
}

PRODUCTION_ENTRY = "kym_tco.supported_host:materialize_and_validate"
PROTOCOL = "skill_materialization_pipeline_v1"
EXPECTED_REGISTRY_DIGEST = "sha256:a34617d19dff67cd472cbaa32fa22b3683083c70d480402014fbd8e55c038475"
REQUIRED_CAPABILITIES = {
    "workspace_bytes_read", "candidate_materialize", "repo_local_cli_invoke",
    "digest_capture", "durable_evidence_write",
}
REQUIRED_SKILLS = {"build-kym", "build-tco-coverage"}
EXTERNAL_ADAPTER_ID = "installed_cli_external_workspace_v1"
RUN_LEDGER_REF = ".kym-tco/runtime/supported-host-invocations-v1.json"
CLAIM_CEILING = "supported_host_invocation_precondition_only_no_synthesis_or_cli_gate_pass"
_LEDGER_LOCK_TIMEOUT_SECONDS = 15.0
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _schema() -> dict[str, Any]:
    text = resources.files("kym_tco.schemas").joinpath("supported_host_registry_v1.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def registry_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("registry_digest", None)
    return digest_value(payload)


def distribution_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("manifest_digest", None)
    return digest_value(payload)


def _archive_manifest_digest(document: dict[str, Any]) -> str:
    return digest_value({
        "artifact_id": document["artifact_id"],
        "skill_ref": document["skill_ref"],
        "skill_byte_digest": document["skill_byte_digest"],
        "source_revision": document["source_revision"],
    })


def validate_skill_distribution(document: dict[str, Any], workspace: Path) -> dict[str, Any]:
    expected_fields = {
        "schema", "skill_id", "distribution_version", "release_status", "source_revision",
        "source_snapshot_status", "artifact_id", "skill_ref", "skill_byte_digest",
        "distribution_tree_digest", "archive_manifest_digest", "manifest_digest",
    }
    if set(document) != expected_fields:
        raise ContractError("Skill distribution 字段集合不匹配")
    if document["schema"] != "kym_tco.skill_distribution.v1":
        raise ContractError("Skill distribution schema 不匹配")
    if document["skill_id"] not in REQUIRED_SKILLS:
        raise ContractError("Skill distribution skill_id 未支持")
    if not isinstance(document["distribution_version"], str) or not document["distribution_version"].startswith("v0.4.") or document["release_status"] != "unreleased":
        raise ContractError("Skill distribution 必须是显式未发行候选版本")
    if document["source_snapshot_status"] != "dirty_unreleased_candidate":
        raise ContractError("Skill distribution 不得伪称 clean/released")
    if not document["source_revision"].startswith("git:") or not document["source_revision"].endswith("+worktree"):
        raise ContractError("Skill distribution source_revision 必须绑定 git HEAD + worktree candidate")
    expected_ref = f".codex/skills/{document['skill_id']}/SKILL.md"
    if document["skill_ref"] != expected_ref:
        raise ContractError("Skill distribution skill_ref 与 skill_id 不匹配")
    ensure_relative_path(document["skill_ref"], "Skill ref")
    skill_path = resolved_within(workspace, document["skill_ref"], "Skill ref")
    if not skill_path.is_file() or digest_bytes(skill_path.read_bytes()) != document["skill_byte_digest"]:
        raise ContractError("Skill distribution SKILL.md byte digest 不匹配")
    skill_dir = skill_path.parent
    tree_files = [
        {"ref": item.relative_to(skill_dir).as_posix(), "byte_digest": digest_bytes(item.read_bytes())}
        for item in sorted(skill_dir.rglob("*"))
        if item.is_file() and item.name != "distribution.json" and not item.is_symlink()
    ]
    if any(item.is_symlink() for item in skill_dir.rglob("*")) or document["distribution_tree_digest"] != digest_value(tree_files):
        raise ContractError("Skill distribution tree digest 不匹配")
    if document["archive_manifest_digest"] != _archive_manifest_digest(document):
        raise ContractError("Skill distribution archive manifest digest 不匹配")
    if document["manifest_digest"] != distribution_payload_digest(document):
        raise ContractError("Skill distribution manifest_digest 不匹配")
    if Path(document["artifact_id"]).is_absolute() or "/Users/" in json.dumps(document, ensure_ascii=False):
        raise ContractError("Skill distribution 含不可移植绝对路径")
    return document


def validate_supported_host_registry(document: dict[str, Any], source: Path | None = None) -> dict[str, Any]:
    errors = sorted(Draft202012Validator(_schema()).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors)
        raise ContractError(f"{source or 'supported-host registry'}: schema 校验失败: {detail}")
    if document["registry_digest"] != registry_payload_digest(document):
        raise ContractError("supported-host registry_digest 与 canonical payload 不一致")
    if document["registry_digest"] != EXPECTED_REGISTRY_DIGEST:
        raise ContractError("supported-host semantic registry digest 不匹配")
    profiles = document["profiles"]
    ids = [profile["host_profile_id"] for profile in profiles]
    if len(ids) != len(set(ids)):
        raise ContractError("supported-host profile id 必须唯一")
    if ids != [PROFILE_ID]:
        raise ContractError(f"supported-host registry 必须精确声明 profile: {PROFILE_ID}")
    profile = profiles[0]
    if profile["adapter_id"] != "codex_repo_local_materialization_v1" or profile["entry_ref"] != PRODUCTION_ENTRY:
        raise ContractError("supported-host production adapter identity 不匹配")
    if profile["protocol_version"] != PROTOCOL:
        raise ContractError("supported-host protocol_version 不匹配")
    if set(profile["required_capabilities"]) != REQUIRED_CAPABILITIES:
        raise ContractError("supported-host required capabilities 必须精确匹配 R2 contract")
    expected = profile["expected_skill_distributions"]
    resolved = profile["resolved_skill_distributions"]
    if {item["skill_id"] for item in expected} != REQUIRED_SKILLS or {item["skill_id"] for item in resolved} != REQUIRED_SKILLS:
        raise ContractError("supported-host 必须分别声明两个 Skill distribution")
    if profile["resolved_skill_distribution_digest"] != combined_skill_distribution_digest(resolved):
        raise ContractError("supported-host combined Skill distribution digest 不匹配")
    return document


def load_supported_host_registry(path: Path) -> dict[str, Any]:
    return validate_supported_host_registry(load_json(path), path)


def combined_skill_distribution_digest(records: list[dict[str, Any]]) -> str:
    return digest_value(sorted(records, key=lambda item: item["skill_id"]))


def resolve_skill_distributions(workspace: Path, profile: dict[str, Any]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for expected in sorted(profile["expected_skill_distributions"], key=lambda item: item["skill_id"]):
        artifact_ref = expected["distribution_artifact_ref"]
        artifact_path = resolved_within(workspace, artifact_ref, "Skill distribution artifact")
        document = validate_skill_distribution(load_json(artifact_path), workspace)
        if document["skill_id"] != expected["skill_id"] or document["skill_ref"] != expected["skill_ref"]:
            raise ContractError("Skill distribution expectation 与实际 manifest 不匹配")
        if document["distribution_version"] != expected["expected_distribution_version"]:
            raise ContractError("Skill distribution version 不匹配")
        resolved.append({
            "skill_id": document["skill_id"],
            "distribution_version": document["distribution_version"],
            "distribution_artifact_ref": artifact_ref,
            "distribution_artifact_digest": digest_bytes(artifact_path.read_bytes()),
            "skill_ref": document["skill_ref"],
            "skill_byte_digest": document["skill_byte_digest"],
        })
    if resolved != sorted(profile["resolved_skill_distributions"], key=lambda item: item["skill_id"]):
        raise ContractError("resolved Skill distribution bytes 与 registry 不匹配")
    return resolved


def require_enabled_profile(workspace: Path, registry_path: Path | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    path = registry_path or workspace / "governance/supported_host_registry_v1.json"
    registry = load_supported_host_registry(path)
    profile = registry["profiles"][0]
    if profile["profile_status"] != "enabled":
        raise ContractError("supported-host profile 未 enabled")
    resolved = resolve_skill_distributions(workspace, profile)
    if combined_skill_distribution_digest(resolved) != profile["resolved_skill_distribution_digest"]:
        raise ContractError("supported-host Skill identity 未闭合")
    return profile


def installed_runtime_root() -> Path:
    """Return the installation root that owns this imported runtime package."""
    return Path(__file__).resolve().parent


def _installed_runtime_distribution(root: Path) -> tuple[Any, list[Any]]:
    """Find the one supported distribution whose RECORD owns this imported package."""
    root = root.resolve()
    candidates: list[tuple[Any, list[Any]]] = []
    for name in ("kym-tco", "bm-kym-tco"):
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        files = list(distribution.files or [])
        owned_files = []
        for item in files:
            path = Path(distribution.locate_file(item)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if "__pycache__" not in path.parts and path.suffix != ".pyc":
                owned_files.append(item)
        if owned_files:
            candidates.append((distribution, files))
    if not candidates:
        raise ContractError("runtime_distribution_manifest_missing")
    if len(candidates) != 1:
        raise ContractError("runtime_distribution_manifest_ambiguous")
    return candidates[0]


def _installed_runtime_distribution_version(distribution: Any, runtime_version: str) -> str:
    """Require installed metadata and the imported runtime to report one version."""
    distribution_version = getattr(distribution, "version", None)
    if not isinstance(distribution_version, str) or distribution_version != runtime_version:
        raise ContractError("runtime_distribution_version_mismatch")
    return distribution_version


def _external_workspace_path(workspace: Path, ref: str, label: str) -> Path:
    """Resolve a workspace-relative path while rejecting every symlink component."""
    relative = ensure_relative_path(ref, label)
    cursor = workspace.resolve(strict=True)
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ContractError(f"workspace_path_escape: symlink component: {label}")
        resolved = cursor.resolve(strict=False)
        root = workspace.resolve(strict=True)
        if resolved != root and root not in resolved.parents:
            raise ContractError(f"{label} 逃逸 workspace: {ref}")
        cursor = resolved
    return cursor


def installed_distribution_identity() -> dict[str, Any]:
    """Verify the importable package against its external installed RECORD manifest."""
    root = installed_runtime_root()
    from . import __version__
    distribution, distribution_files = _installed_runtime_distribution(root)
    distribution_version = _installed_runtime_distribution_version(distribution, __version__)
    expected = []
    observed = []
    for item in distribution_files:
        path = Path(distribution.locate_file(item)).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        file_hash = item.hash
        if file_hash is None or file_hash.mode != "sha256":
            raise ContractError("runtime_distribution_manifest_invalid")
        expected_digest = "sha256:" + base64.urlsafe_b64decode(file_hash.value + "==").hex()
        if not path.is_file():
            raise ContractError("runtime_distribution_file_missing")
        actual_digest = digest_bytes(path.read_bytes())
        expected.append({"ref": relative, "byte_digest": expected_digest})
        observed.append({"ref": relative, "byte_digest": actual_digest})
    expected.sort(key=lambda row: row["ref"])
    observed.sort(key=lambda row: row["ref"])
    if not expected or [row["ref"] for row in expected] != [row["ref"] for row in observed]:
        raise ContractError("runtime_distribution_manifest_invalid")
    expected_distribution_digest = digest_value(expected)
    observed_distribution_digest = digest_value(observed)
    if expected_distribution_digest != observed_distribution_digest:
        raise ContractError("runtime_version_mismatch")
    return {
        "distribution": distribution.metadata.get("Name", ""),
        "version": distribution_version,
        "file_count": len(expected),
        "distribution_digest": expected_distribution_digest,
        "expected_distribution_digest": expected_distribution_digest,
        "observed_distribution_digest": observed_distribution_digest,
        "distribution_manifest_authority": "installed-wheel-RECORD",
        "entry_module": "kym_tco.tco_cli",
        "entry_module_digest": digest_bytes((root / "tco_cli.py").read_bytes()),
    }


def resolve_selected_skill(
    skill_id: str, skill_dir: Path, expected_digest: str | None = None, skill_source: str = "project"
) -> dict[str, str]:
    """Bind a caller-selected installed Skill to its actual bytes and directory."""
    if skill_id not in REQUIRED_SKILLS:
        raise ContractError("unsupported_skill_id")
    if skill_source not in {"global", "project"}:
        raise ContractError("skill_source_invalid")
    if skill_dir.is_symlink():
        raise ContractError("skill_path_symlink")
    directory = skill_dir.expanduser().resolve(strict=True)
    skill_file = directory / "SKILL.md"
    if not skill_file.is_file():
        raise ContractError("skill_unreadable")
    actual = digest_bytes(skill_file.read_bytes())
    registered = EXTERNAL_SKILL_DISTRIBUTIONS[skill_id]
    if expected_digest and expected_digest != registered["skill_byte_digest"]:
        raise ContractError("skill_expectation_unregistered")
    if actual != registered["skill_byte_digest"]:
        raise ContractError("skill_digest_mismatch")
    tree_files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ContractError("skill_path_symlink")
        if path.is_file():
            if path.name != "distribution.json":
                tree_files.append({"ref": path.relative_to(directory).as_posix(), "byte_digest": digest_bytes(path.read_bytes())})
    if digest_value(tree_files) != registered["skill_tree_digest"]:
        raise ContractError("skill_tree_digest_mismatch")
    distribution_path = directory / "distribution.json"
    if not distribution_path.is_file():
        raise ContractError("skill_distribution_manifest_missing")
    distribution = load_json(distribution_path)
    if (
        distribution.get("skill_id") != skill_id
        or distribution.get("distribution_version") != registered["distribution_version"]
        or distribution.get("release_status") != registered["release_status"]
        or distribution.get("skill_byte_digest") != actual
        or distribution.get("distribution_tree_digest") != digest_value(tree_files)
        or distribution.get("manifest_digest") != distribution_payload_digest(distribution)
        or digest_bytes(distribution_path.read_bytes()) != registered["distribution_manifest_digest"]
    ):
        raise ContractError("skill_distribution_manifest_mismatch")
    return {
        "skill_id": skill_id,
        "skill_source": skill_source,
        "skill_dir": str(directory),
        "skill_byte_digest": actual,
        "skill_tree_digest": registered["skill_tree_digest"],
        "distribution_version": registered["distribution_version"],
        "release_status": registered["release_status"],
        "distribution_manifest_digest": registered["distribution_manifest_digest"],
    }


def selection_record_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("record_digest", None)
    return digest_value(payload)


def _validate_operator_skill_precedence(skill_source: str, project_copy_exists: bool) -> None:
    if not isinstance(project_copy_exists, bool):
        raise ContractError("skill_selection_record_incomplete")
    expected_source = "project" if project_copy_exists else "global"
    if skill_source != expected_source:
        raise ContractError("skill_selection_precedence_mismatch")


def build_operator_selection_record(
    workspace: Path, skill_bindings: dict[str, Path], skill_sources: dict[str, str],
    operator_id: str, confirmed_at: str, selection_reasons: dict[str, str],
    evidence_refs: list[str], project_copy_exists: dict[str, bool],
) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    if not operator_id.strip() or not confirmed_at.strip():
        raise ContractError("skill_selection_operator_confirmation_missing")
    if set(skill_bindings) != REQUIRED_SKILLS or set(skill_sources) != REQUIRED_SKILLS:
        raise ContractError("skill_selection_incomplete")
    if set(selection_reasons) != REQUIRED_SKILLS or set(project_copy_exists) != REQUIRED_SKILLS:
        raise ContractError("skill_selection_record_incomplete")
    for skill_id in REQUIRED_SKILLS:
        _validate_operator_skill_precedence(skill_sources[skill_id], project_copy_exists[skill_id])
    evidence = []
    for ref in evidence_refs:
        path = _external_workspace_path(workspace, ref, "selection evidence")
        if not path.is_file():
            raise ContractError("skill_selection_evidence_missing")
        evidence.append({"ref": ref, "byte_digest": digest_bytes(path.read_bytes())})
    if not evidence:
        raise ContractError("skill_selection_evidence_missing")
    selections = []
    for skill_id in sorted(REQUIRED_SKILLS):
        identity = resolve_selected_skill(skill_id, skill_bindings[skill_id], skill_source=skill_sources[skill_id])
        selections.append({
            "skill_id": skill_id,
            "declared_source": skill_sources[skill_id],
            "canonical_skill_dir": identity["skill_dir"],
            "distribution_version": identity["distribution_version"],
            "release_status": identity["release_status"],
            "skill_byte_digest": identity["skill_byte_digest"],
            "skill_tree_digest": identity["skill_tree_digest"],
            "distribution_manifest_digest": identity["distribution_manifest_digest"],
            "project_copy_exists": bool(project_copy_exists[skill_id]),
            "selected_copy_reason": selection_reasons[skill_id],
        })
    record = {
        "schema_version": "kym_tco.skill_selection_record.v1",
        "workspace_realpath": str(workspace),
        "selection_id": digest_value({"workspace": str(workspace), "selections": selections, "confirmed_at": confirmed_at}),
        "selected_at": confirmed_at,
        "selection_assurance": "operator_confirmed",
        "operator_id": operator_id.strip(),
        "operator_assertion": "operator_confirmed records a human selection assertion; it is not host resolver attestation",
        "resolver_name": None,
        "resolver_version": None,
        "precedence_check": "operator_reviewed",
        "evidence_refs": sorted(evidence, key=lambda item: item["ref"]),
        "selections": selections,
    }
    record["record_digest"] = selection_record_payload_digest(record)
    return record


def validate_selection_record(
    workspace: Path, record_ref: str, skill_bindings: dict[str, Path], skill_sources: dict[str, str]
) -> tuple[dict[str, Any], str]:
    path = _external_workspace_path(workspace, record_ref, "skill selection record")
    raw = path.read_bytes()
    record = json.loads(raw.decode("utf-8"))
    expected = {
        "schema_version", "workspace_realpath", "selection_id", "selected_at", "selection_assurance",
        "operator_id", "operator_assertion", "resolver_name", "resolver_version", "precedence_check",
        "evidence_refs", "selections", "record_digest",
    }
    if set(record) != expected or record["schema_version"] != "kym_tco.skill_selection_record.v1":
        raise ContractError("skill_selection_record_invalid")
    if record["selection_assurance"] != "operator_confirmed":
        raise ContractError("skill_selection_assurance_unsupported")
    if record["operator_assertion"] != "operator_confirmed records a human selection assertion; it is not host resolver attestation":
        raise ContractError("skill_selection_authority_invalid")
    if record["workspace_realpath"] != str(workspace.resolve(strict=True)):
        raise ContractError("skill_selection_workspace_mismatch")
    if record["record_digest"] != selection_record_payload_digest(record):
        raise ContractError("skill_selection_record_digest_mismatch")
    if not record["operator_id"] or not record["selected_at"] or not record["evidence_refs"]:
        raise ContractError("skill_selection_record_incomplete")
    for evidence in record["evidence_refs"]:
        if set(evidence) != {"ref", "byte_digest"}:
            raise ContractError("skill_selection_evidence_invalid")
        evidence_path = _external_workspace_path(workspace, evidence["ref"], "selection evidence")
        if not evidence_path.is_file() or digest_bytes(evidence_path.read_bytes()) != evidence["byte_digest"]:
            raise ContractError("skill_selection_evidence_digest_mismatch")
    observed = []
    for skill_id in sorted(REQUIRED_SKILLS):
        identity = resolve_selected_skill(skill_id, skill_bindings[skill_id], skill_source=skill_sources[skill_id])
        observed.append({
            "skill_id": skill_id,
            "declared_source": skill_sources[skill_id],
            "canonical_skill_dir": identity["skill_dir"],
            "distribution_version": identity["distribution_version"],
            "release_status": identity["release_status"],
            "skill_byte_digest": identity["skill_byte_digest"],
            "skill_tree_digest": identity["skill_tree_digest"],
            "distribution_manifest_digest": identity["distribution_manifest_digest"],
        })
    selections = record["selections"]
    if not isinstance(selections, list) or len(selections) != len(REQUIRED_SKILLS):
        raise ContractError("skill_selection_record_incomplete")
    for expected_selection, observed_selection in zip(sorted(selections, key=lambda row: row.get("skill_id", "")), observed):
        for field, value in observed_selection.items():
            if expected_selection.get(field) != value:
                raise ContractError("skill_selection_record_identity_mismatch")
        if not expected_selection.get("selected_copy_reason") or not isinstance(expected_selection.get("project_copy_exists"), bool):
            raise ContractError("skill_selection_record_incomplete")
        _validate_operator_skill_precedence(
            expected_selection.get("declared_source"), expected_selection["project_copy_exists"]
        )
    if record["precedence_check"] != "operator_reviewed":
        raise ContractError("skill_selection_precedence_unconfirmed")
    return record, digest_bytes(raw)


def require_external_profile(profile_id: str, runtime_root: Path, target_workspace: Path) -> dict[str, Any]:
    """Resolve the built-in external profile without consulting target repo internals."""
    if profile_id != EXTERNAL_PROFILE_ID:
        raise ContractError("unknown_profile")
    root = runtime_root.expanduser().resolve(strict=True)
    workspace = target_workspace.expanduser().resolve(strict=True)
    package_root = Path(__file__).resolve().parent
    if root != package_root:
        raise ContractError("runtime_root_mismatch")
    if workspace == root or workspace in root.parents or root in workspace.parents:
        raise ContractError("workspace_runtime_overlap")
    _external_workspace_path(workspace, ".kym-tco", "runtime evidence root")
    profile_contract = {
        "host_profile_id": EXTERNAL_PROFILE_ID,
        "profile_status": "enabled",
        "pipeline_availability": "available_slice4_nonterminal",
        "profile_version": "1.0",
        "expected_skill_distributions": EXTERNAL_SKILL_DISTRIBUTIONS,
        "runtime_distribution_identity": installed_distribution_identity(),
    }
    return {
        **profile_contract,
        "profile_digest": external_profile_digest(profile_contract),
        "runtime_root": str(root),
        "target_workspace": str(workspace),
    }


def build_external_materialization_request(
    runtime_root: Path,
    target_workspace: Path,
    skill_bindings: dict[str, Path],
    skill_sources: dict[str, str],
    source_ledger_ref: str,
    output_refs: list[str],
    attempt_id: str = "initial",
    source_manifest_refs: list[str] | None = None,
    skill_selection_record_ref: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Freeze an external-workspace request against installed CLI and both registered Skills."""
    runtime_root = runtime_root.expanduser().resolve(strict=True)
    workspace = target_workspace.expanduser().resolve(strict=True)
    require_external_profile(EXTERNAL_PROFILE_ID, runtime_root, workspace)
    if set(skill_bindings) != REQUIRED_SKILLS or set(skill_sources) != REQUIRED_SKILLS:
        raise ContractError("skill_selection_incomplete")
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in attempt_id):
        raise ContractError("invalid_attempt_id")
    identities = [
        resolve_selected_skill(skill_id, skill_bindings[skill_id], skill_source=skill_sources[skill_id])
        for skill_id in sorted(REQUIRED_SKILLS)
    ]
    if not source_manifest_refs:
        raise ContractError("source_manifest_required")
    manifest_entries = []
    for manifest_ref in source_manifest_refs:
        manifest_path = _external_workspace_path(workspace, manifest_ref, "source input manifest")
        manifest_bytes = manifest_path.read_bytes()
        manifest_entries.append((manifest_ref, load_source_input_manifest(manifest_path), digest_bytes(manifest_bytes)))
    ledger_path = _external_workspace_path(workspace, source_ledger_ref, "normalized source ledger")
    ledger_document = load_json(ledger_path)
    try:
        verify_normalized_manifests(ledger_document, manifest_entries, workspace)
    except ContractError as exc:
        raise ContractError(f"source_digest_mismatch: {exc}") from exc
    if not skill_selection_record_ref:
        raise ContractError("skill_selection_record_required")
    selection_record, selection_record_digest = validate_selection_record(
        workspace, skill_selection_record_ref, skill_bindings, skill_sources,
    )
    profile = external_profile_with_skills(runtime_root, workspace, identities)
    profile["skill_selection_record"] = selection_record
    profile["profile_digest"] = external_profile_digest(profile)
    skill_digest = profile["resolved_skill_distribution_digest"]
    ledger_digest = digest_bytes(ledger_path.read_bytes())
    outputs = [
        {"ref": ref, "byte_digest": digest_bytes(_external_workspace_path(workspace, ref, "materialized output").read_bytes())}
        for ref in output_refs
    ]
    if not outputs:
        raise ContractError("materialized outputs 缺失")
    generation_id = digest_value({
        "host_profile_id": EXTERNAL_PROFILE_ID,
        "skill_distribution_digest": skill_digest,
        "runtime_distribution_digest": installed_distribution_identity()["distribution_digest"],
        "normalized_source_ledger_ref": source_ledger_ref,
        "normalized_source_ledger_byte_digest": ledger_digest,
        "source_manifest_bindings": ledger_document["source_manifest_bindings"],
        "skill_selection_record_ref": skill_selection_record_ref,
        "skill_selection_record_byte_digest": selection_record_digest,
        "skill_selection_assurance": selection_record["selection_assurance"],
    })
    event_id = materialization_event_identity(generation_id, outputs)
    request = {
        "host_profile_id": EXTERNAL_PROFILE_ID,
        "adapter_id": EXTERNAL_ADAPTER_ID,
        "entry_ref": PRODUCTION_ENTRY,
        "protocol_version": PROTOCOL,
        "available_capabilities": sorted(REQUIRED_CAPABILITIES),
        "skill_distribution_digest": skill_digest,
        "normalized_source_ledger_ref": source_ledger_ref,
        "normalized_source_ledger_byte_digest": ledger_digest,
        "source_manifest_bindings": ledger_document["source_manifest_bindings"],
        "skill_selection_record_ref": skill_selection_record_ref,
        "skill_selection_record_byte_digest": selection_record_digest,
        "skill_selection_assurance": selection_record["selection_assurance"],
        "generation_start_id": generation_id,
        "outputs": outputs,
        "materialization_event_id": event_id,
        "attempt_id": attempt_id,
        "pipeline_run_id": pipeline_run_identity(EXTERNAL_PROFILE_ID, skill_digest, generation_id, event_id, attempt_id),
    }
    return request, identities


def external_profile_with_skills(
    runtime_root: Path, target_workspace: Path, skill_identities: list[dict[str, str]]
) -> dict[str, Any]:
    profile = require_external_profile(EXTERNAL_PROFILE_ID, runtime_root, target_workspace)
    if {item.get("skill_id") for item in skill_identities} != REQUIRED_SKILLS or len(skill_identities) != len(REQUIRED_SKILLS):
        raise ContractError("skill_selection_incomplete")
    records = [
        {
            "skill_id": item["skill_id"],
            "skill_source": item["skill_source"],
            "distribution_version": item["distribution_version"],
            "release_status": item["release_status"],
            "skill_byte_digest": item["skill_byte_digest"],
            "skill_tree_digest": item["skill_tree_digest"],
            "distribution_manifest_digest": item["distribution_manifest_digest"],
        }
        for item in sorted(skill_identities, key=lambda row: row["skill_id"])
    ]
    profile.update({
        "adapter_id": EXTERNAL_ADAPTER_ID,
        "entry_ref": PRODUCTION_ENTRY,
        "protocol_version": PROTOCOL,
        "required_capabilities": sorted(REQUIRED_CAPABILITIES),
        "expected_skill_distributions": records,
        "resolved_skill_distributions": records,
        "resolved_skill_distribution_digest": digest_value(records),
        "selected_skill_identities": skill_identities,
    })
    profile["profile_digest"] = external_profile_digest(profile)
    return profile


def external_profile_digest(profile: dict[str, Any]) -> str:
    payload = deepcopy(profile)
    payload.pop("profile_digest", None)
    return digest_value(payload)


def generation_start_identity(profile_id: str, skill_digest: str, ledger_ref: str, ledger_byte_digest: str) -> str:
    return digest_value({
        "host_profile_id": profile_id,
        "skill_distribution_digest": skill_digest,
        "normalized_source_ledger_ref": ledger_ref,
        "normalized_source_ledger_byte_digest": ledger_byte_digest,
    })


def materialization_event_identity(generation_start_id: str, outputs: list[dict[str, str]]) -> str:
    return digest_value({"generation_start_id": generation_start_id, "outputs": sorted(outputs, key=lambda item: item["ref"])})


def pipeline_run_identity(profile_id: str, skill_digest: str, generation_start_id: str, event_id: str, attempt_id: str | None = None) -> str:
    identity = {
        "host_profile_id": profile_id, "skill_distribution_digest": skill_digest,
        "generation_start_id": generation_start_id, "materialization_event_id": event_id,
    }
    if attempt_id is not None:
        identity["attempt_id"] = attempt_id
    return digest_value(identity)


def build_materialization_request(
    workspace: Path,
    source_ledger_ref: str,
    output_refs: list[str],
) -> dict[str, Any]:
    workspace = workspace.resolve()
    profile = require_enabled_profile(workspace)
    skill_digest = profile["resolved_skill_distribution_digest"]
    ledger_path = resolved_within(workspace, source_ledger_ref, "normalized source ledger")
    ledger_byte_digest = digest_bytes(ledger_path.read_bytes())
    outputs = [
        {"ref": ref, "byte_digest": digest_bytes(resolved_within(workspace, ref, "materialized output").read_bytes())}
        for ref in output_refs
    ]
    generation_id = generation_start_identity(PROFILE_ID, skill_digest, source_ledger_ref, ledger_byte_digest)
    event_id = materialization_event_identity(generation_id, outputs)
    return {
        "host_profile_id": PROFILE_ID,
        "adapter_id": "codex_repo_local_materialization_v1",
        "entry_ref": PRODUCTION_ENTRY,
        "protocol_version": PROTOCOL,
        "available_capabilities": sorted(REQUIRED_CAPABILITIES),
        "skill_distribution_digest": skill_digest,
        "normalized_source_ledger_ref": source_ledger_ref,
        "normalized_source_ledger_byte_digest": ledger_byte_digest,
        "generation_start_id": generation_id,
        "outputs": outputs,
        "materialization_event_id": event_id,
        "pipeline_run_id": pipeline_run_identity(PROFILE_ID, skill_digest, generation_id, event_id),
    }


def _blocked(reason_code: str, detail: str, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "blocked", "reason_code": reason_code, "detail": detail,
        "host_profile_id": request.get("host_profile_id"),
        "generation_start_id": request.get("generation_start_id"),
        "materialization_event_id": request.get("materialization_event_id"),
        "pipeline_run_id": request.get("pipeline_run_id"),
        "precondition_status": "blocked", "cli_preflight_status": "not_run",
        "pipeline_status": "not_run", "claim_ceiling": CLAIM_CEILING,
    }


def _validate_request(workspace: Path, profile: dict[str, Any], request: dict[str, Any]) -> None:
    expected_fields = {
        "host_profile_id", "adapter_id", "entry_ref", "protocol_version", "available_capabilities",
        "skill_distribution_digest", "normalized_source_ledger_ref", "normalized_source_ledger_byte_digest",
        "generation_start_id", "outputs", "materialization_event_id", "pipeline_run_id",
    }
    if request.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        expected_fields.update({
            "attempt_id", "source_manifest_bindings", "skill_selection_record_ref",
            "skill_selection_record_byte_digest", "skill_selection_assurance",
        })
    if set(request) != expected_fields:
        raise ContractError("supported-host request 字段集合不匹配")
    profile_id = request["host_profile_id"]
    if profile_id not in {PROFILE_ID, EXTERNAL_PROFILE_ID} or profile.get("host_profile_id", profile_id) != profile_id:
        raise ContractError("unknown_profile")
    if request["adapter_id"] != profile["adapter_id"] or request["entry_ref"] != PRODUCTION_ENTRY:
        raise ContractError("adapter_mismatch")
    if request["protocol_version"] != PROTOCOL:
        raise ContractError("protocol_mismatch")
    if not REQUIRED_CAPABILITIES.issubset(set(request["available_capabilities"])):
        raise ContractError("capability_unavailable")
    if request["skill_distribution_digest"] != profile["resolved_skill_distribution_digest"]:
        raise ContractError("skill_digest_mismatch")
    ledger_ref = request["normalized_source_ledger_ref"]
    ensure_relative_path(ledger_ref, "normalized source ledger ref")
    ledger_path = (
        _external_workspace_path(workspace, ledger_ref, "normalized source ledger")
        if profile_id == EXTERNAL_PROFILE_ID else resolved_within(workspace, ledger_ref, "normalized source ledger")
    )
    ledger_bytes = ledger_path.read_bytes()
    if digest_bytes(ledger_bytes) != request["normalized_source_ledger_byte_digest"]:
        raise ContractError("normalized source ledger byte digest mismatch")
    ledger = validate_normalized_source_ledger(json.loads(ledger_bytes.decode("utf-8")))
    if profile_id == EXTERNAL_PROFILE_ID:
        if request["source_manifest_bindings"] != ledger["source_manifest_bindings"]:
            raise ContractError("source_digest_mismatch")
        if request["skill_selection_assurance"] != "operator_confirmed":
            raise ContractError("skill_selection_assurance_unsupported")
        selection_path = _external_workspace_path(workspace, request["skill_selection_record_ref"], "skill selection record")
        if digest_bytes(selection_path.read_bytes()) != request["skill_selection_record_byte_digest"]:
            raise ContractError("skill_selection_record_digest_mismatch")
        selection_record = load_json(selection_path)
        if selection_record.get("selection_assurance") != request["skill_selection_assurance"] or selection_record.get("record_digest") != selection_record_payload_digest(selection_record):
            raise ContractError("skill_selection_record_invalid")
        selected_by_id = {item.get("skill_id"): item for item in selection_record.get("selections", [])}
        for selected in profile.get("selected_skill_identities", []):
            recorded = selected_by_id.get(selected["skill_id"], {})
            if any(recorded.get(field) != value for field, value in {
                "canonical_skill_dir": selected["skill_dir"],
                "skill_byte_digest": selected["skill_byte_digest"],
                "skill_tree_digest": selected["skill_tree_digest"],
                "distribution_version": selected["distribution_version"],
                "release_status": selected["release_status"],
                "declared_source": selected["skill_source"],
                "distribution_manifest_digest": selected["distribution_manifest_digest"],
            }.items()):
                raise ContractError("skill_selection_record_identity_mismatch")
        entries = []
        for binding in ledger["source_manifest_bindings"]:
            manifest_path = _external_workspace_path(workspace, binding["ref"], "source input manifest")
            manifest_bytes = manifest_path.read_bytes()
            if digest_bytes(manifest_bytes) != binding["byte_digest"]:
                raise ContractError("source_digest_mismatch")
            entries.append((binding["ref"], load_source_input_manifest(manifest_path), digest_bytes(manifest_bytes)))
        verify_normalized_manifests(ledger, entries, workspace)
        expected_generation = digest_value({
            "host_profile_id": EXTERNAL_PROFILE_ID,
            "skill_distribution_digest": request["skill_distribution_digest"],
            "runtime_distribution_digest": installed_distribution_identity()["distribution_digest"],
            "normalized_source_ledger_ref": ledger_ref,
            "normalized_source_ledger_byte_digest": request["normalized_source_ledger_byte_digest"],
            "source_manifest_bindings": request["source_manifest_bindings"],
            "skill_selection_record_ref": request["skill_selection_record_ref"],
            "skill_selection_record_byte_digest": request["skill_selection_record_byte_digest"],
            "skill_selection_assurance": request["skill_selection_assurance"],
        })
    else:
        expected_generation = generation_start_identity(
            profile_id, request["skill_distribution_digest"], ledger_ref, request["normalized_source_ledger_byte_digest"]
        )
    if request["generation_start_id"] != expected_generation:
        raise ContractError("generation_start_id mismatch")
    if not request["outputs"]:
        raise ContractError("materialized outputs 缺失")
    refs: set[str] = set()
    for output in request["outputs"]:
        if set(output) != {"ref", "byte_digest"} or output["ref"] in refs:
            raise ContractError("materialized output identity 无效或重复")
        refs.add(output["ref"])
        output_path = (
            _external_workspace_path(workspace, output["ref"], "materialized output")
            if profile_id == EXTERNAL_PROFILE_ID else resolved_within(workspace, output["ref"], "materialized output")
        )
        if not output_path.is_file() or digest_bytes(output_path.read_bytes()) != output["byte_digest"]:
            raise ContractError("materialized output byte digest mismatch")
    expected_event = materialization_event_identity(request["generation_start_id"], request["outputs"])
    if request["materialization_event_id"] != expected_event:
        raise ContractError("materialization_event_id mismatch")
    attempt_id = request.get("attempt_id") if profile_id == EXTERNAL_PROFILE_ID else None
    if profile_id == EXTERNAL_PROFILE_ID and (not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in attempt_id)):
        raise ContractError("invalid_attempt_id")
    expected_run = pipeline_run_identity(profile_id, request["skill_distribution_digest"], request["generation_start_id"], expected_event, attempt_id)
    if request["pipeline_run_id"] != expected_run:
        raise ContractError("pipeline_run_id mismatch")


def validate_materialization_request(workspace: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Revalidate a frozen request against current registry and workspace bytes."""
    workspace = workspace.resolve()
    if request.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        raise ContractError("external_profile_requires_runtime_and_skill_bindings")
    profile = require_enabled_profile(workspace)
    _validate_request(workspace, profile, request)
    return profile


def _run_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": "kym_tco.supported_host_invocation_ledger.v1", "records": {}}
    try:
        document = load_json(path)
    except ContractError as exc:
        raise ContractError(f"run_ledger_corrupt: {exc}") from exc
    if set(document) != {"schema", "records"} or document["schema"] != "kym_tco.supported_host_invocation_ledger.v1" or not isinstance(document["records"], dict):
        raise ContractError("run_ledger_corrupt: supported-host invocation ledger 无效")
    for key, record in document["records"].items():
        if not isinstance(key, str) or set(record) != {
            "request_payload_digest", "pipeline_run_id", "effect_count", "state", "result"
        }:
            raise ContractError("run_ledger_corrupt: invocation record 字段无效")
        if record["state"] not in {"claimed", "completed"} or record["effect_count"] not in {0, 1}:
            raise ContractError("run_ledger_corrupt: invocation record state/effect_count 无效")
        if record["state"] == "claimed" and record["effect_count"] != 0:
            raise ContractError("run_ledger_corrupt: claimed record 不得声称 effect")
        if record["state"] == "completed" and record["effect_count"] != 1:
            raise ContractError("run_ledger_corrupt: completed record 必须记录一次 effect")
    return document


def _write_run_ledger_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _thread_lock(lock_path: Path) -> threading.Lock:
    key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _ledger_lock(lock_path: Path):
    local_lock = _thread_lock(lock_path)
    if not local_lock.acquire(timeout=_LEDGER_LOCK_TIMEOUT_SECONDS):
        raise ContractError("run_ledger_lock_failure: process-local lock timeout")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise ContractError("run_ledger_lock_failure: lock path 不得是 symlink")
        handle = lock_path.open("a+b")
        deadline = time.monotonic() + _LEDGER_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ContractError("run_ledger_lock_failure: cross-process lock timeout") from exc
                time.sleep(0.01)
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        local_lock.release()


def _external_reason_code(detail: str | BaseException) -> str:
    if isinstance(detail, OSError) and detail.errno in {errno.EACCES, errno.EPERM}:
        return "workspace_permission_denied"
    normalized = str(detail).casefold()
    reason_map = (
        (("unknown_profile", "unsupported_profile"),),
        (("adapter_mismatch", "capability_unavailable"),),
        (("protocol_mismatch", "capability_unavailable"),),
        (("capability_unavailable", "capability_unavailable"),),
        (("runtime_version_mismatch", "runtime_version_mismatch"), ("runtime_root_mismatch", "runtime_version_mismatch")),
        (("runtime_distribution_manifest", "runtime_version_mismatch"), ("runtime_distribution_file", "runtime_version_mismatch")),
        (("cli_missing", "cli_missing"), ("modulenotfounderror", "cli_missing")),
        (("normalized source ledger byte digest mismatch", "source_digest_mismatch"), ("source_digest_mismatch", "source_digest_mismatch"), ("原始 manifest bytes", "source_digest_mismatch"), ("当前 workspace sources", "source_digest_mismatch")),
        (("逃逸 workspace", "workspace_path_escape"), ("必须是 workspace 内相对路径", "workspace_path_escape")),
        (("permission denied", "workspace_permission_denied"), ("operation not permitted", "workspace_permission_denied")),
        (("skill_selection_ambiguous", "skill_selection_ambiguous"),),
        (("skill_selection_record", "skill_selection_invalid"), ("skill_selection_assurance", "skill_selection_invalid"), ("skill_selection_precedence", "skill_selection_invalid")),
        (("candidate_identity_mismatch", "candidate_digest_mismatch"),),
        (("evidence_terminal_not_passed", "evidence_not_passed"),),
        (("evidence_schema_invalid", "evidence_invalid"), ("evidence_digest_mismatch", "evidence_invalid")),
        (("candidate_or_evidence_missing", "evidence_unknown"),),
        (("skill_version_incompatible", "skill_version_incompatible"),),
        (("skill_digest_mismatch", "skill_digest_mismatch"), ("skill_tree_digest_mismatch", "skill_digest_mismatch"), ("skill_path_symlink", "skill_digest_mismatch"), ("skill_unreadable", "skill_digest_mismatch")),
        (("incomplete_prior_claim", "incomplete_prior_claim"),),
        (("retry_payload_conflict", "retry_payload_conflict"),),
        (("partial_output_quarantine_conflict", "deterministic_gate_failed"),),
        (("run_ledger_lock_failure", "capability_unavailable"), ("run_ledger_corrupt", "capability_unavailable")),
        (("workspace_runtime_overlap", "workspace_path_escape"),),
        (("event_identity_missing", "capability_unavailable"),),
    )
    for alternatives in reason_map:
        for needle, code in alternatives:
            if needle in normalized:
                return code
    return "capability_unavailable"


def _reason_code(detail: str) -> str:
    """Keep the internal supported-host v1 reason contract unchanged."""
    if "normalized source ledger byte digest mismatch" in detail:
        return "source_digest_mismatch"
    for code in (
        "unknown_profile", "adapter_mismatch", "protocol_mismatch", "capability_unavailable",
        "skill_digest_mismatch", "event_identity_missing", "run_ledger_lock_failure",
        "run_ledger_corrupt", "incomplete_prior_claim", "runtime_root_mismatch",
        "workspace_runtime_overlap", "skill_selection_incomplete", "skill_expectation_unregistered",
        "skill_unreadable", "skill_tree_digest_mismatch", "skill_path_symlink", "source_digest_mismatch",
    ):
        if code in detail:
            return code
    return "event_identity_missing"


def _record_external_failure(
    workspace: Path,
    request: dict[str, Any],
    reason_code: str,
    detail: str,
    *,
    terminal_status: str = "blocked",
    gate_ids: list[str] | None = None,
    generated_artifacts: list[dict[str, Any]] | None = None,
    partial_output_disposition: str = "not_started",
    retry_key: str | None = None,
) -> str | None:
    ref = ".kym-tco/runtime/host-failure-records/" + digest_value(request).split(":", 1)[-1] + ".json"
    try:
        path = _external_workspace_path(workspace, ref, "external failure evidence")
        path.parent.mkdir(parents=True, exist_ok=True)
        path = _external_workspace_path(workspace, ref, "external failure evidence")
        document = {
            "schema": "kym_tco.external_host_failure.v2",
            "host_profile_id": request.get("host_profile_id"),
            "request_payload_digest": digest_value(request),
            "reason_code": reason_code,
            "terminal_status": terminal_status,
            "detail_digest": digest_bytes(detail.encode("utf-8", errors="replace")),
            "input_artifacts_digest": digest_value(request.get("outputs", [])),
            "generated_artifacts_digest": digest_value(generated_artifacts or []),
            "gate_ids": sorted(set(gate_ids or [])),
            "retry_key": retry_key or digest_value({"request_payload_digest": digest_value(request), "pipeline_run_id": request.get("pipeline_run_id")}),
            "partial_output_disposition": partial_output_disposition,
            "recorded_at": utc_now(),
            "claim_ceiling": "deterministic_runtime_failure_evidence_only_no_semantic_approval",
        }
        schema = json.loads(resources.files("kym_tco.schemas").joinpath("external_host_failure_v2.schema.json").read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.path))
        if errors:
            raise ContractError(f"external failure evidence schema failed: {errors[0].message}")
        _write_run_ledger_atomic(path, document)
        return ref
    except (ContractError, OSError, TypeError, ValueError):
        return None


def run_external_pipeline(
    runtime_root: Path, workspace: Path, profile_id: str, skill_bindings: dict[str, Path],
    skill_sources: dict[str, str], source_ledger_ref: str, output_refs: list[str], attempt_id: str = "initial",
    source_manifest_refs: list[str] | None = None, skill_selection_record_ref: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    try:
        if profile_id != EXTERNAL_PROFILE_ID:
            raise ContractError("unsupported_profile")
        request, _identities = build_external_materialization_request(
            runtime_root, workspace, skill_bindings, skill_sources, source_ledger_ref, output_refs, attempt_id,
            source_manifest_refs, skill_selection_record_ref,
        )
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        payload = {
            "host_profile_id": profile_id,
            "external_input_digest": digest_value({
                "skill_ids": sorted(skill_bindings),
                "skill_sources": skill_sources,
                "source_ledger_ref": source_ledger_ref,
                "output_refs": output_refs,
                "source_manifest_refs": source_manifest_refs,
                "skill_selection_record_ref": skill_selection_record_ref,
            }),
        }
        reason = "unsupported_profile" if profile_id != EXTERNAL_PROFILE_ID else _external_reason_code(exc)
        result = _blocked(reason, str(exc), payload)
        result["failure_evidence_ref"] = _record_external_failure(workspace, payload, reason, str(exc))
        result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
        return result
    return materialize_and_validate(workspace, request, runtime_root, skill_bindings, skill_sources)


def external_consume_check(workspace: Path, candidate_ref: str, evidence_ref: str) -> dict[str, Any]:
    """Fail-closed consumer check for one candidate artifact and its deterministic evidence bundle."""
    workspace = workspace.expanduser().resolve(strict=True)
    try:
        candidate_path = _external_workspace_path(workspace, candidate_ref, "candidate package")
        evidence_path = _external_workspace_path(workspace, evidence_ref, "validation evidence bundle")
        if not candidate_path.is_file() or not evidence_path.is_file():
            raise ContractError("candidate_or_evidence_missing")
        candidate_digest = digest_bytes(candidate_path.read_bytes())
        bundle = load_json(evidence_path)
        schema = json.loads(resources.files("kym_tco.schemas").joinpath("post_generation_validation_bundle_v1.schema.json").read_text(encoding="utf-8"))
        schema_errors = sorted(Draft202012Validator(schema).iter_errors(bundle), key=lambda error: list(error.path))
        if schema_errors:
            raise ContractError("evidence_schema_invalid")
        from .post_generation_pipeline import bundle_payload_digest
        if bundle.get("bundle_digest") != bundle_payload_digest(bundle):
            raise ContractError("evidence_digest_mismatch")
        context = bundle.get("applicability_context", {})
        identity = bundle.get("pipeline_identity", {})
        host_identity = context.get("supported_host_identity", {})
        if (
            host_identity.get("profile_id") != EXTERNAL_PROFILE_ID
            or host_identity.get("profile_version") != "1.0"
            or host_identity.get("target_workspace_realpath") != str(workspace)
        ):
            raise ContractError("host_profile_identity_mismatch")
        from .post_generation_pipeline import validate_external_disposition_envelope
        validate_external_disposition_envelope(
            workspace,
            bundle.get("partial_output_disposition"),
            expected_pipeline_run_id=identity.get("pipeline_run_id"),
            expected_request_payload_digest=context.get("request_payload_digest"),
        )
        selection = context.get("skill_selection") or {}
        if selection.get("assurance") not in {"host_attested", "operator_confirmed"}:
            raise ContractError("skill_selection_assurance_unsupported")
        selection_ref = selection.get("record_ref")
        selection_path = _external_workspace_path(workspace, selection_ref, "skill selection record")
        selection_record = load_json(selection_path)
        selection_rows = selection_record.get("selections", [])
        if not isinstance(selection_rows, list) or {row.get("skill_id") for row in selection_rows if isinstance(row, dict)} != REQUIRED_SKILLS:
            raise ContractError("skill_selection_record_incomplete")
        skill_bindings = {row["skill_id"]: Path(row["canonical_skill_dir"]) for row in selection_rows}
        skill_sources = {row["skill_id"]: row["declared_source"] for row in selection_rows}
        selection_record, selection_byte_digest = validate_selection_record(
            workspace, selection_ref, skill_bindings, skill_sources,
        )
        if selection_byte_digest != selection.get("record_byte_digest"):
            raise ContractError("skill_selection_record_identity_mismatch")
        skill_identities = [
            resolve_selected_skill(skill_id, skill_bindings[skill_id], skill_source=skill_sources[skill_id])
            for skill_id in sorted(REQUIRED_SKILLS)
        ]
        recomputed_profile = external_profile_with_skills(installed_runtime_root(), workspace, skill_identities)
        recomputed_profile["skill_selection_record"] = selection_record
        recomputed_profile["profile_digest"] = external_profile_digest(recomputed_profile)
        if host_identity.get("profile_digest") != recomputed_profile["profile_digest"]:
            raise ContractError("host_profile_identity_mismatch")
        candidate_refs = context.get("candidate_refs", {})
        candidate_kind = _candidate_kind_for_evidence(candidate_refs, candidate_ref, candidate_digest)
        if candidate_kind is None:
            raise ContractError("candidate_identity_mismatch")
        validation_gate = {
            "target": "G-TARGET-VALIDATE",
            "kym": "G-KYM-VALIDATE",
            "tco_model": "G-TCO-MODEL-VALIDATE",
            "coverage": "G-TCO-COVERAGE-VALIDATE",
        }.get(candidate_kind)
        gate = next((row for row in bundle.get("gates", []) if row.get("gate_id") == validation_gate), None)
        if not gate or not gate.get("required") or not gate.get("applicable") or gate.get("status") != "passed":
            disposition = bundle.get("partial_output_disposition") or {}
            if disposition.get("status") == "cleanup_failed":
                return {
                    "status": "not_consumable_retained_partial", "consumable": False,
                    "reason_code": "partial_output_retained", "candidate_ref": candidate_ref,
                    "candidate_byte_digest": candidate_digest, "pipeline_run_id": identity.get("pipeline_run_id"),
                }
            raise ContractError("candidate_validation_not_passed")
        if bundle.get("aggregate_status") != "passed":
            disposition = bundle.get("partial_output_disposition") or {}
            if disposition.get("status") == "cleanup_failed":
                return {
                    "status": "not_consumable_retained_partial", "consumable": False,
                    "reason_code": "partial_output_retained", "candidate_ref": candidate_ref,
                    "candidate_byte_digest": candidate_digest, "pipeline_run_id": identity.get("pipeline_run_id"),
                }
            raise ContractError("evidence_terminal_not_passed")
        disposition = bundle.get("partial_output_disposition") or {}
        if disposition.get("status") == "cleanup_failed":
            return {
                "status": "not_consumable_retained_partial", "consumable": False,
                "reason_code": "partial_output_retained", "candidate_ref": candidate_ref,
                "candidate_byte_digest": candidate_digest, "pipeline_run_id": identity.get("pipeline_run_id"),
            }
        runtime = context.get("supported_host_identity", {}).get("runtime_distribution", {})
        current_runtime = installed_distribution_identity()
        if (
            runtime.get("version") != current_runtime["version"]
            or runtime.get("distribution") != current_runtime["distribution"]
            or runtime.get("distribution_digest") != current_runtime["distribution_digest"]
            or runtime.get("expected_distribution_digest") != current_runtime["expected_distribution_digest"]
            or runtime.get("observed_distribution_digest") != current_runtime["observed_distribution_digest"]
            or runtime.get("expected_distribution_digest") != runtime.get("observed_distribution_digest")
        ):
            raise ContractError("runtime_version_mismatch")
        for skill in bundle.get("skill_identities", []):
            pinned = EXTERNAL_SKILL_DISTRIBUTIONS.get(skill.get("skill_id"), {})
            if any(skill.get(field) != pinned.get(pin_field) for field, pin_field in (
                ("distribution_version", "distribution_version"),
                ("skill_byte_digest", "skill_byte_digest"),
                ("distribution_tree_digest", "skill_tree_digest"),
                ("distribution_manifest_digest", "distribution_manifest_digest"),
                ("release_status", "release_status"),
            )):
                raise ContractError("skill_version_incompatible")
        return {
            "status": "consumable_after_evidence_check", "consumable": True,
            "lifecycle_status": "candidate_not_approved", "candidate_ref": candidate_ref,
            "candidate_byte_digest": candidate_digest, "evidence_ref": evidence_ref,
            "evidence_bundle_digest": bundle["bundle_digest"], "pipeline_run_id": identity.get("pipeline_run_id"),
            "claim_ceiling": "deterministic_gate_evidence_only_candidate_not_approved",
        }
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc)
        status = "not_consumable_unknown" if reason in {"evidence_schema_invalid", "evidence_digest_mismatch", "candidate_or_evidence_missing"} else "not_consumable"
        return {"status": status, "consumable": False, "reason_code": _external_reason_code(exc), "detail": reason}


def _candidate_kind_for_evidence(candidate_refs: Any, candidate_ref: str, candidate_digest: str) -> str | None:
    """Bind consumption to a candidate identity the pipeline actually discovered and validated."""
    if not isinstance(candidate_refs, dict):
        return None
    for kind, item in candidate_refs.items():
        if isinstance(item, dict) and item.get("ref") == candidate_ref and item.get("byte_digest") == candidate_digest:
            if kind in {"target", "kym", "tco_model", "coverage"}:
                return kind
    return None


def materialize_and_validate(
    workspace: Path, request: dict[str, Any], runtime_root: Path | None = None,
    skill_bindings: dict[str, Path] | None = None, skill_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate an allowlisted materialization and invoke the Slice 4 pipeline once."""
    workspace = workspace.resolve()
    try:
        external = request.get("host_profile_id") == EXTERNAL_PROFILE_ID
        if external:
            if runtime_root is None or skill_bindings is None or skill_sources is None:
                raise ContractError("external_profile_requires_runtime_and_skill_bindings")
            ledger_ref = request["normalized_source_ledger_ref"]
            current_ledger_digest = digest_bytes(_external_workspace_path(workspace, ledger_ref, "normalized source ledger").read_bytes())
            if current_ledger_digest != request["normalized_source_ledger_byte_digest"]:
                raise ContractError("source_digest_mismatch")
            expected_request, identities = build_external_materialization_request(
                runtime_root, workspace, skill_bindings,
                skill_sources,
                request["normalized_source_ledger_ref"], [item["ref"] for item in request["outputs"]],
                request.get("attempt_id", "initial"),
                [item["ref"] for item in request["source_manifest_bindings"]],
                request["skill_selection_record_ref"],
            )
            if request != expected_request:
                raise ContractError("external_request_identity_mismatch")
            profile = external_profile_with_skills(runtime_root, workspace, identities)
            profile["skill_selection_record"] = load_json(
                _external_workspace_path(workspace, request["skill_selection_record_ref"], "skill selection record")
            )
            profile["profile_digest"] = external_profile_digest(profile)
        else:
            profile = require_enabled_profile(workspace)
        _validate_request(workspace, profile, request)
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        reason_code = _external_reason_code(exc) if request.get("host_profile_id") == EXTERNAL_PROFILE_ID else _reason_code(str(exc))
        result = _blocked(reason_code, str(exc), request)
        if request.get("host_profile_id") == EXTERNAL_PROFILE_ID:
            partial_disposition = (
                "cleanup_failed"
                if "partial_output_quarantine" in str(exc).casefold()
                else "not_started"
            )
            result["failure_evidence_ref"] = _record_external_failure(
                workspace, request, reason_code, str(exc),
                partial_output_disposition=partial_disposition,
                retry_key=request.get("pipeline_run_id"),
            )
            result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
        return result

    dedupe_key = pipeline_run_identity(
        request["host_profile_id"], request["skill_distribution_digest"],
        request["generation_start_id"], request["materialization_event_id"],
        request.get("attempt_id") if external else None,
    )
    payload_digest = digest_value(request)
    ledger_path = (
        _external_workspace_path(workspace, RUN_LEDGER_REF, "supported-host run ledger")
        if external else resolved_within(workspace, RUN_LEDGER_REF, "supported-host run ledger")
    )
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    try:
        with _ledger_lock(lock_path):
            ledger = _run_ledger(ledger_path)
            prior = ledger["records"].get(dedupe_key)
            if prior:
                if prior["request_payload_digest"] != payload_digest:
                    result = _blocked("retry_payload_conflict", "same dedupe key with conflicting payload", request)
                    if external:
                        result["failure_evidence_ref"] = _record_external_failure(workspace, request, "retry_payload_conflict", result["detail"])
                        result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
                    return result
                if prior["state"] == "claimed":
                    result = _blocked(
                        "incomplete_prior_claim",
                        "prior process claimed this effect but did not durably complete; effect will not be replayed",
                        request,
                    )
                    if external:
                        result["failure_evidence_ref"] = _record_external_failure(workspace, request, "incomplete_prior_claim", result["detail"])
                        result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
                    return result
                return deepcopy(prior["result"])

            claimed_result = _blocked(
                "incomplete_prior_claim",
                "effect durably claimed; completion not yet recorded",
                request,
            )
            ledger["records"][dedupe_key] = {
                "request_payload_digest": payload_digest,
                "pipeline_run_id": request["pipeline_run_id"],
                "effect_count": 0,
                "state": "claimed",
                "result": claimed_result,
            }
            _write_run_ledger_atomic(ledger_path, ledger)

            module_ref = "src/kym_tco/tco_cli.py"
            if not external and not resolved_within(workspace, module_ref, "repo-local CLI module").is_file():
                result = _blocked("capability_unavailable", "repo-local CLI module unavailable", request)
            else:
                env = dict(os.environ)
                if external:
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
                else:
                    env["PYTHONPATH"] = str((workspace / "src").resolve())
                command = [sys.executable, "-m", "kym_tco.tco_cli", "--help"]
                try:
                    completed = subprocess.run(
                        command, cwd=workspace, env=env, capture_output=True, timeout=15, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    result = _blocked("capability_unavailable", f"repo-local CLI preflight failed: {exc}", request)
                else:
                    if completed.returncode != 0:
                        result = _blocked("capability_unavailable", f"repo-local CLI preflight exit={completed.returncode}", request)
                        result["cli_preflight_status"] = "failed"
                    else:
                        from .post_generation_pipeline import run_post_generation_pipeline
                        pipeline = run_post_generation_pipeline(workspace, request, profile if external else None)
                        bundle = load_json(workspace / pipeline["bundle_ref"])
                        failed_gate_ids = sorted(
                            row["gate_id"] for row in bundle["gates"]
                            if row["required"] and row["applicable"] and row["status"] == "failed"
                        )
                        blocked_gate_ids = sorted(
                            row["gate_id"] for row in bundle["gates"]
                            if row["required"] and row["applicable"] and row["status"] == "blocked"
                        )
                        terminal_status = (
                            pipeline["status"] if not external
                            else "passed" if pipeline["status"] == "passed"
                            else "failed" if failed_gate_ids else "blocked"
                        )
                        reason_code = (
                            "pipeline_passed_deterministic_only" if pipeline["status"] == "passed"
                            else "partial_output_quarantined" if external and pipeline["partial_output_disposition"].get("status") == "cleanup_failed"
                            else "pipeline_required_gates_blocked" if not external
                            else "deterministic_gate_failed" if terminal_status == "failed"
                            else "capability_unavailable"
                        )
                        result = {
                            "status": terminal_status,
                            "reason_code": reason_code,
                            "detail": "installed CLI preflight passed; deterministic post-generation pipeline executed" if external else "repo-local CLI preflight passed; deterministic post-generation pipeline executed",
                            "host_profile_id": request["host_profile_id"],
                            "generation_start_id": request["generation_start_id"],
                            "materialization_event_id": request["materialization_event_id"],
                            "pipeline_run_id": request["pipeline_run_id"],
                            "precondition_status": "passed",
                            "cli_preflight_status": "passed",
                            "runtime_distribution": installed_distribution_identity() if external else None,
                            "host_profile_identity": {
                                "profile_id": profile["host_profile_id"],
                                "profile_version": profile["profile_version"],
                                "profile_digest": profile["profile_digest"],
                            } if external else None,
                            "target_workspace_realpath": str(workspace) if external else None,
                            "selected_skills": [
                                {
                                    "skill_id": item["skill_id"],
                                    "source": item["skill_source"],
                                    "canonical_directory": item["skill_dir"],
                                    "skill_byte_digest": item["skill_byte_digest"],
                                    "distribution_tree_digest": item["skill_tree_digest"],
                                    "distribution_version": item["distribution_version"],
                                }
                                for item in profile["selected_skill_identities"]
                            ] if external else [],
                            "cli_preflight": {
                                "argv": ["python", "-m", "kym_tco.tco_cli", "--help"],
                                "module_ref": module_ref,
                                "exit_code": completed.returncode,
                                "stdout_digest": digest_bytes(completed.stdout),
                                "stderr_digest": digest_bytes(completed.stderr),
                            },
                            "pipeline_status": terminal_status,
                            "pipeline_invoked": True,
                            "failed_gate_ids": failed_gate_ids,
                            "blocked_gate_ids": blocked_gate_ids,
                            "validation_bundle_ref": pipeline["bundle_ref"],
                            "validation_bundle_digest": pipeline["bundle_digest"],
                            "nonterminal_report_ref": pipeline["report_ref"],
                            "nonterminal_html_ref": pipeline["html_ref"],
                            "nonterminal_delivery_manifest_ref": pipeline["delivery_manifest_ref"],
                            "nonterminal_delivery_manifest_digest": pipeline["delivery_manifest_digest"],
                            "partial_output_disposition": pipeline["partial_output_disposition"],
                            "claim_ceiling": pipeline["claim_ceiling"],
                        }
                        if external and terminal_status in {"failed", "blocked"}:
                            result["failure_evidence_ref"] = _record_external_failure(
                                workspace,
                                request,
                                reason_code,
                                "required deterministic gates did not all pass",
                                terminal_status=terminal_status,
                                gate_ids=failed_gate_ids + blocked_gate_ids,
                                generated_artifacts=(
                                    [{
                                        "disposition_digest": digest_value(pipeline["partial_output_disposition"]),
                                        "generated_artifacts": pipeline["partial_output_disposition"].get("generated_artifacts", []),
                                        "retained_artifacts": pipeline["partial_output_disposition"].get("retained_artifacts", []),
                                        "cleanup_errors": pipeline["partial_output_disposition"].get("cleanup_errors", []),
                                        "disposition_manifest_ref": pipeline["partial_output_disposition"].get("manifest_ref"),
                                        "conflict_ref": pipeline["partial_output_disposition"].get("conflict_ref"),
                                    }]
                                    if pipeline["partial_output_disposition"].get("status") == "cleanup_failed"
                                    else bundle["artifacts"]
                                ),
                                partial_output_disposition=(
                                    "cleanup_failed" if pipeline["partial_output_disposition"]["status"] == "cleanup_failed"
                                    else "quarantined" if pipeline["partial_output_disposition"]["status"] == "quarantined"
                                    else "retained_nonterminal"
                                ),
                                retry_key=request["pipeline_run_id"],
                            )
                            result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
            if external and result.get("status") in {"blocked", "failed"} and "failure_evidence_ref" not in result:
                reason_code = result.get("reason_code", "capability_unavailable")
                partial_disposition = (
                    "cleanup_failed"
                    if reason_code == "partial_output_quarantined"
                    else "not_started"
                )
                result["failure_evidence_ref"] = _record_external_failure(
                    workspace, request, reason_code, result.get("detail", "external CLI invocation failed"),
                    partial_output_disposition=partial_disposition,
                )
                result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
            ledger["records"][dedupe_key] = {
                "request_payload_digest": payload_digest,
                "pipeline_run_id": request["pipeline_run_id"],
                "effect_count": 1,
                "state": "completed",
                "result": result,
            }
            _write_run_ledger_atomic(ledger_path, ledger)
            return deepcopy(result)
    except (ContractError, OSError) as exc:
        reason_code = _external_reason_code(exc) if request.get("host_profile_id") == EXTERNAL_PROFILE_ID else _reason_code(str(exc))
        result = _blocked(reason_code, str(exc), request)
        if request.get("host_profile_id") == EXTERNAL_PROFILE_ID:
            result["failure_evidence_ref"] = _record_external_failure(
                workspace, request, reason_code, str(exc), retry_key=request.get("pipeline_run_id"),
            )
            result["failure_evidence_status"] = "written" if result["failure_evidence_ref"] else "write_failed"
        return result
