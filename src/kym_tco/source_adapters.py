"""Slice 2 deterministic source normalization and closure accounting.

This module freezes bytes and applicability only.  It does not synthesize KYM/TCO,
invoke a host, run the post-generation pipeline, or produce a report.
"""
from __future__ import annotations

import fnmatch
import json
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .common import ContractError, digest_bytes, digest_value, ensure_relative_path, load_json, resolved_within


INPUT_KINDS = {
    "repo", "conversation_text", "file_set", "dashboard", "kb", "kym",
    "target", "tco_model", "coverage",
}


def _schema(name: str) -> dict[str, Any]:
    text = resources.files("kym_tco.schemas").joinpath(name).read_text(encoding="utf-8")
    return json.loads(text)


def _validate_schema(document: dict[str, Any], name: str, label: str) -> None:
    errors = sorted(Draft202012Validator(_schema(name)).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors)
        raise ContractError(f"{label} schema 校验失败: {detail}")


def manifest_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("manifest_digest", None)
    return digest_value(payload)


def ledger_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("ledger_digest", None)
    return digest_value(payload)


def validate_source_input_manifest(document: dict[str, Any]) -> dict[str, Any]:
    _validate_schema(document, "source_input_manifest_v1.schema.json", "source input manifest")
    if document["manifest_digest"] != manifest_payload_digest(document):
        raise ContractError("source input manifest_digest 与 canonical payload 不一致")
    ids = [item["input_id"] for item in document["inputs"]]
    if len(ids) != len(set(ids)):
        raise ContractError("source input_id 必须唯一")
    for item in document["inputs"]:
        if item["kind"] not in INPUT_KINDS:
            raise ContractError(f"source input kind 未支持: {item['kind']}")
        source = item["source"]
        if source["mode"] == "inline_text" and item["kind"] != "conversation_text":
            raise ContractError("inline_text 只允许 conversation_text input")
        if source["mode"] == "inline_text" and item["boundary"]["root_ref"]:
            raise ContractError("inline_text boundary.root_ref 必须为空")
        if source["mode"] == "workspace_paths":
            root_text = item["boundary"]["root_ref"]
            if not root_text:
                raise ContractError(f"workspace source boundary.root_ref 不得为空: {item['input_id']}")
            root = ensure_relative_path(root_text, "source boundary root_ref")
            paths = [ref["path"] for ref in source["refs"]]
            if len(paths) != len(set(paths)):
                raise ContractError(f"source refs 不得重复: {item['input_id']}")
            for ref in paths:
                path = ensure_relative_path(ref, "source ref")
                if path != root and root not in path.parents:
                    raise ContractError(f"source ref 不在 declared boundary 内: {ref}")
                resolved_within(Path("."), ref, "source ref")
    return document


def load_source_input_manifest(path: Path) -> dict[str, Any]:
    return validate_source_input_manifest(load_json(path))


def _files_for_ref(path: Path) -> list[Path]:
    if path.is_symlink():
        raise ContractError(f"source ref 不允许 symlink: {path}")
    if path.is_file():
        return [path]
    if path.is_dir():
        files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        if any(candidate.is_symlink() for candidate in files):
            raise ContractError(f"source directory 不允许 symlink file: {path}")
        return files
    raise ContractError(f"source ref 不存在: {path}")


def workspace_ref_digest(workspace: Path, ref: str) -> str:
    target = resolved_within(workspace, ref, "source ref")
    files = _files_for_ref(target)
    if target.is_file():
        return digest_bytes(target.read_bytes())
    snapshot = [
        {"path": file.relative_to(target).as_posix(), "byte_digest": digest_bytes(file.read_bytes())}
        for file in files
    ]
    return digest_value(snapshot)


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or Path(path).match(pattern) for pattern in patterns)


def _unit_disposition(relative: str, include: list[str], exclude: list[str]) -> tuple[str, str]:
    if include and not _matches(relative, include):
        return "skipped", "not_selected_by_include_policy"
    if exclude and _matches(relative, exclude):
        return "skipped", "excluded_by_declared_policy"
    return "included", "selected_by_declared_policy"


