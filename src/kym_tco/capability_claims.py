from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import digest_bytes


OPERATIONS = {
    "compare",
    "execute",
    "ingest",
    "package",
    "render",
    "synthesize",
    "validate",
}
EFFECT_BY_BUCKET = {
    "materialized_output_ids": "materialized",
    "validated_output_ids": "validated",
}
OUTPUT_BUCKETS = (*EFFECT_BY_BUCKET, "unresolved_output_ids")
SYNTHESIS_CLAIM_RE = re.compile(
    r"(?:^|[-_\s])(propose|proposal|generate|generator|synthesize|create|build|draft|produce)(?:$|[-_\s])|生成|创建|提出|产出|构建|起草",
    re.IGNORECASE,
)


def validate_execution_semantics(node: dict[str, Any]) -> list[str]:
    semantics = node.get("execution_semantics")
    if semantics is None:
        return []
    errors: list[str] = []
    operation = semantics.get("operation")
    if operation not in OPERATIONS:
        errors.append(f"unknown operation: {operation}")
    output_ids = {item["id"] for item in node.get("outputs", [])}
    bucket_values = {label: semantics.get(label, []) for label in OUTPUT_BUCKETS}
    materialized = bucket_values["materialized_output_ids"]
    validated = bucket_values["validated_output_ids"]
    unresolved = bucket_values["unresolved_output_ids"]
    for label, values in bucket_values.items():
        if len(values) != len(set(values)):
            errors.append(f"{label} contains duplicates")
        missing = sorted(set(values) - output_ids)
        if missing:
            errors.append(f"{label} references undeclared outputs: {missing}")
    overlap = sorted(
        (set(materialized) & set(validated))
        | (set(materialized) & set(unresolved))
        | (set(validated) & set(unresolved))
    )
    if overlap:
        errors.append(f"outputs must belong to exactly one execution bucket: {overlap}")
    classified = set().union(*(set(values) for values in bucket_values.values()))
    unclassified = sorted(output_ids - classified)
    if unclassified:
        errors.append(f"outputs lack an execution classification: {unclassified}")
    if operation in {"ingest", "package", "render", "synthesize"} and not materialized:
        errors.append(f"operation {operation} must materialize at least one output")
    claim_text = f"{node.get('title', '')} {node.get('capability_type', '')}"
    if SYNTHESIS_CLAIM_RE.search(claim_text) and operation != "synthesize":
        errors.append(
            f"synthesis wording requires operation=synthesize, not {operation}"
        )
    return errors


def _state_rank(config: dict[str, Any], state: str) -> int:
    ranks = {
        item["id"]: int(item["rank"])
        for item in config["coverage_vocabulary"]["states"]
    }
    return ranks.get(state, -1)


def _stdout_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("status") != "passed":
        return None
    try:
        payload = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_witness_artifact(artifact_root: Path, artifact_ref: str) -> Path | None:
    if not artifact_ref or "://" in artifact_ref:
        return None
    root = artifact_root.resolve()
    candidate = Path(artifact_ref)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def _artifact_identity_matches(
    witness: dict[str, Any], artifact_root: Path | None
) -> tuple[bool, Path | None]:
    if artifact_root is None:
        return False, None
    artifact_ref = witness.get("artifact_ref")
    if not isinstance(artifact_ref, str):
        return False, None
    artifact = resolve_witness_artifact(artifact_root, artifact_ref)
    if artifact is None:
        return False, None
    expected = witness.get("artifact_digest")
    return digest_bytes(artifact.read_bytes()) == expected, artifact


