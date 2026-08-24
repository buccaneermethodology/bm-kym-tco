from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .common import (
    ContractError,
    digest_bytes,
    digest_value,
    load_json,
    project_root,
    resolved_within,
    utc_now,
    write_json,
)
from .semantic_risk import audit_tco_candidate
from .tco_ops import (
    render_candidate_preview,
    run_verification,
    validate_candidate_coverage_model,
    validate_candidate_tco_model,
)
from .validation import validate_document
from . import __version__


CLAIM_CEILING = (
    "当前仓库声明输入范围内的多输入 TCO candidate 编排、自验证、候选 package 与 preview；"
    "candidate_not_approved；不声明 semantic correctness、Coverage approval、approval receipt、"
    "production readiness 或任意外部输入泛化。"
)


def _safe_workspace_file(workspace: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        path = candidate.resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ContractError(f"{label} 必须位于 workspace 内: {value}") from exc
    else:
        path = resolved_within(workspace, value, label)
    relative = path.relative_to(workspace.resolve())
    lowered = {part.lower() for part in relative.parts}
    if ".git" in lowered or "dashboard" in lowered or ("kb" in lowered and "data" in lowered):
        raise ContractError(f"{label} 不得读取治理或 git surface: {value}")
    if not path.is_file():
        raise ContractError(f"{label} 文件不存在: {value}")
    return path


def _safe_package_dir(workspace: Path, product_id: str, package_dir: str | None, package_id: str) -> Path:
    root = project_root(workspace, product_id)
    if package_dir is None:
        target = root / "reports" / "tco-multi-input" / package_id
    else:
        target = resolved_within(workspace, package_dir, "package-dir")
        if root / "reports" not in target.parents:
            raise ContractError("package-dir 必须位于 .kym-tco/<product-id>/reports/**")
    if target.exists():
        raise ContractError(f"多输入 candidate package 已存在，拒绝覆盖: {target}")
    return target


def _input_manifest(payload: dict[str, Any], product_id: str) -> list[dict[str, Any]]:
    if payload.get("schema") != "kym_tco.tco_multi_input_input_manifest.v1":
        raise ContractError("input manifest schema 必须为 kym_tco.tco_multi_input_input_manifest.v1")
    if payload.get("product_id") != product_id:
        raise ContractError("input manifest product_id 与命令不一致")
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or len(inputs) < 2:
        raise ContractError("多输入编排至少需要两个 inputs")
    seen: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict):
            raise ContractError("每个 input 必须是 object")
        input_id = item.get("input_id")
        if not isinstance(input_id, str) or not input_id or input_id in seen:
            raise ContractError(f"input_id 必须稳定且唯一: {input_id!r}")
        if not input_id[0].islower() or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in input_id):
            raise ContractError(f"input_id 必须是 lowercase stable id: {input_id!r}")
        seen.add(input_id)
        for key in ("model", "coverage"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ContractError(f"input.{key} 必须是 workspace 相对路径")
        source_refs = item.get("source_refs", [])
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) for ref in source_refs):
            raise ContractError(f"input.source_refs 必须是字符串数组: {input_id}")
        if any(Path(ref).is_absolute() for ref in source_refs):
            raise ContractError(f"input.source_refs 不得保存绝对路径: {input_id}")
    return inputs


def _model_shape(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": model.get("schema"),
        "product_id": model.get("product_id"),
        "coverage_vocabulary_digest": model.get("coverage_vocabulary_digest"),
        "decision_outcome_vocabulary_digest": model.get("decision_outcome_vocabulary_digest"),
        "node_ids": [item.get("id") for item in model.get("nodes", [])],
        "profile_shapes": [
            {
                "source_target_id": profile.get("source_target_id"),
                "requirement_ids": [item.get("id") for item in profile.get("requirements", [])],
                "source_requirement_refs": [item.get("source_requirement_ref") for item in profile.get("requirements", [])],
            }
            for profile in model.get("target_profiles", [])
        ],
    }