def _empty_closure(term: str) -> dict[str, Any]:
    return {"term": term, "discovered": 0, "included": 0, "skipped": 0, "failed": 0, "units": []}


def _blocked_result(item: dict[str, Any], diagnostic: str, status: str = "blocked") -> dict[str, Any]:
    return {
        "input_id": item["input_id"], "kind": item["kind"], "requirement": item["requirement"],
        "attempted": True, "status": status, "input_snapshot_digest": digest_value({"error": diagnostic}),
        "diagnostics": [diagnostic], "closure": _empty_closure(item["boundary"]["requested_closure"]),
    }


def _normalize_input(item: dict[str, Any], workspace: Path) -> dict[str, Any]:
    source = item["source"]
    requested = item["boundary"]["requested_closure"]
    if source["mode"] == "inline_text":
        actual = digest_bytes(source["text"].encode("utf-8"))
        if actual != source["byte_digest"]:
            return _blocked_result(item, "inline_text byte_digest mismatch")
        unit = {
            "source_unit_id": f"{item['input_id']}-u0001", "ref": f"inline:{item['input_id']}",
            "byte_digest": actual, "disposition": "included", "reason": "inline_text_frozen",
        }
        closure = {"term": "declared_closure", "discovered": 1, "included": 1, "skipped": 0, "failed": 0, "units": [unit]}
        return {
            "input_id": item["input_id"], "kind": item["kind"], "requirement": item["requirement"],
            "attempted": True, "status": "passed", "input_snapshot_digest": actual,
            "diagnostics": ["source bytes frozen; semantic synthesis not run"], "closure": closure,
        }

    refs = source["refs"]
    missing_optional_audit = item["requirement"] == "optional" and item["kind"] in {"dashboard", "kb"}
    snapshots: list[dict[str, str]] = []
    all_files: list[tuple[Path, str]] = []
    seen: set[str] = set()
    try:
        for ref in refs:
            target = resolved_within(workspace, ref["path"], "source ref")
            if not target.exists() and missing_optional_audit:
                return _blocked_result(item, "optional source absent; applicability not present", "not_applicable")
            actual = workspace_ref_digest(workspace, ref["path"])
            if actual != ref["byte_digest"]:
                return _blocked_result(item, f"source ref byte_digest mismatch: {ref['path']}")
            snapshots.append({"path": ref["path"], "byte_digest": actual})
            for file in _files_for_ref(target):
                workspace_ref = file.relative_to(workspace.resolve()).as_posix()
                if workspace_ref in seen:
                    return _blocked_result(item, f"source unit duplicate discovery: {workspace_ref}")
                seen.add(workspace_ref)
                all_files.append((file, workspace_ref))
    except (ContractError, OSError) as exc:
        return _blocked_result(item, str(exc))

    include = item["boundary"]["include_patterns"]
    exclude = item["boundary"]["exclude_patterns"]
    units = []
    for index, (file, workspace_ref) in enumerate(sorted(all_files, key=lambda pair: pair[1]), 1):
        boundary_root = item["boundary"]["root_ref"].strip("/")
        relative = workspace_ref
        if boundary_root and workspace_ref.startswith(boundary_root + "/"):
            relative = workspace_ref[len(boundary_root) + 1:]
        disposition, reason = _unit_disposition(relative, include, exclude)
        try:
            byte_digest = digest_bytes(file.read_bytes())
        except OSError as exc:
            disposition, reason = "failed", f"read_failed:{exc.__class__.__name__}"
            byte_digest = digest_value({"unreadable": workspace_ref})
        units.append({
            "source_unit_id": f"{item['input_id']}-u{index:04d}", "ref": workspace_ref,
            "byte_digest": byte_digest, "disposition": disposition, "reason": reason,
        })
    counts = {name: sum(unit["disposition"] == name for unit in units) for name in ("included", "skipped", "failed")}
    term = requested
    if requested == "full_source_closure" and (include or exclude or counts["failed"]):
        return _blocked_result(item, "full_source_closure overclaim: filters or failed units present")
    if requested == "declared_closure" and (include or exclude):
        term = "selected_closure"
    closure = {"term": term, "discovered": len(units), **counts, "units": units}
    status = "blocked" if counts["failed"] else "passed"
    return {
        "input_id": item["input_id"], "kind": item["kind"], "requirement": item["requirement"],
        "attempted": True, "status": status, "input_snapshot_digest": digest_value(snapshots),
        "diagnostics": ["source bytes normalized; semantic synthesis not run"], "closure": closure,
    }