def audit_runtime_capability_witnesses(
    model: dict[str, Any],
    coverage: dict[str, Any],
    config: dict[str, Any],
    verification_results: dict[str, Any],
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    nodes = {item["id"]: item for item in model.get("nodes", [])}
    records = {item["tco_node_id"]: item for item in coverage.get("records", [])}
    threshold = _state_rank(config, "bounded_runtime")
    witnesses: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for command_id, result in verification_results.items():
        payload = _stdout_payload(result)
        if payload is None:
            parse_failures.append(command_id)
            continue
        command_witnesses = payload.get("capability_witnesses")
        if not isinstance(command_witnesses, list):
            parse_failures.append(command_id)
            continue
        for witness in command_witnesses:
            if isinstance(witness, dict):
                witnesses.append({**witness, "verification_command_id": command_id})

    findings: list[dict[str, Any]] = []
    checked_nodes: list[str] = []
    verified_artifacts: dict[tuple[str, str], dict[str, str]] = {}
    for node_id, record in records.items():
        node = nodes.get(node_id)
        if not node or node.get("node_kind") != "m1":
            continue
        requirement_states = [
            item["coverage_state"]
            for item in record.get("requirement_coverage", [])
            if item.get("requirement_id") in set(node.get("mandatory_coverage_requirement_ids", []))
        ]
        declared_states = [
            record.get("coverage_state", "unknown"),
            record.get("effective_state", "unknown"),
            record.get("claim_state", "unknown"),
            *requirement_states,
        ]
        if max((_state_rank(config, state) for state in declared_states), default=-1) < threshold:
            continue
        checked_nodes.append(node_id)
        semantics = node.get("execution_semantics")
        if semantics is None:
            findings.append(
                {
                    "code": "TCO_RUNTIME_EXECUTION_SEMANTICS_MISSING",
                    "node_id": node_id,
                    "message": "bounded_runtime claim requires execution_semantics",
                }
            )
            continue
        static_errors = validate_execution_semantics(node)
        for message in static_errors:
            findings.append(
                {
                    "code": "TCO_RUNTIME_EXECUTION_SEMANTICS_INVALID",
                    "node_id": node_id,
                    "message": message,
                }
            )
        unresolved_outputs = semantics.get("unresolved_output_ids", [])
        if unresolved_outputs:
            findings.append(
                {
                    "code": "TCO_RUNTIME_EXECUTION_OUTPUT_UNRESOLVED",
                    "node_id": node_id,
                    "output_ids": sorted(unresolved_outputs),
                    "message": "bounded_runtime claim cannot retain unresolved output effects",
                }
            )
        for bucket, expected_effect in EFFECT_BY_BUCKET.items():
            for output_id in semantics.get(bucket, []):
                matching = [
                    witness
                    for witness in witnesses
                    if witness.get("node_id") == node_id
                    and witness.get("operation") == semantics.get("operation")
                    and witness.get("output_id") == output_id
                    and witness.get("effect") == expected_effect
                    and isinstance(witness.get("artifact_ref"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", str(witness.get("artifact_digest", "")))
                ]
                if not matching:
                    findings.append(
                        {
                            "code": "TCO_RUNTIME_CAPABILITY_WITNESS_MISMATCH",
                            "node_id": node_id,
                            "output_id": output_id,
                            "expected_operation": semantics.get("operation"),
                            "expected_effect": expected_effect,
                            "message": "bounded_runtime output claim lacks a matching capability witness",
                        }
                    )
                    continue
                identity_matches = []
                for witness in matching:
                    identity_ok, artifact = _artifact_identity_matches(witness, artifact_root)
                    if identity_ok and artifact is not None:
                        identity_matches.append((witness, artifact))
                if not identity_matches:
                    findings.append(
                        {
                            "code": "TCO_RUNTIME_CAPABILITY_ARTIFACT_IDENTITY_MISMATCH",
                            "node_id": node_id,
                            "output_id": output_id,
                            "message": "capability witness artifact is missing, outside the workspace, or has a different byte digest",
                        }
                    )
                    continue
                witness, artifact = identity_matches[0]
                artifact_ref = str(artifact.relative_to(artifact_root.resolve()))
                verified_artifacts[(artifact_ref, witness["artifact_digest"])] = {
                    "artifact_ref": artifact_ref,
                    "artifact_digest": witness["artifact_digest"],
                    "node_id": node_id,
                    "output_id": output_id,
                    "effect": expected_effect,
                }
    return {
        "schema": "kym_tco.capability_claim_audit.v1",
        "checked_node_ids": sorted(checked_nodes),
        "witness_count": len(witnesses),
        "verified_artifacts": sorted(
            verified_artifacts.values(), key=lambda item: (item["artifact_ref"], item["output_id"])
        ),
        "unparseable_or_missing_witness_commands": sorted(parse_failures),
        "findings": findings,
        "error_count": len(findings),
        "verdict": "pass" if not findings else "fail",
        "claim_ceiling": "execution witness alignment only; no semantic correctness or approval",
    }
