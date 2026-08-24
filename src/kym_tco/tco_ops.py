from __future__ import annotations

import copy
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .approval_authorization import validate_system_authorization
from .capability_claims import audit_runtime_capability_witnesses, validate_execution_semantics
from .common import ContractError, digest_value, load_json, project_root, resolved_within, utc_now, write_json
from .reader_language import audit_reader_language
from .evidence_hub import build_integrity_report, json_bytes as evidence_json_bytes
from .kym_ops import (
    persist_approval_receipt,
    semantic_validate_kym,
    source_closure_digest,
    verify_receipt,
)
from .validation import validate_document
from .workspace import DEFAULT_DECISION_OUTCOME_VOCABULARY, load_config, state_rank


PATH_EVIDENCE_KINDS = {"code", "schema", "test", "bdd", "document"}
EFFECT_STATUS_VOCABULARY_REF = "goal_effect_status_v1"
EFFECT_STATUS_VALUES = {"not_executed", "incomplete", "attempted_failed", "achieved"}
COVERAGE_REQUIREMENT_CATEGORIES = {"input", "output", "outcome", "failure", "metadata"}
V4_FORBIDDEN_CONTEXT_FREE_OUTCOMES = {"reject", "rejected"}
V4_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
V4_REQUIREMENT_CATEGORIES = ("input", "output", "outcome", "failure", "metadata")
CAPABILITY_LIFECYCLE_STATES = ("draft", "contract_defined", "implemented", "maintained", "deprecated")
LEGACY_CAPABILITY_LIFECYCLE_STATES = ("unresolved", "contract_defined", "implemented")
VALIDATION_POSTURES = ("not_validated", "producer_checked", "independently_validated")
RESOLUTION_STATUSES = ("resolved", "unresolved", "disputed")
V4_INPUT_ONLY_TOKENS = {"candidate_input"}
V4_OUTPUT_ONLY_ARTIFACT_TOKENS = {
    "p06assessment",
    "p06_assessment",
    "p06validationreport",
    "p06_validation_report",
    "p06decisionrecord",
    "p06_decision_record",
}


def _canonical_token(value: str) -> str:
    return re.sub(r"[-_\s]+", "_", value.strip().lower())