def _derive_applicability(results: list[dict[str, Any]]) -> dict[str, Any]:
    rules = {
        "dashboard": ("dashboard", "RULE-DASHBOARD-PRESENT"),
        "kb": ("kb", "RULE-KB-PRESENT"),
        "git": (None, "RULE-GIT-PRESENT"),
    }

    def matches(axis: str, ref: str) -> bool:
        parts = Path(ref).parts
        if axis == "dashboard":
            return "Dashboard" in parts
        if axis == "kb":
            return "kb" in parts
        return ".git" in parts and parts[-1] == "HEAD"

    applicability: dict[str, Any] = {}
    for axis, (explicit_kind, rule_id) in rules.items():
        evidence: list[str] = []
        for item in results:
            if explicit_kind and item["kind"] == explicit_kind:
                if item["status"] == "not_applicable":
                    evidence.append(f"explicit_input:{item['input_id']}:status=not_applicable:absent")
                else:
                    evidence.append(f"explicit_input:{item['input_id']}:status={item['status']}")
            for unit in item["closure"]["units"]:
                if matches(axis, unit["ref"]):
                    evidence.append(
                        f"discovered_unit:{unit['ref']}:disposition={unit['disposition']}"
                    )
        present = any(
            not item.endswith(":absent")
            for item in evidence
        )
        if not evidence:
            evidence = ["no_explicit_or_discovered_source"]
        applicability[axis] = {
            "status": "applicable" if present else "not_applicable",
            "rule_id": rule_id,
            "evidence": sorted(evidence),
        }
    return applicability


NORMALIZATION_RULE_VERSION = "source-normalization-v1"