def _coverage_self_validation(model: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    node_ids = {item["id"]: item for item in model["nodes"]}
    records = {item["tco_node_id"]: item for item in coverage.get("records", [])}
    denominator: list[dict[str, Any]] = []
    seen_requirement_ids: set[str] = set()
    duplicate_requirement_ids: list[str] = []
    mapping_gaps: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []
    for profile in model["target_profiles"]:
        for requirement in profile["requirements"]:
            req_id = requirement["id"]
            if req_id in seen_requirement_ids:
                duplicate_requirement_ids.append(req_id)
            seen_requirement_ids.add(req_id)
            mapped_nodes = requirement.get("node_ids", [])
            mapping_ok = bool(mapped_nodes) and len(mapped_nodes) == len(set(mapped_nodes)) and all(
                node_ids.get(node_id, {}).get("node_kind") == "m1" for node_id in mapped_nodes
            )
            required_coverage_ids = set(requirement.get("required_coverage_requirement_ids", []))
            present_coverage_ids = {
                item["requirement_id"]
                for node_id in mapped_nodes
                for item in records.get(node_id, {}).get("requirement_coverage", [])
            }
            missing_coverage_ids = sorted(required_coverage_ids - present_coverage_ids)
            if not mapping_ok:
                mapping_gaps.append({"profile_id": profile["id"], "requirement_id": req_id, "node_ids": mapped_nodes})
            if not mapping_ok or missing_coverage_ids:
                coverage_gaps.append({
                    "profile_id": profile["id"],
                    "requirement_id": req_id,
                    "missing_coverage_requirement_ids": missing_coverage_ids,
                    "mapping_ok": mapping_ok,
                })
            denominator.append({
                "profile_id": profile["id"],
                "requirement_id": req_id,
                "node_ids": mapped_nodes,
                "mapping_status": "mapped" if mapping_ok else "gap",
                "coverage_status": "complete" if mapping_ok and not missing_coverage_ids else "gap",
                "missing_coverage_requirement_ids": missing_coverage_ids,
            })
    if duplicate_requirement_ids:
        for item in denominator:
            if item["requirement_id"] in duplicate_requirement_ids:
                item["mapping_status"] = "gap"
                item["coverage_status"] = "gap"
        coverage_gaps.extend(
            {"requirement_id": req_id, "reason": "duplicate_requirement_id"}
            for req_id in sorted(set(duplicate_requirement_ids))
        )
    return {
        "schema": "kym_tco.tco_multi_input_coverage_self_validation.v1",
        "denominator_count": len(denominator),
        "mapped_count": sum(item["mapping_status"] == "mapped" for item in denominator),
        "complete_count": sum(item["coverage_status"] == "complete" for item in denominator),
        "gap_count": len(coverage_gaps),
        "duplicate_requirement_ids": sorted(set(duplicate_requirement_ids)),
        "mapping_gaps": mapping_gaps,
        "coverage_gaps": coverage_gaps,
        "requirement_results": denominator,
        "verdict": "pass" if not coverage_gaps else "gap",
        "claim_ceiling": "Coverage denominator 与 mapping 的确定性候选检查；不创建 Coverage approval。",
    }


def _write_failure(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def _attach_execution_witness(
    target: Path,
    manifest: dict[str, Any],
    workspace: Path,
    product_id: str,
    input_manifest_path: Path,
) -> None:
    """Freeze an in-process runner witness without turning it into approval."""
    root = project_root(workspace, product_id)
    config_path = root / "config.json"
    config_raw = config_path.read_bytes()
    config = load_json(config_path)
    (target / "config-snapshot.json").write_bytes(config_raw)
    relative_manifest = input_manifest_path.resolve().relative_to(workspace.resolve()).as_posix()
    relative_target = target.relative_to(root).as_posix()
    package_status = manifest["state_axes"]["package_execution_status"]
    manifest["config_snapshot"] = "config-snapshot.json"
    execution = {
        "schema": "kym_tco.tco_multi_input_execution_record.v1",
        "run_id": manifest["package_id"],
        "runner_identity": {
            "engine": "in_process_shared_function",
            "module": "kym_tco.tco_multi_input",
            "entrypoint": "build_multi_input_candidate_package",
            "cli_version": __version__,
        },
        "argv": [
            "tco", "orchestrate-candidate", "--product-id", product_id,
            "--input-manifest", relative_manifest,
            "--package-dir", relative_target,
        ],
        "cwd": "workspace",
        "exit_code": 0 if package_status == "completed" else 1,
        "terminal_outcome": "candidate_package_created" if package_status == "completed" else "candidate_package_blocked",
        "stdout_path": "runner-output.json",
        "stdout_digest": "sha256:" + "0" * 64,
        "stderr_path": "runner-stderr.txt",
        "stderr_digest": "sha256:" + "0" * 64,
        "config_digest": digest_bytes(config_raw),
        "verification_command_count": len(config.get("verification_commands", [])),
        "candidate_boundary": "candidate_not_approved",
    }
    runner_output = target / "runner-output.json"
    write_json(runner_output, {
        "schema": "kym_tco.tco_multi_input_runner_output.v1",
        "package_id": manifest["package_id"],
        "package_dir": target.relative_to(workspace).as_posix(),
        "state_axes": manifest["state_axes"],
        "candidate_status": manifest["candidate_status"],
        "terminal_outcome": execution["terminal_outcome"],
        "argv": execution["argv"],
        "candidate_boundary": "candidate_not_approved",
    })
    runner_stderr = target / "runner-stderr.txt"
    runner_stderr.write_text("", encoding="utf-8")
    execution["stdout_digest"] = digest_bytes(runner_output.read_bytes())
    execution["stderr_digest"] = digest_bytes(runner_stderr.read_bytes())
    manifest["execution"] = execution


def build_multi_input_candidate_package(
    workspace: Path,
    product_id: str,
    input_manifest_path: Path,
    package_dir: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    manifest_path = _safe_workspace_file(workspace, str(input_manifest_path), "input-manifest")
    manifest_payload = load_json(manifest_path)
    if not isinstance(manifest_payload, dict):
        raise ContractError("input manifest 顶层必须是 object")
    inputs = _input_manifest(manifest_payload, product_id)
    manifest_digest = digest_bytes(manifest_path.read_bytes())
    package_id = f"multi-input-{manifest_digest.split(':', 1)[1][:16]}"
    target = _safe_package_dir(workspace, product_id, package_dir, package_id)
    target.mkdir(parents=True, exist_ok=False)
    # 把用户声明的输入清单一并冻结进 package；外部路径只作为运行时输入，
    # package 内只保留 workspace-relative 的 manifest 内容和后续 snapshot。
    (target / "input-manifest.json").write_bytes(manifest_path.read_bytes())
    input_results: list[dict[str, Any]] = []
    valid_pairs: list[tuple[dict[str, Any], Path, Path, dict[str, Any], dict[str, Any]]] = []
    shapes: list[dict[str, Any]] = []
    try:
        for item in inputs:
            input_id = item["input_id"]
            result: dict[str, Any] = {
                "input_id": input_id,
                "source_refs": item.get("source_refs", []),
                "status": "input_failed",
                "failure_class": None,
                "model": item["model"],
                "coverage": item["coverage"],
            }
            snapshot_dir = target / "input-snapshots" / input_id
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            try:
                model_path = _safe_workspace_file(workspace, item["model"], f"input {input_id} model")
                coverage_path = _safe_workspace_file(workspace, item["coverage"], f"input {input_id} coverage")
                model_raw = model_path.read_bytes()
                coverage_raw = coverage_path.read_bytes()
                snapshot_model = snapshot_dir / "model.json"
                snapshot_coverage = snapshot_dir / "coverage.json"
                snapshot_model.write_bytes(model_raw)
                snapshot_coverage.write_bytes(coverage_raw)
                result["snapshot"] = {
                    "model": snapshot_model.relative_to(target).as_posix(),
                    "coverage": snapshot_coverage.relative_to(target).as_posix(),
                    "model_file_digest": digest_bytes(model_raw),
                    "coverage_file_digest": digest_bytes(coverage_raw),
                    "model_value_digest": digest_value(load_json(model_path)),
                    "coverage_value_digest": digest_value(load_json(coverage_path)),
                }
            except Exception as exc:
                result["failure_class"] = "input_failure"
                result["error"] = str(exc)
                _write_failure(snapshot_dir / "input-result.json", result)
                input_results.append(result)
                continue
            try:
                model = validate_candidate_tco_model(workspace, product_id, model_path)
            except Exception as exc:
                result["failure_class"] = "model_failure"
                result["error"] = str(exc)
                _write_failure(snapshot_dir / "input-result.json", result)
                input_results.append(result)
                continue
            try:
                validated_model, coverage = validate_candidate_coverage_model(workspace, product_id, model_path, coverage_path)
            except Exception as exc:
                result["failure_class"] = "coverage_failure"
                result["error"] = str(exc)
                _write_failure(snapshot_dir / "input-result.json", result)
                input_results.append(result)
                continue
            result["status"] = "validated"
            result["failure_class"] = None
            result["model_value_digest"] = digest_value(validated_model)
            result["coverage_value_digest"] = digest_value(coverage)
            input_results.append(result)
            valid_pairs.append((item, model_path, coverage_path, validated_model, coverage))
            shapes.append(_model_shape(validated_model))
            _write_failure(snapshot_dir / "input-result.json", result)

        if len(valid_pairs) != len(inputs):
            failure_counts: dict[str, int] = {}
            for item in input_results:
                failure_counts[item.get("failure_class") or "none"] = failure_counts.get(item.get("failure_class") or "none", 0) + 1
            report = {
                "schema": "kym_tco.tco_multi_input_coverage_self_validation.v1",
                "denominator_count": 0,
                "mapped_count": 0,
                "complete_count": 0,
                "gap_count": 0,
                "duplicate_requirement_ids": [],
                "mapping_gaps": [],
                "coverage_gaps": [],
                "requirement_results": [],
                "verdict": "blocked",
                "failure_counts": failure_counts,
                "claim_ceiling": "输入、模型或 Coverage 失败的负向候选证据；不代表语义批准。",
            }
            write_json(target / "coverage-self-validation.json", report)
            package_manifest = _package_manifest(
                package_id, product_id, manifest_digest, input_results, None, None, report,
                {"environment_status": "ready", "package_execution_status": "blocked", "candidate_status": "candidate_not_approved", "verification_status": "not_run", "audit_status": "not_run", "approval_status": "not_requested"},
            )
            _attach_execution_witness(target, package_manifest, workspace, product_id, manifest_path)
            _publish_artifacts(target, package_manifest)
            return package_manifest | {"package_dir": str(target)}

        if len(set(json.dumps(shape, sort_keys=True) for shape in shapes)) != 1:
            for result in input_results:
                result["status"] = "failed"
                result["failure_class"] = "model_failure"
                result["error"] = "多个输入的 model/profile/node shape 不一致，无法形成统一模型分母。"
                _write_failure(
                    target / "input-snapshots" / result["input_id"] / "input-result.json",
                    result,
                )
            report = {
                "schema": "kym_tco.tco_multi_input_coverage_self_validation.v1", "denominator_count": 0,
                "mapped_count": 0, "complete_count": 0, "gap_count": 0, "duplicate_requirement_ids": [],
                "mapping_gaps": [], "coverage_gaps": [], "requirement_results": [], "verdict": "blocked",
                "failure_counts": {"model_failure": len(input_results)},
                "claim_ceiling": "模型 shape 不一致的负向候选证据；不代表语义批准。",
            }
            write_json(target / "coverage-self-validation.json", report)
            package_manifest = _package_manifest(
                package_id, product_id, manifest_digest, input_results, None, None, report,
                {"environment_status": "ready", "package_execution_status": "blocked", "candidate_status": "candidate_not_approved", "verification_status": "not_run", "audit_status": "not_run", "approval_status": "not_requested"},
            )
            _attach_execution_witness(target, package_manifest, workspace, product_id, manifest_path)
            _publish_artifacts(target, package_manifest)
            return package_manifest | {"package_dir": str(target)}

        _, _, _, base_model, base_coverage = valid_pairs[0]
        unified_model = copy.deepcopy(base_model)
        unified_model.setdefault("lineage", []).append({
            "kind": "multi_input_candidate_orchestration",
            "input_ids": [item[0]["input_id"] for item in valid_pairs],
            "input_model_digests": [item[3] and digest_value(item[3]) for item in valid_pairs],
            "input_source_kym_revisions": [item[3].get("source_kym_revision") for item in valid_pairs],
            "input_coverage_digests": [digest_value(item[4]) for item in valid_pairs],
            "candidate_boundary": "candidate_not_approved",
        })
        unified_model_path = target / "unified-model.json"
        write_json(unified_model_path, unified_model)
        unified_coverage = copy.deepcopy(base_coverage)
        root = project_root(workspace, product_id)
        unified_coverage["source_tco_model"] = unified_model_path.relative_to(root).as_posix()
        unified_coverage["source_tco_model_digest"] = digest_value(unified_model)
        unified_coverage["proposal_id"] = f"{base_coverage['proposal_id']}-{package_id}"
        unified_coverage_path = target / "unified-coverage.json"
        write_json(unified_coverage_path, unified_coverage)
        validated_unified_model, validated_unified_coverage = validate_candidate_coverage_model(
            workspace, product_id, unified_model_path, unified_coverage_path
        )
        self_validation = _coverage_self_validation(validated_unified_model, validated_unified_coverage)
        self_validation["input_count"] = len(valid_pairs)
        self_validation["input_ids"] = [item[0]["input_id"] for item in valid_pairs]
        write_json(target / "coverage-self-validation.json", self_validation)
        verification: dict[str, Any] = {}
        verification_status = "not_run"
        if self_validation["verdict"] == "pass":
            try:
                # 只运行项目配置登记的 commands；把模型中未登记的 M1 传给
                # run_verification 会把“未配置”误报为 verification failure。
                verification = run_verification(workspace, product_id, [])
                if not verification:
                    config = load_json(project_root(workspace, product_id) / "config.json")
                    reason = "coverage_mode_not_evaluated" if config.get("coverage_mode") == "not_evaluated" else "no_registered_verification_commands" if not config.get("verification_commands") else "verification_returned_no_commands"
                    verification = {
                        "status": "not_run",
                        "reason": reason,
                        "command_count": len(config.get("verification_commands", [])),
                        "candidate_boundary": "candidate_not_approved",
                    }
                    verification_status = "not_run"
                else:
                    verification_status = "passed" if all(item.get("status") == "passed" for item in verification.values()) else "failed"
            except Exception as exc:
                verification = {"multi_input": {"status": "failed", "error": str(exc)}}
                verification_status = "failed"
        else:
            verification = {"multi_input": {"status": "not_run", "reason": "coverage_gap_or_blocked"}}
        for result in input_results:
            if result.get("status") != "validated":
                continue
            result["coverage_posture"] = self_validation["verdict"]
            if self_validation["verdict"] == "gap":
                result["status"] = "coverage_gap"
                result["failure_class"] = "coverage_gap"
            elif verification_status == "failed":
                result["status"] = "failed"
                result["failure_class"] = "verification_failure"
                result["error"] = "项目登记的 verification command 未全部通过。"
            else:
                result["verification_status"] = verification_status
            input_result_path = target / "input-snapshots" / result["input_id"] / "input-result.json"
            _write_failure(input_result_path, result)
        write_json(target / "verification.json", verification)
        audit = audit_tco_candidate(workspace, product_id, unified_model_path, unified_coverage_path)
        write_json(target / "audit.json", audit)
        audit_status = "passed" if not any(item.get("severity") == "error" for item in audit.get("findings", [])) else "blocker"
        preview_paths: dict[str, str] = {}
        package_status = "completed" if self_validation["verdict"] == "pass" and verification_status in {"passed", "not_run"} and audit_status == "passed" else "blocked"
        if package_status == "completed":
            rendered = render_candidate_preview(
                workspace, product_id, unified_model_path, unified_coverage_path,
                target / "preview", verification if "status" not in verification else {},
                state_axes={"environment_status": "ready", "package_execution_status": package_status, "candidate_status": "candidate_not_approved", "verification_status": verification_status, "audit_status": audit_status, "approval_status": "not_requested"},
                integrity_path_root=target,
            )
            preview_paths = {key: path.relative_to(target).as_posix() for key, path in rendered.items()}
        else:
            write_json(target / "preview-failure.json", {"candidate_status": "candidate_not_approved", "reason": "self_validation_or_verification_or_audit_blocked", "verification_status": verification_status, "audit_status": audit_status})
        package_manifest = _package_manifest(
            package_id, product_id, manifest_digest, input_results,
            {"path": unified_model_path.relative_to(target).as_posix(), "digest": digest_value(unified_model)},
            {"path": unified_coverage_path.relative_to(target).as_posix(), "digest": digest_value(unified_coverage)},
            self_validation,
            {"environment_status": "ready", "package_execution_status": package_status, "candidate_status": "candidate_not_approved", "verification_status": verification_status, "audit_status": audit_status, "approval_status": "not_requested"},
            verification=verification,
            audit=audit,
            preview=preview_paths,
        )
        _attach_execution_witness(target, package_manifest, workspace, product_id, manifest_path)
        _publish_artifacts(target, package_manifest)
        return package_manifest | {"package_dir": str(target)}
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _package_manifest(
    package_id: str,
    product_id: str,
    input_manifest_digest: str,
    inputs: list[dict[str, Any]],
    unified_model: dict[str, Any] | None,
    unified_coverage: dict[str, Any] | None,
    self_validation: dict[str, Any],
    state_axes: dict[str, str],
    *,
    verification: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
    preview: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "kym_tco.tco_multi_input_package_manifest.v1",
        "package_id": package_id,
        "product_id": product_id,
        "created_at": utc_now(),
        "candidate_status": "candidate_not_approved",
        "input_manifest_digest": input_manifest_digest,
        "inputs": inputs,
        "unified_model": unified_model,
        "unified_coverage": unified_coverage,
        "self_validation": self_validation,
        "verification": verification or {},
        "audit": audit or {},
        "preview": preview or {},
        "input_manifest": "input-manifest.json",
        "state_axes": state_axes,
        "approval_boundary": "candidate_not_approved；编排、自验证、package 和 preview 不创建 approval/receipt，不提升 Coverage。",
        "claim_ceiling": CLAIM_CEILING,
        "artifact_index": "artifact-index.json",
    }


def _publish_artifacts(target: Path, manifest: dict[str, Any]) -> None:
    manifest_path = target / "multi-input-manifest.json"
    write_json(manifest_path, manifest)
    artifacts: list[dict[str, Any]] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name in {"artifact-index.json", "multi-input-manifest.json"}:
            continue
        artifacts.append({
            "path": path.relative_to(target).as_posix(),
            "digest": digest_bytes(path.read_bytes()),
            "candidate_boundary": "candidate_not_approved",
        })
    index = {
        "schema": "kym_tco.tco_multi_input_artifact_index.v1",
        "package_id": manifest["package_id"],
        "package_result": "candidate_package_ready" if manifest["state_axes"]["package_execution_status"] == "completed" else "candidate_package_blocked",
        "artifacts": artifacts,
        "claim_ceiling": CLAIM_CEILING,
    }
    write_json(target / "artifact-index.json", index)
    manifest["artifact_index_digest"] = digest_bytes((target / "artifact-index.json").read_bytes())
    write_json(manifest_path, manifest)
    validate_document("tco_multi_input_package_manifest", manifest)
