"""Slice 4 deterministic post-generation validation and nonterminal reader."""
from __future__ import annotations

import html
import json
import os
import platform
import subprocess
import sys
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import __version__
from .common import ContractError, digest_bytes, digest_value, load_json, resolved_within, utc_now, write_json
from .dashboard_observer import observe_dashboard
from .git_freshness import audit_git_freshness
from .gate_registry import load_gate_registry
from .source_adapters import validate_normalized_source_ledger
from .supported_host import EXTERNAL_PROFILE_ID, _external_workspace_path, _validate_request, installed_distribution_identity, validate_materialization_request, validate_skill_distribution
from .tco_ops import build_candidate_report
from .validation import validate_document


RENDERER_VERSION = "slice4-reader-v6-r004-m1-detail-layout"
CLAIM_CEILING = "deterministic_validation_only_no_semantic_synthesis_coverage_promotion_or_approval"
TERMINAL = {"not_run", "blocked", "passed", "failed", "not_applicable"}
SKILL_IDS = ("build-kym", "build-tco-coverage")
COMMAND_MODULES = {
    "G-ENV-DOCTOR": "src/kym_tco/tco_cli.py",
    "G-KYM-VALIDATE": "src/kym_tco/kym_cli.py", "G-KYM-LINT": "src/kym_tco/kym_cli.py",
    "G-KYM-AUDIT": ".codex/skills/build-kym/scripts/audit_candidate.py", "G-KYM-PACKAGE": "src/kym_tco/kym_cli.py",
    "G-TCO-MODEL-VALIDATE": "src/kym_tco/tco_cli.py", "G-TCO-COVERAGE-VALIDATE": "src/kym_tco/tco_cli.py",
    "G-TCO-VERIFY": "src/kym_tco/tco_cli.py", "G-TCO-AUDIT": ".codex/skills/build-tco-coverage/scripts/audit_candidate.py",
    "G-TCO-PACKAGE": "src/kym_tco/tco_cli.py", "G-TCO-RENDER": "src/kym_tco/tco_cli.py",
}


def bundle_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("bundle_digest", None)
    return digest_value(payload)