def _canonical_manifest_byte_digest(document: dict[str, Any]) -> str:
    """Stable fallback for in-process callers that do not have original file bytes."""
    return digest_bytes(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def normalize_source_manifest(
    document: dict[str, Any], workspace: Path, *, manifest_byte_digest: str | None = None,
    manifest_ref: str = "in_process"
) -> dict[str, Any]:
    validate_source_input_manifest(document)
    workspace = workspace.resolve()
    results = [_normalize_input(item, workspace) for item in document["inputs"]]
    required_blocked = any(item["requirement"] == "required" and item["status"] != "passed" for item in results)
    ledger = {
        "schema_version": "normalized_source_ledger_v1",
        "normalization_rule_version": NORMALIZATION_RULE_VERSION,
        "manifest_id": document["manifest_id"], "manifest_digest": document["manifest_digest"],
        "source_manifest_bindings": [{
            "ref": manifest_ref,
            "byte_digest": manifest_byte_digest or _canonical_manifest_byte_digest(document),
            "manifest_id": document["manifest_id"],
            "manifest_digest": document["manifest_digest"],
        }],
        "workspace_boundary": ".", "inputs": results,
        "aggregate_status": "blocked" if required_blocked else "passed",
        "coverage_mode": "repo" if any(item["kind"] == "repo" and item["status"] == "passed" for item in results) else "not_evaluated",
        "applicability": _derive_applicability(results),
    }
    ledger["ledger_digest"] = ledger_payload_digest(ledger)
    return validate_normalized_source_ledger(ledger)


def normalize_source_manifests(
    entries: list[tuple[str, dict[str, Any], str]], workspace: Path
) -> dict[str, Any]:
    """Normalize multiple manifests without dropping or deduplicating declared inputs."""
    if not entries:
        raise ContractError("至少需要一个 source input manifest")
    ordered = sorted(entries, key=lambda item: item[0])
    refs = [item[0] for item in ordered]
    if len(refs) != len(set(refs)):
        raise ContractError("source input manifest ref 不得重复")
    inputs: list[dict[str, Any]] = []
    bindings = []
    seen_ids: set[str] = set()
    for ref, document, byte_digest in ordered:
        validate_source_input_manifest(document)
        bindings.append({
            "ref": ref, "byte_digest": byte_digest,
            "manifest_id": document["manifest_id"], "manifest_digest": document["manifest_digest"],
        })
        for item in document["inputs"]:
            if item["input_id"] in seen_ids:
                raise ContractError(f"合并 manifest 的 input_id 冲突，拒绝去重: {item['input_id']}")
            seen_ids.add(item["input_id"])
            inputs.append(item)
    inputs.sort(key=lambda item: item["input_id"])
    combined = {
        "schema_version": "source_input_manifest_v1",
        "manifest_id": "merged_" + digest_value(bindings).split(":", 1)[-1][:16],
        "workspace_boundary": ".",
        "inputs": inputs,
    }
    combined["manifest_digest"] = manifest_payload_digest(combined)
    ledger = normalize_source_manifest(combined, workspace, manifest_ref="merged")
    ledger["source_manifest_bindings"] = bindings
    ledger["ledger_digest"] = ledger_payload_digest(ledger)
    return validate_normalized_source_ledger(ledger)


def validate_normalized_source_ledger(document: dict[str, Any]) -> dict[str, Any]:
    _validate_schema(document, "normalized_source_ledger_v1.schema.json", "normalized source ledger")
    if document["ledger_digest"] != ledger_payload_digest(document):
        raise ContractError("normalized source ledger_digest 与 canonical payload 不一致")
    ids = [item["input_id"] for item in document["inputs"]]
    if len(ids) != len(set(ids)):
        raise ContractError("normalized source input_id 必须唯一")
    for item in document["inputs"]:
        closure = item["closure"]
        units = closure["units"]
        unit_ids = [unit["source_unit_id"] for unit in units]
        refs = [unit["ref"] for unit in units]
        if len(unit_ids) != len(set(unit_ids)) or len(refs) != len(set(refs)):
            raise ContractError(f"source closure unit 必须唯一: {item['input_id']}")
        actual = {name: sum(unit["disposition"] == name for unit in units) for name in ("included", "skipped", "failed")}
        if closure["discovered"] != len(units) or any(closure[name] != actual[name] for name in actual):
            raise ContractError(f"source closure denominator 不闭合: {item['input_id']}")
        if closure["discovered"] != closure["included"] + closure["skipped"] + closure["failed"]:
            raise ContractError(f"discovered != included + skipped + failed: {item['input_id']}")
        if closure["term"] == "full_source_closure" and (closure["skipped"] or closure["failed"]):
            raise ContractError(f"full_source_closure overclaim: {item['input_id']}")
    required_blocked = any(item["requirement"] == "required" and item["status"] != "passed" for item in document["inputs"])
    expected_aggregate = "blocked" if required_blocked else "passed"
    if document["aggregate_status"] != expected_aggregate:
        raise ContractError("normalized aggregate_status 与 required input outcomes 不一致")
    expected_coverage = "repo" if any(item["kind"] == "repo" and item["status"] == "passed" for item in document["inputs"]) else "not_evaluated"
    if document["coverage_mode"] != expected_coverage:
        raise ContractError("Coverage mode 与 repo input applicability 不一致")
    if document["applicability"] != _derive_applicability(document["inputs"]):
        raise ContractError("Dashboard/KB/Git applicability 不可由 frozen source units 复算")
    return document


def verify_normalized_ledger(
    document: dict[str, Any], manifest: dict[str, Any], workspace: Path, *, manifest_byte_digest: str | None = None,
    manifest_ref: str = "in_process"
) -> dict[str, Any]:
    validate_normalized_source_ledger(document)
    expected = normalize_source_manifest(manifest, workspace, manifest_byte_digest=manifest_byte_digest, manifest_ref=manifest_ref)
    if document != expected:
        raise ContractError("normalized source ledger 与当前 manifest/workspace bytes 不一致")
    return document


def verify_normalized_manifests(
    document: dict[str, Any], entries: list[tuple[str, dict[str, Any], str]], workspace: Path
) -> dict[str, Any]:
    validate_normalized_source_ledger(document)
    expected = normalize_source_manifests(entries, workspace)
    if document != expected:
        raise ContractError("normalized source ledger 与原始 manifest bytes/当前 workspace sources 不一致")
    return document