def _require_unique_values(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ContractError(f"{label} 不得重复")


def _require_no_canonical_collisions(values: list[str], label: str) -> None:
    by_canonical: dict[str, list[str]] = {}
    for value in values:
        by_canonical.setdefault(_canonical_token(value), []).append(value)
    collisions = {
        key: sorted(set(raw_values))
        for key, raw_values in by_canonical.items()
        if len(raw_values) > 1
    }
    if collisions:
        raise ContractError(f"{label} 存在 canonical token collision: {collisions}")


def _require_no_canonical_alias_collisions(values: list[str], label: str) -> None:
    by_canonical: dict[str, set[str]] = {}
    for value in values:
        by_canonical.setdefault(_canonical_token(value), set()).add(value)
    collisions = {
        key: sorted(raw_values)
        for key, raw_values in by_canonical.items()
        if len(raw_values) > 1
    }
    if collisions:
        raise ContractError(f"{label} 存在 canonical token alias collision: {collisions}")


def _object_ids(items: list[dict[str, Any]], label: str) -> list[str]:
    ids = [item["id"] for item in items]
    _require_unique_values(ids, f"{label} id")
    _require_no_canonical_alias_collisions(ids, f"{label} id")
    return ids


def _require_canonical_id_values(values: list[str], label: str) -> None:
    bad = sorted(
        value
        for value in values
        if not V4_ID_RE.fullmatch(value) or _canonical_token(value) != value
    )
    if bad:
        raise ContractError(f"{label} 必须使用 canonical ID token: {bad}")


def _validate_lifecycle_contract(vocabulary: list[str], *, strict_v4: bool) -> None:
    expected = set(CAPABILITY_LIFECYCLE_STATES if strict_v4 else LEGACY_CAPABILITY_LIFECYCLE_STATES)
    if set(vocabulary) != expected:
        label = "TCO v4" if strict_v4 else "legacy TCO"
        raise ContractError(f"{label} capability_lifecycle_vocabulary 必须使用正交 lifecycle 词表: {sorted(expected)}")


def _validate_validation_posture(value: str, label: str) -> None:
    if value not in VALIDATION_POSTURES:
        raise ContractError(f"{label} validation_posture 必须是 {list(VALIDATION_POSTURES)}")


def _validate_resolution_status(value: str, label: str) -> None:
    if value not in RESOLUTION_STATUSES:
        raise ContractError(f"{label} resolution_status 必须是 {list(RESOLUTION_STATUSES)}")


def _validate_v4_lifecycle_record(record: dict[str, Any], node_id: str) -> None:
    lifecycle = record["capability_lifecycle_state"]
    if lifecycle not in CAPABILITY_LIFECYCLE_STATES:
        raise ContractError(
            "TCO v4 capability_lifecycle_state 只能使用 draft/contract_defined/implemented/maintained/deprecated；"
            f"不得混入 Coverage、breadth、validation 或 resolution status: {node_id}: {lifecycle}"
        )
    _validate_validation_posture(record["validation_posture"], f"TCO v4 {node_id}")
    _validate_resolution_status(record["resolution_status"], f"TCO v4 {node_id}")
    if lifecycle == "maintained":
        missing = [
            key
            for key in ("maintained_registration_ref", "lifecycle_owner", "continuous_gate_ref")
            if not str(record.get(key, "")).strip()
        ]
        if missing:
            raise ContractError(
                f"TCO v4 maintained lifecycle 需要 maintained registration、owner 与持续 gate: {node_id}: {missing}"
            )
    if lifecycle == "deprecated" and not (
        str(record.get("replacement_ref", "")).strip()
        or str(record.get("sunset_ref", "")).strip()
    ):
        raise ContractError(f"TCO v4 deprecated lifecycle 需要 replacement_ref 或 sunset_ref: {node_id}")


def _goal_required_validation_posture(goal: dict[str, Any]) -> str:
    return goal.get("required_validation_posture", "not_validated")


def _require_v4_profile_goal_contract(goals: dict[str, dict[str, Any]], count: int) -> None:
    if len(goals) != count or set(goals) not in (
        {"contract", "implementation"},
        {"contract", "implementation", "runtime"},
    ):
        raise ContractError("Profile 必须声明 contract/implementation，并且只能可选增加 runtime goal")
    if (
        goals["contract"]["minimum_coverage_state"] != "design_only"
        or goals["contract"]["minimum_lifecycle_state"] != "contract_defined"
        or _goal_required_validation_posture(goals["contract"]) != "producer_checked"
        or goals["implementation"]["minimum_lifecycle_state"] != "implemented"
        or _goal_required_validation_posture(goals["implementation"]) != "independently_validated"
    ):
        raise ContractError("Profile contract/implementation goal 的 coverage、lifecycle 或 validation posture 无效")
    if "runtime" not in goals:
        if goals["implementation"]["minimum_coverage_state"] != "bounded_runtime":
            raise ContractError("未声明 runtime goal 时 implementation 必须保持 bounded_runtime 阈值")
        return
    if (
        goals["implementation"]["minimum_coverage_state"] != "test_bound"
        or goals["runtime"]["minimum_coverage_state"] != "bounded_runtime"
        or goals["runtime"]["minimum_lifecycle_state"] != "implemented"
        or _goal_required_validation_posture(goals["runtime"]) != "independently_validated"
    ):
        raise ContractError("三目标 Profile 必须分离 test_bound implementation 与 bounded_runtime runtime")


def _require_no_input_only_outcomes(values: list[str], label: str) -> None:
    bad = sorted(value for value in values if _canonical_token(value) in V4_INPUT_ONLY_TOKENS)
    if bad:
        raise ContractError(f"{label} 不得把 input-only token 建模为 outcome/failure: {bad}")


def _metadata_artifact_token_hits(items: list[dict[str, Any]]) -> list[str]:
    hits: list[str] = []
    for item in items:
        for key in ("id", "label"):
            value = str(item.get(key, ""))
            if _canonical_token(value) in V4_OUTPUT_ONLY_ARTIFACT_TOKENS:
                hits.append(value)
    return sorted(set(hits))


def _validate_v4_coverage_requirement_refs(
    node_id: str,
    requirements: list[dict[str, Any]],
    *,
    input_ids: list[str] | None = None,
    output_ids: list[str] | None = None,
    terminal_outcomes: list[str] | None = None,
    failure_outcomes: list[str] | None = None,
    metadata_ids: list[str] | None = None,
    policy_node: bool = False,
) -> None:
    if policy_node:
        for requirement in requirements:
            ref = requirement["ref"]
            if requirement["category"] != "metadata" or not ref.startswith("policy_requirements."):
                raise ContractError(f"TCO v4 policy coverage_requirements 只能是 metadata policy requirement ref: {node_id}: {requirement['id']} -> {ref}")
            target = ref.removeprefix("policy_requirements.")
            if requirement["id"] != f"metadata.{target}":
                raise ContractError(f"TCO v4 policy coverage requirement id/ref 不一致: {node_id}: {requirement['id']} -> {ref}")
        return

    inputs = set(input_ids or [])
    outputs = set(output_ids or [])
    terminal = set(terminal_outcomes or [])
    failure = set(failure_outcomes or [])
    metadata = set(metadata_ids or [])
    for requirement in requirements:
        ref = requirement["ref"]
        category = requirement["category"]
        if category == "input":
            expected_prefix = "inputs."
            target = ref.removeprefix(expected_prefix)
            valid = ref.startswith(expected_prefix) and target in inputs
        elif category == "output":
            expected_prefix = "outputs."
            target = ref.removeprefix(expected_prefix)
            valid = ref.startswith(expected_prefix) and target in outputs
        elif category == "outcome":
            expected_prefix = "decision_semantics.terminal_outcomes."
            target = ref.removeprefix(expected_prefix)
            valid = ref.startswith(expected_prefix) and target in terminal
            if valid:
                _require_no_input_only_outcomes([target], f"TCO v4 {node_id} coverage outcome requirement")
        elif category == "failure":
            expected_prefix = "decision_semantics.failure_outcomes."
            target = ref.removeprefix(expected_prefix)
            valid = ref.startswith(expected_prefix) and target in failure
            if valid:
                _require_no_input_only_outcomes([target], f"TCO v4 {node_id} coverage failure requirement")
        elif category == "metadata":
            expected_prefix = "metadata_requirements."
            target = ref.removeprefix(expected_prefix)
            valid = ref.startswith(expected_prefix) and target in metadata
            if valid and _canonical_token(target) in V4_OUTPUT_ONLY_ARTIFACT_TOKENS:
                raise ContractError(f"TCO v4 P06 artifact output 不得作为 metadata coverage requirement: {node_id}: {requirement['id']} -> {ref}")
        else:
            valid = False
        if not valid:
            raise ContractError(f"TCO v4 coverage requirement ref 与 category 不一致或引用不存在: {node_id}: {requirement['id']} -> {ref}")


def _decision_aliases(decision_vocabulary: dict[str, Any]) -> set[str]:
    return {
        _canonical_token(value)
        for value in decision_vocabulary.get("forbidden_aliases", [])
    } | {_canonical_token(value) for value in V4_FORBIDDEN_CONTEXT_FREE_OUTCOMES}


def _nodes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = document["nodes"]
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ContractError("TCO node id 必须唯一")
    return result


def _validate_graph(document: dict[str, Any]) -> None:
    nodes = _nodes(document)
    if document["root"] not in nodes:
        raise ContractError("TCO root 不存在")
    parent_count = {node_id: 0 for node_id in nodes}
    for node in nodes.values():
        children = node["children"]
        if len(children) != len(set(children)):
            raise ContractError(f"TCO node 存在重复 child: {node['id']}")
        for child in children:
            if child not in nodes:
                raise ContractError(f"TCO node {node['id']} 引用不存在 child: {child}")
            parent_count[child] += 1
    if parent_count[document["root"]] != 0:
        raise ContractError("TCO root 不得有 parent")
    multi_parent = sorted(node_id for node_id, count in parent_count.items() if count > 1)
    if multi_parent:
        raise ContractError(f"TCO node 不得有多个 parent: {multi_parent}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ContractError(f"TCO graph 存在 cycle: {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for child in nodes[node_id]["children"]:
            visit(child)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(document["root"])
    unreachable = sorted(set(nodes) - visited)
    if unreachable:
        raise ContractError(f"TCO graph 存在不可达 nodes: {unreachable}")


def validate_tco_model(workspace: Path, product_id: str, model_path: Path) -> dict[str, Any]:
    document = load_json(model_path)
    if document.get("schema") in {"kym_tco.tco_model.v2", "kym_tco.tco_model.v3", "kym_tco.tco_model.v4"}:
        raise ContractError("TCO candidate_not_approved 必须使用 validate-candidate-model；普通 validate-model 不创建或读取批准态")
    return _validate_tco_model(workspace, product_id, model_path, require_receipts=True)


def validate_candidate_tco_model(workspace: Path, product_id: str, model_path: Path) -> dict[str, Any]:
    document = load_json(model_path)
    if document.get("schema") == "kym_tco.tco_model.v2":
        return _validate_tco_v2_model(workspace, product_id, model_path)
    if document.get("schema") == "kym_tco.tco_model.v3":
        return _validate_tco_v3_model(workspace, product_id, model_path)
    if document.get("schema") == "kym_tco.tco_model.v4":
        return _validate_tco_v4_model(workspace, product_id, model_path)
    return _validate_tco_model(workspace, product_id, model_path, require_receipts=False)


def _validate_tco_v2_model(workspace: Path, product_id: str, model_path: Path) -> dict[str, Any]:
    root = project_root(workspace, product_id); config = load_config(root); document = load_json(model_path)
    validate_document("tco_model_v2", document, model_path)
    if document["product_id"] != product_id or document["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO v2 product 或 coverage vocabulary 不一致")
    decision_vocabulary = config["decision_outcome_vocabulary"]
    if document["decision_outcome_vocabulary_digest"] != config["decision_outcome_vocabulary_digest"]:
        raise ContractError("TCO v2 decision outcome vocabulary digest 已漂移")
    _validate_graph(document); nodes = _nodes(document)
    kym_path = root / "kym" / "revisions" / f"kym-{document['source_kym_revision']}.json"
    kym = load_json(kym_path)
    semantic_validate_kym(kym, root)
    statement_ids = {item["id"] for item in kym["statements"]}
    target_by_id = {item["id"]: item for item in kym["target_profiles"]}
    if any(item["node_kind"] == "mc" and not item["children"] for item in nodes.values()):
        raise ContractError("mc 必须包含 children")
    for item in nodes.values():
        missing_statement_refs = sorted(set(item["source_statement_refs"]) - statement_ids)
        if missing_statement_refs:
            raise ContractError(f"TCO v2 node 引用不存在 KYM statements: {item['id']}: {missing_statement_refs}")
        if item["node_kind"] != "mc" and not item["source_statement_refs"]:
            raise ContractError(f"TCO v2 非 Mc node 必须引用 KYM statements: {item['id']}")
        if len(item["scope"]) != len(set(item["scope"])):
            raise ContractError(f"TCO v2 node scope 不得重复: {item['id']}")
        if set(item["scope_types"]) != set(item["scope"]):
            raise ContractError(f"TCO v2 scope_types 必须精确覆盖所有 Scope: {item['id']}")
        mandatory_scope = item.get("mandatory_scope_items", item["scope"])
        if len(mandatory_scope) != len(set(mandatory_scope)) or set(mandatory_scope) - set(item["scope"]):
            raise ContractError(f"TCO v2 mandatory_scope_items 必须是无重复的 Scope 子集: {item['id']}")
        if item["node_kind"] == "m1":
            required = {"capability_type", "inputs", "outputs", "decision_semantics", "invariants", "witness_artifact_refs", "negative_fixture_refs", "traceability_refs", "effect_status"}
            if required - set(item) or item["children"]:
                raise ContractError(f"m1 必须是完整的无 children 独立能力: {item['id']}")
            decision = item["decision_semantics"]
            outputs = decision["decision_output"]
            terminal_outputs = decision["terminal_decision_outputs"]
            excluded = item.get("excluded_outputs", [])
            if len(outputs) != len(set(outputs)) or len(terminal_outputs) != len(set(terminal_outputs)) or len(excluded) != len(set(excluded)):
                raise ContractError(f"TCO v2 M1 decision/excluded outputs 不得重复: {item['id']}")
            if set(terminal_outputs) - set(outputs):
                raise ContractError(f"TCO v2 terminal_decision_outputs 必须属于 decision_output: {item['id']}")
            noncanonical = sorted(set(terminal_outputs) - set(decision_vocabulary["outcomes"]))
            if noncanonical:
                raise ContractError(f"TCO v2 terminal decision output 必须使用 canonical vocabulary: {item['id']}: {noncanonical}")
            aliases = sorted(
                output
                for output in outputs
                if re.sub(r"[-_\\s]+", " ", output.strip()) in decision_vocabulary["forbidden_aliases"]
            )
            if aliases:
                raise ContractError(f"TCO v2 decision_output 不得使用终态同义词，改用 canonical value: {item['id']}: {aliases}")
            if set(excluded) - set(outputs) or set(excluded) & set(item["scope"]):
                raise ContractError(f"TCO v2 excluded_outputs 必须是未映射 decision_output，且不得与 Scope 重叠: {item['id']}")
            unmapped = sorted(set(outputs) - set(item["scope"]) - set(excluded))
            if unmapped:
                raise ContractError(f"TCO v2 decision_output 必须映射到 Scope 或 excluded_outputs: {item['id']}: {unmapped}")
            misclassified = sorted(output for output in outputs if output in item["scope"] and item["scope_types"][output] not in {"outcome", "failure"})
            if misclassified:
                raise ContractError(f"TCO v2 decision_output 的 Scope 类型必须为 outcome 或 failure: {item['id']}: {misclassified}")
        if item["node_kind"] == "policy" and any(key in item for key in ("inputs", "outputs", "capability_type")):
            raise ContractError(f"policy 不得冒充输入输出能力: {item['id']}")
    seen_targets: set[str] = set()
    for profile in document["target_profiles"]:
        if profile["source_target_id"] in seen_targets:
            raise ContractError("同一 source_target_id 只能有一个稳定 Profile")
        if re.search(r"(?:^|[-_\s])r\d{3,}|revision|\bview\b", profile["id"] + " " + profile["label"], re.IGNORECASE):
            raise ContractError("Profile 不得使用 revision/view 命名；历史视图必须进入 lineage")
        seen_targets.add(profile["source_target_id"])
        if profile["source_target_id"] not in target_by_id:
            raise ContractError("Profile source_target_id 必须引用 KYM candidate Target")
        goals = {goal["id"]: goal for goal in profile["coverage_goals"]}
        if len(goals) != len(profile["coverage_goals"]) or set(goals) != {"contract", "implementation"} or goals["contract"]["minimum_coverage_state"] != "design_only" or goals["contract"]["minimum_lifecycle_state"] != "contract_defined" or goals["implementation"]["minimum_coverage_state"] != "bounded_runtime" or goals["implementation"]["minimum_lifecycle_state"] != "implemented":
            raise ContractError("Profile 必须区分 contract 与 bounded_runtime implementation goals")
        requirement_ids: set[str] = set()
        mapped_source_requirements: set[str] = set()
        for requirement in profile["requirements"]:
            if requirement["id"] in requirement_ids:
                raise ContractError(f"Profile requirement id 重复: {requirement['id']}")
            requirement_ids.add(requirement["id"])
            if requirement["source_requirement_ref"] in mapped_source_requirements:
                raise ContractError(f"Profile source requirement 重复映射: {profile['id']}: {requirement['source_requirement_ref']}")
            mapped_source_requirements.add(requirement["source_requirement_ref"])
            if requirement["source_requirement_ref"] not in target_by_id[profile["source_target_id"]]["requirement_refs"]:
                raise ContractError("Profile requirement 必须引用 source Target requirement")
            missing_nodes = sorted(set(requirement["node_ids"]) - set(nodes))
            if missing_nodes:
                raise ContractError(f"Profile requirement 引用不存在 nodes: {missing_nodes}")
            if len(requirement["node_ids"]) != len(set(requirement["node_ids"])):
                raise ContractError(f"Profile requirement node_ids 不得重复: {profile['id']}: {requirement['id']}")
            if any(nodes[node_id]["node_kind"] == "mc" for node_id in requirement["node_ids"]):
                raise ContractError("Mc 不得进入 Coverage 分母")
            if not any(nodes[node_id]["node_kind"] == "m1" for node_id in requirement["node_ids"]):
                raise ContractError("Profile requirement 必须映射至少一个 M1；Target gap 使用未实现 candidate M1 表达")
            required_scope = set(requirement.get("required_scope", []))
            available_scope = {scope for node_id in requirement["node_ids"] for scope in nodes[node_id]["scope"]}
            if required_scope - available_scope:
                raise ContractError(f"Profile requirement mandatory Scope 不属于映射 M1: {profile['id']}: {requirement['id']}")
            if requirement.get("minimum_effective_state"):
                state_rank(config, requirement["minimum_effective_state"])
        expected_requirements = set(target_by_id[profile["source_target_id"]]["requirement_refs"])
        if mapped_source_requirements != expected_requirements:
            raise ContractError(
                f"Profile 必须精确映射 candidate Target requirements；missing={sorted(expected_requirements-mapped_source_requirements)}, extra={sorted(mapped_source_requirements-expected_requirements)}"
            )
    return document


def _validate_tco_v3_model(workspace: Path, product_id: str, model_path: Path) -> dict[str, Any]:
    root = project_root(workspace, product_id); config = load_config(root); document = load_json(model_path)
    validate_document("tco_model_v3", document, model_path)
    if document["product_id"] != product_id or document["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO v3 product 或 coverage vocabulary 不一致")
    if document["effect_status_vocabulary_ref"] != EFFECT_STATUS_VOCABULARY_REF:
        raise ContractError("TCO v3 effect_status_vocabulary_ref 必须引用 goal_effect_status_v1")
    decision_vocabulary = config["decision_outcome_vocabulary"]
    if document["decision_outcome_vocabulary_digest"] != config["decision_outcome_vocabulary_digest"]:
        raise ContractError("TCO v3 decision outcome vocabulary digest 已漂移")
    _validate_graph(document); nodes = _nodes(document)
    kym_path = root / "kym" / "revisions" / f"kym-{document['source_kym_revision']}.json"
    kym = load_json(kym_path)
    semantic_validate_kym(kym, root)
    statement_ids = {item["id"] for item in kym["statements"]}
    target_by_id = {item["id"]: item for item in kym["target_profiles"]}
    if any(item["node_kind"] == "mc" and not item["children"] for item in nodes.values()):
        raise ContractError("mc 必须包含 children")
    for item in nodes.values():
        missing_statement_refs = sorted(set(item["source_statement_refs"]) - statement_ids)
        if missing_statement_refs:
            raise ContractError(f"TCO v3 node 引用不存在 KYM statements: {item['id']}: {missing_statement_refs}")
        if item["node_kind"] != "mc" and not item["source_statement_refs"]:
            raise ContractError(f"TCO v3 非 Mc node 必须引用 KYM statements: {item['id']}")
        if len(item["scope"]) != len(set(item["scope"])):
            raise ContractError(f"TCO v3 node scope 不得重复: {item['id']}")
        if set(item["scope_types"]) != set(item["scope"]):
            raise ContractError(f"TCO v3 scope_types 必须精确覆盖所有 Scope: {item['id']}")
        mandatory_scope = item.get("mandatory_scope_items", item["scope"])
        if len(mandatory_scope) != len(set(mandatory_scope)) or set(mandatory_scope) - set(item["scope"]):
            raise ContractError(f"TCO v3 mandatory_scope_items 必须是无重复的 Scope 子集: {item['id']}")
        if item["node_kind"] == "m1":
            if "effect_status" in item:
                raise ContractError(f"TCO v3 M1 不得携带 effect_status；execution result 必须单独记录: {item['id']}")
            required = {"capability_type", "inputs", "outputs", "decision_semantics", "invariants", "witness_artifact_refs", "negative_fixture_refs", "traceability_refs"}
            if required - set(item) or item["children"]:
                raise ContractError(f"m1 必须是完整的无 children 独立能力: {item['id']}")
            decision = item["decision_semantics"]
            outputs = decision["decision_output"]
            terminal_outputs = decision["terminal_decision_outputs"]
            excluded = item.get("excluded_outputs", [])
            if len(outputs) != len(set(outputs)) or len(terminal_outputs) != len(set(terminal_outputs)) or len(excluded) != len(set(excluded)):
                raise ContractError(f"TCO v3 M1 decision/excluded outputs 不得重复: {item['id']}")
            if set(terminal_outputs) - set(outputs):
                raise ContractError(f"TCO v3 terminal_decision_outputs 必须属于 decision_output: {item['id']}")
            noncanonical = sorted(set(terminal_outputs) - set(decision_vocabulary["outcomes"]))
            if noncanonical:
                raise ContractError(f"TCO v3 terminal decision output 必须使用 canonical vocabulary: {item['id']}: {noncanonical}")
            aliases = sorted(
                output
                for output in outputs
                if re.sub(r"[-_\\s]+", " ", output.strip()) in decision_vocabulary["forbidden_aliases"]
            )
            if aliases:
                raise ContractError(f"TCO v3 decision_output 不得使用终态同义词，改用 canonical value: {item['id']}: {aliases}")
            if set(excluded) - set(outputs) or set(excluded) & set(item["scope"]):
                raise ContractError(f"TCO v3 excluded_outputs 必须是未映射 decision_output，且不得与 Scope 重叠: {item['id']}")
            unmapped = sorted(set(outputs) - set(item["scope"]) - set(excluded))
            if unmapped:
                raise ContractError(f"TCO v3 decision_output 必须映射到 Scope 或 excluded_outputs: {item['id']}: {unmapped}")
            misclassified = sorted(output for output in outputs if output in item["scope"] and item["scope_types"][output] not in {"outcome", "failure"})
            if misclassified:
                raise ContractError(f"TCO v3 decision_output 的 Scope 类型必须为 outcome 或 failure: {item['id']}: {misclassified}")
        if item["node_kind"] == "policy" and any(key in item for key in ("inputs", "outputs", "capability_type", "effect_status")):
            raise ContractError(f"policy 不得冒充输入输出能力或执行结果: {item['id']}")
    seen_targets: set[str] = set()
    for profile in document["target_profiles"]:
        if profile["source_target_id"] in seen_targets:
            raise ContractError("同一 source_target_id 只能有一个稳定 Profile")
        if re.search(r"(?:^|[-_\s])r\d{3,}|revision|\bview\b", profile["id"] + " " + profile["label"], re.IGNORECASE):
            raise ContractError("Profile 不得使用 revision/view 命名；历史视图必须进入 lineage")
        seen_targets.add(profile["source_target_id"])
        if profile["source_target_id"] not in target_by_id:
            raise ContractError("Profile source_target_id 必须引用 KYM candidate Target")
        goals = {goal["id"]: goal for goal in profile["coverage_goals"]}
        if len(goals) != len(profile["coverage_goals"]) or set(goals) != {"contract", "implementation"} or goals["contract"]["minimum_coverage_state"] != "design_only" or goals["contract"]["minimum_lifecycle_state"] != "contract_defined" or goals["implementation"]["minimum_coverage_state"] != "bounded_runtime" or goals["implementation"]["minimum_lifecycle_state"] != "implemented":
            raise ContractError("Profile 必须区分 contract 与 bounded_runtime implementation goals")
        requirement_ids: set[str] = set()
        mapped_source_requirements: set[str] = set()
        for requirement in profile["requirements"]:
            if requirement["id"] in requirement_ids:
                raise ContractError(f"Profile requirement id 重复: {requirement['id']}")
            requirement_ids.add(requirement["id"])
            if requirement["source_requirement_ref"] in mapped_source_requirements:
                raise ContractError(f"Profile source requirement 重复映射: {profile['id']}: {requirement['source_requirement_ref']}")
            mapped_source_requirements.add(requirement["source_requirement_ref"])
            if requirement["source_requirement_ref"] not in target_by_id[profile["source_target_id"]]["requirement_refs"]:
                raise ContractError("Profile requirement 必须引用 source Target requirement")
            missing_nodes = sorted(set(requirement["node_ids"]) - set(nodes))
            if missing_nodes:
                raise ContractError(f"Profile requirement 引用不存在 nodes: {missing_nodes}")
            if len(requirement["node_ids"]) != len(set(requirement["node_ids"])):
                raise ContractError(f"Profile requirement node_ids 不得重复: {profile['id']}: {requirement['id']}")
            if any(nodes[node_id]["node_kind"] == "mc" for node_id in requirement["node_ids"]):
                raise ContractError("Mc 不得进入 Coverage 分母")
            if not any(nodes[node_id]["node_kind"] == "m1" for node_id in requirement["node_ids"]):
                raise ContractError("Profile requirement 必须映射至少一个 M1；Target gap 使用 candidate M1 表达")
            required_scope = set(requirement.get("required_scope", []))
            available_scope = {scope for node_id in requirement["node_ids"] for scope in nodes[node_id]["scope"]}
            if required_scope - available_scope:
                raise ContractError(f"Profile requirement mandatory Scope 不属于映射 M1: {profile['id']}: {requirement['id']}")
            if requirement.get("minimum_effective_state"):
                state_rank(config, requirement["minimum_effective_state"])
        expected_requirements = set(target_by_id[profile["source_target_id"]]["requirement_refs"])
        if mapped_source_requirements != expected_requirements:
            raise ContractError(
                f"Profile 必须精确映射 candidate Target requirements；missing={sorted(expected_requirements-mapped_source_requirements)}, extra={sorted(mapped_source_requirements-expected_requirements)}"
            )
    return document


def _validate_tco_v4_model(workspace: Path, product_id: str, model_path: Path) -> dict[str, Any]:
    root = project_root(workspace, product_id); config = load_config(root); document = load_json(model_path)
    validate_document("tco_model_v4", document, model_path)
    if document["product_id"] != product_id or document["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO v4 product 或 coverage vocabulary 不一致")
    _validate_lifecycle_contract(document["lifecycle_contract"]["capability_lifecycle_vocabulary"], strict_v4=True)
    if document["effect_status_vocabulary_ref"] != EFFECT_STATUS_VOCABULARY_REF:
        raise ContractError("TCO v4 effect_status_vocabulary_ref 必须引用 goal_effect_status_v1")
    decision_vocabulary = config["decision_outcome_vocabulary"]
    if document["decision_outcome_vocabulary_digest"] != config["decision_outcome_vocabulary_digest"]:
        raise ContractError("TCO v4 decision outcome vocabulary digest 已漂移")
    forbidden_aliases = _decision_aliases(decision_vocabulary)
    _validate_graph(document); nodes = _nodes(document)
    kym_path = root / "kym" / "revisions" / f"kym-{document['source_kym_revision']}.json"
    kym = load_json(kym_path)
    semantic_validate_kym(kym, root)
    statement_ids = {item["id"] for item in kym["statements"]}
    target_by_id = {item["id"]: item for item in kym["target_profiles"]}
    if any(item["node_kind"] == "mc" and not item["children"] for item in nodes.values()):
        raise ContractError("mc 必须包含 children")

    for item in nodes.values():
        missing_statement_refs = sorted(set(item["source_statement_refs"]) - statement_ids)
        if missing_statement_refs:
            raise ContractError(f"TCO v4 node 引用不存在 KYM statements: {item['id']}: {missing_statement_refs}")
        if item["node_kind"] != "mc" and not item["source_statement_refs"]:
            raise ContractError(f"TCO v4 非 Mc node 必须引用 KYM statements: {item['id']}")
        if not isinstance(item["scope"], dict) or set(item["scope"]) != {"boundary"}:
            raise ContractError(f"TCO v4 scope 只能表达 boundary object: {item['id']}")
        boundary = item["scope"]["boundary"]
        if len(boundary["included"]) != len(set(boundary["included"])) or len(boundary["excluded"]) != len(set(boundary["excluded"])):
            raise ContractError(f"TCO v4 scope boundary included/excluded 不得重复: {item['id']}")
        if item["node_kind"] == "m1":
            forbidden_legacy = sorted(set(item) & {"scope_types", "mandatory_scope_items", "effect_status"})
            if forbidden_legacy:
                raise ContractError(f"TCO v4 M1 不得携带 legacy model fields: {item['id']}: {forbidden_legacy}")
            required = {"capability_type", "inputs", "outputs", "execution_semantics", "decision_semantics", "metadata_requirements", "coverage_requirements", "mandatory_coverage_requirement_ids", "invariants", "witness_artifact_refs", "negative_fixture_refs", "traceability_refs"}
            if required - set(item) or item["children"]:
                raise ContractError(f"TCO v4 m1 必须是完整的无 children 独立能力: {item['id']}")
            input_ids = _object_ids(item["inputs"], f"{item['id']} inputs")
            output_ids = _object_ids(item["outputs"], f"{item['id']} outputs")
            execution_errors = validate_execution_semantics(item)
            if execution_errors:
                raise ContractError(
                    f"TCO v4 execution_semantics 无效: {item['id']}: {execution_errors}"
                )
            metadata_ids = _object_ids(item["metadata_requirements"], f"{item['id']} metadata_requirements")
            decision = item["decision_semantics"]
            for label, values in (
                ("decision_inputs", decision["decision_inputs"]),
                ("decision_outputs", decision["decision_outputs"]),
                ("terminal_outcomes", decision["terminal_outcomes"]),
                ("failure_outcomes", decision["failure_outcomes"]),
                ("excluded_outputs", item.get("excluded_outputs", [])),
            ):
                _require_unique_values(values, f"TCO v4 {item['id']} {label}")
                _require_no_canonical_collisions(values, f"TCO v4 {item['id']} {label}")
                _require_canonical_id_values(values, f"TCO v4 {item['id']} {label}")
            missing_inputs = sorted(set(decision["decision_inputs"]) - set(input_ids))
            if missing_inputs:
                raise ContractError(f"TCO v4 decision_inputs 必须引用 declared inputs: {item['id']}: {missing_inputs}")
            terminal = set(decision["terminal_outcomes"])
            failure = set(decision["failure_outcomes"])
            if failure - terminal:
                raise ContractError(f"TCO v4 failure_outcomes 必须属于 terminal_outcomes: {item['id']}: {sorted(failure-terminal)}")
            _require_no_input_only_outcomes(list(terminal | failure), f"TCO v4 {item['id']} terminal/failure outcomes")
            metadata_artifact_hits = _metadata_artifact_token_hits(item["metadata_requirements"])
            if metadata_artifact_hits:
                raise ContractError(f"TCO v4 P06 artifact output 不得作为 metadata_requirements: {item['id']}: {metadata_artifact_hits}")
            alias_hits = sorted(
                value for value in set(decision["decision_outputs"]) | terminal | failure
                if _canonical_token(value) in forbidden_aliases
            )
            if alias_hits:
                raise ContractError(f"TCO v4 decision/terminal/failure outcomes 不得使用无上下文 alias: {item['id']}: {alias_hits}")
            declared_tokens = {
                _canonical_token(value)
                for value in [
                    *input_ids,
                    *output_ids,
                    *metadata_ids,
                    *decision["decision_outputs"],
                    *decision["terminal_outcomes"],
                ]
            }
            excluded_overlap = sorted(
                value
                for value in item.get("excluded_outputs", [])
                if _canonical_token(value) in declared_tokens
            )
            if excluded_overlap:
                raise ContractError(f"TCO v4 excluded_outputs 不得与 declared inputs/outputs/decision outputs/outcomes/metadata 重叠: {item['id']}: {excluded_overlap}")
            if item.get("claim_exclusions") and item.get("claim_exclusions_not_applicable"):
                raise ContractError(f"TCO v4 claim_exclusions 与 claim_exclusions_not_applicable 不得同时声明: {item['id']}")
            if item.get("claim_exclusions"):
                _object_ids(item["claim_exclusions"], f"{item['id']} claim_exclusions")
            coverage_ids = _object_ids(item["coverage_requirements"], f"{item['id']} coverage_requirements")
            mandatory = set(item["mandatory_coverage_requirement_ids"])
            flagged = {row["id"] for row in item["coverage_requirements"] if row["mandatory"]}
            if mandatory != flagged:
                raise ContractError(f"TCO v4 mandatory_coverage_requirement_ids 必须等于 coverage_requirements mandatory=true: {item['id']}")
            if mandatory - set(coverage_ids):
                raise ContractError(f"TCO v4 mandatory coverage requirement 必须引用 coverage_requirements: {item['id']}: {sorted(mandatory-set(coverage_ids))}")
            all_tokens = [*input_ids, *output_ids, *metadata_ids, *decision["decision_outputs"], *decision["terminal_outcomes"], *coverage_ids]
            _require_no_canonical_collisions(all_tokens, f"TCO v4 {item['id']} cross-field tokens")
            _validate_v4_coverage_requirement_refs(
                item["id"],
                item["coverage_requirements"],
                input_ids=input_ids,
                output_ids=output_ids,
                terminal_outcomes=decision["terminal_outcomes"],
                failure_outcomes=decision["failure_outcomes"],
                metadata_ids=metadata_ids,
            )
        if item["node_kind"] == "policy":
            if any(key in item for key in ("inputs", "outputs", "capability_type", "decision_semantics", "effect_status", "scope_types", "mandatory_scope_items")):
                raise ContractError(f"policy 不得冒充输入输出能力、决策能力或 legacy Scope: {item['id']}")
            if "coverage_requirements" in item:
                coverage_ids = _object_ids(item["coverage_requirements"], f"{item['id']} coverage_requirements")
                mandatory = set(item.get("mandatory_coverage_requirement_ids", []))
                flagged = {row["id"] for row in item["coverage_requirements"] if row["mandatory"]}
                if mandatory != flagged or mandatory - set(coverage_ids):
                    raise ContractError(f"TCO v4 policy mandatory coverage requirements 必须一致: {item['id']}")
                _validate_v4_coverage_requirement_refs(item["id"], item["coverage_requirements"], policy_node=True)
    seen_targets: set[str] = set()
    for profile in document["target_profiles"]:
        if profile["source_target_id"] in seen_targets:
            raise ContractError("同一 source_target_id 只能有一个稳定 Profile")
        if re.search(r"(?:^|[-_\s])r\d{3,}|revision|\bview\b", profile["id"] + " " + profile["label"], re.IGNORECASE):
            raise ContractError("Profile 不得使用 revision/view 命名；历史视图必须进入 lineage")
        seen_targets.add(profile["source_target_id"])
        if profile["source_target_id"] not in target_by_id:
            raise ContractError("Profile source_target_id 必须引用 KYM candidate Target")
        goals = {goal["id"]: goal for goal in profile["coverage_goals"]}
        _require_v4_profile_goal_contract(goals, len(profile["coverage_goals"]))
        requirement_ids: set[str] = set()
        mapped_source_requirements: set[str] = set()
        for requirement in profile["requirements"]:
            if requirement["id"] in requirement_ids:
                raise ContractError(f"Profile requirement id 重复: {requirement['id']}")
            requirement_ids.add(requirement["id"])
            if requirement["source_requirement_ref"] in mapped_source_requirements:
                raise ContractError(f"Profile source requirement 重复映射: {profile['id']}: {requirement['source_requirement_ref']}")
            mapped_source_requirements.add(requirement["source_requirement_ref"])
            if requirement["source_requirement_ref"] not in target_by_id[profile["source_target_id"]]["requirement_refs"]:
                raise ContractError("Profile requirement 必须引用 source Target requirement")
            missing_nodes = sorted(set(requirement["node_ids"]) - set(nodes))
            if missing_nodes:
                raise ContractError(f"Profile requirement 引用不存在 nodes: {missing_nodes}")
            if len(requirement["node_ids"]) != len(set(requirement["node_ids"])):
                raise ContractError(f"Profile requirement node_ids 不得重复: {profile['id']}: {requirement['id']}")
            if any(nodes[node_id]["node_kind"] == "mc" for node_id in requirement["node_ids"]):
                raise ContractError("Mc 不得进入 Coverage 分母")
            if not any(nodes[node_id]["node_kind"] == "m1" for node_id in requirement["node_ids"]):
                raise ContractError("Profile requirement 必须映射至少一个 M1；policy 不能独自作为能力分母")
            required_ids = set(requirement.get("required_coverage_requirement_ids", []))
            available_ids = {
                coverage_requirement["id"]
                for node_id in requirement["node_ids"]
                for coverage_requirement in nodes[node_id].get("coverage_requirements", [])
            }
            if required_ids - available_ids:
                raise ContractError(f"Profile requirement mandatory Coverage requirement 不属于映射 M1/policy: {profile['id']}: {requirement['id']}: {sorted(required_ids-available_ids)}")
            if requirement.get("minimum_coverage_state"):
                state_rank(config, requirement["minimum_coverage_state"])
            if requirement.get("minimum_effective_state"):
                state_rank(config, requirement["minimum_effective_state"])
            if requirement.get("minimum_lifecycle_state") and requirement["minimum_lifecycle_state"] not in CAPABILITY_LIFECYCLE_STATES:
                raise ContractError(f"Profile requirement minimum_lifecycle_state 必须使用 lifecycle 词表: {profile['id']}: {requirement['id']}")
            if requirement.get("required_validation_posture"):
                _validate_validation_posture(requirement["required_validation_posture"], f"Profile requirement {profile['id']}:{requirement['id']}")
        expected_requirements = set(target_by_id[profile["source_target_id"]]["requirement_refs"])
        if mapped_source_requirements != expected_requirements:
            raise ContractError(
                f"Profile 必须精确映射 candidate Target requirements；missing={sorted(expected_requirements-mapped_source_requirements)}, extra={sorted(mapped_source_requirements-expected_requirements)}"
            )
    return document


def migrate_v1_to_v2(model_path: Path, coverage_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Deterministically transform v1 to unresolved, candidate-only v2; never copy receipts."""
    legacy = load_json(model_path); validate_document("tco_model", legacy, model_path)
    nodes: list[dict[str, Any]] = []
    for node in legacy["nodes"]:
        kind = {"group": "mc", "capability": "m1", "policy": "policy"}[node["node_kind"]]
        row = {"id": node["id"], "node_kind": kind, "title": node["title"], "topic": node["topic"], "scope": node["scope"], "scope_types": {scope: "metadata" for scope in node["scope"]}, "purpose": node["purpose"], "source_statement_refs": node["source_statement_refs"], "children": node["children"]}
        if kind == "m1":
            row["scope"].append("unresolved-output")
            row["scope_types"]["unresolved-output"] = "outcome"
            row.update({"capability_type": "unresolved", "inputs": ["unresolved-input"], "outputs": ["unresolved-output"], "decision_semantics": {"type": "unresolved", "decision_input": ["unresolved-input"], "decision_output": ["unresolved-output"], "terminal_decision_outputs": [], "decision_rules": ["migration_unresolved"], "failure_modes": ["migration_unresolved"], "authority_owner": "unresolved"}, "invariants": ["migration_unresolved"], "witness_artifact_refs": ["unresolved-witness"], "negative_fixture_refs": ["unresolved-negative-fixture"], "traceability_refs": ["unresolved-traceability"], "effect_status": "not_executed"})
        nodes.append(row)
    profiles = []
    profile_lineage: list[dict[str, Any]] = []
    seen: set[str] = set()
    for profile in legacy["target_profiles"]:
        source_target = profile["source_target_id"]
        if source_target in seen:
            profile_lineage.append({"source_profile_id": profile["id"], "source_target_id": source_target, "merged_into_existing_profile": True})
            continue
        seen.add(source_target)
        profile_lineage.append({"source_profile_id": profile["id"], "source_target_id": source_target, "target_profile_id": profile["id"], "merged_into_existing_profile": False})
        profiles.append({"id": profile["id"], "label": profile["label"], "source_target_id": source_target, "description": profile.get("description", ""), "claim_ceiling": profile["claim_ceiling"], "coverage_goals": [{"id":"contract","minimum_coverage_state":"design_only","minimum_lifecycle_state":"contract_defined"},{"id":"implementation","minimum_coverage_state":"bounded_runtime","minimum_lifecycle_state":"implemented"}], "requirements": [{"id": item["id"], "source_requirement_ref": item["source_requirement_ref"], "title": item["title"], "node_ids": item["node_ids"], "coverage_type":"direct", "validation_owner":"unresolved", "witness_evidence_mapping":[], "nf_evidence_mapping":[], "claim_ceiling":profile["claim_ceiling"], "missing_items":["migration_unresolved"]} for item in profile["requirements"]]})
    model = {"schema":"kym_tco.tco_model.v2", "candidate_status":"candidate_not_approved", "model_revision":"r001", "base_model_digest":None, "product_id":legacy["product_id"], "source_kym_revision":legacy["source_kym_revision"], "coverage_vocabulary_digest":legacy["coverage_vocabulary_digest"], "decision_outcome_vocabulary_digest":digest_value(DEFAULT_DECISION_OUTCOME_VOCABULARY), "root":legacy["root"], "nodes":nodes, "target_profiles":profiles, "traceability_catalog":{"requirements":[],"acceptance_criteria":[],"non_functionals":[],"witness_artifacts":[],"required_outputs":[]}, "lifecycle_contract":{"effect_status_vocabulary":["not_executed","incomplete","attempted_failed","achieved"],"capability_lifecycle_vocabulary":["contract_defined","implemented","unresolved"]}, "authority_boundary":"candidate_not_approved; migration does not inherit approval receipts", "verdict":"NOT_EVALUATED", "functional_acceptance":"not_evaluated", "safety_acceptance":"not_evaluated", "claim_ceiling":"candidate_not_approved; unresolved migration semantics block implementation readiness", "lineage":[{"source_schema":"kym_tco.tco_model.v1","source_digest":digest_value(legacy),"unresolved":True,"profile_lineage":profile_lineage}]}
    coverage = None
    if coverage_path is not None:
        legacy_coverage = load_json(coverage_path); validate_document("coverage", legacy_coverage, coverage_path)
        migrated_nodes = {node["id"]: node for node in nodes}
        migrated_records = []
        for item in legacy_coverage["records"]:
            scope_rows = list(item["scope_coverage"])
            present = {row["scope"] for row in scope_rows}
            for scope in migrated_nodes[item["tco_node_id"]]["scope"]:
                if scope not in present:
                    scope_rows.append({"scope": scope, "coverage_state": "not_started", "notes": "migration_unresolved_scope"})
            migrated_records.append({**item, "capability_lifecycle_state":"unresolved", "counted_evidence":[], "scope_coverage": scope_rows})
        coverage = {"schema":"kym_tco.tco_coverage_model.v2", "product_id":legacy["product_id"], "source_tco_model":"model-v2.json", "source_tco_model_digest":digest_value(model), "coverage_mode":legacy_coverage["coverage_mode"], "coverage_vocabulary_digest":legacy_coverage["coverage_vocabulary_digest"], "proposal_id":legacy_coverage["proposal_id"] + "-v2", "candidate_status":"candidate_not_approved", "records":migrated_records}
    return model, coverage


def migrate_v2_to_v3(model_path: Path, coverage_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Move legacy model-owned effect_status into lineage without creating run results."""
    legacy = load_json(model_path); validate_document("tco_model_v2", legacy, model_path)
    legacy_effect_status = {
        node["id"]: node.get("effect_status")
        for node in legacy["nodes"]
        if node.get("node_kind") == "m1"
    }
    nodes: list[dict[str, Any]] = []
    for node in legacy["nodes"]:
        row = copy.deepcopy(node)
        if row.get("node_kind") == "m1":
            row.pop("effect_status", None)
        nodes.append(row)
    lineage = copy.deepcopy(legacy.get("lineage", []))
    lineage.append({
        "source_schema": "kym_tco.tco_model.v2",
        "source_digest": digest_value(legacy),
        "migration": "migrate-v2-to-v3",
        "effect_status_moved_out_of_capability_model": True,
        "legacy_not_executed_interpretation": "v2_model_had_no_bound_execution_result",
        "legacy_effect_status_by_m1": legacy_effect_status,
        "created_execution_results": [],
    })
    model = {
        **{key: value for key, value in legacy.items() if key not in {"schema", "nodes", "lifecycle_contract", "lineage"}},
        "schema": "kym_tco.tco_model.v3",
        "effect_status_vocabulary_ref": EFFECT_STATUS_VOCABULARY_REF,
        "nodes": nodes,
        "lifecycle_contract": {
            "capability_lifecycle_vocabulary": legacy["lifecycle_contract"]["capability_lifecycle_vocabulary"],
        },
        "lineage": lineage,
    }
    coverage = None
    if coverage_path is not None:
        legacy_coverage = load_json(coverage_path); validate_document("coverage_v2", legacy_coverage, coverage_path)
        if legacy_coverage["source_tco_model_digest"] != digest_value(legacy):
            raise ContractError("TCO v2 Coverage source model digest 已漂移，不能迁移绑定到 v3")
        coverage = {
            **legacy_coverage,
            "source_tco_model": "model-v3.json",
            "source_tco_model_digest": digest_value(model),
            "proposal_id": legacy_coverage["proposal_id"] + "-v3",
        }
    return model, coverage


def _v4_id(value: str) -> str:
    return _canonical_token(value)


def _v4_object(value: str, *, object_id: str | None = None, artifact_type: str | None = None, required: bool = True) -> dict[str, Any]:
    identifier = object_id or _v4_id(value)
    return {
        "id": identifier,
        "label": value,
        "artifact_type": artifact_type or identifier,
        "required": required,
    }


def _legacy_scope_to_v4_requirement_ids(node: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    scope_types = node.get("scope_types", {})
    for scope in node.get("scope", []):
        token = _v4_id(scope)
        scope_type = scope_types.get(scope, "metadata")
        if scope_type == "input":
            mapping[scope] = f"input.{token}"
        elif scope_type == "outcome":
            mapping[scope] = f"outcome.{token}"
        elif scope_type == "failure":
            mapping[scope] = f"failure.{token}"
        else:
            mapping[scope] = f"metadata.{token}"
    return mapping


def migrate_v3_to_v4(model_path: Path, coverage_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Separate v3 typed Scope into v4 capability fields and stable Coverage requirements."""
    legacy = load_json(model_path); validate_document("tco_model_v3", legacy, model_path)
    node_requirement_maps: dict[str, dict[str, str]] = {}
    nodes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for node in legacy["nodes"]:
        row: dict[str, Any] = {
            "id": node["id"],
            "node_kind": node["node_kind"],
            "title": node["title"],
            "topic": node["topic"],
            "scope": {"boundary": {"included": list(node.get("scope", [])), "excluded": []}},
            "purpose": node["purpose"],
            "source_statement_refs": node["source_statement_refs"],
            "children": node["children"],
        }
        if node["node_kind"] == "m1":
            raw_tokens = [
                *node.get("scope", []),
                *node.get("inputs", []),
                *node.get("outputs", []),
                *node.get("decision_semantics", {}).get("decision_input", []),
                *node.get("decision_semantics", {}).get("decision_output", []),
            ]
            _require_no_canonical_alias_collisions(raw_tokens, f"v3->v4 migration {node['id']} legacy tokens")
            scope_types = node.get("scope_types", {})
            decision = node.get("decision_semantics", {})
            input_values = list(dict.fromkeys([*node.get("inputs", []), *[scope for scope in node.get("scope", []) if scope_types.get(scope) == "input"]]))
            output_values = list(dict.fromkeys(node.get("outputs", [])))
            terminal_values = list(dict.fromkeys([*decision.get("terminal_decision_outputs", []), *decision.get("decision_output", [])]))
            input_tokens = {_v4_id(value) for value in input_values}
            terminal_input_conflicts = [value for value in terminal_values if _v4_id(value) in input_tokens]
            if terminal_input_conflicts:
                unresolved.append({"node_id": node["id"], "terminal_outcomes_reclassified_as_inputs": terminal_input_conflicts})
                terminal_values = [value for value in terminal_values if _v4_id(value) not in input_tokens]
            failure_values = [value for value in terminal_values if scope_types.get(value) == "failure"]
            terminal_tokens = {_v4_id(value) for value in terminal_values}
            metadata_values = [
                scope
                for scope in node.get("scope", [])
                if scope_types.get(scope) == "metadata"
                and _v4_id(scope) not in input_tokens | terminal_tokens
            ]
            metadata_tokens = {_v4_id(value) for value in metadata_values}
            output_id_by_value: dict[str, str] = {}
            occupied_tokens = input_tokens | terminal_tokens | metadata_tokens
            for value in output_values:
                token = _v4_id(value)
                output_id = token
                if output_id in occupied_tokens:
                    output_id = f"artifact_{token}"
                    unresolved.append({"node_id": node["id"], "output_id_prefixed_to_avoid_decision_or_input_collision": {"legacy_output": value, "output_id": output_id}})
                output_id_by_value[value] = output_id
                occupied_tokens.add(output_id)
            coverage_requirements: list[dict[str, Any]] = []
            legacy_scope_map = _legacy_scope_to_v4_requirement_ids(node)
            node_requirement_maps[node["id"]] = legacy_scope_map
            mandatory_legacy = set(node.get("mandatory_scope_items", node.get("scope", [])))

            def add_requirement(req_id: str, category: str, ref: str, *, legacy_scope: str | None = None) -> None:
                if req_id in {item["id"] for item in coverage_requirements}:
                    return
                coverage_requirements.append({
                    "id": req_id,
                    "category": category,
                    "ref": ref,
                    "mandatory": legacy_scope in mandatory_legacy if legacy_scope is not None else False,
                })

            for value in input_values:
                token = _v4_id(value); add_requirement(f"input.{token}", "input", f"inputs.{token}", legacy_scope=value if value in legacy_scope_map else None)
            for value in output_values:
                token = output_id_by_value[value]; add_requirement(f"output.{token}", "output", f"outputs.{token}")
            for value in terminal_values:
                token = _v4_id(value)
                category = "failure" if value in failure_values else "outcome"
                ref_prefix = "decision_semantics.failure_outcomes" if category == "failure" else "decision_semantics.terminal_outcomes"
                add_requirement(f"{category}.{token}", category, f"{ref_prefix}.{token}", legacy_scope=value if value in legacy_scope_map else None)
            for value in metadata_values:
                token = _v4_id(value); add_requirement(f"metadata.{token}", "metadata", f"metadata_requirements.{token}", legacy_scope=value)
            mapped_mandatory = {legacy_scope_map[item] for item in mandatory_legacy if item in legacy_scope_map}
            for requirement in coverage_requirements:
                if requirement["id"] in mapped_mandatory:
                    requirement["mandatory"] = True
            unmapped_mandatory = sorted(mandatory_legacy - set(legacy_scope_map))
            if unmapped_mandatory:
                unresolved.append({"node_id": node["id"], "unmapped_mandatory_scope_items": unmapped_mandatory})
            row.update({
                "capability_type": node["capability_type"],
                "inputs": [_v4_object(value) for value in input_values],
                "outputs": [_v4_object(value, object_id=output_id_by_value[value]) for value in output_values],
                "execution_semantics": {
                    "operation": "execute",
                    "materialized_output_ids": [],
                    "validated_output_ids": [],
                    "unresolved_output_ids": sorted(output_id_by_value.values()),
                },
                "decision_semantics": {
                    "type": decision.get("type", "unresolved"),
                    "decision_inputs": [_v4_id(value) for value in decision.get("decision_input", [])],
                    "decision_outputs": ["decision_result"] if terminal_values else [],
                    "terminal_outcomes": [_v4_id(value) for value in terminal_values],
                    "failure_outcomes": [_v4_id(value) for value in failure_values],
                    "decision_rules": decision.get("decision_rules", []),
                    "authority_owner": decision.get("authority_owner", "unresolved"),
                },
                "metadata_requirements": [{"id": _v4_id(value), "label": value, "required": value in mandatory_legacy} for value in metadata_values],
                "coverage_requirements": coverage_requirements,
                "mandatory_coverage_requirement_ids": [item["id"] for item in coverage_requirements if item["mandatory"]],
                "excluded_outputs": [_v4_id(value) for value in node.get("excluded_outputs", [])],
                "claim_exclusions": [],
                "invariants": node["invariants"],
                "witness_artifact_refs": node["witness_artifact_refs"],
                "negative_fixture_refs": node["negative_fixture_refs"],
                "traceability_refs": node["traceability_refs"],
            })
            unresolved.append({
                "node_id": node["id"],
                "execution_semantics_requires_review": {
                    "legacy_title": node["title"],
                    "legacy_capability_type": node["capability_type"],
                    "legacy_output_ids": sorted(output_id_by_value.values()),
                    "reason": "v3 did not distinguish materialized outputs from validated existing artifacts",
                },
            })
        elif node["node_kind"] == "policy":
            legacy_scope_map = {scope: f"metadata.{_v4_id(scope)}" for scope in node.get("scope", [])}
            node_requirement_maps[node["id"]] = legacy_scope_map
            requirements = [
                {
                    "id": legacy_scope_map[scope],
                    "category": "metadata",
                    "ref": f"policy_requirements.{_v4_id(scope)}",
                    "mandatory": scope in node.get("mandatory_scope_items", node.get("scope", [])),
                }
                for scope in node.get("scope", [])
            ]
            row["coverage_requirements"] = requirements
            row["mandatory_coverage_requirement_ids"] = [item["id"] for item in requirements if item["mandatory"]]
        nodes.append(row)
    lineage = copy.deepcopy(legacy.get("lineage", []))
    lineage.append({
        "source_schema": "kym_tco.tco_model.v3",
        "source_digest": digest_value(legacy),
        "migration": "migrate-v3-to-v4",
        "scope_moved_to_boundary": True,
        "coverage_denominator_moved_to_coverage_requirements": True,
        "unresolved": unresolved,
    })
    profiles = []
    for profile in legacy["target_profiles"]:
        requirements = []
        for requirement in profile["requirements"]:
            required_legacy = requirement.get("required_scope", [])
            mapped_required = sorted({
                node_requirement_maps.get(node_id, {}).get(scope)
                for node_id in requirement["node_ids"]
                for scope in required_legacy
                if node_requirement_maps.get(node_id, {}).get(scope)
            })
            requirements.append({
                **{key: value for key, value in requirement.items() if key != "required_scope"},
                "required_coverage_requirement_ids": [item for item in mapped_required if item],
                **(
                    {"minimum_coverage_state": requirement["minimum_effective_state"]}
                    if requirement.get("minimum_effective_state")
                    else {}
                ),
            })
        coverage_goals = []
        for goal in profile["coverage_goals"]:
            coverage_goals.append({
                **goal,
                "required_validation_posture": (
                    "producer_checked"
                    if goal["id"] == "contract"
                    else "independently_validated"
                ),
            })
        profiles.append({**profile, "coverage_goals": coverage_goals, "requirements": requirements})
    model = {
        **{key: value for key, value in legacy.items() if key not in {"schema", "nodes", "target_profiles", "lineage"}},
        "schema": "kym_tco.tco_model.v4",
        "lifecycle_contract": {
            "capability_lifecycle_vocabulary": list(CAPABILITY_LIFECYCLE_STATES),
        },
        "nodes": nodes,
        "target_profiles": profiles,
        "lineage": lineage,
    }
    coverage = None
    if coverage_path is not None:
        legacy_coverage = load_json(coverage_path); validate_document("coverage_v2", legacy_coverage, coverage_path)
        if legacy_coverage["source_tco_model_digest"] != digest_value(legacy):
            raise ContractError("TCO v3 Coverage source model digest 已漂移，不能迁移绑定到 v4")
        migrated_records = []
        migrated_nodes = {node["id"]: node for node in nodes}
        for record in legacy_coverage["records"]:
            node_id = record["tco_node_id"]
            legacy_map = node_requirement_maps.get(node_id, {})
            rows = []
            mapped_scope_rows = {}
            for row in record.get("scope_coverage", []):
                requirement_id = legacy_map.get(row["scope"])
                if requirement_id:
                    rows.append({
                        "requirement_id": requirement_id,
                        "coverage_state": row["coverage_state"],
                        "evidence_refs": [],
                        "notes": row.get("notes", ""),
                    })
                    mapped_scope_rows[requirement_id] = True
            for requirement in migrated_nodes[node_id].get("coverage_requirements", []):
                if requirement["id"] not in mapped_scope_rows:
                    rows.append({
                        "requirement_id": requirement["id"],
                        "coverage_state": "not_started",
                        "evidence_refs": [],
                        "notes": "migration_unmapped_legacy_scope",
                    })
            missing_ids = sorted({
                legacy_map[item]
                for item in record.get("missing_scope_items", [])
                if item in legacy_map
            })
            direct_state = record.get("coverage_state", "unknown")
            row_states = {item["requirement_id"]: item["coverage_state"] for item in rows}
            mandatory_states = [
                row_states.get(requirement_id, "unknown")
                for requirement_id in migrated_nodes[node_id].get("mandatory_coverage_requirement_ids", [])
            ]
            default_rank = {
                "unknown": 0,
                "not_started": 10,
                "design_only": 20,
                "schema_validator": 30,
                "test_bound": 40,
                "bounded_runtime": 50,
                "integrated_runtime": 60,
            }
            derived_effective = min([direct_state, *mandatory_states], key=lambda state: default_rank.get(state, 10_000))
            legacy_lifecycle = record.get("capability_lifecycle_state", "unresolved")
            lifecycle = "draft" if legacy_lifecycle == "unresolved" else legacy_lifecycle
            resolution = "unresolved" if legacy_lifecycle == "unresolved" else record.get("resolution_status", "resolved")
            is_unresolved_m1 = migrated_nodes[node_id].get("node_kind") == "m1"
            if is_unresolved_m1:
                def cap_migration_state(state: str) -> str:
                    return state if default_rank.get(state, 0) <= default_rank["design_only"] else "design_only"

                for requirement_row in rows:
                    requirement_row["coverage_state"] = cap_migration_state(requirement_row["coverage_state"])
                    suffix = "migration_execution_semantics_unresolved"
                    requirement_row["notes"] = "; ".join(
                        item for item in (requirement_row.get("notes", ""), suffix) if item
                    )
                direct_state = cap_migration_state(direct_state)
                derived_effective = cap_migration_state(derived_effective)
                lifecycle = "draft"
                resolution = "unresolved"
            migrated_records.append({
                **{key: value for key, value in record.items() if key not in {"scope_coverage", "missing_scope_items", "counted_evidence", "capability_lifecycle_state"}},
                "coverage_state": direct_state,
                "evidence_peak_state": cap_migration_state(record.get("evidence_peak_state", direct_state)) if is_unresolved_m1 else record.get("evidence_peak_state", direct_state),
                "effective_state": derived_effective if is_unresolved_m1 else record.get("effective_state", derived_effective),
                "claim_state": derived_effective if is_unresolved_m1 else record.get("claim_state", record.get("effective_state", derived_effective)),
                "capability_lifecycle_state": lifecycle,
                "validation_posture": "not_validated" if is_unresolved_m1 else record.get("validation_posture", "not_validated"),
                "resolution_status": resolution,
                "requirement_coverage": rows,
                "missing_coverage_requirement_ids": missing_ids,
            })
        coverage = {
            "schema": "kym_tco.tco_coverage_model.v3",
            "product_id": legacy_coverage["product_id"],
            "source_tco_model": "model-v4.json",
            "source_tco_model_digest": digest_value(model),
            "coverage_mode": legacy_coverage["coverage_mode"],
            "coverage_vocabulary_digest": legacy_coverage["coverage_vocabulary_digest"],
            "proposal_id": legacy_coverage["proposal_id"] + "-v4",
            "candidate_status": "candidate_not_approved",
            "records": migrated_records,
        }
    return model, coverage


def _validate_candidate_model_for_execution_result(
    workspace: Path, product_id: str, model_path: Path
) -> dict[str, Any]:
    schema = load_json(model_path).get("schema")
    if schema == "kym_tco.tco_model.v2":
        return _validate_tco_v2_model(workspace, product_id, model_path)
    if schema == "kym_tco.tco_model.v3":
        return _validate_tco_v3_model(workspace, product_id, model_path)
    if schema == "kym_tco.tco_model.v4":
        return _validate_tco_v4_model(workspace, product_id, model_path)
    return _validate_tco_model(workspace, product_id, model_path, require_receipts=False)


def validate_tco_execution_result(
    workspace: Path, product_id: str, model_path: Path, execution_result_path: Path
) -> dict[str, Any]:
    model = _validate_candidate_model_for_execution_result(workspace, product_id, model_path)
    result = load_json(execution_result_path); validate_document("tco_execution_result", result, execution_result_path)
    if result["product_id"] != product_id:
        raise ContractError("TCO execution result product_id 不一致")
    if result["source_tco_model_digest"] != digest_value(model):
        raise ContractError("TCO execution result source_tco_model_digest 已漂移")
    nodes = _nodes(model)
    node = nodes.get(result["m1_id"])
    if not node or node["node_kind"] != "m1":
        raise ContractError("TCO execution result m1_id 必须引用当前模型中的 M1")
    if result["effect_status"] not in EFFECT_STATUS_VALUES:
        raise ContractError("TCO execution result effect_status 必须使用 goal_effect_status_v1")
    return result


def _load_execution_results(
    workspace: Path, product_id: str, model_path: Path, paths: list[Path] | None
) -> list[dict[str, Any]]:
    results = [
        validate_tco_execution_result(workspace, product_id, model_path, path)
        for path in (paths or [])
    ]
    ids = [item["execution_result_id"] for item in results]
    if len(ids) != len(set(ids)):
        raise ContractError("TCO execution result id 不得重复")
    return results


def materialize_target_gap_m1s(
    model: dict[str, Any], gap_specs: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fail closed when a Target requirement needs an explicit candidate gap M1.

    This helper deliberately does not invent TSP semantics. The caller supplies a
    complete M1 candidate and parent; the function only materializes it when the
    referenced Target requirement has no M1 mapping. It never creates approval
    artifacts or changes candidate status.
    """
    result = copy.deepcopy(model)
    nodes = {item["id"]: item for item in result.get("nodes", [])}
    specs = {item.get("requirement_id"): item for item in gap_specs}
    if len(specs) != len(gap_specs) or None in specs:
        raise ContractError("Target gap M1 specs 必须使用唯一 requirement_id")
    traceability: list[dict[str, Any]] = []
    for profile in result.get("target_profiles", []):
        for requirement in profile.get("requirements", []):
            mapped_m1 = [node_id for node_id in requirement.get("node_ids", []) if nodes.get(node_id, {}).get("node_kind") == "m1"]
            if mapped_m1:
                traceability.append({"profile_id": profile["id"], "requirement_id": requirement["id"], "node_ids": mapped_m1, "materialized_gap": False})
                continue
            spec = specs.get(requirement["id"])
            if not spec:
                raise ContractError(f"Target requirement 缺少 M1 时必须提供 candidate gap M1 spec: {requirement['id']}")
            parent_id = spec.get("parent_id")
            node = copy.deepcopy(spec.get("node"))
            if not isinstance(node, dict) or node.get("node_kind") != "m1" or node.get("effect_status") != "not_executed":
                raise ContractError(f"Target gap M1 必须是完整、未执行的 candidate m1: {requirement['id']}")
            if not node.get("candidate_gap") or not parent_id or parent_id not in nodes:
                raise ContractError(f"Target gap M1 必须标记 candidate_gap 并引用存在的 parent: {requirement['id']}")
            node_id = node.get("id")
            if not node_id or node_id in nodes:
                raise ContractError(f"Target gap M1 id 必须新且唯一: {requirement['id']}")
            nodes[parent_id].setdefault("children", []).append(node_id)
            result["nodes"].append(node)
            nodes[node_id] = node
            requirement["node_ids"] = [*requirement.get("node_ids", []), node_id]
            traceability.append({"profile_id": profile["id"], "requirement_id": requirement["id"], "node_ids": [node_id], "materialized_gap": True})
    unused = sorted(set(specs) - {item["requirement_id"] for item in traceability})
    if unused:
        raise ContractError(f"Target gap M1 spec 未引用 requirement: {unused}")
    return result, traceability


def _validate_tco_model(
    workspace: Path, product_id: str, model_path: Path, *, require_receipts: bool
) -> dict[str, Any]:
    root = project_root(workspace, product_id)
    config = load_config(root)
    document = load_json(model_path)
    validate_document("tco_model", document, model_path)
    if document["product_id"] != product_id:
        raise ContractError("TCO product_id 不一致")
    if document["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO model 的 coverage vocabulary digest 已漂移")
    _validate_graph(document)
    nodes = _nodes(document)
    kym_path = root / "kym" / "revisions" / f"kym-{document['source_kym_revision']}.json"
    kym = load_json(kym_path)
    validate_document("kym", kym, kym_path)
    semantic_validate_kym(kym, root)
    closure_digest = source_closure_digest(kym, root)
    if require_receipts:
        source_kym_receipt = document.get("source_kym_receipt")
        if not source_kym_receipt:
            raise ContractError("approved TCO model 必须包含 source_kym_receipt")
        baseline_receipt_path = resolved_within(root, source_kym_receipt, "source_kym_receipt")
        verify_receipt(
            baseline_receipt_path,
            kym,
            product_id=product_id,
            approval_scope="kym_baseline",
            artifact_type="kym_revision",
            artifact_id=document["source_kym_revision"],
            revision=document["source_kym_revision"],
            source_closure_digest=closure_digest,
        )
    statement_ids = {item["id"] for item in kym["statements"]}
    for node in nodes.values():
        missing = sorted(set(node["source_statement_refs"]) - statement_ids)
        if missing:
            raise ContractError(f"TCO node {node['id']} 引用不存在 KYM statements: {missing}")
        if len(node["scope"]) != len(set(node["scope"])):
            raise ContractError(f"TCO node scope 不得重复: {node['id']}")
        if node["node_kind"] != "group" and not node["source_statement_refs"]:
            raise ContractError(f"非 group TCO node 必须引用 KYM statements: {node['id']}")
        if node["node_kind"] == "group" and not node["children"]:
            raise ContractError(f"group TCO node 必须包含 children: {node['id']}")
    targets = {item["id"]: item for item in kym["target_profiles"]}
    profiles = document["target_profiles"]
    if not profiles:
        raise ContractError("TCO model 至少需要一个 Target Profile")
    profile_ids = [item["id"] for item in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ContractError("TCO Target Profile id 必须唯一")
    for profile in profiles:
        target_id = profile["source_target_id"]
        if target_id not in targets:
            raise ContractError(f"TCO profile 引用不存在 Target: {target_id}")
        if require_receipts:
            target_receipt = profile.get("target_receipt")
            if not target_receipt:
                raise ContractError(f"approved TCO profile 必须包含 target_receipt: {profile['id']}")
            target_receipt_path = resolved_within(root, target_receipt, "target_receipt")
            verify_receipt(
                target_receipt_path,
                targets[target_id],
                product_id=product_id,
                approval_scope="target_profile",
                artifact_type="target_fragment",
                artifact_id=target_id,
                revision=document["source_kym_revision"],
                source_closure_digest=closure_digest,
            )
        if profile["claim_ceiling"] != targets[target_id]["claim_ceiling"]:
            label = "approved Target" if require_receipts else "candidate Target"
            raise ContractError(f"TCO profile claim ceiling 与 {label} 不一致: {profile['id']}")
        requirement_ids: set[str] = set()
        mapped_source_requirements: set[str] = set()
        for requirement in profile["requirements"]:
            if requirement["id"] in requirement_ids:
                raise ContractError(f"profile requirement id 重复: {requirement['id']}")
            requirement_ids.add(requirement["id"])
            source_requirement_ref = requirement["source_requirement_ref"]
            if source_requirement_ref in mapped_source_requirements:
                raise ContractError(
                    f"profile source requirement 重复映射: {profile['id']}: {source_requirement_ref}"
                )
            mapped_source_requirements.add(source_requirement_ref)
            if source_requirement_ref not in targets[target_id]["requirement_refs"]:
                label = "approved Target" if require_receipts else "candidate Target"
                raise ContractError(
                    f"profile 映射了 {label} 之外的 requirement: {source_requirement_ref}"
                )
            missing_nodes = sorted(set(requirement["node_ids"]) - set(nodes))
            if missing_nodes:
                raise ContractError(f"profile requirement 引用不存在 nodes: {missing_nodes}")
            if len(requirement["node_ids"]) != len(set(requirement["node_ids"])):
                raise ContractError(
                    f"profile requirement node_ids 不得重复: {profile['id']}: {requirement['id']}"
                )
            group_nodes = sorted(
                node_id
                for node_id in set(requirement["node_ids"])
                if nodes[node_id]["node_kind"] == "group"
            )
            if group_nodes:
                raise ContractError(
                    f"profile requirement 不得把 group node 计入能力分母: "
                    f"{profile['id']}: {requirement['id']}: {group_nodes}"
                )
            state_rank(config, requirement["target_coverage_state"])
            required_scope = set(requirement.get("required_scope", []))
            available_scope = {scope for node_id in requirement["node_ids"] for scope in nodes[node_id]["scope"]}
            if required_scope - available_scope:
                raise ContractError(f"profile requirement mandatory Scope 不属于映射 M1: {profile['id']}: {requirement['id']}")
            if requirement.get("minimum_effective_state"):
                state_rank(config, requirement["minimum_effective_state"])
        expected_requirements = set(targets[target_id]["requirement_refs"])
        if mapped_source_requirements != expected_requirements:
            label = "approved Target" if require_receipts else "candidate Target"
            raise ContractError(
                f"profile 必须精确映射 {label} requirements；missing={sorted(expected_requirements-mapped_source_requirements)}, extra={sorted(mapped_source_requirements-expected_requirements)}"
            )
    return document


def _path_allowed(workspace: Path, config: dict[str, Any], ref: str) -> Path:
    target = resolved_within(workspace, ref, "evidence ref")
    roots = config.get("evidence_roots", [])
    if roots:
        allowed = False
        for root_ref in roots:
            evidence_root = resolved_within(workspace, root_ref, "evidence root")
            if target == evidence_root or evidence_root in target.parents:
                allowed = True
                break
        if not allowed:
            raise ContractError(f"evidence ref 不在允许 roots 中: {ref}")
    if not target.exists():
        raise ContractError(f"evidence ref 不存在: {ref}")
    return target


def validate_coverage_model(
    workspace: Path, product_id: str, model_path: Path, coverage_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    if load_json(model_path).get("schema") in {"kym_tco.tco_model.v2", "kym_tco.tco_model.v3", "kym_tco.tco_model.v4"} or load_json(coverage_path).get("schema") in {"kym_tco.tco_coverage_model.v2", "kym_tco.tco_coverage_model.v3"}:
        raise ContractError("TCO v2 candidate_not_approved Coverage 必须使用 validate-candidate-coverage；普通 validate-coverage 不创建或读取批准态")
    return _validate_coverage_model(
        workspace, product_id, model_path, coverage_path, require_model_receipts=True
    )


def validate_candidate_coverage_model(
    workspace: Path, product_id: str, model_path: Path, coverage_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage_schema = load_json(coverage_path).get("schema")
    if coverage_schema == "kym_tco.tco_coverage_model.v3":
        return _validate_tco_v4_coverage(workspace, product_id, model_path, coverage_path)
    if coverage_schema == "kym_tco.tco_coverage_model.v2":
        return _validate_tco_v2_coverage(workspace, product_id, model_path, coverage_path)
    return _validate_coverage_model(
        workspace, product_id, model_path, coverage_path, require_model_receipts=False
    )


def _validate_tco_v2_coverage(workspace: Path, product_id: str, model_path: Path, coverage_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = project_root(workspace, product_id); config = load_config(root)
    model_schema = load_json(model_path).get("schema")
    if model_schema == "kym_tco.tco_model.v3":
        model = _validate_tco_v3_model(workspace, product_id, model_path)
    else:
        model = _validate_tco_v2_model(workspace, product_id, model_path)
    coverage = load_json(coverage_path)
    validate_document("coverage_v2", coverage, coverage_path)
    if coverage["product_id"] != product_id or coverage["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO v2 Coverage product 或 vocabulary 不一致")
    if coverage["source_tco_model_digest"] != digest_value(model):
        raise ContractError("TCO v2 Coverage source model digest 已漂移")
    nodes = _nodes(model); counted_ids = {key for key, value in nodes.items() if value["node_kind"] in {"m1", "policy"}}
    records = {item["tco_node_id"]: item for item in coverage["records"]}
    if len(records) != len(coverage["records"]):
        raise ContractError("TCO v2 Coverage record node id 必须唯一")
    if coverage["coverage_mode"] == "not_evaluated":
        if records:
            raise ContractError("TCO v2 not_evaluated mode 不得声明 Coverage records")
        return model, coverage
    if set(records) != counted_ids:
        raise ContractError(
            f"TCO v2 Coverage 必须精确覆盖所有 m1/policy，且不得覆盖 mc；missing={sorted(counted_ids-set(records))}, extra={sorted(set(records)-counted_ids)}"
        )
    for node_id, record in records.items():
        state_rank(config, record["coverage_state"])
        if record["capability_lifecycle_state"] not in {"contract_defined", "implemented", "unresolved"}:
            raise ContractError("未知 capability_lifecycle_state")
        declared_scopes = [item["scope"] for item in record["scope_coverage"]]
        if len(declared_scopes) != len(set(declared_scopes)) or set(declared_scopes) != set(nodes[node_id]["scope"]):
            raise ContractError(f"TCO v2 Scope Coverage 必须精确匹配 node scope: {node_id}")
        scope_by_name = {item["scope"]: item["coverage_state"] for item in record["scope_coverage"]}
        scope_states = [scope_by_name[scope] for scope in nodes[node_id].get("mandatory_scope_items", nodes[node_id]["scope"])]
        for state in scope_states:
            state_rank(config, state)
        direct = record["coverage_state"]
        derived_effective = min([direct, *scope_states], key=lambda state: state_rank(config, state))
        peak = record.get("evidence_peak_state", direct)
        effective = record.get("effective_state", derived_effective)
        claim = record.get("claim_state", effective)
        for state in (peak, effective, claim):
            state_rank(config, state)
        if state_rank(config, peak) < state_rank(config, direct):
            raise ContractError(f"TCO v2 evidence_peak_state 不得低于 direct coverage_state: {node_id}")
        if effective != derived_effective:
            raise ContractError(f"TCO v2 effective_state 必须是 direct 与全部 mandatory Scope 的最低状态: {node_id}")
        if state_rank(config, claim) > state_rank(config, effective):
            raise ContractError(f"TCO v2 claim_state 不得高于 effective_state: {node_id}")
        breadth = record.get("breadth_coverage", {})
        for state in breadth.values():
            state_rank(config, state)
        breadth_evidence = record.get("breadth_evidence", [])
        breadth_ids = [item["id"] for item in breadth_evidence]
        if len(breadth_ids) != len(set(breadth_ids)):
            raise ContractError(f"TCO v2 breadth_evidence id 不得重复: {node_id}")
        evidence_refs = {str(item.get("ref", "")) for item in record.get("evidence", [])}
        if any(item["evidence_ref"] not in evidence_refs for item in breadth_evidence):
            raise ContractError(f"TCO v2 breadth_evidence 必须绑定 record evidence_ref: {node_id}")
        required_counts = {
            "single_trial": lambda rows: len(rows) >= 1,
            "single_repo": lambda rows: len({item["repo_id"] for item in rows}) >= 1,
            "known_family": lambda rows: bool({item.get("family") for item in rows if item.get("family")} ),
            "multi_repo": lambda rows: len({item["repo_id"] for item in rows}) >= 2,
            "cross_language": lambda rows: len({item["language"] for item in rows}) >= 2 and len({item["repo_id"] for item in rows}) >= 2,
            "arbitrary_attempt": lambda rows: any(item["attempt_class"] == "arbitrary" for item in rows) and len({item["repo_id"] for item in rows}) >= 2,
        }
        for level, predicate in required_counts.items():
            if state_rank(config, breadth.get(level, "not_started")) > state_rank(config, "not_started") and not predicate(breadth_evidence):
                raise ContractError(f"TCO v2 breadth_coverage.{level} 缺少相称、已绑定的独立 breadth_evidence: {node_id}")
    return model, coverage


def _count_record_evidence(workspace: Path, config: dict[str, Any], node_id: str, record: dict[str, Any]) -> int:
    commands_by_id = {item["id"]: item for item in config["verification_commands"]}
    counted_evidence = 0
    for evidence in record.get("evidence", []):
        if evidence["kind"] in PATH_EVIDENCE_KINDS:
            _path_allowed(workspace, config, evidence["ref"])
            counted_evidence += 1
        elif evidence["kind"] == "command":
            command_id = evidence.get("verification_command_id") or evidence["ref"]
            if command_id not in commands_by_id:
                raise ContractError(f"Coverage command 未登记白名单: {command_id}")
            if node_id not in commands_by_id[command_id]["node_ids"]:
                raise ContractError(
                    f"Coverage command 未覆盖当前 TCO node: command={command_id}, node={node_id}"
                )
            counted_evidence += 1
    return counted_evidence


def _validate_breadth_coverage(config: dict[str, Any], record: dict[str, Any], node_id: str, *, label: str) -> None:
    breadth = record.get("breadth_coverage", {})
    for state in breadth.values():
        state_rank(config, state)
    breadth_evidence = record.get("breadth_evidence", [])
    breadth_ids = [item["id"] for item in breadth_evidence]
    if len(breadth_ids) != len(set(breadth_ids)):
        raise ContractError(f"{label} breadth_evidence id 不得重复: {node_id}")
    evidence_refs = {str(item.get("ref", "")) for item in record.get("evidence", [])}
    if any(item["evidence_ref"] not in evidence_refs for item in breadth_evidence):
        raise ContractError(f"{label} breadth_evidence 必须绑定 record evidence_ref: {node_id}")
    required_counts = {
        "single_trial": lambda rows: len(rows) >= 1,
        "single_repo": lambda rows: len({item["repo_id"] for item in rows}) >= 1,
        "known_family": lambda rows: bool({item.get("family") for item in rows if item.get("family")}),
        "multi_repo": lambda rows: len({item["repo_id"] for item in rows}) >= 2,
        "cross_language": lambda rows: len({item["language"] for item in rows}) >= 2 and len({item["repo_id"] for item in rows}) >= 2,
        "arbitrary_attempt": lambda rows: any(item["attempt_class"] == "arbitrary" for item in rows) and len({item["repo_id"] for item in rows}) >= 2,
    }
    for level, predicate in required_counts.items():
        if state_rank(config, breadth.get(level, "not_started")) > state_rank(config, "not_started") and not predicate(breadth_evidence):
            raise ContractError(f"{label} breadth_coverage.{level} 缺少相称、已绑定的独立 breadth_evidence: {node_id}")


def _coverage_requirement_completeness(
    node: dict[str, Any],
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    explicit = {
        item.get("requirement_id"): item.get("coverage_state", "unknown")
        for item in (record or {}).get("requirement_coverage", [])
    }
    categories: dict[str, dict[str, Any]] = {}
    all_rows: list[tuple[dict[str, Any], str]] = []
    for category in V4_REQUIREMENT_CATEGORIES:
        requirements = [
            item
            for item in node.get("coverage_requirements", [])
            if item.get("category") == category
        ]
        states = [explicit.get(item["id"], "unknown") for item in requirements]
        missing = [
            item["id"]
            for item, state in zip(requirements, states)
            if state == "unknown"
        ]
        categories[category] = {
            "known": len(requirements) - len(missing),
            "total": len(requirements),
            "missing_requirement_ids": missing,
            "state_counts": {
                state: states.count(state)
                for state in sorted(set(states))
            },
        }
        all_rows.extend(zip(requirements, states))
    overall_missing = [
        item["id"]
        for item, state in all_rows
        if state == "unknown"
    ]
    return {
        "overall": {
            "known": len(all_rows) - len(overall_missing),
            "total": len(all_rows),
            "missing_requirement_ids": overall_missing,
        },
        "categories": categories,
    }


def _validate_tco_v4_coverage(workspace: Path, product_id: str, model_path: Path, coverage_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = project_root(workspace, product_id); config = load_config(root)
    model = _validate_tco_v4_model(workspace, product_id, model_path)
    coverage = load_json(coverage_path)
    validate_document("coverage_v3", coverage, coverage_path)
    if coverage["product_id"] != product_id or coverage["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("TCO v4 Coverage product 或 vocabulary 不一致")
    if coverage["source_tco_model_digest"] != digest_value(model):
        raise ContractError("TCO v4 Coverage source model digest 已漂移")
    nodes = _nodes(model)
    counted_ids = {
        key
        for key, value in nodes.items()
        if value["node_kind"] in {"m1", "policy"}
    }
    records = {item["tco_node_id"]: item for item in coverage["records"]}
    if len(records) != len(coverage["records"]):
        raise ContractError("TCO v4 Coverage record node id 必须唯一")
    if coverage["coverage_mode"] == "not_evaluated":
        if records:
            raise ContractError("TCO v4 not_evaluated mode 不得声明 Coverage records")
        return model, coverage
    if set(records) != counted_ids:
        raise ContractError(
            f"TCO v4 Coverage 必须精确覆盖所有 m1/policy，且不得覆盖 mc；missing={sorted(counted_ids-set(records))}, extra={sorted(set(records)-counted_ids)}"
        )
    for node_id, record in records.items():
        node = nodes[node_id]
        for state in (record["coverage_state"], record["evidence_peak_state"], record["effective_state"], record["claim_state"]):
            state_rank(config, state)
        _validate_v4_lifecycle_record(record, node_id)
        requirement_ids = [item["id"] for item in node.get("coverage_requirements", [])]
        declared_ids = [item["requirement_id"] for item in record["requirement_coverage"]]
        if len(declared_ids) != len(set(declared_ids)) or set(declared_ids) != set(requirement_ids):
            raise ContractError(f"TCO v4 requirement_coverage 必须精确匹配 node coverage_requirements: {node_id}")
        if set(record["missing_coverage_requirement_ids"]) - set(requirement_ids):
            raise ContractError(f"TCO v4 missing_coverage_requirement_ids 必须引用 node coverage_requirements: {node_id}")
        req_by_id = {item["requirement_id"]: item for item in record["requirement_coverage"]}
        for item in record["requirement_coverage"]:
            state_rank(config, item["coverage_state"])
        evidence_refs = {str(item.get("ref", "")) for item in record.get("evidence", [])}
        bad_requirement_refs = sorted(
            ref
            for item in record["requirement_coverage"]
            for ref in item.get("evidence_refs", [])
            if ref not in evidence_refs
        )
        if bad_requirement_refs:
            raise ContractError(f"TCO v4 requirement_coverage evidence_refs 必须绑定 record evidence.ref: {node_id}: {bad_requirement_refs}")
        direct = record["coverage_state"]
        mandatory_states = [
            req_by_id[requirement_id]["coverage_state"]
            for requirement_id in node.get("mandatory_coverage_requirement_ids", [])
        ]
        derived_effective = min([direct, *mandatory_states], key=lambda state: state_rank(config, state))
        if state_rank(config, record["evidence_peak_state"]) < state_rank(config, direct):
            raise ContractError(f"TCO v4 evidence_peak_state 不得低于 direct coverage_state: {node_id}")
        if record["effective_state"] != derived_effective:
            raise ContractError(f"TCO v4 effective_state 必须是 direct 与全部 mandatory Coverage requirement 的最低状态: {node_id}")
        if state_rank(config, record["claim_state"]) > state_rank(config, record["effective_state"]):
            raise ContractError(f"TCO v4 claim_state 不得高于 effective_state: {node_id}")
        _validate_breadth_coverage(config, record, node_id, label="TCO v4")
        declared_states = [direct, *[item["coverage_state"] for item in record["requirement_coverage"]]]
        if (
            max(state_rank(config, state) for state in declared_states)
            > state_rank(config, "not_started")
            and _count_record_evidence(workspace, config, node_id, record) == 0
        ):
            raise ContractError(f"非空 Coverage state 缺少可计数 evidence: {node_id}")
    return model, coverage


_BREADTH_LEVELS = (
    "single_trial", "single_repo", "known_family", "multi_repo", "cross_language", "arbitrary_attempt"
)


def _candidate_record_states(
    record: dict[str, Any],
    config: dict[str, Any],
    mandatory_scope: list[str] | None = None,
    mandatory_requirements: list[str] | None = None,
) -> dict[str, str]:
    """Project old and new candidate records without rewriting historical proposals."""
    direct = record.get("coverage_state", "unknown")
    if "requirement_coverage" in record:
        explicit = {item.get("requirement_id"): item.get("coverage_state", "unknown") for item in record.get("requirement_coverage", [])}
        requirements = [explicit.get(requirement_id, "unknown") for requirement_id in mandatory_requirements] if mandatory_requirements is not None else list(explicit.values())
        effective = min([direct, *requirements], key=lambda state: state_rank(config, state))
    else:
        explicit = {item.get("scope"): item.get("coverage_state", "unknown") for item in record.get("scope_coverage", [])}
        scopes = [explicit.get(scope, "unknown") for scope in mandatory_scope] if mandatory_scope is not None else list(explicit.values())
        effective = min([direct, *scopes], key=lambda state: state_rank(config, state))
    return {
        "direct_state": direct,
        "evidence_peak_state": record.get("evidence_peak_state", direct),
        "effective_state": record.get("effective_state", effective),
        "claim_state": record.get("claim_state", record.get("effective_state", effective)),
    }


def _candidate_lifecycle_satisfies(actual: str | None, minimum: str) -> bool:
    if actual == "deprecated" or minimum == "deprecated":
        return actual == minimum
    order = {"unresolved": 0, "draft": 0, "contract_defined": 1, "implemented": 2, "maintained": 3}
    return order.get(actual or "", -1) >= order.get(minimum, 10)


def _candidate_validation_posture_satisfies(actual: str | None, minimum: str | None) -> bool:
    order = {"not_validated": 0, "producer_checked": 1, "independently_validated": 2}
    required = minimum or "not_validated"
    return order.get(actual or "not_validated", -1) >= order.get(required, 10)


def _candidate_requirement_result(
    requirement: dict[str, Any], node_records: list[dict[str, Any]], goal: dict[str, Any], config: dict[str, Any], mandatory_scopes: dict[str, list[str]]
) -> dict[str, Any]:
    required_state = requirement.get("minimum_effective_state", goal["minimum_coverage_state"])
    required_scope = set(requirement.get("required_scope", []))
    required_breadth = requirement.get("required_breadth")
    node_states = [_candidate_record_states(record, config, mandatory_scopes.get(record.get("tco_node_id", ""))) for record in node_records]
    # A legacy proposal may list informational gaps without a requirement-level
    # mandatory Scope declaration; only the latter is a deterministic Met blocker.
    missing_scope = sorted({item for record in node_records for item in record.get("missing_scope_items", []) if required_scope and item in required_scope})
    breadth_gaps = []
    scope_gaps = []
    for record in node_records:
        explicit = {item.get("scope"): item.get("coverage_state", "unknown") for item in record.get("scope_coverage", [])}
        for scope in required_scope:
            if state_rank(config, explicit.get(scope, "unknown")) < state_rank(config, required_state):
                scope_gaps.append(scope)
        if required_breadth:
            current = record.get("breadth_coverage", {}).get(required_breadth, "not_started")
            if state_rank(config, current) < state_rank(config, required_state):
                breadth_gaps.append(required_breadth)
    met = (
        bool(node_records)
        and not requirement["missing_items"]
        and not missing_scope
        and not scope_gaps
        and not breadth_gaps
        and all(
            state_rank(config, state["effective_state"]) >= state_rank(config, required_state)
            and state_rank(config, state["claim_state"]) >= state_rank(config, required_state)
            and _candidate_lifecycle_satisfies(
                record.get("capability_lifecycle_state"),
                goal["minimum_lifecycle_state"],
            )
            for record, state in zip(node_records, node_states)
        )
    )
    current = min((state["effective_state"] for state in node_states), key=lambda state: state_rank(config, state), default="unknown")
    claim = min((state["claim_state"] for state in node_states), key=lambda state: state_rank(config, state), default="unknown")
    return {"id": requirement["id"], "title": requirement["title"], "met": met, "node_ids": requirement["node_ids"], "missing_items": requirement["missing_items"], "required_scope": sorted(required_scope), "required_breadth": required_breadth, "minimum_effective_state": required_state, "current_effective_state": current, "current_claim_state": claim, "missing_scope_items": missing_scope, "scope_gaps": sorted(set(scope_gaps)), "breadth_gaps": sorted(set(breadth_gaps))}


def _goal_missing_items(requirement: dict[str, Any], goal_id: str) -> list[str]:
    """Apply phased requirement notes only to the goal they describe.

    A synthesis requirement may be contract-complete while its later
    implementation/runtime goals remain open.  Keeping those notes in the
    source requirement is useful for the report, but treating every note as a
    universal blocker would make the contract goal incorrectly fail.
    Unprefixed legacy notes retain the historical all-goal blocking behavior.
    """
    items = list(requirement.get("missing_items", []))
    phased = [
        item for item in items
        if isinstance(item, str) and item.startswith("[") and "]" in item
    ]
    if not phased:
        return items
    prefix = f"[{goal_id}]"
    return [item for item in phased if item.startswith(prefix)]


def _candidate_v4_requirement_result(
    requirement: dict[str, Any],
    node_records: list[dict[str, Any]],
    goal: dict[str, Any],
    config: dict[str, Any],
    mandatory_requirements: dict[str, list[str]],
    node_requirement_ids: dict[str, list[str]],
) -> dict[str, Any]:
    required_state = requirement.get("minimum_coverage_state", requirement.get("minimum_effective_state", goal["minimum_coverage_state"]))
    required_lifecycle = requirement.get("minimum_lifecycle_state", goal["minimum_lifecycle_state"])
    required_validation = requirement.get("required_validation_posture", _goal_required_validation_posture(goal))
    goal_missing_items = _goal_missing_items(requirement, goal["id"])
    explicit_required = set(requirement.get("required_coverage_requirement_ids", []))
    required_breadth = requirement.get("required_breadth")
    node_states = [
        _candidate_record_states(
            record,
            config,
            mandatory_requirements=mandatory_requirements.get(record.get("tco_node_id", "")),
        )
        for record in node_records
    ]
    missing_requirements = []
    requirement_gaps = []
    breadth_gaps = []
    lifecycle_gaps = []
    validation_gaps = []
    for record in node_records:
        node_id = record.get("tco_node_id", "")
        available = set(node_requirement_ids.get(node_id, []))
        required_ids = sorted(explicit_required & available) if explicit_required else mandatory_requirements.get(node_id, [])
        explicit = {item.get("requirement_id"): item.get("coverage_state", "unknown") for item in record.get("requirement_coverage", [])}
        missing_requirements.extend(
            item
            for item in record.get("missing_coverage_requirement_ids", [])
            if item in required_ids
        )
        for requirement_id in required_ids:
            if state_rank(config, explicit.get(requirement_id, "unknown")) < state_rank(config, required_state):
                requirement_gaps.append(requirement_id)
        if required_breadth:
            current = record.get("breadth_coverage", {}).get(required_breadth, "not_started")
            if state_rank(config, current) < state_rank(config, required_state):
                breadth_gaps.append(required_breadth)
        if not _candidate_lifecycle_satisfies(record.get("capability_lifecycle_state"), required_lifecycle):
            lifecycle_gaps.append(f"{node_id}:{record.get('capability_lifecycle_state', 'unknown')}")
        if not _candidate_validation_posture_satisfies(record.get("validation_posture"), required_validation):
            validation_gaps.append(f"{node_id}:{record.get('validation_posture', 'not_validated')}")
    met = (
        bool(node_records)
        and not goal_missing_items
        and not missing_requirements
        and not requirement_gaps
        and not breadth_gaps
        and not lifecycle_gaps
        and not validation_gaps
        and all(
            state_rank(config, state["effective_state"]) >= state_rank(config, required_state)
            and state_rank(config, state["claim_state"]) >= state_rank(config, required_state)
            and _candidate_lifecycle_satisfies(
                record.get("capability_lifecycle_state"),
                required_lifecycle,
            )
            and _candidate_validation_posture_satisfies(
                record.get("validation_posture"),
                required_validation,
            )
            for record, state in zip(node_records, node_states)
        )
    )
    current = min((state["effective_state"] for state in node_states), key=lambda state: state_rank(config, state), default="unknown")
    claim = min((state["claim_state"] for state in node_states), key=lambda state: state_rank(config, state), default="unknown")
    current_validation = min(
        (record.get("validation_posture", "not_validated") for record in node_records),
        key=lambda item: VALIDATION_POSTURES.index(item) if item in VALIDATION_POSTURES else -1,
        default="not_validated",
    )
    return {
        "id": requirement["id"],
        "title": requirement["title"],
        "met": met,
        "node_ids": requirement["node_ids"],
        "missing_items": goal_missing_items,
        "required_coverage_requirement_ids": sorted(explicit_required),
        "required_breadth": required_breadth,
        "minimum_coverage_state": required_state,
        "minimum_effective_state": required_state,
        "minimum_lifecycle_state": required_lifecycle,
        "required_validation_posture": required_validation,
        "current_effective_state": current,
        "current_claim_state": claim,
        "current_validation_posture": current_validation,
        "missing_coverage_requirement_ids": sorted(set(missing_requirements)),
        "coverage_requirement_gaps": sorted(set(requirement_gaps)),
        "breadth_gaps": sorted(set(breadth_gaps)),
        "lifecycle_gaps": sorted(set(lifecycle_gaps)),
        "validation_gaps": sorted(set(validation_gaps)),
    }


def _validate_coverage_model(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    *,
    require_model_receipts: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = project_root(workspace, product_id)
    config = load_config(root)
    model = _validate_tco_model(
        workspace, product_id, model_path, require_receipts=require_model_receipts
    )
    coverage = load_json(coverage_path)
    validate_document("coverage", coverage, coverage_path)
    if coverage["product_id"] != product_id:
        raise ContractError("Coverage product_id 不一致")
    if coverage["coverage_mode"] != config["coverage_mode"]:
        raise ContractError("Coverage mode 与项目配置不一致")
    if coverage["coverage_vocabulary_digest"] != config["coverage_vocabulary_digest"]:
        raise ContractError("Coverage vocabulary digest 已漂移")
    source_model = resolved_within(root, coverage["source_tco_model"], "source_tco_model")
    if source_model.resolve() != model_path.resolve():
        raise ContractError("Coverage source_tco_model 与实际 model 不一致")
    if coverage["source_tco_model_digest"] != digest_value(model):
        raise ContractError("Coverage source_tco_model_digest 已漂移")
    nodes = _nodes(model)
    leaf_ids = {node_id for node_id, node in nodes.items() if node["node_kind"] != "group"}
    records = coverage["records"]
    record_by_id = {record["tco_node_id"]: record for record in records}
    if len(record_by_id) != len(records):
        raise ContractError("Coverage record node id 必须唯一")
    if config["coverage_mode"] == "not_evaluated":
        if records:
            raise ContractError("not_evaluated mode 不得声明 Coverage records")
        return model, coverage
    if set(record_by_id) != leaf_ids:
        raise ContractError(
            f"Coverage 必须精确覆盖所有非 group nodes；missing={sorted(leaf_ids-set(record_by_id))}, extra={sorted(set(record_by_id)-leaf_ids)}"
        )
    commands_by_id = {item["id"]: item for item in config["verification_commands"]}
    for node_id, record in record_by_id.items():
        state_rank(config, record["coverage_state"])
        node = nodes[node_id]
        scopes = [item["scope"] for item in record["scope_coverage"]]
        if len(scopes) != len(set(scopes)) or set(scopes) != set(node["scope"]):
            raise ContractError(f"Coverage scope 必须精确覆盖 TCO node scope: {node_id}")
        for item in record["scope_coverage"]:
            state_rank(config, item["coverage_state"])
        counted_evidence = 0
        for evidence in record["evidence"]:
            if evidence["kind"] in PATH_EVIDENCE_KINDS:
                _path_allowed(workspace, config, evidence["ref"])
                counted_evidence += 1
            elif evidence["kind"] == "command":
                command_id = evidence.get("verification_command_id") or evidence["ref"]
                if command_id not in commands_by_id:
                    raise ContractError(f"Coverage command 未登记白名单: {command_id}")
                if node_id not in commands_by_id[command_id]["node_ids"]:
                    raise ContractError(
                        f"Coverage command 未覆盖当前 TCO node: command={command_id}, node={node_id}"
                    )
                counted_evidence += 1
        declared_states = [record["coverage_state"]] + [
            item["coverage_state"] for item in record["scope_coverage"]
        ]
        if (
            max(state_rank(config, state) for state in declared_states)
            > state_rank(config, "not_started")
            and counted_evidence == 0
        ):
            raise ContractError(f"非空 Coverage state 缺少可计数 evidence: {node_id}")
    return model, coverage


def verify_coverage_approval(root: Path, coverage: dict[str, Any]) -> dict[str, Any]:
    receipt_path = root / "tco" / "approvals" / f"coverage-{coverage['proposal_id']}.approval.json"
    return verify_receipt(
        receipt_path,
        coverage,
        product_id=coverage["product_id"],
        approval_scope="coverage_delta",
        artifact_type="coverage_model",
        artifact_id=coverage["proposal_id"],
        revision=coverage["proposal_id"],
    )


def _effective_states(
    model: dict[str, Any], coverage: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    nodes = _nodes(model)
    records = {item["tco_node_id"]: item for item in coverage["records"]}
    direct: dict[str, str] = {}
    effective: dict[str, str] = {}

    def lowest(states: list[str]) -> str:
        return min(states, key=lambda state: state_rank(config, state))

    def visit(node_id: str) -> str:
        node = nodes[node_id]
        child_states = [visit(child) for child in node["children"]]
        if node["node_kind"] == "group":
            direct[node_id] = "unknown"
            state = lowest(child_states) if child_states else "unknown"
        else:
            record = records[node_id]
            direct[node_id] = record["coverage_state"]
            scope_states = [item["coverage_state"] for item in record["scope_coverage"]]
            aggregate = child_states + scope_states
            state = lowest(aggregate) if aggregate else record["coverage_state"]
        effective[node_id] = state
        return state

    visit(model["root"])
    return effective, direct


def _build_profile_results(
    model: dict[str, Any], effective: dict[str, str], config: dict[str, Any]
) -> list[dict[str, Any]]:
    profiles = []
    for profile in model["target_profiles"]:
        requirements = []
        for requirement in profile["requirements"]:
            states = [effective[node_id] for node_id in requirement["node_ids"]]
            current = min(states, key=lambda state: state_rank(config, state))
            met = all(
                state_rank(config, state) >= state_rank(config, requirement["target_coverage_state"])
                for state in states
            )
            requirements.append(
                {
                    **requirement,
                    "current_coverage_state": current,
                    "met": met,
                }
            )
        met_count = sum(bool(item["met"]) for item in requirements)
        profiles.append(
            {
                "id": profile["id"],
                "label": profile["label"],
                "description": profile.get("description", ""),
                "claim_ceiling": profile.get("claim_ceiling", ""),
                "requirement_count": len(requirements),
                "met_count": met_count,
                "gap_count": len(requirements) - met_count,
                "met": bool(requirements) and met_count == len(requirements),
                "requirements": requirements,
            }
        )
    return profiles


def build_report(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    verification_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if load_json(model_path).get("schema") in {"kym_tco.tco_model.v2", "kym_tco.tco_model.v3", "kym_tco.tco_model.v4"} or load_json(coverage_path).get("schema") in {"kym_tco.tco_coverage_model.v2", "kym_tco.tco_coverage_model.v3"}:
        raise ContractError("TCO v2 candidate_not_approved 必须使用 render-preview 或 compare-candidate-profiles；普通 report 不创建或读取批准态")
    return _build_report(
        workspace,
        product_id,
        model_path,
        coverage_path,
        verification_results,
        require_model_receipts=True,
        require_coverage_receipt=True,
        report_mode="approved",
    )


def build_candidate_report(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    verification_results: dict[str, Any] | None = None,
    execution_result_paths: list[Path] | None = None,
) -> dict[str, Any]:
    if load_json(coverage_path).get("schema") == "kym_tco.tco_coverage_model.v3":
        model, coverage = _validate_tco_v4_coverage(workspace, product_id, model_path, coverage_path)
        execution_results = _load_execution_results(workspace, product_id, model_path, execution_result_paths)
        config = load_config(project_root(workspace, product_id))
        profiles = []
        records = {item["tco_node_id"]: item for item in coverage["records"]}
        node_requirement_ids = {
            node["id"]: [item["id"] for item in node.get("coverage_requirements", [])]
            for node in model["nodes"]
        }
        mandatory_requirements = {
            node["id"]: node.get("mandatory_coverage_requirement_ids", [])
            for node in model["nodes"]
        }
        for profile in model["target_profiles"]:
            goals = []
            for goal in profile["coverage_goals"]:
                if coverage["coverage_mode"] == "not_evaluated":
                    requirements = [
                        {
                            "id": requirement["id"],
                            "title": requirement["title"],
                            "met": None,
                            "node_ids": requirement["node_ids"],
                            "missing_items": requirement["missing_items"],
                            "required_coverage_requirement_ids": requirement.get("required_coverage_requirement_ids", []),
                            "minimum_coverage_state": requirement.get("minimum_coverage_state", requirement.get("minimum_effective_state", goal["minimum_coverage_state"])),
                            "minimum_lifecycle_state": requirement.get("minimum_lifecycle_state", goal["minimum_lifecycle_state"]),
                            "required_validation_posture": requirement.get("required_validation_posture", _goal_required_validation_posture(goal)),
                            "required_breadth": requirement.get("required_breadth"),
                            "status": "not_evaluated",
                        }
                        for requirement in profile["requirements"]
                    ]
                    goals.append({"id": goal["id"], "minimum_coverage_state": goal["minimum_coverage_state"], "minimum_lifecycle_state": goal["minimum_lifecycle_state"], "required_validation_posture": _goal_required_validation_posture(goal), "status": "not_evaluated", "met_count": None, "gap_count": None, "requirements": requirements})
                    continue
                met = 0; requirements = []
                for requirement in profile["requirements"]:
                    node_records = [records.get(node_id, {}) for node_id in requirement["node_ids"]]
                    result = _candidate_v4_requirement_result(requirement, node_records, goal, config, mandatory_requirements, node_requirement_ids)
                    requirements.append(result); met += int(result["met"])
                goals.append({"id": goal["id"], "minimum_coverage_state": goal["minimum_coverage_state"], "minimum_lifecycle_state": goal["minimum_lifecycle_state"], "required_validation_posture": _goal_required_validation_posture(goal), "status": "Met" if met == len(requirements) else "Gap", "met_count": met, "gap_count": len(requirements)-met, "requirements": requirements})
            # Keep the profile denominator at the report surface as well as
            # under each goal.  The source model has one canonical requirement
            # list; each goal adds a different state result for that same list.
            # Omitting this projection made reader renderers display
            # ``Requirements = 0`` even though every goal had requirements.
            profiles.append({
                "id": profile["id"],
                "label": profile["label"],
                "claim_ceiling": profile["claim_ceiling"],
                "source_target_id": profile["source_target_id"],
                "requirements": profile["requirements"],
                "requirement_ids": [requirement["id"] for requirement in profile["requirements"]],
                "requirement_count": len(profile["requirements"]),
                "coverage_goals": goals,
            })
        node_rows = []
        for item in model["nodes"]:
            coverage_record = records.get(item["id"])
            row = {
                **item,
                "coverage": coverage_record,
                "coverage_requirement_completeness": _coverage_requirement_completeness(item, coverage_record),
            }
            if row["coverage"]:
                row.update(_candidate_record_states(row["coverage"], config, mandatory_requirements=mandatory_requirements[item["id"]]))
            node_rows.append(row)
        traceability = [
            {
                "profile_id": profile["id"],
                "requirement_id": requirement["id"],
                "node_ids": requirement["node_ids"],
                "required_coverage_requirement_ids": requirement.get("required_coverage_requirement_ids", []),
                "minimum_coverage_state": requirement.get("minimum_coverage_state", requirement.get("minimum_effective_state")),
                "minimum_lifecycle_state": requirement.get("minimum_lifecycle_state"),
                "required_validation_posture": requirement.get("required_validation_posture"),
                "required_breadth": requirement.get("required_breadth"),
            }
            for profile in model["target_profiles"]
            for requirement in profile["requirements"]
        ]
        report = {
            "schema": "kym_tco.tco_coverage_report.v3",
            "product_id": product_id,
            "report_mode": "candidate_not_approved",
            "coverage_mode": coverage["coverage_mode"],
            "generated_at": utc_now(),
            "coverage_vocabulary": config["coverage_vocabulary"],
            "coverage_vocabulary_digest": config["coverage_vocabulary_digest"],
            "source_tco_model_schema": model["schema"],
            "source_tco_model_digest": digest_value(model),
            "source_coverage_digest": digest_value(coverage),
            "effect_status_vocabulary_ref": model.get("effect_status_vocabulary_ref"),
            "execution_results": execution_results,
            "nodes": node_rows,
            "target_profiles": profiles,
            "target_to_m1_traceability": traceability,
            "verification_results": verification_results or {},
            "authority_note": "candidate_not_approved；v4 capability model 只定义能力边界和稳定 Coverage requirements；Coverage overlay 记录 repo evidence，execution result 只记录单次执行观察，三者不能互相提升或替代。",
        }
        report["projection_metadata"] = _candidate_projection_metadata(workspace, product_id)
        validate_document("coverage_report_v3", report)
        return report
    if load_json(coverage_path).get("schema") == "kym_tco.tco_coverage_model.v2":
        model, coverage = _validate_tco_v2_coverage(workspace, product_id, model_path, coverage_path)
        execution_results = _load_execution_results(workspace, product_id, model_path, execution_result_paths)
        config = load_config(project_root(workspace, product_id))
        profiles = []
        records = {item["tco_node_id"]: item for item in coverage["records"]}
        mandatory_scopes = {
            node["id"]: node.get("mandatory_scope_items", node["scope"])
            for node in model["nodes"]
        }
        for profile in model["target_profiles"]:
            goals = []
            for goal in profile["coverage_goals"]:
                if coverage["coverage_mode"] == "not_evaluated":
                    requirements = [
                        {
                            "id": requirement["id"],
                            "title": requirement["title"],
                            "met": None,
                            "node_ids": requirement["node_ids"],
                            "missing_items": requirement["missing_items"],
                            "status": "not_evaluated",
                        }
                        for requirement in profile["requirements"]
                    ]
                    goals.append({"id": goal["id"], "minimum_coverage_state": goal["minimum_coverage_state"], "minimum_lifecycle_state": goal["minimum_lifecycle_state"], "status": "not_evaluated", "met_count": None, "gap_count": None, "requirements": requirements})
                    continue
                met = 0; requirements = []
                for requirement in profile["requirements"]:
                    node_records = [records.get(node_id, {}) for node_id in requirement["node_ids"]]
                    result = _candidate_requirement_result(requirement, node_records, goal, config, mandatory_scopes)
                    requirements.append(result); met += int(result["met"])
                goals.append({"id": goal["id"], "minimum_coverage_state": goal["minimum_coverage_state"], "minimum_lifecycle_state": goal["minimum_lifecycle_state"], "status": "Met" if met == len(requirements) else "Gap", "met_count": met, "gap_count": len(requirements)-met, "requirements": requirements})
            profiles.append({"id": profile["id"], "label": profile["label"], "claim_ceiling": profile["claim_ceiling"], "source_target_id": profile["source_target_id"], "coverage_goals": goals})
        node_rows = []
        for item in model["nodes"]:
            row = {**item, "coverage": records.get(item["id"])}
            if row["coverage"]:
                row.update(_candidate_record_states(row["coverage"], config, mandatory_scopes[item["id"]]))
            node_rows.append(row)
        traceability = [{"profile_id": profile["id"], "requirement_id": requirement["id"], "node_ids": requirement["node_ids"]} for profile in model["target_profiles"] for requirement in profile["requirements"]]
        report = {"schema":"kym_tco.tco_coverage_report.v2", "product_id":product_id, "report_mode":"candidate_not_approved", "coverage_mode":coverage["coverage_mode"], "source_tco_model_schema":model["schema"], "source_tco_model_digest":digest_value(model), "source_coverage_digest":digest_value(coverage), "effect_status_vocabulary_ref":model.get("effect_status_vocabulary_ref"), "execution_results":execution_results, "nodes":node_rows, "target_profiles":profiles, "target_to_m1_traceability":traceability, "verification_results":verification_results or {}, "authority_note":"candidate_not_approved；迁移或验证不创建、复制或继承 approval receipt。coverage_state 是 direct evidence metadata；effective/claim state 与 breadth 缺口限制声明。execution result 是单次执行观察，不能自动提升 Coverage。"}
        validate_document("coverage_report_v2", report)
        return report
    return _build_report(
        workspace,
        product_id,
        model_path,
        coverage_path,
        verification_results,
        require_model_receipts=False,
        require_coverage_receipt=False,
        report_mode="candidate_not_approved",
    )


def _build_report(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    verification_results: dict[str, Any] | None,
    *,
    require_model_receipts: bool,
    require_coverage_receipt: bool,
    report_mode: str,
) -> dict[str, Any]:
    root = project_root(workspace, product_id)
    config = load_config(root)
    model, coverage = _validate_coverage_model(
        workspace,
        product_id,
        model_path,
        coverage_path,
        require_model_receipts=require_model_receipts,
    )
    if require_coverage_receipt:
        verify_coverage_approval(root, coverage)
    candidate_authority = (
        "candidate_not_approved: Candidate preview only; no baseline, Target, or Coverage receipt was read or created. "
        "Schema validation, verification, profile comparison, and rendering do not create accepted Coverage."
    )
    approved_authority = (
        "Derived evidence inventory only; profile status does not prove product, runtime, or production completion beyond its claim ceiling."
    )
    if config["coverage_mode"] == "not_evaluated":
        node_rows = []
        for node in model["nodes"]:
            row = copy.deepcopy(node)
            row["coverage_state"] = "unknown"
            row["direct_coverage_state"] = "unknown"
            node_rows.append(row)
        profiles = [
            {
                "id": profile["id"],
                "label": profile["label"],
                "status": "not_evaluated",
                "requirement_count": len(profile["requirements"]),
                "met_count": None,
                "gap_count": None,
                "claim_ceiling": profile["claim_ceiling"],
                "requirements": [],
            }
            for profile in model["target_profiles"]
        ]
        return {
            "schema": "kym_tco.tco_coverage_report.v1",
            "product_id": product_id,
            "report_mode": report_mode,
            "coverage_mode": "not_evaluated",
            "generated_at": utc_now(),
            "coverage_vocabulary": config["coverage_vocabulary"],
            "coverage_vocabulary_digest": config["coverage_vocabulary_digest"],
            "source_tco_model_digest": digest_value(model),
            "source_coverage_digest": digest_value(coverage),
            "nodes": node_rows,
            "target_profiles": profiles,
            "verification_results": verification_results or {},
            "authority_note": (
                candidate_authority + " Coverage not evaluated; this preview reports N/A, not 0/N or a gap assessment."
                if report_mode == "candidate_not_approved"
                else "Coverage not evaluated; this report does not represent 0/N implementation or a gap assessment."
            ),
        }
    effective, direct = _effective_states(model, coverage, config)
    records = {item["tco_node_id"]: item for item in coverage["records"]}
    node_rows = []
    for node in model["nodes"]:
        row = copy.deepcopy(node)
        row["coverage_state"] = effective[node["id"]]
        row["direct_coverage_state"] = direct[node["id"]]
        if node["id"] in records:
            row["coverage"] = records[node["id"]]
        node_rows.append(row)
    profiles = _build_profile_results(model, effective, config)
    for profile in profiles:
        profile["status"] = "met" if profile.pop("met") else "gaps"
    return {
        "schema": "kym_tco.tco_coverage_report.v1",
        "product_id": product_id,
        "report_mode": report_mode,
        "coverage_mode": "repo",
        "generated_at": utc_now(),
        "coverage_vocabulary": config["coverage_vocabulary"],
        "coverage_vocabulary_digest": config["coverage_vocabulary_digest"],
        "source_tco_model_digest": digest_value(model),
        "source_coverage_digest": digest_value(coverage),
        "nodes": node_rows,
        "target_profiles": profiles,
        "verification_results": verification_results or {},
        "authority_note": candidate_authority if report_mode == "candidate_not_approved" else approved_authority,
    }


def run_verification(
    workspace: Path, product_id: str, node_ids: list[str] | None
) -> dict[str, Any]:
    root = project_root(workspace, product_id)
    config = load_config(root)
    if config["coverage_mode"] == "not_evaluated":
        return {}
    selected = []
    requested = set(node_ids or [])
    for command in config["verification_commands"]:
        if requested and not (requested & set(command["node_ids"])):
            continue
        selected.append(command)
    if requested:
        known = {node_id for item in config["verification_commands"] for node_id in item["node_ids"]}
        unknown = requested - known
        if unknown:
            raise ContractError(f"没有 verification command 覆盖 requested nodes: {sorted(unknown)}")
    results: dict[str, Any] = {}
    for command in selected:
        cwd = resolved_within(workspace, command["cwd"], "verification cwd")
        allowed_env = set(command.get("allowed_env", []))
        env = {key: value for key, value in os.environ.items() if key in allowed_env}
        executable = Path(command["argv"][0])
        if not executable.is_absolute():
            raise ContractError(
                f"verification command 必须使用绝对 executable，不能依赖隐式 PATH: {command['id']}"
            )
        try:
            completed = subprocess.run(
                command["argv"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=int(command["timeout_seconds"]),
                shell=False,
                check=False,
            )
            status = "passed" if completed.returncode == 0 else "failed"
            results[command["id"]] = {
                "status": status,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-12000:],
                "stderr": completed.stderr[-12000:],
                "argv": command["argv"],
                "node_ids": command["node_ids"],
                "environment_keys": sorted(env),
            }
        except subprocess.TimeoutExpired as exc:
            results[command["id"]] = {
                "status": "timeout",
                "returncode": None,
                "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
                "argv": command["argv"],
                "node_ids": command["node_ids"],
                "environment_keys": sorted(env),
            }
    return results


def _html_node_anchor_map(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """Return deterministic, collision-safe anchors for rendered TCO nodes."""
    anchors: dict[str, str] = {}
    used: set[str] = set()
    for index, node in enumerate(nodes, start=1):
        node_id = str(node.get("id", ""))
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", node_id).strip("-") or f"node-{index}"
        anchor = f"node-{base}"
        suffix = 2
        while anchor in used:
            anchor = f"node-{base}-{suffix}"
            suffix += 1
        used.add(anchor)
        anchors[node_id] = anchor
    return anchors


def _candidate_projection_metadata(workspace: Path, product_id: str) -> dict[str, Any]:
    """Load presentation metadata without changing the report's authority axes."""
    root = project_root(workspace, product_id)
    # Prefer the current governed S117 source closure.  Keep the historical
    # r004 fallback only for older candidate packages; never advertise that
    # fallback as the current Skills/ledger identity.
    ledger_candidates = [
        root / "reports" / "normalized-source-ledger-s117.json",
        root / "reports" / "source-ledger-r004.json",
    ]
    ledger_path = next((path for path in ledger_candidates if path.exists()), ledger_candidates[-1])
    audit_candidates = [
        root / "reports" / "session-m1-reverse-audit-r005.json",
        root / "reports" / "session-m1-reverse-audit-r004.json",
    ]
    audit_path = next((path for path in audit_candidates if path.exists()), audit_candidates[-1])
    ledger = load_json(ledger_path) if ledger_path.exists() else {}
    audit = load_json(audit_path) if audit_path.exists() else {}
    ledger_inputs = ledger.get("inputs", []) if isinstance(ledger.get("inputs", []), list) else []
    ledger_units = [
        unit
        for item in ledger_inputs if isinstance(item, dict)
        for unit in item.get("closure", {}).get("units", [])
        if isinstance(unit, dict) and unit.get("disposition") == "included"
    ]
    unit_refs = [str(unit.get("ref", "")) for unit in ledger_units]
    source_roots = sorted({ref.split("/", 1)[0] for ref in unit_refs if ref})
    dashboard_file_count = sum(1 for ref in unit_refs if ref == "Dashboard" or ref.startswith("Dashboard/"))
    kb_file_count = sum(1 for ref in unit_refs if ref == "kb" or ref.startswith("kb/"))
    manifest_path = workspace / "governance/upstream_manifest.json"
    # Candidate reports are also useful for repo-only or conversation-only
    # inputs.  Missing governance metadata must be visible as ``unknown``;
    # it must not prevent a valid model/coverage pair from being rendered.
    manifest = load_json(manifest_path) if manifest_path.is_file() else {}
    skill_versions = [
        str(item.get("local_version") or "")
        for item in manifest.get("entries", [])
        if item.get("destination_path") in {
            ".codex/skills/build-kym",
            ".codex/skills/build-tco-coverage",
        }
    ]
    skills_version = "/".join(skill_versions) if skill_versions else "unknown"
    ledger_ref = str(ledger_path.relative_to(workspace)) if ledger_path.exists() else ""
    ledger_href = f"../{ledger_path.name}" if ledger_path.exists() else ""
    audit_ref = str(audit_path.relative_to(workspace)) if audit_path.exists() else ""
    audit_href = f"../{audit_path.name}" if audit_path.exists() else ""
    return {
        "skills_version": skills_version,
        "source_ledger_ref": ledger_ref,
        "source_ledger_href": ledger_href,
        "source_ledger_digest": ledger.get("ledger_digest", ""),
        "source_file_count": ledger.get("file_count", len(ledger_units)),
        "source_roots": ledger.get("source_roots", source_roots),
        "dashboard_file_count": ledger.get("dashboard_file_count", dashboard_file_count),
        "kb_file_count": ledger.get("kb_file_count", kb_file_count),
        "session_m1_audit_ref": audit_ref,
        "session_m1_audit_href": audit_href,
        "session_m1_audit_digest": audit.get("audit_digest", ""),
        "session_m1_rows": audit.get("session_rows", []),
        "session_m1_unresolved": audit.get("unresolved_session_ids", []),
    }


def _json_viewer_href(href: str) -> str:
    """Use the local UTF-8 viewer for JSON links in human-facing HTML."""
    return f"{href}.html" if href.lower().endswith(".json") else href


def _write_json_viewer(path: Path) -> Path | None:
    """Write a presentation-only UTF-8 wrapper without changing the JSON file."""
    if path.suffix.lower() != ".json" or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    viewer = path.with_name(f"{path.name}.html")
    title = html.escape(path.name)
    body = html.escape(content)
    viewer.write_text(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title>"
        "<style>body{margin:24px;background:#f8fafc;color:#172033;font:14px/1.6 Menlo,Consolas,monospace}"
        "main{max-width:1400px;margin:auto;background:#fff;border:1px solid #d9e0e8;border-radius:10px;padding:20px;overflow:auto}"
        "a{font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><main>"
        f"<p><a href=\"{html.escape(path.name)}\">返回原始 JSON</a></p><pre>{body}</pre>"
        "</main></body></html>\n",
        encoding="utf-8",
    )
    return viewer


def _html_layout_decorations(report: dict[str, Any]) -> dict[str, str]:
    """Build presentation-only sections from an already validated report."""
    nodes = report.get("nodes", [])
    node_by_id = {str(node.get("id", "")): node for node in nodes}
    anchors = _html_node_anchor_map(nodes)
    profiles = report.get("target_profiles", [])
    coverage_mode = str(report.get("coverage_mode", "not_evaluated"))
    report_mode = str(report.get("report_mode", "approved"))

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def list_text(values: Any) -> str:
        if not values:
            return '<span class="muted">无</span>'
        return "、".join(esc(value) for value in values)

    def node_kind(node: dict[str, Any]) -> str:
        return {"group": "Mc", "capability": "M1", "policy": "Policy"}.get(
            str(node.get("node_kind", "")), str(node.get("node_kind", ""))
        )

    children = {
        str(node.get("id", "")): [str(child) for child in node.get("children", [])]
        for node in nodes
    }
    child_ids = {child for values in children.values() for child in values}
    roots = [str(node.get("id", "")) for node in nodes if str(node.get("id", "")) not in child_ids]

    def tree_item(node_id: str) -> str:
        node = node_by_id.get(node_id)
        if not node:
            return ""
        child_markup = "".join(tree_item(child_id) for child_id in children.get(node_id, []))
        nested = f"<ul>{child_markup}</ul>" if child_markup else ""
        return (
            f'<li><a href="#{esc(anchors[node_id])}">'
            f'<span class="tree-kind">{esc(node_kind(node))}</span>'
            f'{esc(node.get("title", node_id))}<code>{esc(node_id)}</code></a>{nested}</li>'
        )

    tree_markup = (
        '<ul class="cap-tree">' + "".join(tree_item(root_id) for root_id in roots) + "</ul>"
        if roots
        else '<p class="empty">暂无 TCO 节点。</p>'
    )

    leaf_count = sum(node.get("node_kind") != "group" for node in nodes)
    group_count = sum(node.get("node_kind") == "group" for node in nodes)
    requirement_count = sum(len(profile.get("requirements", [])) for profile in profiles)
    records = [node.get("coverage") or {} for node in nodes]
    missing_rows = sum(
        len(record.get("missing_coverage_requirement_ids", record.get("missing_scope_items", [])))
        for record in records
    )
    verification = report.get("verification_results", {})
    failed_verification = sum(
        result.get("status") != "passed" for result in verification.values()
    )
    state_counts: dict[str, int] = {}
    for node in nodes:
        if node.get("node_kind") == "group":
            continue
        state = str(node.get("coverage_state", "unknown"))
        state_counts[state] = state_counts.get(state, 0) + 1
    state_cards = "".join(
        f'<div class="stat"><b>{count}</b><span>{esc(state)}</span></div>'
        for state, count in sorted(state_counts.items())
    )
    if not state_cards:
        state_cards = '<div class="stat"><b>N/A</b><span>Coverage state</span></div>'
    design_only_explanation = (
        '<div class="callout info"><strong>design_only（设计/合同）说明</strong>'
        '<p>这些节点已有 Skill/合同证据，但尚无对应语义生成动作的 host runtime witness；'
        '因此不会仅因输入包含 kym-tco skills，自动升级为 test_bound（测试边界）或 '
        'bounded_runtime（有界 Runtime）。</p></div>'
        if state_counts.get("design_only")
        else ""
    )

    overview_profile_rows: list[str] = []
    profile_rows: list[str] = []
    profile_cards: list[str] = []
    for index, profile in enumerate(profiles, start=1):
        met_count = profile.get("met_count")
        requirement_total = len(profile.get("requirements", []))
        met_text = "N/A" if met_count is None else f"{met_count}/{requirement_total}"
        status = profile.get("status", "not_evaluated")
        status_text = {
            "met": "达到当前 Profile",
            "not_evaluated": "未评估",
            "gaps": "仍有缺口",
        }.get(status, status)
        axis_summary = profile.get("goal_summary", "未提供")
        axis_blockers = sorted(
            {
                blocker
                for requirement in profile.get("requirements", [])
                for key in (
                    "missing_items",
                    "missing_scope_items",
                    "scope_gaps",
                    "breadth_gaps",
                    "coverage_requirement_gaps",
                    "lifecycle_gaps",
                    "validation_gaps",
                )
                for blocker in requirement.get(key, [])
            }
        )
        overview_profile_rows.append(
            "<tr>"
            f"<td><a href=\"#profile-{index}\">{esc(profile.get('label', profile.get('id', '')))}</a></td>"
            f"<td>{esc(met_text)}</td><td>{esc(status_text)}</td>"
            f"<td>{esc(axis_summary)}</td><td>{list_text(axis_blockers)}</td>"
            f"<td>{esc(profile.get('claim_ceiling', ''))}</td></tr>"
        )
        profile_rows.append(
            "<tr>"
            f"<td><a href=\"#profile-{index}\">{esc(profile.get('label', profile.get('id', '')))}</a></td>"
            f"<td>{esc(met_text)}</td><td>{esc(status_text)}</td>"
            f"<td>{esc(profile.get('claim_ceiling', ''))}</td></tr>"
        )
        requirement_rows = []
        for requirement in profile.get("requirements", []):
            met = requirement.get("met")
            met_label = "N/A" if met is None else "Met" if met else "Gap"
            requirement_rows.append(
                "<tr>"
                f"<td><code>{esc(requirement.get('id', ''))}</code></td>"
                f"<td>{esc(requirement.get('title', ''))}</td>"
                f"<td>{esc(met_label)}</td>"
                f"<td>{list_text(requirement.get('node_ids', []))}</td>"
                "</tr>"
            )
        requirement_table = (
            '<div class="table-wrap wide"><table><thead><tr>'
            "<th>Requirement</th><th>标题</th><th>状态</th><th>映射节点</th>"
            "</tr></thead><tbody>"
            + "".join(requirement_rows)
            + "</tbody></table></div>"
            if requirement_rows
            else '<p class="empty">无 Profile requirements。</p>'
        )
        profile_cards.append(
            f'<details class="profile-card" id="profile-{index}" open>'
            "<summary>"
            f'<div><span class="eyebrow">Target Profile {index}</span>'
            f'<h3>{esc(profile.get("label", profile.get("id", "")))}</h3>'
            f'<p>{esc(profile.get("description", ""))}</p></div>'
            f'<span class="badge info">{esc(met_text)} requirements</span>'
            '</summary><div class="details-body">'
            f'<div class="callout"><strong>声明上限</strong>'
            f'<p>{esc(profile.get("claim_ceiling", ""))}</p></div>'
            f'<div class="profile-goals"><span class="label">Goal axes</span>'
            f'<span>{esc(profile.get("goal_summary", "未提供"))}</span></div>'
            f"{requirement_table}</div></details>"
        )

    trace_rows = []
    for item in report.get("target_to_m1_traceability", []):
        trace_rows.append(
            "<tr>"
            f"<td>{esc(item.get('profile_id', ''))}</td>"
            f"<td><code>{esc(item.get('requirement_id', ''))}</code></td>"
            f"<td>{list_text(item.get('node_ids', []))}</td>"
            f"<td>{list_text(item.get('required_coverage_requirement_ids', []))}</td>"
            "</tr>"
        )
    traceability_markup = (
        '<div class="table-wrap wide"><table><thead><tr>'
        "<th>Profile</th><th>Requirement</th><th>Node IDs</th><th>Coverage requirements</th>"
        "</tr></thead><tbody>"
        + "".join(trace_rows)
        + "</tbody></table></div>"
        if trace_rows
        else '<p class="empty">暂无 Target-to-M1 traceability。</p>'
    )

    metadata_rows = "".join(
        f"<div class=\"k\">{esc(label)}</div><div>{esc(value)}</div>"
        for label, value in (
            ("KYM/TCO Skills version", report.get("projection_metadata", {}).get("skills_version", "unknown")),
            ("Report mode", report_mode),
            ("Coverage mode", coverage_mode),
            ("Report schema", report.get("schema", "")),
            ("Model digest", report.get("source_tco_model_digest", "")),
            ("Coverage digest", report.get("source_coverage_digest", "")),
            ("Generated", report.get("generated_at", "")),
        )
    )
    projection = report.get("projection_metadata", {})
    source_ledger_href = projection.get("source_ledger_href", "")
    audit_href = projection.get("session_m1_audit_href", "")
    source_links = []
    if source_ledger_href:
        source_links.append(f'<a href="{esc(_json_viewer_href(source_ledger_href))}">{esc(Path(source_ledger_href).name)}</a>')
    if audit_href:
        source_links.append(f'<a href="{esc(_json_viewer_href(audit_href))}">{esc(Path(audit_href).name)}</a>')
    source_closure_markup = (
        '<div class="callout info"><strong>完整来源闭合与反向审计</strong>'
        f'<p>当前候选纳入 {esc(projection.get("source_file_count", 0))} 个来源文件；Dashboard {esc(projection.get("dashboard_file_count", 0))} 个、KB {esc(projection.get("kb_file_count", 0))} 个；来源根：{list_text(projection.get("source_roots", []))}。'
        f'每个纳入的 Dashboard Session 已归入 M1、gap 或 release/governance 活动，未归位：{list_text(projection.get("session_m1_unresolved", []))}。'
        f' {" · ".join(source_links)}</p></div>'
        if projection
        else ""
    )
    session_audit_rows = "".join(
        "<tr>"
        f"<td><code>{esc(row.get('session_id', ''))}</code></td>"
        f"<td>{esc(row.get('status', ''))}</td>"
        f"<td>{esc(row.get('mapping_type', ''))}</td>"
        f"<td>{list_text(row.get('m1_ids', []))}</td>"
        f"<td>{esc(row.get('reason', ''))}</td></tr>"
        for row in projection.get("session_m1_rows", [])
    )
    session_audit_markup = (
        '<div class="table-wrap wide"><table><thead><tr><th>Session</th><th>Status</th><th>归位类型</th><th>M1</th><th>解释</th></tr></thead><tbody>'
        + session_audit_rows
        + "</tbody></table></div>"
        if session_audit_rows
        else ""
    )
    overview = (
        '<section class="doc-section" id="overview">'
        '<div class="section-head"><div><span class="eyebrow">Section 0</span>'
        '<h2>模型概览</h2></div><p>身份、状态、权威边界与规模摘要。</p></div>'
        '<div class="stats">'
        f'<div class="stat"><b>{group_count}</b><span>Mc / 功能域</span></div>'
        f'<div class="stat"><b>{leaf_count}</b><span>M1 / 规则节点</span></div>'
        f'<div class="stat"><b>{len(profiles)}</b><span>Target Profiles</span></div>'
        f'<div class="stat"><b>{requirement_count}</b><span>Requirements</span></div>'
        "</div>"
        '<div class="stats status-stats">'
        f"{state_cards}</div>"
        f"{design_only_explanation}"
        '<div class="panel overview-panel"><div class="meta-table">'
        f"{metadata_rows}</div>"
        f"{source_closure_markup}"
        f'<div class="callout"><strong>Session → M1 反向审计</strong><p>Session 状态不会自动提升 Coverage；release/governance 活动也不会被强行伪装成能力 M1。</p>{session_audit_markup}</div>'
        f'<div class="callout"><strong>权威边界</strong><p>{esc(report.get("authority_note", ""))}</p></div>'
        f'<div class="callout info"><strong>验证摘要</strong><p>{esc(f"已记录 {len(verification)} 个验证命令，失败 {failed_verification} 个；缺失 mandatory rows：{missing_rows}。" if verification else "本次报告未提供验证命令结果；Coverage 状态不会因渲染而自动提升。")}</p></div>'
        f'<div class="table-wrap"><table class="coverage-table"><thead><tr><th>Target profile</th><th>Met / requirement denominator</th><th>状态</th><th>Contract / Implementation Goal axes（coverage / lifecycle / validation）</th><th>Axis blockers / Breadth blockers</th><th>Claim ceiling</th></tr></thead><tbody>{"".join(overview_profile_rows)}</tbody></table></div>'
        "</div></section>"
    )
    profiles_section = (
        '<section class="doc-section" id="profiles">'
        '<div class="section-head"><div><span class="eyebrow">Section 2</span>'
        '<h2>Target Profiles</h2></div><p>保留 Profile、Goal axes、requirement 映射与声明上限。</p></div>'
        '<div class="panel"><div class="table-wrap"><table><thead><tr>'
        "<th>Target Profile</th><th>Met / Gap</th><th>状态</th><th>Claim ceiling</th>"
        "</tr></thead><tbody>"
        + "".join(profile_rows)
        + "</tbody></table></div></div>"
        + "".join(profile_cards)
        + "</section>"
    )
    traceability_section = (
        '<section class="doc-section" id="traceability">'
        '<div class="section-head"><div><span class="eyebrow">Section 4</span>'
        '<h2>Traceability</h2></div><p>集中展示已有 Target-to-M1 映射，不产生新的语义事实。</p></div>'
        f'<div class="panel">{traceability_markup}</div></section>'
    )
    sidebar = (
        '<nav class="sidebar" aria-label="TCO 报告目录"><strong>报告目录</strong>'
        '<a href="#overview">0. 模型概览</a><a href="#tree">1. 能力树全景</a>'
        '<a href="#profiles">2. Target Profiles</a><a href="#nodes">3. 节点目录</a>'
        '<a href="#traceability">4. Traceability</a></nav>'
    )
    tree_section = (
        '<section class="doc-section" id="tree">'
        '<div class="section-head"><div><span class="eyebrow">Section 1</span>'
        '<h2>能力树全景</h2></div><p>点击节点跳转到下方详细能力卡片。</p></div>'
        f'<div class="panel tree-panel">{tree_markup}</div></section>'
    )
    return {
        "overview": overview,
        "profiles": profiles_section,
        "traceability": traceability_section,
        "sidebar": sidebar,
        "tree": tree_section,
        "state_cards": state_cards,
    }


def _decorate_html_layout(rendered: str, report: dict[str, Any]) -> str:
    """Add the reference-style layout without changing the rendered report facts."""
    decorations = _html_layout_decorations(report)
    css = r"""
:root{--bg:#f5f7fa;--paper:#fff;--ink:#18202a;--muted:#66717f;--line:#d9e0e8;--accent:#205cc7;--accent-soft:#eaf1ff;--warn:#9a5b00;--warn-soft:#fff3d7;--ok:#1b6b46;--ok-soft:#e8f6ef;--shadow:0 10px 28px rgba(24,32,42,.07)}
html,body{overflow-x:hidden}body{background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header.hero{background:linear-gradient(135deg,#14213d 0%,#1e3f72 58%,#205cc7 100%);padding:34px max(24px,calc((100vw - 1400px)/2)) 30px;position:sticky;top:0;z-index:30;box-shadow:0 6px 20px rgba(20,33,61,.16)}
header.hero h1{font-size:clamp(26px,3.3vw,46px);line-height:1.08;margin:.25rem 0 .7rem;letter-spacing:-.03em} header.hero .meta{color:#dbe7ff;max-width:1100px}
.hero-eyebrow{color:#bcd2ff;text-transform:uppercase;letter-spacing:.08em;font-size:11px;font-weight:750;margin-bottom:4px}
.hero .preview-banner{position:static;display:block;margin:0 0 18px;padding:9px max(24px,calc((100vw - 1400px)/2));border-bottom:2px solid #fed7aa}
.layout{max-width:1400px;margin:0 auto;display:grid;grid-template-columns:250px minmax(0,1fr);gap:28px;padding:28px 24px 64px}.layout>main{min-width:0;max-width:none;padding:0}
nav.sidebar{position:sticky;top:20px;align-self:start;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow);max-height:calc(100vh - 40px);overflow:auto}nav.sidebar strong{display:block;margin:4px 8px 10px}nav.sidebar a{display:block;padding:7px 9px;border-radius:8px;color:var(--ink)}nav.sidebar a:hover{background:var(--accent-soft);text-decoration:none;color:var(--accent)}
.doc-section{scroll-margin-top:88px;margin-bottom:30px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:6px 0 14px}.section-head h2{font-size:28px;margin:0;letter-spacing:-.02em}.section-head p{color:var(--muted);margin:0;max-width:680px}.overview-panel,.panel,.profile-card{background:var(--paper);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.overview-panel,.panel{padding:22px;margin-bottom:16px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.stat{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:16px}.stat b{display:block;font-size:28px;line-height:1.1}.stat span{color:var(--muted);font-size:13px}.meta-table{display:grid;grid-template-columns:180px 1fr;border:1px solid var(--line);border-radius:10px;overflow:hidden}.meta-table div{padding:10px 12px;border-bottom:1px solid var(--line)}.meta-table div:nth-last-child(-n+2){border-bottom:0}.meta-table .k{background:#f7f9fc;font-weight:650;color:#485365}.callout{border-left:4px solid var(--warn);background:var(--warn-soft);padding:14px 16px;border-radius:8px;margin:14px 0}.callout.info{border-color:var(--accent);background:var(--accent-soft)}.callout p{margin:.35rem 0 0}
.profile-card{overflow:hidden;margin-bottom:14px}.profile-card>summary{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:18px 20px}.profile-card>summary h3{margin:0}.profile-card>summary p{margin:.3rem 0 0;color:var(--muted)}.profile-card .details-body{border-top:1px solid var(--line);padding:18px 20px 22px}.profile-goals{display:grid;grid-template-columns:180px 1fr;gap:12px;border:1px solid var(--line);border-radius:9px;padding:11px;background:#fbfcfe;margin:14px 0}.cap-tree,.cap-tree ul{list-style:none;margin:0;padding-left:20px;position:relative}.cap-tree{padding-left:0}.cap-tree ul:before{content:"";position:absolute;left:7px;top:0;bottom:8px;border-left:1px solid #cdd5df}.cap-tree li{position:relative;padding:4px 0 4px 13px}.cap-tree li:before{content:"";position:absolute;left:0;top:17px;width:10px;border-top:1px solid #cdd5df}.cap-tree>li{padding-left:0}.cap-tree>li:before{display:none}.cap-tree a{display:inline-flex;align-items:center;gap:7px;flex-wrap:wrap;color:var(--ink);padding:5px 8px;border-radius:7px}.cap-tree a:hover{background:var(--accent-soft);text-decoration:none}.tree-kind{font-size:10px;font-weight:800;color:var(--accent);background:var(--accent-soft);padding:2px 5px;border-radius:4px}.cap-tree code{font-size:11px}
.tco-node{background:var(--paper);border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:14px;padding-left:0;margin:0 0 14px;overflow:hidden;box-shadow:var(--shadow);scroll-margin-top:94px}.tco-node[data-node-kind="group"]{border-left-color:#64748b}.tco-node[data-node-kind="policy"]{border-left-color:#b45309}.tco-node>summary{padding:18px 20px;background:#fff;min-height:64px}.tco-node[data-depth="0"]>summary{background:#dbeafe}.tco-node[data-depth="1"]>summary{background:#e0f2fe}.tco-node[data-depth="2"]>summary{background:#f0fdfa}.tco-node[data-depth="3"]>summary{background:#fff}.tsp-grid{margin:0;padding:18px 20px 22px;border-top:1px solid var(--line)}.tsp-branch{border-left:0;padding-left:0;margin:0 0 12px}.tsp-branch>summary{width:fit-content;min-width:110px;padding:5px 9px;background:var(--accent-soft);color:#174ea6;border-radius:6px}.branch-body{margin:6px 0 12px;background:#fbfcfe;border:1px solid var(--line);border-radius:8px;padding:12px 14px}.subfeatures{margin:0 18px 14px}.subfeatures>summary{width:fit-content;padding:6px 10px;background:#e0f2fe;color:#155e75;border-radius:7px}.children{margin-left:0}.node-id{font-family:"SFMono-Regular",Consolas,monospace}.summary-meta{max-width:62%}.filter-result-summary{position:sticky;top:112px;z-index:15;box-shadow:var(--shadow)}
.node-catalog,.doc-section,.content{min-width:0;max-width:100%}.meta-table,.meta-table>div,.table-wrap{min-width:0;max-width:100%}.meta-table>div{overflow-wrap:anywhere;word-break:break-word}.coverage-panel,.branch-body{min-width:0;max-width:100%;overflow-x:auto}.coverage-kv,.semantic-kv,.evidence-list{min-width:0;max-width:100%}.coverage-panel dt,.coverage-panel dd,.coverage-panel li{min-width:0;overflow-wrap:anywhere;word-break:break-word}
.toolbar{position:sticky;top:74px;z-index:20;background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:12px;box-shadow:var(--shadow)}.toolbar input{flex:1;min-width:240px}.toolbar button:hover{background:var(--accent-soft);border-color:#8aa9e3}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px;margin:10px 0 18px}table{border-collapse:collapse;width:100%;min-width:720px;font-size:13px}table th,table td{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid var(--line)}table th{background:#eef2f7;font-size:12px;color:#435064}table tr:last-child td{border-bottom:0}.wide table{min-width:980px}
a:focus-visible,button:focus-visible,summary:focus-visible,select:focus-visible,input:focus-visible{outline:3px solid #f59e0b;outline-offset:2px}.muted,.empty{color:var(--muted)}
@media(max-width:980px){.layout{grid-template-columns:1fr;padding:20px 18px 50px}nav.sidebar{position:relative;top:0;max-height:none}.stats{grid-template-columns:repeat(2,1fr)}.summary-meta{max-width:100%}.toolbar{top:74px}}
@media(max-width:760px){.evidence-list li{grid-template-columns:1fr}.coverage-panel,.branch-body{overflow-x:auto}}
@media(max-width:580px){header.hero{padding:28px 18px 24px}.hero .preview-banner{margin:0 0 16px;padding:9px 0}.layout{padding:16px 12px 44px}.stats{grid-template-columns:1fr}.meta-table,.profile-goals{grid-template-columns:1fr}.meta-table .k{border-bottom:0}.section-head{display:block}.section-head p{margin-top:6px}.profile-card>summary,.tco-node>summary{flex-direction:column}.profile-card>summary .badge,.summary-meta{justify-content:flex-start;max-width:100%}.tsp-grid{padding:14px 14px 18px}.tco-node>summary{padding:15px 14px}.toolbar label{flex:1 1 160px;min-width:0;max-width:100%}.toolbar select,.toolbar input{min-width:0;max-width:100%;width:100%}}
@media print{body{background:#fff}header.hero{position:static;background:#fff!important;color:#000;padding:18px 0;border-bottom:2px solid #222;box-shadow:none}.hero .preview-banner,nav.sidebar,.toolbar,.filter-result-summary,button{display:none!important}.layout{display:block;padding:0}.overview-panel,.panel,.profile-card,.tco-node{box-shadow:none;break-inside:avoid}.tco-node,.profile-card{display:block}.tco-node:not([open])>.tsp-grid,.profile-card:not([open])>.details-body{display:block}.tco-node>summary:after,.profile-card>summary:after{display:none}}
"""
    rendered = rendered.replace("</style>", css + "</style>", 1)
    rendered = rendered.replace("<header>", '<header class="hero">' + '<div class="hero-eyebrow">KYM / TCO · 候选预览</div>', 1)
    rendered = re.sub(r'<div class="meta">.*?</div>', '', rendered, count=1, flags=re.S)
    rendered = rendered.replace("</header><main>", f"</header><div class=\"layout\">{decorations['sidebar']}<main class=\"content\">", 1)
    rendered = rendered.replace('<main class="content">', '<main class="content">' + decorations["overview"], 1)
    rendered = rendered.replace(
        '<div class="toolbar">',
        decorations["tree"] + decorations["profiles"] + decorations["traceability"]
        + '<section class="doc-section" id="nodes"><div class="section-head"><div><span class="eyebrow">Section 3</span><h2>节点目录</h2></div><p>按当前报告字段展示每个 Mc、能力或规则节点。</p></div><div class="node-catalog">'
        + '<div class="toolbar">',
        1,
    )
    rendered = rendered.replace(
        "</section></main>\n<script>",
        "</section></div></section></main></div>\n<script>",
        1,
    )
    rendered = rendered.replace(
        '<button type="button" onclick="expandAll()">全部展开</button>',
        '<button type="button" onclick="expandAll()">全部展开</button><button type="button" onclick="collapseAll()">全部折叠</button>',
        1,
    )
    rendered = rendered.replace(
        "function expandAll(){",
        "function collapseAll(){document.body.classList.remove('m-only');clearVisibleNodeDetails();document.querySelectorAll('details.tco-node,details.tsp-branch,details.subfeatures').forEach(detail=>detail.open=false);}\nfunction expandAll(){",
        1,
    )
    return rendered


def _render_html(report: dict[str, Any], integrity: dict[str, Any] | None = None) -> str:
    if report.get("schema") in {"kym_tco.tco_coverage_report.v2", "kym_tco.tco_coverage_report.v3"}:
        # Candidate reports use `mc`/`m1` kinds and per-goal requirement records,
        # while the full renderer uses the same data shape with richer presentation.
        # Normalize only for rendering; the machine report remains unchanged.
        report = copy.deepcopy(report)
        report.setdefault("generated_at", utc_now())
        is_requirement_denominator = report.get("schema") == "kym_tco.tco_coverage_report.v3"
        kind_map = {"mc": "group", "m1": "capability"}
        for node in report["nodes"]:
            node["node_kind"] = kind_map.get(node["node_kind"], node["node_kind"])
            coverage = node.get("coverage") or {}
            node["coverage_state"] = node.get("effective_state", coverage.get("effective_state", coverage.get("coverage_state", "unknown")))
            node["direct_coverage_state"] = node.get("direct_state", coverage.get("coverage_state", "unknown"))
            node["evidence_peak_state"] = node.get("evidence_peak_state", coverage.get("evidence_peak_state", node["direct_coverage_state"]))
            node["claim_state"] = node.get("claim_state", coverage.get("claim_state", node["coverage_state"]))
            if node["node_kind"] == "capability":
                semantic = node.get("decision_semantics", {})
                node.setdefault("capability_type", semantic.get("type", ""))
                node.setdefault("inputs", [])
                node.setdefault("outputs", [])
                node.setdefault("invariants", [])
        node_states = {
            node["id"]: node.get("coverage_state", "unknown")
            for node in report["nodes"]
        }
        for profile in report["target_profiles"]:
            goals = profile.get("coverage_goals", [])
            implementation_goal = next(
                (goal for goal in goals if goal.get("id") == "implementation"), None
            )
            primary_goal = implementation_goal or (goals[0] if goals else {})
            requirements_by_id: dict[str, dict[str, Any]] = {}
            for goal in goals:
                for requirement in goal.get("requirements", []):
                    # The preview's profile summary and node filters use the
                    # implementation goal when it exists; contract remains in
                    # the immutable v2 report and is not silently promoted.
                    if requirement["id"] not in requirements_by_id or goal is implementation_goal:
                        node_ids = requirement.get("node_ids", [])
                        requirement_states = [node_states.get(node_id, "unknown") for node_id in node_ids]
                        requirements_by_id[requirement["id"]] = {
                            **requirement,
                            "target_coverage_state": primary_goal.get(
                                "minimum_coverage_state", "unknown"
                            ),
                            "target_lifecycle_state": requirement.get(
                                "minimum_lifecycle_state",
                                primary_goal.get("minimum_lifecycle_state", "unknown"),
                            ),
                            "target_validation_posture": requirement.get(
                                "required_validation_posture",
                                primary_goal.get("required_validation_posture", "not_validated"),
                            ),
                            "current_coverage_state": requirement.get("current_effective_state", requirement_states[0] if len(set(requirement_states)) == 1 and requirement_states else "unknown"),
                            "met": requirement.get("met", False),
                        }
            requirements = list(requirements_by_id.values())
            profile["requirements"] = requirements
            profile["label"] = profile.get("label", profile["id"])
            if report["coverage_mode"] == "not_evaluated":
                profile["status"] = "not_evaluated"
            else:
                profile["status"] = "met" if all(
                    goal.get("status") == "Met" for goal in profile.get("coverage_goals", [])
                ) else "gaps"
            profile["requirement_count"] = len(requirements)
            profile["met_count"] = None if report["coverage_mode"] == "not_evaluated" else (implementation_goal.get("met_count", 0) if implementation_goal else 0)
            profile["gap_count"] = None if report["coverage_mode"] == "not_evaluated" else (implementation_goal.get("gap_count", len(requirements)) if implementation_goal else len(requirements))
            profile["claim_ceiling"] = profile.get("claim_ceiling", "candidate_not_approved")
            profile["goal_summary"] = "；".join(
                f"{goal.get('id')}: " + (
                    "N/A" if goal.get("met_count") is None else f"{goal.get('met_count')}/{len(goal.get('requirements', []))}"
                )
                + f"（coverage>={goal.get('minimum_coverage_state', 'unknown')}，lifecycle>={goal.get('minimum_lifecycle_state', 'unknown')}，validation>={goal.get('required_validation_posture', 'not_validated')}）"
                for goal in goals
            )
        report["coverage_denominator_kind"] = "coverage_requirement" if is_requirement_denominator else "scope"
        report["schema"] = "kym_tco.tco_coverage_report.v1"
    report_mode = report.get("report_mode", "approved")
    is_candidate = report_mode == "candidate_not_approved"
    nodes = {node["id"]: node for node in report["nodes"]}
    node_anchors = _html_node_anchor_map(report["nodes"])
    records = {
        node["id"]: node.get("coverage", {})
        for node in report["nodes"]
        if node.get("coverage")
    }
    execution_results_by_m1: dict[str, list[dict[str, Any]]] = {}
    for result in report.get("execution_results", []):
        execution_results_by_m1.setdefault(result.get("m1_id", ""), []).append(result)
    verification = report.get("verification_results", {})
    profiles = report.get("target_profiles", [])
    vocabulary_states = report.get("coverage_vocabulary", {}).get("states", [])
    state_labels = {item["id"]: item.get("label", item["id"]) for item in vocabulary_states}
    state_ranks = {item["id"]: int(item.get("rank", 0)) for item in vocabulary_states}
    state_labels.update(
        {
            "unknown": "未知",
            "not_started": "未开始",
            "design_only": "设计/合同",
            "schema_validator": "Schema/Validator",
            "test_bound": "测试边界",
            "bounded_runtime": "有界 Runtime",
            "integrated_runtime": "集成 Runtime",
        }
    )
    kind_labels = {"group": "功能域", "capability": "能力", "policy": "规则"}

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def state_label(state: str | None) -> str:
        return state_labels.get(state or "unknown", state or "未知")

    def state_class(state: str | None) -> str:
        token = re.sub(r"[^A-Za-z0-9_-]", "_", state or "unknown").replace("-", "_")
        return "state-" + token

    def badge(state: str | None, *, aggregate: bool = False) -> str:
        label = state_label(state)
        if aggregate:
            label = "聚合：" + label
        return f'<span class="coverage-badge {state_class(state)}">{esc(label)}</span>'

    def status_legend_markup() -> str:
        rows = [
            (
                "Coverage state 顺序",
                "unknown（未知） < not_started（未开始） < design_only（设计/合同） < "
                "schema_validator（Schema/Validator） < test_bound（测试边界） < "
                "bounded_runtime（有界 Runtime） < integrated_runtime（集成 Runtime）。"
                "test_bound 不表示实现完成；bounded_runtime 不表示 production ready。",
            ),
            (
                "Effective state",
                "v4 = direct coverage 与 mandatory Requirement rows 的最低状态；"
                "旧 v2/v3 = direct coverage 与 mandatory Scope rows 的最低状态。",
            ),
            (
                "Claim state",
                "当前允许对外声称的最高状态，不能高于 effective state；"
                "渲染、验证命令结果或 candidate evidence 不会自动提升它。",
            ),
            (
                "Lifecycle state",
                "只表达能力生命周期：draft / contract_defined / implemented / maintained / deprecated。"
                "maintained 需要 registration、owner 与 continuous gate；deprecated 是终止/替换分支，不是更高等级。",
            ),
            (
                "Validation posture",
                "只表达验证姿态：not_validated / producer_checked / independently_validated；"
                "independently_validated 不属于 lifecycle。",
            ),
            (
                "Resolution status",
                "只表达建模问题状态：resolved = 建模语义已收敛；"
                "unresolved = 能力表达仍不清；disputed = 解释仍有争议；不等于 evidence 完整。",
            ),
            (
                "Requirement row completeness / Coverage 跟踪行完整性",
                "模型声明的 coverage_requirements 是否都有 Coverage proposal 的 requirement_coverage row；"
                "它不表示 requirement 已满足、Coverage 已满足或验证通过。",
            ),
        ]
        return (
            '<section class="status-legend" aria-label="状态解释"><h2>状态解释</h2><dl>'
            + "".join(
                f"<dt>{esc(title)}</dt><dd>{esc(body)}</dd>"
                for title, body in rows
            )
            + "</dl></section>"
        )

    def list_markup(values: list[str], ordered: bool = False) -> str:
        if not values:
            return '<p class="empty">无</p>'
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{esc(value)}</li>" for value in values) + f"</{tag}>"

    def object_list_markup(items: list[Any]) -> str:
        values: list[str] = []
        for item in items:
            if isinstance(item, dict):
                parts = [str(item.get("id", ""))]
                if item.get("label") and item.get("label") != item.get("id"):
                    parts.append(str(item["label"]))
                if item.get("artifact_type"):
                    parts.append(f"artifact_type={item['artifact_type']}")
                if item.get("category"):
                    parts.append(f"category={item['category']}")
                if item.get("ref"):
                    parts.append(f"ref={item['ref']}")
                if item.get("claim"):
                    parts.append(f"claim={item['claim']}")
                if item.get("source_refs"):
                    parts.append("source_refs=" + ", ".join(str(ref) for ref in item["source_refs"]))
                if item.get("rationale"):
                    parts.append(f"rationale={item['rationale']}")
                if "required" in item:
                    parts.append(f"required={item['required']}")
                if "mandatory" in item:
                    parts.append(f"mandatory={item['mandatory']}")
                values.append("；".join(part for part in parts if part))
            else:
                values.append(str(item))
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
        return list_markup([f"{scope_item} ({node.get('scope_types', {}).get(scope_item, 'metadata')})" for scope_item in scope], ordered=True)

    def profile_mapping(node_id: str) -> tuple[list[str], list[str]]:
        profile_ids: list[str] = []
        requirement_titles: list[str] = []
        for profile in profiles:
            for requirement in profile.get("requirements", []):
                if node_id in requirement.get("node_ids", []):
                    profile_ids.append(profile["id"])
                    requirement_titles.append(f"{profile['label']}：{requirement['title']}")
        return sorted(set(profile_ids)), requirement_titles

    def scope_rows(node: dict[str, Any]) -> list[dict[str, str]]:
        record = records.get(node["id"], {})
        if "requirement_coverage" in record or node.get("coverage_requirements"):
            explicit = {row["requirement_id"]: row for row in record.get("requirement_coverage", [])}
            return [
                {
                    "scope": requirement["id"],
                    "type": requirement["category"],
                    "state": explicit.get(requirement["id"], {}).get("coverage_state", "unknown"),
                    "source": "coverage_requirement",
                    "notes": (
                        f"ref={requirement['ref']}; "
                        + explicit.get(requirement["id"], {}).get("notes", "")
                    ).strip(),
                    "row_kind": "coverage_requirement",
                }
                for requirement in node.get("coverage_requirements", [])
            ]
        explicit = {row["scope"]: row for row in record.get("scope_coverage", [])}
        if explicit:
            return [
                {
                    "scope": scope,
                    "type": node.get("scope_types", {}).get(scope, "metadata"),
                    "state": explicit[scope].get("coverage_state", "unknown"),
                    "source": "repo_evidence_review",
                    "notes": explicit[scope].get("notes", ""),
                    "row_kind": "scope",
                }
                for scope in node.get("scope", [])
            ]
        scope_items = node.get("scope", [])
        if isinstance(scope_items, dict):
            return []
        child_by_title = {nodes[child]["title"]: nodes[child] for child in node.get("children", [])}
        return [
            {
                "scope": scope,
                "type": node.get("scope_types", {}).get(scope, "metadata"),
                "state": child_by_title.get(scope, {}).get("coverage_state", "unknown"),
                "source": "sub_m_rollup" if scope in child_by_title else "not_declared",
                "notes": "由子 M 的 Coverage 汇总得到。" if scope in child_by_title else "尚未映射到子 M。",
                "row_kind": "scope",
            }
            for scope in scope_items
        ]

    def scope_completeness(rows: list[dict[str, str]]) -> tuple[str, int, int]:
        if not rows:
            return "empty", 0, 0
        covered = sum(row["state"] != "unknown" for row in rows)
        if covered == 0:
            return "none", covered, len(rows)
        if covered == len(rows):
            return "complete", covered, len(rows)
        return "partial", covered, len(rows)

    def requirement_category_completeness_markup(node: dict[str, Any], rows: list[dict[str, str]]) -> str:
        if report.get("coverage_denominator_kind") != "coverage_requirement":
            return ""
        completeness = node.get("coverage_requirement_completeness")
        if not completeness:
            by_category: dict[str, list[dict[str, str]]] = {
                category: [row for row in rows if row["type"] == category]
                for category in V4_REQUIREMENT_CATEGORIES
            }
            categories = {}
            for category, category_rows in by_category.items():
                known = sum(row["state"] != "unknown" for row in category_rows)
                categories[category] = {
                    "known": known,
                    "total": len(category_rows),
                    "missing_requirement_ids": [row["scope"] for row in category_rows if row["state"] == "unknown"],
                }
            completeness = {
                "overall": {
                    "known": sum(row["state"] != "unknown" for row in rows),
                    "total": len(rows),
                    "missing_requirement_ids": [row["scope"] for row in rows if row["state"] == "unknown"],
                },
                "categories": categories,
            }
        label = {
            "input": "Input row completeness",
            "output": "Output row completeness",
            "outcome": "Outcome row completeness",
            "failure": "Failure row completeness",
            "metadata": "Metadata row completeness",
        }
        overall = completeness["overall"]
        rows_html = [
            "<tr>"
            "<td>Overall row completeness</td>"
            f"<td>{overall['known']}/{overall['total']}</td>"
            f"<td>{esc(', '.join(overall.get('missing_requirement_ids', [])) or '无')}</td>"
            "</tr>"
        ]
        for category in V4_REQUIREMENT_CATEGORIES:
            row = completeness["categories"].get(category, {"known": 0, "total": 0, "missing_requirement_ids": []})
            rows_html.append(
                "<tr>"
                f"<td>{esc(label[category])}</td>"
                f"<td>{row['known']}/{row['total']}</td>"
                f"<td>{esc(', '.join(row.get('missing_requirement_ids', [])) or '无')}</td>"
                "</tr>"
            )
        return (
            '<table class="coverage-table"><thead><tr><th>Category</th><th>Rows present / total</th><th>Unknown row IDs</th></tr></thead><tbody>'
            + "".join(rows_html)
            + "</tbody></table>"
        )

    def counts_markup(values: list[str], title: str) -> str:
        if not values:
            return f'<div class="coverage-counts"><strong>{esc(title)}：</strong><span class="muted">无</span></div>'
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        parts = [f"<strong>{esc(title)}：</strong>"]
        for state, count in sorted(counts.items(), key=lambda item: (state_ranks.get(item[0], 10_000), item[0])):
            parts.append(f"{badge(state)}<span class=\"count\">{count}</span>")
        return '<div class="coverage-counts">' + " ".join(parts) + "</div>"

    def evidence_markup(node: dict[str, Any]) -> str:
        record = records.get(node["id"], {})
        evidence = record.get("evidence", [])
        if not evidence:
            return '<p class="empty">暂无人工确认的 repo evidence。</p>'
        rows: list[str] = []
        for item in evidence:
            kind = item.get("kind", "")
            ref = item.get("ref", "")
            result = ""
            if kind == "command":
                command_id = item.get("verification_command_id") or ref
                command_result = verification.get(command_id)
                if command_result:
                    status = command_result.get("status", "not_run")
                    result = f'<span class="verify-result verify-{esc(status)}">{esc(status)}</span>'
            rows.append(
                "<li>"
                f'<span class="evidence-kind">{esc(kind)}</span>'
                f'<span class="evidence-label">{esc(ref.rsplit("/", 1)[-1])}</span>'
                f'<span class="evidence-ref">{esc(ref)}</span>{result}'
                "</li>"
            )
        return '<ul class="evidence-list">' + "".join(rows) + "</ul>"

    def coverage_panel(node: dict[str, Any], rows: list[dict[str, str]], completeness: tuple[str, int, int]) -> str:
        record = records.get(node["id"], {})
        children = [nodes[child] for child in node.get("children", [])]
        child_states = [child["coverage_state"] for child in children]
        row_kind = (
            "coverage_requirement"
            if report.get("coverage_denominator_kind") == "coverage_requirement"
            or any(row.get("row_kind") == "coverage_requirement" for row in rows)
            else "scope"
        )
        row_label = "Requirement row" if row_kind == "coverage_requirement" else "Scope"
        row_label_zh = "Coverage 跟踪行" if row_kind == "coverage_requirement" else "Scope"
        missing_title = "明确缺口 / Missing mandatory requirement row IDs" if row_kind == "coverage_requirement" else "明确缺口 / Missing scope items"
        direct = node.get("direct_coverage_state", "unknown")
        if node["node_kind"] == "group":
            direct_text = "不适用（功能域不拥有直接实现状态）"
            target_text = "不适用（按子 M 汇总）"
            claim = "聚合节点只显示子 M 的保守汇总，不产生独立实现声明。"
        else:
            direct_text = state_label(direct)
            target_rows = []
            for profile in profiles:
                for requirement in profile.get("requirements", []):
                    if node["id"] in requirement.get("node_ids", []):
                        target_rows.append(
                            f"{profile['label']}：{state_label(requirement['target_coverage_state'])}"
                        )
            target_text = "；".join(target_rows) or "未映射 Target requirement"
            claim = record.get("claim_ceiling", "") or "未提供节点级 claim ceiling。"
        lifecycle = record.get("capability_lifecycle_state", "不适用（功能域）")
        validation_posture = record.get("validation_posture", "不适用（功能域）")
        resolution_status = record.get("resolution_status", "不适用（功能域）")
        scope_states = [row["state"] for row in rows]
        scope_unknown = [row["scope"] for row in rows if row["state"] == "unknown"]
        scope_not_started = [row["scope"] for row in rows if row["state"] == "not_started"]
        missing_scope = record.get("missing_coverage_requirement_ids", record.get("missing_scope_items", []))
        breadth = record.get("breadth_coverage", {})
        breadth_text = "；".join(f"{key}: {state_label(value)}" for key, value in breadth.items()) or "未声明（旧 proposal 兼容视图）"
        profile_gaps = []
        for profile in profiles:
            for requirement in profile.get("requirements", []):
                if node["id"] in requirement.get("node_ids", []) and not requirement.get("met", False):
                    current = state_label(requirement.get("current_coverage_state"))
                    target = state_label(requirement.get("target_coverage_state"))
                    blockers = requirement.get("breadth_gaps", [])
                    lifecycle_blockers = requirement.get("lifecycle_gaps", [])
                    validation_blockers = requirement.get("validation_gaps", [])
                    missing = requirement.get("missing_coverage_requirement_ids", requirement.get("missing_scope_items", []))
                    weak_scope = requirement.get("coverage_requirement_gaps", requirement.get("scope_gaps", []))
                    suffix = ""
                    if blockers:
                        suffix += f"，缺少 breadth: {', '.join(blockers)}"
                    if lifecycle_blockers:
                        suffix += f"，lifecycle 不足: {', '.join(lifecycle_blockers)}"
                    if validation_blockers:
                        suffix += f"，validation posture 不足: {', '.join(validation_blockers)}"
                    if missing:
                        suffix += f"，缺少 mandatory {row_label_zh}: {', '.join(missing)}"
                    if weak_scope:
                        suffix += f"，mandatory {row_label_zh} 状态不足: {', '.join(weak_scope)}"
                    profile_gaps.append(f"{profile['label']}：{requirement['title']}（当前 {current}，目标 {target}{suffix}）")
        declared_evidence = len(record.get("evidence", []))
        verified_gates = sum(
            1
            for command_id, result in verification.items()
            if node["id"] in result.get("node_ids", []) and result.get("status") == "passed"
        )
        candidate_evidence = sum(
            item.get("kind") == "candidate_evidence" for item in record.get("evidence", [])
        )
        profile_ids, requirement_titles = profile_mapping(node["id"])
        profile_text = "；".join(requirement_titles) or "无直接 Target requirement"
        mapped_requirements = [
            (profile, requirement)
            for profile in profiles
            for requirement in profile.get("requirements", [])
            if node["id"] in requirement.get("node_ids", [])
        ]
        requirement_statuses = [
            f"{profile['label']}：{requirement['title']}（{'N/A' if profile.get('status') == 'not_evaluated' else 'Met' if requirement.get('met', False) else 'Gap'}）"
            for profile, requirement in mapped_requirements
        ]
        if report["coverage_mode"] == "not_evaluated" or not mapped_requirements:
            target_scope_text = "N/A（未评估或无直接 Target requirement）"
        else:
            target_state = max(
                (requirement.get("target_coverage_state", "unknown") for _, requirement in mapped_requirements),
                key=lambda state: state_ranks.get(state, -1),
            )
            reached = sum(
                state_ranks.get(row["state"], -1) >= state_ranks.get(target_state, -1)
                for row in rows
            )
            target_scope_text = f"{reached}/{len(rows)}（目标 {state_label(target_state)}）"
        scope_table = (
            f'<table class="coverage-table"><thead><tr><th>{row_label}</th><th>类型</th><th>状态</th><th>来源</th><th>说明</th></tr></thead><tbody>'
            + "".join(
                "<tr>"
                f"<td>{esc(row['scope'])}</td>"
                f"<td>{esc(row['type'])}</td>"
                f"<td>{badge(row['state'])}</td>"
                f"<td>{esc(row['source'])}</td>"
                f"<td>{esc(row['notes'])}</td>"
                "</tr>"
                for row in rows
            )
            + "</tbody></table>"
            if rows
            else f'<p class="empty">无 {row_label}。</p>'
        )
        category_markup = requirement_category_completeness_markup(node, rows)
        category_section = (
            f"<h3>Requirement row completeness by category（Coverage 跟踪行完整性）</h3>{category_markup}"
            if category_markup
            else ""
        )
        coverage_table_title = (
            "Requirement row states（Coverage 跟踪行状态）"
            if row_kind == "coverage_requirement"
            else f"{row_label} Coverage"
        )
        return f'''<div class="coverage-panel">
          <dl class="coverage-kv">
            <dt>Node kind</dt><dd>{esc(kind_labels[node["node_kind"]])}</dd>
            <dt>Effective</dt><dd>{badge(node.get("coverage_state"), aggregate=node["node_kind"] == "group")}</dd>
            <dt>Direct</dt><dd>{esc(direct_text)}</dd>
            <dt>Evidence peak</dt><dd>{badge(node.get("evidence_peak_state", direct))}</dd>
            <dt>Claim state</dt><dd>{badge(node.get("claim_state", node.get("coverage_state")))}</dd>
            <dt>Capability lifecycle</dt><dd>{esc(lifecycle)}</dd>
            <dt>Validation posture</dt><dd>{esc(validation_posture)}</dd>
            <dt>Resolution status</dt><dd>{esc(resolution_status)}</dd>
            <dt>Node target</dt><dd>{esc(target_text)}</dd>
            <dt>Target mapping</dt><dd>{esc(profile_text)}</dd>
            <dt>{row_label} completeness</dt><dd>{completeness[1]}/{completeness[2]}</dd>
            <dt>达到目标状态的 {row_label}</dt><dd>{esc(target_scope_text)}</dd>
            <dt>Profile requirement</dt><dd>{esc('；'.join(requirement_statuses) or '无直接 Target requirement')}</dd>
            <dt>Declared evidence</dt><dd>{declared_evidence}</dd>
            <dt>Verified gates</dt><dd>{verified_gates}</dd>
            <dt>Candidate evidence</dt><dd>{candidate_evidence}</dd>
          </dl>
          <p class="claim-ceiling"><strong>Claim ceiling：</strong>{esc(claim)}</p>
          {counts_markup(child_states, "Sub M")}
          {counts_markup(scope_states, row_label)}
          <h3>Evidence</h3>{evidence_markup(node)}
          <h3>{coverage_table_title}</h3>{scope_table}
          {category_section}
          <h3>Target Gap（未达要求）</h3>{list_markup(profile_gaps)}
          <h3>{missing_title}</h3>{list_markup(missing_scope)}
          <h3>Breadth coverage（广度）</h3><p>{esc(breadth_text)}</p>
          <h3>Unknown {row_label}（尚未确认）</h3>{list_markup(scope_unknown)}
          <h3>Not-started {row_label}（已确认未开始）</h3>{list_markup(scope_not_started)}
        </div>'''

    def tsp_branch(title: str, body: str, *, open_by_default: bool = False) -> str:
        opened = " open" if open_by_default else ""
        return f'<details class="tsp-branch"{opened}><summary>{esc(title)}</summary><div class="branch-body">{body}</div></details>'

    def m1_semantics_panel(node: dict[str, Any]) -> str:
        """Render every model-owned M1 semantic field without inferring Coverage."""
        semantics = node.get("decision_semantics", {})
        legacy_effect_status = ""
        if "effect_status" in node:
            legacy_effect_status = (
                f'<dt>Effect status (v2 legacy)</dt><dd>{esc(node.get("effect_status", ""))}'
                '<p class="muted">legacy model field；不是当前 run result。</p></dd>'
            )
        decision_inputs = semantics.get("decision_inputs", semantics.get("decision_input", []))
        decision_outputs = semantics.get("decision_outputs", semantics.get("decision_output", []))
        terminal_outcomes = semantics.get("terminal_outcomes", semantics.get("terminal_decision_outputs", []))
        failure_outcomes = semantics.get("failure_outcomes", semantics.get("failure_modes", []))
        failure_label = "Failure outcomes" if "failure_outcomes" in semantics else "Failure modes"
        not_applicable = node.get("claim_exclusions_not_applicable")
        not_applicable_body = ""
        if isinstance(not_applicable, dict):
            refs = ", ".join(str(ref) for ref in not_applicable.get("source_refs", [])) or "无"
            not_applicable_body = (
                f'<p><strong>Not applicable rationale：</strong>{esc(not_applicable.get("rationale", ""))}</p>'
                f'<p><strong>Source refs：</strong>{esc(refs)}</p>'
            )
        claim_boundary_body = object_list_markup(node.get("claim_exclusions", [])) + not_applicable_body
        decision_body = f'''<dl class="semantic-kv">
          <dt>Type</dt><dd>{esc(semantics.get("type", ""))}</dd>
          <dt>Decision input</dt><dd>{list_markup(decision_inputs)}</dd>
          <dt>Decision output</dt><dd>{list_markup(decision_outputs)}</dd>
          <dt>Terminal outcomes</dt><dd>{list_markup(terminal_outcomes)}</dd>
          <dt>{failure_label}</dt><dd>{list_markup(failure_outcomes)}</dd>
          <dt>Decision rules</dt><dd>{list_markup(semantics.get("decision_rules", []))}</dd>
          <dt>Authority owner</dt><dd>{esc(semantics.get("authority_owner", ""))}</dd>
        </dl>'''
        metadata_body = object_list_markup(node.get("metadata_requirements", []))
        requirements_body = object_list_markup(node.get("coverage_requirements", []))
        execution = node.get("execution_semantics", {})
        return f'''<dl class="semantic-kv">
          <dt>Capability type</dt><dd>{esc(node.get("capability_type", ""))}</dd>
          <dt>Inputs</dt><dd>{object_list_markup(node.get("inputs", []))}</dd>
          <dt>Outputs</dt><dd>{object_list_markup(node.get("outputs", []))}</dd>
          <dt>Execution operation</dt><dd>{esc(execution.get("operation", "not_declared"))}</dd>
          <dt>Materialized outputs</dt><dd>{list_markup(execution.get("materialized_output_ids", []))}</dd>
          <dt>Validated outputs</dt><dd>{list_markup(execution.get("validated_output_ids", []))}</dd>
          <dt>Metadata requirements</dt><dd>{metadata_body}</dd>
          <dt>Coverage requirements</dt><dd>{requirements_body}</dd>
          <dt>Mandatory coverage requirements</dt><dd>{list_markup(node.get("mandatory_coverage_requirement_ids", []))}</dd>
          <dt>Claim exclusions / forbidden claims</dt><dd>{claim_boundary_body}</dd>
          <dt>Invariants</dt><dd>{list_markup(node.get("invariants", []))}</dd>
          <dt>Witness artifacts</dt><dd>{list_markup(node.get("witness_artifact_refs", []))}</dd>
          <dt>Negative fixtures</dt><dd>{list_markup(node.get("negative_fixture_refs", []))}</dd>
          <dt>Traceability refs</dt><dd>{list_markup(node.get("traceability_refs", []))}</dd>
          {legacy_effect_status}
          <dt>Excluded outputs</dt><dd>{list_markup(node.get("excluded_outputs", []))}</dd>
        </dl>
        <h3>Decision semantics</h3>{decision_body}'''

    def execution_observations_panel(node: dict[str, Any]) -> str:
        results = execution_results_by_m1.get(node["id"], [])
        if not results:
            return '<p class="empty">无绑定 execution result；模型本身不表达当前执行状态。</p>'
        rows = []
        for result in results:
            witness = ", ".join(result.get("witness_refs", [])) or "无"
            rows.append(
                "<tr>"
                f"<td>{esc(result['execution_result_id'])}</td>"
                f"<td>{esc(result['effect_status'])}</td>"
                f"<td>{esc(result['run_id'])}</td>"
                f"<td>{esc(result['execution_id'])}</td>"
                f"<td>{esc(result['evaluated_at'])}</td>"
                f"<td>{esc(result['evaluator'])}</td>"
                f"<td>{esc(witness)}</td>"
                "</tr>"
            )
        return (
            '<table class="coverage-table"><thead><tr><th>Result</th><th>Effect status</th><th>Run</th><th>Execution</th><th>Evaluated</th><th>Evaluator</th><th>Witness</th></tr></thead><tbody>'
            + "".join(rows)
            + "</tbody></table>"
        )

    def node_markup(node_id: str, depth: int = 0) -> str:
        node = nodes[node_id]
        rows = scope_rows(node)
        completeness, covered, total = scope_completeness(rows)
        completeness_attr = (
            "data-requirement-completeness"
            if report.get("coverage_denominator_kind") == "coverage_requirement"
            else "data-scope-completeness"
        )
        profile_ids, _ = profile_mapping(node_id)
        profile_attr = " ".join(profile_ids)
        children = "".join(node_markup(child_id, depth + 1) for child_id in node.get("children", []))
        subfeatures = ""
        if children:
            subfeatures = f'''<details class="subfeatures" open><summary>Sub M</summary><div class="children">{children}</div></details>'''
        source_refs = node.get("source_statement_refs", [])
        ref_body = list_markup([f"KYM statement: {ref}" for ref in source_refs])
        return f'''<details id="{esc(node_anchors[node_id])}" class="tco-node" data-node-id="{esc(node_id)}" data-depth="{depth}" data-node-kind="{esc(node["node_kind"])}" data-coverage-state="{esc(node.get("coverage_state", "unknown"))}" {completeness_attr}="{completeness}" data-profile-ids="{esc(profile_attr)}" open>
          <summary><span class="node-title">{esc(node["title"])}</span><span class="summary-meta">{badge(node.get("coverage_state"), aggregate=node["node_kind"] == "group")}<span class="node-kind kind-{esc(node["node_kind"])}">{esc(kind_labels[node["node_kind"]])}</span><span class="node-id">{esc(node["id"])}</span></span></summary>
          <div class="tsp-grid">
            {tsp_branch("Topic", f"<p>{esc(node['topic'])}</p>", open_by_default=True)}
            {tsp_branch("Ref Info", ref_body)}
            {tsp_branch("Scope", scope_body(node), open_by_default=True)}
            {tsp_branch("Purpose", f"<p>{esc(node['purpose'])}</p>", open_by_default=True)}
            {tsp_branch("Capability definition（能力定义）", m1_semantics_panel(node)) if node["node_kind"] == "capability" else ""}
            {tsp_branch("Coverage", coverage_panel(node, rows, (completeness, covered, total)))}
            {tsp_branch("Execution observations（执行观察）", execution_observations_panel(node)) if node["node_kind"] == "capability" else ""}
          </div>
          {subfeatures}
        </details>'''

    # Reports intentionally keep the model root implicit; the unique node with no parent is deterministic.
    child_ids = {child for node in report["nodes"] for child in node.get("children", [])}
    root_id = next((node["id"] for node in report["nodes"] if node["id"] not in child_ids), "")

    leaf_nodes = [node for node in report["nodes"] if node["node_kind"] != "group"]
    state_counts: dict[str, int] = {}
    for node in leaf_nodes:
        state = node.get("coverage_state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    summary_cards = "".join(
        f'<div class="summary-card">{badge(state)}<strong>{count}</strong></div>'
        for state, count in sorted(state_counts.items(), key=lambda item: (state_ranks.get(item[0], 10_000), item[0]))
    ) or '<p class="empty">暂无可计数的非功能域节点。</p>'
    missing_scope_total = sum(len(record.get("missing_scope_items", [])) for record in records.values())
    missing_requirement_total = sum(len(record.get("missing_coverage_requirement_ids", [])) for record in records.values())
    if report.get("coverage_denominator_kind") == "coverage_requirement":
        missing_scope_summary = f'<p class="definition">Missing mandatory requirement rows：{missing_requirement_total}（逐节点和 Target Gap 中可定位；只有 mandatory Requirement row 阻断 Met）。</p>'
    else:
        missing_scope_summary = f'<p class="definition">Missing scope items：{missing_scope_total}（逐节点和 Target Gap 中可定位；只有 requirement 标为 mandatory 的项目阻断 Met）。</p>'
    verification_values = list(report.get("verification_results", {}).values())
    verification_note = (
        f"Verification：已记录 {len(verification_values)} 个验证命令，失败 {sum(item.get('status') != 'passed' for item in verification_values)} 个。"
        if verification_values
        else "Verification：本次报告未提供验证命令结果；Coverage 状态不会因渲染而自动提升。"
    )
    profile_rows_parts: list[str] = []
    for profile in profiles:
        met_text = "N/A" if profile["met_count"] is None else f"{profile['met_count']}/{profile['requirement_count']}"
        status_text = (
            "未评估"
            if profile["status"] == "not_evaluated"
            else "达到当前 Profile"
            if profile["status"] == "met"
            else "仍有缺口"
        )
        axis_blockers = sum(
            len(item.get("breadth_gaps", []))
            + len(item.get("lifecycle_gaps", []))
            + len(item.get("validation_gaps", []))
            for item in profile.get("requirements", [])
        )
        profile_rows_parts.append(
            "<tr>"
            f"<td>{esc(profile['label'])}</td>"
            f"<td>{esc(met_text)} requirements Met/Gap</td>"
            f"<td>{esc(status_text)}</td>"
            f"<td>{esc(profile.get('goal_summary', '未提供'))}</td>"
            f"<td>{axis_blockers}</td>"
            f"<td>{esc(profile['claim_ceiling'])}</td>"
            "</tr>"
        )
    profile_rows = "".join(profile_rows_parts)
    profile_options = "".join(
        f'<option value="{esc(profile["id"])}">{esc(profile["label"])}</option>' for profile in profiles
    )
    title = "TCO Candidate Preview" if is_candidate else "TCO Coverage"
    definition = (
        "TCO = Testing Coverage Outline。功能域用于组织层级；能力与规则节点用 Topic / Scope / Purpose 建模。本页面是 candidate preview，不读取或创建 baseline、Target 或 Coverage receipt，不能当作接受态报告。"
        if is_candidate
        else "TCO = Testing Coverage Outline。功能域用于组织层级；能力与规则节点用 Topic / Scope / Purpose 建模，Coverage 仅显示已批准的证据评估。"
    )
    preview_banner = (
        '<div class="preview-banner" data-report-mode="candidate_not_approved">候选态 · 未批准（candidate_not_approved）</div>'
        if is_candidate
        else ""
    )
    requirement_denominator = report.get("coverage_denominator_kind") == "coverage_requirement"
    completeness_filter_id = "requirementCompletenessFilter" if requirement_denominator else "scopeCompletenessFilter"
    completeness_filter_label = "Requirement row completeness" if requirement_denominator else "Scope completeness"
    completeness_options = (
        (
            '<option value="complete">Requirement rows 全部存在</option>'
            '<option value="partial">Requirement rows 部分存在</option>'
            '<option value="none">Requirement rows 均未知</option>'
            '<option value="empty">无 Requirement</option>'
        )
        if requirement_denominator
        else (
            '<option value="complete">Scope 覆盖完整</option>'
            '<option value="partial">Scope 部分覆盖</option>'
            '<option value="none">Scope 无覆盖</option>'
            '<option value="empty">无 Scope</option>'
        )
    )
    rendered = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(report["product_id"])} {esc(title)}</title>
<style>
:root{{--bg:#f8fafc;--ink:#172033;--muted:#64748b;--line:#0f766e;--node:#ecfeff;--node-strong:#99f6e4;--branch:#fff;--rule:#94a3b8;--accent:#0f766e}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:Arial,"PingFang SC",sans-serif}} header{{position:sticky;top:0;z-index:2;padding:16px 24px;background:#134e4a;color:#fff}} h1{{margin:0 0 6px;font-size:20px}} h2{{margin:22px 0 8px;font-size:17px}} h3{{margin:12px 0 6px;font-size:14px}} .meta{{color:#ccfbf1;font-size:12px;line-height:1.5;overflow-wrap:anywhere}} .preview-banner{{position:sticky;top:0;margin:-16px -24px 12px;padding:9px 24px;background:#f97316;color:#fff;font-weight:800;letter-spacing:.04em;text-transform:uppercase;border-bottom:2px solid #fed7aa}} main{{max-width:1440px;padding:18px 24px 48px}} .definition{{margin:0 0 14px;padding:10px 12px;border-left:3px solid var(--accent);background:#fff;border-radius:6px;color:#334155;line-height:1.55}} .status-legend{{margin:0 0 14px;padding:12px 14px;background:#fff;border:1px solid #cbd5e1;border-left:4px solid #0f766e;border-radius:6px}} .status-legend h2{{margin:0 0 8px;font-size:16px}} .status-legend dl{{display:grid;grid-template-columns:minmax(210px,.35fr) minmax(0,1fr);gap:8px 14px;margin:0}} .status-legend dt{{font-weight:700;color:#134e4a}} .status-legend dd{{margin:0;line-height:1.5;color:#334155}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0;align-items:center}} .toolbar label{{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}} select,input,button{{border:1px solid var(--rule);background:#fff;color:var(--ink);padding:7px 10px;border-radius:6px}} input[type="search"]{{min-width:210px}} button{{cursor:pointer}} .filter-result-summary{{margin:0 0 12px;padding:9px 12px;background:#fff;border:1px solid #cbd5e1;border-left:4px solid var(--accent);border-radius:6px;color:#134e4a;font-weight:700}} .coverage-summary{{margin:0 0 14px}} .summary-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}} .summary-card{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:6px;background:#fff;border:1px solid #dbe4ee}} .summary-card strong{{font-size:20px}}
details{{margin:8px 0}} summary{{cursor:pointer}} .tco-node{{border-left:2px dashed var(--line);padding-left:14px;margin-left:10px}} .tco-node>summary{{display:flex;justify-content:space-between;gap:12px;padding:9px 12px;border-radius:6px;background:var(--node-strong);font-weight:700}} .tco-node[hidden]{{display:none}} .summary-meta{{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}} .node-id{{color:var(--muted);font-weight:400;font-size:12px}} .tsp-grid{{margin:8px 0 12px 18px}} .tsp-branch{{border-left:2px dashed var(--line);padding-left:12px}} .tsp-branch>summary{{width:fit-content;min-width:96px;padding:5px 8px;border-radius:5px;background:var(--node);color:#115e59;font-weight:700}} .branch-body{{background:var(--branch);border:1px solid #cbd5e1;border-radius:6px;margin:6px 0 10px 12px;padding:10px 12px;line-height:1.5}} .branch-body p{{margin:0}} .branch-body ol,.branch-body ul{{margin:0;padding-left:22px}} .branch-body li{{margin:5px 0}} .empty,.muted{{color:var(--muted)}} .subfeatures{{margin-left:18px}} .subfeatures>summary{{width:fit-content;padding:5px 8px;border-radius:5px;background:#ccfbf1;color:#115e59;font-weight:700}} .children{{margin-left:8px}}
body.m-only .tsp-grid{{display:none}} body.m-only .tco-node.detail-visible>.tsp-grid{{display:block}} body.m-only .tco-node.detail-visible>summary{{outline:2px solid #0f766e;outline-offset:2px}}
.coverage-badge{{display:inline-block;min-width:72px;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:700;text-align:center;border:1px solid transparent}} .node-kind{{display:inline-block;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid #cbd5e1;background:#fff}} .kind-group{{color:#334155}} .kind-capability{{color:#166534}} .kind-policy{{color:#7c2d12}} .state-unknown{{color:#475569;background:#f1f5f9;border-color:#cbd5e1}} .state-not_started{{color:#7f1d1d;background:#fee2e2;border-color:#fecaca}} .state-design_only{{color:#6b21a8;background:#f3e8ff;border-color:#e9d5ff}} .state-schema_validator{{color:#1d4ed8;background:#dbeafe;border-color:#bfdbfe}} .state-test_bound{{color:#0f766e;background:#ccfbf1;border-color:#99f6e4}} .state-bounded_runtime{{color:#166534;background:#dcfce7;border-color:#bbf7d0}} .state-integrated_runtime{{color:#064e3b;background:#a7f3d0;border-color:#6ee7b7}} [data-depth="0"]>summary{{background:#5eead4}} [data-depth="1"]>summary{{background:#99f6e4}} [data-depth="2"]>summary{{background:#ccfbf1}} [data-depth="3"]>summary{{background:#ecfeff}}
.coverage-kv,.semantic-kv{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px 12px;margin:0 0 10px}} .coverage-kv dt,.semantic-kv dt{{color:var(--muted);font-size:12px}} .coverage-kv dd,.semantic-kv dd{{margin:2px 0 0;font-weight:700;overflow-wrap:anywhere}} .semantic-kv dd ul{{font-weight:400}} .coverage-counts{{margin:6px 0}} .coverage-counts .count{{margin:0 8px 0 3px;color:var(--muted)}} .claim-ceiling{{margin:8px 0}} .evidence-list{{list-style:none;padding-left:0!important}} .evidence-list li{{display:grid;grid-template-columns:72px minmax(0,1fr) minmax(0,1.8fr) max-content;gap:10px;align-items:start;padding:6px 0;border-bottom:1px solid #e2e8f0}} .evidence-kind{{color:var(--muted);font-size:12px}} .evidence-label,.evidence-ref{{overflow-wrap:anywhere;word-break:break-word;line-height:1.35}} .evidence-ref{{color:#334155;font-family:Menlo,Consolas,monospace;font-size:12px}} .verify-result{{padding:2px 6px;border-radius:999px;font-size:12px;font-weight:700}} .verify-passed{{color:#166534;background:#dcfce7}} .verify-failed,.verify-timeout{{color:#991b1b;background:#fee2e2}} .verify-not_run{{color:#475569;background:#f1f5f9}} .coverage-table{{width:100%;border-collapse:collapse;font-size:13px}} .coverage-table th,.coverage-table td{{text-align:left;vertical-align:top;border-bottom:1px solid #e2e8f0;padding:7px 6px}} .coverage-table th{{color:var(--muted);font-weight:700}}
@media (max-width:760px){{main{{padding:12px}}.node-id{{display:none}}.tco-node{{margin-left:0;padding-left:10px}}.tsp-grid{{margin-left:8px}}.evidence-list li{{grid-template-columns:1fr}}}}
</style></head><body><header>{preview_banner}<h1>{esc(report["product_id"])} {esc(title)}</h1><div class="meta">Report mode: {esc(report_mode)} · Coverage mode: {esc(report["coverage_mode"])} · model digest: {esc(report["source_tco_model_digest"])} · coverage digest: {esc(report["source_coverage_digest"])} · generated: {esc(report["generated_at"])}</div></header><main>
<p class="definition">{esc(definition)}<br>{esc(report["authority_note"])}{("<br>N/A (not_evaluated)：Coverage 尚未评估，不表示 0/N 或 Gap。" if report["coverage_mode"] == "not_evaluated" else "")}</p>
{status_legend_markup()}
<section class="coverage-summary"><div class="summary-cards">{summary_cards}</div><p class="definition">统计只计算能力与规则节点；Profile 分母是 requirement 数，不是 M1、Mc 或 repo 数。非评估模式使用 N/A，不表示 0/N。<br>{esc(verification_note)}</p>{missing_scope_summary}<table class="coverage-table"><thead><tr><th>Target profile</th><th>Met / requirement denominator</th><th>状态</th><th>Contract / Implementation Goal axes（coverage / lifecycle / validation）</th><th>Axis blockers / Breadth blockers</th><th>Claim ceiling</th></tr></thead><tbody>{profile_rows}</tbody></table></section>
<!-- Capability / Policy / Decision / Witness / NF -->
<div class="toolbar"><button type="button" onclick="expandAll()">全部展开</button><button type="button" onclick="showOnlyM()">只保留 M</button><button type="button" onclick="hideRefInfo()">隐藏 Ref Info</button><label>Coverage<select id="coverageFilter" onchange="applyFilters()"><option value="">全部</option>{''.join(f'<option value="{esc(state)}">{esc(label)}</option>' for state,label in state_labels.items())}</select></label><label>Node kind<select id="nodeKindFilter" onchange="applyFilters()"><option value="">全部</option><option value="group">功能域</option><option value="capability">能力</option><option value="policy">规则</option></select></label><label>{esc(completeness_filter_label)}<select id="{esc(completeness_filter_id)}" onchange="applyFilters()"><option value="">全部</option>{completeness_options}</select></label><label>Target<select id="profileFilter" onchange="applyFilters()"><option value="">全部</option>{profile_options}</select></label><label>关键词<input id="keywordSearch" type="search" placeholder="M1 关键词" oninput="applyFilters()"></label></div>
<div id="filterResultSummary" class="filter-result-summary" aria-live="polite">直接命中：MC 0 / M1 0</div>
<section id="tco-tree">{node_markup(root_id) if root_id else '<p class="empty">暂无 TCO 节点。</p>'}</section></main>
<script>
function clearVisibleNodeDetails(){{document.querySelectorAll('.tco-node.detail-visible').forEach(node=>node.classList.remove('detail-visible'));}}
function openVisibleMContainers(){{document.querySelectorAll('.tco-node').forEach(node=>{{if(!node.hidden)node.open=true;}});document.querySelectorAll('.subfeatures').forEach(group=>{{group.open=Array.from(group.querySelectorAll('.tco-node')).some(child=>!child.hidden);}});}}
function expandAll(){{document.body.classList.remove('m-only');clearVisibleNodeDetails();document.querySelectorAll('details').forEach(detail=>detail.open=true);}}
function hideRefInfo(){{document.body.classList.remove('m-only');clearVisibleNodeDetails();document.querySelectorAll('.tsp-branch').forEach(branch=>branch.open=branch.querySelector('summary').textContent!=='Ref Info');}}
function showOnlyM(){{applyFilters();document.body.classList.add('m-only');clearVisibleNodeDetails();document.querySelectorAll('.tsp-branch').forEach(branch=>branch.open=false);openVisibleMContainers();}}
function currentFilters(){{return{{coverageState:document.getElementById('coverageFilter').value,nodeKind:document.getElementById('nodeKindFilter').value,denominatorCompleteness:document.getElementById('{esc(completeness_filter_id)}').value,profileId:document.getElementById('profileFilter').value,keyword:document.getElementById('keywordSearch').value}};}}
function searchTokens(keyword){{return keyword.trim().toLowerCase().split(/\s+/).filter(Boolean);}}
function nodeSearchText(node){{const summary=node.querySelector(':scope>summary');const grid=node.querySelector(':scope>.tsp-grid');return ((summary?summary.textContent:'')+' '+(grid?grid.textContent:'')).toLowerCase();}}
function keywordMatches(node,keyword){{const tokens=searchTokens(keyword);if(!tokens.length)return true;if(node.dataset.nodeKind!=='capability')return false;const text=nodeSearchText(node);return tokens.every(token=>text.includes(token));}}
function matchesFilters(node,filters){{const completeness=node.dataset.requirementCompleteness||node.dataset.scopeCompleteness||'';const structuralMatch=(!filters.coverageState||node.dataset.coverageState===filters.coverageState)&&(!filters.nodeKind||node.dataset.nodeKind===filters.nodeKind)&&(!filters.denominatorCompleteness||completeness===filters.denominatorCompleteness)&&(!filters.profileId||node.dataset.profileIds.split(' ').includes(filters.profileId));return structuralMatch&&keywordMatches(node,filters.keyword);}}
function updateFilterSummary(nodes){{let mc=0;let m1=0;nodes.forEach(node=>{{if(node.dataset.selfMatch!=='true')return;if(node.dataset.nodeKind==='group')mc+=1;if(node.dataset.nodeKind==='capability')m1+=1;}});const summary=document.getElementById('filterResultSummary');if(summary)summary.textContent=`直接命中：MC ${{mc}} / M1 ${{m1}}`;}}
function applyFilters(){{const filters=currentFilters();const active=Object.values(filters).some(value=>value.trim?value.trim():Boolean(value));const nodes=Array.from(document.querySelectorAll('.tco-node'));nodes.forEach(node=>{{node.dataset.selfMatch=(!active||matchesFilters(node,filters))?'true':'false';node.dataset.visibleMatch=node.dataset.selfMatch;}});nodes.slice().reverse().forEach(node=>{{const descendantMatch=Array.from(node.querySelectorAll(':scope .tco-node')).some(child=>child.dataset.visibleMatch==='true');const visible=node.dataset.selfMatch==='true'||descendantMatch;node.dataset.visibleMatch=visible?'true':'false';node.hidden=!visible;if(visible&&active)node.open=true;}});updateFilterSummary(nodes);if(active)openVisibleMContainers();document.querySelectorAll('.tco-node.detail-visible').forEach(node=>{{if(node.hidden)node.classList.remove('detail-visible');}});}}
document.querySelectorAll('.tco-node>summary').forEach(summary=>summary.addEventListener('click',event=>{{if(!document.body.classList.contains('m-only'))return;event.preventDefault();event.stopPropagation();const node=summary.parentElement;const wasVisible=node.classList.contains('detail-visible');clearVisibleNodeDetails();if(!wasVisible){{node.open=true;node.classList.add('detail-visible');node.querySelectorAll(':scope>.tsp-grid>.tsp-branch').forEach(branch=>branch.open=true);}}}}));
applyFilters();
</script></body></html>'''
    if integrity is not None:
        integrity_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(item['label']))}</td>"
            f"<td><a href=\"{html.escape(_json_viewer_href(str(item['href'])))}\">{html.escape(str(item['href']))}</a></td>"
            f"<td>{html.escape(str(item['status']))}</td>"
            f"<td>{html.escape(str(item.get('digest') or item.get('reason') or ''))}"
            f"{(' / expected=' + str(item.get('expected_digest'))) if item.get('expected_digest') else ''}</td>"
            "</tr>"
            for item in integrity.get("artifacts", [])
        )
        axes_rows = "".join(
            f"<dt>{html.escape(str(key))}</dt><dd>{html.escape(str(value))}</dd>"
            for key, value in integrity.get("state_axes", {}).items()
        )
        evidence_markup = (
            '<section class="evidence-hub" id="evidence-hub">'
            '<h2>证据中心 / Evidence Hub</h2>'
            f"<p><strong>Integrity：</strong>{html.escape(str(integrity.get('status')))}；"
            f"<strong>Finding owner：</strong>{html.escape(str(integrity.get('finding_owner')))}；"
            f"<strong>Claim ceiling：</strong>{html.escape(str(integrity.get('claim_ceiling')))}</p>"
            f'<dl class="evidence-axes">{axes_rows}</dl>'
            '<table class="coverage-table"><thead><tr><th>Evidence</th><th>Link</th><th>Integrity</th><th>Digest / reason</th></tr></thead>'
            f"<tbody>{integrity_rows}</tbody></table>"
            f"<p class=\"muted\">{html.escape(str(integrity.get('authority_note')))}</p>"
            "</section>"
        )
        rendered = rendered.replace('<section class="coverage-summary">', evidence_markup + '<section class="coverage-summary">', 1)
    return _decorate_html_layout(rendered, report)


def render_report(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    output_dir: Path | None,
    verification_results: dict[str, Any] | None = None,
) -> dict[str, Path]:
    report = build_report(workspace, product_id, model_path, coverage_path, verification_results)
    return _write_report_files(
        workspace,
        product_id,
        report,
        output_dir,
        report_filename="tco_coverage_report.json",
        manifest_filename="tco_coverage_manifest.json",
        html_filename="tco.html",
    )


def _render_candidate_preview_files(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    output_dir: Path | None,
    verification_results: dict[str, Any] | None = None,
    execution_result_paths: list[Path] | None = None,
    state_axes: dict[str, str] | None = None,
    evidence_source_paths: list[Path] | None = None,
    expected_digests: dict[str, str] | None = None,
    finding_owner: str | None = None,
    integrity_path_root: Path | None = None,
) -> dict[str, Path]:
    report = build_candidate_report(workspace, product_id, model_path, coverage_path, verification_results, execution_result_paths)
    return _write_report_files(
        workspace,
        product_id,
        report,
        output_dir,
        report_filename="tco_candidate_preview_report.json",
        manifest_filename="tco_candidate_preview_manifest.json",
        html_filename="tco-preview.html",
        source_paths=evidence_source_paths if evidence_source_paths is not None else [model_path, coverage_path, *(execution_result_paths or [])],
        state_axes=state_axes,
        expected_digests=expected_digests,
        finding_owner=finding_owner,
        integrity_path_root=integrity_path_root,
    )


def render_candidate_preview(
    workspace: Path,
    product_id: str,
    model_path: Path,
    coverage_path: Path,
    output_dir: Path | None,
    verification_results: dict[str, Any] | None = None,
    execution_result_paths: list[Path] | None = None,
    state_axes: dict[str, str] | None = None,
    evidence_source_paths: list[Path] | None = None,
    expected_digests: dict[str, str] | None = None,
    finding_owner: str | None = None,
    integrity_path_root: Path | None = None,
) -> dict[str, Path]:
    model = load_json(model_path)
    if model.get("schema") == "kym_tco.tco_model.v4":
        reader_language_audit = audit_reader_language(model)
        if reader_language_audit["verdict"] != "pass":
            findings = [
                {"code": item["code"], "node_id": item["node_id"], "path": item["path"]}
                for item in reader_language_audit["findings"]
            ]
            raise ContractError(
                "TCO v4 reader-language audit 未通过，候选 preview 未写入: "
                + json.dumps(findings, ensure_ascii=False, sort_keys=True)
            )
        coverage = load_json(coverage_path)
        config = load_config(project_root(workspace, product_id))
        capability_claim_audit = audit_runtime_capability_witnesses(
            model, coverage, config, verification_results or {}, workspace
        )
        if capability_claim_audit["verdict"] != "pass":
            raise ContractError(
                "TCO v4 capability witness 对账未通过，候选 preview 未写入: "
                + json.dumps(
                    capability_claim_audit["findings"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    return _render_candidate_preview_files(
        workspace,
        product_id,
        model_path,
        coverage_path,
        output_dir,
        verification_results,
        execution_result_paths,
        state_axes,
        evidence_source_paths,
        expected_digests,
        finding_owner,
        integrity_path_root,
    )


def compare_candidate_profiles(
    workspace: Path, product_id: str, model_path: Path, coverage_path: Path
) -> list[dict[str, Any]]:
    return build_candidate_report(workspace, product_id, model_path, coverage_path)["target_profiles"]


def _write_report_files(
    workspace: Path,
    product_id: str,
    report: dict[str, Any],
    output_dir: Path | None,
    *,
    report_filename: str,
    manifest_filename: str,
    html_filename: str,
    source_paths: list[Path] | None = None,
    state_axes: dict[str, str] | None = None,
    expected_digests: dict[str, str] | None = None,
    finding_owner: str | None = None,
    integrity_path_root: Path | None = None,
) -> dict[str, Path]:
    root = project_root(workspace, product_id)
    target_dir = output_dir or root / "reports" / "latest"
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / report_filename
    manifest_path = target_dir / manifest_filename
    html_path = target_dir / html_filename
    effective_axes = state_axes or {
        "environment_status": "ready",
        "package_execution_status": "completed",
        "candidate_status": "candidate_not_approved",
        "verification_status": "passed" if report.get("verification_results") else "not_run",
        "audit_status": "not_run",
        "approval_status": "not_requested",
    }
    manifest = {
        "schema": "kym_tco.tco_coverage_manifest.v1",
        "product_id": product_id,
        "report_mode": report.get("report_mode", "approved"),
        "coverage_mode": report["coverage_mode"],
        "node_count": len(report["nodes"]),
        "target_profiles": report["target_profiles"],
        "failed_verification_commands": sorted(
            command_id
            for command_id, result in report["verification_results"].items()
            if result["status"] != "passed"
        ),
        "report_digest": digest_value({key: value for key, value in report.items() if key != "generated_at"}),
        "source_tco_model_digest": report["source_tco_model_digest"],
        "source_coverage_digest": report["source_coverage_digest"],
        "authority_note": report["authority_note"],
    }
    base_payloads = {
        json_path: json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }
    for path, content in base_payloads.items():
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_dir, delete=False) as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    viewer_paths = [_write_json_viewer(path) for path in (list(base_payloads) + list(source_paths or []))]
    projection = report.get("projection_metadata", {})
    for ref_key in ("source_ledger_ref", "session_m1_audit_ref"):
        ref = projection.get(ref_key)
        if ref:
            viewer_paths.append(_write_json_viewer(workspace / ref))
    artifact_specs = [("report", json_path), ("manifest", manifest_path)]
    artifact_specs.extend((f"source-{index}", path) for index, path in enumerate(source_paths or [], start=1))
    integrity = build_integrity_report(
        html_path=html_path,
        allowed_root=workspace,
        artifacts=artifact_specs,
        claim_ceiling="candidate_not_approved；HTML 只展示证据，不创建 semantic correctness、approval 或 receipt",
        state_axes=effective_axes,
        expected_digests=expected_digests,
        finding_owner=finding_owner or ("verification" if effective_axes["verification_status"] == "failed" else "audit" if effective_axes["audit_status"] in {"findings", "blocker"} else "package-integrity" if effective_axes["package_execution_status"] in {"blocked", "failed"} else "none"),
        path_root=integrity_path_root,
    )
    validate_document("evidence_hub_integrity", integrity)
    integrity_path = target_dir / "evidence-hub-integrity.json"
    payloads = {integrity_path: evidence_json_bytes(integrity), html_path: _render_html(report, integrity)}
    for path, content in payloads.items():
        if isinstance(content, bytes):
            with tempfile.NamedTemporaryFile("wb", dir=target_dir, delete=False) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
        else:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_dir, delete=False) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
        temp_path.replace(path)
    return {"report": json_path, "manifest": manifest_path, "html": html_path, "integrity": integrity_path}


def approve_coverage(
    workspace: Path,
    product_id: str,
    model_path: Path,
    proposal_path: Path,
    approval_reference: str,
    approved_by: str,
    approval_source: str = "human",
    authorization_path: Path | None = None,
) -> tuple[Path, Path]:
    root = project_root(workspace, product_id)
    model, proposal = validate_coverage_model(workspace, product_id, model_path, proposal_path)
    authorization = None
    if approval_source == "authorized_system":
        authorization = validate_system_authorization(
            root, authorization_path, product_id=product_id, approval_scope="coverage_delta",
            approved_by=approved_by, approval_reference=approval_reference,
            target_ids={profile["source_target_id"] for profile in model["target_profiles"]},
        )
    elif approval_source != "human":
        raise ContractError(f"不支持的 approval_source: {approval_source}")
    elif authorization_path is not None:
        raise ContractError("human approval 不接受系统授权文件")
    digest = digest_value(proposal)
    approved_path = root / "tco" / "coverage" / f"coverage-{proposal['proposal_id']}.json"
    if approved_path.exists() and digest_value(load_json(approved_path)) != digest:
        raise ContractError(f"approved coverage identity 冲突: {approved_path}")
    write_json(approved_path, proposal)
    receipt = {
        "schema": "kym_tco.approval_receipt.v1",
        "receipt_id": f"approve-coverage-delta-{proposal['proposal_id']}-{digest.split(':')[1][:12]}",
        "product_id": product_id,
        "approval_scope": "coverage_delta",
        "artifact_type": "coverage_model",
        "artifact_id": proposal["proposal_id"],
        "revision": proposal["proposal_id"],
        "artifact_digest": digest,
        "approved_by": approved_by,
        "approval_source": approval_source,
        "approval_reference": approval_reference,
        "approved_at": utc_now(),
        "notes": f"source_tco_model_digest={proposal['source_tco_model_digest']}",
    }
    if authorization is not None:
        receipt.update(authorization)
    validate_document("approval", receipt)
    receipt_path = root / "tco" / "approvals" / f"coverage-{proposal['proposal_id']}.approval.json"
    return approved_path, persist_approval_receipt(receipt_path, receipt)


def diff_json(base: Any, candidate: Any) -> dict[str, Any]:
    if base == candidate:
        changes: list[dict[str, Any]] = []
    elif isinstance(base, dict) and isinstance(candidate, dict):
        changes = []
        for key in sorted(set(base) | set(candidate)):
            if key not in base:
                changes.append({"path": key, "change": "added", "after": candidate[key]})
            elif key not in candidate:
                changes.append({"path": key, "change": "removed", "before": base[key]})
            elif base[key] != candidate[key]:
                changes.append({"path": key, "change": "modified", "before": base[key], "after": candidate[key]})
    else:
        changes = [{"path": "<root>", "change": "modified", "before": base, "after": candidate}]
    return {
        "schema": "kym_tco.tco_delta.v1",
        "base_digest": digest_value(base),
        "candidate_digest": digest_value(candidate),
        "changes": changes,
    }