def _portable(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _artifact_identity(workspace: Path, ref: str) -> dict[str, Any]:
    path = resolved_within(workspace, ref, "pipeline artifact")
    if not path.exists():
        return {"ref": ref, "kind": "missing", "byte_digest": None, "file_count": 0}
    if path.is_file():
        return {"ref": ref, "kind": "file", "byte_digest": digest_bytes(path.read_bytes()), "file_count": 1}
    if not path.is_dir():
        return {"ref": ref, "kind": "missing", "byte_digest": None, "file_count": 0}
    files = [
        {"ref": item.relative_to(path).as_posix(), "byte_digest": digest_bytes(item.read_bytes())}
        for item in sorted(path.rglob("*")) if item.is_file()
    ]
    return {"ref": ref, "kind": "directory", "byte_digest": digest_value(files), "file_count": len(files)}


def _candidate_refs(ledger: dict[str, Any], request: dict[str, Any], workspace: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    for item in ledger["inputs"]:
        if item["status"] == "passed" and item["kind"] in {"kym", "target", "tco_model", "coverage"}:
            included = [unit["ref"] for unit in item["closure"]["units"] if unit["disposition"] == "included"]
            if included:
                refs[item["kind"]] = included[0]
    for output in request["outputs"]:
        try:
            document = load_json(resolved_within(workspace, output["ref"], "materialized output"))
        except ContractError:
            continue
        schema = str(document.get("schema", ""))
        if schema == "kym_tco.kym_model.v1":
            refs["kym"] = output["ref"]
        elif schema == "kym_tco.target_fragment.v1":
            refs["target"] = output["ref"]
        elif schema.startswith("kym_tco.tco_model."):
            refs["tco_model"] = output["ref"]
        elif schema.startswith("kym_tco.tco_coverage_model."):
            refs["coverage"] = output["ref"]
    return refs


def _applicability_context(
    workspace: Path, request: dict[str, Any], profile_override: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    profile = profile_override or validate_materialization_request(workspace, request)
    if profile_override:
        _validate_request(workspace, profile, request)
    ledger_path = resolved_within(workspace, request["normalized_source_ledger_ref"], "normalized source ledger")
    ledger = validate_normalized_source_ledger(load_json(ledger_path))
    refs = _candidate_refs(ledger, request, workspace)
    candidate_refs = {
        kind: {"ref": ref, "byte_digest": digest_bytes(resolved_within(workspace, ref, "candidate ref").read_bytes())}
        for kind, ref in sorted(refs.items())
    }
    context = {
        "normalized_source_ledger_ref": request["normalized_source_ledger_ref"],
        "normalized_source_ledger_byte_digest": digest_bytes(ledger_path.read_bytes()),
        "source_manifest_bindings": ledger.get("source_manifest_bindings", []),
        "normalization_rule_version": ledger.get("normalization_rule_version"),
        "skill_selection": {
            "record_ref": request.get("skill_selection_record_ref"),
            "record_byte_digest": request.get("skill_selection_record_byte_digest"),
            "assurance": request.get("skill_selection_assurance", "not_applicable"),
        } if request.get("host_profile_id") == EXTERNAL_PROFILE_ID else None,
        "request_payload_digest": digest_value(request),
        "generation_start_id": request["generation_start_id"],
        "materialization_event_id": request["materialization_event_id"],
        "pipeline_run_id": request["pipeline_run_id"],
        "materialized_outputs": sorted(request["outputs"], key=lambda item: item["ref"]),
        "input_kinds": sorted({item["kind"] for item in ledger["inputs"]}),
        "source_applicability": ledger["applicability"],
        "candidate_refs": candidate_refs,
        "report_projection_requested": "tco_model" in refs and "coverage" in refs,
    }
    if profile.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        context["supported_host_identity"] = {
            "profile_id": profile["host_profile_id"],
            "profile_version": profile["profile_version"],
            "profile_digest": profile["profile_digest"],
            "target_workspace_realpath": str(workspace),
            "runtime_distribution": installed_distribution_identity(),
        }
    if context["source_applicability"]["dashboard"]["status"] == "applicable":
        try:
            observation = observe_dashboard(workspace)
            context["dashboard_observation"] = {
                "dashboard_ref": observation["dashboard_ref"],
                "pre_fix_byte_digest": observation["pre_fix_byte_digest"],
                "observation_digest": observation["observation_digest"],
                "duplicate_count": len(observation["duplicate_findings"]),
                "invalid_status_count": len(observation["invalid_status_occurrences"]),
                "unresolved_count": len(observation["unresolved_occurrences"]),
            }
        except ContractError as exc:
            context["dashboard_observation"] = {"status": "blocked", "error": str(exc)}
    else:
        context["dashboard_observation"] = {"status": "not_applicable"}
    context["context_digest"] = digest_value(context)
    return context, profile, refs


def _skill_identities(workspace: Path, profile: dict[str, Any]) -> list[dict[str, Any]]:
    if profile.get("selected_skill_identities"):
        identities = []
        for item in sorted(profile["selected_skill_identities"], key=lambda row: row["skill_id"]):
            identity = {
                "skill_id": item["skill_id"],
                "skill_source": item["skill_source"],
                "distribution_version": item["distribution_version"],
                "release_status": item["release_status"],
                "skill_byte_digest": item["skill_byte_digest"],
                "skill_tree_digest": item["skill_tree_digest"],
                "distribution_manifest_digest": item["distribution_manifest_digest"],
            }
            identities.append({
                "skill_id": item["skill_id"],
                "distribution_version": item["distribution_version"],
                "release_status": item["release_status"],
                "source_revision": "profile:" + profile["profile_digest"],
                "artifact_id": "external-profile:" + item["skill_id"],
                "skill_ref": "host-selected/" + item["skill_source"] + "/" + item["skill_id"] + "/SKILL.md",
                "skill_byte_digest": item["skill_byte_digest"],
                "distribution_tree_digest": item["skill_tree_digest"],
                "distribution_manifest_digest": item["distribution_manifest_digest"],
                "selection_assurance": profile.get("skill_selection_record", {}).get("selection_assurance", "not_attested"),
                "distribution_artifact_ref": "host-profile:installed_cli_external_workspace_v1",
                "distribution_artifact_byte_digest": digest_value({"profile_digest": profile["profile_digest"], **identity}),
                "archive_manifest_digest": digest_value(identity),
                "manifest_digest": digest_value({"profile_digest": profile["profile_digest"], **identity}),
            })
        return identities
    identities: list[dict[str, Any]] = []
    expected_by_id = {item["skill_id"]: item for item in profile["expected_skill_distributions"]}
    for skill_id in SKILL_IDS:
        artifact_ref = expected_by_id[skill_id]["distribution_artifact_ref"]
        artifact_path = resolved_within(workspace, artifact_ref, "Skill distribution")
        document = validate_skill_distribution(load_json(artifact_path), workspace)
        identities.append({
            "skill_id": skill_id,
            "distribution_version": document["distribution_version"],
            "release_status": document["release_status"],
            "source_revision": document["source_revision"],
            "artifact_id": document["artifact_id"],
            "skill_ref": document["skill_ref"],
            "skill_byte_digest": document["skill_byte_digest"],
            "distribution_artifact_ref": artifact_ref,
            "distribution_artifact_byte_digest": digest_bytes(artifact_path.read_bytes()),
            "archive_manifest_digest": document["archive_manifest_digest"],
            "manifest_digest": document["manifest_digest"],
        })
    return identities


def _cli_identity(workspace: Path, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    if profile and profile.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        distribution = installed_distribution_identity()
        module_digest = distribution["entry_module_digest"]
        return {
            "package_version": __version__, "release_status": "installed_distribution",
            "module_ref": "installed:kym_tco.tco_cli", "module_byte_digest": module_digest,
            "import_source": "installed_distribution", "distribution_ref": "installed:kym-tco",
            "distribution_digest": distribution["distribution_digest"],
            "expected_distribution_digest": distribution["expected_distribution_digest"],
            "observed_distribution_digest": distribution["observed_distribution_digest"],
            "distribution_manifest_authority": distribution["distribution_manifest_authority"],
            "distribution_file_count": distribution["file_count"],
        }
    module_ref = "src/kym_tco/__init__.py"
    distribution_ref = "pyproject.toml"
    return {
        "package_version": __version__,
        "release_status": "unreleased",
        "module_ref": module_ref,
        "module_byte_digest": digest_bytes((workspace / module_ref).read_bytes()),
        "import_source": "repo_src",
        "distribution_ref": distribution_ref,
        "distribution_digest": digest_bytes((workspace / distribution_ref).read_bytes()),
    }


def _gate_applicability(
    gate: dict[str, Any], context: dict[str, Any], refs: dict[str, str]
) -> tuple[bool, list[dict[str, Any]]]:
    gid = gate["gate_id"]
    raw = set(context["input_kinds"]) & {"repo", "conversation_text", "file_set", "dashboard", "kb"}
    pair = "tco_model" in refs and "coverage" in refs
    app = context["source_applicability"]
    applicable = True
    predicate_inputs: Any = {"pipeline_run_id": context["pipeline_run_id"]}
    if gid == "G-SOURCE-NORMALIZE":
        applicable = bool(raw); predicate_inputs = {"raw_input_kinds": sorted(raw)}
    elif gid == "G-SOURCE-CLOSURE":
        applicable = bool(raw & {"repo", "file_set"}); predicate_inputs = {"discoverable_input_kinds": sorted(raw & {"repo", "file_set"})}
    elif gid == "G-TARGET-VALIDATE":
        applicable = "target" in refs; predicate_inputs = context["candidate_refs"].get("target", {"state": "absent"})
    elif gid.startswith("G-KYM-"):
        applicable = "kym" in refs; predicate_inputs = context["candidate_refs"].get("kym", {"state": "absent"})
    elif gid == "G-TCO-MODEL-VALIDATE":
        applicable = "tco_model" in refs; predicate_inputs = context["candidate_refs"].get("tco_model", {"state": "absent"})
    elif gid == "G-TCO-COVERAGE-VALIDATE":
        applicable = "coverage" in refs; predicate_inputs = context["candidate_refs"].get("coverage", {"state": "absent"})
    elif gid in {"G-TCO-VERIFY", "G-TCO-AUDIT", "G-TCO-PACKAGE", "G-TCO-RENDER"}:
        applicable = pair; predicate_inputs = {"model": context["candidate_refs"].get("tco_model"), "coverage": context["candidate_refs"].get("coverage")}
    elif gid == "G-DASHBOARD-OBSERVE":
        applicable = app["dashboard"]["status"] == "applicable"; predicate_inputs = app["dashboard"]
    elif gid == "G-DASHBOARD-REMEDIATE":
        observation = context.get("dashboard_observation", {})
        applicable = observation.get("duplicate_count", 0) > 0
        predicate_inputs = {"duplicate_count": observation.get("duplicate_count", 0), "slice5_authorized": False}
    elif gid == "G-KB-AUDIT":
        applicable = app["kb"]["status"] == "applicable"; predicate_inputs = app["kb"]
    elif gid == "G-GIT-FRESHNESS":
        applicable = app["git"]["status"] == "applicable"; predicate_inputs = app["git"]
    elif gid == "G-REPORT-SCHEMA":
        applicable = pair; predicate_inputs = {"report_projection_requested": pair, "candidate_refs": context["candidate_refs"]}
    elif gid == "G-FINAL-PACKAGE":
        applicable = False; predicate_inputs = {"slice6_authorized": False}
    evidence = [
        {"field": "rule_ref", "value": gate["applicability_rule_id"]},
        {"field": "normalized_source_ledger", "ref": context["normalized_source_ledger_ref"], "byte_digest": context["normalized_source_ledger_byte_digest"]},
        {"field": "materialization_event_id", "value": context["materialization_event_id"]},
        {"field": "context_digest", "value": context["context_digest"]},
        {"field": "predicate_inputs", "value": predicate_inputs},
        {"field": "recompute_result", "value": "applicable" if applicable else "not_applicable"},
    ]
    return applicable, evidence


def _runtime_module_path(profile: dict[str, Any], module_ref: str) -> Path:
    if module_ref.startswith("src/kym_tco/"):
        return Path(__file__).resolve().parent / Path(module_ref).name
    if module_ref.startswith(".codex/skills/"):
        parts = Path(module_ref).parts
        skill_id = parts[2]
        relative = Path(*parts[3:])
        selected = {item["skill_id"]: Path(item["skill_dir"]) for item in profile["selected_skill_identities"]}
        if skill_id not in selected:
            raise ContractError("selected_skill_module_missing")
        return (selected[skill_id] / relative).resolve(strict=True)
    if module_ref == "src/kym_tco/post_generation_pipeline.py":
        return Path(__file__).resolve()
    raise ContractError("runtime_module_not_registered")


def _command_identity(
    workspace: Path, argv: list[str], module_ref: str, profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    external = bool(profile and profile.get("host_profile_id") == EXTERNAL_PROFILE_ID)
    if external:
        module_path = _runtime_module_path(profile, module_ref)
        module_digest = digest_bytes(module_path.read_bytes())
        safe_argv = [str(item) for item in argv]
        skill_dirs = {item["skill_dir"]: f"host-selected/{item['skill_id']}" for item in profile["selected_skill_identities"]}
        for index, item in enumerate(safe_argv):
            for path, alias in skill_dirs.items():
                if item == path:
                    item = alias
                elif item.startswith(path + os.sep):
                    item = alias + item[len(path):]
            safe_argv[index] = item
        module = {"byte_digest": module_digest}
        normalized_argv = ["python", *safe_argv[1:]]
    else:
        module = _artifact_identity(workspace, module_ref)
        normalized_argv = ["python", *argv[1:]]
    version = platform.python_version()
    return {
        "argv": normalized_argv,
        "cwd": ".",
        "executable_ref": "python",
        "executable_version": version,
        "executable_version_digest": digest_value({"implementation": platform.python_implementation(), "version": version}),
        "module_ref": module_ref,
        "module_byte_digest": module["byte_digest"],
    }


def _failure_summary(workspace: Path, stdout: bytes, stderr: bytes, returncode: int) -> str:
    for raw in (stderr, stdout):
        text = raw.decode("utf-8", errors="replace").strip().replace(str(workspace), ".")
        if not text:
            continue
        try:
            payload = json.loads(text)
            detail = payload.get("error") or payload.get("reason") or payload.get("status")
            if detail:
                return f"command exit={returncode}: {detail}"
        except json.JSONDecodeError:
            pass
        return f"command exit={returncode}: {' '.join(text.split())[:500]}"
    return f"command exit={returncode}"


def _outcome(gate: dict[str, Any], applicable: bool, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "gate_id": gate["gate_id"], "stage": gate["stage"],
        "required": bool(gate["required_when_applicable"]), "applicable": applicable,
        "applicability_rule_id": gate["applicability_rule_id"], "predicate_evidence": evidence,
        "dependencies": gate["dependencies"], "attempted": False,
        "status": "not_run" if applicable else "not_applicable",
        "command_identity": {"argv": [], "cwd": ".", "executable_ref": None, "executable_version": None, "executable_version_digest": None, "module_ref": None, "module_byte_digest": None},
        "exit_code": None, "stdout_digest": digest_bytes(b""), "stderr_digest": digest_bytes(b""),
        "artifact_refs": [], "blocker": None, "claim_ceiling": CLAIM_CEILING,
    }


def _internal(
    workspace: Path, row: dict[str, Any], status: str, refs: list[str] | None = None,
    blocker: str | None = None, profile: dict[str, Any] | None = None,
) -> None:
    module_ref = "src/kym_tco/post_generation_pipeline.py"
    row["attempted"] = status not in {"blocked", "not_applicable", "not_run"}
    row["status"] = status
    row["command_identity"] = {
        "argv": ["internal", row["gate_id"]], "cwd": ".", "executable_ref": "internal",
        "executable_version": RENDERER_VERSION, "executable_version_digest": digest_value(RENDERER_VERSION),
        "module_ref": module_ref,
        "module_byte_digest": (
            digest_bytes(_runtime_module_path(profile, module_ref).read_bytes())
            if profile and profile.get("host_profile_id") == EXTERNAL_PROFILE_ID
            else digest_bytes((workspace / module_ref).read_bytes())
        ),
    }
    row["exit_code"] = 0 if status == "passed" else (1 if status == "failed" else None)
    row["artifact_refs"] = [_artifact_identity(workspace, ref) for ref in (refs or [])]
    row["blocker"] = blocker


def _run_command(
    row: dict[str, Any], workspace: Path, argv: list[str], module_ref: str,
    expected_artifacts: list[str] | None = None, profile: dict[str, Any] | None = None,
) -> None:
    row["attempted"] = True
    row["command_identity"] = _command_identity(workspace, argv, module_ref, profile)
    if row["command_identity"]["module_byte_digest"] is None:
        row["status"] = "blocked"; row["blocker"] = f"command module unavailable: {module_ref}"
        return
    env = dict(os.environ)
    if profile and profile.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    else:
        env["PYTHONPATH"] = str((workspace / "src").resolve())
    try:
        completed = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        row["status"] = "blocked"; row["blocker"] = f"command unavailable/timeout: {exc}"
        row["stderr_digest"] = digest_bytes(str(exc).encode("utf-8"))
        row["artifact_refs"] = [_artifact_identity(workspace, ref) for ref in (expected_artifacts or [])]
        return
    row["exit_code"] = completed.returncode
    row["stdout_digest"] = digest_bytes(completed.stdout); row["stderr_digest"] = digest_bytes(completed.stderr)
    row["artifact_refs"] = [_artifact_identity(workspace, ref) for ref in (expected_artifacts or [])]
    row["status"] = "passed" if completed.returncode == 0 else "failed"
    row["blocker"] = None if completed.returncode == 0 else _failure_summary(workspace, completed.stdout, completed.stderr, completed.returncode)
    if completed.returncode == 0 and any(item["kind"] == "missing" for item in row["artifact_refs"]):
        row["status"] = "failed"; row["blocker"] = "command declared artifact missing"


def _expected_artifacts(workspace: Path, request: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = {request["normalized_source_ledger_ref"], *(item["ref"] for item in request["outputs"])}
    refs.update(artifact["ref"] for row in rows for artifact in row["artifact_refs"])
    return [_artifact_identity(workspace, ref) for ref in sorted(refs)]


def _quarantine_partial_packages(
    workspace: Path,
    request: dict[str, Any],
    rows: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    output_root: Path,
) -> dict[str, Any]:
    required_failures = [
        row["gate_id"] for row in rows
        if row["required"] and row["applicable"] and row["status"] == "failed"
    ]
    required_blockers = [
        row["gate_id"] for row in rows
        if row["required"] and row["applicable"] and row["status"] == "blocked"
    ]
    if not required_failures and not required_blockers:
        return {"status": "not_required", "manifest_ref": None, "generated_artifacts": []}

    run_id = request["pipeline_run_id"].split(":", 1)[-1]
    quarantine_root_ref = f".kym-tco/runtime/quarantine/{run_id}"
    quarantine_root = _external_workspace_path(workspace, quarantine_root_ref, "partial output quarantine")
    quarantine_root.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    conflict_ref: str | None = None
    retained: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for gate_id in ("G-KYM-PACKAGE", "G-TCO-PACKAGE"):
        row = by_id[gate_id]
        rewritten: list[dict[str, Any]] = []
        for artifact in row["artifact_refs"]:
            source_ref = artifact["ref"]
            try:
                source = _external_workspace_path(workspace, source_ref, "generated candidate package")
                output_abs = output_root.resolve()
                source_abs = source.resolve(strict=True)
            except (ContractError, OSError) as exc:
                errors.append({"gate_id": gate_id, "source_ref": source_ref, "error": str(exc)})
                retained.append({**artifact, "gate_id": gate_id, "disposition": "retained_unresolved"})
                rewritten.append(artifact)
                continue
            if artifact["kind"] != "directory" or not source.exists() or (source_abs != output_abs and output_abs not in source_abs.parents):
                rewritten.append(artifact)
                continue
            destination_ref = f"{quarantine_root_ref}/{gate_id.removeprefix('G-').lower()}"
            destination: Path | None = None
            try:
                destination = _external_workspace_path(workspace, destination_ref, "quarantined candidate package")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    conflict_ref = conflict_ref or destination_ref
                    source_identity = _artifact_identity(workspace, source_ref)
                    digest_suffix = source_identity["byte_digest"].removeprefix("sha256:")
                    base_ref = f"{quarantine_root_ref}/{gate_id.removeprefix('G-').lower()}-conflict-{digest_suffix}"
                    destination_ref = base_ref
                    destination = _external_workspace_path(workspace, destination_ref, "conflict-safe quarantined package")
                    suffix = 1
                    while destination.exists() or destination.is_symlink():
                        destination_ref = f"{base_ref}-{suffix}"
                        destination = _external_workspace_path(workspace, destination_ref, "conflict-safe quarantined package")
                        suffix += 1
                source.rename(destination)
                moved = _artifact_identity(workspace, destination_ref)
                generated.append({
                    "gate_id": gate_id,
                    "original_ref": source_ref,
                    "quarantine_ref": destination_ref,
                    "byte_digest": moved["byte_digest"],
                    "file_count": moved["file_count"],
                })
                rewritten.append(moved)
            except (ContractError, OSError, ValueError) as exc:
                if "symlink" in str(exc).casefold():
                    conflict_ref = conflict_ref or destination_ref
                errors.append({"gate_id": gate_id, "source_ref": source_ref, "error": str(exc)})
                try:
                    if destination is not None and destination_ref and destination.exists() and not source.exists():
                        try:
                            moved = _artifact_identity(workspace, destination_ref)
                        except (ContractError, OSError, ValueError):
                            # A rename preserves the already measured bytes; retain that identity
                            # even if a later read at the quarantine path is denied.
                            moved = {**artifact, "ref": destination_ref}
                        generated.append({
                            "gate_id": gate_id,
                            "original_ref": source_ref,
                            "quarantine_ref": destination_ref,
                            "byte_digest": moved["byte_digest"],
                            "file_count": moved["file_count"],
                        })
                        rewritten.append(moved)
                        continue
                except (ContractError, OSError, ValueError) as identity_exc:
                    errors.append({"gate_id": gate_id, "source_ref": destination_ref, "error": str(identity_exc)})
                try:
                    if source.exists() and source.is_dir():
                        retained.append({**_artifact_identity(workspace, source_ref), "gate_id": gate_id, "disposition": "retained_in_output"})
                    else:
                        retained.append({**artifact, "gate_id": gate_id, "disposition": "retained_identity_unverified"})
                except (ContractError, OSError, ValueError) as identity_exc:
                    errors.append({"gate_id": gate_id, "source_ref": source_ref, "error": str(identity_exc)})
                    retained.append({**artifact, "gate_id": gate_id, "disposition": "retained_identity_unverified"})
                rewritten.append(artifact)
        row["artifact_refs"] = rewritten

    manifest_ref = f"{quarantine_root_ref}/partial-output-disposition.json"
    manifest = {
        "schema": "kym_tco.partial_output_disposition.v2",
        "pipeline_run_id": request["pipeline_run_id"],
        "request_payload_digest": digest_value(request),
        "status": "cleanup_failed" if errors or retained else "quarantined" if generated else "no_partial_package_outputs",
        "reason_code": "deterministic_gate_failed" if required_failures else "required_gate_blocked",
        "failed_gate_ids": sorted(required_failures),
        "blocked_gate_ids": sorted(required_blockers),
        "conflict_ref": conflict_ref,
        "candidate_inputs_retained": [
            {"ref": item["ref"], "byte_digest": item["byte_digest"]}
            for item in request["outputs"]
        ],
        "generated_artifacts": generated,
        "retained_artifacts": retained,
        "cleanup_errors": errors,
        "recorded_at": utc_now(),
        "claim_ceiling": (
            "failed_deterministic_candidate_run_partial_outputs_retained_not_candidate"
            if retained else "failed_deterministic_candidate_run_no_candidate_package_available"
        ),
    }
    manifest_schema = json.loads(resources.files("kym_tco.schemas").joinpath("partial_output_disposition_v2.schema.json").read_text(encoding="utf-8"))
    manifest_errors = sorted(Draft202012Validator(manifest_schema).iter_errors(manifest), key=lambda error: list(error.path))
    if manifest_errors:
        raise ContractError(f"partial output disposition schema failed: {manifest_errors[0].message}")
    try:
        write_json(workspace / manifest_ref, manifest)
        manifest_raw_digest = digest_bytes((workspace / manifest_ref).read_bytes())
        by_id["G-ARTIFACT-INTEGRITY"]["artifact_refs"].append(_artifact_identity(workspace, manifest_ref))
    except (OSError, ContractError, ValueError) as exc:
        manifest["status"] = "cleanup_failed"
        manifest["cleanup_errors"].append({"gate_id": "G-ARTIFACT-INTEGRITY", "source_ref": manifest_ref, "error": str(exc)})
        manifest["claim_ceiling"] = (
            "failed_deterministic_candidate_run_partial_outputs_retained_not_candidate"
            if retained else "failed_deterministic_candidate_run_no_candidate_package_available"
        )
        manifest_ref = None
        manifest_raw_digest = None
    return {
        "status": manifest["status"],
        "manifest_ref": manifest_ref,
        "manifest_raw_digest": manifest_raw_digest,
        "manifest": manifest,
        "generated_artifacts": generated,
        "retained_artifacts": retained,
        "cleanup_errors": manifest["cleanup_errors"],
        "claim_ceiling": manifest["claim_ceiling"],
        "conflict_ref": conflict_ref,
    }


def _external_disposition_envelope(partial_output: dict[str, Any]) -> dict[str, Any]:
    manifest = partial_output.get("manifest")
    if not isinstance(manifest, dict):
        raise ContractError("external disposition manifest missing")
    return {
        "schema": "kym_tco.external_partial_output_disposition.v1",
        "status": manifest.get("status"),
        "request_payload_digest": manifest.get("request_payload_digest"),
        "pipeline_run_id": manifest.get("pipeline_run_id"),
        "claim_ceiling": manifest.get("claim_ceiling"),
        "manifest_ref": partial_output.get("manifest_ref"),
        "manifest_raw_digest": partial_output.get("manifest_raw_digest"),
        "manifest": manifest,
    }


def validate_external_disposition_envelope(
    workspace: Path,
    envelope: Any,
    *,
    expected_pipeline_run_id: str | None = None,
    expected_request_payload_digest: str | None = None,
) -> dict[str, Any]:
    """Validate the sole external disposition truth and its optional physical manifest."""
    schema = json.loads(resources.files("kym_tco.schemas").joinpath("external_partial_output_disposition_v1.schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(envelope), key=lambda error: list(error.path))
    if errors:
        raise ContractError(f"external disposition envelope schema failed: {errors[0].message}")
    manifest = envelope["manifest"]
    manifest_schema = json.loads(resources.files("kym_tco.schemas").joinpath("partial_output_disposition_v2.schema.json").read_text(encoding="utf-8"))
    manifest_errors = sorted(Draft202012Validator(manifest_schema).iter_errors(manifest), key=lambda error: list(error.path))
    if manifest_errors:
        raise ContractError(f"external disposition manifest schema failed: {manifest_errors[0].message}")
    projections = {
        "status": "status",
        "request_payload_digest": "request_payload_digest",
        "pipeline_run_id": "pipeline_run_id",
        "claim_ceiling": "claim_ceiling",
    }
    if any(envelope[key] != manifest[value] for key, value in projections.items()):
        raise ContractError("external disposition envelope projection mismatch")
    if expected_pipeline_run_id is not None and manifest["pipeline_run_id"] != expected_pipeline_run_id:
        raise ContractError("external disposition pipeline identity mismatch")
    if expected_request_payload_digest is not None and manifest["request_payload_digest"] != expected_request_payload_digest:
        raise ContractError("external disposition request identity mismatch")
    ref, raw_digest = envelope["manifest_ref"], envelope["manifest_raw_digest"]
    if (ref is None) != (raw_digest is None):
        raise ContractError("external disposition manifest ref/digest pair mismatch")
    if ref is None:
        if manifest["status"] != "cleanup_failed" or not manifest["cleanup_errors"]:
            raise ContractError("external disposition manifest fallback is not fail-only")
    else:
        path = resolved_within(workspace.resolve(), ref, "partial output disposition manifest")
        raw = path.read_bytes()
        if digest_bytes(raw) != raw_digest:
            raise ContractError("external disposition manifest raw digest mismatch")
        referenced = load_json(path)
        if referenced != manifest:
            raise ContractError("external disposition manifest content mismatch")
    if manifest["status"] == "cleanup_failed":
        if manifest["retained_artifacts"]:
            expected_claim = "failed_deterministic_candidate_run_partial_outputs_retained_not_candidate"
        else:
            if not manifest["cleanup_errors"]:
                raise ContractError("retained-empty cleanup_failed requires cleanup errors")
            expected_claim = "failed_deterministic_candidate_run_no_candidate_package_available"
        if manifest["claim_ceiling"] != expected_claim:
            raise ContractError("cleanup_failed disposition claim ceiling mismatch")
    return manifest


def validate_delivery_manifest(workspace: Path, document: dict[str, Any]) -> dict[str, Any]:
    if set(document) != {"schema", "pipeline_run_id", "artifacts", "claim_ceiling"}:
        raise ContractError("delivery manifest fields mismatch")
    if document["schema"] != "kym_tco.slice4_nonterminal_delivery_manifest.v1" or document["claim_ceiling"] != CLAIM_CEILING:
        raise ContractError("delivery manifest contract mismatch")
    if not isinstance(document["artifacts"], list) or len(document["artifacts"]) < 2:
        raise ContractError("delivery manifest artifact denominator incomplete")
    for artifact in document["artifacts"]:
        if artifact != _artifact_identity(workspace.resolve(), artifact["ref"]):
            raise ContractError("delivery manifest artifact bytes mismatch")
    return document


def validate_validation_bundle(
    document: dict[str, Any], registry: dict[str, Any], workspace: Path, request: dict[str, Any],
    profile_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    schema = json.loads(resources.files("kym_tco.schemas").joinpath("post_generation_validation_bundle_v1.schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        raise ContractError(f"validation bundle schema failed: {errors[0].message}")
    if document["bundle_digest"] != bundle_payload_digest(document):
        raise ContractError("validation bundle digest mismatch")
    context, profile, refs = _applicability_context(workspace, request, profile_override)
    if document["applicability_context"] != context:
        raise ContractError("frozen applicability context mismatch")
    if document["skill_identities"] != _skill_identities(workspace, profile):
        raise ContractError("Skill identities mismatch")
    if document["cli_identity"] != _cli_identity(workspace, profile):
        raise ContractError("CLI identity mismatch")
    if profile.get("host_profile_id") == EXTERNAL_PROFILE_ID:
        identity = document.get("pipeline_identity", {})
        validate_external_disposition_envelope(
            workspace,
            document.get("partial_output_disposition"),
            expected_pipeline_run_id=identity.get("pipeline_run_id"),
            expected_request_payload_digest=context.get("request_payload_digest"),
        )
    expected = {gate["gate_id"]: gate for gate in registry["gates"]}
    ids = [row["gate_id"] for row in document["gates"]]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ContractError("gate denominator unknown/duplicate/missing")
    by_id = {row["gate_id"]: row for row in document["gates"]}
    for row in document["gates"]:
        contract = expected[row["gate_id"]]
        applicable, evidence = _gate_applicability(contract, context, refs)
        if row["stage"] != contract["stage"] or row["dependencies"] != contract["dependencies"]:
            raise ContractError(f"gate semantic identity mismatch: {row['gate_id']}")
        if row["required"] != bool(contract["required_when_applicable"]):
            raise ContractError(f"gate required flag mismatch: {row['gate_id']}")
        if row["applicable"] != applicable or row["applicability_rule_id"] != contract["applicability_rule_id"] or row["predicate_evidence"] != evidence:
            raise ContractError(f"gate applicability recomputation mismatch: {row['gate_id']}")
        if row["status"] == "not_applicable" and (applicable or row["attempted"]):
            raise ContractError(f"gate N/A not recomputable: {row['gate_id']}")
        if not applicable and row["status"] != "not_applicable":
            raise ContractError(f"non-applicable gate status mismatch: {row['gate_id']}")
        if row["status"] in {"passed", "failed"} and not row["attempted"]:
            raise ContractError(f"gate attempted mismatch: {row['gate_id']}")
        failed_dependencies = [dep for dep in row["dependencies"] if by_id[dep]["status"] != "passed"]
        if applicable and failed_dependencies and row["status"] != "blocked":
            raise ContractError(f"gate dependency bypass: {row['gate_id']}")
        status = row["status"]
        if status == "passed" and (not row["applicable"] or not row["attempted"] or row["exit_code"] != 0 or row["blocker"] is not None):
            raise ContractError(f"gate outcome coherence mismatch: {row['gate_id']}")
        if status == "failed" and (not row["applicable"] or not row["attempted"] or not isinstance(row["exit_code"], int) or row["exit_code"] == 0 or not row["blocker"]):
            raise ContractError(f"gate outcome coherence mismatch: {row['gate_id']}")
        if status == "blocked" and (not row["applicable"] or not row["blocker"] or row["exit_code"] is not None):
            raise ContractError(f"gate outcome coherence mismatch: {row['gate_id']}")
        if status == "not_run" and (not row["applicable"] or row["attempted"] or row["exit_code"] is not None):
            raise ContractError(f"gate outcome coherence mismatch: {row['gate_id']}")
        if status == "not_applicable" and (row["applicable"] or row["attempted"] or row["exit_code"] is not None):
            raise ContractError(f"gate outcome coherence mismatch: {row['gate_id']}")
        failed_dependencies = [dep for dep in row["dependencies"] if by_id[dep]["status"] != "passed"]
        if applicable and failed_dependencies and row["status"] != "blocked":
            raise ContractError(f"gate dependency bypass: {row['gate_id']}")
        if row["status"] == "passed" and failed_dependencies:
            raise ContractError(f"passed gate dependency mismatch: {row['gate_id']}")
        command = row["command_identity"]
        if "/Users/" in json.dumps(command, ensure_ascii=False) or (command.get("module_ref") and Path(command["module_ref"]).is_absolute()):
            raise ContractError(f"command identity is not portable: {row['gate_id']}")
        if row["attempted"] and row["gate_id"] in COMMAND_MODULES:
            expected_module = COMMAND_MODULES[row["gate_id"]]
            if command.get("module_ref") != expected_module:
                raise ContractError(f"command module identity mismatch: {row['gate_id']}")
            module_identity = (
                {"byte_digest": digest_bytes(_runtime_module_path(profile, expected_module).read_bytes())}
                if profile.get("host_profile_id") == EXTERNAL_PROFILE_ID
                else _artifact_identity(workspace, expected_module)
            )
            if command.get("module_byte_digest") != module_identity["byte_digest"]:
                raise ContractError(f"command module digest mismatch: {row['gate_id']}")
        if row["attempted"] and row["gate_id"] not in COMMAND_MODULES:
            command_identity = row["command_identity"]
            if command_identity.get("executable_ref") != "internal" or command_identity.get("module_ref") != "src/kym_tco/post_generation_pipeline.py":
                raise ContractError(f"internal command identity mismatch: {row['gate_id']}")
            expected_internal_digest = (
                digest_bytes(_runtime_module_path(profile, "src/kym_tco/post_generation_pipeline.py").read_bytes())
                if profile.get("host_profile_id") == EXTERNAL_PROFILE_ID
                else digest_bytes((workspace / "src/kym_tco/post_generation_pipeline.py").read_bytes())
            )
            if command_identity.get("module_byte_digest") != expected_internal_digest:
                raise ContractError(f"internal command digest mismatch: {row['gate_id']}")
        if row["attempted"] and row["gate_id"] in COMMAND_MODULES and not command.get("argv"):
            raise ContractError(f"external command argv missing: {row['gate_id']}")
        for artifact in row["artifact_refs"]:
            if artifact != _artifact_identity(workspace, artifact["ref"]):
                raise ContractError(f"gate artifact identity mismatch: {row['gate_id']}")
    expected_artifacts = _expected_artifacts(workspace, request, document["gates"])
    if document["artifacts"] != expected_artifacts:
        raise ContractError("artifact denominator or bytes mismatch")
    integrity = by_id["G-ARTIFACT-INTEGRITY"]
    retained_partial = (
        document.get("partial_output_disposition", {}).get("status") == "cleanup_failed"
        if isinstance(document.get("partial_output_disposition"), dict) else False
    )
    expected_integrity = "failed" if retained_partial or any(item["kind"] == "missing" for item in expected_artifacts) else "passed"
    if integrity["status"] != expected_integrity:
        raise ContractError("artifact integrity gate mismatch")
    counts = {status: sum(row["status"] == status for row in document["gates"]) for status in TERMINAL}
    if document["denominator"].get("registry_total") != len(expected) or document["denominator"].get("outcome_total") != len(ids):
        raise ContractError("gate denominator total mismatch")
    if any(document["denominator"].get(status) != count for status, count in counts.items()):
        raise ContractError("gate denominator counts mismatch")
    blockers = [row["gate_id"] for row in document["gates"] if row["required"] and row["applicable"] and row["status"] != "passed"]
    if document["denominator"].get("required_blockers") != blockers:
        raise ContractError("required blocker denominator mismatch")
    failed_required = [
        row["gate_id"] for row in document["gates"]
        if row["required"] and row["applicable"] and row["status"] == "failed"
    ]
    expected_aggregate = (
        ("failed" if failed_required else "passed" if not blockers else "blocked")
        if profile.get("host_profile_id") == EXTERNAL_PROFILE_ID
        else ("passed" if not blockers else "blocked")
    )
    if document["aggregate_status"] != expected_aggregate:
        raise ContractError("aggregate readiness mismatch")
    return document


def _profile_requirements(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the one canonical profile denominator from either report shape."""
    requirements = profile.get("requirements")
    if isinstance(requirements, list) and requirements:
        return requirements
    # Older v3 reports only projected requirements below each goal.  Recover
    # the denominator deterministically for backward-compatible readers while
    # the report builder emits the explicit top-level projection.
    recovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for goal in profile.get("coverage_goals", []):
        for requirement in goal.get("requirements", []):
            requirement_id = str(requirement.get("id", ""))
            if requirement_id and requirement_id not in seen:
                seen.add(requirement_id)
                recovered.append(requirement)
    return recovered


def _runtime_blocker_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize and deduplicate runtime gaps without losing affected scope."""
    grouped: dict[str, dict[str, Any]] = {}
    for profile_item in report.get("target_profiles", []):
        profile_id = str(profile_item.get("id", ""))
        for goal in profile_item.get("coverage_goals", []):
            if goal.get("id") != "runtime" and goal.get("minimum_coverage_state") != "bounded_runtime":
                continue
            for requirement in goal.get("requirements", []):
                for item in requirement.get("missing_items", []):
                    message = str(item).strip()
                    while message.lower().startswith("[runtime]"):
                        message = message[len("[runtime]"):].strip()
                    if not message:
                        continue
                    key = message.casefold()
                    record = grouped.setdefault(
                        key,
                        {
                            "category": "runtime",
                            "code": "runtime_gap_" + str(len(grouped) + 1),
                            "message": message,
                            "profiles": [],
                            "goal_ids": [],
                            "requirement_ids": [],
                            "node_ids": [],
                        },
                    )
                    if profile_id and profile_id not in record["profiles"]:
                        record["profiles"].append(profile_id)
                    goal_id = str(goal.get("id", ""))
                    if goal_id and goal_id not in record["goal_ids"]:
                        record["goal_ids"].append(goal_id)
                    requirement_id = str(requirement.get("id", ""))
                    if requirement_id and requirement_id not in record["requirement_ids"]:
                        record["requirement_ids"].append(requirement_id)
                    for node_id in requirement.get("node_ids", []):
                        if node_id not in record["node_ids"]:
                            record["node_ids"].append(node_id)
    return list(grouped.values())


def _runtime_blockers(report: dict[str, Any]) -> list[str]:
    """Compatibility projection used by existing callers and tests."""
    return [f"[runtime] {record['message']}" for record in _runtime_blocker_records(report)]


def _render_reader(report: dict[str, Any], bundle: dict[str, Any]) -> str:
    source_schema = report.get("schema", "not_generated")
    blockers = _runtime_blockers(report)
    esc = lambda value: html.escape(str(value), quote=True)
    jdump = lambda value: html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), quote=False)
    status = bundle.get("aggregate_status", "blocked")
    status_class = "good" if status == "passed" else "bad" if status in {"failed", "blocked"} else "warn"
    disposition = bundle.get("partial_output_disposition")
    partial_warning = (
        "<div class='callout bad'><strong>部分输出不可作为候选物</strong>"
        f"<p>处置状态：{esc(disposition.get('status'))}。这些输出不得因路径存在或单个 artifact ref 而被消费；请检查本 bundle 的 required gates 和处置证据。</p>"
        f"<p>处置清单：<code>{esc(disposition.get('manifest_ref') or '未能写入')}</code>；保留项：{esc(len((disposition.get('manifest') or {}).get('retained_artifacts', [])))}</p></div>"
        if isinstance(disposition, dict) and disposition.get("status") == "cleanup_failed" else ""
    )
    denominator = bundle["denominator"]
    nodes = report.get("nodes", [])
    profiles = report.get("target_profiles", [])
    cli = bundle["cli_identity"]
    projection = report.get("projection_metadata", {}) if isinstance(report.get("projection_metadata", {}), dict) else {}
    session_rows = projection.get("session_m1_rows", []) if isinstance(projection.get("session_m1_rows", []), list) else []
    source_roots = ", ".join(str(item) for item in projection.get("source_roots", [])) or "未提供"
    source_links = " · ".join(
        f"<code>{esc(projection.get(key, ''))}</code>"
        for key in ("source_ledger_ref", "session_m1_audit_ref")
        if projection.get(key)
    ) or "未提供"
    source_closure_markup = (
        "<div class='callout info'><strong>完整来源闭合与 Dashboard 反向审计</strong>"
        f"<p>当前候选纳入 {esc(projection.get('source_file_count', 0))} 个来源单元；Dashboard {esc(projection.get('dashboard_file_count', 0))} 个、KB {esc(projection.get('kb_file_count', 0))} 个；来源根：{esc(source_roots)}。"
        f"未归位 Session：{esc(', '.join(projection.get('session_m1_unresolved', [])) or '无')}。</p>"
        f"<p>机器证据：{source_links}</p></div>"
    )
    session_audit_rows = "".join(
        "<tr>"
        f"<td><code>{esc(row.get('session_id', ''))}</code></td>"
        f"<td>{esc(row.get('status', ''))}</td>"
        f"<td>{esc(row.get('mapping_type', ''))}</td>"
        f"<td>{esc(', '.join(row.get('m1_ids', [])) or '—')}</td>"
        f"<td>{esc(row.get('reason', ''))}</td></tr>"
        for row in session_rows if isinstance(row, dict)
    )
    session_audit_markup = (
        f"<details><summary>展开 Dashboard Session → M1 归位（{len(session_rows)} 条）</summary>"
        "<div style='overflow:auto'><table><thead><tr><th>Session</th><th>Status</th><th>归位类型</th><th>M1</th><th>解释</th></tr></thead><tbody>"
        f"{session_audit_rows}</tbody></table></div></details>"
        if session_rows else ""
    )

    json_counter = 0

    def json_preview(value: Any, label: str) -> str:
        """Render structured HTML JSON preview controls, not raw JSON by default."""
        nonlocal json_counter
        json_counter += 1
        payload_id = f"json-payload-{json_counter}"
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        # Prevent a source string from terminating the application/json script.
        payload = payload.replace("<", "\\u003c").replace("&", "\\u0026")
        return (
            f"<button type='button' class='json-open' data-json-id='{payload_id}'>{esc(label)}</button>"
            f"<script type='application/json' id='{payload_id}'>{payload}</script>"
        )

    profile_node_ids: dict[str, list[str]] = {}
    profile_node_runtime_targets: dict[str, list[str]] = {}
    profile_node_goal_statuses: dict[str, list[str]] = {}
    for profile in profiles:
        for requirement in _profile_requirements(profile):
            for node_id in requirement.get("node_ids", []):
                profile_node_ids.setdefault(str(node_id), []).append(str(profile.get("id", "")))
        for goal in profile.get("coverage_goals", []):
            goal_id = str(goal.get("id", ""))
            goal_status = str(goal.get("status", "not_evaluated"))
            target_state = str(goal.get("minimum_coverage_state", "not_declared"))
            for requirement in goal.get("requirements", []):
                for node_id in requirement.get("node_ids", []):
                    node_key = str(node_id)
                    profile_node_goal_statuses.setdefault(node_key, []).append(
                        f"{profile.get('id', '')}/{goal_id}: {goal_status}"
                    )
                    if goal_id == "runtime":
                        profile_node_runtime_targets.setdefault(node_key, []).append(target_state)

    def node_state(node: dict[str, Any]) -> dict[str, Any]:
        coverage = node.get("coverage") or {}
        completeness = node.get("coverage_requirement_completeness", {}).get("overall", {})
        total = int(completeness.get("total", 0) or 0)
        known = int(completeness.get("known", 0) or 0)
        completeness_state = "complete" if total and known == total else "incomplete" if total else "not_evaluated"
        goal_statuses = profile_node_goal_statuses.get(str(node.get("id")), [])
        goal_status = (
            "Gap" if any(item.endswith(": Gap") for item in goal_statuses)
            else "Met" if goal_statuses and all(item.endswith(": Met") for item in goal_statuses)
            else "not_evaluated"
        )
        runtime_targets = sorted(set(profile_node_runtime_targets.get(str(node.get("id")), [])))
        return {
            "coverage_state": coverage.get("coverage_state", "not_evaluated"),
            "capability_lifecycle": coverage.get("capability_lifecycle_state", "not_evaluated"),
            "validation_posture": coverage.get("validation_posture", "not_validated"),
            "resolution_status": coverage.get("resolution_status", "unresolved"),
            "profile_goal_status": goal_status,
            "profile_goal_details": goal_statuses,
            "runtime_target": ", ".join(runtime_targets) or "not_declared",
            "runtime_evidence": coverage.get("coverage_state", "not_evaluated"),
            "evidence_status": coverage.get("effective_state", "not_evaluated"),
            "requirement_completeness": completeness_state,
            "requirement_count": total,
            "known_requirement_count": known,
        }

    state_labels = {
        "unknown": "未知",
        "not_started": "未开始",
        "design_only": "设计/合同",
        "schema_validator": "Schema/Validator",
        "test_bound": "测试边界",
        "bounded_runtime": "有界 Runtime",
        "integrated_runtime": "集成 Runtime",
        "not_evaluated": "未评估",
    }

    def state_label(value: Any) -> str:
        token = str(value or "unknown")
        return state_labels.get(token, token)

    def state_badge(value: Any) -> str:
        token = str(value or "unknown").replace("-", "_")
        return f"<span class='coverage-badge state-{esc(token)}'>{esc(state_label(value))}</span>"

    def list_markup(values: Any, ordered: bool = False) -> str:
        if not isinstance(values, list) or not values:
            return "<p class='empty'>无</p>"
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{esc(value)}</li>" for value in values) + f"</{tag}>"

    def object_list_markup(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return "<p class='empty'>无</p>"
        values: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                values.append(str(item))
                continue
            parts = [str(item.get("id", ""))]
            if item.get("label") and item.get("label") != item.get("id"):
                parts.append(str(item["label"]))
            for key in ("artifact_type", "category", "ref", "claim", "rationale"):
                if item.get(key):
                    parts.append(f"{key}={item[key]}")
            if item.get("source_refs"):
                parts.append("source_refs=" + ", ".join(str(ref) for ref in item["source_refs"]))
            for key in ("required", "mandatory"):
                if key in item:
                    parts.append(f"{key}={item[key]}")
            values.append("；".join(part for part in parts if part))
        return list_markup(values)

    def scope_body(node: dict[str, Any]) -> str:
        scope = node.get("scope", [])
        if isinstance(scope, dict):
            boundary = scope.get("boundary", {})
            return (
                "<h3>Boundary included</h3>"
                + list_markup(boundary.get("included", []), ordered=True)
                + "<h3>Boundary excluded</h3>"
                + list_markup(boundary.get("excluded", []), ordered=True)
            )
        return list_markup(scope, ordered=True)

    def scope_rows(node: dict[str, Any]) -> list[dict[str, str]]:
        coverage = node.get("coverage") or {}
        requirements = node.get("coverage_requirements") or []
        explicit = {
            str(row.get("requirement_id")): row
            for row in coverage.get("requirement_coverage", [])
            if isinstance(row, dict)
        }
        if requirements:
            return [
                {
                    "id": str(requirement.get("id", "")),
                    "type": str(requirement.get("category", "")),
                    "state": str(explicit.get(str(requirement.get("id", "")), {}).get("coverage_state", "unknown")),
                    "source": "coverage_requirement",
                    "notes": (
                        f"ref={requirement.get('ref', '')}; "
                        + str(explicit.get(str(requirement.get("id", "")), {}).get("notes", ""))
                    ).strip("; "),
                }
                for requirement in requirements
                if isinstance(requirement, dict)
            ]
        return []

    def profile_mapping(node_id: str) -> str:
        mapped: list[str] = []
        for profile in profiles:
            for requirement in _profile_requirements(profile):
                if node_id in requirement.get("node_ids", []):
                    mapped.append(f"{profile.get('label', profile.get('id', ''))}：{requirement.get('title', requirement.get('id', ''))}")
        return "；".join(mapped) or "无直接 Target requirement"

    def requirement_completeness(node: dict[str, Any], rows: list[dict[str, str]]) -> str:
        completeness = node.get("coverage_requirement_completeness") or {}
        overall = completeness.get("overall") if isinstance(completeness, dict) else None
        if isinstance(overall, dict) and "known" in overall and "total" in overall:
            return f"{overall.get('known', 0)}/{overall.get('total', 0)}"
        known = sum(row["state"] != "unknown" for row in rows)
        return f"{known}/{len(rows)}"

    def requirement_category_markup(node: dict[str, Any], rows: list[dict[str, str]]) -> str:
        categories = ("input", "output", "outcome", "failure", "metadata")
        completeness = node.get("coverage_requirement_completeness") or {}
        by_category = completeness.get("categories", {}) if isinstance(completeness, dict) else {}
        body: list[str] = []
        for category in categories:
            item = by_category.get(category, {}) if isinstance(by_category, dict) else {}
            if item:
                known = item.get("known", 0)
                total = item.get("total", 0)
                missing = ", ".join(item.get("missing_requirement_ids", [])) or "无"
            else:
                category_rows = [row for row in rows if row["type"] == category]
                known = sum(row["state"] != "unknown" for row in category_rows)
                total = len(category_rows)
                missing = ", ".join(row["id"] for row in category_rows if row["state"] == "unknown") or "无"
            body.append(f"<tr><td>{esc(category.title())} row completeness</td><td>{esc(known)}/{esc(total)}</td><td>{esc(missing)}</td></tr>")
        overall = requirement_completeness(node, rows)
        all_missing = ", ".join(row["id"] for row in rows if row["state"] == "unknown") or "无"
        return (
            "<table class='coverage-table'><thead><tr><th>Category</th><th>Rows present / total</th><th>Unknown row IDs</th></tr></thead><tbody>"
            f"<tr><td>Overall row completeness</td><td>{esc(overall)}</td><td>{esc(all_missing)}</td></tr>"
            + "".join(body) + "</tbody></table>"
        )

    def m1_semantics_panel(node: dict[str, Any]) -> str:
        semantics = node.get("decision_semantics") or {}
        execution = node.get("execution_semantics") or {}
        return f"""
          <dl class='semantic-kv'>
            <dt>Capability type</dt><dd>{esc(node.get('capability_type', ''))}</dd>
            <dt>Inputs</dt><dd>{object_list_markup(node.get('inputs', []))}</dd>
            <dt>Outputs</dt><dd>{object_list_markup(node.get('outputs', []))}</dd>
            <dt>Execution operation</dt><dd>{esc(execution.get('operation', 'not_declared'))}</dd>
            <dt>Materialized outputs</dt><dd>{list_markup(execution.get('materialized_output_ids', []))}</dd>
            <dt>Validated outputs</dt><dd>{list_markup(execution.get('validated_output_ids', []))}</dd>
            <dt>Unresolved outputs</dt><dd>{list_markup(execution.get('unresolved_output_ids', []))}</dd>
            <dt>Metadata requirements</dt><dd>{object_list_markup(node.get('metadata_requirements', []))}</dd>
            <dt>Coverage requirements</dt><dd>{object_list_markup(node.get('coverage_requirements', []))}</dd>
            <dt>Mandatory coverage requirements</dt><dd>{list_markup(node.get('mandatory_coverage_requirement_ids', []))}</dd>
            <dt>Claim exclusions / forbidden claims</dt><dd>{object_list_markup(node.get('claim_exclusions', []))}</dd>
            <dt>Invariants</dt><dd>{list_markup(node.get('invariants', []))}</dd>
            <dt>Witness artifacts</dt><dd>{list_markup(node.get('witness_artifact_refs', []))}</dd>
            <dt>Negative fixtures</dt><dd>{list_markup(node.get('negative_fixture_refs', []))}</dd>
            <dt>Traceability refs</dt><dd>{list_markup(node.get('traceability_refs', []))}</dd>
            <dt>Excluded outputs</dt><dd>{list_markup(node.get('excluded_outputs', []))}</dd>
          </dl>
          <h3>Decision semantics</h3>
          <dl class='semantic-kv'>
            <dt>Type</dt><dd>{esc(semantics.get('type', ''))}</dd>
            <dt>Decision input</dt><dd>{list_markup(semantics.get('decision_inputs', []))}</dd>
            <dt>Decision output</dt><dd>{list_markup(semantics.get('decision_outputs', []))}</dd>
            <dt>Terminal outcomes</dt><dd>{list_markup(semantics.get('terminal_outcomes', []))}</dd>
            <dt>Failure outcomes</dt><dd>{list_markup(semantics.get('failure_outcomes', []))}</dd>
            <dt>Decision rules</dt><dd>{list_markup(semantics.get('decision_rules', []))}</dd>
            <dt>Authority owner</dt><dd>{esc(semantics.get('authority_owner', ''))}</dd>
          </dl>"""

    def coverage_panel(node: dict[str, Any], rows: list[dict[str, str]]) -> str:
        coverage = node.get("coverage") or {}
        states = [row["state"] for row in rows]
        unknown = [row["id"] for row in rows if row["state"] == "unknown"]
        not_started = [row["id"] for row in rows if row["state"] == "not_started"]
        evidence = coverage.get("evidence", []) or []
        evidence_markup = list_markup([
            f"{item.get('kind', 'evidence')}：{item.get('ref', '')}（{item.get('note', '')}）" if isinstance(item, dict) else str(item)
            for item in evidence
        ])
        target_state = "；".join(
            f"{goal.get('id', '')}：{state_label(goal.get('minimum_coverage_state', 'not_declared'))}"
            for profile in profiles for goal in profile.get('coverage_goals', [])
            if any(node.get('id') in req.get('node_ids', []) for req in goal.get('requirements', []))
        ) or "未声明"
        table = (
            "<table class='coverage-table'><thead><tr><th>Requirement row</th><th>类型</th><th>状态</th><th>来源</th><th>说明</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{esc(row['id'])}</td><td>{esc(row['type'])}</td><td>{state_badge(row['state'])}</td><td>{esc(row['source'])}</td><td>{esc(row['notes'])}</td></tr>"
                for row in rows
            )
            + "</tbody></table>"
            if rows else "<p class='empty'>无 Requirement row。</p>"
        )
        completeness = requirement_completeness(node, rows)
        return f"""
          <dl class='coverage-kv'>
            <dt>Node kind</dt><dd>{esc(node.get('node_kind', ''))}</dd>
            <dt>Effective</dt><dd>{state_badge((coverage.get('effective_state') or coverage.get('coverage_state')))}</dd>
            <dt>Direct</dt><dd>{state_badge(coverage.get('coverage_state'))}</dd>
            <dt>Evidence peak</dt><dd>{state_badge(coverage.get('evidence_peak_state'))}</dd>
            <dt>Claim state</dt><dd>{state_badge(coverage.get('claim_state'))}</dd>
            <dt>Capability lifecycle</dt><dd>{esc(coverage.get('capability_lifecycle_state', 'not_evaluated'))}</dd>
            <dt>Validation posture</dt><dd>{esc(coverage.get('validation_posture', 'not_validated'))}</dd>
            <dt>Resolution status</dt><dd>{esc(coverage.get('resolution_status', 'unresolved'))}</dd>
            <dt>Target state</dt><dd>{esc(target_state)}</dd>
            <dt>Target mapping</dt><dd>{esc(profile_mapping(str(node.get('id', ''))))}</dd>
            <dt>Requirement row completeness</dt><dd>{esc(completeness)}</dd>
            <dt>Declared evidence</dt><dd>{esc(len(evidence))}</dd>
            <dt>Candidate evidence</dt><dd>{esc(len(coverage.get('candidate_evidence', []) or []))}</dd>
            <dt>Claim ceiling</dt><dd>{esc(coverage.get('claim_ceiling', ''))}</dd>
          </dl>
          <h3>Evidence</h3>{evidence_markup}
          <h3>Requirement row states（Coverage 跟踪状态）</h3>{table}
          <h3>Requirement row completeness by category（Coverage 跟踪完整性）</h3>{requirement_category_markup(node, rows)}
          <h3>Target Gap（未达要求）</h3>{list_markup(coverage.get('missing_coverage_requirement_ids', []))}
          <h3>Unknown Requirement rows（尚未确认）</h3>{list_markup(unknown)}
          <h3>Not-started Requirement rows（已确认未开始）</h3>{list_markup(not_started)}
          <h3>Breadth coverage（广度）</h3>{object_list_markup([f"{key}={value}" for key, value in (coverage.get('breadth_coverage', {}) or {}).items()])}
        """

    def node_detail(node: dict[str, Any]) -> str:
        coverage = node.get("coverage") or {}
        state = node_state(node)
        state_rows = "".join(
            f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>"
            for label, value in (
                ("Coverage state", state["coverage_state"]),
                ("Capability lifecycle", state["capability_lifecycle"]),
                ("Validation posture", state["validation_posture"]),
                ("Resolution status", state["resolution_status"]),
                ("Profile Goal status", state["profile_goal_status"]),
                ("Profile Goal details", "; ".join(state["profile_goal_details"]) or "not_evaluated"),
                ("Runtime target (Profile)", state["runtime_target"]),
                ("Runtime evidence", state["runtime_evidence"]),
                ("Evidence status", state["evidence_status"]),
                ("Requirement completeness", f"{state['known_requirement_count']}/{state['requirement_count']} ({state['requirement_completeness']})"),
            )
        )
        rows = scope_rows(node)
        return (
            "<div class='tsp-grid'>"
            f"<details class='tsp-branch' open><summary>状态轴与覆盖</summary><div class='branch-body'><dl class='detail-kv'>{state_rows}</dl></div></details>"
            f"<details class='tsp-branch' open><summary>Topic</summary><div class='branch-body'><p>{esc(node.get('topic', ''))}</p></div></details>"
            f"<details class='tsp-branch'><summary>Ref Info</summary><div class='branch-body'>{list_markup([f'KYM statement: {ref}' for ref in node.get('source_statement_refs', [])])}</div></details>"
            f"<details class='tsp-branch' open><summary>Scope</summary><div class='branch-body'>{scope_body(node)}</div></details>"
            f"<details class='tsp-branch' open><summary>Purpose</summary><div class='branch-body'><p>{esc(node.get('purpose', ''))}</p></div></details>"
            + (f"<details class='tsp-branch'><summary>Capability definition（能力定义）</summary><div class='branch-body'>{m1_semantics_panel(node)}</div></details>" if node.get('node_kind') == 'm1' else "")
            + f"<details class='tsp-branch'><summary>Coverage</summary><div class='branch-body'>{coverage_panel(node, rows)}</div></details>"
            + f"<details class='tsp-branch'><summary>Evidence 与 Claim ceiling</summary><div class='branch-body'><p>{esc(coverage.get('claim_ceiling', ''))}</p>{json_preview(coverage, '打开 Evidence HTML 预览')}</div></details>"
            + f"<details class='tsp-branch'><summary>完整节点数据</summary><div class='branch-body'>{json_preview(node, '打开节点 HTML 预览')}</div></details>"
            + "</div>"
        )

    by_id = {str(node.get("id")): node for node in nodes}
    child_ids = {str(child_id) for node in nodes for child_id in node.get("children", [])}

    def render_node(node_id: str, depth: int = 0) -> str:
        node = by_id[node_id]
        kind = str(node.get("node_kind", "m1")).lower()
        dom_kind = "group" if kind == "mc" else "capability" if kind == "m1" else "policy"
        state = node_state(node)
        children = "".join(render_node(str(child), depth + 1) for child in node.get("children", []) if str(child) in by_id)
        profile_ids = " ".join(sorted(set(profile_node_ids.get(node_id, []))))
        status_class = "state-bounded_runtime" if state["coverage_state"] == "bounded_runtime" else "state-design_only" if state["coverage_state"] == "design_only" else "state-unknown"
        return (
            f"<details id='node-{esc(node_id)}' class='tco-node' data-node-id='{esc(node_id)}' data-depth='{depth}' data-node-kind='{dom_kind}' data-coverage-state='{esc(state['coverage_state'])}' data-requirement-completeness='{esc(state['requirement_completeness'])}' data-profile-ids='{esc(profile_ids)}' open>"
            f"<summary><span>{esc(node.get('title', node_id))}</span><span class='summary-meta'><span class='coverage-badge {status_class}'>Coverage: {esc(state['coverage_state'])}</span><span class='coverage-badge state-unknown'>Profile Goal: {esc(state['profile_goal_status'])}</span><span class='coverage-badge state-unknown'>Validation: {esc(state['validation_posture'])}</span><span class='node-kind kind-{dom_kind}'>{esc(kind)}</span><span class='node-id'>{esc(node_id)}</span></span></summary>"
            f"{node_detail(node)}{children}</details>"
        )

    root_ids = [str(node.get("id")) for node in nodes if str(node.get("id")) not in child_ids]
    tree_html = "".join(render_node(node_id) for node_id in root_ids) or "<p class='empty'>没有可投影的 Mc/M1 节点</p>"

    skill_cards = "".join(
        "<div class='identity-card'><div class='eyebrow'>SKILL</div>"
        f"<h3>{esc(identity.get('skill_id', ''))}</h3>"
        f"<div class='big'>{esc(identity.get('distribution_version', ''))}</div>"
        f"<p class='muted'>Source revision: {esc(identity.get('source_revision', ''))} · {esc(identity.get('release_status', ''))}</p>"
        f"<div>{json_preview(identity, '查看完整 identity HTML 预览')}</div></div>"
        for identity in bundle["skill_identities"]
    )
    def profile_goal_summary(profile: dict[str, Any]) -> str:
        rows: list[str] = []
        for goal in profile.get("coverage_goals", []):
            if not isinstance(goal, dict):
                continue
            goal_status = str(goal.get("status", "not_evaluated"))
            goal_class = "good" if goal_status == "Met" else "bad" if goal_status == "Gap" else "warn"
            rows.append(
                f"<div class='profile-goal-summary'><code>{esc(goal.get('id', ''))}</code>: "
                f"<span class='state {goal_class}'>{esc(goal_status)}</span> · "
                f"Met {esc(goal.get('met_count', '—'))} / Gap {esc(goal.get('gap_count', '—'))}</div>"
            )
        return "".join(rows)

    profile_rows = "".join(
        "<tr>"
        f"<td><strong>{esc(profile.get('id', ''))}</strong><div class='muted'>{esc(profile.get('label', ''))}</div></td>"
        f"<td><strong>{len(profile.get('coverage_goals', []))}</strong>{profile_goal_summary(profile)}<div class='muted'>Goal status is separate from Coverage evidence.</div></td><td>{len(_profile_requirements(profile))}</td>"
        f"<td>{json_preview({'coverage_goals': profile.get('coverage_goals', []), 'requirements': _profile_requirements(profile)}, '查看目标与要求 HTML 预览')}</td></tr>"
        for profile in profiles
    ) or "<tr><td colspan='4' class='muted'>没有可投影的 Target Profile</td></tr>"
    runtime_gates = []
    for gate_id, result in report.get("verification_results", {}).items():
        if "agent-" not in gate_id:
            continue
        payload: Any = {}
        try:
            payload = json.loads(result.get("stdout", "{}"))
        except (TypeError, json.JSONDecodeError):
            payload = {"stdout": result.get("stdout", "")}
        runtime_gates.append({"gate_id": gate_id, "status": result.get("status"), "claim_ceiling": payload.get("claim_ceiling", ""), "command_identity": {"argv": result.get("argv", [])}, "artifact_refs": payload.get("capability_witnesses", []), "predicate_evidence": payload, "exit_code": result.get("returncode")})
    runtime_rows = "".join(
        "<div class='runtime-row'>"
        f"<div><span class='state good'>{esc(row.get('status', ''))}</span> <strong>{esc(row.get('gate_id', ''))}</strong></div>"
        f"<p class='muted'>runtime verdict is bounded witness only；{esc(row.get('claim_ceiling', ''))}</p>"
        f"{json_preview({'command_identity': row.get('command_identity', {}), 'artifact_refs': row.get('artifact_refs', []), 'predicate_evidence': row.get('predicate_evidence', {}), 'exit_code': row.get('exit_code')}, '查看 command / receipt / attestation HTML 预览')}</div>"
        for row in runtime_gates
    ) or "<p class='muted'>本次 bundle 没有匹配的 synthesis runtime gate。</p>"
    gate_rows = "".join(
        "<tr>"
        f"<td><code>{esc(row['gate_id'])}</code></td><td>{esc(row['stage'])}</td>"
        f"<td>{'是' if row['required'] else '否'}</td><td>{'是' if row['applicable'] else '否'}</td>"
        f"<td><span class='state {'good' if row['status'] == 'passed' else 'bad' if row['status'] in {'failed', 'blocked'} else 'warn'}'>{esc(row['status'])}</span></td>"
        f"<td>{esc(row.get('applicability_rule_id', ''))}</td><td>{esc(row.get('exit_code', '—'))}</td>"
        f"<td>{json_preview({'predicate_evidence': row.get('predicate_evidence', {}), 'dependencies': row.get('dependencies', []), 'command_identity': row.get('command_identity', {}), 'artifact_refs': row.get('artifact_refs', []), 'blocker': row.get('blocker'), 'claim_ceiling': row.get('claim_ceiling')}, '证据 HTML 预览')}</td></tr>"
        for row in bundle["gates"]
    )
    blocker_records = _runtime_blocker_records(report)
    blocker_html = "".join(
        f"<li><span class='blocker-category'>[{esc(record['category'])}] {esc(record['message'])}</span>"
        f"<div class='muted'>影响 Profile：{esc(', '.join(record['profiles']) or '未指定')}；Requirement：{esc(', '.join(record['requirement_ids']) or '未指定')}；M1：{esc(', '.join(record['node_ids']) or '未指定')}</div></li>"
        for record in blocker_records
    ) or "<li>当前 report 没有 runtime goal blocker。</li>"
    denominator_rows = "".join(f"<tr><th>{esc(key)}</th><td>{esc(json.dumps(value, ensure_ascii=False))}</td></tr>" for key, value in denominator.items())
    css = """
:root{--ink:#182238;--muted:#667085;--line:#dfe5ef;--brand:#3758d3;--good:#16804d;--warn:#a56300;--bad:#b42318;--shadow:0 12px 32px rgba(37,52,90,.08)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:linear-gradient(145deg,#eef3ff,#f8fafc 38%,#f5f0ff)}
.reader-layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;align-items:start}.reader-sidebar{position:sticky;top:18px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:var(--shadow)}.reader-sidebar a{display:block;padding:7px 8px;color:var(--ink);border-radius:6px;text-decoration:none}.reader-sidebar a:hover{background:#eaf1ff;color:#205cc7}.reader-content{min-width:0}.doc-section{scroll-margin-top:88px;margin-bottom:24px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0;padding:12px;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}.toolbar label{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}.toolbar select,.toolbar input,.toolbar button{border:1px solid #94a3b8;background:#fff;color:var(--ink);padding:7px 10px;border-radius:6px}.toolbar input[type=search]{min-width:220px}.toolbar button,.json-open{cursor:pointer}.filter-result-summary{margin:0 0 12px;padding:9px 12px;background:#fff;border:1px solid #cbd5e1;border-left:4px solid #0f766e;border-radius:6px;color:#134e4a;font-weight:700}.cap-tree{margin:0}.tco-node{border-left:2px dashed #0f766e;padding-left:14px;margin:0 0 10px;background:transparent;box-shadow:none;border-radius:0}.tco-node>summary{display:flex;justify-content:space-between;gap:12px;padding:9px 12px;border-radius:6px;background:#99f6e4;font-weight:700;cursor:pointer}.tco-node[data-depth='1']>summary{background:#ccfbf1}.tco-node[data-depth='2']>summary{background:#ecfeff}.tco-node[hidden]{display:none}.summary-meta{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}.node-id{color:#64748b;font-weight:400;font-size:12px}.coverage-badge{display:inline-block;min-width:72px;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:700;text-align:center;border:1px solid transparent}.state-bounded_runtime{color:#166534;background:#dcfce7;border-color:#bbf7d0}.state-design_only{color:#6b21a8;background:#f3e8ff;border-color:#e9d5ff}.state-unknown{color:#475569;background:#f1f5f9;border-color:#cbd5e1}.node-kind{display:inline-block;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid #cbd5e1;background:#fff}.kind-group{color:#334155}.kind-capability{color:#166534}.kind-policy{color:#7c2d12}.tsp-grid{margin:8px 0 12px 18px}.tsp-branch{border-left:2px dashed #0f766e;padding-left:12px;margin:8px 0}.tsp-branch>summary{width:fit-content;min-width:110px;padding:5px 8px;border-radius:5px;background:#eaf1ff;color:#174ea6;font-weight:700;cursor:pointer}.branch-body{background:#fff;border:1px solid #cbd5e1;border-radius:6px;margin:6px 0 10px 12px;padding:10px 12px;line-height:1.5;overflow:auto}.coverage-table{width:100%;border-collapse:collapse;font-size:13px}.coverage-table th,.coverage-table td{text-align:left;vertical-align:top;border-bottom:1px solid #e2e8f0;padding:7px 6px}.coverage-table th{color:#64748b;font-weight:700}.json-open{border:1px solid #8aa9e3;background:#eaf1ff;color:#174ea6;padding:5px 8px;border-radius:5px;font-size:12px}.json-open:hover{background:#dbeafe}.json-dialog{width:min(980px,92vw);max-height:86vh;border:1px solid #cbd5e1;border-radius:12px;box-shadow:0 20px 60px rgba(20,33,61,.25);padding:0}.json-dialog::backdrop{background:rgba(20,33,61,.4)}.json-dialog-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid #dfe5ef;background:#f7f9fc}.json-dialog-head strong{flex:1}.json-dialog-body{padding:14px 16px;overflow:auto;max-height:68vh}.json-tools{display:flex;gap:8px;margin-bottom:10px}.json-tools input{flex:1;padding:7px 9px;border:1px solid #94a3b8;border-radius:6px}.json-tools button{padding:7px 9px;border:1px solid #94a3b8;border-radius:6px;background:#fff}.json-tree{font:12px/1.5 "SFMono-Regular",Consolas,monospace}.json-tree details{margin:2px 0}.json-tree summary{color:#174ea6;cursor:pointer}.json-leaf{padding:2px 0 2px 20px;overflow-wrap:anywhere}.json-key{color:#7c2d12}.json-string{color:#166534}.json-number{color:#1d4ed8}.json-boolean{color:#7c3aed}.json-null{color:#64748b}.json-hidden{display:none!important}.blocker-category{display:inline-block;color:#9a5b00;background:#fff3d7;border-radius:999px;padding:2px 7px;font-weight:700}.empty{color:#667085}.tco-node.detail-visible>.tsp-grid{display:block}body.m-only .tsp-grid{display:none}body.m-only .tco-node.detail-visible>.tsp-grid{display:block}body.m-only .tco-node.detail-visible>summary{outline:2px solid #0f766e;outline-offset:2px}@media(max-width:900px){.reader-layout{grid-template-columns:1fr}.reader-sidebar{position:relative;top:0}.summary-meta{max-width:100%}}@media(max-width:600px){.reader-layout{display:block}.toolbar select,.toolbar input{width:100%;min-width:0}.toolbar label{flex:1 1 100%}}
"""
    css += ".detail-kv,.semantic-kv,.coverage-kv{display:grid;grid-template-columns:190px minmax(0,1fr);margin:0;border:1px solid #e2e8f0;border-radius:6px;overflow:hidden}.detail-kv dt,.detail-kv dd,.semantic-kv dt,.semantic-kv dd,.coverage-kv dt,.coverage-kv dd{margin:0;padding:8px 10px;border-bottom:1px solid #e2e8f0;overflow-wrap:anywhere}.detail-kv dt,.semantic-kv dt,.coverage-kv dt{background:#f7f9fc;color:#334155;font-weight:700}.detail-kv dd,.semantic-kv dd,.coverage-kv dd{background:#fff}.detail-kv ul,.detail-kv ol,.semantic-kv ul,.semantic-kv ol,.coverage-kv ul,.coverage-kv ol{margin:0;padding-left:20px}.semantic-kv,.coverage-kv{margin-bottom:12px}.coverage-panel h3{margin:16px 0 6px}.state-test_bound{color:#075985;background:#e0f2fe;border-color:#bae6fd}.state-schema_validator{color:#92400e;background:#fef3c7;border-color:#fde68a}.state-integrated_runtime{color:#065f46;background:#d1fae5;border-color:#a7f3d0}.state-not_started{color:#9a3412;background:#ffedd5;border-color:#fed7aa}.state-not_evaluated{color:#475569;background:#f1f5f9;border-color:#cbd5e1}@media(max-width:900px){.detail-kv,.semantic-kv,.coverage-kv{grid-template-columns:160px minmax(0,1fr)}}@media(max-width:600px){.detail-kv,.semantic-kv,.coverage-kv{grid-template-columns:1fr}.detail-kv dt,.semantic-kv dt,.coverage-kv dt{border-bottom:0;padding-bottom:2px}.detail-kv dd,.semantic-kv dd,.coverage-kv dd{padding-top:2px}}"
    script = r"""
function clearVisibleNodeDetails(){document.querySelectorAll('.tco-node.detail-visible').forEach(node=>node.classList.remove('detail-visible'));}
function openVisibleMContainers(){document.querySelectorAll('.tco-node').forEach(node=>{if(!node.hidden)node.open=true;});}
function collapseAll(){document.body.classList.remove('m-only');clearVisibleNodeDetails();document.querySelectorAll('details.tco-node,details.tsp-branch,details.subfeatures').forEach(detail=>detail.open=false);}
function expandAll(){document.body.classList.remove('m-only');clearVisibleNodeDetails();document.querySelectorAll('details').forEach(detail=>detail.open=true);}
function showOnlyM(){applyFilters();document.body.classList.add('m-only');clearVisibleNodeDetails();document.querySelectorAll('.tsp-branch').forEach(branch=>branch.open=false);openVisibleMContainers();}
function currentFilters(){return{coverageState:document.getElementById('coverageFilter').value,nodeKind:document.getElementById('nodeKindFilter').value,denominatorCompleteness:document.getElementById('requirementCompletenessFilter').value,profileId:document.getElementById('profileFilter').value,keyword:document.getElementById('keywordSearch').value};}
function searchTokens(keyword){return keyword.trim().toLowerCase().split(/\s+/).filter(Boolean);}
function nodeSearchText(node){const summary=node.querySelector(':scope>summary');const grid=node.querySelector(':scope>.tsp-grid');return ((summary?summary.textContent:'')+' '+(grid?grid.textContent:'')).toLowerCase();}
function keywordMatches(node,keyword){const tokens=searchTokens(keyword);if(!tokens.length)return true;if(node.dataset.nodeKind!=='capability')return false;const text=nodeSearchText(node);return tokens.every(token=>text.includes(token));}
function matchesFilters(node,filters){const structuralMatch=(!filters.coverageState||node.dataset.coverageState===filters.coverageState)&&(!filters.nodeKind||node.dataset.nodeKind===filters.nodeKind)&&(!filters.denominatorCompleteness||node.dataset.requirementCompleteness===filters.denominatorCompleteness)&&(!filters.profileId||node.dataset.profileIds.split(' ').includes(filters.profileId));return structuralMatch&&keywordMatches(node,filters.keyword);}
function updateFilterSummary(nodes){let mc=0;let m1=0;nodes.forEach(node=>{if(node.dataset.selfMatch!=='true')return;if(node.dataset.nodeKind==='group')mc+=1;if(node.dataset.nodeKind==='capability')m1+=1;});const summary=document.getElementById('filterResultSummary');if(summary)summary.textContent=`直接命中：MC ${mc} / M1 ${m1}`;}
function applyFilters(){const filters=currentFilters();const active=Object.values(filters).some(value=>value.trim?value.trim():Boolean(value));const nodes=Array.from(document.querySelectorAll('.tco-node'));nodes.forEach(node=>{node.dataset.selfMatch=(!active||matchesFilters(node,filters))?'true':'false';node.dataset.visibleMatch=node.dataset.selfMatch;});nodes.slice().reverse().forEach(node=>{const descendantMatch=Array.from(node.querySelectorAll(':scope .tco-node')).some(child=>child.dataset.visibleMatch==='true');const visible=node.dataset.selfMatch==='true'||descendantMatch;node.dataset.visibleMatch=visible?'true':'false';node.hidden=!visible;if(visible&&active)node.open=true;});updateFilterSummary(nodes);if(active)openVisibleMContainers();document.querySelectorAll('.tco-node.detail-visible').forEach(node=>{if(node.hidden)node.classList.remove('detail-visible');});}
document.querySelectorAll('.tco-node>summary').forEach(summary=>summary.addEventListener('click',event=>{if(!document.body.classList.contains('m-only'))return;event.preventDefault();event.stopPropagation();const node=summary.parentElement;const wasVisible=node.classList.contains('detail-visible');clearVisibleNodeDetails();if(!wasVisible){node.open=true;node.classList.add('detail-visible');node.querySelectorAll(':scope>.tsp-grid>.tsp-branch').forEach(branch=>branch.open=true);}}));
function jsonText(value){if(value===null)return ['null','json-null'];if(typeof value==='string')return ['"'+value+'"','json-string'];if(typeof value==='number')return [String(value),'json-number'];if(typeof value==='boolean')return [String(value),'json-boolean'];return [String(value),''];}
function renderJsonValue(key,value,depth=0){const wrapper=document.createElement('div');if(value&&typeof value==='object'){const details=document.createElement('details');details.open=depth===0;const summary=document.createElement('summary');summary.textContent=(key?key+' ':'')+(Array.isArray(value)?'[array '+value.length+']':'{object}');details.appendChild(summary);Object.entries(value).forEach(([childKey,childValue])=>details.appendChild(renderJsonValue(childKey,childValue,depth+1)));wrapper.appendChild(details);return wrapper;}const leaf=document.createElement('div');leaf.className='json-leaf';const keyNode=document.createElement('span');keyNode.className='json-key';keyNode.textContent=key?key+': ':'';const val=jsonText(value);const valueNode=document.createElement('span');valueNode.className=val[1];valueNode.textContent=val[0];leaf.append(keyNode,valueNode);return leaf;}
function openJsonPreview(button){const payload=document.getElementById(button.dataset.jsonId);const dialog=document.getElementById('json-dialog');const tree=document.getElementById('json-tree');const title=document.getElementById('json-dialog-title');const value=JSON.parse(payload.textContent);title.textContent=button.textContent;tree.replaceChildren(renderJsonValue('',value));dialog.showModal();document.getElementById('json-search').value='';}
function filterJson(){const q=document.getElementById('json-search').value.trim().toLowerCase();document.querySelectorAll('#json-tree .json-leaf').forEach(node=>{node.classList.toggle('json-hidden',Boolean(q)&&!node.textContent.toLowerCase().includes(q));});}
function jsonExpand(open){document.querySelectorAll('#json-tree details').forEach(node=>node.open=open);}
document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('.json-open').forEach(button=>button.addEventListener('click',()=>openJsonPreview(button)));document.getElementById('json-close').addEventListener('click',()=>document.getElementById('json-dialog').close());document.getElementById('json-search').addEventListener('input',filterJson);document.getElementById('json-expand').addEventListener('click',()=>jsonExpand(true));document.getElementById('json-collapse').addEventListener('click',()=>jsonExpand(false));applyFilters();});
"""
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta name='source-report-schema' content='{esc(source_schema)}'><meta name='renderer-version' content='{esc(RENDERER_VERSION)}'>
<meta name='artifact-identity' content='slice4_nonterminal'><title>KYM/TCO 全仓覆盖验证全景图</title><style>{css}</style></head><body>
<header class='hero'><div class='eyebrow'>KYM/TCO CANDIDATE VALIDATION</div><h1>KYM/TCO 全仓覆盖验证全景图</h1>
<p class='lede'>这是基于当前 repo、固定 Skills、CLI、候选 KYM/TCO/Coverage、来源 ledger 与 Dashboard 适用性结果生成的 testable coverage outline。用户可以从这里看到能力树、Target Profile、每个 M1 的覆盖状态和全部 Gate 证据。</p>
<div class='callout {'good' if status == 'passed' else ''}'><strong>{esc(status)} / candidate_not_approved</strong>　确定性 Gate 通过只说明本次输入满足机械验证；CLI/gate pass 不证明语义综合、Coverage promotion 或 approval；也不等于独立批准、promotion 或生产就绪。Renderer: <strong>{esc(RENDERER_VERSION)}</strong>。</div></header>
<div class='shell reader-layout'><nav class='reader-sidebar' aria-label='TCO 报告目录'><strong>报告目录</strong><a href='#overview'>0. 模型概览</a><a href='#inputs'>1. 版本与输入</a><a href='#tree'>2. 能力树全景</a><a href='#profiles'>3. Target Profiles</a><a href='#runtime'>4. Runtime witness</a><a href='#gates'>5. 全部验证</a><a href='#json'>6. Report JSON</a></nav><main class='reader-content'>
<section id='overview' class='metrics'><div class='metric'><div class='label'>总状态</div><div class='value {status_class}'>{esc(status)}</div></div><div class='metric'><div class='label'>Required blockers</div><div class='value {'good' if not bundle['denominator']['required_blockers'] else 'bad'}'>{len(bundle['denominator']['required_blockers'])}</div></div><div class='metric'><div class='label'>Mc/M1 节点</div><div class='value'>{len(nodes)}</div></div><div class='metric'><div class='label'>Target Profiles</div><div class='value'>{len(profiles)}</div></div><div class='metric'><div class='label'>验证 Gates</div><div class='value'>{bundle['denominator'].get('registry_total', 0)}</div></div><div class='metric'><div class='label'>CLI / Renderer</div><div class='value' style='font-size:16px'>{esc(cli.get('package_version', ''))}<br><span class='muted'>{esc(RENDERER_VERSION)}</span></div></div></section>
<section id='inputs' class='panel'><h2>版本、来源与输入闭合</h2><div class='grid'><div><h3>Skills distribution</h3><div class='grid'>{skill_cards}</div></div><div class='card'><h3>CLI identity</h3><p><strong>{esc(cli.get('package_version', ''))}</strong> · {esc(cli.get('release_status', ''))}</p><p class='muted'>{esc(cli.get('module_ref', ''))}<br>{esc(cli.get('module_byte_digest', ''))}<br>{esc(cli.get('distribution_ref', ''))}<br>{esc(cli.get('distribution_digest', ''))}</p><p class='compatibility-labels'>Artifact id · SKILL digest · Archive manifest digest · Manifest digest · Package version · Portable module ref</p>{json_preview(cli, '查看 CLI identity HTML 预览')}</div></div>{source_closure_markup}{session_audit_markup}{json_preview(bundle['applicability_context'], '查看完整 applicability HTML 预览')}</section>
<section id='tree' class='panel'><h2>Mc/M1 能力树与覆盖状态</h2><p class='muted'>保留 r004 的能力树、筛选和 M1 展开方式；新增的六轴状态、Runtime witness 与 Gate evidence 作为树节点详情展示。</p><div class='toolbar'><label>Coverage <select id='coverageFilter'><option value=''>全部</option><option value='design_only'>design_only</option><option value='test_bound'>test_bound</option><option value='bounded_runtime'>bounded_runtime</option><option value='not_evaluated'>not_evaluated</option></select></label><label>类型 <select id='nodeKindFilter'><option value=''>全部</option><option value='group'>MC</option><option value='capability'>M1</option><option value='policy'>Policy</option></select></label><label>Requirement <select id='requirementCompletenessFilter'><option value=''>全部</option><option value='complete'>complete</option><option value='incomplete'>incomplete</option><option value='not_evaluated'>not_evaluated</option></select></label><label>Profile <select id='profileFilter'><option value=''>全部</option>{''.join(f"<option value='{esc(profile.get('id', ''))}'>{esc(profile.get('id', ''))}</option>" for profile in profiles)}</select></label><input id='keywordSearch' type='search' placeholder='搜索能力、M1、状态或证据'><button type='button' onclick='applyFilters()'>筛选</button><button type='button' onclick='showOnlyM()'>只显示 M1</button><button type='button' onclick='expandAll()'>全部展开</button><button type='button' onclick='collapseAll()'>全部收起</button></div><div id='filterResultSummary' class='filter-result-summary'>直接命中：MC 0 / M1 0</div><div class='cap-tree'>{tree_html}</div></section>
<section id='profiles' class='panel'><h2>Target Profiles、Goal 与 Requirement</h2><p class='muted'>Profile 是下一阶段验收边界；每个 requirement 的缺口会保留在展开详情中。</p><div style='overflow:auto'><table><thead><tr><th>Profile</th><th>Goals</th><th>Requirements</th><th>完整明细</th></tr></thead><tbody>{profile_rows}</tbody></table></div></section>
<section id='runtime' class='panel'><h2>三个 synthesis runtime witness</h2><p>当前版本使用 governed runtime contract <strong>v1.0</strong>、allowlisted issuer <code>kym-tco.repo-local.synthesis-materializer.v1</code> 与 content-bound attestation verifier。它证明的是受治理的 repo-local candidate materialization；不证明深层语义正确性，也不创建 approval/promotion。</p>{runtime_rows}</section>
<section class='panel'>{partial_warning}<h2>Runtime blockers 与下一步</h2><ul>{blocker_html}</ul><p class='muted'>若这里仍有缺口，下一步应补充对应 requirement 的实现或独立语义审查；不要仅通过修改状态字段来消除缺口。</p></section>
<section id='gates' class='panel'><h2>完整 Gate denominator 与逐 Gate evidence</h2><div class='grid'><div class='card'><h3>Denominator</h3><table>{denominator_rows}</table></div><div class='card'><h3>Claim ceiling</h3><p>{esc(bundle.get('claim_ceiling', ''))}</p><p class='compatibility-labels'>Rule + actual evidence · Command identity · Exit/stdout/stderr</p><h3>Projection</h3><p>Report projection gate status: <strong>{esc(bundle['report_identity'].get('projection_gate_status', ''))}</strong></p></div></div><div style='overflow:auto;margin-top:18px'><table><thead><tr><th>Gate</th><th>Stage</th><th>Required</th><th>Applicable</th><th>Status</th><th>Rule</th><th>Exit</th><th>Evidence</th></tr></thead><tbody>{gate_rows}</tbody></table></div></section>
<section id='json' class='panel'><h2>Report JSON</h2>{json_preview(report, '打开完整 Report JSON HTML 预览')}<p class='footer-note'>Source report schema: <strong>{esc(source_schema)}</strong> · Renderer version: <strong>{esc(RENDERER_VERSION)}</strong> · Bundle digest: {esc(bundle.get('bundle_digest', ''))} · 本页为 candidate_not_approved reader。</p></section></main></div><dialog id='json-dialog' class='json-dialog'><div class='json-dialog-head'><strong id='json-dialog-title'>JSON HTML 预览</strong><button id='json-close' type='button'>关闭</button></div><div class='json-dialog-body'><div class='json-tools'><input id='json-search' type='search' placeholder='筛选 JSON key/value'><button id='json-expand' type='button'>展开</button><button id='json-collapse' type='button'>收起</button></div><div id='json-tree' class='json-tree'></div></div></dialog><script>{script}</script></body></html>"""


def run_post_generation_pipeline(
    workspace: Path, request: dict[str, Any], profile_override: dict[str, Any] | None = None
) -> dict[str, Any]:
    workspace = workspace.resolve()
    external = bool(profile_override and profile_override.get("host_profile_id") == EXTERNAL_PROFILE_ID)
    registry = load_gate_registry(None) if external else load_gate_registry(workspace / "governance/gate_registry_v1.json")
    context, profile, refs = _applicability_context(workspace, request, profile_override)
    _record_internal = globals()["_internal"]
    _record_command = globals()["_run_command"]

    def _internal(*args: Any, **kwargs: Any) -> None:
        _record_internal(*args, **kwargs, profile=profile)

    def _run_command(*args: Any, **kwargs: Any) -> None:
        _record_command(*args, **kwargs, profile=profile)

    ledger_ref = request["normalized_source_ledger_ref"]
    product_id = "slice4-candidate"
    for key in ("tco_model", "coverage", "kym"):
        if key in refs:
            product_id = str(load_json(workspace / refs[key]).get("product_id", product_id)); break
    output_root = workspace / ".kym-tco" / product_id / "reports" / "slice4_nonterminal" / request["pipeline_run_id"].split(":", 1)[-1]
    if external:
        _external_workspace_path(
            workspace,
            _portable(output_root, workspace),
            "external pipeline output root",
        )
    output_root.mkdir(parents=True, exist_ok=True)
    observation_ref = _portable(output_root / "dashboard-observation.json", workspace)
    if context.get("dashboard_observation", {}).get("status") != "not_applicable":
        try:
            write_json(workspace / observation_ref, observe_dashboard(workspace))
        except ContractError:
            pass
    git_ref = _portable(output_root / "git-freshness.json", workspace)
    if context["source_applicability"]["git"]["status"] == "applicable":
        write_json(workspace / git_ref, audit_git_freshness(workspace))
    verification_ref = _portable(output_root / "verification.json", workspace)
    report_ref = _portable(output_root / "candidate-report-v3.json", workspace)
    html_ref = _portable(output_root / "slice4-reader.html", workspace)
    bundle_ref = _portable(output_root / "validation-bundle.json", workspace)
    delivery_manifest_ref = _portable(output_root / "delivery-manifest.json", workspace)
    rows: list[dict[str, Any]] = []; by_id: dict[str, dict[str, Any]] = {}
    python = sys.executable
    for gate in registry["gates"]:
        applicable, evidence = _gate_applicability(gate, context, refs)
        row = _outcome(gate, applicable, evidence); rows.append(row); by_id[gate["gate_id"]] = row
        if not applicable:
            continue
        failed_dependencies = [dep for dep in gate["dependencies"] if by_id.get(dep, {}).get("status") != "passed"]
        if failed_dependencies:
            _internal(workspace, row, "blocked", blocker=f"dependencies not passed: {failed_dependencies}")
            continue
        gid = gate["gate_id"]
        if gid in {"G-HOST-PROFILE", "G-SKILL-IDENTITY", "G-SOURCE-NORMALIZE", "G-SOURCE-CLOSURE"}:
            _internal(workspace, row, "passed", [ledger_ref])
        elif gid == "G-ENV-DOCTOR":
            doctor_skill_dir = next((item["skill_dir"] for item in profile.get("selected_skill_identities", []) if item["skill_id"] == "build-tco-coverage"), ".codex/skills/build-tco-coverage")
            _run_command(row, workspace, [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "doctor", "--product-id", product_id, "--skill-dir", doctor_skill_dir, "--expected-cli-version", __version__], "src/kym_tco/tco_cli.py")
        elif gid == "G-TARGET-VALIDATE":
            target_ref = refs["target"]
            try:
                validate_document("target_fragment", load_json(workspace / target_ref), workspace / target_ref)
            except ContractError as exc:
                _internal(workspace, row, "failed", [target_ref], str(exc))
            else:
                _internal(workspace, row, "passed", [target_ref])
        elif gid == "G-KYM-VALIDATE":
            _run_command(row, workspace, [python, "-m", "kym_tco.kym_cli", "--workspace", ".", "validate", "--product-id", product_id, "--input", refs["kym"]], "src/kym_tco/kym_cli.py")
        elif gid == "G-KYM-LINT":
            _run_command(row, workspace, [python, "-m", "kym_tco.kym_cli", "--workspace", ".", "lint", "--product-id", product_id, "--input", refs["kym"]], "src/kym_tco/kym_cli.py")
        elif gid == "G-KYM-AUDIT":
            kym_audit = str(Path(next(item["skill_dir"] for item in profile["selected_skill_identities"] if item["skill_id"] == "build-kym")) / "scripts/audit_candidate.py") if external else ".codex/skills/build-kym/scripts/audit_candidate.py"
            _run_command(row, workspace, [python, kym_audit, "--workspace", ".", "--product-id", product_id, "--candidate", refs["kym"]], ".codex/skills/build-kym/scripts/audit_candidate.py")
        elif gid == "G-KYM-PACKAGE":
            package_ref = _portable(output_root / "kym-package", workspace)
            candidate_document = load_json(workspace / refs["kym"])
            base_revision = candidate_document.get("base_revision")
            base_ref = f".kym-tco/{product_id}/kym/revisions/kym-{base_revision}.json" if base_revision else ""
            package_argv = [python, "-m", "kym_tco.kym_cli", "--workspace", ".", "package-candidate", "--product-id", product_id, "--candidate", refs["kym"]]
            if base_ref and (workspace / base_ref).is_file():
                package_argv.extend(["--base", base_ref])
            package_argv.extend(["--package-dir", package_ref])
            _run_command(row, workspace, package_argv, "src/kym_tco/kym_cli.py", [package_ref])
        elif gid == "G-TCO-MODEL-VALIDATE":
            _run_command(row, workspace, [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "validate-candidate-model", "--product-id", product_id, "--model", refs["tco_model"]], "src/kym_tco/tco_cli.py")
        elif gid == "G-TCO-COVERAGE-VALIDATE":
            _run_command(row, workspace, [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "validate-candidate-coverage", "--product-id", product_id, "--model", refs["tco_model"], "--coverage", refs["coverage"]], "src/kym_tco/tco_cli.py")
        elif gid == "G-TCO-VERIFY":
            _run_command(row, workspace, [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "verify", "--product-id", product_id, "--output", verification_ref], "src/kym_tco/tco_cli.py", [verification_ref])
        elif gid == "G-TCO-AUDIT":
            tco_audit = str(Path(next(item["skill_dir"] for item in profile["selected_skill_identities"] if item["skill_id"] == "build-tco-coverage")) / "scripts/audit_candidate.py") if external else ".codex/skills/build-tco-coverage/scripts/audit_candidate.py"
            _run_command(row, workspace, [python, tco_audit, "--workspace", ".", "--product-id", product_id, "--model", refs["tco_model"], "--coverage", refs["coverage"]], ".codex/skills/build-tco-coverage/scripts/audit_candidate.py")
        elif gid == "G-TCO-PACKAGE":
            package_ref = _portable(output_root / "tco-package", workspace)
            _run_command(row, workspace, [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "package-candidate", "--product-id", product_id, "--model", refs["tco_model"], "--coverage", refs["coverage"], "--package-dir", package_ref], "src/kym_tco/tco_cli.py", [package_ref])
        elif gid == "G-TCO-RENDER":
            render_ref = _portable(output_root / "tco-render", workspace)
            render_argv = [python, "-m", "kym_tco.tco_cli", "--workspace", ".", "render-preview", "--product-id", product_id, "--model", refs["tco_model"], "--coverage", refs["coverage"]]
            if (workspace / verification_ref).is_file():
                render_argv.extend(["--verification-results", verification_ref])
            render_argv.extend(["--output-dir", render_ref])
            _run_command(row, workspace, render_argv, "src/kym_tco/tco_cli.py", [render_ref])
        elif gid == "G-DASHBOARD-OBSERVE":
            if (workspace / observation_ref).is_file():
                _internal(workspace, row, "passed", [observation_ref])
            else:
                _internal(workspace, row, "failed", blocker="Dashboard observation could not be created")
        elif gid == "G-DASHBOARD-REMEDIATE":
            decision_ref = "Dashboard/Artifacts/Stage-Plan-SP-030/S116_DashboardDuplicate_RemediationDecision.json"
            if not (workspace / decision_ref).is_file():
                _internal(workspace, row, "blocked", blocker="Dashboard duplicate requires independent governance decision before remediation")
            else:
                _internal(workspace, row, "blocked", blocker="Dashboard remediation is explicit Slice 5 write and must be invoked by the authorized remediator")
        elif gid == "G-KB-AUDIT":
            _internal(workspace, row, "passed", [ledger_ref])
        elif gid == "G-GIT-FRESHNESS":
            if (workspace / git_ref).is_file() and load_json(workspace / git_ref).get("status") != "unknown":
                _internal(workspace, row, "passed", [git_ref])
            else:
                _internal(workspace, row, "failed", [git_ref] if (workspace / git_ref).is_file() else [], "Git freshness is unknown")
        elif gid == "G-REPORT-SCHEMA":
            _internal(workspace, row, "not_run", blocker="report projection evaluated after command gates")
        elif gid in {"G-DENOMINATOR-COMPLETE", "G-ARTIFACT-INTEGRITY"}:
            _internal(workspace, row, "not_run", blocker="aggregate evaluated after command gates")

    report: dict[str, Any] = {"schema": "not_generated", "runtime_blockers": ["[runtime] model+coverage pair not available"]}
    pair_valid = "tco_model" in refs and "coverage" in refs and by_id["G-TCO-MODEL-VALIDATE"]["status"] == "passed" and by_id["G-TCO-COVERAGE-VALIDATE"]["status"] == "passed"
    report_error: str | None = None
    if pair_valid:
        verification = load_json(workspace / verification_ref) if (workspace / verification_ref).is_file() else {}
        try:
            report = build_candidate_report(workspace, product_id, workspace / refs["tco_model"], workspace / refs["coverage"], verification)
            write_json(workspace / report_ref, report)
        except ContractError as exc:
            report_error = str(exc)
    report_gate = by_id["G-REPORT-SCHEMA"]
    if report_gate["applicable"]:
        deps = [dep for dep in report_gate["dependencies"] if by_id[dep]["status"] != "passed"]
        if deps:
            _internal(workspace, report_gate, "blocked", [report_ref] if (workspace / report_ref).is_file() else [], f"dependencies not passed: {deps}; blocked-state reader only")
        elif report_error:
            _internal(workspace, report_gate, "failed", blocker=report_error)
        elif (workspace / report_ref).is_file():
            _internal(workspace, report_gate, "passed", [report_ref])
        else:
            _internal(workspace, report_gate, "failed", [report_ref], "report artifact missing")

    partial_output = (
        _quarantine_partial_packages(workspace, request, rows, by_id, output_root)
        if external else {"status": "not_applicable", "manifest_ref": None, "generated_artifacts": []}
    )
    ids = [row["gate_id"] for row in rows]
    denominator_ok = len(ids) == len(set(ids)) == len(registry["gates"]) and set(ids) == {gate["gate_id"] for gate in registry["gates"]}
    _internal(workspace, by_id["G-DENOMINATOR-COMPLETE"], "passed" if denominator_ok else "failed")
    provisional_artifacts = _expected_artifacts(workspace, request, rows)
    missing = [item["ref"] for item in provisional_artifacts if item["kind"] == "missing"]
    retained_partial = external and partial_output.get("status") == "cleanup_failed"
    integrity_refs = [item["ref"] for item in provisional_artifacts]
    integrity_refs.extend(item["ref"] for item in partial_output.get("retained_artifacts", []))
    integrity_refs = sorted(set(integrity_refs))
    integrity_blockers = []
    if missing:
        integrity_blockers.append(f"missing artifacts: {missing}")
    if retained_partial:
        integrity_blockers.append("PARTIAL_OUTPUT_NOT_CANDIDATE: retained partial output is not deliverable; inspect disposition evidence")
    _internal(
        workspace, by_id["G-ARTIFACT-INTEGRITY"],
        "failed" if missing or retained_partial else "passed",
        integrity_refs,
        "; ".join(integrity_blockers) if integrity_blockers else None,
    )
    artifacts = _expected_artifacts(workspace, request, rows)
    counts = {status: sum(row["status"] == status for row in rows) for status in TERMINAL}
    required_blockers = [row["gate_id"] for row in rows if row["required"] and row["applicable"] and row["status"] != "passed"]
    required_failures = [row["gate_id"] for row in rows if row["required"] and row["applicable"] and row["status"] == "failed"]
    aggregate = (
        ("failed" if required_failures else "passed" if not required_blockers else "blocked")
        if external else ("passed" if not required_blockers else "blocked")
    )
    bundle = {
        "schema": "kym_tco.post_generation_validation_bundle.v1",
        "pipeline_identity": {"pipeline_run_id": request["pipeline_run_id"], "materialization_event_id": request["materialization_event_id"], "artifact_identity": "slice4_nonterminal"},
        "applicability_context": context,
        "report_identity": {"source_report_schema": report.get("schema"), "renderer_version": RENDERER_VERSION, "projection_gate_status": report_gate["status"], "report_ref": report_ref if (workspace / report_ref).is_file() else None, "html_ref": html_ref},
        "skill_identities": _skill_identities(workspace, profile), "cli_identity": _cli_identity(workspace, profile),
        "gate_registry_digest": registry["registry_digest"], "gates": rows,
        "denominator": {"registry_total": len(registry["gates"]), "outcome_total": len(rows), **counts, "required_blockers": required_blockers},
        "aggregate_status": aggregate, "artifacts": artifacts,
        "partial_output_disposition": _external_disposition_envelope(partial_output) if external else None,
        "claim_ceiling": CLAIM_CEILING,
    }
    bundle["bundle_digest"] = bundle_payload_digest(bundle)
    validate_validation_bundle(bundle, registry, workspace, request, profile)
    write_json(workspace / bundle_ref, bundle)
    (workspace / html_ref).write_text(_render_reader(report, bundle), encoding="utf-8")
    delivery_manifest = {
        "schema": "kym_tco.slice4_nonterminal_delivery_manifest.v1",
        "pipeline_run_id": request["pipeline_run_id"],
        "artifacts": [_artifact_identity(workspace, ref) for ref in [bundle_ref, html_ref, *([report_ref] if (workspace / report_ref).is_file() else [])]],
        "claim_ceiling": CLAIM_CEILING,
    }
    write_json(workspace / delivery_manifest_ref, delivery_manifest)
    validate_delivery_manifest(workspace, load_json(workspace / delivery_manifest_ref))
    return {
        "status": aggregate, "bundle_ref": bundle_ref, "report_ref": bundle["report_identity"]["report_ref"],
        "html_ref": html_ref, "delivery_manifest_ref": delivery_manifest_ref,
        "delivery_manifest_digest": digest_bytes((workspace / delivery_manifest_ref).read_bytes()),
        "bundle_digest": digest_bytes((workspace / bundle_ref).read_bytes()),
        "partial_output_disposition": partial_output,
        "required_blockers": required_blockers, "claim_ceiling": CLAIM_CEILING,
    }
