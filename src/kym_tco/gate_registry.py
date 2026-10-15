"""Slice 1 deterministic contract loader for the gate registry."""
from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .common import ContractError, digest_value, load_json


EXPECTED_GATE_IDS = {
    "G-HOST-PROFILE", "G-SKILL-IDENTITY", "G-ENV-DOCTOR", "G-SOURCE-NORMALIZE",
    "G-SOURCE-CLOSURE", "G-TARGET-VALIDATE", "G-KYM-VALIDATE", "G-KYM-LINT",
    "G-KYM-AUDIT", "G-KYM-PACKAGE", "G-TCO-MODEL-VALIDATE",
    "G-TCO-COVERAGE-VALIDATE", "G-TCO-VERIFY", "G-TCO-AUDIT", "G-TCO-PACKAGE",
    "G-TCO-RENDER", "G-DASHBOARD-OBSERVE", "G-DASHBOARD-REMEDIATE", "G-KB-AUDIT",
    "G-GIT-FRESHNESS", "G-REPORT-SCHEMA", "G-DENOMINATOR-COMPLETE",
    "G-ARTIFACT-INTEGRITY", "G-FINAL-PACKAGE",
}
EXPECTED_INPUT_KINDS = {"repo", "conversation_text", "file_set", "dashboard", "kb", "kym", "target", "tco_model", "coverage", "mixed"}
EXPECTED_AXES = {"normalize", "closure", "target", "kym", "model", "coverage", "pair", "dashboard", "kb", "git"}
EXPECTED_SEMANTIC_CONTRACT_DIGEST = "sha256:672e4ad0d56b2e37a9d099a59488915363aa8a7f73d4e004fdc3d17a3ae5827b"


def _schema() -> dict[str, Any]:
    text = resources.files("kym_tco.schemas").joinpath("gate_registry_v1.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def registry_payload_digest(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("registry_digest", None)
    return digest_value(payload)


def _assert_acyclic(gates: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(gate_id: str) -> None:
        if gate_id in visiting:
            raise ContractError(f"gate dependency cycle: {gate_id}")
        if gate_id in visited:
            return
        visiting.add(gate_id)
        for dependency in gates[gate_id]["dependencies"]:
            visit(dependency)
        visiting.remove(gate_id)
        visited.add(gate_id)

    for gate_id in gates:
        visit(gate_id)


def validate_gate_registry(document: dict[str, Any], source: Path | None = None) -> dict[str, Any]:
    errors = sorted(Draft202012Validator(_schema()).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors)
        raise ContractError(f"{source or 'gate registry'}: schema 校验失败: {detail}")
    if document["registry_digest"] != registry_payload_digest(document):
        raise ContractError("gate registry_digest 与 canonical payload 不一致")
    if document["registry_digest"] != EXPECTED_SEMANTIC_CONTRACT_DIGEST:
        raise ContractError("gate registry semantic contract digest 不匹配；禁止重新签名改变 Appendix B 语义")
    rule_ids = [rule["rule_id"] for rule in document["rules"]]
    if len(rule_ids) != len(set(rule_ids)):
        raise ContractError("gate applicability rule id 必须唯一")
    rules = set(rule_ids)
    gate_ids = [gate["gate_id"] for gate in document["gates"]]
    if len(gate_ids) != len(set(gate_ids)):
        raise ContractError("gate id 必须唯一")
    if set(gate_ids) != EXPECTED_GATE_IDS:
        missing = sorted(EXPECTED_GATE_IDS - set(gate_ids))
        unknown = sorted(set(gate_ids) - EXPECTED_GATE_IDS)
        raise ContractError(f"gate denominator 不闭合; missing={missing}; unknown={unknown}")
    gates = {gate["gate_id"]: gate for gate in document["gates"]}
    for gate in gates.values():
        if gate["applicability_rule_id"] not in rules:
            raise ContractError(f"gate rule 未声明: {gate['gate_id']}")
        if not gate["predicate_evidence"]:
            raise ContractError(f"gate predicate evidence 缺失: {gate['gate_id']}")
        unknown_dependencies = set(gate["dependencies"]) - EXPECTED_GATE_IDS
        if unknown_dependencies:
            raise ContractError(f"gate dependency 未声明: {sorted(unknown_dependencies)}")
    _assert_acyclic(gates)
    rows = document["input_kind_applicability"]
    kinds = [row["input_kind"] for row in rows]
    if len(kinds) != len(set(kinds)) or set(kinds) != EXPECTED_INPUT_KINDS:
        raise ContractError("input-kind applicability denominator 不闭合")
    for row in rows:
        axes = row["axes"]
        if set(axes) != EXPECTED_AXES:
            raise ContractError(f"applicability axes 不闭合: {row['input_kind']}")
        if any(item["rule_id"] not in rules for item in axes.values()):
            raise ContractError(f"applicability rule 未声明: {row['input_kind']}")
    return document


def load_gate_registry(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        try:
            text = resources.files("kym_tco.schemas").joinpath("gate_registry_v1.json").read_text(encoding="utf-8")
            document = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"installed gate registry unavailable: {exc}") from exc
        return validate_gate_registry(document, Path("<installed:kym_tco.schemas/gate_registry_v1.json>"))
    return validate_gate_registry(load_json(path), path)
